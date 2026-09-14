"""Observed climate per commune, 2016–2025 → data/processed/meteo_climat.csv.

SAFRAN is Météo-France's daily reanalysis of surface weather on an 8 km grid:
station observations analysed onto the grid over climatically homogeneous zones
and altitude bands. It is continuous from 1958 to last week, and it is what the
French climate services themselves use for "what the weather was".

Three things are done to it here, in order:

1. **Summarise.** Each year's daily file becomes one row per grid point —
   rainfall, rainy days, solar energy, mean / summer-high / winter-low
   temperature — plus tables of hot-day and frost-day counts at a range of
   temperature shifts (step 3 needs them). Ten years are averaged.

2. **Calibrate against stations.** Each grid cell's temperature is the
   temperature at the cell's *mean* altitude. In the mountains that is hundreds
   of metres above the valley floor where the town is, and at a station
   300 m or more away from its cell's mean altitude the raw error is over 3 °C.
   SAFRAN's summer highs also run ~2 °C cool everywhere, because they are the
   hottest hourly value rather than the instantaneous peak a thermometer logs.
   So for each temperature, station minus grid is fitted as a + b × (altitude
   difference) over ~650 Météo-France stations with complete records, and the
   fit is scored by 10-fold cross-validation by station. The build prints that
   score and fails if it degrades past `max_cv_rmse`.

3. **Move to where people live.** Every populated INSEE 200 m cell gets its
   grid cell's values, corrected from the cell's mean altitude to its own, and
   each commune is the population-weighted mean of its cells. Hot days and
   frost days are counted with the same correction applied to each day's
   temperature before the threshold, not by shifting the count afterwards.

Rainfall and solar energy are not altitude-corrected: neither has a rule as
simple as a lapse rate, and at stations both are already within ~8%.
"""
from __future__ import annotations

import re

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from pyproj import Transformer

from pipeline.common import BuildError, Log
from pipeline.elevation import altitude
from pipeline.population import LAEA, assign_communes, read_population, weighted_by_commune

LAMBERT2E = 27572
WGS84 = 4326
COLUMNS = ["LAMBX", "LAMBY", "DATE", "PRENEI", "PRELIQ", "T", "SSI", "HU", "TINF_H", "TSUP_H"]
J_CM2_PER_KWH_M2 = 360.0      # SSI is J/cm² per day
GRID_M = 8000
OFFSET_HM = (40, 10)          # lattice origin: LAMBX = 40 + 80k, LAMBY = 10 + 80k (hectometres)
SHIFTS = np.arange(-12.0, 12.001, 0.25)   # °C added to daily TX / TN before counting

SUMMER = (7, 8)
WINTER = (12, 1, 2)
DARK = (11, 12, 1, 2)

# station column → grid column, for the three temperatures that get a fitted correction
TEMPERATURES = {"temp_moy": ("TM", "tmean"), "tx_ete": ("TX", "tx_summer"), "tn_hiver": ("TN", "tn_winter")}

# The climate table: per-month grid indicators, and for each one that gets a
# monthly altitude correction, the station column it is fitted on and its unit.
# Humidity is on the list because valley floors hold damp, cold air in winter
# that the cell's mean altitude does not see (Chamonix: ~90% in December).
MONTHLY = ["tx", "tm", "tn", "tx_max", "tn_min", "precip", "rainy", "hu", "ssi"]
MONTHLY_CORRECTED = {"tx": ("TX", " °C"), "tm": ("TM", " °C"), "tn": ("TN", " °C"),
                     "tx_max": ("TXAB", " °C"), "tn_min": ("TNAB", " °C"), "hu": ("UMM", " points")}


# ----------------------------------------------------------------- grid
def year_summary(path, rainy_mm: float, hot_c: float, snow_day: dict) -> tuple[pd.DataFrame, np.ndarray, np.ndarray, dict]:
    """One year of SAFRAN: per-point indicators, and hot/frost counts at each of SHIFTS.

    Returns (summary indexed by LAMBX/LAMBY, hot[points × shifts], frost[points × shifts],
    monthly — see month_summary), all in the same point order.
    """
    df = pd.read_csv(path, sep=";", usecols=COLUMNS, dtype={"DATE": str})
    days = df["DATE"].str[:8]
    years = days.str[:4].unique()
    if len(years) != 1:
        raise BuildError(f"{path.name} spans more than one year: {sorted(years)[:3]}")
    expected = 366 if int(years[0]) % 4 == 0 else 365
    if days.nunique() != expected:
        raise BuildError(f"{path.name}: {days.nunique()} days, expected {expected} — incomplete year")

    df = df.assign(DATE=days).sort_values(["LAMBX", "LAMBY", "DATE"], kind="mergesort")
    month = df["DATE"].str[4:6].astype(int)
    precip = df["PRELIQ"] + df["PRENEI"]
    df = df.assign(
        precip=precip,
        rainy=(precip >= rainy_mm).astype(int),
        ssi_dark=df["SSI"].where(month.isin(DARK), 0.0),
        tx_summer=df["TSUP_H"].where(month.isin(SUMMER)),
        tn_winter=df["TINF_H"].where(month.isin(WINTER)),
    )
    g = df.groupby(["LAMBX", "LAMBY"], sort=True)
    summary = pd.DataFrame({
        "precip": g["precip"].sum(),
        "rainy": g["rainy"].sum(),
        "ssi": g["SSI"].sum() / J_CM2_PER_KWH_M2,
        "ssi_dark": g["ssi_dark"].sum() / J_CM2_PER_KWH_M2,
        "tmean": g["T"].mean(),
        "tx_summer": g["tx_summer"].mean(),
        "tn_winter": g["tn_winter"].mean(),
    })

    n_points = len(summary)
    if len(df) != n_points * expected:
        raise BuildError(f"{path.name}: {len(df):,} rows for {n_points:,} points × {expected} days — ragged grid")
    tx = df["TSUP_H"].to_numpy().reshape(n_points, expected)
    tn = df["TINF_H"].to_numpy().reshape(n_points, expected)
    hot = np.stack([(tx + s >= hot_c).sum(axis=1) for s in SHIFTS], axis=1)
    frost = np.stack([(tn + s < 0).sum(axis=1) for s in SHIFTS], axis=1)

    def daily(col):
        return df[col].to_numpy().reshape(n_points, expected)

    month_of_day = month.to_numpy()[:expected] - 1   # rows are sorted by point, then date
    monthly = month_summary(month_of_day, tx, tn, daily("T"), daily("precip"), daily("HU"), daily("SSI"),
                            rainy_mm, snow_day)
    return summary, hot, frost, monthly


