"""Three bulk exports from the Open Data SNCF explore API."""
from pipeline.common import BuildError, Log, download

URL = "{base}/{dataset}/exports/{fmt}?limit=-1{select}"


def fetch(ctx, force: bool = False) -> None:
    base = ctx.meta["api_base"]
    for key, spec in ctx.meta["exports"].items():
        select = f"&select={spec['select']}" if spec.get("select") else ""
        url = URL.format(base=base, dataset=spec["dataset"], fmt=spec["format"], select=select)
        dest = download(url, ctx.raw_dir / spec["filename"], force=force)
        # The explore API answers an unknown dataset with a JSON error object and
        # HTTP 200, which would otherwise reach transform as an empty export.
        head = dest.read_text(encoding="utf-8", errors="replace")[:400].lstrip()
        if '"errorcode"' in head or '"error_code"' in head:
            dest.unlink()
            raise BuildError(f"{spec['dataset']} export failed: {head[:200]}")
    Log.ok(f"{len(ctx.meta['exports'])} SNCF export(s) in data/raw/{ctx.source.id}")
