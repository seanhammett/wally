"""Labelled cultural venues → data/processed/culture_lieux.geojson.

Keeps the Basilic rows whose label code is declared in source.yaml, weights each
by its label (or, for a Musée de France, by its attendance), and drops a second
label on the same site in the same category.
"""
from __future__ import annotations

import collections
import csv
import math
import statistics

from pipeline.common import METRO_BBOX, BuildError, Log, human_bytes, is_metropolitan, normalise_insee, write_geojson

ATTENDANCE = "attendance"


def read_csv(path) -> list[dict]:
    if not path.exists():
        raise BuildError(f"missing {path}; run fetch first")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    # Some exports carry a second byte-order mark inside the first header.
    return [{k.lstrip("﻿"): v for k, v in row.items()} for row in rows]


def number(value) -> float | None:
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def museum_visitors(rows: list[dict], years: list[int]) -> tuple[dict[str, int], set[str]]:
    """Muséofile id → median of its positive annual totals over the chosen years,
    and every id the file knows at all (a museum may be listed with no figure)."""
    wanted = {str(y) for y in years}
    known = set()
    totals: dict[str, list[float]] = collections.defaultdict(list)
    for row in rows:
        mid = (row.get("IDMuseofile") or "").strip()
        known.add(mid)
        if row.get("annee") not in wanted:
            continue
        total = number(row.get("total"))
        if total and total > 0:
            totals[mid].append(total)
    return {mid: int(statistics.median(values)) for mid, values in totals.items()}, known


def transform(ctx) -> None:
    res = ctx.meta["resources"]
    labels = ctx.meta["labels"]
    mw = ctx.meta["museum_weight"]

    basilic = read_csv(ctx.raw_dir / res["basilic"]["filename"])
    visitors, listed = museum_visitors(read_csv(ctx.raw_dir / res["frequentation"]["filename"]), mw["years"])
    Log.info(f"{len(basilic):,} Basilic rows · attendance for {len(visitors):,} Musées de France")

    category_of = {code: (cat, weight) for cat, codes in labels.items() for code, weight in codes.items()}
    missing = set(category_of) - {row["Ident"] for row in basilic}
    if missing:
        raise BuildError(f"label code(s) in source.yaml match no Basilic row: {', '.join(sorted(missing))} "
                         "— the Ministry may have renamed them")

    unmapped = collections.Counter(row["Ident"] for row in basilic if row["Ident"] not in category_of)
    Log.info("left out: " + " · ".join(f"{code} {n:,}" for code, n in unmapped.most_common()))

    minx, miny, maxx, maxy = METRO_BBOX
    best: dict[tuple, dict] = {}
    outside = doubled = museums = matched = found = 0
    for row in basilic:
        spec = category_of.get(row["Ident"])
        if spec is None:
            continue
        lon, lat = number(row.get("Longitude")), number(row.get("Latitude"))
        code = normalise_insee(row.get("code_insee"))
        if lon is None or lat is None or not (minx <= lon <= maxx and miny <= lat <= maxy) \
                or (code and not is_metropolitan(code)):
            outside += 1
            continue

        cat, weight = spec
        seen = None
        if weight == ATTENDANCE:
            museums += 1
            mid = row["Identifiant origine"].strip()
            found += mid in listed
            seen = visitors.get(mid)
            if seen is None:
                weight = float(mw["unreported"])
            else:
                matched += 1
                weight = 1 + math.log10(seen / float(mw["reference_visitors"]))
                weight = min(max(weight, float(mw["min"])), float(mw["max"]))

        feature = {
            "type": "Feature",
            "properties": {
                "nom": row["Nom"].strip(),
                "code_insee": code,
                "commune": row.get("libelle_geographique"),
                "categorie": cat,
                "label": row["Label et appellation"] or row["Type équipement ou lieu"],
                "poids": round(float(weight), 2),
                "visiteurs": seen,
                "jauge": int(number(row["Jauge_du_theatre"])) if number(row.get("Jauge_du_theatre")) else None,
            },
            "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
        }
        key = (cat, round(lon, 5), round(lat, 5))
        if key in best:
            doubled += 1
            if best[key]["properties"]["poids"] >= feature["properties"]["poids"]:
                continue
        best[key] = feature

    if outside:
        Log.info(f"{outside:,} labelled venue(s) outside metropolitan France dropped")
    if doubled:
        Log.info(f"{doubled:,} second label(s) on the same site and category dropped, the stronger kept")
    if museums:
        rate = found / museums
        Log.info(f"Musées de France: {found:,} of {museums:,} listed in the attendance file ({rate:.1%}), "
                 f"{matched:,} with a figure in {mw['years'][0]}–{mw['years'][-1]}; "
                 f"the other {museums - matched:,} weigh {mw['unreported']}")
        if rate < float(ctx.meta["min_attendance_match"]):
            raise BuildError(f"only {rate:.1%} of Musées de France are in the attendance file "
                             f"(need {float(ctx.meta['min_attendance_match']):.0%}) — check the Muséofile ids")

    features = sorted(best.values(), key=lambda f: -f["properties"]["poids"])
    for cat in labels:
        rows = [f["properties"] for f in features if f["properties"]["categorie"] == cat]
        Log.info(f"  {cat:<7} {len(rows):>5,} venues · total weight {sum(r['poids'] for r in rows):,.0f}")
        per_label = collections.Counter(r["label"] for r in rows)
        Log.info("    " + " · ".join(f"{label} {n}" for label, n in per_label.most_common()))
    top = [f["properties"] for f in features if f["properties"]["visiteurs"]]
    top.sort(key=lambda p: -p["visiteurs"])
    Log.info("  most visited museums: " + " · ".join(f"{p['nom']} ({p['visiteurs']:,})" for p in top[:5]))

    write_geojson(features, ctx.out_path)
    Log.info(f"output {human_bytes(ctx.out_path.stat().st_size)}")
