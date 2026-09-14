"""Terrain height at arbitrary points, from the Copernicus GLO-90 tiles.

The tiles are 1° GeoTIFFs in lon/lat (source `copernicus_dem`). Points are
grouped by tile and each tile is read once, so asking for millions of points
costs one decompression per tile rather than one read per point.

    alt = altitude(lon, lat)                     # metres, NaN over sea / missing tiles
"""
from __future__ import annotations

import numpy as np

from pipeline.common import RAW_DIR, BuildError, Log

SOURCE_ID = "copernicus_dem"


def tile_name(lat_floor: int, lon_floor: int) -> str:
    return f"{'N' if lat_floor >= 0 else 'S'}{abs(lat_floor):02d}{'E' if lon_floor >= 0 else 'W'}{abs(lon_floor):03d}"


def altitude(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """Height in metres of the terrain pixel under each lon/lat point."""
    import rasterio

    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    out = np.full(len(lon), np.nan)
    raw = RAW_DIR / SOURCE_ID
    if not raw.exists() or not any(raw.glob("*.tif")):
        raise BuildError(f"no terrain tiles in {raw} — run the fetch stage for '{SOURCE_ID}' first")

    keys = np.floor(lat).astype(int) * 1000 + np.floor(lon).astype(int)
    missing_points = 0
    for key in np.unique(keys):
        idx = np.nonzero(keys == key)[0]
        lat_floor = int(np.floor(lat[idx[0]]))
        lon_floor = int(np.floor(lon[idx[0]]))
        path = raw / f"{tile_name(lat_floor, lon_floor)}.tif"
        if not path.exists():
            missing_points += len(idx)  # all-sea tiles are not published
            continue
        with rasterio.open(path) as ds:
            band = ds.read(1, masked=True).astype("float32").filled(np.nan)
            rows, cols = rasterio.transform.rowcol(ds.transform, lon[idx], lat[idx])
        rows = np.clip(np.asarray(rows), 0, band.shape[0] - 1)
        cols = np.clip(np.asarray(cols), 0, band.shape[1] - 1)
        out[idx] = band[rows, cols]
    if missing_points:
        Log.info(f"{missing_points:,} point(s) fall on tiles with no terrain data (sea)")
    return out
