"""Page the BD CARTO hydrographic network out of the Géoplateforme WFS.

250,653 features do not come back in one response, so this walks STARTINDEX in
pages of 5,000. Each page is written as its own file in data/raw, which makes the
fetch resumable: an interrupted run picks up at the first page that is missing
rather than starting again.
"""
from __future__ import annotations

import json
import urllib.parse

from pipeline.common import BuildError, Log, download

QUERY = (
    "{endpoint}?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
    "&TYPENAMES={typename}&OUTPUTFORMAT=application/json&SRSNAME=EPSG:2154"
    "&COUNT={count}&STARTINDEX={start}&SORTBY={sort}&PROPERTYNAME={props}&CQL_FILTER={cql}"
)
HITS = (
    "{endpoint}?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
    "&TYPENAMES={typename}&RESULTTYPE=hits&CQL_FILTER={cql}"
)


def width_filter(classes: list[str]) -> str:
    """CQL `classe_de_largeur IN ('…','…')`, URL-encoded."""
    quoted = ",".join("'" + c.replace("'", "''") + "'" for c in classes)
    return urllib.parse.quote(f"classe_de_largeur IN ({quoted})", safe="")


def page_path(ctx, index: int):
    return ctx.raw_dir / f"troncon-{index:03d}.geojson"


def fetch(ctx, force: bool = False) -> None:
    wfs = ctx.meta["wfs"]
    cql = width_filter(ctx.meta["width_classes"])
    size = int(wfs["page_size"])

    # Ask how many there are first, so the page count comes from the server
    # rather than from a loop that stops when a page comes back short — the WFS
    # occasionally returns a short page that is not the last one.
    hits_url = HITS.format(endpoint=wfs["endpoint"], typename=wfs["typename"], cql=cql)
    hits_path = ctx.scratch / "hits.xml"
    download(hits_url, hits_path, force=True)
    text = hits_path.read_text(encoding="utf-8", errors="replace")
    marker = 'numberMatched="'
    if marker not in text:
        raise BuildError(f"WFS did not report numberMatched; got:\n    {text[:300]}")
    total = int(text.split(marker, 1)[1].split('"', 1)[0])
    pages = (total + size - 1) // size
    Log.info(f"{total:,} segments ≥5 m wide → {pages} page(s) of {size:,}")

    props = urllib.parse.quote(",".join(wfs["properties"]), safe="")
    for i in range(pages):
        url = QUERY.format(
            endpoint=wfs["endpoint"], typename=wfs["typename"], count=size,
            start=i * size, sort=wfs["sort_by"], props=props, cql=cql,
        )
        dest = page_path(ctx, i)
        download(url, dest, force=force)
        # A WFS error comes back as 200 with an XML ExceptionReport, which would
        # otherwise sit in data/raw looking like a valid page until transform
        # tripped over it 40 pages later.
        head = dest.read_text(encoding="utf-8", errors="replace")[:200].lstrip()
        if not head.startswith("{"):
            dest.unlink()
            raise BuildError(f"page {i} came back as XML, not GeoJSON:\n    {head[:200]}")

    got = sum(len(json.loads(page_path(ctx, i).read_text(encoding="utf-8"))["features"]) for i in range(pages))
    if got != total:
        Log.warn(f"fetched {got:,} features but the server reported {total:,}")
    Log.ok(f"{got:,} hydrographic segments in data/raw/{ctx.source.id}")
