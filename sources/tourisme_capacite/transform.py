"""Tourist beds per commune → data/processed/tourisme_capacite.csv.

Two INSEE cubes, both far too wide to load whole: the capacity file carries every
star rating and geography, the census file every dwelling characteristic. Each is
read in chunks and filtered down to the totals rows before anything is kept.
"""
from __future__ import annotations

import pandas as pd

from pipeline.common import BuildError, Log, extract_zip, is_metropolitan, normalise_insee

CHUNK = 500_000


def _read_filtered(path, usecols, keep) -> pd.DataFrame:
    """Read a ;-separated INSEE cube in chunks, keeping the rows `keep` selects."""
    parts = []
    for chunk in pd.read_csv(path, sep=";", usecols=usecols, dtype=str, chunksize=CHUNK):
        parts.append(chunk[keep(chunk)])
    if not parts:
        raise BuildError(f"{path.name}: no rows survived filtering")
    return pd.concat(parts, ignore_index=True)


def _capacity(path, meta) -> pd.DataFrame:
    """Beds by type per commune, from DS_TOUR_CAP."""
    cols = ["GEO", "GEO_OBJECT", "ACTIVITY", "UNIT_LOC_RANKING", "L_STAY", "TOUR_MEASURE", "OBS_VALUE"]
    codes = {spec["code"] for spec in meta["activites"].values()}
    df = _read_filtered(
        path, cols,
        # Only communes, only the all-star / all-length-of-stay totals, and only
        # the three activity codes — the sub-breakdowns would double-count.
        lambda c: (c["GEO_OBJECT"] == "COM") & c["ACTIVITY"].isin(codes) & (c["UNIT_LOC_RANKING"] == "_T"),
    )
    df["code_insee"] = df["GEO"].map(normalise_insee)
    df["OBS_VALUE"] = pd.to_numeric(df["OBS_VALUE"], errors="coerce")
    df = df[df["code_insee"].notna() & df["code_insee"].map(is_metropolitan)]

    out = {}
    long_stay = meta.get("camping_longue_duree_est_residentiel", False)
    for name, spec in meta["activites"].items():
        rows = df[(df["ACTIVITY"] == spec["code"]) & (df["TOUR_MEASURE"] == spec["mesure"])]
        total = rows[rows["L_STAY"] == "_T"].set_index("code_insee")["OBS_VALUE"]
        if total.index.duplicated().any():
            raise BuildError(f"{name}: duplicate commune rows in the capacity file")
        out[name] = total
        # Campings carry the only L_STAY split: pitches let by the year vs to
        # passing trade. Keep the residential half apart if source.yaml asks.
        if name == "camping" and long_stay:
            out["camping_annuel"] = rows[rows["L_STAY"] == "LONG"].set_index("code_insee")["OBS_VALUE"]

    cap = pd.DataFrame(out)
    Log.info(f"capacity: {len(cap):,} communes · {int(cap['hotel'].sum()):,} hotel rooms · "
             f"{int(cap['camping'].sum()):,} camping pitches · {int(cap['collectif'].sum()):,} other beds")
    return cap


def _dwellings(path, meta) -> pd.DataFrame:
    """Main dwellings, second homes and total per commune, from the census."""
    rp = meta["recensement"]
    wanted = {rp["categorie_residence_secondaire"], rp["categorie_residence_principale"], rp["categorie_total"]}
    header = pd.read_csv(path, sep=";", nrows=0).columns.tolist()
    # Every other dimension must be its total, or the same dwelling is counted
    # once per age band, heating type and so on.
    dims = [c for c in header if c not in
            {"GEO", "GEO_OBJECT", "FREQ", "RP_MEASURE", "OCS", "TIME_PERIOD", "OBS_VALUE",
             "OBS_STATUS", "OBS_STATUS_FR", "CONF_STATUS", "UNIT_MULT", "DECIMALS", "UNIT_MEASURE"}]
    Log.info(f"census: collapsing over {len(dims)} other dimension(s): {', '.join(dims) or 'none'}")

    def keep(c):
        ok = (c["GEO_OBJECT"] == "COM") & (c["RP_MEASURE"] == "DWELLINGS") \
            & (c["TIME_PERIOD"] == rp["annee"]) & c["OCS"].isin(wanted)
        for d in dims:
            ok &= c[d] == "_T"
        return ok

    df = _read_filtered(path, ["GEO", "GEO_OBJECT", "RP_MEASURE", "OCS", "TIME_PERIOD", "OBS_VALUE"] + dims, keep)
    df["code_insee"] = df["GEO"].map(normalise_insee)
    df["OBS_VALUE"] = pd.to_numeric(df["OBS_VALUE"], errors="coerce")
    df = df[df["code_insee"].notna() & df["code_insee"].map(is_metropolitan)]

    wide = df.pivot_table(index="code_insee", columns="OCS", values="OBS_VALUE", aggfunc="sum")
    missing = wanted - set(wide.columns)
    if missing:
        raise BuildError(f"census {rp['annee']}: missing occupancy categor(y/ies) {sorted(missing)}")
    out = pd.DataFrame({
        "residences_secondaires": wide[rp["categorie_residence_secondaire"]],
        "residences_principales": wide[rp["categorie_residence_principale"]],
        "logements_total": wide[rp["categorie_total"]],
    })
    Log.info(f"census {rp['annee']}: {len(out):,} communes · "
             f"{int(out['residences_secondaires'].sum()):,} second homes")
    return out


