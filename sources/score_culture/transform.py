"""Reach of labelled culture per commune → data/processed/score_culture.csv.

Every venue and festival (from culture_lieux and festivals) adds its weight to
each commune within `max_km`, halving every `half_km`. The sums are published
raw and on a saturating 0–100 scale anchored per category, one per category,
with the distance to the nearest major venue beside each. Positions among
communes are only logged, for comparison.
"""
from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from pipeline.common import SOURCES_DIR, BuildError, Log, load_source

LAMBERT93 = 2154


def reach(communes: np.ndarray, venues: np.ndarray, weights: np.ndarray, half_km: float, max_km: float) -> np.ndarray:
    """Σ weight × 0.5 ** (d / half_km) over the venues within max_km of each commune."""
    if not len(venues):
        return np.zeros(len(communes))
    i, j = shapely.STRtree(venues).query(communes, predicate="dwithin", distance=max_km * 1000)
    km = shapely.distance(communes[i], venues[j]) / 1000
    return np.bincount(i, weights=weights[j] * 0.5 ** (km / half_km), minlength=len(communes))


def saturating(acces: pd.Series, anchor: float) -> pd.Series:
    """Reach on a 0–100 scale that does not depend on the other communes: 95 at the anchor."""
    return 100 * (1 - np.exp(-acces / (anchor / 3)))


def nearest_km(communes: np.ndarray, venues: np.ndarray) -> np.ndarray:
    if not len(venues):
        return np.full(len(communes), np.nan)
    (i, _), km = shapely.STRtree(venues).query_nearest(communes, return_distance=True, all_matches=False)
    out = np.full(len(communes), np.nan)
    out[i] = km / 1000
    return out


def transform(ctx) -> None:
    meta = ctx.meta
    half_km, max_km = float(meta["falloff"]["half_km"]), float(meta["falloff"]["max_km"])
    rank_exposure = load_source(SOURCES_DIR / "score_habitabilite").script("transform").rank_exposure

    communes = gpd.read_file(ctx.communes_geojson())[["code_insee", "nom", "geometry"]].to_crs(LAMBERT93)
    index = pd.Index(communes["code_insee"].to_numpy(), name="code_insee")
    nom = pd.Series(communes["nom"].to_numpy(), index=index)
    rep = communes.representative_point()
    points = shapely.points(rep.x.to_numpy(), rep.y.to_numpy())
    Log.info(f"{len(index):,} communes · falloff halves every {half_km:g} km, stops at {max_km:g} km")

    layers: dict[str, gpd.GeoDataFrame] = {}
    out = pd.DataFrame(index=index)
    scores, positions = [], []
    for cat in meta["categories"]:
        cid = cat["id"]
        if cat["source"] not in layers:
            layers[cat["source"]] = gpd.read_file(ctx.processed(cat["source"])).to_crs(LAMBERT93)
        venues = layers[cat["source"]]
        for column, value in (cat.get("filter") or {}).items():
            venues = venues[venues[column] == value]
        if venues.empty:
            raise BuildError(f"{cid}: no venue in {cat['source']} matches {cat.get('filter')}")

        geoms = shapely.points(venues.geometry.x.to_numpy(), venues.geometry.y.to_numpy())
        weights = venues["poids"].to_numpy(dtype=float)
        major = weights >= float(cat["nearest_min_poids"])

        acces = pd.Series(reach(points, geoms, weights, half_km, max_km), index=index, name=f"cult_{cid}_acces")
        anchor = float(cat["anchor"])
        score = saturating(acces, anchor)
        out[f"cult_{cid}"] = score.round(1)
        out[f"cult_{cid}_acces"] = acces.round(2)
        out[f"cult_{cid}_km"] = np.round(nearest_km(points, geoms[major]), 1)
        scores.append(score)
        positions.append(rank_exposure(acces))

        zero = (acces == 0).mean()
        q = acces.quantile([0.5, 0.9, 0.99])
        Log.info(f"{cat['label']}: {len(venues):,} venues ({int(major.sum()):,} major, weight ≥ "
                 f"{cat['nearest_min_poids']}) · reach median {q[0.5]:.2f} · p90 {q[0.9]:.2f} · p99 {q[0.99]:.1f} · "
                 f"{zero:.1%} of communes reach nothing")
        Log.info(f"  nearest major venue: median {out[f'cult_{cid}_km'].median():.0f} km · "
                 f"p90 {out[f'cult_{cid}_km'].quantile(0.9):.0f} km")
        top = acces.sort_values(ascending=False).head(12)
        Log.info("  most reach: " + " · ".join(f"{nom[c]} {v:.0f}" for c, v in top.items()))

        Log.info(f"  score, 95 at {anchor:g}: p10 {score.quantile(0.1):.0f} · median {score.median():.0f} · "
                 f"p90 {score.quantile(0.9):.0f} · {(score >= 90).mean():.1%} at 90+ · {(score < 10).mean():.1%} under 10")

    out["culture_score"] = pd.concat(scores, axis=1).mean(axis=1).round(1)
    position_score = pd.concat(positions, axis=1).mean(axis=1)
    corr = pd.concat(scores, axis=1).corr(method="spearman").round(2)
    ids = [c["id"] for c in meta["categories"]]
    Log.info("rank correlation between the three: " + " · ".join(
        f"{a}/{b} {corr.iloc[x, y]:.2f}" for x, a in enumerate(ids) for y, b in enumerate(ids) if x < y))

    for ref in meta.get("reference_communes", []):
        code = ref["code_insee"]
        if code not in out.index:
            Log.warn(f"reference commune {code} ({ref['name']}) is not in the commune set")
            continue
        row = out.loc[code]
        Log.info(f"  {ref['name']:<16} " + " · ".join(
            f"{cid} {row[f'cult_{cid}']:.0f}/{positions[k][code]:.0f} ({row[f'cult_{cid}_km']:.0f} km)"
            for k, cid in enumerate(ids))
            + f" · combined {row['culture_score']:.0f}/{position_score[code]:.0f}")
    Log.info("  (reference scores read score/position among communes)")

    out.reset_index().to_csv(ctx.out_path, index=False)
