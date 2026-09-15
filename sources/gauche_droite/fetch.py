"""CHES party positions and the 2024 European and legislative commune results.

The 2022 presidential results come from presidentielle_2022's output instead.
"""
from pipeline.common import download


def fetch(ctx, force: bool = False) -> None:
    ches = ctx.meta["ches"]
    download(ches["url"], ctx.raw_dir / ches["filename"], force=force)
    for election in ctx.meta["elections"].values():
        if "url" in election:
            download(election["url"], ctx.raw_dir / election["filename"], force=force, timeout=900)
