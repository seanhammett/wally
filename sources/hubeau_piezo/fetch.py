"""Fetch Hub'Eau piezometry stations, one file per department.

Hub'Eau caps `size × page` at 20,000 results and offers no working cursor, so a
single national query cannot reach all 23,000 stations — it returns HTTP 400 on
page 5. Splitting by department stays well inside the cap, and makes a partial
failure cheap to resume: anything already in data/raw is left alone.
"""
import json
import urllib.parse

from pipeline.common import Log, get_json

DEPTS_URL = "https://geo.api.gouv.fr/departements?fields=code"


def metropolitan_departments() -> list[str]:
    return sorted(d["code"] for d in get_json(DEPTS_URL) if not d["code"].startswith("97"))


def fetch(ctx, force: bool = False) -> None:
    api = ctx.meta["api"]
    depts = metropolitan_departments()
    total = 0

    for dept in depts:
        dest = ctx.raw_dir / f"stations-{dept}.json"
        if dest.exists() and not force:
            total += len(json.loads(dest.read_text(encoding="utf-8")))
            continue

        stations: list[dict] = []
        page = 1
        while True:
            query = urllib.parse.urlencode(
                {
                    "code_departement": dept,
                    "size": api["page_size"],
                    "page": page,
                    "format": "json",
                    "srid": api["srid"],
                }
            )
            payload = get_json(f"{api['url']}?{query}")
            batch = payload.get("data", [])
            stations.extend(batch)
            if not payload.get("next") or not batch:
                break
            page += 1

        dest.write_text(json.dumps(stations, ensure_ascii=False), encoding="utf-8")
        total += len(stations)
        Log.info(f"dept {dept}: {len(stations):,} stations ({total:,} so far)")

    Log.ok(f"{total:,} stations across {len(depts)} departments")
