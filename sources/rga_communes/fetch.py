"""The BRGM 2026 clay exposure map, as the PMTiles archive Géorisques publishes."""
from pipeline.common import download


def fetch(ctx, force: bool = False) -> None:
    res = ctx.meta["resource"]
    download(res["url"], ctx.raw_dir / res["filename"], force=force, timeout=900)
