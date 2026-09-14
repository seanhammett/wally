"""Municipal 2026 commune results → data/processed/municipales_2026.csv."""
import csv

import pandas as pd

from pipeline.common import BuildError, Log, is_metropolitan, normalise_insee

# Fixed-prefix positions.
CODE, TURNOUT = 2, 6
# Offsets inside each repeating list block.
PANEL, NUANCE, LIST_LABEL, VOTES = 0, 4, 6, 7


def number(value):
    """'55,08%' → 55.08. Blank cells and dashes become None."""
    if value is None:
        return None
    text = str(value).strip().replace("%", "").replace(",", ".").replace("\xa0", "").replace(" ", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def lists_on_ballot(row, prefix: int, block: int):
    """Yield (nuance, label, votes) for each list that actually stood."""
    for start in range(prefix, len(row) - block + 1, block):
        if not (row[start + PANEL] or "").strip():
            continue
        yield (
            (row[start + NUANCE] or "").strip(),
            (row[start + LIST_LABEL] or "").strip(),
            number(row[start + VOTES]),
        )


def read(path, layout):
    with path.open(encoding=layout["encoding"], newline="") as fh:
        reader = csv.reader(fh, delimiter=layout["delimiter"])
        next(reader, None)
        for row in reader:
            if len(row) > layout["prefix_columns"]:
                yield row


def winner(row, prefix, block):
    """The list with the most votes, and how many lists stood at all."""
    standing = list(lists_on_ballot(row, prefix, block))
    best = None
    for nuance, label, votes in standing:
        if votes is not None and (best is None or votes > best[0]):
            best = (votes, nuance, label)
    return best, len(standing)


def transform(ctx) -> None:
    layout = ctx.meta["layout"]
    prefix, block = layout["prefix_columns"], layout["block_size"]
    nuances = ctx.meta["nuances"]
    by_tour = {r["tour"]: ctx.raw_dir / r["filename"] for r in ctx.meta["resources"]}
    for tour, path in by_tour.items():
        if not path.exists():
            raise BuildError(f"missing {path.name}; run fetch first")

    records: dict[str, dict] = {}
    unknown: set[str] = set()

    # -- round one: every commune, same day, so turnout is comparable ----
    for row in read(by_tour[1], layout):
        code = normalise_insee(row[CODE])
        if code is None or not is_metropolitan(code):
            continue
        best, standing = winner(row, prefix, block)
        rec = {
            "muni26_participation_t1_pct": number(row[TURNOUT]),
            "muni26_nb_listes": standing,
            "muni26_second_tour": "non",
            "muni26_nuance_gagnante": "",
            "muni26_liste_gagnante": "",
        }
        if best is not None:
            _votes, nuance, label = best
            rec["muni26_liste_gagnante"] = label
            if nuance:
                if nuance not in nuances:
                    unknown.add(nuance)
                rec["muni26_nuance_gagnante"] = nuances.get(nuance, nuance)
        records[code] = rec

    Log.info(f"first round: {len(records):,} metropolitan communes")

    # -- round two: only where the council was not settled on 15 March ---
    second = 0
    for row in read(by_tour[2], layout):
        code = normalise_insee(row[CODE])
        if code is None or not is_metropolitan(code):
            continue
        rec = records.setdefault(code, {"muni26_nb_listes": 0, "muni26_participation_t1_pct": None})
        rec["muni26_second_tour"] = "oui"
        second += 1
        # The runoff decides who governs, so its winner overrides the first round.
        best, _standing = winner(row, prefix, block)
        if best is not None:
            _votes, nuance, label = best
            rec["muni26_liste_gagnante"] = label
            if nuance:
                if nuance not in nuances:
                    unknown.add(nuance)
                rec["muni26_nuance_gagnante"] = nuances.get(nuance, nuance)

    if unknown:
        Log.warn(f"nuance code(s) not in source.yaml, passed through as-is: {', '.join(sorted(unknown))}")
    Log.info(f"second round: {second:,} communes")

    df = pd.DataFrame(
        [{"code_insee": code, **rec} for code, rec in sorted(records.items())],
        columns=[
            "code_insee",
            "muni26_participation_t1_pct",
            "muni26_nb_listes",
            "muni26_second_tour",
            "muni26_nuance_gagnante",
            "muni26_liste_gagnante",
        ],
    )
    df["muni26_participation_t1_pct"] = pd.to_numeric(df["muni26_participation_t1_pct"], errors="coerce").round(2)
    df["muni26_nb_listes"] = pd.to_numeric(df["muni26_nb_listes"], errors="coerce").fillna(0).astype(int)

    single = int((df["muni26_nb_listes"] <= 1).sum())
    with_nuance = int((df["muni26_nuance_gagnante"].astype(str).str.len() > 0).sum())
    Log.info(f"turnout median {df['muni26_participation_t1_pct'].median():.1f}%")
    Log.info(f"{single:,} communes ({single / len(df):.1%}) had one list or none on the ballot")
    Log.info(f"{with_nuance:,} communes ({with_nuance / len(df):.1%}) have a nuance for the winning list")
    df.to_csv(ctx.out_path, index=False)
