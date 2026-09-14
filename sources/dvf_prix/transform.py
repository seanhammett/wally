"""Property prices per commune → data/processed/dvf_prix.csv.

DVF ships one row per parcel and per lot, with the mutation's total value
repeated on every one of them. Everything here follows from that: group by
mutation first, decide what the mutation actually sold, and only then divide.
"""
from __future__ import annotations

import csv
import gzip
import io
import statistics
from collections import defaultdict

import pandas as pd

from pipeline.common import BuildError, Log, is_metropolitan, normalise_insee


class Mutation:
    """One sale, accumulated across however many rows DVF splits it into."""

    __slots__ = ("value", "communes", "types", "surface")

    def __init__(self, value: float):
        self.value = value
        self.communes = set()
        self.types = set()
        self.surface = 0.0

    def price_per_m2(self):
        return self.value / self.surface if self.surface else None

    def resolves(self, wanted_types: set, min_surface: float):
        """A mutation is usable only if it is one dwelling type in one commune.

        A sale bundling a house, a barn and three fields has a single value and
        no surface it can honestly be divided by.
        """
        if self.value <= 0 or len(self.communes) != 1 or len(self.types) != 1:
            return None
        kind = next(iter(self.types))
        if kind not in wanted_types or self.surface < min_surface:
            return None
        return next(iter(self.communes)), kind


def read_year(path, nature: str, mutations: dict) -> int:
    rows = 0
    with gzip.open(path, "rb") as raw:
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
        for row in reader:
            if row.get("nature_mutation") != nature:
                continue
            rows += 1
            # id_mutation restarts each year, so scope the key by file.
            key = (path.parent.name, row["id_mutation"])
            mut = mutations.get(key)
            if mut is None:
                try:
                    value = float(row.get("valeur_fonciere") or 0)
                except ValueError:
                    value = 0.0
                mut = mutations[key] = Mutation(value)
            mut.communes.add(row.get("code_commune") or "")
            kind = row.get("type_local") or ""
            if kind:
                mut.types.add(kind)
                try:
                    mut.surface += float(row.get("surface_reelle_bati") or 0)
                except ValueError:
                    pass
    return rows


def arrondissement_map(spec: dict) -> dict:
    """Expand the declared inclusive ranges into arrondissement → parent commune."""
    lookup = {}
    for parent, (first, last) in spec.items():
        for code in range(int(first), int(last) + 1):
            lookup[str(code)] = parent
    return lookup


def transform(ctx) -> None:
    cfg = ctx.meta["dvf"]
    flt = ctx.meta["filters"]
    wanted_types = set(flt["types"])
    excluded = set(ctx.meta["excluded_departments"])
    arrondissements = arrondissement_map(ctx.meta["arrondissements"])

    mutations: dict = {}
    total_rows = 0
    for year in cfg["years"]:
        year_dir = ctx.raw_dir / str(year)
        files = sorted(year_dir.glob("*.csv.gz"))
        if not files:
            raise BuildError(f"{year_dir} has no department files — run the fetch stage first")
        for path in files:
            total_rows += read_year(path, flt["nature"], mutations)
        Log.info(f"{year}: {len(files)} departments read")
    Log.info(f"{total_rows:,} sale rows collapsed into {len(mutations):,} mutations")

    prices: dict = defaultdict(lambda: {"Maison": [], "Appartement": [], "total": []})
    dropped = 0
    for mut in mutations.values():
        resolved = mut.resolves(wanted_types, flt["min_surface_m2"])
        if resolved is None:
            dropped += 1
            continue
        code_raw, kind = resolved
        # Roll Paris/Lyon/Marseille arrondissements up to the parent commune,
        # which is the only one the geometry has.
        code_raw = arrondissements.get(code_raw, code_raw)
        ppm = mut.price_per_m2()
        if ppm is None or not (flt["min_eur_m2"] <= ppm <= flt["max_eur_m2"]):
            dropped += 1
            continue
        code = normalise_insee(code_raw)
        if code is None or not is_metropolitan(code):
            dropped += 1
            continue
        bucket = prices[code]
        bucket[kind].append((ppm, mut.value))
        bucket["total"].append(ppm)

    Log.info(f"{dropped:,} mutations dropped (mixed lots, bare land, outliers) · "
             f"{len(mutations) - dropped:,} usable")

    min_sales = int(flt["min_sales"])
    rows = []
    for code in sorted(prices):
        b = prices[code]
        houses = [p for p, _ in b["Maison"]]
        flats = [p for p, _ in b["Appartement"]]
        house_values = [v for _, v in b["Maison"]]
        rows.append(
            {
                "code_insee": code,
                "prix_m2": _median(b["total"], min_sales, 0),
                "prix_m2_maison": _median(houses, min_sales, 0),
                "prix_m2_appart": _median(flats, min_sales, 0),
                "prix_maison_median": _median(house_values, min_sales, -2),
                "ventes_nb": len(b["total"]),
            }
        )

    df = pd.DataFrame(rows).sort_values("code_insee")
    mapped = int(df["prix_m2"].notna().sum())
    Log.info(f"{len(df):,} communes with a sale · {mapped:,} with at least {min_sales} "
             f"({mapped / len(df):.1%}) get a published median")
    for col in ("prix_m2", "prix_m2_maison", "prix_m2_appart"):
        have = df[col].dropna()
        if len(have):
            Log.info(f"  {col:<18} {len(have):>6,} communes · median €{have.median():,.0f}/m²")
    for parent in sorted(ctx.meta["arrondissements"]):
        row = df[df["code_insee"] == parent]
        if row.empty or pd.isna(row.iloc[0]["prix_m2"]):
            raise BuildError(
                f"commune {parent} has no price after the arrondissement rollup — "
                "Paris, Lyon and Marseille must not be blank"
            )
        Log.info(f"  rolled up {parent}: €{int(row.iloc[0]['prix_m2']):,}/m² "
                 f"from {int(row.iloc[0]['ventes_nb']):,} sales")
    Log.warn(f"departments {', '.join(sorted(excluded))} are absent by design (livre foncier)")
    df.to_csv(ctx.out_path, index=False)


def _median(values: list, min_sales: int, places: int):
    """Blank rather than a median drawn from a handful of sales."""
    if len(values) < min_sales:
        return None
    med = statistics.median(values)
    return round(med, places) if places > 0 else int(round(med, places))
