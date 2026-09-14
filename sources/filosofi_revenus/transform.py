"""Income, inequality and poverty per commune → data/processed/filosofi_revenus.csv.

Two members of the FiLoSoFi zip, 732 and 141 columns wide, reduced to the seven
that get mapped. The only real subtlety is INSEE's suppression marker.
"""
from __future__ import annotations

import csv
import io
import zipfile

import pandas as pd

from pipeline.common import BuildError, Log, is_metropolitan, normalise_insee

KEY = "CODGEO"


def parse(value: str, marker: str):
    """INSEE writes 's' for a figure withheld under statistical secrecy.

    Returning None rather than 0 is the whole point: a commune too small to
    publish an inequality ratio for is not an equal commune.
    """
    text = (value or "").strip()
    if not text or text == marker or text in ("ND", "NA"):
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def read_member(zf: zipfile.ZipFile, name: str, wanted: dict, marker: str) -> dict:
    with zf.open(name) as raw:
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline=""), delimiter=";")
        missing = [c for c in wanted if c not in (reader.fieldnames or [])]
        if missing:
            raise BuildError(f"{name} is missing expected column(s): {', '.join(missing)}")
        out = {}
        for row in reader:
            code = normalise_insee(row.get(KEY))
            if code is None or not is_metropolitan(code):
                continue
            out[code] = {out_name: parse(row.get(src), marker) for src, out_name in wanted.items()}
    return out


def transform(ctx) -> None:
    res = ctx.meta["resource"]
    cols = ctx.meta["columns"]
    marker = ctx.meta["suppressed_marker"]
    zip_path = ctx.raw_dir / res["filename"]

    with zipfile.ZipFile(zip_path) as zf:
        disp = read_member(zf, res["members"]["disp"], cols["disp"], marker)
        pauvres = read_member(zf, res["members"]["pauvres"], cols["pauvres"], marker)

    Log.info(f"{len(disp):,} communes in the income file · {len(pauvres):,} in the poverty file")

    rows = []
    for code in sorted(set(disp) | set(pauvres)):
        row = {"code_insee": code}
        row.update(disp.get(code, {}))
        row.update(pauvres.get(code, {}))
        rows.append(row)

    df = pd.DataFrame(rows)
    # Round to the precision each indicator is actually published at, so the
    # popup does not imply accuracy the source does not claim.
    for col, places in (
        ("revenu_median", 0), ("revenu_d1", 0), ("revenu_d9", 0),
        ("revenu_interdecile", 2), ("revenu_gini", 3),
        ("taux_pauvrete", 1), ("menages_fiscaux", 0),
    ):
        if col in df:
            df[col] = df[col].round(places)
            if places == 0:
                df[col] = df[col].astype("Int64")

    df = df.sort_values("code_insee")
    total = len(df)
    for col in ("revenu_median", "revenu_interdecile", "revenu_gini", "taux_pauvrete"):
        have = int(df[col].notna().sum())
        Log.info(f"  {col:<20} {have:>6,} communes ({have / total:.1%}) · median {df[col].median():,.2f}")
    Log.info(f"{total:,} communes written; blanks are INSEE statistical secrecy, not zeros")
    df.to_csv(ctx.out_path, index=False)
