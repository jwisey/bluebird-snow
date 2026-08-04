#!/usr/bin/env python3
"""
Bluebird snow baker — Phase B, enhanced physical model.

Builds a smooth, regional, DRAMATIC snow-depth raster for the Alps and writes it as PMTiles.

Model (all driven by free data + the high-res DEM — the terrain provides the detail):
  base   = depth sampled from Open-Meteo at 4 elevations, interpolated by DEM height
  × aspect factor   (north faces hold snow, south faces melt)
  × slope factor    (steep sheds, bowls collect)
  + fresh snow      (recent snowfall, where it's cold enough)
  − degree-day melt (temperature by altitude × sun exposure × season)
  + day-to-day accumulation (yesterday's depth + new snow − melt)  [if prior state exists]

Forcings (free): Open-Meteo archive — snow_depth, snowfall, freezing_level_height.
Terrain (free): AWS terrarium DEM (the SAME source the app drapes its 3D mesh on, so the
                snow field registers with the terrain) with SRTM3 as a fallback.

Runs on GitHub Actions; see .github/workflows/bake-snow.yml.

Env knobs:
  SNOW_DATE      YYYY-MM-DD to pin a date (default: today − 6d, the ERA5 archive lag)
  SNOW_BACKFILL  START:END:STEP to bake a whole season carrying state through
  SNOW_SELFTEST  1 = no network: synthetic Alpine DEM + synthetic winter forcings.
                 Validates the whole pipeline offline and writes preview PNGs.
  SNOW_PREVIEW   1 = also write preview PNGs alongside the real tiles
  DEM_SOURCE     terrarium (default) | srtm
  DEM_ZOOM       terrarium tile zoom (default 9 ≈ 215 m/px; 10 ≈ 108 m/px = 4× the tiles)
"""
import os, math, datetime, subprocess, io, sys, time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
from scipy.interpolate import LinearNDInterpolator, NearestNDInterpolator
import rasterio
from rasterio.transform import from_bounds
from rasterio.enums import Resampling

# ---- config -----------------------------------------------------------------
BBOX = (5.8, 43.5, 13.0, 48.0)        # lon_min, lat_min, lon_max, lat_max
SAMPLE_STEP = 0.25
WINDOW_DAYS = 4                        # look-back for fresh snow + melt
# Open-Meteo does elevation-corrected downscaling when you pass `elevation`. Sampling the
# SAME lat/lon at several elevations recovers the real depth-vs-height profile. The old
# model fitted one straight line from the snow line through a single anchor at the model
# cell's mean elevation (~1200 m in the Alps) and extrapolated it to 3000 m+ — which badly
# under-read high ground (measured: 30 cm median vs 450 cm reported at Zermatt mid-mountain).
# 4000 m is sampled rather than extrapolated to. The first corrected bake ran without it and
# the linear extrapolation above 3250 m pushed the top of the distribution into the 800 cm
# clip (p99 691, max 800) - invented depth on exactly the high glaciated terrain the map
# makes the most of. Above the top sample the profile is now held flat: there is barely any
# Alpine terrain above 4000 m, and what there is gets wind-scoured rather than deeper.
PROFILE_ELEVS = (1000, 1750, 2500, 3250, 4000)
# Depth below this is not cover. Without it the profile's low-elevation tail left a wash of
# sub-centimetre "snow" across the valleys: the first corrected bake reported 55.8% coverage
# with p10 and p25 both rounding to 0 cm.
MIN_COVER_CM = 3.0
# Concurrent Open-Meteo requests. Their free tier allows 600/minute; this is 6.
# Concurrency for the Open-Meteo sampling. Six was too many: each of these requests carries
# 50 coordinates and takes ~16s to serve, so six at once tripped their per-minute allowance
# and run #8 died on "Minutely API request limit exceeded" after 17 minutes of retrying.
# The limit is about the WEIGHT of what is in flight, not the request count, so the fix is a
# shared minimum gap between request starts rather than just fewer threads.
SAMPLE_WORKERS = int(os.environ.get("SAMPLE_WORKERS", "3"))
MIN_REQUEST_GAP = float(os.environ.get("MIN_REQUEST_GAP", "2.0"))   # seconds, across all threads
RATE_LIMIT_WAIT = float(os.environ.get("RATE_LIMIT_WAIT", "65"))    # 429 says "try again in a minute"
# Ceiling on a SAMPLED profile depth. Open-Meteo's snow_depth over permanent ice is an
# artefact: the land model accumulates snow on a glacier cell with no ice dynamics and no
# summer floor, so it drifts upward without bound. The 2025-01-15 bake showed it plainly -
# p90 was 109 cm and p99 was 800 cm, the clip ceiling, a bimodal jump that is the high
# glaciers pinned at the limit rather than any real gradient. 450 cm is a defensible deep
# mid-winter pack on a high Alpine glacier plateau; beyond that we are drawing model drift.
MAX_PROFILE_CM = float(os.environ.get("MAX_PROFILE_CM", "450"))
DEM_MAX_WIDTH = 2400                   # only used by the SRTM fallback
MIN_ZOOM = int(os.environ.get("SNOW_MIN_ZOOM", "5"))
MAX_ZOOM = int(os.environ.get("SNOW_MAX_ZOOM", "11"))

