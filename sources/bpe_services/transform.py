"""Everyday-services basket per commune → data/processed/bpe_services.csv.

Scores each commune 0–20 against the basket declared in source.yaml. A slot
counts as filled when the commune has at least one facility of any code in it.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from collections import defaultdict

import pandas as pd

from pipeline.common import Log, is_metropolitan, normalise_insee

# DS_BPE_*_data.csv is a tidy long file; see bpe_alimentation for the full header.
GEO, GEO_OBJECT, FACILITY_TYPE, OBS_VALUE = 0, 1, 4, 10
COMMUNE_LEVEL = "COM"
GP_CODE = "D265"


def classify(score: int, cuts: dict) -> str:
    """Five classes on the 0–20 basket score, thresholds from source.yaml."""
    if score <= cuts["desert"]:
        return "desert"
    if score <= cuts["tres_limite"]:
        return "tres_limite"
    if score <= cuts["limite"]:
        return "limite"
    if score <= cuts["correct"]:
        return "correct"
    return "complet"


def transform(ctx) -> None:
    res = ctx.meta["resource"]
    basket = ctx.meta["basket"]
    cuts = ctx.meta["classes"]

    # code → the slot it fills, so one pass over the file can bucket every row.
    slot_of = {}
    for theme, slots in basket.items():
        for slot, codes in slots.items():
            for code in codes:
                slot_of[code] = (theme, slot)
    Log.info(f"basket: {sum(len(s) for s in basket.values())} slots over {len(slot_of)} BPE codes")

    communes = json.loads(ctx.communes_geojson().read_text(encoding="utf-8"))
    codes = sorted(
        {
            f["properties"]["code_insee"]
            for f in communes["features"]
            if (f.get("properties") or {}).get("code_insee")
        }
    )
    present: dict[str, set] = defaultdict(set)
    gps: dict[str, int] = defaultdict(int)
    known = set(codes)

    seen = matched = 0
    zip_path = ctx.raw_dir / res["filename"]
    with zipfile.ZipFile(zip_path) as zf, zf.open(res["member"]) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8"), delimiter=";")
        next(reader, None)  # header
        for row in reader:
            if len(row) <= OBS_VALUE or row[GEO_OBJECT] != COMMUNE_LEVEL:
                continue
            slot = slot_of.get(row[FACILITY_TYPE])
            if slot is None:
                continue
            seen += 1
            try:
                count = int(row[OBS_VALUE])
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue
            code = normalise_insee(row[GEO])
            if code is None or not is_metropolitan(code) or code not in known:
                continue
            present[code].add(slot)
            if row[FACILITY_TYPE] == GP_CODE:
                gps[code] += count
            matched += 1

    Log.info(f"{seen:,} commune-level basket rows read · {matched:,} landed on a 2026 commune")

    themes = list(basket)
    rows = []
    for code in codes:
        filled = present.get(code, set())
        per_theme = {t: sum(1 for th, _ in filled if th == t) for t in themes}
        score = sum(per_theme.values())
        rows.append(
            {
                "code_insee": code,
                "serv_panier": score,
                "serv_desert": classify(score, cuts),
                "serv_sante": per_theme["sante"],
                "serv_ecole": per_theme["ecole"],
                "serv_public": per_theme["public"],
                "serv_generalistes": gps.get(code, 0),
            }
        )

    df = pd.DataFrame(rows).sort_values("code_insee")
    Log.info(f"{len(df):,} communes · median basket score {df['serv_panier'].median():.0f} of 20")
    counts = df["serv_desert"].value_counts()
    for name in ("desert", "tres_limite", "limite", "correct", "complet"):
        n = int(counts.get(name, 0))
        Log.info(f"  {name:<12} {n:>6,} communes ({n / len(df):.1%})")
    with_gp = int((df["serv_generalistes"] > 0).sum())
    Log.info(f"{with_gp:,} communes have at least one GP ({with_gp / len(df):.1%})")
    df.to_csv(ctx.out_path, index=False)
