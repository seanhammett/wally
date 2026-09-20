"""RP 2023 activity and employment → data/processed/emploi_statut.csv.

Three census cubes, each reduced to a handful of totals per commune. Every
dimension that is not being split is pinned to its total `_T`: the cubes carry
sex, age, education and working-time breakdowns side by side, and summing over
any of them counts the same people again.
"""
from __future__ import annotations

import pandas as pd

from pipeline.common import BuildError, Log, read_melodi_cube


def _cube(ctx, key: str, keep: dict, split: list[str]) -> pd.DataFrame:
    """One cube, pivoted to a column per combination of the `split` dimensions."""
    spec = ctx.meta["resources"][key]
    keep = {"TIME_PERIOD": [ctx.meta["millesime"]], **keep}
    df = read_melodi_cube(ctx.raw_dir / spec["filename"], spec["member"], keep)
    df["col"] = df[split].agg("|".join, axis=1)
    if df.duplicated(["code_insee", "col"]).any():
        raise BuildError(f"{key}: more than one row per commune and cell — a dimension is not pinned")
    wide = df.pivot(index="code_insee", columns="col", values="OBS_VALUE")
    Log.info(f"{key}: {len(wide):,} communes · cells {sorted(wide.columns)}")
    return wide


def _need(wide: pd.DataFrame, key: str, cols: list[str]) -> None:
    missing = set(cols) - set(wide.columns)
    if missing:
        raise BuildError(f"{key}: expected cell(s) {sorted(missing)} are not in the file")


def transform(ctx) -> None:
    floor = float(ctx.meta["min_denominateur"])

    # Residents by activity status. EMPSTA_ENQ: 1 employed, 1T2 active,
    # 2 unemployed, 31 retired, 33 student, _T everyone.
    res = _cube(ctx, "residents", {
        "SEX": ["_T"], "EDUC": ["_T"], "RP_MEASURE": ["POP"],
        "AGE": ["Y15T64", "Y_GE15"], "EMPSTA_ENQ": ["_T", "1", "1T2", "2", "31", "33"],
    }, ["AGE", "EMPSTA_ENQ"])
    _need(res, "residents", ["Y15T64|_T", "Y15T64|1", "Y15T64|1T2", "Y15T64|2",
                             "Y_GE15|_T", "Y_GE15|1", "Y_GE15|31", "Y_GE15|33"])

    # Employed residents (15+) by employment form and working time.
    # EMPFORM: 1 self-employed, 2 employee, 22T27 employee on a non-permanent contract.
    forms = _cube(ctx, "formes", {
        "SEX": ["_T"], "AGE": ["Y_GE15"], "EMPSTA_ENQ": ["1"], "RP_MEASURE": ["POP"],
        "WKTIME": ["_T", "PT"], "EMPFORM": ["_T", "1", "2", "22T27"],
    }, ["WKTIME", "EMPFORM"])
    _need(forms, "formes", ["_T|_T", "_T|1", "_T|2", "_T|22T27", "PT|_T"])

    # Jobs located in the commune, whoever holds them.
    lt = _cube(ctx, "lieu_travail", {
        "SEX": ["_T"], "WKTIME": ["_T"], "EMPFORM": ["_T"], "AGE": ["_T"],
        "EMPSTA_ENQ": ["1"], "RP_MEASURE": ["NBEMP"],
    }, ["EMPSTA_ENQ"])
    _need(lt, "lieu_travail", ["1"])

    df = res.join(forms, how="outer").join(lt.rename(columns={"1": "emplois"}), how="outer")

    def rate(num: pd.Series, den: pd.Series) -> pd.Series:
        return (100 * num / den).where(den >= floor).round(1)

    out = pd.DataFrame(index=df.index)
    out["taux_emploi_15_64"] = rate(df["Y15T64|1"], df["Y15T64|_T"])
    out["taux_chomage"] = rate(df["Y15T64|2"], df["Y15T64|1T2"])
    out["part_retraites"] = rate(df["Y_GE15|31"], df["Y_GE15|_T"])
    out["part_etudiants"] = rate(df["Y_GE15|33"], df["Y_GE15|_T"])
    out["part_independants"] = rate(df["_T|1"], df["_T|_T"])
    out["part_precaires"] = rate(df["_T|22T27"], df["_T|2"])
    out["part_temps_partiel"] = rate(df["PT|_T"], df["_T|_T"])
    out["actifs_occupes"] = df["_T|_T"].round().astype("Int64")
    out["emplois_au_lieu_de_travail"] = df["emplois"].round().astype("Int64")
    # Jobs here per 100 residents in work: INSEE's indicateur de concentration
    # d'emploi. Same floor on the denominator as the rates.
    out["indicateur_concentration_emploi"] = (100 * df["emplois"] / df["_T|_T"]).where(
        df["_T|_T"] >= floor).round()

    out.index.name = "code_insee"
    _log(out, df, ctx.meta)
    out.sort_index().to_csv(ctx.out_path)


def _log(out: pd.DataFrame, df: pd.DataFrame, meta: dict) -> None:
    Log.info(f"{len(out):,} communes")
    # Person-weighted national figures, to check against INSEE's published ones,
    # then the commune distribution the layer breaks are read from.
    national = {
        "employment rate 15–64": df["Y15T64|1"].sum() / df["Y15T64|_T"].sum(),
        "unemployment (census)": df["Y15T64|2"].sum() / df["Y15T64|1T2"].sum(),
        "self-employed": df["_T|1"].sum() / df["_T|_T"].sum(),
        "insecure contracts": df["_T|22T27"].sum() / df["_T|2"].sum(),
        "part-time": df["PT|_T"].sum() / df["_T|_T"].sum(),
    }
    for label, value in national.items():
        Log.info(f"  {label:<24} nationally {value:.1%}")
    for col in ("taux_emploi_15_64", "taux_chomage", "part_independants", "part_precaires",
                "indicateur_concentration_emploi"):
        q = out[col].quantile([0.1, 0.25, 0.5, 0.75, 0.9]).round(1).tolist()
        Log.info(f"  {col:<32} deciles 10/25/50/75/90 {q} · blank {out[col].isna().sum():,}")
    for ref in meta.get("reference_communes", []):
        if ref["code_insee"] not in out.index:
            Log.warn(f"  {ref['name']}: not in the output")
            continue
        r = out.loc[ref["code_insee"]]
        Log.info(f"  {ref['name']:<22} employed {r['taux_emploi_15_64']}% · unemployed {r['taux_chomage']}% · "
                 f"self-employed {r['part_independants']}% · jobs/100 workers {r['indicateur_concentration_emploi']}")