# model knobs (tune these for more/less drama)
ASPECT_STRENGTH = float(os.environ.get("ASPECT_STRENGTH", 0.30))   # N/S contrast (0..1)
SLOPE_SHED = float(os.environ.get("SLOPE_SHED", 0.45))             # how much steep slopes lose
LAPSE_C_PER_KM = 6.5                                               # temperature lapse rate
MELT_CM_PER_DEGDAY = float(os.environ.get("MELT_CM_PER_DEGDAY", 0.45))
FRESH_GAIN = float(os.environ.get("FRESH_GAIN", 0.6))              # how much recent snowfall adds
STATE_BLEND = float(os.environ.get("STATE_BLEND", 0.65))           # yesterday's state vs model

SELFTEST = os.environ.get("SNOW_SELFTEST") == "1"
PREVIEW = SELFTEST or os.environ.get("SNOW_PREVIEW") == "1"
DEM_SOURCE = os.environ.get("DEM_SOURCE", "terrarium")
DEM_ZOOM = int(os.environ.get("DEM_ZOOM", "9"))

# Stamped into snow-state.tif. The daily run blends STATE_BLEND (0.65) of yesterday's
# persisted depth into today's model output. That is right for continuity between runs of
# the SAME model and completely wrong across a model change: carrying the old single-anchor
# state into the profile model dilutes the correction back to about a third of itself and
# hides the fix. Bump this whenever the depth model changes; state written by any other
# version is ignored rather than blended in.
MODEL_VERSION = "2026-08-04-profile-4000m-capped"
IGNORE_STATE = os.environ.get("SNOW_IGNORE_STATE") == "1"

STATE_TIF = "snow-state.tif"           # persisted depth (cm) for accumulation

# Depth-coded ramp (depth cm -> rgb) - DARKER = DEEPER. Thin snow is near-white,
# deepening through blues to dark navy-indigo. 0 cm transparent via alpha (green
# valleys come from the app's valley layer under this raster).
#
# FITTED, not guessed. Stops sit on the measured percentiles of the 2025-01-15 bake
# (551-point sample, terrarium z9 DEM):
#     p10 7, p25 17, median 38, p75 67, p90 109 cm, over 32.1% coverage
# That is equal-frequency binning up to p90, so the colour actually varies where the data
# is instead of most of the map sitting in one band. The last two stops cover the glacier
# tail up to MAX_PROFILE_CM.
#
# TO RE-FIT after a model change: read the DEPTH line out of the bake log and move the
# stops onto the new percentiles. Do not guess.
RAMP = [
    (3,   (250, 252, 254)),   # cover floor: bright white
    (7,   (236, 244, 252)),   # p10
    (17,  (214, 233, 250)),   # p25 - pale ice-blue
    (38,  (170, 207, 245)),   # median
    (67,  (122, 175, 235)),   # p75 - mid blue
    (109, ( 80, 132, 214)),   # p90
    (200, ( 52,  84, 180)),   # deep indigo
    (450, ( 34,  38, 116)),   # glacier ceiling: dark navy
]


def bake_date():
    if os.environ.get("SNOW_DATE"):
        return datetime.date.fromisoformat(os.environ["SNOW_DATE"])
    return datetime.date.today() - datetime.timedelta(days=6)   # ERA5 archive lag


# ---- 1. forcings from Open-Meteo -------------------------------------------
class _RateLimited(Exception):
    """HTTP 429 - over the per-minute allowance, distinct from a network hiccup."""


