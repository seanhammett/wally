#!/usr/bin/env python3
"""Rebuild the entire static site from data/raw.

    python pipeline/build.py                 # fetch, transform, validate, join, tile, manifest
    python pipeline/build.py --skip-fetch    # rebuild from what is already in data/raw
    python pipeline/build.py --only pnr      # one source (plus the commune geometry it needs)

Order matters: transform → validate → join → tile. Nothing is tiled until it has
passed validation, so a bad transform can never reach the map.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from pipeline import stats as statistics
from pipeline import tile as tiling
from pipeline import validate as validation
from pipeline.common import (
    PROCESSED_DIR,
    RAW_DIR,
    ROOT,
    SITE_DIR,
    TILES_DIR,
    BuildError,
    Log,
    ManualFetchRequired,
    Source,
    human_bytes,
    load_sources,
    normalise_insee,
    scratch_dir,
)

COMMUNE_SOURCE = "communes"
COMMUNE_TILESET = "communes"
JOINED_PATH = PROCESSED_DIR / "communes_joined.geojson"


@dataclass
class Ctx:
    """What a source's fetch.py / transform.py is handed."""
    source: Source
    raw_dir: Path
    out_path: Path
    scratch: Path
    files_dir: Path | None = None   # set when source.yaml declares site_files
    log = Log

    @property
    def meta(self) -> dict[str, Any]:
        return self.source.meta

    def communes_geojson(self) -> Path:
        path = PROCESSED_DIR / f"{COMMUNE_SOURCE}.geojson"
        if not path.exists():
            raise BuildError(
                f"{self.source.id} needs the commune geometry, which has not been built yet. "
                f"Run the build without --only, or include '{COMMUNE_SOURCE}'."
            )
        return path

    def processed(self, source_id: str) -> Path:
        """Another source's data/processed output, for a source that declares it in `inputs`."""
        if source_id not in self.source.meta.get("inputs", []):
            raise BuildError(f"{self.source.id} reads '{source_id}' but does not list it in source.yaml inputs")
        path = load_sources([source_id])[0].output_path
        if not path.exists():
            raise BuildError(f"{self.source.id} needs the output of '{source_id}', which has not been built yet "
                             f"({path.relative_to(ROOT)})")
        return path


def make_ctx(source: Source) -> Ctx:
    source.raw_dir.mkdir(parents=True, exist_ok=True)
    return Ctx(
        source=source,
        raw_dir=source.raw_dir,
        out_path=source.output_path,
        scratch=scratch_dir(source.id),
        files_dir=source.files_dir,
    )


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------
def stage_fetch(source: Source, ctx: Ctx, *, force: bool) -> None:
    if source.fetch_mode == "none":
        Log.info("nothing to fetch (remote or metadata-only source)")
        return
    mod = source.script("fetch")
    if mod is None or not hasattr(mod, "fetch"):
        raise BuildError(f"source '{source.id}' has no fetch.py with a fetch(ctx) function")
    mod.fetch(ctx, force=force) if _accepts_force(mod.fetch) else mod.fetch(ctx)


def _accepts_force(fn) -> bool:
    import inspect

    return "force" in inspect.signature(fn).parameters


def stage_transform(source: Source, ctx: Ctx) -> None:
    if not source.produces_output:
        Log.info("no transform (layer points at a remote tileset)")
        return
    mod = source.script("transform")
    if mod is None or not hasattr(mod, "transform"):
        raise BuildError(f"source '{source.id}' has no transform.py with a transform(ctx) function")
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    mod.transform(ctx)
    if not ctx.out_path.exists():
        raise BuildError(f"{source.id}: transform.py did not write {ctx.out_path.relative_to(ROOT)}")
    if ctx.files_dir is not None and not (ctx.files_dir / "index.json").exists():
        raise BuildError(f"{source.id}: declares site_files but transform.py did not write "
                         f"{(ctx.files_dir / 'index.json').relative_to(ROOT)}")
    Log.ok(f"wrote {ctx.out_path.relative_to(ROOT)} ({human_bytes(ctx.out_path.stat().st_size)})")


