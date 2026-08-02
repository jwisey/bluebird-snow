#!/usr/bin/env python3
"""
Bluebird hillshade baker — crisp "lidar-look" terrain texture for the Alps.

Copernicus GLO-30 DEM (30 m, free, AWS Open Data, no auth) -> multidirectional
hillshade (gdaldem) -> raster PMTiles. Replaces the washed-out Esri World Hillshade
as the app's terrain texture (MapStyle.TerrainConfig.hillshadeHiResURL).

Run once via workflow_dispatch (.github/workflows/bake-hillshade.yml); re-run only to retune.

Env knobs:
  HS_MAX_ZOOM  max tile zoom (default 12 ≈ honest for 30 m data; 13 = sharper-looking, ~4x bigger)
  HS_Z         vertical exaggeration for shading (default 1.4 — punchier ridges/gullies)
"""
import os, math, subprocess, sys

BBOX = (5.8, 43.5, 12.5, 48.0)          # lon_min, lat_min, lon_max, lat_max (same as bake_snow)
MAXZ = int(os.environ.get("HS_MAX_ZOOM", "12") or "12")
ZFAC = os.environ.get("HS_Z", "1.4") or "1.4"
S3 = "https://copernicus-dem-30m.s3.amazonaws.com"
OUT_PMTILES = "alps-hillshade-hires.pmtiles"
# ETRS89 / LAEA Europe — metres, minimal distortion over the Alps. Shading in a
# metric CRS avoids the lat/lon anisotropy you get from `gdaldem -s 111120`
# (a degree of longitude at 46°N is only ~0.69 of a degree of latitude, so
# shading straight off EPSG:4326 stretches every slope east-west by ~1.4x).
WORK_CRS = "EPSG:3035"


def run(cmd):
    print("+", " ".join(cmd)); sys.stdout.flush()
    subprocess.run(cmd, check=True)


def main():
    os.makedirs("dem", exist_ok=True)
    got = []
    for lat in range(math.floor(BBOX[1]), math.ceil(BBOX[3])):
        for lon in range(math.floor(BBOX[0]), math.ceil(BBOX[2])):
            name = f"Copernicus_DSM_COG_10_N{lat:02d}_00_E{lon:03d}_00_DEM"
            dst = f"dem/{name}.tif"
            if not os.path.exists(dst):
                url = f"{S3}/{name}/{name}.tif"
                r = subprocess.run(["curl", "-sfL", "--retry", "4", "--retry-delay", "3",
                                    "-o", dst + ".part", url])
                if r.returncode != 0:
                    print(f"  skip {name} (no tile)")
                    if os.path.exists(dst + ".part"):
                        os.remove(dst + ".part")
                    continue
                os.rename(dst + ".part", dst)
            got.append(dst)
    print(f"{len(got)} DEM tiles")
    if not got:
        raise SystemExit("No Copernicus tiles downloaded — check the bucket/naming.")

    run(["gdalbuildvrt", "alps-dem.vrt"] + got)
    # Reproject to metres FIRST, then shade with -s 1 (true metric slopes).
    run(["gdalwarp", "-t_srs", WORK_CRS, "-r", "bilinear", "-tr", "30", "30",
         "-te_srs", "EPSG:4326", "-te", str(BBOX[0]), str(BBOX[1]), str(BBOX[2]), str(BBOX[3]),
         "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES", "-co", "BIGTIFF=YES",
         "-multi", "-wo", "NUM_THREADS=ALL_CPUS",
         "-overwrite", "alps-dem.vrt", "alps-dem-m.tif"])
    run(["gdaldem", "hillshade", "-multidirectional", "-compute_edges",
         "-z", ZFAC, "-s", "1",
         "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES", "-co", "BIGTIFF=YES",
         "alps-dem-m.tif", "hs-grey.tif"])
    # grey -> 3-band RGB (rio mbtiles wants at least 3 bands)
    run(["gdal_translate", "-b", "1", "-b", "1", "-b", "1", "-co", "PHOTOMETRIC=RGB",
         "-co", "COMPRESS=DEFLATE", "-co", "TILED=YES", "-co", "BIGTIFF=YES",
         "hs-grey.tif", "hs-rgb.tif"])
    # rio-mbtiles 1.6.0 dies with "NameError: ... 'appending'" when the output does
    # not already exist. Touching it first takes the branch that binds the flag.
    open("alps-hillshade.mbtiles", "a").close()
    run(["rio", "mbtiles", "hs-rgb.tif", "alps-hillshade.mbtiles",
         "--zoom-levels", f"5..{MAXZ}", "--format", "PNG", "--overwrite"])
    if os.path.exists(OUT_PMTILES):
        os.remove(OUT_PMTILES)
    run(["pmtiles", "convert", "alps-hillshade.mbtiles", OUT_PMTILES])
    print(f"Done -> {OUT_PMTILES} ({os.path.getsize(OUT_PMTILES) / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