def _sample_grid():
    lats, lons = [], []
    lat = BBOX[1]
    while lat <= BBOX[3]:
        lon = BBOX[0]
        while lon <= BBOX[2]:
            lats.append(round(lat, 2)); lons.append(round(lon, 2)); lon += SAMPLE_STEP
        lat += SAMPLE_STEP
    return lats, lons


_rate_lock = threading.Lock()
_last_request = [0.0]


def _throttle():
    """Space request STARTS at least MIN_REQUEST_GAP apart, across every worker thread.
    Sleeping while holding the lock is deliberate: it makes threads queue in turn instead of
    all waking together and bursting, which is what got us rate-limited in the first place."""
    with _rate_lock:
        wait = _last_request[0] + MIN_REQUEST_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.monotonic()


def _get_json(url, params, tries=6):
    """Open-Meteo rate-limits and occasionally times out; throttle, then retry with backoff.

    A 429 is not a transient blip - it means we are over the per-minute allowance and so is
    every other request in flight. Backing off 5s and trying again just burns a retry, so a
    429 waits out the full minute the API asks for. Timeouts keep the exponential ladder."""
    import requests
    last = None
    for a in range(tries):
        _throttle()
        try:
            r = requests.get(url, params=params, timeout=180)
            if r.status_code == 429:
                raise _RateLimited(r.text[:160])
            if r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            return r.json()
        except _RateLimited as e:
            last = e
            print(f"  rate-limited, waiting {RATE_LIMIT_WAIT:.0f}s ({a + 1}/{tries})")
            sys.stdout.flush()
            time.sleep(RATE_LIMIT_WAIT)
        except Exception as e:                      # noqa: BLE001
            last = e
            wait = 5 * (2 ** a)
            print(f"  retry {a + 1}/{tries} in {wait}s ({e})"); sys.stdout.flush()
            time.sleep(wait)
    raise RuntimeError(f"Open-Meteo failed after {tries} tries: {last}")


def fetch_samples(end_date):
    """-> list of (lon, lat, snowline_m, fresh_cm, freezing_m, *depths)
    where depths[N] = snow depth (cm) at PROFILE_ELEVS[N]."""
    if SELFTEST:
        return synthetic_samples(end_date)
    start = (end_date - datetime.timedelta(days=WINDOW_DAYS - 1)).isoformat()
    end = end_date.isoformat()
    lats, lons = _sample_grid()
    n = len(lats)
    CH = 50
    ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

    # These 72 requests used to run one after another and took 19 of the bake's 25 minutes,
    # which made every model tweak a 25-minute round trip. They are independent, so run them
    # concurrently. Open-Meteo's free tier allows 600 calls/minute; 6 in flight is nowhere
    # near it, and keeps us a polite neighbour rather than the reason they add a limit.
    prof = [[0.0] * n for _ in PROFILE_ELEVS]
    fresh = [0.0] * n
    freez = [0.0] * n

    def depth_job(k, E, i):
        la, lo = lats[i:i + CH], lons[i:i + CH]
        arr = _get_json(ARCHIVE, {
            "latitude": ",".join(map(str, la)), "longitude": ",".join(map(str, lo)),
            "elevation": ",".join([str(E)] * len(la)),
            "start_date": end, "end_date": end,
            "hourly": "snow_depth", "timezone": "UTC",
        })
        arr = arr if isinstance(arr, list) else [arr]
        for j, loc in enumerate(arr):
            sd = [v for v in ((loc.get("hourly") or {}).get("snow_depth") or [])
                  if v is not None] or [0]
            prof[k][i + j] = min(MAX_PROFILE_CM, max(0.0, sd[-1] * 100.0))   # metres -> cm

    def forcing_job(i):
        la, lo = lats[i:i + CH], lons[i:i + CH]
        arr = _get_json(ARCHIVE, {
            "latitude": ",".join(map(str, la)), "longitude": ",".join(map(str, lo)),
            "start_date": start, "end_date": end,
            "hourly": "snowfall,freezing_level_height", "timezone": "UTC",
        })
        arr = arr if isinstance(arr, list) else [arr]
        for j, loc in enumerate(arr):
            h = loc.get("hourly") or {}
            sf = [v for v in (h.get("snowfall") or []) if v is not None] or [0]
            fz = [v for v in (h.get("freezing_level_height") or []) if v is not None] or [0]
            fresh[i + j] = max(0.0, sum(sf))                  # snowfall is cm
            freez[i + j] = float(np.mean(fz))

    jobs = [(depth_job, (k, E, i))
            for k, E in enumerate(PROFILE_ELEVS) for i in range(0, n, CH)]
    jobs += [(forcing_job, (i,)) for i in range(0, n, CH)]
    print(f"sampling {n} points: {len(jobs)} requests, {SAMPLE_WORKERS} at a time")
    sys.stdout.flush()
    done = 0
    with ThreadPoolExecutor(max_workers=SAMPLE_WORKERS) as pool:
        futures = [pool.submit(fn, *args) for fn, args in jobs]
        for f in as_completed(futures):
            f.result()          # re-raises; _get_json has already retried 4 times
            done += 1
            if done % 12 == 0 or done == len(jobs):
                print(f"  {done}/{len(jobs)}"); sys.stdout.flush()

    out = []
    for i in range(n):
        # snowline is kept ONLY for the melt term now. Snow COVER comes from the depth
        # profile going to zero — freezing level is where precip falls as snow today,
        # not where snow lies, and using it as the cover boundary erased lying snow on
        # mild days (Zermatt showed a 450 cm base above a bare green mountain).
        out.append((lons[i], lats[i], max(0.0, freez[i] - 150.0), fresh[i], freez[i],
                    *[prof[k][i] for k in range(len(PROFILE_ELEVS))]))
    return out


