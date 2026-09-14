"""Passenger stations and their annual footfall, from Open Data SNCF."""
from pipeline.common import BuildError, Log, download

URL = "{base}/{dataset}/exports/{fmt}?limit=-1"


def fetch(ctx, force: bool = False) -> None:
    base = ctx.meta["api_base"]
    for spec in ctx.meta["exports"].values():
        url = URL.format(base=base, dataset=spec["dataset"], fmt=spec["format"])
        dest = download(url, ctx.raw_dir / spec["filename"], force=force)
        head = dest.read_text(encoding="utf-8", errors="replace")[:400].lstrip()
        if '"errorcode"' in head or '"error_code"' in head:
            dest.unlink()
            raise BuildError(f"{spec['dataset']} export failed: {head[:200]}")
    Log.ok(f"{len(ctx.meta['exports'])} SNCF export(s) in data/raw/{ctx.source.id}")
