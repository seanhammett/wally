"""Food retail per commune → data/processed/bpe_alimentation.csv.

The BPE ships one row per (geography, facility type) *that exists*. Communes with
no food shop are absent rather than zero, so the whole point of this transform is
to start from the commune list and fill the gaps explicitly.
"""
import csv
import io
import json
import zipfile

import pandas as pd

from pipeline.common import Log, is_metropolitan, normalise_insee

# Column positions in DS_BPE_*_data.csv, which is a tidy long file:
# GEO;GEO_OBJECT;FACILITY_DOM;FACILITY_SDOM;FACILITY_TYPE;BPE_MEASURE;
# UNIT_MEASURE;OBS_STATUS;UNIT_MULT;TIME_PERIOD;OBS_VALUE
GEO, GEO_OBJECT, FACILITY_TYPE, OBS_VALUE = 0, 1, 4, 10
COMMUNE_LEVEL = "COM"


def classify(large: int, general: int, specialist: int) -> str:
    """Four classes, ordered by what you can actually buy without leaving town."""
    if large:
        return "supermarche"
    if general:
        return "proximite"
    if specialist:
        return "specialise"
    return "aucun"


def transform(ctx) -> None:
    res = ctx.meta["resource"]
    zip_path = ctx.raw_dir / res["filename"]
    general = dict(ctx.meta["types"]["general"])
    specialist = dict(ctx.meta["types"]["specialist"])
    large = set(ctx.meta["large"])
    wanted = set(general) | set(specialist)

    # Start from the commune geometry so that "no row in the BPE" becomes an
    # explicit zero rather than a hole in the map.
    communes = json.loads(ctx.communes_geojson().read_text(encoding="utf-8"))
    codes = sorted(
        {
            f["properties"]["code_insee"]
            for f in communes["features"]
            if (f.get("properties") or {}).get("code_insee")
        }
    )
    counts = {code: dict.fromkeys(wanted, 0) for code in codes}
    Log.info(f"{len(codes):,} commune geometries to fill")

    seen_rows = matched_rows = 0
    with zipfile.ZipFile(zip_path) as zf, zf.open(res["member"]) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8"), delimiter=";")
        next(reader, None)  # header
        for row in reader:
            if len(row) <= OBS_VALUE or row[GEO_OBJECT] != COMMUNE_LEVEL:
                continue
            ftype = row[FACILITY_TYPE]
            if ftype not in wanted:
                continue
            seen_rows += 1
            code = normalise_insee(row[GEO])
            if code is None or not is_metropolitan(code):
                continue
            bucket = counts.get(code)
            if bucket is None:
                continue  # a commune the 2026 geometry no longer has
            try:
                bucket[ftype] += int(row[OBS_VALUE])
            except (TypeError, ValueError):
                continue
            matched_rows += 1

    Log.info(f"{seen_rows:,} commune-level food rows read · {matched_rows:,} landed on a 2026 commune")

    rows = []
    for code in codes:
        b = counts[code]
        n_large = sum(b[t] for t in large)
        n_general = sum(b[t] for t in general)
        n_specialist = sum(b[t] for t in specialist)
        rows.append(
            {
                "code_insee": code,
                "acces_alimentaire": classify(n_large, n_general, n_specialist),
                "commerces_alimentaires": n_general + n_specialist,
                "epiceries_supermarches": n_general,
                "boulangeries": b.get("B207", 0),
            }
        )

    df = pd.DataFrame(rows).sort_values("code_insee")
    share = df["acces_alimentaire"].value_counts()
    for name in ("aucun", "specialise", "proximite", "supermarche"):
        n = int(share.get(name, 0))
        Log.info(f"  {name:<12} {n:>6,} communes ({n / len(df):.1%})")
    Log.info(f"{int(df['commerces_alimentaires'].sum()):,} food shops in total")
    df.to_csv(ctx.out_path, index=False)
