"""Commune-aggregated DRIAS heat indicators → data/processed/drias_chaleur.csv.

Expects the zonal-statistics step to have already happened (see the instructions
in source.yaml); this file only normalises and range-checks the result.
"""
import pandas as pd

from pipeline.common import Log, is_metropolitan, normalise_insee, require_manual

COLUMNS = {"nuits_tropicales_2050": (0, 200), "jours_35c_2050": (0, 200)}


def transform(ctx) -> None:
    path = require_manual(ctx.source, ctx.meta["expected_files"])[0]

    # French exports are semicolon-separated with a comma decimal mark far more
    # often than not, and are as likely to be Latin-1 as UTF-8.
    for encoding in ("utf-8", "latin-1"):
        try:
            df = pd.read_csv(path, sep=";", decimal=",", dtype={"code_insee": str}, encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise RuntimeError(f"could not decode {path} as UTF-8 or Latin-1")

    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise RuntimeError(f"{path.name} is missing expected column(s): {', '.join(missing)}")

    df["code_insee"] = df["code_insee"].map(normalise_insee)
    df = df[df["code_insee"].notna() & df["code_insee"].map(is_metropolitan)]

    for col, (lo, hi) in COLUMNS.items():
        values = pd.to_numeric(df[col], errors="coerce")
        out_of_range = int(((values < lo) | (values > hi)).sum())
        if out_of_range:
            raise RuntimeError(
                f"{col}: {out_of_range} value(s) outside the plausible range {lo}–{hi} — "
                "the zonal-statistics step is probably misaligned"
            )
        df[col] = values.round(1)

    df = df[["code_insee", *COLUMNS]].drop_duplicates(subset="code_insee").sort_values("code_insee")
    Log.info(f"{len(df):,} communes · median {df['nuits_tropicales_2050'].median():.0f} tropical nights by 2050")
    df.to_csv(ctx.out_path, index=False)
