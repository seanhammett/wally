"""Projected summer low flow and its duration → data/processed/explore2_etiage.csv.

Two RARE files, each long-format and 130–160 MB: VCN10 (how low the summer
minimum gets) and dtBE (how long the low-water period lasts). Two warming levels
and three statistics are kept and pivoted into one row per commune.
"""
from __future__ import annotations

import pandas as pd

from pipeline.common import Log
from pipeline.explore2 import opt, read_commune_indicator


def transform(ctx) -> None:
    resources = ctx.meta["resources"]
    warming = ctx.meta["warming"]
    stats = ctx.meta["statistics"]
    ref, high = warming["reference"], warming["high"]
    levels = {ref, high}
    wanted = {stats["median"], stats["low"], stats["high"]}

    def read(key):
        res = resources[key]
        return read_commune_indicator(ctx.raw_dir / res["filename"], levels, wanted, res["indicator"])

    values, origin = read("vcn10")
    duration, _ = read("duration")

    out = []
    for code in sorted(values):
        v = values[code]
        median = v.get((ref, stats["median"]))
        if median is None:
            continue
        d = duration.get(code, {})
        out.append(
            {
                "code_insee": code,
                "etiage_27_pct": round(median, 1),
                "etiage_27_min": opt(v.get((ref, stats["low"]))),
                "etiage_27_max": opt(v.get((ref, stats["high"]))),
                "etiage_4_pct": opt(v.get((high, stats["median"]))),
                "etiage_origine": origin.get(code, ""),
                "etiage_duree_27": opt(d.get((ref, stats["median"]))),
                "etiage_duree_27_min": opt(d.get((ref, stats["low"]))),
                "etiage_duree_27_max": opt(d.get((ref, stats["high"]))),
                "etiage_duree_4": opt(d.get((high, stats["median"]))),
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
    dur = pd.to_numeric(df["etiage_duree_27"], errors="coerce")
    longer = int((dur > 0).sum())
    Log.info(f"  low-water period: median {dur.median():+.0f} days at {ref}, longer in {longer:,} communes "
             f"({longer / dur.notna().sum():.1%}); {int(dur.isna().sum()):,} without a value")
    df.to_csv(ctx.out_path, index=False)
