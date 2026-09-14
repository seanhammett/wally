"""Base permanente des équipements 2025 (commune level, France entière)."""
from pipeline.common import download


def fetch(ctx, force: bool = False) -> None:
    res = ctx.meta["resource"]
    download(res["url"], ctx.raw_dir / res["filename"], force=force)
