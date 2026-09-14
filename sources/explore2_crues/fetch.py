"""Explore2 TRACC annual flood-peak indicator, aggregated to commune by RARE."""
from pipeline.common import download


def fetch(ctx, force: bool = False) -> None:
    res = ctx.meta["resource"]
    download(res["url"], ctx.raw_dir / res["filename"], force=force, timeout=900)
