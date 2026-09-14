"""SAFRAN daily grid (SIM2) by year, and monthly station records by department.

The grid is what gets mapped. The stations are what it is checked and
calibrated against: measured temperatures, rain and sunshine at ~1,300
Météo-France sites, which the transform uses to correct the grid for altitude
and to report how far the result is from the thermometer.
"""
import re

from pipeline.common import BuildError, Log, download, get_json

DATASET_API = "https://www.data.gouv.fr/api/1/datasets/{slug}/"


def station_files(slug: str, first_year: int, last_year: int) -> list[tuple[str, str]]:
    """(url, filename) for each metropolitan department's monthly files covering the years.

    Météo-France renames the period files every January (1950-2024 becomes
    1950-2025), so the URLs are looked up from the dataset each time rather than
    written down.
    """
    resources = get_json(DATASET_API.format(slug=slug))["resources"]
    out = []
    for res in resources:
        m = re.match(r"MENS_departement_(\d\d)_periode_(\d{4})-(\d{4})$", res["title"])
        if not m:
            continue  # the pre-1949 files, overseas departments (3 digits), and docs
        start, end = int(m.group(2)), int(m.group(3))
        if end < first_year or start > last_year:
            continue
        out.append((res["url"], res["url"].rsplit("/", 1)[-1]))
    depts = {re.search(r"MENSQ_(\d\d)_", f).group(1) for _, f in out}
    if len(depts) < 95:
        raise BuildError(f"station files found for only {len(depts)} departments — the dataset layout has changed")
    return out


def fetch(ctx, force: bool = False) -> None:
    sim = ctx.meta["sim2"]
    for year in sim["years"]:
        # 137 MB from a fast bucket; the timeout only has to catch a stall.
        download(sim["url"].format(year=year), ctx.raw_dir / f"QUOT_SIM2_{year}.csv.gz",
                 force=force, timeout=120)

    stations = ctx.meta["stations"]
    files = station_files(stations["dataset"], min(sim["years"]), max(sim["years"]))
    for url, filename in files:
        download(url, ctx.raw_dir / "stations" / filename, force=force, timeout=120)
    Log.ok(f"{len(files)} station files present")
