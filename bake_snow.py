#!/usr/bin/env python3
"""
Bluebird snow baker — Phase B, enhanced physical model.

Builds a smooth, regional, DRAMATIC snow-depth raster for the Alps and writes it as PMTiles.

Model (all driven by free data + the high-res DEM — the terrain provides the detail):
  base   = clamp(slope·(elev − snowLine), 0, cap)        # altitude profile, anchored to reports
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
import numpy as np
from scipy.interpolate import LinearNDInterpolator, NearestNDInterpolator
import rasterio
from rasterio.transform import from_bounds
from rasterio.enums import Resampling

# ---- config -----------------------------------------------------------------
BBOX = (5.8, 43.5, 12.5, 48.0)        # lon_min, lat_min, lon_max, lat_max
SAMPLE_STEP = 0.25
WINDOW_DAYS = 4                        # look-back for fresh snow + melt
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

STATE_TIF = "snow-state.tif"           # persisted depth (cm) for accumulation

# Depth-coded ramp (depth cm -> rgb) — DARKER = DEEPER. Thin snow is near-white,
# deepening through blues to dark navy-indigo. 0 cm transparent via alpha (green
# valleys come from the app's valley layer under this raster).
RAMP = [
    (0,   (250, 252, 254)),   # trace: bright white
    (10,  (232, 242, 251)),
    (40,  (170, 210, 245)),   # pale ice-blue
    (90,  (110, 170, 232)),
    (180, ( 72, 122, 210)),   # mid blue
    (300, ( 52,  78, 176)),   # deep blue-indigo
    (450, ( 34,  38, 116)),   # deepest: dark navy
]


def bake_date():
    if os.environ.get("SNOW_DATE"):
        return datetime.date.fromisoformat(os.environ["SNOW_DATE"])
    return datetime.date.today() - datetime.timedelta(days=6)   # ERA5 archive lag


# ---- 1. forcings from Open-Meteo -------------------------------------------
def _sample_grid():
    lats, lons = [], []
    lat = BBOX[1]
    while lat <= BBOX[3]:
        lon = BBOX[0]
        while lon <= BBOX[2]:
            lats.append(round(lat, 2)); lons.append(round(lon, 2)); lon += SAMPLE_STEP
        lat += SAMPLE_STEP
    return lats, lons


def _get_json(url, params, tries=4):
    """Open-Meteo rate-limits and occasionally times out; retry with backoff."""
    import requests
    last = None
    for a in range(tries):
        try:
            r = requests.get(url, params=params, timeout=180)
            if r.status_code == 429 or r.status_code >= 500:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            return r.json()
        except Exception as e:                      # noqa: BLE001
            last = e
            wait = 5 * (2 ** a)
            print(f"  retry {a + 1}/{tries} in {wait}s ({e})"); sys.stdout.flush()
            time.sleep(wait)
    raise RuntimeError(f"Open-Meteo failed after {tries} tries: {last}")


def fetch_samples(end_date):
    """-> list of (lon, lat, elev, base_cm, snowline_m, fresh_cm, freezing_m)"""
    if SELFTEST:
        return synthetic_samples(end_date)
    start = (end_date - datetime.timedelta(days=WINDOW_DAYS - 1)).isoformat()
    end = end_date.isoformat()
    lats, lons = _sample_grid()
    samples = []
    CH = 50          # was 120 — smaller chunks are far less likely to 400 / time out
    for i in range(0, len(lats), CH):
        la, lo = lats[i:i + CH], lons[i:i + CH]
        params = {
            "latitude": ",".join(map(str, la)), "longitude": ",".join(map(str, lo)),
            "start_date": start, "end_date": end,
            "hourly": "snow_depth,snowfall,freezing_level_height", "timezone": "UTC",
        }
        arr = _get_json("https://archive-api.open-meteo.com/v1/archive", params)
        arr = arr if isinstance(arr, list) else [arr]
        for j, loc in enumerate(arr):
            elev = float(loc.get("elevation", 0) or 0)
            h = loc.get("hourly", {})
            sd = [v for v in (h.get("snow_depth") or []) if v is not None] or [0]
            sf = [v for v in (h.get("snowfall") or []) if v is not None] or [0]
            fz = [v for v in (h.get("freezing_level_height") or []) if v is not None] or [elev]
            base_cm = max(0.0, sd[-1] * 100.0)       # snow_depth is metres
            fresh_cm = max(0.0, sum(sf))             # snowfall is cm
            freezing = float(np.mean(fz))
            snowline = max(0.0, freezing - 150.0)
            samples.append((lo[j], la[j], elev, base_cm, snowline, fresh_cm, freezing))
        print(f"  fetched {min(i + CH, len(lats))}/{len(lats)}"); sys.stdout.flush()
    return samples


def synthetic_samples(end_date):
    """Offline stand-in: physically plausible deep-winter Alpine forcings."""
    lats, lons = _sample_grid()
    rng = np.random.default_rng(20240215)
    out = []
    for lat, lon in zip(lats, lons):
        core = math.exp(-(((lat - 46.3) / 1.5) ** 2 + ((lon - 9.3) / 2.6) ** 2))
        elev = 250 + 2100 * core + rng.normal(0, 90)
        freezing = 1150 - 260 * (lat - 46.0) - 170 * core + rng.normal(0, 70)
        freezing = float(np.clip(freezing, 600, 2600))
        snowline = max(0.0, freezing - 150.0)
        base_cm = float(max(0.0, (elev - snowline) * 0.12 + rng.normal(0, 8)))
        fresh_cm = float(max(0.0, rng.normal(9, 6) * (0.4 + core)))
        out.append((lon, lat, float(elev), base_cm, snowline, fresh_cm, freezing))
    return out


# ---- 2. smooth param fields -------------------------------------------------
def fields(samples, grid, shape):
    """One Delaunay triangulation reused for every field (was 10 rebuilds)."""
    pts = np.array([(s[0], s[1]) for s in samples], dtype=float)
    elev_anchor = np.array([s[2] for s in samples], dtype=float)
    base_anchor = np.array([s[3] for s in samples], dtype=float)
    snowline_a = np.array([s[4] for s in samples], dtype=float)
    fresh_a = np.array([s[5] for s in samples], dtype=float)
    freezing_a = np.array([s[6] for s in samples], dtype=float)

    slope_i = np.where((elev_anchor > snowline_a) & (base_anchor > 0),
                       base_anchor / np.maximum(1.0, elev_anchor - snowline_a), 0.0)
    cap_i = np.minimum(500.0, np.maximum(base_anchor * 2.0, 80.0))

    stack = np.column_stack([snowline_a, fresh_a, freezing_a, slope_i, cap_i])
    lin = LinearNDInterpolator(pts, stack)        # triangulates ONCE
    v = lin(grid)
    bad = ~np.isfinite(v[:, 0])
    if bad.any():
        near = NearestNDInterpolator(pts, stack)
        v[bad] = near(grid[bad])
    v = v.reshape(tuple(shape) + (5,))
    return dict(snowline=v[..., 0], fresh=v[..., 1], freezing=v[..., 2],
                slope=v[..., 3], cap=v[..., 4])


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
    base = np.clip(f["slope"] * (dem - f["snowline"]), 0, f["cap"])
    aspect_factor = 1.0 + ASPECT_STRENGTH * northness
    slope_factor = 1.0 - SLOPE_SHED * np.clip((slope - 22.0) / 40.0, 0, 1)
    cold = (dem > f["snowline"]).astype(float)
    fresh = FRESH_GAIN * f["fresh"] * cold

    # temperature above freezing by altitude (positive => melt)
    t_above = LAPSE_C_PER_KM * (f["freezing"] - dem) / 1000.0
    south = np.clip(0.5 - 0.5 * northness, 0, 1)              # 0 N .. 1 S
    season = 0.7 + 0.6 * max(0.0, math.sin(math.pi * (doy - 60) / 180.0))  # peaks in spring
    melt = MELT_CM_PER_DEGDAY * WINDOW_DAYS * np.maximum(0, t_above) * (0.5 + 0.9 * south) * season

    depth = base * aspect_factor * slope_factor + fresh - melt
    return np.clip(depth, 0, f["cap"])


def save_state(depth, bounds):
    h, w = depth.shape
    tr = from_bounds(bounds.left, bounds.bottom, bounds.right, bounds.top, w, h)
    with rasterio.open(STATE_TIF, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype="float32", crs="EPSG:4326", transform=tr,
                       compress="deflate") as d:
        d.write(depth.astype("float32"), 1)


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
        depth = np.clip(STATE_BLEND * np.clip(evolved, 0, f["cap"]) +
                        (1 - STATE_BLEND) * depth, 0, f["cap"])
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
        if os.path.exists(STATE_TIF):
            with rasterio.open(STATE_TIF) as ds:
                prev = ds.read(1, out_shape=dem.shape,
                               resampling=Resampling.bilinear).astype(float)
        depth, pm = bake_one(d, dem, bounds, grid, prev, terr)
        save_state(depth, bounds)
        shutil.copy(pm, "alps-snow.pmtiles")
        json.dump({"dates": [{"date": d.isoformat(), "url": base + "alps-snow.pmtiles"}]},
                  open("snow-manifest.json", "w"))
    print("Done.")


if __name__ == "__main__":
    main()