def synthetic_samples(end_date):
    """Offline stand-in: physically plausible deep-winter Alpine forcings, sampled as a
    CONVEX depth-vs-elevation profile so SELFTEST exercises the same code path as live."""
    lats, lons = _sample_grid()
    rng = np.random.default_rng(20240215)
    out = []
    for lat, lon in zip(lats, lons):
        core = math.exp(-(((lat - 46.3) / 1.5) ** 2 + ((lon - 9.3) / 2.6) ** 2))
        freezing = 1150 - 260 * (lat - 46.0) - 170 * core + rng.normal(0, 70)
        freezing = float(np.clip(freezing, 600, 2600))
        snowline = max(0.0, freezing - 150.0)
        prof = []
        for E in PROFILE_ELEVS:
            # convex: little at the snow line, accelerating with height
            above = max(0.0, E - snowline)
            d = (above ** 1.32) * 0.0055 * (0.55 + 0.9 * core) + rng.normal(0, 4)
            prof.append(float(max(0.0, d)))
        fresh = float(max(0.0, rng.normal(9, 6) * (0.4 + core)))
        out.append((lon, lat, snowline, fresh, freezing, *prof))
    return out


# ---- 2. smooth param fields -------------------------------------------------
def fields(samples, grid, shape):
    """One Delaunay triangulation reused for every field.
    Returns the depth profile (one level per PROFILE_ELEVS) plus the forcings, on the DEM
    grid. Sized off PROFILE_ELEVS rather than hard-coded, so adding a sample elevation is a
    one-line change instead of four."""
    np_lv = len(PROFILE_ELEVS)
    ncol = 3 + np_lv                             # snowline, fresh, freezing, then the profile
    pts = np.array([(s[0], s[1]) for s in samples], dtype=float)
    cols = np.array([[s[2], s[3], s[4], *s[5:5 + np_lv]] for s in samples], dtype=float)

    lin = LinearNDInterpolator(pts, cols)        # triangulates ONCE
    v = lin(grid)
    bad = ~np.isfinite(v[:, 0])
    if bad.any():
        v[bad] = NearestNDInterpolator(pts, cols)(grid[bad])
    v = v.reshape(tuple(shape) + (ncol,))
    return dict(snowline=v[..., 0], fresh=v[..., 1], freezing=v[..., 2],
                profile=np.clip(v[..., 3:ncol], 0, None))


def profile_depth(dem, profile):
    """Piecewise-linear depth from the sampled elevation profile, per pixel.
    Replaces the old single-anchor straight-line extrapolation."""
    E = [float(e) for e in PROFILE_ELEVS]
    p = [profile[..., k] for k in range(len(E))]
    d = np.zeros_like(dem)

    lo_slope = (p[1] - p[0]) / (E[1] - E[0])
    d = np.where(dem <= E[0], p[0] + lo_slope * (dem - E[0]), d)
    for k in range(len(E) - 1):
        m = (dem > E[k]) & (dem <= E[k + 1])
        t = (dem - E[k]) / (E[k + 1] - E[k])
        d = np.where(m, p[k] * (1 - t) + p[k + 1] * t, d)
    # Above the highest SAMPLED elevation, hold flat. Extrapolating the 3250->4000 m slope
    # out to Mont Blanc is invention, and the first corrected bake showed exactly that: the
    # top of the distribution ran into the 800 cm clip.
    d = np.where(dem > E[-1], p[-1], d)
    return np.clip(d, 0, 800)


