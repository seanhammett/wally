"""Clay shrink–swell exposure of each commune's residents → data/processed/rga_communes.csv.

Polygons are read straight out of the PMTiles archive at its full-detail zoom
(GDAL's PMTiles driver, through pyogrio). At that zoom features are clipped to
their tile, so the pieces tile the zones without overlapping and a point falls
in at most one — a few land exactly on a shared edge and take the higher class.
"""
from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from pyproj import Transformer

from pipeline.common import BuildError, Log
from pipeline.population import LAEA, assign_communes, read_population, weighted_by_commune

WEB_MERCATOR = 3857


def transform(ctx) -> None:
    res = ctx.meta["resource"]
    classes = {str(k): int(v) for k, v in ctx.meta["classes"].items()}
    path = ctx.raw_dir / res["filename"]
    if not path.exists():
        raise BuildError(f"{path} is missing — run the fetch stage first")

    zones = pyogrio.read_dataframe(path, layer=res["layer"], ZOOM_LEVEL=int(res["zoom"]))
    if zones.crs is None or zones.crs.to_epsg() != WEB_MERCATOR:
        raise BuildError(f"{path.name}: expected EPSG:{WEB_MERCATOR}, got {zones.crs}")
    unknown = set(zones["ALEA"].dropna().unique()) - set(classes)
    if unknown:
        raise BuildError(f"{path.name}: unexpected exposure class(es) {sorted(unknown)}")
    zones["cls"] = zones["ALEA"].map(classes)
    area = zones.to_crs(LAEA).area / 1e6
    by_class = area.groupby(zones["ALEA"]).sum()
    Log.info(f"{len(zones):,} zone pieces · " + " · ".join(f"{k} {v:,.0f} km²" for k, v in by_class.items())
             + f" · moderate or high {by_class.get('Moyen', 0) + by_class.get('Fort', 0):,.0f} km²")

    communes = gpd.read_file(ctx.communes_geojson())[["code_insee", "geometry"]].to_crs(LAEA)
    people = read_population()
    people["code"] = assign_communes(people, communes)
    to_mercator = Transformer.from_crs(LAEA, WEB_MERCATOR, always_xy=True)

    def class_at(xs, ys) -> np.ndarray:
        mx, my = to_mercator.transform(xs, ys)
        pts = gpd.GeoDataFrame(geometry=gpd.points_from_xy(mx, my), crs=WEB_MERCATOR)
        hit = gpd.sjoin(pts, zones[["cls", "geometry"]], predicate="within", how="left")
        return hit.groupby(level=0)["cls"].max().reindex(pts.index).fillna(0).to_numpy()

    cls = class_at(people["x"].to_numpy(), people["y"].to_numpy())
    pop = people["pop"].to_numpy(dtype=float)
    shares = {c: pop[cls == c].sum() / pop.sum() for c in range(4)}
    Log.info("residents by class: " + " · ".join(f"{c} {s:.1%}" for c, s in shares.items()))

    columns = {
        "rga_part_moyen_fort": (cls >= 2).astype(float) * 100,
        "rga_part_fort": (cls == 3).astype(float) * 100,
        "rga_indice": cls,
    }
    result = pd.DataFrame(index=pd.Index(communes["code_insee"].to_numpy(), name="code_insee"))
    for name, values in columns.items():
        result[name] = weighted_by_commune(people["code"], values, pop).reindex(result.index)

    # Communes with no populated cell: the class at the representative point.
    empty = result["rga_indice"].isna().to_numpy()
    if empty.any():
        rep = communes.geometry[empty].representative_point()
        rep_cls = class_at(rep.x.to_numpy(), rep.y.to_numpy())
        result.loc[empty, "rga_part_moyen_fort"] = (rep_cls >= 2) * 100.0
        result.loc[empty, "rga_part_fort"] = (rep_cls == 3) * 100.0
        result.loc[empty, "rga_indice"] = rep_cls
        Log.info(f"{int(empty.sum()):,} commune(s) with no populated cell use their representative point")

    result["rga_part_moyen_fort"] = result["rga_part_moyen_fort"].round(0).astype(int)
    result["rga_part_fort"] = result["rga_part_fort"].round(0).astype(int)
    result["rga_indice"] = result["rga_indice"].round(2)
    q = result["rga_part_moyen_fort"]
    Log.info(f"{len(result):,} communes · median {q.median():.0f}% of residents on moderate/high exposure · "
             f"{int((q >= 90).sum()):,} communes at 90%+ · {int((q == 0).sum()):,} at 0%")
    result.reset_index().sort_values("code_insee").to_csv(ctx.out_path, index=False)
