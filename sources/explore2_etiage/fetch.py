"""Explore2 TRACC summer low-flow indicators, aggregated to commune by RARE."""
from pipeline.common import download


def fetch(ctx, force: bool = False) -> None:
    for res in ctx.meta["resources"].values():
        download(res["url"], ctx.raw_dir / res["filename"], force=force, timeout=900)
