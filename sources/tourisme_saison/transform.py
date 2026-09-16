"""Tourist seasonality → data/processed/tourisme_saison.csv.

Two readings, kept apart. The department's hotel season is measured monthly and
carried down to every commune in it, labelled as departmental. The commune's own
seasonal capacity — the share of its beds that shut out of season — is a fact
about the building stock and is genuinely local. They are never multiplied
together; source.yaml says why.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from pipeline.common import BuildError, Log, dept_of, extract_zip

MONTHS = list(range(1, 13))
MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]

# Beds per unit, matching tourisme_capacite's coefficients — the shares below
# are of beds, not of rooms and pitches, or a hotel would count as one bed.
BEDS_PER_ROOM = 2.0
BEDS_PER_PITCH = 3.0


def _hotel_curves(path, serie) -> pd.DataFrame:
    """Department × month share of hotel nights, averaged over the chosen years."""
    years = {str(y) for y in serie["annees"]}
    cols = ["GEO", "GEO_OBJECT", "FREQ", "ACTIVITY", "TOUR_RESID", "TOUR_MEASURE", "TIME_PERIOD", "OBS_VALUE"]
    df = pd.read_csv(path, sep=";", usecols=cols, dtype=str)
    df = df[
        (df["GEO_OBJECT"] == serie["geo"]) & (df["FREQ"] == "M")
        & (df["ACTIVITY"] == serie["activite"]) & (df["TOUR_MEASURE"] == serie["mesure"])
        # Nights are published split by visitor origin as well as in total;
        # "_T" is the total, and adding the parts to it would double-count.
        & (df["TOUR_RESID"] == "_T")
        & df["TIME_PERIOD"].str[:4].isin(years)
    ].copy()
    if df.empty:
        raise BuildError(f"no monthly {serie['activite']} nights for {sorted(years)} — has the cube changed?")

    df["mois"] = df["TIME_PERIOD"].str[5:7].astype(int)
    df["nuitees"] = pd.to_numeric(df["OBS_VALUE"], errors="coerce")
    df = df.dropna(subset=["nuitees"])

    totals = df.pivot_table(index="GEO", columns="mois", values="nuitees", aggfunc="sum").reindex(columns=MONTHS)
    # A department missing months would produce a curve that is not a year.
    complete = totals.notna().all(axis=1)
    if not complete.all():
        Log.warn(f"{(~complete).sum()} department(s) lack a full year and are dropped: "
                 f"{', '.join(totals.index[~complete][:5])}")
    totals = totals[complete]
    Log.info(f"hotel curves: {len(totals)} departments over {min(years)}–{max(years)}")
    return totals.div(totals.sum(axis=1), axis=0)


def _seasonal_share(capacity: pd.DataFrame, flags: dict) -> pd.Series:
    """Share of each commune's *bookable* beds that shut out of season.

    Bookable, not all, on purpose. Second homes outnumber commercial beds several
    to one in exactly the communes this measures, so including them would bury
    the answer under "how many second homes are there" — and a second home is
    usable in any season anyway, its owner simply chooses. Pitches let by the
    year belong with the second homes for the same reason, and capacity already
    counts them there.
    """
    beds = {
        "hotel": capacity["chambres_hotel"] * BEDS_PER_ROOM,
        "camping": (capacity["emplacements_camping"] - capacity["emplacements_camping_annuels"]) * BEDS_PER_PITCH,
        "collectif": capacity["lits_hebergement_collectif"],
    }
    missing = set(flags) - set(beds)
    if missing:
        raise BuildError(f"lits_saisonniers names unknown bed type(s): {sorted(missing)}")
    total = sum(beds.values())
    # The parts must reconstruct what tourisme_capacite published, or the two
    # sources have drifted apart and the share is of the wrong denominator.
    drift = (total - capacity["lits_marchands"]).abs()
    if (drift > 1).any():
        worst = drift.idxmax()
        raise BuildError(f"bed arithmetic disagrees with tourisme_capacite at {worst}: "
                         f"{total[worst]:.0f} vs lits_marchands {capacity['lits_marchands'][worst]:.0f}")
    seasonal = sum(b for name, b in beds.items() if flags.get(name))
    return (seasonal / total * 100).where(total > 0)


def transform(ctx) -> None:
    meta = ctx.meta
    spec = meta["resource"]
    scratch = extract_zip(ctx.raw_dir / spec["filename"], ctx.scratch, members=[spec["member"]])
    curves = _hotel_curves(scratch / spec["member"], meta["serie"])

    capacity = pd.read_csv(ctx.processed("tourisme_capacite"), dtype={"code_insee": str}).set_index("code_insee")

    ete = curves[meta["saisons"]["ete"]].sum(axis=1) * 100
    hiver = curves[meta["saisons"]["hiver"]].sum(axis=1) * 100
    peak = pd.Series([MONTH_NAMES[i] for i in curves.to_numpy().argmax(axis=1)], index=curves.index)
    # How far the department's busiest month sits above a flat year: 1.0 is the
    # same traffic every month, 2.0 is a sixth of the year in one month.
    amplitude = curves.max(axis=1) * 12

    dept = pd.Series([dept_of(c) for c in capacity.index], index=capacity.index)
    unknown = sorted(set(dept) - set(curves.index))
    if unknown:
        Log.warn(f"{dept.isin(unknown).sum():,} communes in {len(unknown)} department(s) with no hotel curve: "
                 f"{', '.join(unknown[:5])}")

    out = pd.DataFrame(index=capacity.index)
    out["saison_hotel_ete"] = dept.map(ete)
    out["saison_hotel_hiver"] = dept.map(hiver)
    out["saison_hotel_pointe"] = dept.map(peak)
    out["saison_hotel_amplitude"] = dept.map(amplitude)
    out["part_lits_saisonniers"] = _seasonal_share(capacity, meta["lits_saisonniers"])

    # A commune with nothing bookable has no seasonal capacity to speak of; the
    # departmental columns still apply to it, so only this one is blanked.
    no_beds = capacity["lits_marchands"].fillna(0) <= 0
    out.loc[no_beds, "part_lits_saisonniers"] = np.nan

    numeric = ["saison_hotel_ete", "saison_hotel_hiver", "saison_hotel_amplitude", "part_lits_saisonniers"]
    out[numeric] = out[numeric].astype(float).round(1)

    _log_references(out, meta, no_beds)
    out.sort_index().to_csv(ctx.out_path)


def _log_references(out: pd.DataFrame, meta: dict, no_beds: pd.Series) -> None:
    Log.info(f"{len(out):,} communes · {no_beds.sum():,} with nothing bookable · "
             f"median {out['part_lits_saisonniers'].median():.0f}% of bookable beds seasonal")
    Log.info(f"departmental summer share spans {out['saison_hotel_ete'].min():.0f}–"
             f"{out['saison_hotel_ete'].max():.0f}%, winter {out['saison_hotel_hiver'].min():.0f}–"
             f"{out['saison_hotel_hiver'].max():.0f}%")
    for ref in meta.get("reference_communes", []):
        if ref["code_insee"] not in out.index:
            Log.warn(f"  {ref['name']}: not in the output")
            continue
        row = out.loc[ref["code_insee"]]
        seasonal = "—" if pd.isna(row["part_lits_saisonniers"]) else f"{row['part_lits_saisonniers']:>5.1f}%"
        Log.info(f"  {ref['name']:<24} dept {row['saison_hotel_ete']:>5.1f}% summer / "
                 f"{row['saison_hotel_hiver']:>5.1f}% winter, peak {row['saison_hotel_pointe']:<9} · "
                 f"seasonal beds {seasonal}")
