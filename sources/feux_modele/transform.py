"""Wildfire risk calibrated on observed fires → data/processed/feux_modele.csv.

A Poisson regression of each commune's count of ≥ 10 ha BDIFF fires on its
neighbours' fire record, fire-weather days, fire regime, strong winds and
population density, with area × years as exposure. It is fitted on the training
years, checked against each test season it never saw, refitted on the final
window, and taken to 2050 by an external fire-weather factor. Every choice is in
source.yaml; the method follows Projet Celsius's methodology note (section 3).
"""
from __future__ import annotations

import json

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from pyproj import Transformer

from pipeline.common import (SOURCES_DIR, BuildError, Log, dept_of, is_metropolitan, load_source,
                             normalise_insee, scratch_dir)
from pipeline.population import LAEA

AREA = "Surface parcourue (m2)"
CODE = "Code INSEE"
LAMBERT2E = 27572
REGIME_LABELS = {"mediterraneen": "Mediterranean", "sud_atlantique": "South-Atlantic"}
NORTH = "North"
# Rates are fires per 1,000 km² per decade (Celsius's unit, per 100 km² per
# year, × 100), so the popup's two decimals are enough.
PER_KM2_YEAR = 1000 * 10


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------
def read_fires(ctx, codes: pd.Index, first: int, last: int) -> pd.DataFrame:
    """year, code_insee, ha for every metropolitan type-F fire, on 2026 commune codes."""
    bdiff = load_source(SOURCES_DIR / "bdiff_incendies")
    read_year = bdiff.script("transform").read_year
    member = bdiff.meta["bdiff"]["member"]
    cfg = ctx.meta["merged_communes"]
    merged = {d["code"]: d["chefLieu"] for d in json.loads((ctx.raw_dir / cfg["filename"]).read_text())}

    records = []
    for year in range(first, last + 1):
        path = bdiff.raw_dir / f"incendies-{year}.zip"
        if not path.exists():
            raise BuildError(f"missing {path.name}: fetch bdiff_incendies (its archive_start must be ≤ {first})")
        for row in read_year(path, member):
            code = normalise_insee(row.get(CODE))
            if code is None or not is_metropolitan(code):
                continue
            try:
                ha = float(row.get(AREA) or 0) / 1e4
            except (TypeError, ValueError):
                ha = 0.0
            records.append((year, code, ha))
    fires = pd.DataFrame(records, columns=["year", "code_insee", "ha"])

    known = set(codes)
    old = ~fires["code_insee"].isin(known)
    fires.loc[old, "code_insee"] = fires.loc[old, "code_insee"].map(merged)
    matched = fires["code_insee"].isin(known)
    share = matched.mean()
    Log.info(f"{len(fires):,} metropolitan fires {first}–{last} · {int(old.sum()):,} under a pre-2026 code, "
             f"{int((old & matched).sum()):,} moved to the commune that absorbed it · "
             f"{int((~matched).sum()):,} dropped ({1 - share:.2%})")
    if share < float(ctx.meta["min_fire_match"]):
        raise BuildError(f"only {share:.2%} of fire records land on a 2026 commune")
    return fires[matched].reset_index(drop=True)


