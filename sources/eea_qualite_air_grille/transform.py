"""The EEA 1 km air quality grid as polygons → data/processed/eea_qualite_air_grille.geojson.

One square per land cell of metropolitan France, carrying the multi-year mean
of each pollutant. The squares are built in LAEA (EPSG:3035), where they really
are squares on the source grid, and only their corners are reprojected: two
neighbouring cells share corner coordinates exactly, so they stay gap-free
after the transform instead of each being reprojected on its own.
"""
from __future__ import annotations

import json

import geopandas as gpd
import numpy as np
from pyproj import Transformer

from pipeline.common import RAW_DIR, SOURCES_DIR, BuildError, Log, load_source
from pipeline.raster import read_france

LAEA = 3035


def transform(ctx) -> None:
    upstream = load_source(SOURCES_DIR / ctx.meta["raw_from"])
    raw_dir = RAW_DIR / upstream.id
    pollutants = upstream.meta["eea"]["pollutants"]

    means = {}
    grid0 = None
    for name, spec in pollutants.items():
        stack = []
        for year in sorted(spec["files"]):
            grid = read_france(raw_dir / spec["files"][year][1], valid_min=0)
            if grid0 is None:
                grid0 = grid
            # Origins differ between files by float noise (1e-7 m), so compare with a tolerance.
            elif grid.values.shape != grid0.values.shape or max(
                abs(grid.x0 - grid0.x0), abs(grid.y0 - grid0.y0), abs(grid.dx - grid0.dx)) > 1:
                raise BuildError(f"{spec['files'][year][1]} is not on the same grid as the others — "
                                 "averaging cell by cell would mix different places")
            stack.append(grid.values)
        # A cell missing in any year gets no mean rather than a mean of fewer years.
        means[name] = np.mean(np.stack(stack), axis=0)
        Log.info(f"{name}: {len(stack)} years stacked")

    # Land cells in France: the ones whose centre falls inside a commune.
    rows, cols = np.nonzero(~np.isnan(means["pm25"]))
    cx = grid0.x0 + (cols + 0.5) * grid0.dx
    cy = grid0.y0 - (rows + 0.5) * grid0.dy
    communes = gpd.read_file(ctx.communes_geojson())[["geometry"]].to_crs(LAEA)
    pts = gpd.GeoDataFrame(geometry=gpd.points_from_xy(cx, cy), crs=LAEA)
    inside = gpd.sjoin(pts, communes, predicate="within", how="inner").index.unique().to_numpy()
    rows, cols = rows[inside], cols[inside]
    Log.info(f"{len(rows):,} land cells in metropolitan France")

    # Corners on the grid lattice, reprojected once per unique lattice point.
    left = grid0.x0 + cols * grid0.dx
    top = grid0.y0 - rows * grid0.dy
    to_wgs = Transformer.from_crs(LAEA, 4326, always_xy=True)
    corners = [(left, top - grid0.dy), (left + grid0.dx, top - grid0.dy), (left + grid0.dx, top), (left, top)]
    lonlat = [to_wgs.transform(x, y) for x, y in corners]

    vals = {name: means[name][rows, cols] for name in means}
    fields = {"pm25": ("grille_pm25", 1), "no2": ("grille_no2", 1), "o3_somo35": ("grille_somo35", 0)}

    with ctx.out_path.open("w", encoding="utf-8") as out:
        out.write('{"type":"FeatureCollection","features":[\n')
        for i in range(len(rows)):
            ring = [[round(lonlat[k][0][i], 5), round(lonlat[k][1][i], 5)] for k in range(4)]
            ring.append(ring[0])
            props = {}
            for name, (field, digits) in fields.items():
                v = vals[name][i]
                props[field] = None if np.isnan(v) else (int(round(v)) if digits == 0 else round(float(v), digits))
            feat = {"type": "Feature", "properties": props, "geometry": {"type": "Polygon", "coordinates": [ring]}}
            out.write(("," if i else "") + json.dumps(feat, separators=(",", ":")) + "\n")
        out.write("]}\n")

    for name, (field, _) in fields.items():
        v = vals[name][~np.isnan(vals[name])]
        Log.info(f"{field}: p10 {np.percentile(v, 10):.1f} · median {np.median(v):.1f} · p90 {np.percentile(v, 90):.1f} · max {v.max():.1f}")
