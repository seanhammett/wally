"""One BDIFF export per year → data/raw/bdiff_incendies/incendies-{year}.zip.

BDIFF publishes no bulk file and no API. The export endpoint serves whatever
search criteria are held in the PHP session, so each year needs two requests on a
shared cookie: run the search form, then ask for the zip.
"""
import http.cookiejar
import urllib.parse
import urllib.request

from pipeline.common import USER_AGENT, BuildError, Log


def _session():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def fetch_year(cfg, year: int, dest, timeout: int = 300) -> None:
    opener = _session()
    criteria = urllib.parse.urlencode(
        {
            "if[periodeAnnees][anneeDeb]": year,
            "if[periodeAnnees][anneeFin]": year,
            "if[fr]": 1,
            "if[submit]": "",
        }
    )
    headers = {"User-Agent": USER_AGENT}
    search = f"{cfg['search_url']}?{criteria}"
    Log.info(f"GET {search}")
    with opener.open(urllib.request.Request(search, headers=headers), timeout=timeout) as resp:
        resp.read()

    with opener.open(urllib.request.Request(cfg["export_url"], headers=headers), timeout=timeout) as resp:
        payload = resp.read()
    if not payload.startswith(b"PK"):
        raise BuildError(f"BDIFF returned {len(payload)} bytes that are not a zip for {year}")

    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(payload)
    tmp.replace(dest)
    Log.ok(f"saved {dest.name} ({len(payload):,} bytes)")


def fetch(ctx, force: bool = False) -> None:
    cfg = ctx.meta["bdiff"]
    years = ctx.meta["years"]
    start = int(ctx.meta.get("archive_start", years["start"]))
    for year in range(start, int(years["end"]) + 1):
        dest = ctx.raw_dir / f"incendies-{year}.zip"
        if dest.exists() and not force:
            Log.info(f"cached {dest.name} ({dest.stat().st_size:,} bytes)")
            continue
        fetch_year(cfg, year, dest)
