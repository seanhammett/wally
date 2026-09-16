"""Wally's 2050 habitability score → data/processed/score_habitabilite.csv.

Reads other sources' processed commune tables (declared as `inputs` in
source.yaml), turns each indicator into a 0–100 exposure, averages them into six
categories and the categories into a score. Every choice of indicator, scale and
direction is in source.yaml; this file only applies them.

Every rank indicator also declares a scale of its own (`sat` or `abs`). The
published score, habitabilite_2050, scores those indicators on that scale; the
Celsius-method score, which scores them as positions among communes, is
published beside it as habitabilite_2050_pos and printed against Celsius's
worked example.
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


def scale_exposure(values: pd.Series, spec: dict, key: str) -> pd.Series:
    """An indicator on its own declared scale, 0–100.

    sat: [from, half]  0 at `from`, 50 at `from + half`, approaching 100.
    abs: [lo, hi]      straight line from lo (0) to hi (100), clamped.
    """
    worse = spec.get("worse", "higher")
    if "sat" in spec:
        start, half = (float(b) for b in spec["sat"])
        if half == 0 or (half > 0) != (worse == "higher"):
            raise BuildError(f"{key}: sat {spec['sat']} does not run the way worse: {worse} does")
        return (1 - 0.5 ** ((values - start) / half).clip(lower=0)) * 100
    lo, hi = (float(b) for b in spec["abs"])
    if lo == hi or (hi > lo) != (worse == "higher"):
        raise BuildError(f"{key}: abs {spec['abs']} does not run the way worse: {worse} does")
    return ((values - lo) / (hi - lo)).clip(0, 1) * 100


def describe_scale(spec: dict) -> str:
    if "sat" in spec:
        return f"sat    0 at {spec['sat'][0]:g}, 50 at {spec['sat'][0] + spec['sat'][1]:g}"
    return f"abs    {spec['abs'][0]:g} → {spec['abs'][1]:g}"


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
    intensity_abs = pd.DataFrame(index=index)
    rank_specs = [spec for cat in categories for spec in cat["indicators"] if spec["scale"] == "rank"]
    with_abs = all("abs" in spec or "sat" in spec for spec in rank_specs)
    for cat in categories:
        parts, parts_abs = [], []
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
                absolute = None
                if with_abs:
                    absolute = scale_exposure(raw, spec, key)
                    parts_abs.append(absolute)
            elif spec["scale"] == "linear":
                top = float(spec["max"])
                if (raw.dropna() < 0).any() or (raw.dropna() > top).any():
                    raise BuildError(f"{key}: values outside 0–{top:g}")
                values = raw / top * 100
                absolute = None
                parts_abs.append(values)
            else:
                raise BuildError(f"{key}: unknown scale '{spec['scale']}'")
            exposure[key] = values
            category_of[key] = cat["id"]
            parts.append(values)
            zero = (values == 0).sum() / values.notna().sum()
            Log.info(f"{cat['id']:<12} {key:<24} {spec['scale']:<6} covers {values.notna().mean():.1%} · "
                     f"median {values.median():.1f} · {zero:.0%} at 0")
            if absolute is not None:
                Log.info(f"{'':<12} {'':<24} {describe_scale(spec)} · "
                         f"median {absolute.median():.1f} · {(absolute == 0).mean():.0%} at 0 · "
                         f"{(absolute == 100).mean():.0%} at 100")
        intensity[cat["id"]] = pd.concat(parts, axis=1).mean(axis=1)    # mean of the available ones
        if with_abs:
            intensity_abs[cat["id"]] = pd.concat(parts_abs, axis=1).mean(axis=1)

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

    if not with_abs:
        raise BuildError("every rank indicator needs a scale of its own (sat or abs) for the published score")
    score_abs = (100 - intensity_abs.mean(axis=1)).where(scored)

    out = pd.DataFrame({"habitabilite_2050": score_abs.round().astype("Int64")}, index=index)
    for cat in intensity_abs.columns:
        out[f"hab_{cat}"] = intensity_abs[cat].where(scored).round(1)
    out["habitabilite_2050_pos"] = score.round().astype("Int64")
    for cat in intensity.columns:
        out[f"hab_{cat}_pos"] = intensity[cat].where(scored).round(1)
    out["hab_alertes"] = alerts.where(scored & (alerts != ""))

    s = out["habitabilite_2050_pos"].astype(float)
    Log.info("positional score (Celsius method):")
    Log.info(f"{int(scored.sum()):,} communes scored · p5 {s.quantile(0.05):.0f} · p10 {s.quantile(0.1):.0f} · "
             f"median {s.median():.0f} · p90 {s.quantile(0.9):.0f} · p95 {s.quantile(0.95):.0f}")
    corr = intensity.corr().round(2)
    Log.info("category correlations: " + " · ".join(
        f"{a}/{b} {corr.loc[a, b]:+.2f}" for i, a in enumerate(corr.columns) for b in corr.columns[i + 1:]))
    ranked = pd.DataFrame({"score": s, "nom": nom}).dropna().sort_values("score")
    Log.info("least habitable: " + ", ".join(f"{r.nom} {r.score:.0f}" for r in ranked.head(8).itertuples()))
    Log.info("most habitable: " + ", ".join(f"{r.nom} {r.score:.0f}" for r in ranked.tail(8).itertuples()))

    if with_abs:
        a = out["habitabilite_2050"].astype(float)
        Log.info(f"published (absolute) score: p5 {a.quantile(0.05):.0f} · median {a.median():.0f} · "
                 f"p95 {a.quantile(0.95):.0f} · min {a.min():.0f} · max {a.max():.0f} · "
                 f"rank correlation with the positional score {s.rank().corr(a.rank()):.2f}")
        diff = (a - s).dropna()
        Log.info(f"  absolute minus positional: median {diff.median():+.0f} · p5 {diff.quantile(0.05):+.0f} · "
                 f"p95 {diff.quantile(0.95):+.0f}")
        ranked_abs = pd.DataFrame({"score": a, "nom": nom}).dropna().sort_values("score")
        Log.info("  least habitable: " + ", ".join(f"{r.nom} {r.score:.0f}" for r in ranked_abs.head(8).itertuples()))
        Log.info("  most habitable: " + ", ".join(f"{r.nom} {r.score:.0f}" for r in ranked_abs.tail(8).itertuples()))

    check = ctx.meta.get("check")
    if check and check["code_insee"] in index:
        code, theirs = check["code_insee"], check["celsius"]
        ours = {key: values.get(code) for key, values in exposure.items()}
        ours.update({cat: intensity.at[code, cat] for cat in intensity.columns})
        ours["score"] = score.get(code)
        Log.info(f"{check['name']} ({code}), Wally / Celsius: " + " · ".join(
            f"{key} {ours.get(key, np.nan):.1f}/{value:g}" for key, value in theirs.items()))
        if with_abs:
            Log.info(f"{check['name']} published: score {out.at[code, 'habitabilite_2050']} "
                     f"(positional {out.at[code, 'habitabilite_2050_pos']}) · " + " · ".join(
                         f"{cat} {intensity_abs.at[code, cat]:.0f}" for cat in intensity_abs.columns))

    out.reset_index().sort_values("code_insee").to_csv(ctx.out_path, index=False)