# ---- 3. DEM + terrain derivatives ------------------------------------------
class _B:                                   # tiny bounds shim (matches rasterio's fields)
    def __init__(self, l, b, r, t):
        self.left, self.bottom, self.right, self.top = l, b, r, t


def _terrarium_dem(zoom):
    """Mosaic AWS terrarium tiles — the SAME DEM the app drapes its 3D mesh on."""
    import requests
    from PIL import Image
    n = 2 ** zoom

    def x_of(lon):
        return (lon + 180.0) / 360.0 * n

    def y_of(lat):
        r = math.radians(lat)
        return (1.0 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2.0 * n

    x0, x1 = int(math.floor(x_of(BBOX[0]))), int(math.ceil(x_of(BBOX[2])))
    y0, y1 = int(math.floor(y_of(BBOX[3]))), int(math.ceil(y_of(BBOX[1])))
    tw, th = (x1 - x0), (y1 - y0)
    print(f"terrarium z{zoom}: {tw}x{th} = {tw * th} tiles -> {tw * 256}x{th * 256} px")
    mosaic = np.zeros((th * 256, tw * 256), dtype=np.float32)
    sess = requests.Session()
    missing = 0
    for ty in range(y0, y1):
        for tx in range(x0, x1):
            url = (f"https://s3.amazonaws.com/elevation-tiles-prod/terrarium/"
                   f"{zoom}/{tx}/{ty}.png")
            px = None
            for a in range(3):
                try:
                    r = sess.get(url, timeout=60)
                    if r.status_code == 404:
                        break
                    r.raise_for_status()
                    px = np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"),
                                    dtype=np.float32)
                    break
                except Exception as e:                  # noqa: BLE001
                    if a == 2:
                        print(f"  tile {zoom}/{tx}/{ty} failed: {e}")
                    time.sleep(2 * (a + 1))
            if px is None:
                missing += 1
                continue
            elev = px[:, :, 0] * 256.0 + px[:, :, 1] + px[:, :, 2] / 256.0 - 32768.0
            mosaic[(ty - y0) * 256:(ty - y0 + 1) * 256,
                   (tx - x0) * 256:(tx - x0 + 1) * 256] = elev
        print(f"  row {ty - y0 + 1}/{th}"); sys.stdout.flush()
    if missing > tw * th * 0.2:
        raise RuntimeError(f"{missing}/{tw * th} terrarium tiles missing")

    def lon_of(x):
        return x / n * 360.0 - 180.0

    def lat_of(y):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))

    return mosaic.astype(float), _B(lon_of(x0), lat_of(y1), lon_of(x1), lat_of(y0))


def _srtm_dem():
    import elevation
    tif = os.path.abspath("alps-dem.tif")
    print("Downloading + clipping SRTM DEM ...")
    elevation.clip(bounds=BBOX, output=tif, product="SRTM3")   # ~90 m
    with rasterio.open(tif) as ds:
        scale = min(1.0, DEM_MAX_WIDTH / ds.width)
        w, h = int(ds.width * scale), int(ds.height * scale)
        dem = ds.read(1, out_shape=(h, w), resampling=Resampling.bilinear).astype(float)
        bounds = ds.bounds
    return dem, bounds


def _synthetic_dem(width=1400):
    """Ridged-multifractal Alps stand-in for offline validation."""
    from scipy.ndimage import map_coordinates, gaussian_filter
    h = int(width * (BBOX[3] - BBOX[1]) / (BBOX[2] - BBOX[0]) / 0.7)
    rng = np.random.default_rng(7)
    field = np.zeros((h, width))
    amp, cells = 1.0, 3
    for _ in range(7):
        g = rng.normal(0, 1, (max(3, cells), max(3, int(cells * 1.5))))
        Y, X = np.meshgrid(np.linspace(0, g.shape[0] - 1, h),
                           np.linspace(0, g.shape[1] - 1, width), indexing="ij")
        layer = map_coordinates(g, [Y, X], order=3, mode="reflect")
        m = np.abs(layer).max() + 1e-9
        field += amp * (1.0 - np.abs(layer / m))          # ridged
        amp *= 0.5; cells = int(cells * 2.1)
    field = gaussian_filter(field, 1.0)
    field = (field - field.min()) / (np.ptp(field) + 1e-9)
    lat = np.linspace(BBOX[3], BBOX[1], h)[:, None]
    lon = np.linspace(BBOX[0], BBOX[2], width)[None, :]
    core = np.exp(-(((lat - 46.3) / 1.6) ** 2 + ((lon - 9.3) / 2.8) ** 2))
    dem = 180 + 4400 * (field ** 2.2) * (0.25 + 0.95 * core)
    return dem, _B(BBOX[0], BBOX[1], BBOX[2], BBOX[3])


