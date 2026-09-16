#!/usr/bin/env python3
"""Enforce the source output contract. Exits non-zero on any failure.

This is the whole safety net: nothing reaches the map that has not passed
through here. Failures are loud and specific — a silent 8% join failure is the
single most likely bug in a project like this, so join diagnostics are printed
for every commune table whether or not they pass.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from pipeline.common import (
    METRO_BBOX,
    PROCESSED_DIR,
    ROOT,
    BuildError,
    Log,
    Source,
    is_metropolitan,
    load_sources,
    normalise_insee,
)

COMMUNE_GEOMETRY_SOURCE = "communes"
VALUE_TYPES = {"numeric", "ordinal", "categorical"}
SCALE_TYPES = {"numeric", "continuous", "ordinal", "categorical", "none"}


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, source_id: str, msg: str) -> None:
        self.errors.append(f"[{source_id}] {msg}")
        Log.error(msg)

    def warn(self, source_id: str, msg: str) -> None:
        self.warnings.append(f"[{source_id}] {msg}")
        Log.warn(msg)

    @property
    def ok(self) -> bool:
        return not self.errors


# --------------------------------------------------------------------------
# Per-kind validators
# --------------------------------------------------------------------------
def declared_fields(source: Source) -> dict[str, dict]:
    """Every value column a source publishes, from its layer.json `fields` blocks."""
    fields: dict[str, dict] = {}
    for layer in source.layers:
        for f in layer.get("fields", []):
            fields[f["name"]] = f
    return fields


def validate_commune_table(source: Source, report: Report, communes: set[str] | None) -> pd.DataFrame | None:
    path = source.output_path
    if not path.exists():
        report.error(source.id, f"missing output {path.relative_to(ROOT)}")
        return None

    df = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
    if "code_insee" not in df.columns:
        report.error(source.id, "commune_table output has no 'code_insee' column")
        return None
    if len(df) == 0:
        report.error(source.id, "commune_table output has zero rows")
        return None

    # -- key integrity ---------------------------------------------------
    bad = df[df["code_insee"].map(lambda v: normalise_insee(v) is None)]
    if len(bad):
        sample = ", ".join(bad["code_insee"].head(5).astype(str))
        report.error(source.id, f"{len(bad)} row(s) with a malformed INSEE code (e.g. {sample})")
    unpadded = df[df["code_insee"].str.len() != 5]
    if len(unpadded):
        sample = ", ".join(unpadded["code_insee"].head(5).astype(str))
        report.error(source.id, f"{len(unpadded)} INSEE code(s) not 5 characters (e.g. {sample}) — zero-pad them")
    dupes = df[df["code_insee"].duplicated(keep=False)]
    if len(dupes):
        sample = ", ".join(sorted(dupes["code_insee"].unique())[:5])
        report.error(source.id, f"{dupes['code_insee'].nunique()} duplicated INSEE code(s) (e.g. {sample})")

    # -- value columns ---------------------------------------------------
    value_cols = [c for c in df.columns if c != "code_insee"]
    if not value_cols:
        report.error(source.id, "commune_table has a key but no value columns")
    fields = declared_fields(source)
    for col in value_cols:
        spec = fields.get(col)
        if spec is None:
            report.error(source.id, f"column '{col}' is not declared in layer.json fields[]")
            continue
        if spec.get("type") not in VALUE_TYPES:
            report.error(source.id, f"column '{col}' has type '{spec.get('type')}'; expected one of {sorted(VALUE_TYPES)}")
        if "unit" not in spec:
            report.error(source.id, f"column '{col}' declares no unit (use \"\" if genuinely unitless)")
        if spec.get("type") == "numeric":
            coerced = pd.to_numeric(df[col], errors="coerce")
            nonnull = df[col].notna()
            broken = int((coerced.isna() & nonnull).sum())
            if broken:
                report.error(source.id, f"column '{col}' is declared numeric but {broken} value(s) will not parse")
    for name in fields:
        if name not in value_cols:
            report.error(source.id, f"layer.json declares field '{name}' that the transform did not produce")

    if source.meta.get("via_shapefile"):
        long_names = [c for c in df.columns if len(c) > 10]
        if long_names:
            report.warn(source.id, f"shapefile-derived columns longer than 10 chars: {long_names} — rename explicitly")

    # -- join diagnostics ------------------------------------------------
    if communes is not None:
        codes = set(df["code_insee"].dropna())
        matched = codes & communes
        unmatched = sorted(codes - communes)
        rate = len(matched) / max(1, len(codes))
        Log.info(
            f"join: {len(df):,} rows · {len(matched):,} matched · {len(unmatched):,} unmatched "
            f"({rate:.2%} of source rows)"
        )
        coverage = len(matched) / max(1, len(communes))
        Log.info(f"      covers {coverage:.2%} of the {len(communes):,} commune geometries")
        if unmatched:
            Log.info(f"      unmatched sample: {', '.join(unmatched[:10])}")
        threshold = float(source.meta.get("min_join_rate", 0.95))
        if rate < threshold:
            report.error(
                source.id,
                f"join rate {rate:.2%} is below the required {threshold:.0%} — "
                "check the millésime of the source against the commune geometry",
            )
    return df


def validate_geojson(source: Source, report: Report) -> dict | None:
    path = source.output_path
    if not path.exists():
        report.error(source.id, f"missing output {path.relative_to(ROOT)}")
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.error(source.id, f"output is not valid JSON: {exc}")
        return None

    if data.get("type") != "FeatureCollection":
        report.error(source.id, "output is not a GeoJSON FeatureCollection")
        return None
    feats = data.get("features", [])
    if not feats:
        report.error(source.id, "FeatureCollection has zero features")
        return None

    # A named CRS member means someone skipped the reprojection.
    crs = data.get("crs")
    if crs and "4326" not in json.dumps(crs) and "CRS84" not in json.dumps(crs):
        report.error(source.id, f"output declares a non-4326 CRS: {crs}")

    # Substring match, so Multi* variants of each satisfy their own kind. A
    # source may declare more in `also_geometry` — rivieres carries its lakes as
    # polygons alongside the line network, drawn beneath it by the page.
    expected_geom = {"point": "Point", "line": "LineString"}.get(source.kind, "Polygon")
    allowed_geom = [expected_geom, *source.meta.get("also_geometry", [])]
    minx, miny, maxx, maxy = float("inf"), float("inf"), float("-inf"), float("-inf")
    invalid_geom = 0
    wrong_type = 0

    try:
        from shapely.geometry import shape

        have_shapely = True
    except ImportError:  # pragma: no cover - shapely is a hard dep in practice
        have_shapely = False
        report.warn(source.id, "shapely unavailable; skipping geometry validity check")

    for feat in feats:
        geom = feat.get("geometry")
        if not geom:
            invalid_geom += 1
            continue
        if not any(g in geom.get("type", "") for g in allowed_geom):
            wrong_type += 1
        for x, y in _iter_coords(geom.get("coordinates")):
            minx, miny = min(minx, x), min(miny, y)
            maxx, maxy = max(maxx, x), max(maxy, y)
        if have_shapely:
            try:
                if not shape(geom).is_valid:
                    invalid_geom += 1
            except Exception:
                invalid_geom += 1

    Log.info(f"{len(feats):,} features · bbox [{minx:.2f}, {miny:.2f}, {maxx:.2f}, {maxy:.2f}]")
    if wrong_type:
        report.error(source.id, f"{wrong_type} feature(s) are not {' or '.join(allowed_geom)} geometries")
    if invalid_geom:
        report.error(source.id, f"{invalid_geom} invalid geometr(ies) — apply .make_valid() in transform.py")

    # Longitude/latitude order and range. Lambert-93 metres would blow this instantly.
    if not (-180 <= minx <= 180 and -90 <= miny <= 90 and -180 <= maxx <= 180 and -90 <= maxy <= 90):
        report.error(source.id, "coordinates are outside the lon/lat range — output is not EPSG:4326 lon/lat")
    elif source.meta.get("bounds", "metropolitan") == "metropolitan":
        mminx, mminy, mmaxx, mmaxy = METRO_BBOX
        if minx < mminx or miny < mminy or maxx > mmaxx or maxy > mmaxy:
            report.error(
                source.id,
                f"bbox [{minx:.2f}, {miny:.2f}, {maxx:.2f}, {maxy:.2f}] escapes metropolitan France "
                f"{METRO_BBOX} — filter the overseas territories or set bounds: worldwide in source.yaml",
            )

    # Declared attribute fields must actually be present on the features.
    fields = declared_fields(source)
    present = set()
    for feat in feats[:2000]:
        present.update((feat.get("properties") or {}).keys())
    # A field only one geometry carries (a lake's area, after 230k river segments)
    # is looked for in the rest of the file before it counts as absent.
    if set(fields) - present:
        for feat in feats[2000:]:
            present.update((feat.get("properties") or {}).keys())
            if not set(fields) - present:
                break
    for name, spec in fields.items():
        if name not in present:
            report.error(source.id, f"layer.json declares field '{name}' absent from the features")
        elif spec.get("type") not in VALUE_TYPES:
            report.error(source.id, f"field '{name}' has type '{spec.get('type')}'; expected one of {sorted(VALUE_TYPES)}")
        elif "unit" not in spec:
            report.error(source.id, f"field '{name}' declares no unit (use \"\" if genuinely unitless)")
    return data


def _iter_coords(coords):
    if coords is None:
        return
    if isinstance(coords[0], (int, float)):
        yield coords[0], coords[1]
        return
    for c in coords:
        yield from _iter_coords(c)


# --------------------------------------------------------------------------
# Layer manifest validation
# --------------------------------------------------------------------------
def validate_layers(source: Source, report: Report) -> None:
    for layer in source.layers:
        for key in ("id", "label", "group", "type"):
            if key not in layer:
                report.error(source.id, f"layer.json entry missing '{key}'")
        if layer.get("type") not in {"choropleth", "polygon", "point", "line"}:
            report.error(source.id, f"layer '{layer.get('id')}' has unsupported type '{layer.get('type')}'")
        # source_file is resolved by build.py from what the source actually produced;
        # a layer only has to declare one when nothing local backs it.
        if layer.get("type") != "choropleth" and not source.produces_output and not layer.get("source_url_tiles"):
            report.error(
                source.id,
                f"layer '{layer.get('id')}' has no local output and no source_url_tiles to point at",
            )
        paint = layer.get("paint") or {}
        scale = paint.get("scale", "none")
        if scale not in SCALE_TYPES:
            report.error(source.id, f"layer '{layer.get('id')}' paint.scale '{scale}' is unsupported")
        if scale == "numeric":
            if not paint.get("breaks") or not paint.get("colors"):
                report.error(source.id, f"layer '{layer.get('id')}' numeric scale needs both breaks and colors")
            elif len(paint["colors"]) != len(paint["breaks"]) + 1:
                report.error(
                    source.id,
                    f"layer '{layer.get('id')}' has {len(paint['breaks'])} breaks but "
                    f"{len(paint['colors'])} colors; expected {len(paint['breaks']) + 1}",
                )
        if scale == "continuous":
            # [value, "#rrggbb"] pairs in rising order, interpolated between.
            stops = paint.get("stops") or []
            values = [st[0] for st in stops if isinstance(st, list) and len(st) >= 2]
            colors = [st[1] for st in stops if isinstance(st, list) and len(st) >= 2]
            if len(stops) < 2 or len(values) != len(stops):
                report.error(source.id, f"layer '{layer.get('id')}' continuous scale needs two or more [value, color] stops")
            elif any(b <= a for a, b in zip(values, values[1:])):
                report.error(source.id, f"layer '{layer.get('id')}' continuous stops must rise strictly")
            elif not all(isinstance(c, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", c) for c in colors):
                report.error(source.id, f"layer '{layer.get('id')}' continuous stop colors must be #rrggbb")
        if scale in {"ordinal", "categorical"} and not paint.get("stops"):
            report.error(source.id, f"layer '{layer.get('id')}' {scale} scale needs stops")
        if layer.get("ranked") and not (layer.get("uncertainty_note") or layer.get("class_band")):
            report.error(
                source.id,
                f"layer '{layer.get('id')}' is ranked but shows a bare rank — "
                "declare class_band or uncertainty_note",
            )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def run(sources: list[Source]) -> Report:
    report = Report()

    communes: set[str] | None = None
    geom_source = next((s for s in sources if s.id == COMMUNE_GEOMETRY_SOURCE), None)
    if geom_source and geom_source.output_path.exists():
        data = json.loads(geom_source.output_path.read_text(encoding="utf-8"))
        communes = {
            f["properties"]["code_insee"]
            for f in data["features"]
            if (f.get("properties") or {}).get("code_insee")
        }

    seen_columns: dict[str, str] = {}
    for source in sources:
        Log.step(f"validate {source.id} ({source.kind})")
        Log.indent()
        try:
            validate_layers(source, report)
            if not source.produces_output:
                Log.info("no local output (remote or metadata-only source)")
            elif not source.output_path.exists() and source.meta.get("optional"):
                # An optional manual source that has not been downloaded yet is a
                # documented gap, not a broken build.
                Log.info(f"optional source not built (no {source.output_path.name}); skipped")
            elif source.kind == "commune_table":
                df = validate_commune_table(source, report, communes if source.id != COMMUNE_GEOMETRY_SOURCE else None)
                if df is not None:
                    for col in df.columns:
                        if col == "code_insee":
                            continue
                        if col in seen_columns:
                            report.error(
                                source.id,
                                f"column '{col}' collides with source '{seen_columns[col]}'; "
                                "commune-table columns share one namespace and must be unique",
                            )
                        seen_columns[col] = source.id
            else:
                validate_geojson(source, report)
        finally:
            Log.dedent()

    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate processed source outputs against the contract.")
    ap.add_argument("--only", nargs="*", help="validate only these source ids")
    args = ap.parse_args()

    try:
        sources = load_sources(args.only)
    except BuildError as exc:
        Log.error(str(exc))
        return 2

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    report = run(sources)

    print()
    if report.warnings:
        Log.step(f"{len(report.warnings)} warning(s)")
        for w in report.warnings:
            Log.warn(w)
    if report.errors:
        Log.step(f"\033[31m{len(report.errors)} validation error(s)\033[0m")
        for e in report.errors:
            Log.error(e)
        return 1
    Log.step("\033[32mall sources conform to the output contract\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
