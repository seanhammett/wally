"""DRIAS TRACC-2023 heat, dry-soil and fire-weather indicators per commune → data/processed/drias_chaleur.csv.

The archives are read in place (they are plain tar, so members are read
directly). Each indicator file is a grid of 19,162 SAFRAN points in lat/lon; the
export has defects, described in source.yaml, which read_grid() repairs and
checks. Grid values reach communes through where residents live.
"""
from __future__ import annotations

import re
import tarfile

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from pyproj import Transformer

from pipeline.common import BuildError, Log, require_manual
from pipeline.population import LAEA, assign_communes, read_population, weighted_by_commune

WGS84 = 4326
DAYS_IN_YEAR = 366


def member_name(names: list[str], indicator: str, level: str, stat: str, anomaly: bool,
                optional: bool = False) -> str | None:
    pattern = re.compile(
        rf"^{indicator}_h?yr_RWL-{level}_TIMEavg_{'ANOMd-1976-2005_' if anomaly else ''}GEOxy_.*_ENS{stat}\.csv$")
    hits = [n for n in names if pattern.match(n)]
    if optional and not hits:
        return None
    if len(hits) != 1:
        raise BuildError(f"expected one {indicator} RWL-{level} {'anomaly ' if anomaly else ''}ENS{stat} file, "
                         f"found {len(hits)}")
    return hits[0]


def to_days(values: pd.Series) -> pd.Series:
    """Plain numbers, or the pandas time spans ("2 days 12:00:00") some files were written as."""
    raw = values.replace("nan", np.nan)
    out = pd.to_numeric(raw, errors="coerce")
    spans = raw[out.isna() & raw.notna()]
    if len(spans):
        out.loc[spans.index] = pd.to_timedelta(spans) / pd.Timedelta(days=1)
    return out


def read_grid(tar: tarfile.TarFile, name: str, n_points: int | None = None,
              land: np.ndarray | None = None) -> pd.DataFrame:
    """Point, lat, lon, value — in the file's point order."""
    df = pd.read_csv(tar.extractfile(name), sep=";", comment="#", dtype=str)
    if list(df.columns[:5]) != ["Point", "Latitude", "Longitude", "RWL", "Value"]:
        raise BuildError(f"{name}: unexpected columns {list(df.columns)}")
    df = df.replace("nan", np.nan)

    if n_points is not None and len(df) == 2 * n_points:
        # Two stacked tables: values without coordinates, then coordinates without values.
        values, coords = df.iloc[:n_points], df.iloc[n_points:]
        if values["Latitude"].notna().any() or coords["Value"].notna().any():
            raise BuildError(f"{name}: {len(df):,} rows but not the stacked values/coordinates layout")
        mask = values["Value"].notna().to_numpy()
        if land is None or not np.array_equal(mask, land):
            raise BuildError(f"{name}: the value block's land mask does not match the reference grid, "
                             "so its rows cannot be matched to points")
        df = coords.reset_index(drop=True).assign(Value=values["Value"].to_numpy())

    out = pd.DataFrame({
        "point": pd.to_numeric(df["Point"], errors="coerce").astype("Int64"),
        "lat": pd.to_numeric(df["Latitude"], errors="coerce"),
        "lon": pd.to_numeric(df["Longitude"], errors="coerce"),
        "value": to_days(df["Value"]),
    })
    if out[["point", "lat", "lon"]].isna().any().any():
        raise BuildError(f"{name}: rows without a point or coordinates")
    return out