def load_dem():
    if SELFTEST:
        print("SELFTEST: synthetic Alpine DEM (no network)")
        dem, bounds = _synthetic_dem()
    elif DEM_SOURCE == "terrarium":
        try:
            dem, bounds = _terrarium_dem(DEM_ZOOM)
        except Exception as e:                       # noqa: BLE001
            print(f"terrarium DEM failed ({e}); falling back to SRTM3")
            dem, bounds = _srtm_dem()
    else:
        dem, bounds = _srtm_dem()
    dem = np.asarray(dem, dtype=float)
    dem[~np.isfinite(dem)] = 0.0
    dem[dem < -1000] = 0.0
    print(f"DEM {dem.shape[1]}x{dem.shape[0]}  elev {dem.min():.0f}..{dem.max():.0f} m")
    return dem, bounds


def terrain(dem, bounds):
    h, w = dem.shape
    lat_mid = (bounds.top + bounds.bottom) / 2.0
    dx = (bounds.right - bounds.left) / w * 111320.0 * math.cos(math.radians(lat_mid))
    dy = (bounds.top - bounds.bottom) / h * 110540.0
    gy, gx = np.gradient(dem, dy, dx)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    aspect = np.degrees(np.arctan2(-gx, gy)) % 360.0          # 0 = N
    northness = np.cos(np.radians(aspect))                    # +1 N, -1 S
    return slope, northness


# ---- 4. the model -----------------------------------------------------------
def model(dem, f, slope, northness, doy):
    # Base depth now comes from the SAMPLED profile rather than a line through one anchor.
    base = profile_depth(dem, f["profile"])
    aspect_factor = 1.0 + ASPECT_STRENGTH * northness
    slope_factor = 1.0 - SLOPE_SHED * np.clip((slope - 22.0) / 40.0, 0, 1)

    # Fresh snow lands where it is cold enough — freezing level is the right variable
    # for THIS, unlike for snow cover.
    fresh = FRESH_GAIN * f["fresh"] * (dem > f["freezing"] - 150.0).astype(float)

    t_above = LAPSE_C_PER_KM * (f["freezing"] - dem) / 1000.0
    south = np.clip(0.5 - 0.5 * northness, 0, 1)              # 0 N .. 1 S
    season = 0.7 + 0.6 * max(0.0, math.sin(math.pi * (doy - 60) / 180.0))  # peaks in spring
    melt = MELT_CM_PER_DEGDAY * WINDOW_DAYS * np.maximum(0, t_above) * (0.5 + 0.9 * south) * season

    depth = base * aspect_factor * slope_factor + fresh - melt
    depth = np.clip(depth, 0, 800)
    # A millimetre of modelled snow is not cover. Cutting it here keeps the coverage figure
    # honest and stops the valleys picking up a faint tint from the profile's low-end tail.
    return np.where(depth < MIN_COVER_CM, 0.0, depth)


def save_state(depth, bounds):
    h, w = depth.shape
    tr = from_bounds(bounds.left, bounds.bottom, bounds.right, bounds.top, w, h)
    with rasterio.open(STATE_TIF, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype="float32", crs="EPSG:4326", transform=tr,
                       compress="deflate") as d:
        d.write(depth.astype("float32"), 1)
        d.update_tags(model_version=MODEL_VERSION)


