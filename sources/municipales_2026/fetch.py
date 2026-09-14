"""Commune-level results for both rounds of the 2026 municipal elections."""
from pipeline.common import download


def fetch(ctx, force: bool = False) -> None:
    for res in ctx.meta["resources"]:
        download(res["url"], ctx.raw_dir / res["filename"], force=force)
