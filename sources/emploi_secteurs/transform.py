"""Sector mix, residents and local jobs → data/processed/emploi_secteurs.csv.

Residents come from the census's ACT3 cube (38 sectors × self-employed or
employee); local jobs from FLORES (salaried jobs by 88 NAF divisions). Both are
folded into the groups source.yaml defines, then compared with France as a
whole to find each commune's most over-represented sector.
"""
from __future__ import annotations

import pandas as pd

from pipeline.common import BuildError, Log, read_melodi_cube

MIXED = "Mixed"


def _residents(ctx, groups: dict) -> pd.DataFrame:
    """Employed residents per group, split self-employed (1) / employee (2)."""
    spec = ctx.meta["resources"]["residents"]
    df = read_melodi_cube(ctx.raw_dir / spec["filename"], spec["member"], {
        "TIME_PERIOD": [spec["millesime"]], "SEX": ["_T"], "AGE": ["Y_GE15"],
        "EMPSTA_ENQ": ["1"], "RP_MEASURE": ["POP"], "EMPFORM": ["1", "2"],
    })
    # Rebuilt from the file's own EMP_ACTIVITY codes rather than trusted from
    # source.yaml, so a code INSEE adds or renames fails loudly instead of
    # silently dropping out of every group.
    codes = set(df["EMP_ACTIVITY"]) - {"_T"}
    mapped = {c for g in groups.values() for c in g["a38"]}
    if codes - mapped:
        raise BuildError(f"residents: A38 code(s) {sorted(codes - mapped)} are in no group in source.yaml")
    if mapped - codes:
        raise BuildError(f"residents: source.yaml lists A38 code(s) {sorted(mapped - codes)} the file does not have")

    wide = df.pivot_table(index="code_insee", columns=["EMP_ACTIVITY", "EMPFORM"],
                          values="OBS_VALUE", aggfunc="sum").fillna(0.0)
    out = pd.DataFrame(index=wide.index)
    out["actifs"] = wide[("_T", "1")] + wide[("_T", "2")]
    for key, g in groups.items():
        out[f"{key}|1"] = sum(wide[(c, "1")] for c in g["a38"])
        out[f"{key}|2"] = sum(wide[(c, "2")] for c in g["a38"])
    summed = sum(out[f"{k}|1"] + out[f"{k}|2"] for k in groups)
    gap = ((summed - out["actifs"]).abs() / out["actifs"]).where(out["actifs"] > 0)
    if (gap > 0.01).sum():
        Log.warn(f"residents: {(gap > 0.01).sum():,} commune(s) where the groups do not add up to the total")
    Log.info(f"residents: {len(out):,} communes · {out['actifs'].sum():,.0f} people in work")
    return out


def _local_jobs(ctx, groups: dict) -> pd.DataFrame:
    """Salaried jobs located in the commune per group, from FLORES."""
    spec = ctx.meta["resources"]["emplois"]
    df = read_melodi_cube(ctx.raw_dir / spec["filename"], spec["member"], {
        "TIME_PERIOD": [spec["millesime"]], "FLORES_MEASURE": ["EMPL3112"],
        "LEGAL_FORM_WITH_PUBLIC": ["1T9X7"],
    })
    df = df.rename(columns={"ACTIVITY": "a88"})
    # K = value withheld and counted in another cell. There are about a hundred
    # such cells nationally; a group that contains one is blank for that commune
    # rather than silently short.
    df["masque"] = df["OBS_STATUS"] == "K"
    Log.info(f"local jobs: {df['masque'].sum():,} withheld cell(s) of {len(df):,}")
    mapped = {c for g in groups.values() for c in g["a88"]}
    codes = set(df["a88"]) - {"_T"}
    if codes - mapped:
        raise BuildError(f"local jobs: A88 code(s) {sorted(codes - mapped)} are in no group in source.yaml")

    values = df.pivot_table(index="code_insee", columns="a88", values="OBS_VALUE", aggfunc="sum").fillna(0.0)
    masked = df.pivot_table(index="code_insee", columns="a88", values="masque", aggfunc="any").fillna(False)
    out = pd.DataFrame(index=values.index)
    out["emplois_salaries"] = values["_T"]
    for key, g in groups.items():
        cols = [c for c in g["a88"] if c in values.columns]
        out[f"emplois_{key}"] = values[cols].sum(axis=1).where(~masked[cols].any(axis=1))
    Log.info(f"local jobs: {len(out):,} communes · {out['emplois_salaries'].sum():,.0f} salaried jobs")
    return out