def stage_join(sources: list[Source]) -> tuple[Path, list[dict]] | None:
    """Merge every commune table into the commune geometry, once, at build time.

    Runtime setFeatureState would avoid this, but re-tiling 35,000 communes takes
    seconds and a single joined tileset is far simpler to reason about.

    Returns the joined file and the features themselves: the correlator's columns
    are cut from the same in-memory table, so they cannot disagree with the tiles.
    """
    geom_path = PROCESSED_DIR / f"{COMMUNE_SOURCE}.geojson"
    if not geom_path.exists():
        Log.warn("no commune geometry; skipping join")
        return None

    tables = [s for s in sources if s.kind == "commune_table" and s.id != COMMUNE_SOURCE and s.output_path.exists()]
    data = json.loads(geom_path.read_text(encoding="utf-8"))
    features = data["features"]
    by_code = {f["properties"]["code_insee"]: f for f in features}
    Log.info(f"{len(by_code):,} commune geometries")

    for src in tables:
        df = pd.read_csv(src.output_path, dtype=str, keep_default_na=False, na_values=[""])
        df["code_insee"] = df["code_insee"].map(normalise_insee)
        field_types = {f["name"]: f.get("type") for layer in src.layers for f in layer.get("fields", [])}

        matched = 0
        for row in df.to_dict("records"):
            code = row.pop("code_insee", None)
            feat = by_code.get(code)
            if feat is None:
                continue
            matched += 1
            props = feat["properties"]
            for key, value in row.items():
                if value is None or (isinstance(value, float) and pd.isna(value)):
                    continue
                if field_types.get(key) == "numeric":
                    try:
                        num = float(value)
                        props[key] = int(num) if num.is_integer() else round(num, 4)
                    except (TypeError, ValueError):
                        continue
                else:
                    props[key] = value
        rate = matched / max(1, len(df))
        line = f"{src.id}: {matched:,}/{len(df):,} rows joined ({rate:.2%})"
        (Log.ok if rate >= 0.95 else Log.warn)(line)

    JOINED_PATH.write_text(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False), encoding="utf-8")
    Log.ok(f"wrote {JOINED_PATH.relative_to(ROOT)} ({human_bytes(JOINED_PATH.stat().st_size)})")
    return JOINED_PATH, features


def stage_tiles(sources: list[Source], joined: Path | None) -> dict[str, dict]:
    """Publish every geometry output into site/tiles/. Returns id → manifest fragment."""
    published: dict[str, dict] = {}

    if joined is not None:
        opts = next((s.meta.get("tiling", {}) for s in sources if s.id == COMMUNE_SOURCE), {})
        opts = {"tiles": "always", "min_zoom": 4, "max_zoom": 11, **opts}
        Log.step(f"tile {COMMUNE_TILESET} (all commune choropleths share this tileset)")
        Log.indent()
        published[COMMUNE_TILESET] = tiling.publish(joined, COMMUNE_TILESET, layer_name=COMMUNE_TILESET, options=opts)
        Log.dedent()

    communes = PROCESSED_DIR / f"{COMMUNE_SOURCE}.geojson"
    if communes.exists():
        Log.step("France outline (the 'No map' basemap)")
        Log.indent()
        tiling.country_outline(communes, TILES_DIR / "france_outline.geojson")
        if joined is not None:
            tiling.country_cities(joined, TILES_DIR / "france_cities.geojson")
        Log.dedent()

    for src in sources:
        # The commune geometry is published as the joined tileset above, not on its own.
        if src.id == COMMUNE_SOURCE or src.kind == "commune_table":
            continue
        if not src.produces_output or not src.output_path.exists():
            continue
        Log.step(f"tile {src.id}")
        Log.indent()
        published[src.id] = tiling.publish(src.output_path, src.id, options=src.meta.get("tiling", {}))
        Log.dedent()
    return published


