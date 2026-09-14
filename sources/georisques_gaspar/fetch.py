"""Download the national GASPAR export (one zip, every commune).

files.georisques.fr serves this at around 20 KB/s, so a cold fetch of 8 MB takes
several minutes. The generous timeout is deliberate, not a workaround.
"""
from pipeline.common import download


def fetch(ctx, force: bool = False) -> None:
    res = ctx.meta["resource"]
    download(res["url"], ctx.raw_dir / res["filename"], force=force, timeout=900)
