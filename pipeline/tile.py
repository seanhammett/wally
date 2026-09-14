#!/usr/bin/env python3
"""GeoJSON → PMTiles, plus mapshaper simplification.

tippecanoe (v2.17+) writes .pmtiles directly, so there is no separate convert
step. Simplification goes through mapshaper rather than Shapely: Shapely
simplifies each polygon independently and opens visible gaps between
neighbouring communes; mapshaper preserves shared edges.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.common import ROOT, TILES_DIR, BuildError, Log, human_bytes, scratch_dir

# Below this, a plain GeoJSON file is simpler and loads faster than a tileset.
TILE_THRESHOLD_FEATURES = 5000
TILE_THRESHOLD_BYTES = 4 * 1024 * 1024


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _run(cmd: list[str]) -> None:
    Log.info(" ".join(str(c) for c in cmd[:6]) + (" …" if len(cmd) > 6 else ""))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise BuildError(f"{cmd[0]} failed:\n    " + "\n    ".join(tail))


def simplify(
    src: Path,
    out: Path,
    percent: str = "10%",
    *,
    clean: bool = True,
    filter_islands: str | None = None,
) -> Path:
    """mapshaper -simplify keep-shapes. Topology-aware, so shared borders stay shared.

    `clean` is right for a mosaic of adjacent polygons (communes: it closes the
    slivers simplification opens along shared edges) and wrong for a layer whose
    polygons legitimately overlap — a park cœur inside its aire d'adhésion — where
    it treats every overlap as a sliver to resolve and the output grows instead of
    shrinking. Set clean=False there.
    """
    if not have("mapshaper"):
        Log.warn("mapshaper not installed; skipping simplification (npm i -g mapshaper)")
        shutil.copyfile(src, out)
        return out
    cmd = ["mapshaper", str(src), "-simplify", percent, "keep-shapes"]
    if filter_islands:
        cmd += ["-filter-islands", f"min-area={filter_islands}"]
    if clean:
        cmd += ["-clean"]
    cmd += ["-o", "format=geojson", "precision=0.000001", str(out)]
    _run(cmd)
    Log.ok(f"simplified {percent} → {human_bytes(out.stat().st_size)}")
    return out


def to_pmtiles(
    src: Path,
    out: Path,
    layer_name: str,
    *,
    min_zoom: int = 4,
    max_zoom: int = 11,
    extra: list[str] | None = None,
) -> Path:
    if not have("tippecanoe"):
        raise BuildError(
            "tippecanoe is not installed and is required to tile this layer.\n"
            "    brew install tippecanoe   (or see github.com/felt/tippecanoe)"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "tippecanoe",
        "-o", str(out),
        "--force",
        "-l", layer_name,
        "-Z", str(min_zoom),
        "-z", str(max_zoom),
        "--drop-densest-as-needed",
        "--extend-zooms-if-still-dropping",
        "--simplification=4",
        "--no-tile-size-limit",
        "--quiet",
    ]
    cmd += extra or []
    cmd.append(str(src))
    _run(cmd)
    Log.ok(f"tiled → {out.relative_to(ROOT)} ({human_bytes(out.stat().st_size)})")
    return out


def feature_count(path: Path) -> int:
    try:
        return len(json.loads(path.read_text(encoding="utf-8")).get("features", []))
    except Exception:
        return 0


def publish(src: Path, source_id: str, *, layer_name: str | None = None, options: dict | None = None) -> dict:
    """Put one processed GeoJSON into site/tiles/, tiling it only when worth it.

    Returns the manifest fragment describing what was written.
    """
    options = options or {}
    TILES_DIR.mkdir(parents=True, exist_ok=True)
    layer_name = layer_name or source_id
    n = feature_count(src)
    size = src.stat().st_size

    staged = src
    if options.get("simplify"):
        staged = simplify(
            src,
            scratch_dir("simplify") / f"{source_id}.geojson",
            options["simplify"],
            clean=options.get("clean", True),
        )

    force_tiles = options.get("tiles") == "always"
    plain = options.get("tiles") == "never" or (
        not force_tiles and n < TILE_THRESHOLD_FEATURES and size < TILE_THRESHOLD_BYTES
    )

    if plain:
        out = TILES_DIR / f"{source_id}.geojson"
        shutil.copyfile(staged, out)
        Log.ok(f"copied → {out.relative_to(ROOT)} ({human_bytes(out.stat().st_size)}, {n:,} features, no tiling needed)")
        return {"source_file": out.name, "format": "geojson", "features": n}

    out = TILES_DIR / f"{source_id}.pmtiles"
    to_pmtiles(
        staged,
        out,
        layer_name,
        min_zoom=int(options.get("min_zoom", 4)),
        max_zoom=int(options.get("max_zoom", 11)),
        extra=options.get("tippecanoe_args"),
    )
    return {"source_file": out.name, "format": "pmtiles", "source_layer": layer_name, "features": n}


def country_outline(src: Path, out: Path, percent: str = "5%") -> Path | None:
    """Dissolve the commune mosaic into one metropolitan-France shape.

    It backs the "No map" basemap: a coastline and land border to read the data
    against, with no tiles to fetch. Small islets are dropped; at the zooms the
    outline is useful they are specks, and they are most of the file.
    """
    if not have("mapshaper"):
        Log.warn("mapshaper not installed; the 'No map' basemap will have no outline (npm i -g mapshaper)")
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    _run([
        "mapshaper", str(src),
        "-dissolve2",
        "-simplify", percent, "keep-shapes",
        "-filter-islands", "min-area=1km2",
        "-o", "format=geojson", "geojson-type=FeatureCollection", "precision=0.00001", str(out),
    ])
    Log.ok(f"outline → {out.relative_to(ROOT)} ({human_bytes(out.stat().st_size)})")
    return out


def country_cities(src: Path, out: Path, min_population: int = 10_000) -> Path | None:
    """One point per commune of at least `min_population`, for the "No map" labels.

    Cut from the joined commune file, so the population is the same INSEE figure
    the choropleth shows. `-points inner` rather than a centroid: a centroid can
    fall outside a crescent-shaped commune, and the label would sit in the sea.
    Written largest first, which is the order the browser places labels in.
    """
    if not have("mapshaper"):
        Log.warn("mapshaper not installed; the 'No map' basemap will have no city labels")
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    _run([
        "mapshaper", str(src),
        "-filter", f"population >= {int(min_population)}",
        "-points", "inner",
        "-filter-fields", "nom,population",
        "-sort", "population", "descending",
        "-o", "format=geojson", "precision=0.0001", str(out),
    ])
    Log.ok(f"cities → {out.relative_to(ROOT)} ({human_bytes(out.stat().st_size)}, {feature_count(out):,} places)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Tile a GeoJSON file to PMTiles.")
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--layer", required=True)
    ap.add_argument("--min-zoom", type=int, default=4)
    ap.add_argument("--max-zoom", type=int, default=11)
    args = ap.parse_args()
    try:
        to_pmtiles(Path(args.input), Path(args.output), args.layer,
                   min_zoom=args.min_zoom, max_zoom=args.max_zoom)
    except BuildError as exc:
        Log.error(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
