"""Population and density per commune → data/processed/commune_reference.csv."""
import json

import pandas as pd

from pipeline.common import Log, is_metropolitan, normalise_insee


def transform(ctx) -> None:
    records = json.loads((ctx.raw_dir / "communes.json").read_text(encoding="utf-8"))

    rows = []
    for rec in records:
        code = normalise_insee(rec.get("code"))
        if code is None or not is_metropolitan(code):
            continue
        pop = rec.get("population")
        surface_ha = rec.get("surface")
        # surface is published in hectares; 1 km² = 100 ha.
        density = round(pop / (surface_ha / 100.0), 1) if pop and surface_ha else None
        rows.append({"code_insee": code, "population": pop, "densite_hab_km2": density})

    df = pd.DataFrame(rows).dropna(subset=["population"])
    df = df.drop_duplicates(subset="code_insee").sort_values("code_insee")
    df["population"] = df["population"].astype(int)
    Log.info(
        f"{len(df):,} communes · median population {int(df['population'].median()):,} · "
        f"median density {df['densite_hab_km2'].median():.0f} hab/km²"
    )
    df.to_csv(ctx.out_path, index=False)
