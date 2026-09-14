"""Planned and under-construction railway, from OpenStreetMap via Overpass.

Overpass is a shared public service that refuses requests when it is busy, and
it signals that with an HTML error page under HTTP 200. So the fetch tries each
mirror in turn, several times, and only accepts a body that parses as JSON.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from pipeline.common import USER_AGENT, BuildError, Log, human_bytes

QUERY = """[out:json][timeout:600];
(
  way["railway"="construction"]({s},{w},{n},{e});
  way["railway"="proposed"]({s},{w},{n},{e});
);
out geom;
"""


def fetch(ctx, force: bool = False) -> None:
    cfg = ctx.meta["overpass"]
    dest = ctx.raw_dir / cfg["filename"]
    if dest.exists() and not force:
        Log.info(f"cached {dest.name} ({dest.stat().st_size:,} bytes)")
        return

    s, w, n, e = cfg["bbox"]
    body = QUERY.format(s=s, w=w, n=n, e=e).encode("utf-8")
    endpoints = cfg["endpoints"]
    attempts = int(cfg.get("attempts", 6))

    for attempt in range(attempts):
        for endpoint in endpoints:
            try:
                req = urllib.request.Request(
                    endpoint, data=body,
                    headers={"User-Agent": USER_AGENT, "Content-Type": "text/plain; charset=utf-8"},
                )
                with urllib.request.urlopen(req, timeout=300) as resp:
                    raw = resp.read()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                Log.warn(f"{endpoint.split('/')[2]}: {exc}")
                continue

            # A busy Overpass answers 200 with an HTML error page.
            if not raw.lstrip()[:1] == b"{":
                Log.warn(f"{endpoint.split('/')[2]}: server busy, returned a non-JSON body")
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                Log.warn(f"{endpoint.split('/')[2]}: unreadable JSON ({exc})")
                continue

            ways = payload.get("elements") or []
            if not ways:
                Log.warn(f"{endpoint.split('/')[2]}: query returned zero ways")
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
            Log.ok(f"{len(ways):,} ways from {endpoint.split('/')[2]} "
                   f"→ {dest.name} ({human_bytes(len(raw))})")
            return

        if attempt < attempts - 1:
            Log.info(f"all mirrors busy; retrying in 30 s (attempt {attempt + 2}/{attempts})")
            time.sleep(30)

    raise BuildError(
        f"every Overpass mirror refused the query after {attempts} rounds.\n"
        "    This is a load problem on a shared public service, not a data problem — "
        "re-run later, or the source can be skipped with `enabled: false`."
    )