def transform(ctx) -> None:
    paths = require_manual(ctx.source, ctx.meta["expected_files"])
    levels = ctx.meta["levels"]
    indicators = ctx.meta["indicators"]
    by_level = {}
    for path in paths:
        match = re.search(r"_RWL(\d\d)_", path.name)
        if not match:
            raise BuildError(f"{path.name}: no warming level in the file name")
        by_level[match.group(1)] = path
    ref, high, present = levels["reference"], levels["high"], levels["present"]
    present_indicators = set(ctx.meta["present_indicators"])

    grid = None          # point, lat, lon of the reference file
    columns: dict[str, np.ndarray] = {}
    for level in (ref, high, present):
        with tarfile.open(by_level[level]) as tar:
            names = tar.getnames()
            if grid is None:
                first = read_grid(tar, member_name(names, "TR", level, "q50", False))
                grid = first[["point", "lat", "lon"]]
                land = first["value"].notna().to_numpy()
                Log.info(f"grid: {len(grid):,} points, {int(land.sum()):,} over land")

            def get(indicator, stat, anomaly=False, optional=False):
                name = member_name(names, indicator, level, stat, anomaly, optional)
                if name is None:
                    return None
                frame = read_grid(tar, name, len(grid), land)
                if not frame["point"].equals(grid["point"]) or not np.allclose(frame["lat"], grid["lat"]):
                    raise BuildError(f"{indicator} {stat}: points differ from the reference grid")
                if not np.array_equal(frame["value"].notna().to_numpy(), land):
                    raise BuildError(f"{indicator} {stat}: land mask differs from the reference grid")
                return frame["value"].to_numpy()

            for code, stem in indicators.items():
                if level == present and code not in present_indicators:
                    continue
                median = get(code, "q50")
                if np.nanmin(median) < 0 or np.nanmax(median) > DAYS_IN_YEAR:
                    raise BuildError(f"{code} RWL-{level}: medians outside 0–{DAYS_IN_YEAR} days")
                if level == high:
                    columns[f"{stem}_4"] = median
                    continue
                if level == present:
                    columns[f"{stem}_20"] = median
                    Log.info(f"{code} +2.0 °C: grid median {np.nanmedian(median):.1f} days")
                    continue
                top = get(code, "max")
                if (median[land] > top[land] + 1e-6).any():
                    raise BuildError(f"{code}: the ENSq50 file exceeds ENSmax somewhere — not a median")
                columns[f"{stem}_2050"] = median
                columns[f"{stem}_2050_max"] = top
                bottom = get(code, "min", optional=True)      # SWI04D has no absolute minimum file
                if bottom is not None:
                    if (bottom[land] > median[land] + 1e-6).any():
                        raise BuildError(f"{code}: ENSmin exceeds ENSq50 somewhere")
                    columns[f"{stem}_2050_min"] = bottom
                columns[f"{stem}_ecart"] = get(code, "q50", anomaly=True)
                Log.info(f"{code} +{int(level) / 10:.1f} °C: grid median {np.nanmedian(median):.1f} days, "
                         f"range {np.nanmin(median):.1f}–{np.nanmax(median):.1f}; change vs 1976–2005 median "
                         f"{np.nanmedian(columns[f'{stem}_ecart']):+.1f}")

    # Nearest land grid point for every home, in LAEA metres.
    to_laea = Transformer.from_crs(WGS84, LAEA, always_xy=True)
    gx, gy = to_laea.transform(grid["lon"].to_numpy()[land], grid["lat"].to_numpy()[land])
    tree = shapely.STRtree(shapely.points(gx, gy))

    def values_at(xs, ys, label: str) -> dict[str, np.ndarray]:
        _, nearest = tree.query_nearest(shapely.points(xs, ys), all_matches=False)
        dist = np.hypot(gx[nearest] - xs, gy[nearest] - ys)
        # Grid points are 8 km apart, so anything much past ~6 km is off the grid's
        # land edge: islands (Ouessant is the farthest, at 15 km) and a few capes.
        (Log.warn if dist.max() > 25_000 else Log.info)(
            f"{label}: {int((dist > 8000).sum()):,} more than 8 km from a land grid point, "
            f"farthest {dist.max() / 1000:.1f} km")
        return {name: values[land][nearest] for name, values in columns.items()}

    communes = gpd.read_file(ctx.communes_geojson())[["code_insee", "geometry"]].to_crs(LAEA)
    people = read_population()
    people["code"] = assign_communes(people, communes)
    at_people = values_at(people["x"].to_numpy(), people["y"].to_numpy(), "populated cells")
    rep = communes.representative_point()
    at_rep = values_at(rep.x.to_numpy(), rep.y.to_numpy(), "commune representative points")

    result = pd.DataFrame(index=pd.Index(communes["code_insee"].to_numpy(), name="code_insee"))
    pop = people["pop"].to_numpy(dtype=float)
    for name, values in at_people.items():
        by_pop = weighted_by_commune(people["code"], values, pop)
        result[name] = by_pop.reindex(result.index).fillna(pd.Series(at_rep[name], index=result.index)).round(1)

    order = [c for stem in indicators.values()
             for c in (f"{stem}_2050", f"{stem}_2050_min", f"{stem}_2050_max", f"{stem}_ecart", f"{stem}_4",
                       f"{stem}_20")
             if c in result.columns]
    result = result[order]
    if result.isna().any().any():
        raise BuildError(f"{int(result.isna().any(axis=1).sum())} commune(s) with a missing value")
    for stem in indicators.values():
        col = result[f"{stem}_2050"]
        Log.info(f"{stem}_2050: p5 {col.quantile(0.05):.1f} · median {col.median():.1f} · p95 {col.quantile(0.95):.1f}")
    result.reset_index().sort_values("code_insee").to_csv(ctx.out_path, index=False)
