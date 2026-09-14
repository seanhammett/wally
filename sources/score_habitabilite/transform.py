"""Wally's 2050 habitability score → data/processed/score_habitabilite.csv.

Reads other sources' processed commune tables (declared as `inputs` in
source.yaml), turns each indicator into a 0–100 exposure, averages them into six
categories and the categories into a score. Every choice of indicator, scale and
direction is in source.yaml; this file only applies them.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pyogrio

from pipeline.common import BuildError, Log


def rank_exposure(values: pd.Series, worse: str = "higher") -> pd.Series:
    """Share of communes strictly less exposed, × 100. NaN stays NaN."""
    exposure = values if worse == "higher" else -values
    valid = exposure.dropna()
    if len(valid) < 2:
        raise BuildError(f"{values.name}: fewer than two values to rank")
    below = valid.rank(method="min") - 1          # communes with a strictly lower exposure
    return (below / (len(valid) - 1) * 100).reindex(values.index)


def gaspar_composites(gaspar: pd.DataFrame) -> pd.DataFrame:
    """Register counts, 0–3, from the GASPAR categorical columns (NaN where GASPAR has no row)."""
    def flag(column, yes):
        col = gaspar[column]
        return col.isin(yes).astype(float).where(col.notna())

    flood = flag("inond_azi", ["oui"]) + flag("inond_registre", ["oui"]) + \
        flag("inond_ppr", ["prescrit", "approuve", "caduque"])
    coast = flag("littoral_alea", ["submersion", "les_deux"]) + flag("littoral_alea", ["recul", "les_deux"]) + \
        flag("littoral_ppr", ["prescrit", "approuve", "caduque"])
    return pd.DataFrame({"inond_terrain": flood, "littoral_expo": coast})


def transform(ctx) -> None:
    categories = ctx.meta["categories"]
    alert_at = float(ctx.meta["alert_at"])

    names = pyogrio.read_dataframe(ctx.communes_geojson(), columns=["code_insee", "nom"], read_geometry=False)
    index = pd.Index(names["code_insee"], name="code_insee")
    nom = pd.Series(names["nom"].to_numpy(), index=index)

    tables: dict[str, pd.DataFrame] = {}

    def table(source_id: str) -> pd.DataFrame:
        if source_id not in tables:
            df = pd.read_csv(ctx.processed(source_id), dtype={"code_insee": str}, low_memory=False)
            if df["code_insee"].duplicated().any():
                raise BuildError(f"{source_id}: duplicate commune codes")
            tables[source_id] = df.set_index("code_insee").reindex(index)
        return tables[source_id]

    derived = gaspar_composites(table("georisques_gaspar"))

    exposure: dict[str, pd.Series] = {}          # indicator key → 0–100
    category_of: dict[str, str] = {}
    intensity = pd.DataFrame(index=index)
    for cat in categories:
        parts = []
        for spec in cat["indicators"]:
            if "derived" in spec:
                key, raw = spec["derived"], derived[spec["derived"]]
            else:
                key = spec["column"]
                frame = table(spec["source"])
                if key not in frame.columns:
                    raise BuildError(f"{spec['source']} has no column '{key}'")
                raw = pd.to_numeric(frame[key], errors="coerce")
            raw = raw.rename(key)
            if spec["scale"] == "rank":
                values = rank_exposure(raw, spec.get("worse", "higher"))
            elif spec["scale"] == "linear":
                top = float(spec["max"])
                if (raw.dropna() < 0).any() or (raw.dropna() > top).any():
                    raise BuildError(f"{key}: values outside 0–{top:g}")
                values = raw / top * 100
            else:
                raise BuildError(f"{key}: unknown scale '{spec['scale']}'")
            exposure[key] = values
            category_of[key] = cat["id"]
            parts.append(values)
            zero = (values == 0).sum() / values.notna().sum()
            Log.info(f"{cat['id']:<12} {key:<24} {spec['scale']:<6} covers {values.notna().mean():.1%} · "
                     f"median {values.median():.1f} · {zero:.0%} at 0")
        intensity[cat["id"]] = pd.concat(parts, axis=1).mean(axis=1)    # mean of the available ones

    required = ctx.meta["required_category"]
    scored = intensity[required].notna()
    missing = {c: int((intensity[c].isna() & scored).sum()) for c in intensity.columns}
    Log.info("communes scored without a category: " + " · ".join(f"{c} {n:,}" for c, n in missing.items() if n)
             if any(missing.values()) else "every scored commune has all six categories")
    score = (100 - intensity.mean(axis=1)).where(scored)

    labels = {cat["id"]: cat["label"] for cat in categories}
    alert = pd.DataFrame({key: values >= alert_at for key, values in exposure.items()})
    flagged = pd.DataFrame({cat: alert[[k for k, c in category_of.items() if c == cat]].any(axis=1)
                            for cat in intensity.columns})
    alerts = flagged.apply(lambda row: ", ".join(labels[cat] for cat, hit in row.items() if hit), axis=1)

    out = pd.DataFrame({"habitabilite_2050": score.round().astype("Int64")}, index=index)
    for cat in intensity.columns:
        out[f"hab_{cat}"] = intensity[cat].where(scored).round(1)
    out["hab_alertes"] = alerts.where(scored & (alerts != ""))

    s = out["habitabilite_2050"].astype(float)
    Log.info(f"{int(scored.sum()):,} communes scored · p5 {s.quantile(0.05):.0f} · p10 {s.quantile(0.1):.0f} · "
             f"median {s.median():.0f} · p90 {s.quantile(0.9):.0f} · p95 {s.quantile(0.95):.0f}")
    corr = intensity.corr().round(2)
    Log.info("category correlations: " + " · ".join(
        f"{a}/{b} {corr.loc[a, b]:+.2f}" for i, a in enumerate(corr.columns) for b in corr.columns[i + 1:]))
    ranked = pd.DataFrame({"score": s, "nom": nom}).dropna().sort_values("score")
    Log.info("least habitable: " + ", ".join(f"{r.nom} {r.score:.0f}" for r in ranked.head(8).itertuples()))
    Log.info("most habitable: " + ", ".join(f"{r.nom} {r.score:.0f}" for r in ranked.tail(8).itertuples()))

    check = ctx.meta.get("check")
    if check and check["code_insee"] in index:
        code, theirs = check["code_insee"], check["celsius"]
        ours = {key: values.get(code) for key, values in exposure.items()}
        ours.update({cat: intensity.at[code, cat] for cat in intensity.columns})
        ours["score"] = score.get(code)
        Log.info(f"{check['name']} ({code}), Wally / Celsius: " + " · ".join(
            f"{key} {ours.get(key, np.nan):.1f}/{value:g}" for key, value in theirs.items()))

    out.reset_index().sort_values("code_insee").to_csv(ctx.out_path, index=False)
