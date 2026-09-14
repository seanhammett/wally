"""Per-department GeoJSON → one canonical commune layer in EPSG:4326.

Only identity attributes survive here (code, name, department). Every indicator
is attached later, at the join step, so this file stays the one place the
canonical geometry is defined.
"""
import json

from shapely.geometry import mapping, shape
from shapely.validation import make_valid

from pipeline.common import Log, human_bytes, is_metropolitan, normalise_insee, write_geojson
from pipeline.tile import simplify


def transform(ctx) -> None:
    files = sorted(ctx.raw_dir.glob("communes-*.geojson"))
    if not files:
        raise RuntimeError("no department files in data/raw/communes — run fetch first")

    features = []
    dropped_overseas = 0
    bad_codes = 0
    repaired = 0

    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        for feat in data.get("features", []):
            props = feat.get("properties") or {}
            code = normalise_insee(props.get("code"))
            if code is None:
                bad_codes += 1
                continue
            if not is_metropolitan(code):
                dropped_overseas += 1
                continue
            geom = feat.get("geometry")
            if not geom:
                continue
            g = shape(geom)
            if not g.is_valid:
                # Self-intersections in administrative boundaries are common and
                # will crash tippecanoe. Repair unconditionally.
                g = make_valid(g)
                repaired += 1
            if g.is_empty:
                continue
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "code_insee": code,
                        "nom": props.get("nom"),
                        "dep": props.get("codeDepartement"),
                        "reg": props.get("codeRegion"),
                    },
                    "geometry": mapping(g),
                }
            )

    features.sort(key=lambda f: f["properties"]["code_insee"])
    Log.info(f"{len(features):,} communes · {repaired} geometr(ies) repaired · {dropped_overseas} overseas dropped")
    if bad_codes:
        Log.warn(f"{bad_codes} feature(s) had an unusable INSEE code and were dropped")

    # ADMIN EXPRESS at full resolution is ~150 MB of GeoJSON — far more detail than
    # a national web map can draw. Simplify once, here, with mapshaper: it is
    # topology-aware, so neighbouring communes keep their shared edge and no
    # slivers open up between them. data/raw keeps the full-resolution original,
    # so changing this percentage is a re-run, not a re-download.
    full = ctx.scratch / "communes-full.geojson"
    write_geojson(features, full)
    Log.info(f"full resolution: {human_bytes(full.stat().st_size)}")
    simplify(full, ctx.out_path, ctx.meta.get("simplify", "15%"))
