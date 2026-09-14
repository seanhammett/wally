"""Ten years of BDIFF fire records → data/processed/bdiff_incendies.csv."""
import csv
import io
import zipfile
from collections import defaultdict

import pandas as pd

from pipeline.common import BuildError, Log, is_metropolitan, normalise_insee

HEADER_START = "Année;"
CODE = "Code INSEE"
AREA = "Surface parcourue (m2)"
YEAR = "Année"


def read_year(zip_path, member: str):
    """Yield rows from one export. BDIFF prefixes the CSV with a count line, a
    criteria line, and sometimes a schema-change warning, so the header is found
    by looking for it rather than by skipping a fixed number of lines."""
    with zipfile.ZipFile(zip_path) as zf, zf.open(member) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8").read()
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(HEADER_START)), None)
    if start is None:
        raise BuildError(f"{zip_path.name}: no '{HEADER_START}' header line in {member}")
    yield from csv.DictReader(lines[start:], delimiter=";")


def transform(ctx) -> None:
    cfg = ctx.meta["bdiff"]
    years = ctx.meta["years"]
    incomplete = set(ctx.meta.get("incomplete_departments", []))

    count = defaultdict(int)
    hectares = defaultdict(float)
    largest = defaultdict(float)
    latest = {}
    skipped_overseas = unparsed = 0

    for year in range(int(years["start"]), int(years["end"]) + 1):
        path = ctx.raw_dir / f"incendies-{year}.zip"
        if not path.exists():
            raise BuildError(f"missing {path.name}; run fetch first")
        n = 0
        burned = 0.0
        for row in read_year(path, cfg["member"]):
            code = normalise_insee(row.get(CODE))
            if code is None:
                unparsed += 1
                continue
            if not is_metropolitan(code):
                skipped_overseas += 1
                continue
            try:
                ha = float(row.get(AREA) or 0) / 1e4
            except (TypeError, ValueError):
                ha = 0.0
            count[code] += 1
            hectares[code] += ha
            largest[code] = max(largest[code], ha)
            row_year = (row.get(YEAR) or "").strip()
            if row_year.isdigit():
                latest[code] = max(latest.get(code, 0), int(row_year))
            n += 1
            burned += ha
        Log.info(f"{year}: {n:,} forest fires · {burned:,.0f} ha")

    if skipped_overseas:
        Log.info(f"{skipped_overseas:,} fire(s) in the overseas departments, dropped")
    if unparsed:
        Log.warn(f"{unparsed:,} record(s) had no usable INSEE code")

    rows = [
        {
            "code_insee": code,
            "incendies_nb": count[code],
            "incendies_ha": round(hectares[code], 2),
            "incendie_max_ha": round(largest[code], 2),
            "incendie_annee_recente": latest.get(code, ""),
        }
        for code in sorted(count)
    ]
    df = pd.DataFrame(rows)
    Log.info(
        f"{len(df):,} communes with at least one recorded forest fire · "
        f"{int(df['incendies_nb'].sum()):,} fires · {df['incendies_ha'].sum():,.0f} ha total"
    )
    Log.info(f"largest single fire: {df['incendie_max_ha'].max():,.0f} ha")
    if incomplete:
        Log.warn(
            f"departments {', '.join(sorted(incomplete))} report an unknown fire count for part of "
            "the window; their communes are blank rather than zero"
        )
    df.to_csv(ctx.out_path, index=False)
