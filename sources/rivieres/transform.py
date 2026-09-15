"""BD CARTO hydrographic segments and lake surfaces → one EPSG:4326 GeoJSON.

The only real decisions here are what to call things and which zoom each feature
earns. Everything else is the standard Pattern B shape: read, reproject once,
drop what falls outside metropolitan France, write.

Lines and lake polygons share one file and one tileset. The page draws the
polygons as a fill beneath the lines, under the same switch, so rivers stay lines
and a lake is the water they run through.
"""
from __future__ import annotations

import json

import geopandas as gpd
import pandas as pd
from shapely.geometry import mapping
from shapely.ops import unary_union

from pipeline.common import METRO_BBOX, BuildError, Log, human_bytes, write_geojson

# BD CARTO `nature`, collapsed to what a reader of the map cares about. Every
# value the source actually publishes is listed: an unmapped one is reported
# rather than quietly becoming a river, because that is how a future millésime's
# new category would otherwise arrive — invisibly, and wrong.
NATURE = {
    # Flowing water, natural or in an engineered channel. A canalised stream is
    # still that stream, so it stays a river; a Canal is a built navigation and
    # does not.
    "Ecoulement naturel": "riviere",
    "Ecoulement canalisé": "riviere",
    "Ecoulement karstique": "riviere",
    "Ecoulement endoréique": "riviere",
    "Ravine": "riviere",
    "Inconnue": "riviere",
    "Canal": "canal",
    "Estuaire": "estuaire",
    "Lagune": "estuaire",
    # Standing water the network is traced through. Drawing a reservoir or a
    # marsh as a river overstates how much of the network is moving water, so
    # these carry their own class.
    "Retenue": "plan_deau",
    "Retenue-barrage": "plan_deau",
    "Retenue-digue": "plan_deau",
    "Retenue-bassin portuaire": "plan_deau",
    "Réservoir-bassin": "plan_deau",
    "Réservoir-bassin d'orage": "plan_deau",
    "Plan d'eau de gravière": "plan_deau",
    "Marais": "plan_deau",
    "Lac": "plan_deau",
}
# Buried, pressurised or frozen: engineering and ice, not waterways. A pressure
# pipe crossing a valley is not a river and a glacier is not a stream.
DROP_NATURE = {"Aqueduc", "Conduit forcé", "Conduit buse", "Glacier, névé"}

WIDTH_LABEL = {
    "Entre 5 et 15 m": "moyen",
    "Entre 15 et 50 m": "grand",
    "Plus de 50 m": "majeur",
}


def load_pages(ctx, prefix: str = "troncon") -> gpd.GeoDataFrame:
    pages = sorted(ctx.raw_dir.glob(f"{prefix}-*.geojson"))
    if not pages:
        raise BuildError(f"no WFS pages in {ctx.raw_dir}; run fetch first")
    frames = []
    for path in pages:
        gdf = gpd.read_file(path)
        if len(gdf):
            frames.append(gdf)
    gdf = pd.concat(frames, ignore_index=True)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=frames[0].crs)
    Log.info(f"{len(pages)} {prefix} page(s) → {len(gdf):,} features")

    # Paging is only safe because the request sorts by cleabs; if that ever stops
    # holding, the duplicates show up here rather than as a thicker Loire.
    before = len(gdf)
    gdf = gdf.drop_duplicates(subset="cleabs")
    if len(gdf) != before:
        Log.warn(f"dropped {before - len(gdf):,} duplicate {prefix} feature(s) across page boundaries")
    return gdf


def first_name(name):
    """BD CARTO packs alternative names into one field with a slash
    ("la Neuve Rivière/la Neuve Livière"). The first is the retained form."""
    if not isinstance(name, str) or not name:
        return None
    return name.split("/", 1)[0]


def in_metro(gdf: gpd.GeoDataFrame, what: str) -> gpd.GeoDataFrame:
    minx, miny, maxx, maxy = METRO_BBOX
    b = gdf.geometry.bounds
    keep = (b.maxx > minx) & (b.minx < maxx) & (b.maxy > miny) & (b.miny < maxy)
    if (~keep).any():
        Log.info(f"{int((~keep).sum()):,} {what} outside metropolitan France dropped")
    return gdf[keep]


