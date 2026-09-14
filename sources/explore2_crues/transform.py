"""Projected change in the annual flood peak → data/processed/explore2_crues.csv."""
from __future__ import annotations

import pandas as pd

from pipeline.common import Log
from pipeline.explore2 import opt, read_commune_indicator


def transform(ctx) -> None:
    res = ctx.meta["resource"]
    warming = ctx.meta["warming"]
    stats = ctx.meta["statistics"]
    ref, high = warming["reference"], warming["high"]
    values, origin = read_commune_indicator(ctx.raw_dir / res["filename"], {ref, high},
                                            {stats["median"], stats["low"], stats["high"]}, res["indicator"])

    out = []
    for code in sorted(values):
        v = values[code]
        median = v.get((ref, stats["median"]))
        if median is None:
            continue
        out.append(
            {
                "code_insee": code,
                "crues_27_pct": round(median, 1),
                "crues_27_min": opt(v.get((ref, stats["low"]))),
                "crues_27_max": opt(v.get((ref, stats["high"]))),
                "crues_4_pct": opt(v.get((high, stats["median"]))),
                "crues_origine": origin.get(code, ""),
            }
        )

    df = pd.DataFrame(out).sort_values("code_insee")
    higher = int((df["crues_27_pct"] > 0).sum())
    Log.info(f"{len(df):,} communes · median change {df['crues_27_pct'].median():+.1f}% at {ref}, "
             f"higher flood peak in {higher:,} ({higher / len(df):.1%})")
    at4 = pd.to_numeric(df["crues_4_pct"], errors="coerce")
    Log.info(f"  median change at {high}: {at4.median():+.1f}%")
    df.to_csv(ctx.out_path, index=False)
