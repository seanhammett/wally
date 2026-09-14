"""Passenger stations → data/processed/gares.geojson.

Two exports joined on the UIC code: the station reference supplies position and
SNCF's size segment, the footfall file supplies how many people use it.
"""
from __future__ import annotations

import collections
import json

from pipeline.common import METRO_BBOX, BuildError, Log, human_bytes, normalise_insee, write_geojson


def load(path):
    if not path.exists():
        raise BuildError(f"missing {path}; run fetch first")
    return json.loads(path.read_text(encoding="utf-8"))


def uic8(value) -> str | None:
    """Both files key on the UIC code, but only one of them zero-pads it."""
    if value is None:
        return None
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    text = "".join(ch for ch in text if ch.isdigit())
    if not text:
        return None
    return text.zfill(8)[-8:]



def best_segment(raw) -> str | None:
    """A handful of stations arrive with their segment repeated or merged.

    The export concatenates multiple reference rows, so a station can come back
    as "A;A" or "B;A". A is the most important of the grades, so the strongest
    one present is the station's real segment.
    """
    if not raw:
        return None
    parts = {p.strip().upper() for p in str(raw).split(";") if p.strip()}
    for grade in ("A", "B", "C"):
        if grade in parts:
            return grade
    return None


def transform(ctx) -> None:
    ex = ctx.meta["exports"]
    year = int(ctx.meta["annee"])
    prev = int(ctx.meta["annee_precedente"])

    gares = load(ctx.raw_dir / ex["gares"]["filename"])
    freq = load(ctx.raw_dir / ex["frequentation"]["filename"])
    Log.info(f"{len(gares):,} stations · {len(freq):,} footfall records")

    col_now, col_prev = f"total_voyageurs_{year}", f"total_voyageurs_{prev}"
    sample = freq[0] if freq else {}
    for col in (col_now, col_prev):
        if col not in sample:
            raise BuildError(
                f"the footfall export has no column '{col}'. Available year columns: "
                + ", ".join(sorted(k for k in sample if k.startswith("total_voyageurs_")))
                + " — update `annee` / `annee_precedente` in source.yaml."
            )

    by_uic = {}
    for row in freq:
        code = uic8(row.get("code_uic_complet"))
        if code:
            by_uic[code] = row

    minx, miny, maxx, maxy = METRO_BBOX
    features, no_pos, outside, matched = [], 0, 0, 0

    for g in gares:
        pos = g.get("position_geographique") or {}
        lon, lat = pos.get("lon"), pos.get("lat")
        if lon is None or lat is None:
            no_pos += 1
            continue
        if not (minx <= lon <= maxx and miny <= lat <= maxy):
            outside += 1
            continue

        code = uic8(g.get("codes_uic"))
        f = by_uic.get(code)
        if f:
            matched += 1

        def count(col):
            if not f:
                return None
            v = f.get(col)
            try:
                return int(float(v)) if v is not None else None
            except (TypeError, ValueError):
                return None

        now, before = count(col_now), count(col_prev)
        trend = None
        if now is not None and before:
            trend = round(100 * (now - before) / before, 1)

        features.append(
            {
                "type": "Feature",
                "properties": {
                    "nom": g.get("nom"),
                    "code_insee": normalise_insee(g.get("codeinsee")),
                    "code_uic": code,
                    "segment": best_segment(g.get("segment_drg")),
                    "voyageurs": now,
                    "voyageurs_2019": before,
                    "tendance_pct": trend,
                },
                "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
            }
        )

    if no_pos:
        Log.info(f"{no_pos:,} station(s) had no coordinates")
    if outside:
        Log.info(f"{outside:,} station(s) outside metropolitan France dropped")

    with_count = [f for f in features if f["properties"]["voyageurs"] is not None]
    Log.info(f"{len(features):,} stations · {matched:,} matched to footfall ({matched / len(features):.1%})")

    if with_count:
        counts = sorted((f["properties"]["voyageurs"] for f in with_count), reverse=True)
        total = sum(counts)
        top50 = sum(counts[:50])
        Log.info(f"  {total:,.0f} passengers in {year} across {len(with_count):,} stations")
        Log.info(f"  the 50 busiest carry {top50 / total:.1%} of all journeys")
        Log.info(f"  median station {counts[len(counts) // 2]:,} · quietest {counts[-1]:,}")
        busiest = max(with_count, key=lambda f: f["properties"]["voyageurs"])
        Log.info(f"  busiest: {busiest['properties']['nom']} ({busiest['properties']['voyageurs']:,})")

    seg = collections.Counter(f["properties"]["segment"] for f in features)
    for s, n in sorted(seg.items(), key=lambda kv: str(kv[0])):
        Log.info(f"  segment {str(s):<5} {n:>6,}")

    features.sort(key=lambda f: -(f["properties"]["voyageurs"] or 0))
    write_geojson(features, ctx.out_path)
    Log.info(f"output {human_bytes(ctx.out_path.stat().st_size)}")
