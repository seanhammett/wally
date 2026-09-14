"""EEA 1 km interpolated air quality rasters.

The EEA files sit in a public Nextcloud share. Each is a Europe-wide GeoTIFF of
20–80 MB; the whole file lands in data/raw and the transform windows France out
of it. They are read through the share's WebDAV endpoint, authenticated with the
public share token, not through its browser download link: the same file comes
at ~2 MB/s over WebDAV and ~20 KB/s over the link, which is the difference
between eight minutes and half a day.

The expected byte count comes from the share's WebDAV listing, so a truncated
download is caught here rather than surfacing as a corrupt TIFF in the transform.
"""
import re
import urllib.request
from base64 import b64encode

from pipeline.common import USER_AGENT, BuildError, Log, download


WEBDAV = "https://sdi.eea.europa.eu/datashare/public.php/webdav"


def auth_header(share: str) -> dict:
    """A public share's WebDAV takes the share token as the user, no password."""
    return {"Authorization": "Basic " + b64encode(f"{share}:".encode()).decode()}


def listing(share: str, folder: str) -> dict:
    """{filename: bytes} for one folder of the public share, via WebDAV PROPFIND."""
    req = urllib.request.Request(
        f"{WEBDAV}/{folder}/", method="PROPFIND",
        headers={"Depth": "1", "User-Agent": USER_AGENT, **auth_header(share)},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read().decode("utf-8")
    sizes = {}
    for block in re.findall(r"<d:response>(.*?)</d:response>", body, re.S):
        href = re.search(r"<d:href>([^<]+)", block)
        size = re.search(r"<d:getcontentlength>(\d+)", block)
        if href and size:
            sizes[href.group(1).rstrip("/").rsplit("/", 1)[-1]] = int(size.group(1))
    return sizes


def fetch(ctx, force: bool = False) -> None:
    eea = ctx.meta["eea"]
    share = eea["share"]
    for pollutant, spec in eea["pollutants"].items():
        for year, (folder, filename) in sorted(spec["files"].items()):
            dest = ctx.raw_dir / filename
            if dest.exists() and not force:
                Log.info(f"cached {pollutant} {year}: {filename}")
                continue
            expected = listing(share, folder).get(filename)
            if expected is None:
                raise BuildError(f"{folder}/{filename} is no longer in the EEA share — check {ctx.meta['url']}")
            download(f"{WEBDAV}/{folder}/{filename}", dest, force=force, timeout=120,
                     headers=auth_header(share))
            got = dest.stat().st_size
            if got != expected:
                dest.unlink()
                raise BuildError(f"{filename}: got {got:,} bytes, the share advertises {expected:,} — truncated")
