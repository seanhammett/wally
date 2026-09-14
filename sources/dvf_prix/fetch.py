"""Fetch DVF, one gzipped CSV per department per year.

The national per-year file is ~95 MB and the CDN behind it times out often; the
per-department files are small enough to retry cheaply and anything already in
data/raw is left alone.
"""
from pipeline.common import Log, download, get_json

DEPTS_URL = "https://geo.api.gouv.fr/departements?fields=code"


def metropolitan_departments(excluded: set) -> list:
    codes = sorted(d["code"] for d in get_json(DEPTS_URL) if not d["code"].startswith("97"))
    return [c for c in codes if c not in excluded]


def fetch(ctx, force: bool = False) -> None:
    cfg = ctx.meta["dvf"]
    excluded = set(ctx.meta["excluded_departments"])
    depts = metropolitan_departments(excluded)
    Log.info(f"{len(depts)} departments × {len(cfg['years'])} years "
             f"({', '.join(sorted(excluded))} have no DVF — livre foncier)")

    for year in cfg["years"]:
        year_dir = ctx.raw_dir / str(year)
        got = 0
        for dept in depts:
            dest = year_dir / f"{dept}.csv.gz"
            if dest.exists() and not force:
                got += 1
                continue
            # These are 0.2–3 MB files. A 300-second socket timeout would sit
            # on a stalled connection for five minutes before retrying; 60 fails
            # fast and download() already retries three times with a backoff.
            download(
                f"{cfg['base_url']}/{year}/departements/{dept}.csv.gz",
                dest, force=force, timeout=60,
            )
            got += 1
        Log.ok(f"{year}: {got}/{len(depts)} department files present")
