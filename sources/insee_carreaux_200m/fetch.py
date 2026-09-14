"""INSEE 200 m population grid — read by other sources as weights, never mapped itself."""
from pipeline.common import download


def fetch(ctx, force: bool = False) -> None:
    res = ctx.meta["resource"]
    download(res["url"], ctx.raw_dir / res["filename"], force=force, timeout=300)
