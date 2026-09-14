"""Projected change in summer low flow → data/processed/explore2_etiage.csv.

The RARE file is long-format and 154 MB: one row per commune × warming level ×
ensemble statistic. This keeps two warming levels and three statistics and
pivots them into one row per commune.
"""
from __future__ import annotations

import csv
from collections import defaultdict

import pandas as pd

from pipeline.common import BuildError, Log, is_metropolitan, normalise_insee

# RARE's quality flag, verbatim from the file, mapped to something short enough
# to put in a popup. It says where the simulation points behind a commune's
# value actually are.
ORIGIN = {
    "Oui (bassin versant du territoire)": "propre",
    "Non (bassin versant proche)": "proche",
    "Non (bassin versant éloigné)": "eloigne",
}


def transform(ctx) -> None:
    res = ctx.meta["resource"]
    warming = ctx.meta["warming"]
    stats = ctx.meta["statistics"]
    path = ctx.raw_dir / res["filename"]

    wanted_warming = {warming["reference"], warming["high"]}
    wanted_stats = {stats["median"], stats["low"], stats["high"]}

    # (code, warming, statistic) → value, collected in one streaming pass.
    values: dict[str, dict] = defaultdict(dict)
    origin: dict[str, str] = {}
    rows_read = 0

    # utf-8-sig: the file carries a BOM, which would otherwise become part of
    # the first column name and silently break the DictReader lookup.
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        required = {"code_territoire", "rechauffement_france", "statistique", "resultat"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise BuildError(
                f"{path.name} is missing expected column(s): {', '.join(sorted(missing))} — "
                "the RARE export layout has changed"
            )
        for row in reader:
            level = row["rechauffement_france"]
            stat = row["statistique"]
            if level not in wanted_warming or stat not in wanted_stats:
                continue
            code = normalise_insee(row["code_territoire"])
            if code is None or not is_metropolitan(code):
                continue
            try:
                values[code][(level, stat)] = float(row["resultat"])
            except (TypeError, ValueError):
                continue
            origin.setdefault(code, ORIGIN.get(row.get("donnees_issues_territoire", ""), ""))
            rows_read += 1

    Log.info(f"{rows_read:,} rows kept for {len(values):,} communes")
    if not values:
        raise BuildError(f"{path.name} produced no usable rows — check the warming/statistic labels")

    ref, high = warming["reference"], warming["high"]
    out = []
    for code in sorted(values):
        v = values[code]
        median = v.get((ref, stats["median"]))
        if median is None:
            continue
        out.append(
            {
                "code_insee": code,
                "etiage_27_pct": round(median, 1),
                "etiage_27_min": _opt(v.get((ref, stats["low"]))),
                "etiage_27_max": _opt(v.get((ref, stats["high"]))),
                "etiage_4_pct": _opt(v.get((high, stats["median"]))),
                "etiage_origine": origin.get(code, ""),
            }
        )

    df = pd.DataFrame(out).sort_values("code_insee")
    drier = int((df["etiage_27_pct"] < 0).sum())
    Log.info(f"{len(df):,} communes · median change {df['etiage_27_pct'].median():.1f}% at {ref}")
    Log.info(f"  {drier:,} communes project a drier summer low flow ({drier / len(df):.1%})")
    at4 = pd.to_numeric(df["etiage_4_pct"], errors="coerce")
    Log.info(f"  median change at {high}: {at4.median():.1f}%")
    counts = df["etiage_origine"].value_counts()
    Log.info("  " + " · ".join(f"{k} {v:,} ({v / len(df):.0%})" for k, v in counts.items()))
    df.to_csv(ctx.out_path, index=False)


def _opt(value):
    """Keep blanks blank rather than writing a misleading zero."""
    return "" if value is None else round(value, 1)