def wind_days(cfg: dict) -> pd.DataFrame:
    """Fire-season days a year with SIM2 daily mean wind ≥ threshold: x, y (LAEA), days."""
    meteo = load_source(SOURCES_DIR / "meteo_climat")
    sim2 = meteo.meta["sim2"]
    years, months, threshold = sim2["years"], list(cfg["months"]), float(cfg["min_ms"])
    cache = scratch_dir("feux_modele") / (
        f"vent_{threshold:g}ms_m{'-'.join(map(str, months))}_{years[0]}-{years[-1]}.csv")
    if cache.exists():
        points = pd.read_csv(cache)
    else:
        per_year = []
        for year in years:
            path = meteo.raw_dir / sim2["url"].format(year=year).rsplit("/", 1)[1]
            if not path.exists():
                raise BuildError(f"missing {path.name}: fetch meteo_climat first")
            df = pd.read_csv(path, sep=";", usecols=["LAMBX", "LAMBY", "DATE", "FF"])
            df = df[(df["DATE"] // 100 % 100).isin(months)]
            per_year.append((df["FF"] >= threshold).groupby([df["LAMBX"], df["LAMBY"]]).sum().rename(year))
        table = pd.concat(per_year, axis=1)
        if table.isna().any().any():
            raise BuildError(f"{int(table.isna().any(axis=1).sum())} SIM2 point(s) missing from some years")
        points = table.mean(axis=1).rename("days").reset_index()
        points.to_csv(cache, index=False)
    unit = float(sim2["coord_unit_m"])
    x, y = Transformer.from_crs(LAMBERT2E, LAEA, always_xy=True).transform(
        points["LAMBX"].to_numpy() * unit, points["LAMBY"].to_numpy() * unit)
    Log.info(f"wind: {len(points):,} SIM2 points · fire-season days with FF ≥ {threshold:g} m/s, "
             f"{years[0]}–{years[-1]}: median {points['days'].median():.1f} · p90 {points['days'].quantile(0.9):.1f}")
    return pd.DataFrame({"x": x, "y": y, "days": points["days"].to_numpy()})


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------
def neighbour_pairs(points: np.ndarray, radius_km: float) -> tuple[np.ndarray, np.ndarray]:
    """(i, j) for every pair of distinct communes whose points are within the radius."""
    i, j = shapely.STRtree(points).query(points, predicate="dwithin", distance=radius_km * 1000)
    keep = i != j
    return i[keep], j[keep]


def neighbour_rate(pairs, counts: np.ndarray, area_km2: np.ndarray, n_years: int) -> np.ndarray:
    """Fires per 1,000 km² per decade among each commune's neighbours, itself excluded."""
    i, j = pairs
    n = len(counts)
    fires = np.bincount(i, weights=counts[j], minlength=n)
    area = np.bincount(i, weights=area_km2[j], minlength=n)
    return np.divide(fires * PER_KM2_YEAR, area * n_years, out=np.zeros(n), where=area > 0)


def fit_poisson(X: np.ndarray, y: np.ndarray, offset: np.ndarray, ridge: float) -> np.ndarray:
    """Poisson GLM, log link, by iteratively reweighted least squares. X is standardised;
    the returned coefficients start with the intercept, which is not penalised."""
    A = np.column_stack([np.ones(len(X)), X])
    penalty = np.full(A.shape[1], ridge)
    penalty[0] = 0.0
    beta = np.zeros(A.shape[1])
    beta[0] = np.log(y.sum() / np.exp(offset).sum())
    for _ in range(100):
        # numpy 2.0 on macOS Accelerate raises spurious divide/overflow warnings
        # from matmul on finite inputs; the finiteness check stands in for them.
        with np.errstate(all="ignore"):
            eta = A @ beta + offset
            mu = np.exp(eta)
            z = eta - offset + (y - mu) / mu
            weighted = A.T * mu
            new = np.linalg.solve(weighted @ A + np.diag(penalty), weighted @ z)
        if not np.isfinite(new).all():
            raise BuildError("the Poisson fit produced a non-finite coefficient")
        if np.max(np.abs(new - beta)) < 1e-9:
            return new
        beta = new
    raise BuildError("the Poisson fit did not converge in 100 iterations")


def auc(score: np.ndarray, hit: np.ndarray) -> float:
    """Chance a hit commune outranks a commune without one (ties count half)."""
    ranks = pd.Series(score).rank().to_numpy()
    n1 = int(hit.sum())
    n0 = len(hit) - n1
    return float((ranks[hit].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


class Model:
    """Features for one window of fire years, a fit on them, and the rate it predicts."""

    def __init__(self, base: pd.DataFrame, fires: pd.DataFrame, years: list[int], pairs, ctx):
        first, last = years
        self.n_years = last - first + 1
        window = fires[fires["year"].between(first, last)]
        big = window[window["ha"] >= float(ctx.meta["min_hectares"])]
        index = base.index
        self.big = big["code_insee"].value_counts().reindex(index, fill_value=0).to_numpy(dtype=float)
        every = window["code_insee"].value_counts().reindex(index, fill_value=0).to_numpy(dtype=float)
        area = base["area_km2"].to_numpy()

        self.voisins = neighbour_rate(pairs, self.big, area, self.n_years)
        voisins_tous = neighbour_rate(pairs, every, area, self.n_years)
        density = np.log1p(base["densite"].to_numpy())
        features = pd.DataFrame({
            # Floors of about one fire in 20 years over a 20 km disc (1 per
            # 1,000 km² per decade), so a quiet neighbourhood is not an
            # infinitely negative log.
            "voisins_grands": np.log(self.voisins + 1),
            "voisins_tous": np.log(voisins_tous + 10),
            "meteo": np.log1p(base["meteo"].to_numpy()),
            "vent": np.log1p(base["vent"].to_numpy()),
            "densite": density,
            "densite2": (density - density.mean()) ** 2,
        }, index=index)
        for key in REGIME_LABELS:
            features[key] = (base["regime"] == REGIME_LABELS[key]).astype(float)
        self.names = list(features.columns)
        self.mean, self.std = features.mean(), features.std()
        X = ((features - self.mean) / self.std).to_numpy()
        offset = np.log(area * self.n_years)
        self.beta = fit_poisson(X, self.big, offset, float(ctx.meta["ridge"]))
        with np.errstate(all="ignore"):                          # see fit_poisson
            self.linear = np.column_stack([np.ones(len(X)), X]) @ self.beta
        if not np.isfinite(self.linear).all():
            raise BuildError("non-finite prediction")
        mu = np.exp(self.linear + offset)
        self.rate = np.exp(self.linear) * PER_KM2_YEAR
        self.dispersion = float(((self.big - mu) ** 2 / mu).sum() / (len(X) - len(self.beta)))
        self.years = years

    def describe(self) -> None:
        first, last = self.years
        Log.info(f"fit {first}–{last}: {int(self.big.sum()):,} fires ≥ 10 ha in "
                 f"{int((self.big > 0).sum()):,} communes · Pearson dispersion {self.dispersion:.2f}")
        Log.info("  coefficients per standard deviation: " + " · ".join(
            f"{name} {b:+.2f}" for name, b in zip(self.names, self.beta[1:])))


# --------------------------------------------------------------------------
# Transform
# --------------------------------------------------------------------------
def transform(ctx) -> None:
    meta = ctx.meta
    train, test, final = meta["train_years"], meta["test_years"], meta["final_years"]
    min_ha = float(meta["min_hectares"])

    communes = gpd.read_file(ctx.communes_geojson())[["code_insee", "nom", "geometry"]].to_crs(LAEA)
    index = pd.Index(communes["code_insee"].to_numpy(), name="code_insee")
    rep = communes.representative_point()
    points = shapely.points(rep.x.to_numpy(), rep.y.to_numpy())

    def table(source_id: str, columns: list[str]) -> pd.DataFrame:
        df = pd.read_csv(ctx.processed(source_id), dtype={"code_insee": str}).set_index("code_insee")
        missing = index.difference(df.index)
        if len(missing):
            raise BuildError(f"{source_id}: no row for {len(missing):,} commune(s), e.g. {', '.join(missing[:5])}")
        return df.reindex(index)[columns]

    drias = table("drias_chaleur", ["feux_meteo_20", "feux_meteo_2050"])
    reference = table("commune_reference", ["population", "densite_hab_km2"])

    regime = pd.Series(NORTH, index=index)
    departments = pd.Series([dept_of(c) for c in index], index=index)
    for key, depts in meta["regimes"].items():
        regime[departments.isin(depts)] = REGIME_LABELS[key]

    wind = wind_days(meta["wind"])
    tree = shapely.STRtree(shapely.points(wind["x"], wind["y"]))
    _, nearest = tree.query_nearest(points, all_matches=False)
    far = np.hypot(wind["x"].to_numpy()[nearest] - rep.x.to_numpy(), wind["y"].to_numpy()[nearest] - rep.y.to_numpy())
    Log.info(f"wind to communes: nearest SIM2 point, farthest {far.max() / 1000:.1f} km")

    base = pd.DataFrame({
        "area_km2": communes["geometry"].area.to_numpy() / 1e6,
        "meteo": drias["feux_meteo_20"].to_numpy(),
        "vent": wind["days"].to_numpy()[nearest],
        "densite": reference["densite_hab_km2"].fillna(0).to_numpy(),
        "regime": regime.to_numpy(),
    }, index=index)
    if base[["meteo", "vent"]].isna().any().any():
        raise BuildError("a commune has no fire-weather or wind value")
    Log.info("regimes: " + " · ".join(f"{k} {v:,}" for k, v in regime.value_counts().items()))

    fires = read_fires(ctx, index, min(train[0], final[0]), max(test[1], final[1]))
    big = fires[fires["ha"] >= min_ha]
    Log.info(f"fires ≥ {min_ha:g} ha: " + " · ".join(
        f"{y} {n}" for y, n in big["year"].value_counts().sort_index().items()))

    # -- every candidate radius on the first test season, for the log
    first_test = test[0]
    hit_first = big[big["year"] == first_test]["code_insee"].value_counts().reindex(index, fill_value=0) > 0
    radius = float(meta["neighbour_radius_km"])
    pairs_by_radius = {}
    for r in meta["neighbour_radius_candidates_km"]:
        pairs_by_radius[float(r)] = neighbour_pairs(points, float(r))
        m = Model(base, fires, train, pairs_by_radius[float(r)], ctx)
        Log.info(f"radius {r:g} km: {first_test} AUC {auc(m.rate, hit_first.to_numpy()):.3f}"
                 f"{'  (used)' if float(r) == radius else ''}")
    pairs = pairs_by_radius.get(radius) or neighbour_pairs(points, radius)

    # -- fit on train, test on every season it never saw
    model = Model(base, fires, train, pairs, ctx)
    model.describe()
    gain = None
    for year in range(test[0], test[1] + 1):
        hit = (big[big["year"] == year]["code_insee"].value_counts().reindex(index, fill_value=0) > 0).to_numpy()
        top = model.rate >= np.quantile(model.rate, 0.9)
        a_rate = auc(model.rate, hit)
        a_count = auc(model.rate * base["area_km2"].to_numpy(), hit)
        a_weather = auc(base["meteo"].to_numpy(), hit)
        Log.info(f"test {year}: {int(hit.sum()):,} communes hit · AUC rate {a_rate:.3f} · expected count "
                 f"{a_count:.3f} · fire weather alone {a_weather:.3f} · top decile holds {hit[top].sum() / hit.sum():.0%}")
        if year == first_test:
            gain = a_rate - a_weather
    if gain is None or gain < float(meta["min_auc_gain"]):
        raise BuildError(f"on {first_test} the model beats fire weather alone by {gain:+.3f} AUC, "
                         f"less than the required {float(meta['min_auc_gain']):.3f}")

    # -- refit on the final window and take it to 2050
    model = Model(base, fires, final, pairs, ctx)
    model.describe()
    k = float(meta["factor_2050"]["smoothing_days"])
    factor = (drias["feux_meteo_2050"].to_numpy() + k) / (drias["feux_meteo_20"].to_numpy() + k)
    rate_2050 = model.rate * factor
    Log.info(f"2050 factor: p5 {np.quantile(factor, 0.05):.2f} · median {np.median(factor):.2f} · "
             f"p95 {np.quantile(factor, 0.95):.2f}")

    out = pd.DataFrame({
        "feux_risque_2050": np.round(rate_2050, 3),
        "feux_risque_actuel": np.round(model.rate, 3),
        "feux_facteur_2050": np.round(factor, 2),
        "feux_grands_nb": model.big.astype(int),
        "feux_voisins": np.round(model.voisins, 2),
        "feux_vent_jours": np.round(base["vent"].to_numpy(), 1),
        "feux_regime": base["regime"].to_numpy(),
    }, index=index)

    # -- logged checks against Celsius
    rank_exposure = load_source(SOURCES_DIR / "score_habitabilite").script("transform").rank_exposure
    position = rank_exposure(out["feux_risque_2050"].rename("feux_risque_2050"))
    s = out["feux_risque_2050"]
    Log.info(f"rate 2050 per 1,000 km² per decade: p10 {s.quantile(0.1):.2f} · median {s.median():.2f} · "
             f"p90 {s.quantile(0.9):.2f} · max {s.max():.1f}")
    names = pd.Series(communes["nom"].to_numpy(), index=index)
    top = s.sort_values(ascending=False).head(10)
    Log.info("highest: " + ", ".join(f"{names[c]} {v:.2f}" for c, v in top.items()))
    check = meta.get("check")
    if check and check["code_insee"] in index:
        code = check["code_insee"]
        Log.info(f"{check['name']} ({code}), Wally / Celsius: rate 2050 {s[code]:.2f}/{check['celsius_rate_2050']} · "
                 f"position {position[code]:.1f}/{check['celsius_position']} · factor {factor[index.get_loc(code)]:.2f} · "
                 f"neighbours {out.at[code, 'feux_voisins']:.2f} · fires ≥ 10 ha {out.at[code, 'feux_grands_nb']}")
        pop = reference["population"].fillna(0)
        parts = []
        for dep, theirs in check["departments"].items():
            inside = departments == dep
            mean = position[inside].mean()
            weighted = (position[inside] * pop[inside]).sum() / pop[inside].sum()
            parts.append(f"{dep} {mean:.1f} (pop-weighted {weighted:.1f})/{theirs}")
        Log.info("department positions, Wally / Celsius: " + " · ".join(parts))

    out.reset_index().sort_values("code_insee").to_csv(ctx.out_path, index=False)
