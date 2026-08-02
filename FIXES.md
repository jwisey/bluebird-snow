# Baker fixes — 2026-08-02

The baker had never been executed. It was run end-to-end in a cloud container
(synthetic forcings, since Open-Meteo/AWS were outside that container's network
allowlist). Three separate faults would each have broken the very first
GitHub Actions run.

## Blockers (would have failed run #1)

1. **`pip install -r requirements.txt` fails.** `rio-mbtiles` 1.6.0 pins
   `shapely~=1.7.0`, which has no wheel for Python 3.11 and fails to compile.
   The install step died before any baking happened.
   *Fix:* shapely 2.x + rio-mbtiles' real deps in `requirements.txt`; install
   `rio-mbtiles` itself with `--no-deps` in both workflows.

2. **`rio mbtiles` crashes on a fresh output file.** Bug in rio-mbtiles 1.6.0:
   it only binds its internal `appending` flag when the output file already
   exists, so the first run raised
   `NameError: cannot access free variable 'appending'`.
   *Fix:* touch the `.mbtiles` before invoking it (both bakers).

3. **`--rgba` was missing.** The baker writes a 4-band RGBA GeoTIFF, but
   `rio mbtiles` drops the 4th band unless `--rgba` is passed. Every tile would
   have been fully opaque — including the 0 cm areas, which the ramp paints
   near-white. The result: an opaque grey-white sheet over the whole Alps,
   hiding the valleys and the terrain. See `ramp-check.png` (right panel).
   *Fix:* pass `--rgba`; verified the alpha channel survives into the tiles.

## Improvements

- **DEM source -> AWS terrarium** (`DEM_SOURCE=terrarium`, default), the same
  tiles the app uses for its 3D mesh, so the snow field registers pixel-for-pixel
  with the terrain. Removes the fragile CGIAR/SRTM + `make` dependency.
  Automatic fallback to SRTM3.
- **~8x faster field interpolation** — one Delaunay triangulation shared across
  all five parameter fields (was 10 rebuilds per date). Matters a lot for
  season backfills.
- **Terrain derivatives computed once**, not per backfilled date.
- **Backfill now saves `snow-state.tif`**, so the following daily run continues
  the season instead of cold-starting.
- **Honest-zoom guard** — prints the DEM's real m/px and warns when `MAX_ZOOM`
  is upsampling blurry data (default max zoom lowered 12 -> 11; raise `DEM_ZOOM`
  for real detail instead).
- **Retries + backoff** on Open-Meteo, chunk size 120 -> 50 (large multi-location
  hourly requests are the most likely thing to 400/time out).
- **Offline self-test** (`SNOW_SELFTEST=1`) + a smoke-test step in the workflow.
- **Hillshade baker:** shading now happens in EPSG:3035 (metres) instead of
  straight off lat/lon with `-s 111120`. A degree of longitude at 46°N is only
  ~0.69 of a degree of latitude, so the old version stretched every slope
  east-west by ~1.4x. Also added DEFLATE/BIGTIFF on the intermediates and curl
  retries.

## Still untested (needs real network)
- The live Open-Meteo archive response shape and multi-location limits.
- The terrarium tile mosaic download.
- Copernicus GLO-30 tile naming on the AWS bucket.
Each has retries and, where possible, a fallback — but the first live run is
still the real test.