def transform(ctx) -> None:
    meta = ctx.meta
    coef = meta["lits_par"]
    res = meta["resources"]

    scratch = extract_zip(ctx.raw_dir / res["capacite"]["filename"], ctx.scratch,
                          members=[res["capacite"]["member"]])
    cap = _capacity(scratch / res["capacite"]["member"], meta)
    scratch = extract_zip(ctx.raw_dir / res["logements"]["filename"], ctx.scratch,
                          members=[res["logements"]["member"]])
    dwell = _dwellings(scratch / res["logements"]["member"], meta)

    pop = pd.read_csv(ctx.processed("commune_reference"), dtype={"code_insee": str}).set_index("code_insee")

    df = pop[["population"]].join(cap, how="left").join(dwell, how="left")
    df[cap.columns.tolist()] = df[cap.columns.tolist()].fillna(0.0)

    # Commercial beds: hotel rooms, the collective file's own bed count, and the
    # camping pitches let to passing trade.
    pitches_annual = df["camping_annuel"] if "camping_annuel" in df else 0.0
    pitches_passing = df["camping"] - pitches_annual
    df["lits_marchands"] = (
        df["hotel"] * coef["chambre_hotel"]
        + df["collectif"]
        + pitches_passing * coef["emplacement_camping"]
    )
    # Non-commercial: second homes, plus the pitches someone keeps by the year.
    df["lits_residences_secondaires"] = (
        df["residences_secondaires"].fillna(0.0) * coef["residence_secondaire"]
        + pitches_annual * coef["emplacement_camping"]
    )
    df["lits_touristiques"] = df["lits_marchands"] + df["lits_residences_secondaires"]
    df["lits_par_habitant"] = (df["lits_touristiques"] / df["population"]).where(df["population"] > 0)
    df["part_lits_marchands"] = (df["lits_marchands"] / df["lits_touristiques"] * 100).where(
        df["lits_touristiques"] > 0)
    df["part_residences_secondaires"] = (
        df["residences_secondaires"] / df["logements_total"] * 100).where(df["logements_total"] > 0)

    # Published so that a source reading this table can tell the two kinds of
    # pitch apart without re-deriving the split from lits_marchands.
    df["emplacements_camping_annuels"] = pitches_annual
    df = df.rename(columns={"hotel": "chambres_hotel", "camping": "emplacements_camping",
                            "collectif": "lits_hebergement_collectif"})
    out = df[[
        "lits_touristiques", "lits_par_habitant", "lits_marchands", "lits_residences_secondaires",
        "part_lits_marchands", "chambres_hotel", "emplacements_camping", "emplacements_camping_annuels",
        "lits_hebergement_collectif", "residences_secondaires", "part_residences_secondaires",
    ]].round(2)

    for col in ("lits_touristiques", "lits_marchands", "lits_residences_secondaires",
                "chambres_hotel", "emplacements_camping", "emplacements_camping_annuels",
                "lits_hebergement_collectif", "residences_secondaires"):
        out[col] = out[col].round().astype("Int64")

    _log_references(out, meta)
    out.sort_index().to_csv(ctx.out_path)


def _log_references(out: pd.DataFrame, meta: dict) -> None:
    share = (out["lits_par_habitant"] > 1).mean() * 100
    Log.info(f"{len(out):,} communes · median {out['lits_par_habitant'].median():.2f} beds/resident · "
             f"{share:.1f}% have more tourist beds than residents")
    for ref in meta.get("reference_communes", []):
        row = out.loc[ref["code_insee"]] if ref["code_insee"] in out.index else None
        if row is None:
            Log.warn(f"  {ref['name']}: not in the output")
            continue
        Log.info(f"  {ref['name']:<24} {row['lits_touristiques']:>8,} beds · "
                 f"{row['lits_par_habitant']:>6.2f} per resident · "
                 f"{row['part_lits_marchands']:>5.1f}% commercial")