def lake_features(ctx) -> list[dict]:
    """Standing water as polygons: lakes, ponds and lagoons, reservoirs and basins."""
    if not any(ctx.raw_dir.glob("surface-*.geojson")):
        raise BuildError(f"no lake surface pages in {ctx.raw_dir}; run fetch first")
    kind_of = {n: kind for kind, natures in ctx.meta["surfaces"]["natures"].items() for n in natures}
    tiers = sorted(((float(a), int(z)) for a, z in ctx.meta["surface_tiers"]), reverse=True)

    gdf = load_pages(ctx, "surface")
    gdf = gdf.set_crs(ctx.meta["source_crs"], allow_override=True)
    unmapped = sorted(set(gdf["nature"].dropna()) - set(kind_of))
    if unmapped:
        raise BuildError(f"surface natures not in source.yaml `surfaces.natures`: {', '.join(unmapped)}")

    gdf["geometry"] = gdf.geometry.make_valid()
    # make_valid can split a self-touching ring into a collection with stray
    # lines; only the area is a lake.
    gdf["geometry"] = gdf.geometry.apply(polygonal)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    gdf["surface_km2"] = (gdf.geometry.area / 1e6).round(3)
    gdf = gdf[gdf["surface_km2"] > 0]
    gdf = gdf.to_crs("EPSG:4326")
    gdf = in_metro(gdf, "lake surface(s)")

    features = []
    for rec, geom in zip(gdf.drop(columns="geometry").to_dict("records"), gdf.geometry):
        area = float(rec["surface_km2"])
        minzoom = next((z for floor, z in tiers if area >= floor), tiers[-1][1])
        features.append(
            {
                "type": "Feature",
                "tippecanoe": {"minzoom": minzoom},
                "properties": {
                    "nom": first_name(rec.get("cpx_toponyme_de_plan_d_eau")),
                    "nature": kind_of[rec["nature"]],
                    "persistance": "intermittent" if rec.get("persistance") == "Intermittent" else "permanent",
                    "surface_km2": area,
                },
                "geometry": mapping(geom),
            }
        )

    kinds = pd.Series([f["properties"]["nature"] for f in features]).value_counts()
    total = sum(f["properties"]["surface_km2"] for f in features)
    Log.info(f"{len(features):,} lake surfaces · {total:,.0f} km² of standing water")
    for label, count in kinds.items():
        Log.info(f"  {label:<12} {count:>7,}")
    for floor, z in tiers:
        n = sum(1 for f in features if f["tippecanoe"]["minzoom"] == z)
        Log.info(f"  ≥{floor:g} km² from z{z}: {n:,}")
    return features


def polygonal(geom):
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    parts = [g for g in getattr(geom, "geoms", []) if g.geom_type in ("Polygon", "MultiPolygon")]
    if not parts:
        return None
    return unary_union(parts)


def transform(ctx) -> None:
    gdf = load_pages(ctx)
    gdf = gdf.set_crs(ctx.meta["source_crs"], allow_override=True)

    before = len(gdf)
    gdf = gdf[~gdf["nature"].isin(DROP_NATURE)]
    if len(gdf) != before:
        Log.info(f"dropped {before - len(gdf):,} conduit/glacier segment(s) — not waterways")

    unmapped = sorted(set(gdf["nature"].dropna()) - set(NATURE))
    if unmapped:
        Log.warn(
            f"{len(unmapped)} unmapped `nature` value(s), drawn as rivers by default — "
            f"add them to NATURE in transform.py: {', '.join(unmapped)}"
        )

    # The whole projection problem, solved in one call.
    gdf = gdf.to_crs("EPSG:4326")

    # Length is computed in Lambert-93 metres, where it is a length, not in
    # degrees, where it is nothing.
    gdf["longueur_km"] = (gdf.to_crs("EPSG:2154").geometry.length / 1000).round(2)

    gdf["geometry"] = gdf.geometry.make_valid()
    gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()]
    # A two-identical-point LineString is valid to shapely and invisible to
    # everything else; tippecanoe keeps it and the tileset carries dead weight.
    gdf = gdf[gdf.geometry.geom_type.isin({"LineString", "MultiLineString"})]
    gdf = gdf[gdf["longueur_km"] > 0]

    gdf = in_metro(gdf, "segment(s)")

    tiers = ctx.meta["zoom_tiers"]
    nav_tier = int(ctx.meta["navigable_tier"])

    features = []
    for rec, geom in zip(gdf.drop(columns="geometry").to_dict("records"), gdf.geometry):
        classe = rec.get("classe_de_largeur")
        nature = NATURE.get(rec.get("nature"), "riviere")
        navigable = bool(rec.get("navigabilite"))
        minzoom = int(tiers.get(classe, 8))
        if navigable:
            minzoom = min(minzoom, nav_tier)

        name = first_name(rec.get("cpx_toponyme_de_cours_d_eau"))

        features.append(
            {
                "type": "Feature",
                # Read by tippecanoe, not by the browser: it decides which zoom a
                # segment first appears at, so low zooms show the Loire and the
                # Rhône rather than whatever --drop-densest-as-needed spared.
                "tippecanoe": {"minzoom": minzoom},
                "properties": {
                    "nom": name,
                    "nature": nature,
                    "largeur": WIDTH_LABEL.get(classe, "moyen"),
                    "navigable": navigable,
                    "persistance": "intermittent" if rec.get("persistance") == "Intermittent" else "permanent",
                    "longueur_km": float(rec["longueur_km"]),
                },
                "geometry": mapping(geom),
            }
        )

    named = sum(1 for f in features if f["properties"]["nom"])
    by_nature = pd.Series([f["properties"]["nature"] for f in features]).value_counts()
    by_width = pd.Series([f["properties"]["largeur"] for f in features]).value_counts()
    intermittent = sum(1 for f in features if f["properties"]["persistance"] == "intermittent")
    total_km = sum(f["properties"]["longueur_km"] for f in features)

    Log.info(f"{len(features):,} segments · {total_km:,.0f} km of waterway")
    Log.info(f"  named: {named:,} ({named / len(features):.1%})")
    for label, count in by_nature.items():
        Log.info(f"  {label:<12} {count:>7,}")
    for label in ("majeur", "grand", "moyen"):
        Log.info(f"  {label:<12} {int(by_width.get(label, 0)):>7,}")
    Log.info(f"  intermittent {intermittent:>7,} ({intermittent / len(features):.1%})")
    Log.info(f"  navigable    {sum(1 for f in features if f['properties']['navigable']):>7,}")

    features.extend(lake_features(ctx))
    write_geojson(features, ctx.out_path)
    Log.info(f"output {human_bytes(ctx.out_path.stat().st_size)}")
