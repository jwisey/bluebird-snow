# Bluebird snow baker (Phase B)

Bakes a smooth, regional, real **snow-depth raster** for the Alps and publishes it as
**PMTiles** for the Bluebird app to drape over the 3D terrain. Free: runs on GitHub Actions,
uses Open-Meteo's free archive + free SRTM DEM, publishes the tiles as a GitHub Release asset.

## How it works
`bake_snow.py`:
1. Samples **Open-Meteo** (free, no key) for `snow_depth` + `freezing_level` on a coarse grid.
2. Derives smooth **param fields** — snow line, depth-per-metre slope, cap — that vary by region.
3. Interpolates those onto a high-res **SRTM DEM** and runs a physical model per pixel:
   altitude base `clamp(slope·(elev−snowLine),0,cap)` × **aspect** (N faces hold, S faces melt)
   × **slope** (steep sheds) + **fresh snow** − **degree-day melt** (temp-by-altitude × sun × season),
   then blends with **yesterday's depth** (accumulation). Smooth + zoom-stable (high-res DEM),
   regional (params vary), dramatic (terrain-driven aspect/slope/melt), and it evolves day to day.
4. Colour-ramps depth → RGBA GeoTIFF → MBTiles → **`alps-snow.pmtiles`**.

The GitHub Action runs it daily and uploads `alps-snow.pmtiles` to a release tagged `snow`.

## Setup (one time)
1. Put this folder in its **own GitHub repo** (public is simplest for serving tiles).
2. Push — the workflow at `.github/workflows/bake-snow.yml` is already in place.
3. Run it once: Actions tab → **Bake Alps snow tiles** → **Run workflow**.
4. After it succeeds, the tiles are at:
   `https://github.com/<you>/<repo>/releases/download/snow/alps-snow.pmtiles`
5. Put that URL in the app: `SnowConfig.tilesURL` (Map/MapStyle.swift).

## Hi-res hillshade (crisp "lidar" terrain texture)
`bake_hillshade.py` bakes a crisp multidirectional hillshade of the Alps from the free
Copernicus GLO-30 DEM (30 m) — much sharper ridgelines/gullies than the Esri texture the
app uses by default. One-time run:
1. Actions tab → **Bake Alps hi-res hillshade** → **Run workflow** (defaults are fine).
2. After it succeeds, paste
   `https://github.com/<you>/<repo>/releases/download/terrain/alps-hillshade-hires.pmtiles`
   into `TerrainConfig.hillshadeHiResURL` (Map/MapStyle.swift).
- `maxzoom` 12 is honest for 30 m data (~350 MB); 13 looks a touch sharper up close but is ~4× bigger.
- `exaggeration` 1.4 = punchier shading; lower toward 1.0 if it feels heavy.

## Validated 2026-08-02
The whole pipeline was run end-to-end offline (`SNOW_SELFTEST=1`) and three
first-run blockers were fixed — see `FIXES.md`. Run the self-test any time:

```bash
SNOW_SELFTEST=1 SNOW_DATE=2024-02-15 SNOW_MAX_ZOOM=8 python bake_snow.py
```
It needs no network, uses a synthetic Alpine DEM + synthetic winter forcings,
exercises model -> colourise -> GeoTIFF -> mbtiles -> pmtiles, and drops
`preview-YYYYMMDD.png` so you can eyeball the ramp without building the app.
The real workflow runs this as a smoke test before the live bake.

## Notes / tuning
- **Date:** defaults to ~6 days ago (ERA5 archive lag). Set the `SNOW_DATE` env (YYYY-MM-DD) to pin a deep-winter day for testing, e.g. `2024-02-15`.
- **DEM:** now defaults to the AWS **terrarium** tiles — the same DEM the app drapes its 3D mesh on, so the snow field registers with the terrain. `DEM_ZOOM=9` ≈ 215 m/px; `10` ≈ 108 m/px for 4x the tiles. Falls back to SRTM3 automatically if AWS is unreachable.
- **Resolution / tile size:** `DEM_MAX_WIDTH` and `MIN/MAX_ZOOM` trade detail vs file size.
- **Model knobs** (top of `bake_snow.py`): `ASPECT_STRENGTH`, `SLOPE_SHED`, `MELT_CM_PER_DEGDAY`, `FRESH_GAIN`, `STATE_BLEND`, and `RAMP` tune drama vs realism.
- **Accumulation:** the run pulls `snow-state.tif` from the `snow` release, evolves it, and re-publishes it. Delete that asset to cold-start.
- **Later:** swap the altitude model for the season accumulation/melt model; same pipeline, better depths.