def stage_files(sources: list[Source]) -> dict[str, dict]:
    """Copy each source's static files to site/files/<site_files>/. Returns id → manifest fragment.

    For data a page fetches on demand rather than draws: too much to carry in
    every tile, and only wanted for one place at a time.
    """
    published: dict[str, dict] = {}
    root = SITE_DIR / "files"
    for src in sources:
        folder = src.files_dir
        if folder is None:
            continue
        if not (folder / "index.json").exists():
            Log.warn(f"{src.id}: no {folder.relative_to(ROOT)}/index.json yet; its files are not published")
            continue
        name = str(src.meta["site_files"])
        dest = root / name
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(folder, dest)
        size = sum(p.stat().st_size for p in dest.iterdir())
        Log.ok(f"{src.id}: site/files/{name}/ ({len(list(dest.iterdir()))} files, {human_bytes(size)})")
        published[src.id] = {"dir": f"files/{name}", "index": f"files/{name}/index.json",
                             "attribution": src.attribution}
    return published


# Draw order by layer type. Choropleths are opaque and belong at the bottom;
# outlines and points must stay legible above whatever fill is switched on.
# A layer.json can override with an explicit "z".
DEFAULT_Z = {"choropleth": 10, "polygon": 20, "line": 30, "point": 40}


def stage_manifest(sources: list[Source], published: dict[str, dict], skipped: list[str],
                   stats: dict | None = None, files: dict | None = None) -> Path:
    """Concatenate every layer.json into site/layers.json. Manifest order = draw order."""
    layers: list[dict] = []
    for src in sources:
        if src.id in skipped:
            continue
        for layer in src.layers:
            entry = dict(layer)
            entry["source_id"] = src.id
            entry["attribution"] = src.attribution
            entry["source_name"] = src.name
            entry["source_url"] = src.meta.get("url", "")
            entry["fetched"] = str(src.meta.get("fetched", ""))
            entry.setdefault("default_visible", False)
            entry.setdefault("default_opacity", 0.75)

            if entry.get("source_url_tiles"):
                entry["source_file"] = entry.pop("source_url_tiles")
                entry["remote"] = True
            else:
                key = COMMUNE_TILESET if entry["type"] == "choropleth" else src.id
                info = published.get(key)
                if info is None:
                    Log.warn(f"layer '{entry['id']}' has no tiles (source {key} produced nothing); skipping")
                    continue
                entry["source_file"] = info["source_file"]
                entry["format"] = info["format"]
                if info.get("source_layer"):
                    entry["source_layer"] = info["source_layer"]
                if info.get("source_parts"):
                    entry["source_parts"] = info["source_parts"]
            entry.setdefault("z", DEFAULT_Z.get(entry["type"], 20))
            layers.append(entry)

    # Stable sort: within one z, sources keep their source.yaml order.
    layers.sort(key=lambda e: e["z"])

    manifest = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "basemaps": json.loads((ROOT / "pipeline" / "basemaps.json").read_text(encoding="utf-8")),
        "overlays": json.loads((ROOT / "pipeline" / "overlays.json").read_text(encoding="utf-8")),
        # The view a visitor with no link lands on, and the layers listed first.
        "start": json.loads((ROOT / "pipeline" / "start.json").read_text(encoding="utf-8")),
        "layers": layers,
        "stats": stats or {"index": None, "count": 0, "fields": []},
        "files": files or {},
    }
    out = SITE_DIR / "layers.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    Log.ok(f"wrote {out.relative_to(ROOT)} — {len(layers)} layer(s), "
           f"{len((stats or {}).get('fields', []))} correlatable stat(s)")
    return out


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Rebuild site/ from data/raw.")
    ap.add_argument("--only", nargs="*", help="build only these source ids")
    ap.add_argument("--skip-fetch", action="store_true", help="use what is already in data/raw")
    ap.add_argument("--skip-tiles", action="store_true", help="stop after validation")
    ap.add_argument("--force-fetch", action="store_true", help="re-download even if cached")
    args = ap.parse_args()

    started = time.time()
    try:
        all_sources = load_sources(None)
        targets = load_sources(args.only) if args.only else all_sources
    except BuildError as exc:
        Log.error(str(exc))
        return 2

    # --only restricts what is re-fetched and re-transformed. Everything
    # downstream — validation, the commune join, tiling, the manifest — always
    # runs over every source that has an output, or a partial run would silently
    # publish a tileset and a manifest missing all the other layers.
    #
    # A source built from other sources' outputs (`inputs:` in source.yaml) is
    # re-run whenever one of its inputs is, so it can never be left describing
    # an older version of them.
    if args.only:
        chosen = {s.id for s in targets}
        grew = True
        while grew:
            extra = [s for s in all_sources if s.id not in chosen and chosen & set(s.meta.get("inputs", []))]
            chosen |= {s.id for s in extra}
            grew = bool(extra)
        if len(chosen) > len(targets):
            Log.info(f"--only: also re-running {', '.join(sorted(chosen - {s.id for s in targets}))}, "
                     "which read(s) their output")
        targets = [s for s in all_sources if s.id in chosen]
        Log.info(f"--only: re-running {', '.join(s.id for s in targets)}; "
                 "other sources reuse their existing data/processed output")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    skipped: list[str] = []
    manual_notes: list[str] = []
    hard_failures: list[str] = []

    for src in targets:
        Log.step(f"{src.id} — {src.name}")
        Log.indent()
        ctx = make_ctx(src)
        try:
            failed_inputs = sorted(set(src.meta.get("inputs", [])) & set(skipped))
            if failed_inputs:
                raise BuildError(f"not rebuilt: its input(s) {', '.join(failed_inputs)} failed in this run")
            if not args.skip_fetch:
                stage_fetch(src, ctx, force=args.force_fetch)
            stage_transform(src, ctx)
        except ManualFetchRequired as exc:
            Log.warn(str(exc))
            manual_notes.append(str(exc))
            skipped.append(src.id)
            if not src.meta.get("optional", False):
                hard_failures.append(f"{src.id}: required manual download is missing")
        except BuildError as exc:
            Log.error(str(exc))
            skipped.append(src.id)
            hard_failures.append(f"{src.id}: {exc}")
        except Exception as exc:  # a source's own bug must not take the build down
            import traceback

            Log.error(f"{type(exc).__name__}: {exc}")
            for line in traceback.format_exc().strip().splitlines()[-4:]:
                Log.info(line)
            skipped.append(src.id)
            hard_failures.append(f"{src.id}: {type(exc).__name__}: {exc}")
        finally:
            Log.dedent()

    active = [
        s
        for s in all_sources
        if s.id not in skipped and (not s.produces_output or s.output_path.exists())
    ]
    missing = [s.id for s in all_sources if s not in active and s.id not in skipped]
    if missing:
        Log.info(f"no output yet, excluded from this build: {', '.join(missing)}")

    print()
    Log.step("validate")
    Log.indent()
    report = validation.run(active)
    Log.dedent()
    if not report.ok:
        print()
        Log.error(f"{len(report.errors)} validation error(s); nothing was tiled")
        for e in report.errors:
            Log.error(e)
        return 1

    if args.skip_tiles:
        Log.ok("validation passed (--skip-tiles, stopping here)")
        return 0

    print()
    Log.step("join commune tables into the commune geometry")
    Log.indent()
    joined = stage_join(active)
    Log.dedent()
    joined_path = joined[0] if joined else None

    print()
    Log.step("commune stat columns (the correlator reads these, not the tiles)")
    Log.indent()
    stats = statistics.build(joined[1], active) if joined else None
    if stats is None:
        Log.warn("no joined commune table; the correlator will be unavailable")
    Log.dedent()

    print()
    published = stage_tiles(active, joined_path)

    print()
    Log.step("static files")
    Log.indent()
    files = stage_files(active)
    Log.dedent()

    print()
    Log.step("manifest")
    Log.indent()
    stage_manifest(active, published, skipped, stats, files)
    Log.dedent()

    print()
    if manual_notes:
        Log.step("manual downloads still outstanding")
        for note in manual_notes:
            Log.warn(note)
    if hard_failures:
        Log.step("\033[31mfailures\033[0m")
        for f in hard_failures:
            Log.error(f)
        return 1

    total = sum(p.stat().st_size for p in TILES_DIR.glob("*") if p.is_file())
    Log.step(
        f"\033[32mbuild complete in {time.time() - started:.1f}s\033[0m — "
        f"{len(active)} source(s), site/tiles is {human_bytes(total)}"
    )
    Log.info("serve with:  python pipeline/serve.py --port 8000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
