"""Heat indices per commune → data/processed/meteo_chaleur.csv.

The Aladin files are on an 8 km grid of 8,602 land points, not on communes, so
each commune takes the values of its nearest grid point. Communes are smaller
than the grid cell far more often than not, which makes nearest-point the
honest assignment rather than a shortcut — but it is an assignment, and the
distance is carried through to the output so it can be seen.
"""
from __future__ import annotations

import geopandas as gpd
import pandas as pd

from pipeline.common import BuildError, Log

# Projected metres, so "nearest" is a real distance rather than a mix of
# degrees of latitude and longitude that shrink as you go north.
LAMBERT93 = 2154
COMMENT = "#"
MIN_FIELDS = 24
HORIZON = 4  # zero-based column holding REF / H1 / H2 / H3


def read_grid(path, horizon: str, indices: dict) -> pd.DataFrame:
    """Parse one Aladin text file, keeping a single horizon.

    The files are Latin-1 with a '#'-commented preamble whose length varies
    between scenarios, and every row carries a trailing ';'.
    """
    wanted = max(indices.values())
    rows = []
    with path.open(encoding="latin-1") as fh:
        for line in fh:
            if line.startswith(COMMENT):
                continue
            parts = line.rstrip().rstrip(";").split(";")
            if len(parts) < MIN_FIELDS or len(parts) <= wanted:
                continue
            if parts[HORIZON] != horizon:
                continue
            try:
                row = {"lat": float(parts[1]), "lon": float(parts[2])}
                row.update({name: float(parts[i]) for name, i in indices.items()})
            except ValueError:
                continue
            rows.append(row)
    if not rows:
        raise BuildError(f"{path.name}: no rows for horizon '{horizon}' — the file layout has changed")
    return pd.DataFrame(rows)


def to_points(df: pd.DataFrame) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs="EPSG:4326"
    ).to_crs(LAMBERT93)


def nearest(communes: gpd.GeoDataFrame, points: gpd.GeoDataFrame, cols: list[str]) -> pd.DataFrame:
    joined = gpd.sjoin_nearest(
        communes[["code_insee", "geometry"]], points[cols + ["geometry"]],
        how="left", distance_col="dist_m",
    )
    # A commune equidistant from two grid points matches both; keep one.
    return joined.drop_duplicates(subset="code_insee").drop(columns="geometry")


def transform(ctx) -> None:
    res = ctx.meta["resources"]
    indices = ctx.meta["indices"]
    names = list(indices)
    max_km = float(ctx.meta["max_grid_distance_km"])

    grids = {}
    for key, spec in res.items():
        path = ctx.raw_dir / spec["filename"]
        if not path.exists():
            raise BuildError(f"{path} is missing — run the fetch stage first")
        grids[key] = read_grid(path, spec["horizon"], indices)
        Log.info(f"{key}: {len(grids[key]):,} grid points at horizon {spec['horizon']}")

    communes = gpd.read_file(ctx.communes_geojson())
    # representative_point stays inside awkward shapes where a centroid can fall
    # outside them — coastal communes and the ones wrapped around a neighbour.
    communes = communes.to_crs(LAMBERT93)
    communes["geometry"] = communes.representative_point()
    Log.info(f"{len(communes):,} commune representative points")

    hot = nearest(communes, to_points(grids["rcp85"]), names)
    df = pd.DataFrame(
        {
            "code_insee": hot["code_insee"].values,
            "chaleur_nuits_trop": hot["NORTR"].round(0).astype("Int64").values,
            "chaleur_jours_ete": hot["NORSD"].round(0).astype("Int64").values,
            "chaleur_tx_moyen": hot["NORTXAV"].round(1).values,
            "chaleur_grille_km": (hot["dist_m"] / 1000).round(1).values,
        }
    )

    # The reference period and the moderate scenario, on the same grid, so the
    # popup can say how much of the change is already in the reference.
    for key, column in (("reference", "chaleur_nuits_trop_ref"), ("rcp45", "chaleur_nuits_trop_45")):
        side = nearest(communes, to_points(grids[key]), ["NORTR"])
        df[column] = side["NORTR"].round(0).astype("Int64").values

    far = df[df["chaleur_grille_km"] > max_km]
    if len(far):
        Log.warn(
            f"{len(far):,} commune(s) more than {max_km:.0f} km from a grid point "
            f"(worst {df['chaleur_grille_km'].max():.1f} km): {', '.join(far['code_insee'].head(5))}"
        )

    df = df.sort_values("code_insee")
    Log.info(
        f"{len(df):,} communes · median {df['chaleur_nuits_trop'].median():.0f} tropical nights "
        f"by 2041–2070 (RCP8.5), against {df['chaleur_nuits_trop_ref'].median():.0f} in 1976–2005"
    )
    Log.info(
        f"  grid distance: median {df['chaleur_grille_km'].median():.1f} km, "
        f"max {df['chaleur_grille_km'].max():.1f} km"
    )
    df.to_csv(ctx.out_path, index=False)