def transform(ctx) -> None:
    meta = ctx.meta
    groups = meta["groupes"]
    res = _residents(ctx, groups)
    jobs = _local_jobs(ctx, groups)

    min_parts = float(meta["min_actifs_parts"])
    min_dist = float(meta["min_actifs_distinctif"])
    min_lq = float(meta["min_lq"])
    min_jobs = float(meta["min_emplois"])

    out = pd.DataFrame(index=res.index.union(jobs.index))
    out.index.name = "code_insee"
    res = res.reindex(out.index)
    jobs = jobs.reindex(out.index)
    actifs = res["actifs"]
    out["actifs_occupes_secteur"] = actifs.round().astype("Int64")

    national = {}
    lq = pd.DataFrame(index=out.index)
    for key in groups:
        n = res[f"{key}|1"] + res[f"{key}|2"]
        national[key] = n.sum() / actifs.sum()
        out[f"part_{key}"] = (100 * n / actifs).where(actifs >= min_parts).round(1)
        lq[key] = (n / actifs) / national[key]
    for key in meta.get("independants_pour", []):
        n = res[f"{key}|1"] + res[f"{key}|2"]
        # The self-employed share of a sector needs the sector itself to have
        # people in it, not just the commune.
        out[f"part_independants_{key}"] = (100 * res[f"{key}|1"] / n).where(
            (actifs >= min_parts) & (n >= 10)).round(1)

    # Distinctive sector: the highest location quotient among the groups that
    # may carry one, published only where the sample can support a comparison.
    eligible = [k for k, g in groups.items() if g.get("distinctif", True)]
    top = lq[eligible].fillna(0.0).idxmax(axis=1)
    top_lq = lq[eligible].max(axis=1)
    labels = {k: g["label"] for k, g in groups.items()}
    enough = actifs >= min_dist
    out["secteur_distinctif"] = top.map(labels).where(top_lq >= min_lq, MIXED).where(enough)
    out["lq_distinctif"] = top_lq.where(enough & (top_lq >= min_lq)).round(2)

    salaried = jobs["emplois_salaries"]
    out["emplois_salaries"] = salaried.round().astype("Int64")
    for key in groups:
        out[f"part_emplois_{key}"] = (100 * jobs[f"emplois_{key}"] / salaried).where(salaried >= min_jobs).round(1)
    for key in ("hotellerie_restauration", "culture_medias_loisirs"):
        out[f"emplois_{key}"] = jobs[f"emplois_{key}"].round().astype("Int64")

    _log(out, national, groups, meta)
    out.sort_index().to_csv(ctx.out_path)


def _log(out: pd.DataFrame, national: dict, groups: dict, meta: dict) -> None:
    Log.info(f"{len(out):,} communes · sector shares published for "
             f"{out['part_commerce'].notna().sum():,}, distinctive sector for "
             f"{out['secteur_distinctif'].notna().sum():,}")
    total = 0.0
    for key, share in national.items():
        total += share
        col = out[f"part_{key}"]
        q = col.quantile([0.1, 0.5, 0.9]).round(1).tolist()
        Log.info(f"  {groups[key]['label']:<36} nationally {share:6.1%} · communes 10/50/90 {q}")
    Log.info(f"  groups add up to {total:.1%} of residents in work")
    counts = out["secteur_distinctif"].value_counts()
    Log.info("  distinctive: " + " · ".join(f"{k} {v:,}" for k, v in counts.items()))
    for ref in meta.get("reference_communes", []):
        if ref["code_insee"] not in out.index:
            Log.warn(f"  {ref['name']}: not in the output")
            continue
        r = out.loc[ref["code_insee"]]
        Log.info(f"  {ref['name']:<22} {r['secteur_distinctif']} (×{r['lq_distinctif']}) · "
                 f"hospitality {r['part_hotellerie_restauration']}% of residents, "
                 f"{r['part_emplois_hotellerie_restauration']}% of local jobs · "
                 f"culture {r['part_culture_medias_loisirs']}% / {r['part_emplois_culture_medias_loisirs']}%")
