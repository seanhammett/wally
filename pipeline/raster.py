"""Reading gridded rasters without GDAL on the command line.

Kept to the two things a transform needs from a Europe-wide GeoTIFF: cut out
metropolitan France once, and look values up at many points in that cut-out.
Sampling is plain array indexing rather than rasterio's per-point `sample`,
because the callers ask for millions of points.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from pipeline.common import METRO_BBOX, BuildError


@dataclass
class Grid:
    """A 2-D array plus the affine terms needed to go from x/y to row/col."""
    values: np.ndarray       # float64, NaN where the source has no value
    x0: float                # left edge of column 0
    y0: float                # top edge of row 0
    dx: float                # cell width, positive
    dy: float                # cell height, positive (rows run downwards)
    crs: str

    def cell_index(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        col = np.floor((x - self.x0) / self.dx).astype(np.int64)
        row = np.floor((self.y0 - y) / self.dy).astype(np.int64)
        inside = (row >= 0) & (row < self.values.shape[0]) & (col >= 0) & (col < self.values.shape[1])
        return row, col, inside

    def sample(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """Value of the cell containing each point; NaN outside the grid or on nodata."""
        row, col, inside = self.cell_index(x, y)
        out = np.full(len(x), np.nan)
        out[inside] = self.values[row[inside], col[inside]]
        return out

    def centres(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """x, y of every cell centre with a value, and its value."""
        rows, cols = np.nonzero(~np.isnan(self.values))
        x = self.x0 + (cols + 0.5) * self.dx
        y = self.y0 - (rows + 0.5) * self.dy
        return x, y, self.values[rows, cols]


def read_france(path: Path, *, valid_min: float | None = None, pad_m: float = 20_000) -> Grid:
    """Read the part of a raster covering metropolitan France, NaN for nodata.

    `valid_min` catches sentinel values that are not declared as nodata — some
    of the EEA files mark the sea with a large negative float rather than a
    nodata tag.
    """
    import rasterio
    from pyproj import Transformer
    from rasterio.windows import Window, from_bounds

    if not path.exists():
        raise BuildError(f"{path} is missing — run the fetch stage first")
    with rasterio.open(path) as ds:
        if ds.crs is None:
            raise BuildError(f"{path.name} has no CRS — refusing to guess")
        # The bbox corners alone under-cover a projected France, whose edges
        # bow outwards; densify the outline before taking its extent.
        lon0, lat0, lon1, lat1 = METRO_BBOX
        lons = np.concatenate([np.linspace(lon0, lon1, 50), np.full(50, lon1), np.linspace(lon1, lon0, 50), np.full(50, lon0)])
        lats = np.concatenate([np.full(50, lat0), np.linspace(lat0, lat1, 50), np.full(50, lat1), np.linspace(lat1, lat0, 50)])
        xs, ys = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform(lons, lats)
        window = from_bounds(xs.min() - pad_m, ys.min() - pad_m, xs.max() + pad_m, ys.max() + pad_m, ds.transform)
        window = window.round_offsets().round_lengths().intersection(Window(0, 0, ds.width, ds.height))
        band = ds.read(1, window=window, masked=True).astype("float64").filled(np.nan)
        t = ds.window_transform(window)
        if abs(t.b) > 1e-9 or abs(t.d) > 1e-9:
            raise BuildError(f"{path.name} is rotated; this reader handles north-up grids only")
        if valid_min is not None:
            band[band < valid_min] = np.nan
        return Grid(values=band, x0=t.c, y0=t.f, dx=t.a, dy=-t.e, crs=ds.crs.to_string())
