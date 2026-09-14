"""Commune-level first and second round results from the Ministry of the Interior."""
from pipeline.common import download


def fetch(ctx, force: bool = False) -> None:
    for res in ctx.meta["resources"]:
        download(res["url"], ctx.raw_dir / res["filename"], force=force)
