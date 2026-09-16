#!/usr/bin/env python3
"""Publish the commune value columns the correlator reads, plus a centroid index.

The choropleths render from `communes.pmtiles`, and a vector tileset is the wrong
place to compute from: tippecanoe drops features at low zoom, so anything derived
from what is on screen would be derived from a biased sample of France. The
correlator needs every commune every time, so the same joined table that feeds the
tiles is also published here as plain columns.

Layout, under site/stats/:

    index.json          codes, names, departments and centroids — one row order
                        that every column below is aligned to
    <field>.json        one column, aligned to index.json, null where the commune
                        has no value

One file per field so the page fetches exactly the two columns the user picked
(~150 KB each) instead of a single multi-megabyte table. Columns are only written
for `type: numeric` fields; a field can opt out with `"correlate": false` in its
layer.json, which is the right thing for a year or a grid resolution — numeric,
but not a statistic anyone should correlate.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from pipeline.common import ROOT, SITE_DIR, Log, Source, human_bytes

STATS_DIR = SITE_DIR / "stats"

# Enough for ~11 m, which is far finer than a commune and keeps index.json small.
COORD_DP = 4

# Below this a column is too sparse for a correlation anyone should read, so it
# is published but flagged, and the page warns before it draws a map from it.
THIN_COVERAGE = 0.25


def _ring_centroid(ring: list[Any]) -> tuple[float, float, float]:
    """Shoelace centroid of one ring, with its absolute area.

    Degrees are treated as a plane. Over a commune the distortion is far below
    the precision anything downstream asks of a centroid.
    """
    n = len(ring)
    if n < 3:
        xs = [p[0] for p in ring] or [0.0]
        ys = [p[1] for p in ring] or [0.0]
        return sum(xs) / len(xs), sum(ys) / len(ys), 0.0

    a = cx = cy = 0.0
    for i in range(n):
        x0, y0 = ring[i][0], ring[i][1]
        x1, y1 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        f = x0 * y1 - x1 * y0
        a += f
        cx += (x0 + x1) * f
        cy += (y0 + y1) * f
    if a == 0.0:  # degenerate ring (all points collinear) — fall back to the mean
        return sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n, 0.0
    a *= 0.5
    return cx / (6.0 * a), cy / (6.0 * a), abs(a)


def centroid(geometry: dict | None) -> tuple[float, float]:
    """Centre of the largest ring — where a commune reads as being.

    The largest ring rather than the whole multipolygon, so a commune with an
    offshore islet is placed on its mainland body and not in the water between
    the two.
    """
    if not geometry:
        return 0.0, 0.0
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    rings: list[list[Any]] = []
    if kind == "Polygon":
        rings = [r for r in coords if r]
    elif kind == "MultiPolygon":
        rings = [poly[0] for poly in coords if poly and poly[0]]
    if not rings:
        return 0.0, 0.0

    best = max((_ring_centroid(r) for r in rings), key=lambda c: c[2])
    return round(best[0], COORD_DP), round(best[1], COORD_DP)


def correlatable_fields(sources: Iterable[Source]) -> list[dict[str, Any]]:
    """Every numeric commune column, once, tagged with where it came from.

    A field can be declared by several layers (`serv_sante` is both its own layer
    and part of the services basket). The first declaration wins, which is the
    one whose layer is the field's natural home.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for src in sources:
        for layer in src.layers:
            if layer.get("type") != "choropleth":
                continue
            primary = (layer.get("paint") or {}).get("property")
            for f in layer.get("fields", []):
                name = f["name"]
                if f.get("type") != "numeric" or f.get("correlate") is False or name in seen:
                    continue
                seen.add(name)
                label = f.get("label") or name
                out.append({
                    "name": name,
                    "type": "numeric",
                    # The layer's own name reads better than the field's long
                    # gloss whenever the field *is* what the layer draws.
                    "label": layer["label"] if name == primary else label,
                    "description": label,
                    "unit": f.get("unit", ""),
                    "group": layer.get("group", "Other"),
                    "layer": layer["id"],
                    "source_name": src.name,
                    "attribution": src.attribution,
                    # The optimiser's absolute mode: the field's natural 0 and 1,
                    # or another column that holds the same thing on such a scale.
                    **{k: f[k] for k in ("abs", "abs_column") if k in f},
                })
    return out


def build(features: list[dict], sources: Iterable[Source]) -> dict[str, Any]:
    """Write site/stats/ and return the manifest fragment describing it."""
    sources = list(sources)
    STATS_DIR.mkdir(parents=True, exist_ok=True)
    for stale in STATS_DIR.glob("*.json"):
        stale.unlink()

    codes, names, deps, lons, lats = [], [], [], [], []
    for feat in features:
        props = feat.get("properties") or {}
        lon, lat = centroid(feat.get("geometry"))
        codes.append(props.get("code_insee", ""))
        names.append(props.get("nom", ""))
        deps.append(props.get("dep", ""))
        lons.append(lon)
        lats.append(lat)

    index = {"count": len(codes), "codes": codes, "names": names, "dep": deps, "lon": lons, "lat": lats}
    (STATS_DIR / "index.json").write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    Log.ok(f"index.json — {len(codes):,} communes, {human_bytes((STATS_DIR / 'index.json').stat().st_size)}")

    published: list[dict[str, Any]] = []
    thin: list[str] = []
    for spec in correlatable_fields(sources):
        name = spec["name"]
        column = [feat["properties"].get(name) for feat in features]
        column = [v if isinstance(v, (int, float)) and not isinstance(v, bool) else None for v in column]
        present = sum(1 for v in column if v is not None)
        if present == 0:
            Log.warn(f"{name}: no commune carries a value; not published")
            continue

        out = STATS_DIR / f"{name}.json"
        out.write_text(json.dumps(column, separators=(",", ":")), encoding="utf-8")
        coverage = present / max(1, len(column))
        if coverage < THIN_COVERAGE:
            thin.append(f"{name} ({coverage:.0%})")
        published.append({**spec, "file": f"stats/{name}.json", "coverage": present})

    total = sum(p.stat().st_size for p in STATS_DIR.glob("*.json"))
    Log.ok(f"{len(published)} correlatable column(s), site/stats is {human_bytes(total)}")
    if thin:
        Log.info(f"thin coverage, flagged in the UI: {', '.join(thin)}")

    return {"index": "stats/index.json", "count": len(codes), "fields": published}
