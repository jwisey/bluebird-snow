#!/usr/bin/env python3
"""
Bluebird piste baker - every ski run and lift in the Alps as a vector PMTiles layer.

WHY: the app's "Terrain & difficulty" section had a grey rectangle labelled "Piste map", and
the main map shows snow on terrain but no actual skiing. OpenStreetMap has the runs - mapped
as `piste:type=downhill` ways carrying `piste:difficulty` - and the lifts as `aerialway=*`.
That is the same data OpenSkiMap is built from, it is free, and it needs no key.

Output: alps-pistes.pmtiles with two vector layers
  pistes  - LineStrings, properties: difficulty (novice/easy/intermediate/advanced/expert),
            name, ref, gladed, lit
  lifts   - LineStrings, properties: kind (gondola/chair_lift/...), name

Zoom 9-14. Below z9 a piste is thinner than a pixel and the tiles are pure weight; above z14
MapLibre overzooms the z14 tile, which for line geometry is visually identical and free.

Env knobs:
  PISTE_MIN_ZOOM / PISTE_MAX_ZOOM   default 9 / 14
  PISTE_SELFTEST=1                  synthetic geometry, no network - exercises the whole
                                    GeoJSON -> tippecanoe -> pmtiles path offline
"""
import datetime
import json, math, os, subprocess, sys, time, urllib.parse, urllib.request

BBOX = (5.8, 43.5, 13.0, 48.0)          # lon_min, lat_min, lon_max, lat_max - matches bake_snow
TILE = 0.5                              # degrees per Overpass query
CACHE = ".osm-piste-cache"
MIN_ZOOM = int(os.environ.get("PISTE_MIN_ZOOM", "9"))
MAX_ZOOM = int(os.environ.get("PISTE_MAX_ZOOM", "14"))
SELFTEST = os.environ.get("PISTE_SELFTEST") == "1"
OUT = "alps-pistes.pmtiles"
UA = "bluebird-piste-baker/1.0 (jacob.wise2@gmail.com)"

# Tiles Overpass never gave us. A failed tile is a HOLE in the piste layer - a resort with
# no runs drawn, indistinguishable from a resort that has none. Watching the terrain script
# run showed 504s and 429s are routine on the public instances, so this is tracked and the
# bake refuses to publish if too much is missing rather than shipping a map with gaps.
FAILED = []

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]

# The piste grades OSM uses, normalised. Anything unrecognised becomes "unknown" and is drawn
# grey rather than silently coloured as if we knew.
DIFFICULTIES = {"novice", "easy", "intermediate", "advanced", "expert", "freeride", "extreme"}
LIFT_TYPES = {
    "cable_car", "gondola", "mixed_lift", "chair_lift", "drag_lift", "t-bar", "j-bar",
    "platter", "rope_tow", "magic_carpet", "funicular",
}


def run(cmd):
    print("+", " ".join(cmd)); sys.stdout.flush()
    subprocess.run(cmd, check=True)


# How far outside a resort's own tile we still fetch. 0.25 degrees is about 28 km here, so a
# resort sitting near a tile edge still gets the neighbouring tile and its runs are not cut in
# half. Bigger buffers cost real time for terrain nobody in this app will look at.
NEAR_RESORT_DEG = 0.25


_TILES = None


def tiles():
    """Only the tiles that can contain a run we will ever draw. Computed once.

    The full bounding box is 135 tiles, and most of them are the Po plain, the Rhone valley
    or the outskirts of Munich - flat ground with no pistes on it, each still costing a full
    Overpass geometry query. The first real run spent three hours on that and was killed by
    the job timeout before it wrote anything. Filtering to tiles near one of the 70 resorts
    cuts it to about 57, which is what the terrain script has always fetched.

    The honest consequence: this layer covers the resorts the app knows about, not every
    piste in the Alps. Pan far enough from a resort and the runs stop.
    """
    global _TILES
    if _TILES is not None:
        return _TILES
    with open("resort_coords.json") as f:
        resorts = json.load(f)
    out, skipped = [], 0
    lat = math.floor(BBOX[1] / TILE) * TILE
    while lat < BBOX[3]:
        lon = math.floor(BBOX[0] / TILE) * TILE
        while lon < BBOX[2]:
            s_, w_ = round(lat, 2), round(lon, 2)
            n_, e_ = s_ + TILE, w_ + TILE
            b = NEAR_RESORT_DEG
            if any(s_ - b <= r["lat"] <= n_ + b and w_ - b <= r["lon"] <= e_ + b
                   for r in resorts):
                out.append((s_, w_))
            else:
                skipped += 1
            lon += TILE
        lat += TILE
    print(f"{len(out)} tiles to fetch, {skipped} skipped as having no resort within "
          f"{NEAR_RESORT_DEG} degrees", flush=True)
    _TILES = out
    return out


