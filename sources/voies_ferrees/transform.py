"""SNCF line exports → one EPSG:4326 GeoJSON line layer.

Geometry and category come from the LGV/gauge export. Speed and status are
joined on `code_ligne`, because the three files segment the network at different
points and only the line code is common to all of them.
"""
from __future__ import annotations

import collections
import json

from shapely.geometry import shape, mapping

from pipeline.common import METRO_BBOX, BuildError, Log, human_bytes, write_geojson

# Statuses that mean a train can still run. Everything else is a line that has
# been closed, lifted, sold off or turned into sidings.
LIVE_STATUS = {"Exploitée", "S9A3 - Ligne en travaux"}


def load(path):
    if not path.exists():
        raise BuildError(f"missing {path}; run fetch first")
    return json.loads(path.read_text(encoding="utf-8"))


def dedupe(features: list[dict]) -> list[dict]:
    """The geometry export ships every segment twice.

    Each line segment appears once with its PK range filled in and once with
    `pkd`/`pkf` null, and the two carry byte-identical geometry. Left in, the
    network is drawn twice and every length is double the truth — which is how
    the LGV network first came out at 5,214 km against a real 2,607. The copy
    with the PK range is the one kept.
    """
    best: dict = {}
    for feat in features:
        props = feat.get("properties") or {}
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        if not coords:
            continue
        key = (props.get("code_ligne"), props.get("rg_troncon"),
               tuple(coords[0]), tuple(coords[-1]), len(coords))
        current = best.get(key)
        if current is None or (props.get("pkd") and not (current["properties"] or {}).get("pkd")):
            best[key] = feat
    dropped = len(features) - len(best)
    if dropped:
        Log.info(f"{dropped:,} duplicate record(s) dropped — the export ships each segment twice")
    return list(best.values())


def transform(ctx) -> None:
    ex = ctx.meta["exports"]
    cats = ctx.meta["categories"]
    tiers = ctx.meta["zoom_tiers"]

    base = load(ctx.raw_dir / ex["base"]["filename"])["features"]
    vitesse = load(ctx.raw_dir / ex["vitesse"]["filename"])
    statut = load(ctx.raw_dir / ex["statut"]["filename"])
    Log.info(f"{len(base):,} geometry records · {len(vitesse):,} speed · {len(statut):,} status records")

    base = dedupe(base)

    # Fastest speed recorded anywhere on each line.
    speed_by_line: dict[str, int] = {}
    for row in vitesse:
        code, raw = row.get("code_ligne"), row.get("v_max")
        if not code or raw in (None, ""):
            continue
        try:
            v = int(float(raw))
        except (TypeError, ValueError):
            continue
        if v > speed_by_line.get(code, 0):
            speed_by_line[code] = v

    # A line counts as live if any part of it is still exploited.
    status_by_line: dict[str, set] = collections.defaultdict(set)
    for row in statut:
        if row.get("code_ligne") and row.get("statut"):
            status_by_line[row["code_ligne"]].add(row["statut"])

    unknown_cats = collections.Counter()
    minx, miny, maxx, maxy = METRO_BBOX
    features, dropped_bbox, invalid = [], 0, 0

    for feat in base:
        props = feat.get("properties") or {}
        geom = feat.get("geometry")
        if not geom:
            invalid += 1
            continue
        catlig = props.get("catlig")
        kind = cats.get(catlig)
        if kind is None:
            unknown_cats[catlig] += 1
            kind = "conventionnel"

        try:
            g = shape(geom)
            if not g.is_valid:
                g = g.buffer(0) if g.geom_type.endswith("Polygon") else g
            if g.is_empty:
                invalid += 1
                continue
        except Exception:
            invalid += 1
            continue

        gminx, gminy, gmaxx, gmaxy = g.bounds
        if gmaxx < minx or gminx > maxx or gmaxy < miny or gminy > maxy:
            dropped_bbox += 1
            continue

        code = props.get("code_ligne")
        statuses = status_by_line.get(code, set())
        features.append(
            {
                "type": "Feature",
                "tippecanoe": {"minzoom": int(tiers.get(kind, 8))},
                "properties": {
                    "code_ligne": code,
                    "type_ligne": kind,
                    "v_max": speed_by_line.get(code),
                    "exploitee": bool(statuses & LIVE_STATUS) if statuses else None,
                    "longueur_km": None,   # filled below, in metres-based CRS
                },
                "geometry": mapping(g),
            }
        )

    if unknown_cats:
        Log.warn(
            "unmapped catlig value(s), classed as conventional — add them to `categories`: "
            + ", ".join(f"{k!r} ({n})" for k, n in unknown_cats.items())
        )
    if invalid:
        Log.info(f"{invalid:,} segment(s) had no usable geometry")
    if dropped_bbox:
        Log.info(f"{dropped_bbox:,} segment(s) outside metropolitan France dropped")

    # Length in Lambert-93 metres, where a length is a length.
    import geopandas as gpd

    gdf = gpd.GeoDataFrame(
        {"i": range(len(features))},
        geometry=[shape(f["geometry"]) for f in features],
        crs="EPSG:4326",
    ).to_crs("EPSG:2154")
    for feat, length in zip(features, gdf.geometry.length):
        feat["properties"]["longueur_km"] = round(length / 1000, 2)

    by_type = collections.Counter(f["properties"]["type_ligne"] for f in features)
    have_speed = sum(1 for f in features if f["properties"]["v_max"] is not None)
    live = sum(1 for f in features if f["properties"]["exploitee"])
    km = {k: 0.0 for k in by_type}
    for f in features:
        km[f["properties"]["type_ligne"]] += f["properties"]["longueur_km"]

    Log.info(f"{len(features):,} segments · {sum(km.values()):,.0f} km of track")
    for k, n in by_type.most_common():
        Log.info(f"  {k:<16} {n:>6,} segments · {km[k]:>9,.0f} km")
    Log.info(f"  with a speed    {have_speed:>6,} ({have_speed / len(features):.1%})")
    Log.info(f"  still exploited {live:>6,} ({live / len(features):.1%})")
    fast = [f["properties"]["v_max"] for f in features if f["properties"]["v_max"]]
    Log.info(f"  speed range     {min(fast)}–{max(fast)} km/h")

    write_geojson(features, ctx.out_path)
    Log.info(f"output {human_bytes(ctx.out_path.stat().st_size)}")
