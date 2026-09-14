"""Copernicus GLO-90 terrain tiles over metropolitan France, from the AWS open data bucket."""
import urllib.error
import urllib.request

from pipeline.common import USER_AGENT, Log, download


def exists(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):  # S3 answers 403 or 404 for a key that is not there
            return False
        raise


def fetch(ctx, force: bool = False) -> None:
    got = skipped = 0
    for tile in ctx.meta["tiles"]:
        lat, lon = tile[:3], tile[3:]
        dest = ctx.raw_dir / f"{tile}.tif"
        if dest.exists() and not force:
            got += 1
            continue
        url = ctx.meta["tile_url"].format(lat=lat, lon=lon)
        if not exists(url):
            skipped += 1
            continue
        download(url, dest, force=force, timeout=120)
        got += 1
    Log.ok(f"{got} terrain tiles present, {skipped} all-sea tiles not in the bucket")