def healthy(d):
    """True if this is a real Overpass answer rather than a broken replica.

    A mirror serving an uninitialised database answers HTTP 200, with well-formed JSON and
    ZERO elements, stamping `timestamp_osm_base` with a bare sequence number ("116149")
    instead of an ISO date. No exception is raised, so no retry fires and the empty answer
    is treated as truth. In the terrain run this silently erased the pistes of Cortina,
    Alpe d'Huez, Zell am See, Kaprun and Saalbach. Here it would punch holes in the map.
    """
    stamp = (d.get("osm3s") or {}).get("timestamp_osm_base") or ""
    try:
        datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def fetch_tile(s, w, tries=4):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{s:.2f}_{w:.2f}.json")
    if os.path.exists(path) and os.path.getsize(path) > 2:
        with open(path) as f:
            return json.load(f)
    n, e = s + TILE, w + TILE
    q = f"""
[out:json][timeout:120];
(
  way["piste:type"="downhill"]({s},{w},{n},{e});
  way["aerialway"]({s},{w},{n},{e});
);
out geom tags;
"""
    last = None
    for attempt in range(tries):
        url = OVERPASS[attempt % len(OVERPASS)]
        try:
            req = urllib.request.Request(
                url, data=urllib.parse.urlencode({"data": q}).encode(),
                headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.loads(r.read().decode())
            if not healthy(d):
                raise RuntimeError(
                    f"{url} answered 200 but from a broken replica "
                    f"(timestamp_osm_base="
                    f"{(d.get('osm3s') or {}).get('timestamp_osm_base')!r}, "
                    f"{len(d.get('elements', []))} elements) - not caching it")
            # Only validated answers reach the cache. A cached bad answer poisons every
            # later run, which is worse than the original failure.
            with open(path, "w") as f:
                json.dump(d, f)
            return d
        except Exception as ex:                                  # noqa: BLE001
            last = ex
            wait = 10 * (attempt + 1)
            print(f"    {type(ex).__name__}: {ex} - retrying in {wait}s", flush=True)
            time.sleep(wait)
    print(f"    GAVE UP on tile {s},{w}: {last}", flush=True)
    FAILED.append((s, w))
    return {"elements": []}


def normalise_difficulty(tags):
    d = (tags.get("piste:difficulty") or "").strip().lower()
    if d in ("freeride", "extreme"):
        return "expert"
    return d if d in DIFFICULTIES else "unknown"


def collect():
    """-> (piste features, lift features). Deduped by OSM way id across overlapping tiles."""
    if SELFTEST:
        return synthetic()
    pistes, lifts, seen = [], [], set()
    tl = tiles()
    print(f"caching in {CACHE}/")
    # The job is capped at 180 minutes and the tiling and upload still have to happen after
    # this. Stop fetching at 120 so the run ends with a message naming the tiles it never
    # got, rather than being killed silently three hours in with nothing written.
    budget = float(os.environ.get("PISTE_FETCH_BUDGET_MIN", "120")) * 60
    started = time.monotonic()
    for i, (s, w) in enumerate(tl, 1):
        if time.monotonic() - started > budget:
            print(f"\nfetch budget of {budget/60:.0f} min spent at tile {i} of {len(tl)} - "
                  f"stopping here rather than being killed mid-bake", flush=True)
            FAILED.extend(tl[i - 1:])
            break
        t0 = time.monotonic()
        d = fetch_tile(s, w)
        got_p = got_l = 0
        for el in d.get("elements", []):
            if el.get("id") in seen:
                continue
            geom = el.get("geometry") or []
            if len(geom) < 2:
                continue
            seen.add(el["id"])
            tags = el.get("tags", {})
            coords = [[round(p["lon"], 5), round(p["lat"], 5)] for p in geom]
            if tags.get("aerialway") in LIFT_TYPES:
                lifts.append({
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": coords},
                    "properties": {"kind": tags["aerialway"], "name": tags.get("name", "")},
                })
                got_l += 1
            elif tags.get("piste:type") == "downhill":
                pistes.append({
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": coords},
                    "properties": {
                        "difficulty": normalise_difficulty(tags),
                        "name": tags.get("piste:name") or tags.get("name", ""),
                        "ref": tags.get("piste:ref") or tags.get("ref", ""),
                        "gladed": 1 if tags.get("gladed") == "yes" else 0,
                        "lit": 1 if tags.get("lit") == "yes" else 0,
                    },
                })
                got_p += 1
        print(f"[{i}/{len(tl)}] {s:.1f},{w:.1f}  +{got_p} pistes +{got_l} lifts "
              f"(running {len(pistes)}/{len(lifts)})  {time.monotonic() - t0:.0f}s"
              f"  [{(time.monotonic() - started) / 60:.0f}/{budget / 60:.0f} min]", flush=True)
        time.sleep(1.5)
    return pistes, lifts


def synthetic():
    """Offline stand-in so the tiling path can be exercised without Overpass."""
    print("PISTE_SELFTEST: synthetic geometry, no network")
    pistes, lifts = [], []
    grades = ["novice", "easy", "intermediate", "advanced", "expert"]
    for i in range(400):
        lat = 45.9 + (i % 20) * 0.05
        lon = 6.8 + (i // 20) * 0.05
        pistes.append({
            "type": "Feature",
            "geometry": {"type": "LineString",
                         "coordinates": [[lon, lat], [lon + 0.01, lat + 0.008],
                                         [lon + 0.018, lat + 0.02]]},
            "properties": {"difficulty": grades[i % 5], "name": f"Run {i}",
                           "ref": str(i), "gladed": 0, "lit": 0},
        })
        if i % 4 == 0:
            lifts.append({
                "type": "Feature",
                "geometry": {"type": "LineString",
                             "coordinates": [[lon, lat], [lon + 0.018, lat + 0.02]]},
                "properties": {"kind": "gondola", "name": f"Lift {i}"},
            })
    return pistes, lifts


def write_geojson(path, features):
    with open(path, "w") as f:
        f.write('{"type":"FeatureCollection","features":[\n')
        for i, feat in enumerate(features):
            f.write(json.dumps(feat, separators=(",", ":")))
            f.write(",\n" if i < len(features) - 1 else "\n")
        f.write("]}\n")
    print(f"  {path}: {len(features)} features, {os.path.getsize(path) / 1e6:.1f} MB")


def main():
    pistes, lifts = collect()
    if not pistes:
        raise SystemExit("No pistes collected - refusing to publish an empty layer.")

    by_grade = {}
    for p in pistes:
        g = p["properties"]["difficulty"]
        by_grade[g] = by_grade.get(g, 0) + 1
    if FAILED:
        share = 100.0 * len(FAILED) / max(1, len(tiles()))
        print(f"\n{len(FAILED)} of {len(tiles())} tiles failed ({share:.0f}%): {FAILED}")
        if share > 5.0:
            raise SystemExit(
                "Too many tiles missing - that would publish a piste layer with holes in it, "
                "where a resort with no runs drawn looks identical to a resort with none. "
                "Re-run: the cache keeps everything already fetched, so it only retries the "
                "gaps.")
        print("  under the 5% threshold; continuing, but those areas will be sparse.")

    print(f"\n{len(pistes)} pistes, {len(lifts)} lifts")
    print("  by difficulty: " + ", ".join(f"{k} {v}" for k, v in sorted(by_grade.items())))
    unknown = by_grade.get("unknown", 0)
    print(f"  {100.0 * unknown / len(pistes):.0f}% of runs carry no difficulty tag "
          f"- those are drawn grey, not guessed")

    write_geojson("pistes.geojson", pistes)
    write_geojson("lifts.geojson", lifts)

    if os.path.exists(OUT):
        os.remove(OUT)
    # --no-tile-size-limit / --no-feature-limit: a dense resort like the 3 Vallees blows
    # tippecanoe's default 500KB tile budget, and its default response is to DROP features.
    # Silently losing runs in exactly the resorts people care about most is worse than a
    # bigger tile. Line geometry compresses well, so the cost is modest.
    run(["tippecanoe", "-o", OUT,
         "-Z", str(MIN_ZOOM), "-z", str(MAX_ZOOM),
         "--no-feature-limit", "--no-tile-size-limit",
         "--simplification=2",
         "-L", "pistes:pistes.geojson",
         "-L", "lifts:lifts.geojson",
         "--force"])
    print(f"Done -> {OUT} ({os.path.getsize(OUT) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
