"""Aladin-Climat annual temperature indices: reference plus two RCP scenarios."""
from pipeline.common import download


def fetch(ctx, force: bool = False) -> None:
    for key, res in ctx.meta["resources"].items():
        download(res["url"], ctx.raw_dir / res["filename"], force=force)
