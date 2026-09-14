"""Air quality exposure per commune → data/processed/eea_qualite_air.csv.

Three pollutants from the EEA 1 km interpolated maps, averaged over four years,
and weighted by where people actually live inside each commune.

The weighting is the point. A commune's area mean counts the forest, the
motorway verge and the mountain top as much as the village, and for NO2 in
particular that is a different number. So each populated 200 m cell of the
INSEE grid (source insee_carreaux_200m) takes the value of the 1 km air cell it sits in, and the commune
value is the population-weighted mean of those. The 200 m grid is only used
as weights — the air data does not get any finer than 1 km by this.

Communes with no populated 200 m cell (uninhabited, or too small to carry
one) fall back to the plain mean of the 1 km cells whose centre they contain,
and failing that to the cell under their representative point. Which method
each commune got is written out, not hidden.
"""
from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd

from pipeline.common import Log
from pipeline.population import LAEA, assign_communes, read_population, weighted_by_commune
from pipeline.raster import read_france


def transform(ctx) -> None:
    pollutants = ctx.meta["eea"]["pollutants"]

    communes = gpd.read_file(ctx.communes_geojson())[["code_insee", "geometry"]].to_crs(LAEA)
    Log.info(f"{len(communes):,} communes")

    people = read_population()
    people["code"] = assign_communes(people, communes)
    lost = people["code"].isna()
    Log.info(f"{lost.sum():,} populated cells ({people.loc[lost, 'pop'].sum():,.0f} people) fall outside every "
             "commune polygon — coastline and border slivers — and are left out")

    # Fallback sample points, built from the first grid: every 1 km cell centre,
    # then each commune's representative point for the ones too small to hold one.
    first = next(iter(pollutants.values()))["files"]
    x, y, _ = read_france(ctx.raw_dir / first[min(first)][1], valid_min=0).centres()
    cells = pd.DataFrame({"x": x, "y": y})
    cells["code"] = assign_communes(cells, communes)
    cells = cells[cells["code"].notna()].reset_index(drop=True)
    rep = communes.representative_point()
    reps = pd.DataFrame({"x": rep.x.to_numpy(), "y": rep.y.to_numpy(), "code": communes["code_insee"].to_numpy()})

    result = pd.DataFrame({"code_insee": communes["code_insee"].to_numpy()}).set_index("code_insee")
    populated = set(people["code"].dropna())
    with_cell = set(cells["code"])
    result["air_ponderation"] = [
        "population" if c in populated else "surface" if c in with_cell else "point" for c in result.index
    ]

    for name, spec in pollutants.items():
        years = sorted(spec["files"])
        per_year = {}
        cell_sum = np.zeros(len(people))
        cell_n = np.zeros(len(people))
        for year in years:
            grid = read_france(ctx.raw_dir / spec["files"][year][1], valid_min=0)
            at_people = grid.sample(people["x"].to_numpy(), people["y"].to_numpy())
            by_pop = weighted_by_commune(people["code"], at_people, people["pop"].to_numpy())
            by_area = weighted_by_commune(cells["code"], grid.sample(cells["x"].to_numpy(), cells["y"].to_numpy()),
                                          np.ones(len(cells)))
            at_rep = pd.Series(grid.sample(reps["x"].to_numpy(), reps["y"].to_numpy()), index=reps["code"])
            per_year[year] = by_pop.reindex(result.index).fillna(by_area.reindex(result.index)).fillna(at_rep)
            valid = ~np.isnan(at_people)
            cell_sum[valid] += at_people[valid]
            cell_n[valid] += 1
            Log.info(f"{name} {year}: population-weighted median commune {per_year[year].median():.1f}")

        table = pd.DataFrame(per_year)
        result[f"air_{name}"] = table.mean(axis=1)
        result[f"air_{name}_an_min"] = table.min(axis=1)
        result[f"air_{name}_an_max"] = table.max(axis=1)

        # The worst-placed resident: the highest multi-year value over the
        # commune's populated cells. For a big commune this is the number the
        # average hides — a ring road through one quarter of Marseille.
        if spec.get("worst_cell"):
            cell_mean = np.where(cell_n == len(years), cell_sum / np.maximum(cell_n, 1), np.nan)
            ok = people["code"].notna().to_numpy() & ~np.isnan(cell_mean)
            worst = pd.Series(cell_mean[ok]).groupby(people["code"][ok].to_numpy()).max()
            result[f"air_{name}_pire"] = worst.reindex(result.index)

    counts = result["air_ponderation"].value_counts()
    Log.info("weighting: " + " · ".join(f"{k} {v:,}" for k, v in counts.items()))

    out = result.reset_index()
    rounding = {c: (0 if "somo35" in c else 1) for c in out.columns if c.startswith("air_") and c != "air_ponderation"}
    for col, digits in rounding.items():
        out[col] = out[col].round(digits)
        if digits == 0:
            out[col] = out[col].astype("Int64")
    # _pire is blank by construction for a commune with no populated cell.
    missing = out[[c for c in rounding if not c.endswith("_pire")]].isna().any(axis=1)
    if missing.any():
        Log.warn(f"{missing.sum():,} commune(s) with a missing value: {', '.join(out.loc[missing, 'code_insee'].head(8))}")
    out.sort_values("code_insee").to_csv(ctx.out_path, index=False)