def month_summary(month: np.ndarray, tx, tn, t, precip, hu, ssi, rainy_mm: float, snow_day: dict) -> dict:
    """Per-point monthly indicators for one year, for the climate table.

    Arrays are points × days; `month` is 0–11 per day. Returns points × 12
    arrays, the year's own extremes (points), and `snow`: points × 12 ×
    SHIFTS, wet days whose low reaches the snow threshold after adding each
    shift, so the count can be moved to another altitude the way hot days are.
    """
    n = tx.shape[0]
    out = {key: np.empty((n, 12)) for key in MONTHLY}
    for m in range(12):
        sel = month == m
        out["tx"][:, m] = tx[:, sel].mean(axis=1)
        out["tm"][:, m] = t[:, sel].mean(axis=1)
        out["tn"][:, m] = tn[:, sel].mean(axis=1)
        out["tx_max"][:, m] = tx[:, sel].max(axis=1)
        out["tn_min"][:, m] = tn[:, sel].min(axis=1)
        out["precip"][:, m] = precip[:, sel].sum(axis=1)
        out["rainy"][:, m] = (precip[:, sel] >= rainy_mm).sum(axis=1)
        out["hu"][:, m] = hu[:, sel].mean(axis=1)
        out["ssi"][:, m] = ssi[:, sel].sum(axis=1) / J_CM2_PER_KWH_M2
    out["tx_max_year"] = tx.max(axis=1)
    out["tn_min_year"] = tn.min(axis=1)

    # tn + s <= low  ⇔  tn - low <= -s, and SHIFTS is symmetric, so one
    # cumulative histogram of (tn - low) over wet days gives every shift at once.
    wet = precip >= float(snow_day["precip_mm"])
    bins = np.searchsorted(SHIFTS, tn - float(snow_day["low_c"]), side="left")   # value <= SHIFTS[bin]
    width = len(SHIFTS) + 1
    flat = ((np.arange(n)[:, None] * 12 + month[None, :]) * width + bins)[wet]
    hist = np.bincount(flat, minlength=n * 12 * width).reshape(n, 12, width)
    at_or_below = np.cumsum(hist, axis=2)[:, :, :len(SHIFTS)]
    out["snow"] = at_or_below[:, :, ::-1]
    return out


def lattice_key(x_m: np.ndarray, y_m: np.ndarray) -> pd.MultiIndex:
    """Snap Lambert II étendu metres to the nearest SAFRAN grid point (hectometre keys)."""
    step = GRID_M / 100
    gx = OFFSET_HM[0] + np.rint((np.asarray(x_m) / 100 - OFFSET_HM[0]) / step) * step
    gy = OFFSET_HM[1] + np.rint((np.asarray(y_m) / 100 - OFFSET_HM[1]) / step) * step
    return pd.MultiIndex.from_arrays([gx.astype(int), gy.astype(int)], names=["LAMBX", "LAMBY"])


def cell_sample_points(points: pd.MultiIndex, per_side: int = 16) -> tuple[np.ndarray, np.ndarray]:
    """Lambert II étendu x, y of a per_side × per_side lattice across every 8 km cell.

    Averaging terrain over these gives the cell's mean altitude — the altitude
    SAFRAN's values belong to.
    """
    offsets = (np.arange(per_side) + 0.5) / per_side * GRID_M - GRID_M / 2
    ox, oy = np.meshgrid(offsets, offsets)
    cx = points.get_level_values(0).to_numpy() * 100.0
    cy = points.get_level_values(1).to_numpy() * 100.0
    xs = (cx[:, None] + ox.ravel()[None, :]).ravel()
    ys = (cy[:, None] + oy.ravel()[None, :]).ravel()
    return xs, ys


def interpolate_counts(table: np.ndarray, rows: np.ndarray, shift: np.ndarray) -> np.ndarray:
    """Count at an arbitrary shift, linear between the tabulated SHIFTS."""
    step = SHIFTS[1] - SHIFTS[0]
    pos = np.clip((shift - SHIFTS[0]) / step, 0, len(SHIFTS) - 1)
    lo = np.minimum(np.floor(pos).astype(int), len(SHIFTS) - 2)
    frac = pos - lo
    return table[rows, lo] * (1 - frac) + table[rows, lo + 1] * frac