# ---- 5. colourise + tile ----------------------------------------------------
def colourise(depth, sparkle):
    h, w = depth.shape
    rgba = np.zeros((4, h, w), dtype=np.uint8)
    xs = [r[0] for r in RAMP]
    r0 = np.interp(depth, xs, [r[1][0] for r in RAMP])
    g0 = np.interp(depth, xs, [r[1][1] for r in RAMP])
    b0 = np.interp(depth, xs, [r[1][2] for r in RAMP])
    sp = np.clip(sparkle, 0, 1) * 0.6                     # fresh snow blends toward bright white
    r0 = r0 * (1 - sp) + 240 * sp
    g0 = g0 * (1 - sp) + 245 * sp
    b0 = b0 * (1 - sp) + 255 * sp
    rgba[0] = r0.astype(np.uint8); rgba[1] = g0.astype(np.uint8); rgba[2] = b0.astype(np.uint8)
    rgba[3] = (np.clip(depth / 20.0, 0, 1) * 0.80 * 255).astype(np.uint8)
    return rgba


def write_preview(rgba, depth, dem, tag):
    """Flat PNG + a hillshade-composited PNG that approximates the app's wrap pass."""
    from PIL import Image
    a = rgba[3:4].astype(float) / 255.0
    rgb = rgba[:3].astype(float)
    gy, gx = np.gradient(dem)
    s = np.hypot(gx, gy).std() * 4 + 1e-9
    sh = np.clip(0.55 + 0.9 * (gx * 0.6 - gy * 0.8) / s, 0.3, 1.55)
    rock = np.stack([np.full(dem.shape, 226.0), np.full(dem.shape, 231.0),
                     np.full(dem.shape, 228.0)])
    green = np.stack([np.full(dem.shape, 138.0), np.full(dem.shape, 176.0),
                      np.full(dem.shape, 126.0)])
    low = np.clip((1250.0 - dem) / 950.0, 0, 1)
    ground = rock * (1 - low) + green * low
    comp = np.clip((rgb * a + ground * (1 - a)) * sh, 0, 255).astype(np.uint8)
    Image.fromarray(np.transpose(rgba, (1, 2, 0)), "RGBA").save(f"preview-flat-{tag}.png")
    Image.fromarray(np.transpose(comp, (1, 2, 0)), "RGB").save(f"preview-{tag}.png")
    pos = depth[depth > 0]
    print(f"  preview-{tag}.png  depth {depth.min():.0f}..{depth.max():.0f} cm, "
          f"mean-where-snow {pos.mean() if pos.size else 0:.0f} cm, "
          f"covered {100.0 * pos.size / depth.size:.0f}%")


def bake_one(d, dem, bounds, grid, prev, terr):
    doy = d.timetuple().tm_yday
    samples = fetch_samples(d)
    f = fields(samples, grid, dem.shape)
    slope, northness = terr                       # computed once, not per date
    depth = model(dem, f, slope, northness, doy)
    if prev is not None:
        evolved = prev + FRESH_GAIN * f["fresh"] * (depth > 0)
        depth = np.clip(STATE_BLEND * np.clip(evolved, 0, 800) +
                        (1 - STATE_BLEND) * depth, 0, 800)
    # Log the distribution every run: the colour ramp is fitted to these numbers, so they
    # need to be readable straight off the CI log rather than requiring a tile download.
    pos = depth[depth > 0]
    if pos.size:
        q = np.percentile(pos, [10, 25, 50, 75, 90, 99])
        print(f"DEPTH {d.isoformat()}  covered {100.0 * pos.size / depth.size:.1f}%  "
              f"p10 {q[0]:.0f}  p25 {q[1]:.0f}  median {q[2]:.0f}  p75 {q[3]:.0f}  "
              f"p90 {q[4]:.0f}  p99 {q[5]:.0f}  max {depth.max():.0f} cm")
    else:
        print(f"DEPTH {d.isoformat()}  no snow anywhere - check the sampling.")
    sys.stdout.flush()
    sparkle = np.clip(f["fresh"] / 15.0, 0, 1) * (depth > 0)
    rgba = colourise(depth, sparkle)
    tag = d.strftime("%Y%m%d")
    if PREVIEW:
        write_preview(rgba, depth, dem, tag)
    tif, mb, pm = f"snow-{tag}.tif", f"snow-{tag}.mbtiles", f"alps-snow-{tag}.pmtiles"
    h, w = dem.shape
    tr = from_bounds(bounds.left, bounds.bottom, bounds.right, bounds.top, w, h)
    with rasterio.open(tif, "w", driver="GTiff", height=h, width=w, count=4,
                       dtype="uint8", crs="EPSG:4326", transform=tr,
                       compress="deflate") as dst:
        dst.write(rgba)
        dst.colorinterp = [rasterio.enums.ColorInterp.red, rasterio.enums.ColorInterp.green,
                           rasterio.enums.ColorInterp.blue, rasterio.enums.ColorInterp.alpha]
    # rio-mbtiles 1.6.0 bug: in --overwrite mode it only binds its internal
    # `appending` flag when the output ALREADY exists, so a fresh run dies with
    # "NameError: cannot access free variable 'appending'". Touching the file
    # first takes the branch that binds it. Harmless on fixed versions.
    open(mb, "a").close()
    # --rgba is REQUIRED. Without it rio-mbtiles drops the alpha band and the snow
    # becomes an OPAQUE sheet over the whole map, including the 0 cm areas.
    subprocess.run(["rio", "mbtiles", tif, mb, "--zoom-levels",
                    f"{MIN_ZOOM}..{MAX_ZOOM}", "--format", "PNG", "--rgba",
                    "--overwrite"], check=True)
    if os.path.exists(pm):
        os.remove(pm)
    subprocess.run(["pmtiles", "convert", mb, pm], check=True)
    print(f"  baked {pm} ({os.path.getsize(pm) / 1e6:.1f} MB)")
    return depth, pm


