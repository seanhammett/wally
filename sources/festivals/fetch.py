"""The Ministry of Culture's national festival list."""
from pipeline.common import BuildError, Log, download


def fetch(ctx, force: bool = False) -> None:
    spec = ctx.meta["resource"]
    dest = download(spec["url"], ctx.raw_dir / spec["filename"], force=force)
    head = dest.read_bytes()[:300].decode("utf-8", errors="replace")
    if "festival" not in head.lower():
        dest.unlink()
        raise BuildError(f"festival list: unexpected header {head[:120]!r}")
    Log.ok(f"festival list in data/raw/{ctx.source.id}")
