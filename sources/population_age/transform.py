"""RP 2023 age structure → data/processed/population_age.csv.

A 648 MB CSV of 12.8 million observations, streamed out of the zip rather than
extracted, reduced to one row per commune. Only the COM rows at SEX=_T are kept:
the file also carries every other geographic level and the male/female splits,
and summing across SEX would count everyone twice.
"""
from __future__ import annotations

import csv
import io
import zipfile

import pandas as pd

from pipeline.common import BuildError, Log, is_metropolitan, normalise_insee

DATA_MEMBER = "DS_RP_TD_POPULATION_AGESEX_PRINC_2023_data.csv"
TOP_AGE = 100          # Y_GE100 is the top code; everyone in it is counted at 100
N_AGES = TOP_AGE + 1


def age_index(code: str) -> int | None:
    """'Y0'…'Y99' → 0…99, 'Y_GE100' → 100. '_T' and anything else → None."""
    if code == "Y_GE100":
        return TOP_AGE
    if len(code) > 1 and code[0] == "Y" and code[1:].isdigit():
        n = int(code[1:])
        return n if n <= TOP_AGE else TOP_AGE
    return None


def read_communes(zip_path) -> tuple[dict[str, list[float]], dict[str, float]]:
    counts: dict[str, list[float]] = {}
    totals: dict[str, float] = {}
    kept = skipped_geo = 0

    with zipfile.ZipFile(zip_path) as zf:
        if DATA_MEMBER not in zf.namelist():
            raise BuildError(f"{DATA_MEMBER} is not in the archive; members: {zf.namelist()}")
        with zf.open(DATA_MEMBER) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8", newline=""), delimiter=";")
            header = next(reader)
            try:
                i_geo, i_obj, i_age, i_sex, i_val = (
                    header.index(c) for c in ("GEO", "GEO_OBJECT", "AGE", "SEX", "OBS_VALUE")
                )
            except ValueError as exc:
                raise BuildError(f"unexpected header in {DATA_MEMBER}: {header}") from exc

            for row in reader:
                if row[i_obj] != "COM" or row[i_sex] != "_T":
                    continue
                code = normalise_insee(row[i_geo])
                if code is None or not is_metropolitan(code):
                    skipped_geo += 1
                    continue
                try:
                    value = float(row[i_val])
                except (ValueError, IndexError):
                    continue

                age = row[i_age]
                if age == "_T":
                    totals[code] = value
                    continue
                idx = age_index(age)
                if idx is None:
                    continue
                slot = counts.get(code)
                if slot is None:
                    slot = counts[code] = [0.0] * N_AGES
                # Y_GE100 lands in the same slot as Y100 would, hence += not =.
                slot[idx] += value
                kept += 1

    Log.info(f"{kept:,} commune-age observations kept · {skipped_geo:,} rows outside metropolitan France")
    return counts, totals


def median_age(slot: list[float], total: float) -> float | None:
    """Median from single years of age, interpolated inside the containing year.

    Taking it from bands would make the answer a multiple of the band width; the
    file has single years, so the median is worth computing properly.
    """
    if total <= 0:
        return None
    half = total / 2.0
    cum = 0.0
    for age, n in enumerate(slot):
        if n <= 0:
            continue
        if cum + n >= half:
            # Ages are whole-year bins: someone recorded at age 40 is uniformly
            # somewhere in [40, 41), so the interpolation runs across that year.
            return round(age + (half - cum) / n, 1)
        cum += n
    return float(TOP_AGE)


def transform(ctx) -> None:
    res = ctx.meta["resource"]
    bands = ctx.meta["bands"]
    min_u20 = int(ctx.meta["min_under_20"])

    counts, totals = read_communes(ctx.raw_dir / res["filename"])
    Log.info(f"{len(counts):,} metropolitan communes")

    rows = []
    mismatched = 0
    for code, slot in counts.items():
        summed = sum(slot)
        total = totals.get(code, summed)
        # The published _T and the sum over single years should agree; they are
        # both extrapolations of the same survey, so a gap means a parse fault.
        if total > 0 and abs(summed - total) / total > 0.01:
            mismatched += 1
            total = summed
        if total <= 0:
            continue

        row = {"code_insee": code, "population_rp": round(total)}
        for name, (lo, hi) in bands.items():
            row[name] = round(sum(slot[lo : min(hi, TOP_AGE) + 1]))

        under_20 = sum(slot[0:20])
        over_65 = sum(slot[65:])
        row["part_moins_20"] = round(100 * under_20 / total, 1)
        row["part_65_plus"] = round(100 * over_65 / total, 1)
        row["part_75_plus"] = round(100 * sum(slot[75:]) / total, 1)
        row["age_median"] = median_age(slot, total)
        # Blank, not a number, where the denominator is too small to carry one.
        row["indice_vieillissement"] = (
            round(100 * over_65 / under_20, 1) if under_20 >= min_u20 else None
        )
        rows.append(row)

    if mismatched:
        Log.warn(f"{mismatched:,} commune(s) where the published total and the age sum disagreed by >1%")

    df = pd.DataFrame(rows).sort_values("code_insee")
    for col in ("population_rp", *bands):
        df[col] = df[col].astype("Int64")

    national = df["population_rp"].sum()
    Log.info(f"{len(df):,} communes · {national:,} inhabitants")
    Log.info(f"  median age        national median commune {df['age_median'].median():.1f} yr "
             f"· decile range {df['age_median'].quantile(0.1):.1f}–{df['age_median'].quantile(0.9):.1f}")
    # Population-weighted, so this is the national figure rather than the
    # median commune's — the two differ by four points, because the communes
    # with the oldest populations are the ones with the fewest people in them.
    weighted_65 = (df["part_65_plus"] / 100 * df["population_rp"]).sum() / national * 100
    Log.info(f"  % 65 and over     median commune {df['part_65_plus'].median():.1f}% "
             f"· nationally {weighted_65:.1f}% of people")
    have = int(df["indice_vieillissement"].notna().sum())
    Log.info(f"  ageing ratio      published for {have:,} communes ({have / len(df):.1%}); "
             f"the rest have fewer than {min_u20} under-20s")
    Log.info(f"  oldest commune    median age {df['age_median'].max():.1f} · "
             f"youngest {df['age_median'].min():.1f}")

    df.to_csv(ctx.out_path, index=False)
