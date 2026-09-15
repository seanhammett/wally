"""Presidential 2022 commune results → data/processed/presidentielle_2022.csv."""
from __future__ import annotations

import csv

import pandas as pd

from pipeline.common import BuildError, Log, is_metropolitan, normalise_insee

# Fixed-prefix positions shared by both rounds.
DEPT, COMMUNE3, TURNOUT, EXPRESSED = 0, 2, 9, 16
# Offsets inside each repeating candidate block.
NAME, VOTES, PCT_EXPRESSED = 2, 4, 6

# The surname as printed, mapped to how it should read on a legend.
DISPLAY = {
    "LE PEN": "Le Pen",
    "MACRON": "Macron",
    "MÉLENCHON": "Mélenchon",
    "LASSALLE": "Lassalle",
    "ZEMMOUR": "Zemmour",
    "PÉCRESSE": "Pécresse",
    "ROUSSEL": "Roussel",
    "ARTHAUD": "Arthaud",
    "JADOT": "Jadot",
    "HIDALGO": "Hidalgo",
    "DUPONT-AIGNAN": "Dupont-Aignan",
    "POUTOU": "Poutou",
}

# The surname as printed → the column slug of that candidate's first-round share.
SLUG = {
    "ARTHAUD": "arthaud", "POUTOU": "poutou", "ROUSSEL": "roussel", "MÉLENCHON": "melenchon",
    "HIDALGO": "hidalgo", "JADOT": "jadot", "MACRON": "macron", "LASSALLE": "lassalle",
    "PÉCRESSE": "pecresse", "DUPONT-AIGNAN": "dupont_aignan", "LE PEN": "lepen", "ZEMMOUR": "zemmour",
}
SHARE_COLUMNS = [f"pres22_t1_{slug}_pct" for slug in SLUG.values()]


def number(value):
    """French decimals use a comma; blanks and stray text become None."""
    if value is None:
        return None
    text = str(value).strip().replace("%", "").replace(",", ".").replace("\xa0", "").replace(" ", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def insee(row) -> str | None:
    """The INSEE code is the department code plus the three-digit commune slice.

    Reading 'Code de la commune' alone yields '001', which matches nothing.
    """
    dept = (row[DEPT] or "").strip()
    town = (row[COMMUNE3] or "").strip()
    if not dept or not town:
        return None
    return normalise_insee(f"{dept}{town.zfill(3)}")


def candidates(row, prefix: int, block: int):
    """Walk the repeating blocks; the row length decides how many there are."""
    for start in range(prefix, len(row) - block + 1, block):
        name = (row[start + NAME] or "").strip().upper()
        if not name:
            continue
        yield name, number(row[start + VOTES]), number(row[start + PCT_EXPRESSED])


def read(path, layout):
    with path.open(encoding=layout["encoding"], newline="") as fh:
        reader = csv.reader(fh, delimiter=layout["delimiter"])
        next(reader, None)
        for row in reader:
            if len(row) > layout["prefix_columns"]:
                yield row


def transform(ctx) -> None:
    layout = ctx.meta["layout"]
    prefix, block = layout["prefix_columns"], layout["block_size"]
    by_tour = {r["tour"]: ctx.raw_dir / r["filename"] for r in ctx.meta["resources"]}
    for tour, path in by_tour.items():
        if not path.exists():
            raise BuildError(f"missing {path.name}; run fetch first")

    records: dict[str, dict] = {}

    # -- first round: who came top, and everyone's share ------------------
    unknown = set()
    for row in read(by_tour[1], layout):
        code = insee(row)
        if code is None or not is_metropolitan(code):
            continue
        best = None
        rec = {"pres22_exprimes_t1": number(row[EXPRESSED])}
        for name, votes, pct in candidates(row, prefix, block):
            if name not in SLUG:
                unknown.add(name)
                continue
            rec[f"pres22_t1_{SLUG[name]}_pct"] = pct
            if votes is not None and (best is None or votes > best[0]):
                best = (votes, name)
        if best is None:
            continue
        rec["pres22_tete_t1"] = DISPLAY[best[1]]
        records[code] = rec
    if unknown:
        raise BuildError(f"first-round candidate(s) with no slug mapped: {', '.join(sorted(unknown))}")
    Log.info(f"first round: {len(records):,} metropolitan communes")

    # -- second round: the two-way split and turnout ---------------------
    missing_t2 = 0
    for row in read(by_tour[2], layout):
        code = insee(row)
        if code is None or not is_metropolitan(code):
            continue
        rec = records.setdefault(code, {})
        rec["pres22_participation_t2_pct"] = number(row[TURNOUT])
        for name, _votes, pct in candidates(row, prefix, block):
            if name == "LE PEN":
                rec["pres22_lepen_t2_pct"] = pct
            elif name == "MACRON":
                rec["pres22_macron_t2_pct"] = pct
        if "pres22_lepen_t2_pct" not in rec:
            missing_t2 += 1
    if missing_t2:
        Log.warn(f"{missing_t2:,} commune(s) had no second-round Le Pen line")

    df = pd.DataFrame(
        [{"code_insee": code, **rec} for code, rec in sorted(records.items())],
        columns=[
            "code_insee",
            "pres22_tete_t1",
            "pres22_exprimes_t1",
            *SHARE_COLUMNS,
            "pres22_lepen_t2_pct",
            "pres22_macron_t2_pct",
            "pres22_participation_t2_pct",
        ],
    )
    for col in (*SHARE_COLUMNS, "pres22_lepen_t2_pct", "pres22_macron_t2_pct", "pres22_participation_t2_pct"):
        df[col] = pd.to_numeric(df[col], errors="coerce").round(2)
    df["pres22_exprimes_t1"] = pd.to_numeric(df["pres22_exprimes_t1"], errors="coerce").astype("Int64")
    total = df[SHARE_COLUMNS].sum(axis=1)
    off = df.loc[(df["pres22_exprimes_t1"] > 0) & ((total - 100).abs() > 0.2), "code_insee"]
    if len(off):
        raise BuildError(f"{len(off):,} commune(s) whose first-round shares do not sum to 100, e.g. {off.iloc[0]}")

    lead = df["pres22_tete_t1"].value_counts()
    Log.info(f"{len(df):,} communes · first-round leader: " + ", ".join(f"{k} {v:,}" for k, v in lead.head(4).items()))
    Log.info(
        f"second round Le Pen share: median {df['pres22_lepen_t2_pct'].median():.1f}% · "
        f"{int((df['pres22_lepen_t2_pct'] > 50).sum()):,} communes above 50%"
    )
    Log.info(f"turnout median {df['pres22_participation_t2_pct'].median():.1f}%")
    df.to_csv(ctx.out_path, index=False)
