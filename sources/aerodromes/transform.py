"""Aerodromes → data/processed/aerodromes.geojson (points).

BD TOPO publishes an aerodrome as its ground footprint. A polygon is the wrong
shape for this layer — what matters is where the airport is and how big it is,
not the exact edge of the apron — so each footprint becomes a point at its
centroid carrying the surface area as an attribute.
"""
from __future__ import annotations

import collections
import json

import geopandas as gpd

from pipeline.common import METRO_BBOX, BuildError, Log, human_bytes, write_geojson


def load(path):
    if not path.exists():
        raise BuildError(f"missing {path}; run fetch first")
    return json.loads(path.read_text(encoding="utf-8"))


def transform(ctx) -> None:
    wfs = ctx.meta["wfs"]
    cats = ctx.meta["categories"]
    natures = ctx.meta["natures"]
    files = {e["name"]: e["file"] for e in wfs["typenames"]}

    aero_path = ctx.raw_dir / files["BDTOPO_V3:aerodrome"]
    peb_path = ctx.raw_dir / files["dgac_peb_arrete_wfs:dgac_peb_arrete_wfs"]

    gdf = gpd.read_file(aero_path).set_crs(ctx.meta["source_crs"], allow_override=True)
    Log.info(f"{len(gdf):,} aerodrome records")

    if ctx.meta.get("drop_fictif", True) and "fictif" in gdf.columns:
        before = len(gdf)
        gdf = gdf[~gdf["fictif"].fillna(False).astype(bool)]
        Log.info(f"{before - len(gdf):,} placeholder (fictif) record(s) dropped")

    # Area is a real area only in a metric CRS, so it is taken before reprojecting.
    gdf["surface_ha"] = (gdf.geometry.area / 10_000).round(1)
    gdf = gdf.to_crs("EPSG:4326")
    gdf["geometry"] = gdf.geometry.make_valid()
    gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()]

    # The DGAC index: which aerodromes carry a statutory noise-exposure plan.
    peb = load(peb_path)["features"]
    peb_by_icao = {}
    for f in peb:
        p = f.get("properties") or {}
        icao = (p.get("oaci") or "").strip().upper()
        if icao:
            peb_by_icao[icao] = p.get("arrete_peb")
    Log.info(f"{len(peb_by_icao):,} aerodromes with a PEB decree on record")

    minx, miny, maxx, maxy = METRO_BBOX
    features, outside, matched = [], 0, 0
    unknown_cat, unknown_nat = collections.Counter(), collections.Counter()

    for rec, geom in zip(gdf.drop(columns="geometry").to_dict("records"), gdf.geometry):
        pt = geom.representative_point()
        if not (minx <= pt.x <= maxx and miny <= pt.y <= maxy):
            outside += 1
            continue

        cat_raw, nat_raw = rec.get("categorie"), rec.get("nature")
        if cat_raw not in cats:
            unknown_cat[cat_raw] += 1
        if nat_raw not in natures:
            unknown_nat[nat_raw] += 1

        icao = (rec.get("code_icao") or "").strip().upper() or None
        arrete = peb_by_icao.get(icao) if icao else None
        if arrete:
            matched += 1

        features.append(
            {
                "type": "Feature",
                "properties": {
                    "nom": rec.get("toponyme"),
                    "categorie": cats.get(cat_raw, "local"),
                    "nature": natures.get(nat_raw, "aerodrome"),
                    "usage": rec.get("usage"),
                    "code_icao": icao,
                    "code_iata": (rec.get("code_iata") or "").strip().upper() or None,
                    "surface_ha": float(rec["surface_ha"]) if rec.get("surface_ha") == rec.get("surface_ha") else None,
                    "peb": bool(arrete),
                    "peb_arrete": arrete,
                },
                "geometry": {"type": "Point", "coordinates": [round(pt.x, 6), round(pt.y, 6)]},
            }
        )

    for label, counter in (("categorie", unknown_cat), ("nature", unknown_nat)):
        if counter:
            Log.warn(f"unmapped {label} value(s): " + ", ".join(f"{k!r} ({n})" for k, n in counter.items()))
    if outside:
        Log.info(f"{outside:,} aerodrome(s) outside metropolitan France dropped")

    by_cat = collections.Counter(f["properties"]["categorie"] for f in features)
    by_nat = collections.Counter(f["properties"]["nature"] for f in features)
    Log.info(f"{len(features):,} aerodromes in metropolitan France")
    for k, n in by_cat.most_common():
        Log.info(f"  {k:<14} {n:>5,}")
    for k, n in by_nat.most_common():
        Log.info(f"  {k:<14} {n:>5,}")
    Log.info(f"  with a PEB    {matched:>5,} ({matched / max(1, len(features)):.1%})")
    Log.info(f"  with ICAO     {sum(1 for f in features if f['properties']['code_icao']):>5,}")

    features.sort(key=lambda f: -(f["properties"]["surface_ha"] or 0))
    write_geojson(features, ctx.out_path)
    Log.info(f"output {human_bytes(ctx.out_path.stat().st_size)}")
