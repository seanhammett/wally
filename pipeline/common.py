"""Shared plumbing for the France data overlay pipeline.

Everything here is deliberately dependency-light so that `fetch.py` scripts can
run without GeoPandas. Only the geometry helpers import it, and they do so lazily.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT / "sources"
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"
SITE_DIR = ROOT / "site"
TILES_DIR = SITE_DIR / "tiles"

# Metropolitan France, generously bounded (includes Corsica, excludes the DROM).
METRO_BBOX = (-5.4, 41.2, 9.8, 51.2)

KINDS = {"commune_table", "polygon", "point", "line"}
FETCH_MODES = {"auto", "manual", "none"}

INSEE_RE = re.compile(r"^(?:\d{5}|2[AB]\d{3})$")

USER_AGENT = "france-map-tool/1.0 (static map build pipeline)"


# --------------------------------------------------------------------------
# Logging. Plain, greppable, no dependencies.
# --------------------------------------------------------------------------
class Log:
    _indent = 0

    @classmethod
    def step(cls, msg: str) -> None:
        print(f"{'  ' * cls._indent}\033[1m▸ {msg}\033[0m", flush=True)

    @classmethod
    def info(cls, msg: str) -> None:
        print(f"{'  ' * cls._indent}  {msg}", flush=True)

    @classmethod
    def ok(cls, msg: str) -> None:
        print(f"{'  ' * cls._indent}  \033[32m✓\033[0m {msg}", flush=True)

    @classmethod
    def warn(cls, msg: str) -> None:
        print(f"{'  ' * cls._indent}  \033[33m!\033[0m {msg}", flush=True)

    @classmethod
    def error(cls, msg: str) -> None:
        print(f"{'  ' * cls._indent}  \033[31m✗\033[0m {msg}", flush=True)

    @classmethod
    def indent(cls, n: int = 1) -> None:
        cls._indent += n

    @classmethod
    def dedent(cls, n: int = 1) -> None:
        cls._indent = max(0, cls._indent - n)


class BuildError(RuntimeError):
    """Any failure that should stop the build with a readable message."""


class ManualFetchRequired(BuildError):
    """Raised when a `fetch: manual` source has no file in data/raw yet."""

    def __init__(self, source_id: str, filenames: Iterable[str], url: str, instructions: str = ""):
        self.source_id = source_id
        self.filenames = list(filenames)
        files = "\n".join(f"      {RAW_DIR / source_id / f}" for f in self.filenames)
        msg = (
            f"source '{source_id}' needs a manual download.\n"
            f"    Download from: {url}\n"
            f"    Save as:\n{files}"
        )
        if instructions:
            msg += f"\n    Notes: {instructions}"
        super().__init__(msg)


# --------------------------------------------------------------------------
# Source adapter contract
# --------------------------------------------------------------------------
@dataclass
class Source:
    id: str
    dir: Path
    meta: dict[str, Any]
    layers: list[dict[str, Any]] = field(default_factory=list)

    # -- convenience accessors -------------------------------------------
    @property
    def name(self) -> str:
        return self.meta.get("name", self.id)

    @property
    def kind(self) -> str:
        return self.meta["kind"]

    @property
    def fetch_mode(self) -> str:
        return self.meta.get("fetch", "auto")

    @property
    def order(self) -> int:
        return int(self.meta.get("order", 100))

    @property
    def enabled(self) -> bool:
        return bool(self.meta.get("enabled", True))

    @property
    def attribution(self) -> str:
        return self.meta.get("attribution", "")

    @property
    def raw_dir(self) -> Path:
        return RAW_DIR / self.id

    @property
    def output_path(self) -> Path:
        ext = "csv" if self.kind == "commune_table" else "geojson"
        return PROCESSED_DIR / f"{self.id}.{ext}"

    @property
    def files_dir(self) -> Path | None:
        """Static files a transform publishes beside the tiles (`site_files` in source.yaml)."""
        return PROCESSED_DIR / f"{self.id}_files" if self.meta.get("site_files") else None

    @property
    def produces_output(self) -> bool:
        """A source may be pure metadata (e.g. a layer pointing at a remote tileset)."""
        return self.kind in KINDS and not self.meta.get("no_output", False)

    def script(self, name: str):
        """Import `fetch.py` / `transform.py` from the source folder."""
        import importlib.util

        path = self.dir / f"{name}.py"
        if not path.exists():
            return None
        spec = importlib.util.spec_from_file_location(f"sources.{self.id}.{name}", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        return mod


def load_source(path: Path) -> Source:
    meta_path = path / "source.yaml"
    if not meta_path.exists():
        raise BuildError(f"{path} has no source.yaml")
    meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}

    missing = [k for k in ("id", "name", "attribution", "url", "kind") if k not in meta]
    if missing:
        raise BuildError(f"{meta_path} is missing required key(s): {', '.join(missing)}")
    if meta["id"] != path.name:
        raise BuildError(f"{meta_path}: id '{meta['id']}' does not match folder name '{path.name}'")
    if meta["kind"] not in KINDS:
        raise BuildError(f"{meta_path}: kind must be one of {sorted(KINDS)}")
    if meta.get("fetch", "auto") not in FETCH_MODES:
        raise BuildError(f"{meta_path}: fetch must be one of {sorted(FETCH_MODES)}")

    layers: list[dict[str, Any]] = []
    layer_path = path / "layer.json"
    if layer_path.exists():
        parsed = json.loads(layer_path.read_text(encoding="utf-8"))
        layers = parsed if isinstance(parsed, list) else [parsed]

    return Source(id=meta["id"], dir=path, meta=meta, layers=layers)


def load_sources(only: list[str] | None = None) -> list[Source]:
    sources = []
    for path in sorted(SOURCES_DIR.iterdir()):
        if not path.is_dir() or path.name.startswith((".", "_")):
            continue
        src = load_source(path)
        if not src.enabled:
            continue
        if only and src.id not in only:
            continue
        sources.append(src)
    sources.sort(key=lambda s: (s.order, s.id))
    if only:
        unknown = set(only) - {s.id for s in sources}
        if unknown:
            raise BuildError(f"unknown source id(s): {', '.join(sorted(unknown))}")
    return sources


# --------------------------------------------------------------------------
# Fetch helpers
# --------------------------------------------------------------------------
def _advertised_size(resp, resumed: bool) -> int | None:
    """Full size of the file behind a response, when the server states it.

    A compressed transfer encoding makes Content-Length describe the wire bytes
    rather than the file, so no check is possible and None is returned.
    """
    if resp.headers.get("Content-Encoding"):
        return None
    if resumed:
        match = re.search(r"/(\d+)$", resp.headers.get("Content-Range", ""))
        return int(match.group(1)) if match else None
    length = resp.headers.get("Content-Length")
    return int(length) if length and length.isdigit() else None


def download(url: str, dest: Path, *, force: bool = False, timeout: int = 300,
             headers: dict[str, str] | None = None) -> Path:
    """Download `url` to `dest`, skipping if already present.

    Raw downloads are immutable: once a file is in data/raw it is never rewritten
    unless the caller explicitly asks with force=True.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force:
        Log.info(f"cached {dest.relative_to(ROOT)} ({dest.stat().st_size:,} bytes)")
        return dest

    tmp = dest.with_suffix(dest.suffix + ".part")
    # A .part left by an earlier, killed run is of unknown provenance; start clean.
    tmp.unlink(missing_ok=True)
    Log.info(f"GET {url}")
    failures = 0
    expected: int | None = None
    while True:
        have = tmp.stat().st_size if tmp.exists() else 0
        hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
        if have:
            hdrs["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                # Resume only if the server honoured the range; a plain 200 is the whole file again.
                resumed = have and resp.status == 206
                total = _advertised_size(resp, resumed)
                if total is not None:
                    expected = total
                with tmp.open("ab" if resumed else "wb") as out:
                    shutil.copyfileobj(resp, out)
            # A connection the server closes early can end the read without any
            # exception, so a short file would otherwise be saved as complete.
            size = tmp.stat().st_size
            if expected is None or size == expected:
                break
            if size > expected:
                raise BuildError(f"{url}: received {size:,} bytes, more than the {expected:,} advertised")
            raise ConnectionError(f"connection closed at {size:,} of {expected:,} bytes")
        # OSError, not TimeoutError: on Python 3.9 a read that stalls mid-transfer
        # raises socket.timeout, which only became an alias of TimeoutError in 3.10.
        except OSError as exc:
            progressed = tmp.exists() and tmp.stat().st_size > have
            failures = 0 if progressed else failures + 1
            if failures >= 3:
                raise BuildError(f"download failed after 3 attempts without progress: {url}\n    {exc}") from exc
            done = tmp.stat().st_size if tmp.exists() else 0
            Log.warn(f"interrupted at {done:,} bytes ({exc}); resuming")
            time.sleep(2 * (failures + 1))
    tmp.replace(dest)
    Log.ok(f"saved {dest.relative_to(ROOT)} ({dest.stat().st_size:,} bytes)")
    return dest


def get_json(url: str, timeout: int = 180, attempts: int = 4) -> Any:
    """GET JSON with retries. Public French APIs time out often enough to matter."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == attempts - 1:
                raise BuildError(f"GET failed after {attempts} attempts: {url}\n    {exc}") from exc
            Log.warn(f"attempt {attempt + 1} failed ({exc}); retrying")
            time.sleep(3 * (attempt + 1))


def require_manual(source: Source, filenames: Iterable[str]) -> list[Path]:
    """Check a manual source's expected files exist; raise a readable error if not."""
    missing = [f for f in filenames if not (source.raw_dir / f).exists()]
    if missing:
        raise ManualFetchRequired(
            source.id, missing, source.meta.get("url", ""), source.meta.get("manual_instructions", "")
        )
    return [source.raw_dir / f for f in filenames]


def extract_zip(zip_path: Path, dest_dir: Path, *, members: list[str] | None = None) -> Path:
    """Extract a raw zip into a scratch dir. Never writes back into data/raw."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = members or zf.namelist()
        for name in names:
            target = dest_dir / Path(name).name
            if target.exists() and target.stat().st_size > 0:
                continue
            with zf.open(name) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
    return dest_dir


# --------------------------------------------------------------------------
# Data helpers
# --------------------------------------------------------------------------
def normalise_insee(value: Any) -> str | None:
    """Coerce anything that might be an INSEE commune code into a 5-char string.

    Pandas will happily read '01001' as the integer 1001; this puts it back.
    Corsica ('2A004') is never numeric, so the column must stay a string throughout.
    """
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text or text in {"NAN", "NONE"}:
        return None
    if text.endswith(".0"):
        text = text[:-2]
    if len(text) < 5 and text.isdigit():
        text = text.zfill(5)
    return text if INSEE_RE.match(text) else None


def is_metropolitan(code_insee: str) -> bool:
    """Metropolitan France = departments 01–95 plus 2A/2B. DROM codes start with 97/98."""
    if not code_insee:
        return False
    return not code_insee.startswith(("97", "98", "99"))


def dept_of(code_insee: str) -> str:
    return code_insee[:3] if code_insee.startswith(("97", "98")) else code_insee[:2]


def json_safe(value: Any) -> Any:
    """Coerce a property value into something JSON can hold.

    GDAL re-infers ISO date strings as timestamps on every GeoJSON round-trip, so
    a transform that reads its own output back gets datetime objects it never
    created. Dates are labels here, not arithmetic, so they become strings.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if value != value else value  # NaN is not valid JSON
    if hasattr(value, "isoformat"):
        text = value.isoformat()[:19]
        return text[:10] if text.endswith("T00:00:00") else text
    if hasattr(value, "item"):  # numpy scalars
        try:
            return json_safe(value.item())
        except Exception:
            pass
    return str(value)


def write_geojson(features: list[dict], path: Path, *, precision: int = 6) -> Path:
    """Write a FeatureCollection with coordinates rounded — 6dp is ~10 cm, plenty."""
    path.parent.mkdir(parents=True, exist_ok=True)

    def round_coords(obj):
        if isinstance(obj, (int, float)):
            return round(obj, precision)
        if isinstance(obj, list):
            return [round_coords(x) for x in obj]
        return obj

    for feat in features:
        if feat.get("geometry"):
            feat["geometry"]["coordinates"] = round_coords(feat["geometry"]["coordinates"])
        props = feat.get("properties")
        if props:
            feat["properties"] = {k: json_safe(v) for k, v in props.items()}
    payload = {"type": "FeatureCollection", "features": features}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return str(n)


def scratch_dir(name: str) -> Path:
    d = ROOT / "data" / "scratch" / name
    d.mkdir(parents=True, exist_ok=True)
    return d
