"""Basilic and the Musées de France attendance file, from the Ministry of Culture."""
from pipeline.common import BuildError, Log, download


def fetch(ctx, force: bool = False) -> None:
    for key, spec in ctx.meta["resources"].items():
        dest = download(spec["url"], ctx.raw_dir / spec["filename"], force=force)
        head = dest.read_bytes()[:200].decode("utf-8", errors="replace")
        if ";" not in head:
            dest.unlink()
            raise BuildError(f"{key}: expected a ;-separated CSV, got {head[:120]!r}")
    Log.ok(f"{len(ctx.meta['resources'])} Ministry of Culture file(s) in data/raw/{ctx.source.id}")
