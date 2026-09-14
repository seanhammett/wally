"""Download commune contours from geo.api.gouv.fr, one file per department.

Per-department requests keep each response small enough to be reliable and make
a partial failure cheap to resume — anything already in data/raw is left alone.
"""
from pipeline.common import Log, download, get_json

DEPTS_URL = "https://geo.api.gouv.fr/departements?fields=code,nom"
COMMUNES_URL = (
    "https://geo.api.gouv.fr/departements/{dept}/communes"
    "?fields=code,nom,codeDepartement,codeRegion,population,surface,contour&format=geojson&geometry=contour"
)


def metropolitan_departments() -> list[str]:
    depts = get_json(DEPTS_URL)
    # 971–976 are the DROM; they are excluded deliberately, not by accident.
    return sorted(d["code"] for d in depts if not d["code"].startswith("97"))


def fetch(ctx, force: bool = False) -> None:
    depts = metropolitan_departments()
    Log.info(f"{len(depts)} metropolitan departments")
    for i, dept in enumerate(depts, 1):
        dest = ctx.raw_dir / f"communes-{dept}.geojson"
        if dest.exists() and not force:
            continue
        download(COMMUNES_URL.format(dept=dept), dest, force=force)
        if i % 20 == 0:
            Log.info(f"  {i}/{len(depts)} departments")
    Log.ok(f"{len(list(ctx.raw_dir.glob('communes-*.geojson')))} department files in data/raw/communes")
