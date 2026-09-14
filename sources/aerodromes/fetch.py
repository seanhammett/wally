"""Aerodrome footprints from BD TOPO, plus the DGAC's PEB decree index."""
from pipeline.common import BuildError, Log, download

QUERY = (
    "{endpoint}?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
    "&TYPENAMES={typename}&OUTPUTFORMAT=application/json&SRSNAME={srs}&COUNT={count}"
)


def fetch(ctx, force: bool = False) -> None:
    wfs = ctx.meta["wfs"]
    for entry in wfs["typenames"]:
        url = QUERY.format(
            endpoint=wfs["endpoint"], typename=entry["name"],
            srs=entry["srs"], count=wfs["page_size"],
        )
        dest = download(url, ctx.raw_dir / entry["file"], force=force)
        head = dest.read_text(encoding="utf-8", errors="replace")[:200].lstrip()
        if not head.startswith("{"):
            dest.unlink()
            raise BuildError(f"{entry['name']} came back as XML, not GeoJSON:\n    {head[:200]}")
    Log.ok(f"{len(wfs['typenames'])} WFS layer(s) in data/raw/{ctx.source.id}")
