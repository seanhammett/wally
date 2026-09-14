"""Flood and coastal exposure per commune → data/processed/georisques_gaspar.csv.

Reads four members of the GASPAR zip and reduces each to one row per commune:
how often flooding has been declared a natural disaster, whether a flood or
coastal PPR exists and how far it has got, and which coastal hazards the
préfecture register lists.
"""
from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict

import pandas as pd

from pipeline.common import BuildError, Log, is_metropolitan, normalise_insee

# PPR strength, strongest first. A commune often holds several procedures for
# the same risk; only the strongest one reaches the map.
PPR_RANK = {"approuve": 3, "prescrit": 2, "caduque": 1, "aucun": 0}


def ppr_state(row: dict) -> str | None:
    """Collapse GASPAR's état/sous-état pair into one of three words.

    `LIBELLE ETAT` is the lifecycle stage and `LIBELLE SOUS-ETAT` the detail;
    either can be blank, so both are consulted before giving up on a row.
    """
    etat = (row.get("LIBELLE ETAT") or "").strip()
    sous = (row.get("LIBELLE SOUS-ETAT") or "").strip()
    if etat == "Opposable" or sous == "Approuvé":
        return "approuve"
    if etat == "Prescrit" or sous in ("Prescrit", "Prorogé", "Anticipé"):
        return "prescrit"
    if etat == "Caduque" or sous in ("Abrogé", "Annulé", "Déprescrit"):
        return "caduque"
    return None


def strongest(current: str, candidate: str) -> str:
    return candidate if PPR_RANK[candidate] > PPR_RANK[current] else current


def member(zf: zipfile.ZipFile, prefix: str) -> str:
    """Resolve a datestamped member name from its stable prefix."""
    names = [n for n in zf.namelist() if n.startswith(prefix) and n.endswith(".csv")]
    if not names:
        raise BuildError(
            f"gaspar.zip has no member starting '{prefix}' — "
            f"found {', '.join(sorted(zf.namelist()))}"
        )
    return sorted(names)[-1]


def rows(zf: zipfile.ZipFile, name: str):
    """GASPAR ships UTF-8 with ';' separators, but a few free-text fields carry
    stray bytes; replacing them is safer than failing a 250,000-row file."""
    with zf.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
        yield from csv.DictReader(text, delimiter=";")


def commune(value) -> str | None:
    code = normalise_insee(value)
    return code if code and is_metropolitan(code) else None


def transform(ctx) -> None:
    zip_path = ctx.raw_dir / ctx.meta["resource"]["filename"]
    members = ctx.meta["members"]
    flood_labels = set(ctx.meta["catnat_flood"])
    nums = ctx.meta["risk_numbers"]
    labels = ctx.meta["ppr_labels"]
    coastal_ppr_labels = set(labels["littoral"])

    catnat_n: dict[str, int] = defaultdict(int)
    catnat_year: dict[str, int] = {}
    ddrm: dict[str, set[str]] = defaultdict(set)
    ppr_flood: dict[str, str] = defaultdict(lambda: "aucun")
    ppr_coast: dict[str, str] = defaultdict(lambda: "aucun")
    azi: set[str] = set()

    with zipfile.ZipFile(zip_path) as zf:
        # --- arrêtés de catastrophe naturelle, flooding only -----------------
        total = 0
        for row in rows(zf, member(zf, members["catnat"])):
            if row.get("lib_risque_jo") not in flood_labels:
                continue
            code = commune(row.get("code_commune"))
            if code is None:
                continue
            catnat_n[code] += 1
            total += 1
            year = (row.get("date_debut") or "")[:4]
            if year.isdigit():
                catnat_year[code] = max(catnat_year.get(code, 0), int(year))
        Log.info(f"{total:,} flood CATNAT arrêtés across {len(catnat_n):,} communes")

        # --- préfecture risk register (DDRM) ---------------------------------
        for row in rows(zf, member(zf, members["ddrm"])):
            code = commune(row.get("cod_commune"))
            if code is not None:
                ddrm[code].add((row.get("num_risque") or "").strip())

        # --- PPR naturel: strongest procedure per risk family ----------------
        for row in rows(zf, member(zf, members["pprn"])):
            code = commune(row.get("CODE INSEE COMMUNE"))
            state = ppr_state(row)
            if code is None or state is None:
                continue
            if (row.get("LIBELLE RISQUE 2") or "").strip() == labels["inondation"]:
                ppr_flood[code] = strongest(ppr_flood[code], state)
            if (row.get("LIBELLE RISQUE 3") or "").strip() in coastal_ppr_labels:
                ppr_coast[code] = strongest(ppr_coast[code], state)

        # --- atlas des zones inondables --------------------------------------
        for row in rows(zf, member(zf, members["azi"])):
            code = commune(row.get("cod_commune"))
            if code is not None:
                azi.add(code)

    Log.info(
        f"{len(ddrm):,} communes in the DDRM register · "
        f"{len(ppr_flood):,} with a flood PPR · {len(azi):,} with a flood atlas"
    )

    codes = sorted(set(catnat_n) | set(ddrm) | set(ppr_flood) | set(ppr_coast) | azi)
    out = []
    for code in codes:
        risks = ddrm[code]
        submersion = nums["submersion"] in risks
        recul = nums["recul_trait_cote"] in risks
        if submersion and recul:
            littoral = "les_deux"
        elif submersion:
            littoral = "submersion"
        elif recul:
            littoral = "recul"
        else:
            littoral = "aucun"
        out.append(
            {
                "code_insee": code,
                "inond_catnat_nb": catnat_n.get(code, 0),
                "inond_catnat_annee": catnat_year.get(code, ""),
                "inond_ppr": ppr_flood[code],
                "inond_azi": "oui" if code in azi else "non",
                "inond_registre": "oui" if nums["inondation"] in risks else "non",
                "littoral_alea": littoral,
                "littoral_ppr": ppr_coast[code],
            }
        )

    df = pd.DataFrame(out).sort_values("code_insee")
    Log.info(f"{len(df):,} communes with at least one GASPAR record")
    Log.info(f"  flood CATNAT median {df['inond_catnat_nb'].median():.0f}, max {df['inond_catnat_nb'].max():,}")
    for col in ("inond_ppr", "littoral_alea"):
        parts = ", ".join(f"{k} {v:,}" for k, v in df[col].value_counts().items())
        Log.info(f"  {col}: {parts}")
    df.to_csv(ctx.out_path, index=False)
