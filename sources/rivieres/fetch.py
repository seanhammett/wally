"""Page the BD CARTO hydrographic network out of the Géoplateforme WFS.

250,653 features do not come back in one response, so this walks STARTINDEX in
pages of 5,000. Each page is written as its own file in data/raw, which makes the
fetch resumable: an interrupted run picks up at the first page that is missing
rather than starting again.

The lake surfaces come from a second table on the same service and are paged the
same way into `surface-NNN.geojson`.
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


def in_filter(field: str, values: list[str]) -> str:
    """CQL `field IN ('…','…')`, URL-encoded."""
    quoted = ",".join("'" + v.replace("'", "''") + "'" for v in values)
    return urllib.parse.quote(f"{field} IN ({quoted})", safe="")


def page_path(ctx, index: int, prefix: str = "troncon"):
    return ctx.raw_dir / f"{prefix}-{index:03d}.geojson"


def fetch(ctx, force: bool = False) -> None:
    wfs = ctx.meta["wfs"]
    fetch_table(ctx, wfs, in_filter("classe_de_largeur", ctx.meta["width_classes"]),
                "troncon", "segments ≥5 m wide", force)

    surfaces = ctx.meta["surfaces"]
    natures = [n for group in surfaces["natures"].values() for n in group]
    fetch_table(ctx, {"endpoint": wfs["endpoint"], **surfaces}, in_filter("nature", natures),
                "surface", "lake and reservoir surfaces", force)


def fetch_table(ctx, wfs: dict, cql: str, prefix: str, what: str, force: bool) -> None:
    size = int(wfs["page_size"])

    # Ask how many there are first, so the page count comes from the server
    # rather than from a loop that stops when a page comes back short — the WFS
    # occasionally returns a short page that is not the last one.
    hits_url = HITS.format(endpoint=wfs["endpoint"], typename=wfs["typename"], cql=cql)
    hits_path = ctx.scratch / f"hits-{prefix}.xml"
    download(hits_url, hits_path, force=True)
    text = hits_path.read_text(encoding="utf-8", errors="replace")
    marker = 'numberMatched="'
    if marker not in text:
        raise BuildError(f"WFS did not report numberMatched; got:\n    {text[:300]}")
    total = int(text.split(marker, 1)[1].split('"', 1)[0])
    pages = (total + size - 1) // size
    Log.info(f"{total:,} {what} → {pages} page(s) of {size:,}")

    props = urllib.parse.quote(",".join(wfs["properties"]), safe="")
    for i in range(pages):
        url = QUERY.format(
            endpoint=wfs["endpoint"], typename=wfs["typename"], count=size,
            start=i * size, sort=wfs["sort_by"], props=props, cql=cql,
        )
        dest = page_path(ctx, i, prefix)
        download(url, dest, force=force)
        # A WFS error comes back as 200 with an XML ExceptionReport, which would
        # otherwise sit in data/raw looking like a valid page until transform
        # tripped over it 40 pages later.
        head = dest.read_text(encoding="utf-8", errors="replace")[:200].lstrip()
        if not head.startswith("{"):
            dest.unlink()
            raise BuildError(f"page {i} came back as XML, not GeoJSON:\n    {head[:200]}")

    got = sum(len(json.loads(page_path(ctx, i, prefix).read_text(encoding="utf-8"))["features"]) for i in range(pages))
    if got != total:
        Log.warn(f"fetched {got:,} features but the server reported {total:,}")
    Log.ok(f"{got:,} {what} in data/raw/{ctx.source.id}")
