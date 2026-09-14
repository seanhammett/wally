"""Read RARE's commune-level Explore2 TRACC files.

Every indicator RARE publishes (summer low flow, low-flow duration, annual flood
peak, …) comes as the same long-format CSV: one row per commune × warming level
× ensemble statistic, 130–160 MB each. This streams one and keeps only what is
asked for.

    values, origin = read_commune_indicator(path, levels={"+2.7°C France"}, stats={"q50"})
    values["40192"][("+2.7°C France", "q50")]    # → 22.0
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from pipeline.common import BuildError, Log, is_metropolitan, normalise_insee

# RARE's quality flag, verbatim from the file, mapped to something short enough
# to put in a popup. It says where the simulation points behind a commune's
# value actually are.
ORIGIN = {
    "Oui (bassin versant du territoire)": "propre",
    "Non (bassin versant proche)": "proche",
    "Non (bassin versant éloigné)": "eloigne",
}

REQUIRED = {"code_territoire", "rechauffement_france", "statistique", "resultat", "code_indicateur"}


def read_commune_indicator(path: Path, levels: set[str], stats: set[str],
                           indicator: str | None = None) -> tuple[dict[str, dict], dict[str, str]]:
    """(code → {(level, statistic): value}, code → origin flag) for metropolitan communes.

    `indicator`, when given, must match the file's code_indicateur on every row
    kept, so a resource swapped on data.gouv cannot be read as the wrong thing.
    """
    values: dict[str, dict] = defaultdict(dict)
    origin: dict[str, str] = {}
    rows_read = 0
    # utf-8-sig: the files carry a BOM, which would otherwise become part of
    # the first column name and silently break the DictReader lookup.
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = REQUIRED - set(reader.fieldnames or [])
        if missing:
            raise BuildError(f"{path.name} is missing expected column(s): {', '.join(sorted(missing))} — "
                             "the RARE export layout has changed")
        for row in reader:
            if row["rechauffement_france"] not in levels or row["statistique"] not in stats:
                continue
            if indicator is not None and row["code_indicateur"] != indicator:
                raise BuildError(f"{path.name} holds {row['code_indicateur']!r}, expected {indicator!r}")
            code = normalise_insee(row["code_territoire"])
            if code is None or not is_metropolitan(code):
                continue
            try:
                values[code][(row["rechauffement_france"], row["statistique"])] = float(row["resultat"])
            except (TypeError, ValueError):
                continue
            origin.setdefault(code, ORIGIN.get(row.get("donnees_issues_territoire", ""), ""))
            rows_read += 1

    Log.info(f"{path.name}: {rows_read:,} rows kept for {len(values):,} communes")
    if not values:
        raise BuildError(f"{path.name} produced no usable rows — check the warming/statistic labels")
    return values, origin


def opt(value):
    """Keep blanks blank rather than writing a misleading zero."""
    return "" if value is None else round(value, 1)