# ------------------------------------------------------------- stations
def read_stations(folder, years: list[int]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Per station-year observed indicators (complete years only), station metadata, and the monthly rows."""
    wanted = ["NUM_POSTE", "LAT", "LON", "ALTI", "AAAAMM", "RR", "NBJRR1", "TM", "TX", "TN",
              "NBJGELEE", "NBJTX30", "GLOT", "TXAB", "TNAB", "UMM", "NBJNEIG"]
    frames = []
    files = sorted(folder.glob("MENSQ_*.csv.gz"))
    if not files:
        raise BuildError(f"no station files in {folder} — run the fetch stage first")
    for path in files:
        if not re.match(r"MENSQ_\d\d_", path.name):
            continue
        df = pd.read_csv(path, sep=";", usecols=lambda c: c in wanted, dtype={"NUM_POSTE": str}, low_memory=False)
        frames.append(df[(df["AAAAMM"] // 100).isin(years)])
    m = pd.concat(frames).drop_duplicates(["NUM_POSTE", "AAAAMM"], keep="last")
    m["year"] = m["AAAAMM"] // 100
    m["month"] = m["AAAAMM"] % 100

    def complete(col, months):
        sub = m[m["month"].isin(months)]
        n = sub.groupby(["NUM_POSTE", "year"])[col].count()
        return n == len(months), sub.groupby(["NUM_POSTE", "year"])[col]

    out = {}
    for name, col, months, how in [
        ("precip", "RR", range(1, 13), "sum"), ("rainy", "NBJRR1", range(1, 13), "sum"),
        ("TM", "TM", range(1, 13), "mean"), ("TX", "TX", SUMMER, "mean"), ("TN", "TN", WINTER, "mean"),
        ("hot", "NBJTX30", range(1, 13), "sum"), ("frost", "NBJGELEE", range(1, 13), "sum"),
        ("ssi", "GLOT", range(1, 13), "sum"),
    ]:
        ok, grouped = complete(col, list(months))
        values = getattr(grouped, how)()
        out[name] = values.where(ok)
    annual = pd.DataFrame(out)
    annual["ssi"] = annual["ssi"] / J_CM2_PER_KWH_M2
    meta = m.groupby("NUM_POSTE")[["LAT", "LON", "ALTI"]].first()
    return annual.reset_index(), meta, m


def fit_correction(err: pd.Series, dz_km: pd.Series, folds: int = 10) -> tuple[float, float, np.ndarray]:
    """OLS err = a + b·dz, plus out-of-fold predictions (folds by station, fixed seed)."""
    rng = np.random.default_rng(0)
    fold = rng.permutation(len(err)) % folds
    pred = np.empty(len(err))
    X = np.c_[np.ones(len(err)), dz_km.to_numpy()]
    y = err.to_numpy()
    for k in range(folds):
        train, test = fold != k, fold == k
        coef = np.linalg.lstsq(X[train], y[train], rcond=None)[0]
        pred[test] = X[test] @ coef
    a, b = np.linalg.lstsq(X, y, rcond=None)[0]
    return float(a), float(b), y - pred


# --------------------------------------------------------- climate table
# One row of the table per entry, in display order. `year` is how the Year
# column is formed from the months, except for "fit", which has its own
# station-calibrated annual value (the mean of each year's single highest
# day is not an average of the monthly ones).
TABLE_ROWS = [
    {"id": "record_high", "label": "Highest", "unit": "°C", "decimals": 1, "scale": "temp", "year": "max"},
    {"id": "mean_max", "label": "Mean maximum", "unit": "°C", "decimals": 1, "scale": "temp", "year": "fit"},
    {"id": "tx", "label": "Mean daily maximum", "unit": "°C", "decimals": 1, "scale": "temp", "year": "mean"},
    {"id": "tm", "label": "Daily mean", "unit": "°C", "decimals": 1, "scale": "temp", "year": "mean"},
    {"id": "tn", "label": "Mean daily minimum", "unit": "°C", "decimals": 1, "scale": "temp", "year": "mean"},
    {"id": "mean_min", "label": "Mean minimum", "unit": "°C", "decimals": 1, "scale": "temp", "year": "fit"},
    {"id": "record_low", "label": "Lowest", "unit": "°C", "decimals": 1, "scale": "temp", "year": "min"},
    {"id": "precip", "label": "Precipitation", "unit": "mm", "decimals": 1, "scale": "precip", "year": "sum"},
    {"id": "rainy", "label": "Precipitation days (≥ 1 mm)", "unit": "days", "decimals": 1, "scale": "days", "year": "sum"},
    {"id": "snow", "label": "Snowy days", "unit": "days", "decimals": 1, "scale": "snow", "year": "sum"},
    {"id": "hu", "label": "Relative humidity", "unit": "%", "decimals": 0, "scale": "humidity", "year": "mean"},
    {"id": "solar", "label": "Solar energy", "unit": "kWh/m²", "decimals": 0, "scale": "solar", "year": "sum"},
]


def calibrate_table(obs: pd.DataFrame, stations: pd.DataFrame, row_of: pd.Series, months: dict,
                    snow: np.ndarray, years: list[int], min_years: int, limits: dict) -> tuple[dict, dict]:
    """Per-month corrections for the climate table, and a station check of every row.

    `obs` is the monthly station rows; `stations` has LAMBX/LAMBY and dz_km;
    `months[key]` is years × points × 12. Returns (coef, checks). coef[key] is
    (a, b) per month for each temperature — with a 13th row, for the extremes,
    fitted on each year's single highest or lowest day — and coef["rainy"] the
    per-month scale factor. checks[row id] is the one-line accuracy note the
    page shows.
    """
    st = pd.DataFrame({
        "row": row_of.reindex(pd.MultiIndex.from_frame(stations[["LAMBX", "LAMBY"]])).to_numpy().astype(int),
        "dz_km": stations["dz_km"].to_numpy(),
    }, index=stations.index)
    year_idx = {y: i for i, y in enumerate(years)}
    o = obs[obs["NUM_POSTE"].isin(st.index) & obs["year"].isin(years)].copy()
    o_row = st["row"].reindex(o["NUM_POSTE"]).to_numpy()
    o_year = o["year"].map(year_idx).to_numpy()
    for key in MONTHLY:
        o["grid_" + key] = months[key][o_year, o_row, o["month"].to_numpy() - 1]

    def per_station_month(col: str, grid_col: str) -> pd.DataFrame:
        sub = o.dropna(subset=[col])
        g = sub.groupby(["NUM_POSTE", "month"]).agg(obs=(col, "mean"), grid=(grid_col, "mean"), n=(col, "size"))
        return g[g["n"] >= min_years].reset_index().join(st["dz_km"], on="NUM_POSTE")

    coef, checks = {}, {}
    for key, (col, unit) in MONTHLY_CORRECTED.items():
        per = per_station_month(col, "grid_" + key)
        extreme = key in ("tx_max", "tn_min")
        ab = np.zeros((13 if extreme else 12, 2))
        rmse, rmse_mountain = np.zeros(12), np.zeros(12)
        for month, p in per.groupby("month"):
            a, b, cv = fit_correction(p["obs"] - p["grid"], p["dz_km"])
            ab[month - 1] = (a, b)
            mountain = (p["dz_km"].abs() > 0.3).to_numpy()
            rmse[month - 1] = np.sqrt((cv ** 2).mean())
            rmse_mountain[month - 1] = np.sqrt((cv[mountain] ** 2).mean())
        if extreme:
            how = "max" if key == "tx_max" else "min"
            yearly = o.dropna(subset=[col]).groupby(["NUM_POSTE", "year"])[col].agg([how, "size"])
            yearly = yearly[yearly["size"] == 12]
            posts, yrs = yearly.index.get_level_values(0), yearly.index.get_level_values(1)
            grid_year = months[f"{key}_year"][yrs.map(year_idx).to_numpy(), st["row"].reindex(posts).to_numpy()]
            per_year = (pd.DataFrame({"obs": yearly[how].to_numpy(), "grid": grid_year, "post": posts})
                          .groupby("post").agg(obs=("obs", "mean"), grid=("grid", "mean"), n=("obs", "size")))
            per_year = per_year[per_year["n"] >= min_years].join(st["dz_km"])
            a, b, cv = fit_correction(per_year["obs"] - per_year["grid"], per_year["dz_km"])
            ab[12] = (a, b)
            Log.info(f"table {key} (year): {len(per_year)} stations · error {np.sqrt((cv ** 2).mean()):.2f}{unit} cross-validated")
        coef[key] = ab
        Log.info(f"table {key}: {per['NUM_POSTE'].nunique()} stations · monthly error {rmse.min():.2f}–{rmse.max():.2f}{unit} "
                 f"cross-validated ({rmse_mountain.min():.1f}–{rmse_mountain.max():.1f} in the mountains)")
        if rmse.max() > float(limits[key]):
            raise BuildError(f"table {key}: cross-validated error {rmse.max():.2f}{unit} in the worst month "
                             f"exceeds max_cv_rmse_monthly {limits[key]}")
        checks[key] = (f"Checked against {per['NUM_POSTE'].nunique():,} stations: typical error "
                       f"{np.median(rmse):.1f}{unit} ({np.median(rmse_mountain):.1f}{unit} in the mountains).")
    checks["mean_max"], checks["mean_min"] = checks.pop("tx_max"), checks.pop("tn_min")

    # The highest and lowest days of the ten years, against stations with every year.
    for key, col, how, row_id in (("tx_max", "TXAB", "max", "record_high"), ("tn_min", "TNAB", "min", "record_low")):
        sub = o.dropna(subset=[col])
        rec = sub.groupby(["NUM_POSTE", "month"]).agg(obs=(col, how), grid=("grid_" + key, how), n=(col, "size"))
        rec = rec[rec["n"] >= len(years) - 1].reset_index().join(st["dz_km"], on="NUM_POSTE")
        ab = coef[key][rec["month"].to_numpy() - 1]
        err = rec["obs"] - (rec["grid"] + ab[:, 0] + ab[:, 1] * rec["dz_km"])
        Log.info(f"table {row_id}: {rec['NUM_POSTE'].nunique()} stations with {len(years) - 1}+ years · "
                 f"error {np.sqrt((err ** 2).mean()):.2f} °C, bias {err.mean():+.2f}")
        checks[row_id] = (f"Checked against {rec['NUM_POSTE'].nunique():,} stations: typical error "
                          f"{np.sqrt((err ** 2).mean()):.1f} °C. A single day, so a ten-year window is far "
                          "short of a record.")

    per = per_station_month("NBJRR1", "grid_rainy")
    ratio = (per["obs"] / per["grid"]).where(per["grid"] >= 0.5)
    coef["rainy"] = ratio.groupby(per["month"]).median().reindex(range(1, 13)).to_numpy()
    scaled = per["grid"] * coef["rainy"][per["month"].to_numpy() - 1]
    err = (scaled - per["obs"]).abs()
    Log.info(f"table rainy: SAFRAN counts scaled by {coef['rainy'].min():.3f}–{coef['rainy'].max():.3f} by month · "
             f"median error {err.median():.1f} days")
    checks["rainy"] = (f"Scaled month by month to {per['NUM_POSTE'].nunique():,} rain gauges, which count "
                       f"5–11% fewer rainy days than the grid; typical error {err.median():.1f} days a month.")

    o["GLOT_kwh"] = o["GLOT"] / J_CM2_PER_KWH_M2
    for key, col, label in (("precip", "RR", "precip"), ("ssi", "GLOT_kwh", "solar")):
        per = per_station_month(col, "grid_" + key)
        err = ((per["grid"] - per["obs"]) / per["obs"]).where(per["obs"] > 5)
        Log.info(f"table {label}: {per['NUM_POSTE'].nunique()} stations · median error {err.median():+.1%} · "
                 f"median absolute {err.abs().median():.1%} (not corrected)")
        checks[label] = (f"Within {err.abs().median():.0%} of {per['NUM_POSTE'].nunique():,} stations month by month; "
                         "not corrected.")

    per = per_station_month("NBJNEIG", "grid_rainy")
    ab = coef["tn"][per["month"].to_numpy() - 1]
    rows = st["row"].reindex(per["NUM_POSTE"]).to_numpy()
    est = np.array([interpolate_counts(snow[:, m - 1, :], np.array([r]), np.array([a + b * dz]))[0]
                    for m, r, a, b, dz in zip(per["month"], rows, ab[:, 0], ab[:, 1], per["dz_km"])])
    per = per.assign(est=est, err=est - per["obs"])
    full = per.groupby("NUM_POSTE").filter(lambda d: len(d) == 12)
    season = full.groupby("NUM_POSTE")[["obs", "est"]].sum()
    season_err = (season["est"] - season["obs"]).abs()
    mountain = per["dz_km"].abs() > 0.3
    Log.info(f"table snow: {per['NUM_POSTE'].nunique()} stations · month MAE {per['err'].abs().mean():.2f} days "
             f"({per.loc[mountain, 'err'].abs().mean():.2f} in the mountains) · bias {per['err'].mean():+.2f} · "
             f"whole-year MAE {season_err.mean():.1f} days over {len(season)} stations "
             f"(mean {season['obs'].mean():.1f} observed)")
    checks["snow"] = (f"A day with at least 1 mm of precipitation and a low at or below 0 °C at residents' altitude. "
                      f"Checked against the {per['NUM_POSTE'].nunique():,} stations where observers log snowfall: "
                      f"off by {season_err.mean():.0f} days over a year on average.")
    return coef, checks


def commune_table(pairs: pd.DataFrame, codes: pd.Index, months: dict, snow: np.ndarray, coef: dict,
                  day_weights: np.ndarray) -> np.ndarray:
    """communes × TABLE_ROWS × 13 (Jan–Dec, Year).

    `pairs` has one row per (commune, grid point) with `pop` and the
    population-weighted `dz_km` of the homes in it. Every row but snow days is
    linear in the grid value and dz, so a weighted mean over pairs is exact;
    snow days use each pair's mean dz.
    """
    ci = codes.get_indexer(pairs["code"])
    if (ci < 0).any():
        raise BuildError("climate table: a population cell's commune is not in the commune list")
    r = pairs["row"].to_numpy()
    w = pairs["pop"].to_numpy()
    dz = pairs["dz_km"].to_numpy()[:, None]
    total = np.bincount(ci, weights=w, minlength=len(codes))

    def by_commune(values: np.ndarray) -> np.ndarray:
        values = values.reshape(len(pairs), -1)
        return np.stack([np.bincount(ci, weights=w * values[:, k], minlength=len(codes))
                         for k in range(values.shape[1])], axis=1) / total[:, None]

    def corrected(key: str, grid: np.ndarray) -> np.ndarray:
        ab = coef[key][:12]
        return grid[r] + ab[:, 0] + ab[:, 1] * dz

    mean10 = {key: months[key].mean(axis=0) for key in MONTHLY}
    out = np.full((len(codes), len(TABLE_ROWS), 13), np.nan)
    rows = {row["id"]: i for i, row in enumerate(TABLE_ROWS)}

    def put(row_id: str, monthly: np.ndarray) -> None:
        out[:, rows[row_id], :12] = monthly

    put("record_high", by_commune(corrected("tx_max", months["tx_max"].max(axis=0))))
    put("mean_max", by_commune(corrected("tx_max", mean10["tx_max"])))
    put("tx", by_commune(corrected("tx", mean10["tx"])))
    put("tm", by_commune(corrected("tm", mean10["tm"])))
    put("tn", by_commune(corrected("tn", mean10["tn"])))
    put("mean_min", by_commune(corrected("tn_min", mean10["tn_min"])))
    put("record_low", by_commune(corrected("tn_min", months["tn_min"].min(axis=0))))
    put("precip", by_commune(mean10["precip"][r]))
    put("rainy", by_commune(mean10["rainy"][r] * coef["rainy"]))
    put("hu", by_commune(corrected("hu", mean10["hu"])))
    put("solar", by_commune(mean10["ssi"][r]))
    ab = coef["tn"]
    put("snow", by_commune(np.stack([interpolate_counts(snow[:, m, :], r, ab[m, 0] + ab[m, 1] * dz[:, 0])
                                     for m in range(12)], axis=1)))

    for key, row_id in (("tx_max", "mean_max"), ("tn_min", "mean_min")):
        a, b = coef[key][12]
        out[:, rows[row_id], 12] = by_commune(months[f"{key}_year"].mean(axis=0)[r] + a + b * dz[:, 0])[:, 0]
    for row in TABLE_ROWS:
        i, values = rows[row["id"]], out[:, rows[row["id"]], :12]
        if row["year"] == "max":
            out[:, i, 12] = values.max(axis=1)
        elif row["year"] == "min":
            out[:, i, 12] = values.min(axis=1)
        elif row["year"] == "sum":
            out[:, i, 12] = values.sum(axis=1)
        elif row["year"] == "mean":
            out[:, i, 12] = (values * day_weights).sum(axis=1) / day_weights.sum()
    return out


def write_table(folder, codes: pd.Index, altitude_m: pd.Series, table: np.ndarray, checks: dict,
                years: list[int], attribution: str) -> None:
    """index.json (rows, notes) and one {department}.json of commune → rows, fetched on demand."""
    import json
    import shutil

    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    decimals = np.array([row["decimals"] for row in TABLE_ROWS])
    by_dept: dict[str, dict] = {}
    for i, code in enumerate(codes):
        values = [[None if np.isnan(v) else (round(float(v), int(d)) if d else int(round(float(v)))) for v in row]
                  for row, d in zip(table[i], decimals)]
        alt = altitude_m.get(code)
        by_dept.setdefault(code[:2], {})[code] = {"alt": None if pd.isna(alt) else int(alt), "rows": values}
    for dept, communes in by_dept.items():
        (folder / f"{dept}.json").write_text(json.dumps(communes, ensure_ascii=False, separators=(",", ":")),
                                             encoding="utf-8")
    period = f"{years[0]}–{years[-1]}"
    index = {
        "period": period,
        "columns": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Year"],
        "rows": [{**{k: row[k] for k in ("id", "label", "unit", "decimals", "scale")},
                  "label": f"{row['label']}, {period}" if row["id"] in ("record_high", "record_low") else row["label"],
                  "check": checks.get(row["id"], "")} for row in TABLE_ROWS],
        "notes": [
            f"Averages of {period}, from Météo-France's SAFRAN reanalysis (8 km daily grid), weighted by where "
            "the commune's residents live. Temperatures are corrected month by month from the grid cell's mean "
            "altitude to theirs, fitted on Météo-France stations.",
            "Highest and lowest are the extremes of these ten years, not all-time records. Mean maximum is the "
            "average of each month's (or year's) single hottest day; mean minimum likewise for the coldest.",
            "Solar energy stands in for sunshine hours, which only ~190 stations measure. As a guide, 1,050 "
            "kWh/m² a year is about 1,800–1,900 hours of sun, 1,600 about 2,800–2,900.",
            "Rainfall is not corrected for altitude: in a sheltered mountain valley the grid can overstate it. "
            "On the Mediterranean shore, sea breezes keep summer highs lower than an 8 km cell reaching inland.",
        ],
        "attribution": attribution,
        "departments": sorted(by_dept),
    }
    (folder / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    size = sum(p.stat().st_size for p in folder.iterdir())
    Log.ok(f"climate table: {len(codes):,} communes in {len(by_dept)} department files, {size / 1e6:.1f} MB")


# ------------------------------------------------------------ transform
def transform(ctx) -> None:
    sim = ctx.meta["sim2"]
    years = list(sim["years"])
    rainy_mm = float(ctx.meta["rainy_day_mm"])
    hot_c = float(ctx.meta["hot_day_c"])
    snow_day = ctx.meta["snow_day"]
    if not np.array_equal(SHIFTS, -SHIFTS[::-1]):
        raise BuildError("SHIFTS must be symmetric about zero for the snow-day table")

    # 1 — grid summaries -------------------------------------------------
    summaries, hot_tables, frost_tables, points = {}, [], [], None
    monthly_years, snow_sum = [], 0
    for year in years:
        path = ctx.raw_dir / f"QUOT_SIM2_{year}.csv.gz"
        if not path.exists():
            raise BuildError(f"{path} is missing — run the fetch stage first")
        summary, hot, frost, monthly = year_summary(path, rainy_mm, hot_c, snow_day)
        if points is None:
            points = summary.index
        elif not summary.index.equals(points):
            raise BuildError(f"{path.name}: grid points differ from {years[0]}")
        summaries[year] = summary
        hot_tables.append(hot)
        frost_tables.append(frost)
        snow_sum = snow_sum + monthly.pop("snow")
        monthly_years.append(monthly)
        Log.info(f"{year}: median {summary['precip'].median():.0f} mm, {summary['rainy'].median():.0f} rainy days, "
                 f"{summary['tmean'].median():.1f} °C, {summary['ssi'].median():.0f} kWh/m²")
    by_year = pd.concat(summaries, names=["year"])
    grid = by_year.groupby(level=["LAMBX", "LAMBY"]).mean().reindex(points)
    driest = by_year["precip"].groupby(level=["LAMBX", "LAMBY"]).min().reindex(points)
    wettest = by_year["precip"].groupby(level=["LAMBX", "LAMBY"]).max().reindex(points)
    hot_table = np.mean(hot_tables, axis=0)
    frost_table = np.mean(frost_tables, axis=0)
    months = {key: np.stack([m[key] for m in monthly_years]) for key in monthly_years[0]}   # years × points (× 12)
    snow_table = snow_sum / len(years)
    del monthly_years
    calendar = pd.date_range(f"{years[0]}-01-01", f"{years[-1]}-12-31")
    day_weights = np.bincount(calendar.month - 1, minlength=12) / len(years)
    zero = int(np.argmin(np.abs(SHIFTS)))
    row_of = pd.Series(np.arange(len(points)), index=points)

    # Every point needing a terrain height goes through one altitude() call.
    communes = gpd.read_file(ctx.communes_geojson())[["code_insee", "geometry"]].to_crs(LAEA)
    people = read_population()
    people["code"] = assign_communes(people, communes)
    rep = communes.representative_point()
    annual, stations, station_months = read_stations(ctx.raw_dir / "stations", years)

    laea_to_l2e = Transformer.from_crs(LAEA, LAMBERT2E, always_xy=True)
    laea_to_wgs = Transformer.from_crs(LAEA, WGS84, always_xy=True)
    l2e_to_wgs = Transformer.from_crs(LAMBERT2E, WGS84, always_xy=True)
    wgs_to_l2e = Transformer.from_crs(WGS84, LAMBERT2E, always_xy=True)

    cx, cy = cell_sample_points(points)
    cell_lon, cell_lat = l2e_to_wgs.transform(cx, cy)
    px, py = people["x"].to_numpy(), people["y"].to_numpy()
    p_lon, p_lat = laea_to_wgs.transform(px, py)
    r_lon, r_lat = laea_to_wgs.transform(rep.x.to_numpy(), rep.y.to_numpy())
    lon = np.concatenate([cell_lon, p_lon, r_lon])
    lat = np.concatenate([cell_lat, p_lat, r_lat])
    Log.info(f"terrain heights for {len(lon):,} points")
    heights = altitude(lon, lat)
    n_cell, n_people = len(cell_lon), len(p_lon)
    samples = heights[:n_cell].reshape(len(points), -1)
    grid_alt = pd.Series(np.nanmean(np.where(np.isnan(samples), 0.0, samples), axis=1), index=points)
    people_alt = heights[n_cell:n_cell + n_people]
    rep_alt = heights[n_cell + n_people:]

    # 2 — calibrate against stations ----------------------------------
    sx, sy = wgs_to_l2e.transform(stations["LON"].to_numpy(), stations["LAT"].to_numpy())
    skey = lattice_key(sx, sy)
    stations = stations.assign(LAMBX=skey.get_level_values(0), LAMBY=skey.get_level_values(1))
    stations = stations[pd.MultiIndex.from_frame(stations[["LAMBX", "LAMBY"]]).isin(points)]
    stations["dz_km"] = (stations["ALTI"] - grid_alt.reindex(
        pd.MultiIndex.from_frame(stations[["LAMBX", "LAMBY"]])).to_numpy()) / 1000

    grid_years = by_year.reset_index()
    joined = (annual.merge(stations.reset_index(), on="NUM_POSTE")
                    .merge(grid_years, on=["year", "LAMBX", "LAMBY"], suffixes=("_obs", "")))
    min_years = int(ctx.meta["stations"]["min_years"])
    limits = ctx.meta["max_cv_rmse"]
    coef = {}
    for name, (obs_col, grid_col) in TEMPERATURES.items():
        sub = joined[["NUM_POSTE", "dz_km", obs_col, grid_col]].dropna()
        per = sub.groupby("NUM_POSTE").agg(dz_km=("dz_km", "first"), obs=(obs_col, "mean"),
                                           grid=(grid_col, "mean"), n=(obs_col, "size"))
        per = per[per["n"] >= min_years]
        raw = per["obs"] - per["grid"]
        a, b, cv = fit_correction(raw, per["dz_km"])
        coef[name] = (a, b)
        rmse_raw, rmse_cv = float(np.sqrt((raw ** 2).mean())), float(np.sqrt((cv ** 2).mean()))
        mountain = (per["dz_km"].abs() > 0.3).to_numpy()
        Log.info(f"{name}: {len(per)} stations · correction {a:+.2f} °C {b:+.2f} °C/km · error at stations "
                 f"{rmse_raw:.2f} → {rmse_cv:.2f} °C cross-validated "
                 f"({np.sqrt((raw[mountain] ** 2).mean()):.2f} → {np.sqrt((cv[mountain] ** 2).mean()):.2f} "
                 f"where the station is 300 m+ off its cell's mean altitude)")
        if rmse_cv > float(limits[name]):
            raise BuildError(f"{name}: cross-validated error {rmse_cv:.2f} °C exceeds max_cv_rmse {limits[name]}")

    # The climate table's monthly fits; rainy days on the map use its monthly scaling too,
    # so the map and the table's Year column agree.
    table_coef, table_checks = calibrate_table(station_months, stations, row_of, months, snow_table, years,
                                               min_years, ctx.meta["max_cv_rmse_monthly"])
    rainy_scaled = months["rainy"].mean(axis=0) @ table_coef["rainy"]
    rain = joined[["NUM_POSTE", "rainy_obs", "LAMBX", "LAMBY"]].dropna().groupby("NUM_POSTE").agg(
        obs=("rainy_obs", "mean"), n=("rainy_obs", "size"), LAMBX=("LAMBX", "first"), LAMBY=("LAMBY", "first"))
    rain = rain[rain["n"] >= min_years]
    rain_est = rainy_scaled[row_of.reindex(pd.MultiIndex.from_frame(rain[["LAMBX", "LAMBY"]])).to_numpy()]
    rain_err = (rain_est - rain["obs"]) / rain["obs"]
    Log.info(f"rainy days: {len(rain)} stations · after monthly scaling, median error {rain_err.median():+.1%}, "
             f"median absolute {rain_err.abs().median():.1%}")
    for obs_col, grid_col, label in (("precip_obs", "precip", "rainfall"), ("ssi_obs", "ssi", "solar energy")):
        s = joined[["NUM_POSTE", obs_col, grid_col]].dropna().groupby("NUM_POSTE").agg(
            obs=(obs_col, "mean"), grid=(grid_col, "mean"), n=(obs_col, "size"))
        s = s[s["n"] >= min_years]
        rel = (s["grid"] - s["obs"]) / s["obs"]
        Log.info(f"{label}: {len(s)} stations · median error {rel.median():+.1%}, median absolute {rel.abs().median():.1%} "
                 "(not corrected)")

    # Station check of the threshold counts, with the same per-day shift the communes get.
    srow = row_of.reindex(pd.MultiIndex.from_frame(stations[["LAMBX", "LAMBY"]])).to_numpy()
    for name, table, (a, b), obs_col in (("days ≥30 °C", hot_table, coef["tx_ete"], "hot"),
                                        ("frost days", frost_table, coef["tn_hiver"], "frost")):
        est = interpolate_counts(table, srow, a + b * stations["dz_km"].to_numpy())
        obs = annual.groupby("NUM_POSTE")[obs_col].agg(["mean", "count"])
        cmp = pd.DataFrame({"est": est}, index=stations.index).join(obs)
        cmp = cmp[cmp["count"] >= min_years]
        err = cmp["mean"] - cmp["est"]
        Log.info(f"{name}: {len(cmp)} stations · bias {err.mean():+.1f} · median absolute error "
                 f"{err.abs().median():.1f} days · RMSE {np.sqrt((err ** 2).mean()):.1f}")

    # 3 — to where people live ------------------------------------------
    # Grid points in metres, for the few places whose own lattice position is
    # not in SAFRAN — shoreline homes whose 8 km square is mostly sea, islands.
    grid_tree = shapely.STRtree(shapely.points(points.get_level_values(0) * 100.0,
                                               points.get_level_values(1) * 100.0))

    def grid_rows(xs_laea, ys_laea):
        x2, y2 = laea_to_l2e.transform(xs_laea, ys_laea)
        rows = row_of.reindex(lattice_key(x2, y2)).to_numpy().astype(float)
        off = np.isnan(rows)
        if off.any():
            which, nearest = grid_tree.query_nearest(shapely.points(x2[off], y2[off]), all_matches=False)
            fill = rows[off]
            fill[which] = nearest
            rows[off] = fill
        return rows

    def at(xs_laea, ys_laea, alts):
        rows = grid_rows(xs_laea, ys_laea)
        ok = ~np.isnan(rows)
        r = np.where(ok, rows, 0).astype(int)
        dz = (alts - grid_alt.to_numpy()[r]) / 1000
        dz = np.where(np.isnan(dz), 0.0, dz)
        cols = {}
        for name, (_, grid_col) in TEMPERATURES.items():
            a, b = coef[name]
            cols[f"climat_{name}"] = grid[grid_col].to_numpy()[r] + a + b * dz
        a, b = coef["tx_ete"]
        cols["climat_jours_30"] = interpolate_counts(hot_table, r, a + b * dz)
        a, b = coef["tn_hiver"]
        cols["climat_jours_gel"] = interpolate_counts(frost_table, r, a + b * dz)
        cols["climat_jours_pluie"] = rainy_scaled[r]
        cols["climat_pluie_mm"] = grid["precip"].to_numpy()[r]
        cols["climat_pluie_sec_mm"] = driest.to_numpy()[r]
        cols["climat_pluie_humide_mm"] = wettest.to_numpy()[r]
        cols["climat_soleil_kwh"] = grid["ssi"].to_numpy()[r]
        cols["climat_soleil_hiver_kwh"] = grid["ssi_dark"].to_numpy()[r]
        cols["climat_altitude"] = alts
        return {k: np.where(ok, v, np.nan) for k, v in cols.items()}, ok

    people_vals, _ = at(px, py, people_alt)
    rep_vals, _ = at(rep.x.to_numpy(), rep.y.to_numpy(), rep_alt)

    result = pd.DataFrame(index=pd.Index(communes["code_insee"].to_numpy(), name="code_insee"))
    for name, values in people_vals.items():
        by_pop = weighted_by_commune(people["code"], values, people["pop"].to_numpy())
        result[name] = by_pop.reindex(result.index).fillna(pd.Series(rep_vals[name], index=result.index))

    order = ["climat_jours_pluie", "climat_pluie_mm", "climat_pluie_sec_mm", "climat_pluie_humide_mm",
             "climat_soleil_kwh", "climat_soleil_hiver_kwh", "climat_tx_ete", "climat_temp_moy",
             "climat_tn_hiver", "climat_jours_30", "climat_jours_gel", "climat_altitude"]
    result = result[order]
    for col in order:
        if col in ("climat_tx_ete", "climat_temp_moy", "climat_tn_hiver"):
            result[col] = result[col].round(1)
        else:
            result[col] = result[col].round(0).astype("Int64")
    missing = result.isna().any(axis=1)
    if missing.any():
        Log.warn(f"{missing.sum():,} commune(s) with a missing value: {', '.join(result.index[missing][:8])}")

    q = result.quantile([0.05, 0.5, 0.95])
    for col in ("climat_jours_pluie", "climat_soleil_kwh", "climat_tx_ete", "climat_jours_30"):
        Log.info(f"{col}: p5 {q.loc[0.05, col]} · median {q.loc[0.5, col]} · p95 {q.loc[0.95, col]}")
    result.reset_index().sort_values("code_insee").to_csv(ctx.out_path, index=False)

    # 4 — the climate table -----------------------------------------------
    # One row per (commune, grid point) with the homes' population and their mean
    # altitude offset; communes with no populated cell use their representative point.
    def pairs_of(codes, xs, ys, alts, weights):
        rows = grid_rows(xs, ys)
        dz = (alts - grid_alt.to_numpy()[np.nan_to_num(rows).astype(int)]) / 1000
        frame = pd.DataFrame({"code": codes, "row": rows, "pop": weights,
                              "popdz": weights * np.nan_to_num(dz)}).dropna(subset=["code", "row"])
        frame = frame.groupby(["code", frame["row"].astype(int)])[["pop", "popdz"]].sum().reset_index()
        return frame

    pairs = pairs_of(people["code"].to_numpy(), px, py, people_alt, people["pop"].to_numpy(dtype=float))
    pairs = pairs[pairs["pop"] > 0]
    empty = ~communes["code_insee"].isin(pairs["code"]).to_numpy()
    pairs = pd.concat([pairs, pairs_of(communes["code_insee"].to_numpy()[empty], rep.x.to_numpy()[empty],
                                       rep.y.to_numpy()[empty], rep_alt[empty], np.ones(empty.sum()))])
    pairs["dz_km"] = pairs["popdz"] / pairs["pop"]
    codes = pd.Index(communes["code_insee"].to_numpy())
    table = commune_table(pairs, codes, months, snow_table, table_coef, day_weights)

    rows = {row["id"]: i for i, row in enumerate(TABLE_ROWS)}
    gap = (pd.Series(table[:, rows["tm"], 12], index=codes) - result["climat_temp_moy"]).abs()
    Log.info(f"table vs map: annual mean temperature differs by median {gap.median():.2f}, max {gap.max():.2f} °C; "
             f"rainy days by max {np.nanmax(np.abs(table[:, rows['rainy'], 12] - result['climat_jours_pluie'].reindex(codes).astype(float))):.1f}")
    if np.isnan(table).any():
        Log.warn(f"climate table: {int(np.isnan(table).any(axis=(1, 2)).sum())} commune(s) with a missing value")
    write_table(ctx.files_dir, codes, result["climat_altitude"], table, table_checks, years, ctx.meta["attribution"])
