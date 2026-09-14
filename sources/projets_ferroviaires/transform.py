"""OSM construction/proposed railway → data/processed/projets_ferroviaires.geojson.

The Overpass query is a bounding box, and the bounding box of metropolitan
France contains substantial parts of five neighbouring countries. Clipping is
done against the commune geometry this project already builds, so "in France"
means the same thing here as it does in every other layer.
"""
from __future__ import annotations

import collections
import json

import geopandas as gpd
from shapely.geometry import LineString, mapping

from pipeline.common import BuildError, Log, human_bytes, write_geojson

# OSM records the eventual mode in construction:railway / proposed:railway.
MODES = {
    "rail": "train",
    "light_rail": "tram_train",
    "tram": "tram",
    "subway": "metro",
    "monorail": "metro",
    "narrow_gauge": "train",
    "funicular": "funiculaire",
}


def mode_of(tags: dict) -> str:
    for key in ("construction:railway", "proposed:railway", "construction", "proposed"):
        value = tags.get(key)
        if value in MODES:
            return MODES[value]
    return "train"


def is_highspeed(tags: dict) -> bool:
    if tags.get("highspeed") == "yes":
        return True
    name = (tags.get("name") or "").upper()
    return "LGV" in name or "GRANDE VITESSE" in name or "LIGNE NOUVELLE" in name


def transform(ctx) -> None:
    cfg = ctx.meta["overpass"]
    statuts = ctx.meta["statuts"]
    path = ctx.raw_dir / cfg["filename"]
    if not path.exists():
        raise BuildError(f"missing {path}; run fetch first")

    ways = json.loads(path.read_text(encoding="utf-8")).get("elements") or []
    Log.info(f"{len(ways):,} ways in the Overpass result")

    rows, geoms = [], []
    for way in ways:
        coords = [(p["lon"], p["lat"]) for p in way.get("geometry") or []]
        if len(coords) < 2:
            continue
        tags = way.get("tags") or {}
        statut = statuts.get(tags.get("railway"))
        if statut is None:
            continue
        rows.append(
            {
                "osm_id": way.get("id"),
                "nom": tags.get("name") or tags.get("construction:name") or None,
                "statut": statut,
                "mode": mode_of(tags),
                "grande_vitesse": is_highspeed(tags),
                "operateur": tags.get("operator") or None,
            }
        )
        geoms.append(LineString(coords))

    gdf = gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:4326")
    Log.info(f"{len(gdf):,} usable segments before clipping to France")

    # Clip to France the same way every other layer defines France.
    communes = gpd.read_file(ctx.communes_geojson(), columns=["geometry"])
    Log.info(f"clipping against {len(communes):,} commune polygons")
    inside = gpd.sjoin(gdf, communes.to_crs(gdf.crs), predicate="intersects", how="inner")
    keep = sorted(set(inside.index))
    dropped = len(gdf) - len(keep)
    gdf = gdf.loc[keep]
    Log.info(f"{dropped:,} segment(s) outside France dropped (the bbox includes five neighbours)")

    gdf["longueur_km"] = (gdf.to_crs("EPSG:2154").geometry.length / 1000).round(2)

    features = []
    for rec, geom in zip(gdf.drop(columns="geometry").to_dict("records"), gdf.geometry):
        props = {k: rec[k] for k in ("nom", "statut", "mode", "grande_vitesse", "operateur", "osm_id")}
        props["longueur_km"] = float(rec["longueur_km"])
        features.append({"type": "Feature", "properties": props, "geometry": mapping(geom)})

    by_statut = collections.Counter(f["properties"]["statut"] for f in features)
    by_mode = collections.Counter(f["properties"]["mode"] for f in features)
    hs = [f for f in features if f["properties"]["grande_vitesse"]]
    km = collections.defaultdict(float)
    for f in features:
        km[f["properties"]["statut"]] += f["properties"]["longueur_km"]

    Log.info(f"{len(features):,} segments in France · {sum(km.values()):,.0f} km")
    for k, n in by_statut.most_common():
        Log.info(f"  {k:<12} {n:>5,} segments · {km[k]:>8,.0f} km")
    for k, n in by_mode.most_common():
        Log.info(f"  {k:<12} {n:>5,}")
    Log.info(f"  high-speed   {len(hs):>5,} segments · "
             f"{sum(f['properties']['longueur_km'] for f in hs):,.0f} km")

    named = collections.Counter(
        f["properties"]["nom"] for f in features if f["properties"]["nom"]
    )
    Log.info("  largest named projects:")
    for name, _ in named.most_common(8):
        total = sum(f["properties"]["longueur_km"] for f in features if f["properties"]["nom"] == name)
        Log.info(f"    {name[:52]:<52} {total:>7,.0f} km")

    write_geojson(features, ctx.out_path)
    Log.info(f"output {human_bytes(ctx.out_path.stat().st_size)}")