def main():
    import json, shutil
    dem, bounds = load_dem()
    h, w = dem.shape
    m_per_px = (bounds.right - bounds.left) / w * 111320.0 * math.cos(math.radians(46))
    honest = max(0, math.floor(math.log2(156543.0 * math.cos(math.radians(46)) / m_per_px)))
    print(f"DEM ≈ {m_per_px:.0f} m/px -> honest max zoom ≈ z{honest} (baking to z{MAX_ZOOM})")
    if MAX_ZOOM > honest + 1:
        print(f"  NOTE: z{MAX_ZOOM} upsamples blurry data and multiplies tile count. "
              f"Raise DEM_ZOOM instead of SNOW_MAX_ZOOM for real detail.")

    lon = np.linspace(bounds.left, bounds.right, w)
    lat = np.linspace(bounds.top, bounds.bottom, h)
    LON, LAT = np.meshgrid(lon, lat)
    grid = np.column_stack([LON.ravel(), LAT.ravel()])
    terr = terrain(dem, bounds)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    base = f"https://github.com/{repo}/releases/download/snow/" if repo else ""

    backfill = os.environ.get("SNOW_BACKFILL")    # "YYYY-MM-DD:YYYY-MM-DD:stepdays"
    if backfill:
        start, end, step = backfill.split(":")
        d1 = datetime.date.fromisoformat(end); st = int(step)
        prev, manifest, d = None, [], datetime.date.fromisoformat(start)
        while d <= d1:
            print(f"Backfill {d}")
            prev, pm = bake_one(d, dem, bounds, grid, prev, terr)
            manifest.append({"date": d.isoformat(), "url": base + pm})
            d += datetime.timedelta(days=st)
        json.dump({"dates": manifest}, open("snow-manifest.json", "w"))
        if manifest:
            shutil.copy(os.path.basename(manifest[-1]["url"]), "alps-snow.pmtiles")
            save_state(prev, bounds)          # so the next daily run continues the season
        print(f"Backfilled {len(manifest)} dates -> snow-manifest.json")
    else:
        d = bake_date()
        prev = None
        if IGNORE_STATE:
            print("SNOW_IGNORE_STATE=1 - baking from the model alone, no carried state.")
        elif os.path.exists(STATE_TIF):
            with rasterio.open(STATE_TIF) as ds:
                got = ds.tags().get("model_version")
                if got == MODEL_VERSION:
                    prev = ds.read(1, out_shape=dem.shape,
                                   resampling=Resampling.bilinear).astype(float)
                    print(f"Carried snow state from {STATE_TIF} (model {got}).")
                else:
                    print(f"Ignoring {STATE_TIF}: written by model '{got}', this is "
                          f"'{MODEL_VERSION}'. Blending it would drag the new depths "
                          f"back toward the old ones.")
        depth, pm = bake_one(d, dem, bounds, grid, prev, terr)
        save_state(depth, bounds)
        shutil.copy(pm, "alps-snow.pmtiles")
        json.dump({"dates": [{"date": d.isoformat(), "url": base + "alps-snow.pmtiles"}]},
                  open("snow-manifest.json", "w"))
    print("Done.")


if __name__ == "__main__":
    main()
