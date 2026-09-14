"""FiLoSoFi 2021 commune-level income, inequality and poverty indicators."""
from pipeline.common import download


def fetch(ctx, force: bool = False) -> None:
    res = ctx.meta["resource"]
    download(res["url"], ctx.raw_dir / res["filename"], force=force)
