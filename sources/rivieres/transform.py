"""BD CARTO hydrographic segments → one EPSG:4326 GeoJSON line layer.

The only real decisions here are what to call things and which zoom each segment
earns. Everything else is the standard Pattern B shape: read, reproject once,
drop what falls outside metropolitan France, write.
"""
from __future__ import annotations

import json

import geopandas as gpd
import pandas as pd
from shapely.geometry import mapping

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


def load_pages(ctx) -> gpd.GeoDataFrame:
    pages = sorted(ctx.raw_dir.glob("troncon-*.geojson"))
    if not pages:
        raise BuildError(f"no WFS pages in {ctx.raw_dir}; run fetch first")
    frames = []
    for path in pages:
        gdf = gpd.read_file(path)
        if len(gdf):
            frames.append(gdf)
    gdf = pd.concat(frames, ignore_index=True)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=frames[0].crs)
    Log.info(f"{len(pages)} page(s) → {len(gdf):,} segments")

    # Paging is only safe because the request sorts by cleabs; if that ever stops
    # holding, the duplicates show up here rather than as a thicker Loire.
    before = len(gdf)
    gdf = gdf.drop_duplicates(subset="cleabs")
    if len(gdf) != before:
        Log.warn(f"dropped {before - len(gdf):,} duplicate segment(s) across page boundaries")
    return gdf


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

    minx, miny, maxx, maxy = METRO_BBOX
    b = gdf.geometry.bounds
    keep = (b.maxx > minx) & (b.minx < maxx) & (b.maxy > miny) & (b.miny < maxy)
    if (~keep).any():
        Log.info(f"{int((~keep).sum()):,} segment(s) outside metropolitan France dropped")
    gdf = gdf[keep]

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

        name = rec.get("cpx_toponyme_de_cours_d_eau") or None
        # BD CARTO packs alternative names into one field with a slash
        # ("la Neuve Rivière/la Neuve Livière"). The first is the retained form.
        if name and "/" in name:
            name = name.split("/", 1)[0]

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

    write_geojson(features, ctx.out_path)
    Log.info(f"output {human_bytes(ctx.out_path.stat().st_size)}")
