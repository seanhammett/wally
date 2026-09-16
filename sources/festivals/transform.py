"""Festivals → data/processed/festivals.geojson.

One point per festival, weighted by its declared reach and how long it has run
(both scales in source.yaml).
"""
from __future__ import annotations

import collections
import csv
import re

from pipeline.common import METRO_BBOX, BuildError, Log, human_bytes, is_metropolitan, normalise_insee, write_geojson

YEAR = re.compile(r"(1[89]\d\d|20[0-2]\d)")


def founding_year(row) -> int | None:
    """`Année de création` arrives as 2005, 2005.0 or 01/01/2005."""
    match = YEAR.search(row.get("Année de création du festival") or "")
    return int(match.group(1)) if match else None


def reach_weight(text: str, patterns: list[dict]) -> float:
    for spec in patterns:
        if re.search(spec["match"], text, flags=re.IGNORECASE):
            return float(spec["weight"])
    return 1.0


def weight(row, year: int | None, meta) -> float:
    reach = reach_weight(row.get("Envergure territoriale") or "", meta["envergure"])
    factor = 1.0
    if year is not None:
        for step in meta["longevite"]:
            if year < int(step["before"]):
                factor = float(step["factor"])
                break
    return round(reach * factor, 2)


def transform(ctx) -> None:
    path = ctx.raw_dir / ctx.meta["resource"]["filename"]
    if not path.exists():
        raise BuildError(f"missing {path}; run fetch first")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        # The first header carries a second byte-order mark of its own.
        rows = [{k.lstrip("﻿"): v for k, v in row.items()} for row in csv.DictReader(fh, delimiter=";")]
    if rows and "Nom du festival" not in rows[0]:
        raise BuildError(f"festival list: no 'Nom du festival' column; got {', '.join(list(rows[0])[:6])}")
    Log.info(f"{len(rows):,} festivals")

    minx, miny, maxx, maxy = METRO_BBOX
    features, no_pos, outside = [], 0, 0
    for row in rows:
        try:
            lat, lon = (float(p) for p in (row.get("Géocodage xy") or "").split(","))
        except ValueError:
            no_pos += 1
            continue
        code = normalise_insee(row.get("Code Insee commune"))
        if not (minx <= lon <= maxx and miny <= lat <= maxy) or (code and not is_metropolitan(code)):
            outside += 1
            continue
        year = founding_year(row)
        features.append({
            "type": "Feature",
            "properties": {
                "nom": row["Nom du festival"].strip(),
                "code_insee": code,
                "commune": row.get("Commune principale de déroulement"),
                "discipline": row.get("Discipline dominante") or None,
                "periode": (row.get("Période principale de déroulement du festival") or "").strip().capitalize() or None,
                "annee_creation": year,
                "envergure": (row.get("Envergure territoriale") or "").strip().capitalize() or None,
                "poids": weight(row, year, ctx.meta),
                "site_web": (row.get("Site internet du festival") or "").strip() or None,
            },
            "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
        })

    if no_pos:
        Log.info(f"{no_pos:,} festival(s) had no coordinates")
    if outside:
        Log.info(f"{outside:,} festival(s) outside metropolitan France dropped")
    if not features:
        raise BuildError("no festival left after filtering")

    props = [f["properties"] for f in features]
    Log.info(f"{len(features):,} festivals · total weight {sum(p['poids'] for p in props):,.0f}")
    reach = collections.Counter(reach_weight(p["envergure"] or "", ctx.meta["envergure"]) for p in props)
    Log.info("  reach weight: " + " · ".join(f"{w:g} {n:,}" for w, n in sorted(reach.items())))
    for key in ("discipline", "poids"):
        counts = collections.Counter(p[key] for p in props)
        Log.info(f"  {key}: " + " · ".join(f"{v} {n:,}" for v, n in sorted(counts.items(), key=lambda kv: -kv[1])))
    towns = collections.Counter(p["commune"] for p in props)
    Log.info("  most festivals: " + " · ".join(f"{t} {n}" for t, n in towns.most_common(8)))

    features.sort(key=lambda f: -f["properties"]["poids"])
    write_geojson(features, ctx.out_path)
    Log.info(f"output {human_bytes(ctx.out_path.stat().st_size)}")
