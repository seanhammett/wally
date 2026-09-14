"""Pull park perimeters from the Géoplateforme WFS, in Lambert-93."""
from pipeline.common import Log, download

QUERY = (
    "{endpoint}?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
    "&TYPENAMES={typename}&OUTPUTFORMAT=application/json&SRSNAME=EPSG:2154&COUNT=5000"
)


def fetch(ctx, force: bool = False) -> None:
    wfs = ctx.meta["wfs"]
    for entry in wfs["typenames"]:
        url = QUERY.format(endpoint=wfs["endpoint"], typename=entry["name"])
        download(url, ctx.raw_dir / entry["file"], force=force)
    Log.ok(f"{len(wfs['typenames'])} WFS layer(s) in data/raw/{ctx.source.id}")
