"""RP 2023 population by sex and single year of age, from INSEE's Melodi API.

Melodi serves this 73 MB file over a connection that stalls every few tens of
megabytes and then holds the socket open rather than closing it, so a plain GET
hangs at an arbitrary byte and a truncated body still looks like HTTP 200. The
endpoint does honour Range requests, so the fix is to resume in segments until
the file reaches the length the catalogue publishes.
"""
from __future__ import annotations

import shutil
import urllib.error
import urllib.request
from pathlib import Path

from pipeline.common import USER_AGENT, BuildError, Log, human_bytes

STALL_TIMEOUT = 30      # seconds with no data before giving up on one attempt
MAX_ATTEMPTS = 40


def resume_download(url: str, dest: Path, expected: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    if dest.exists() and dest.stat().st_size == expected:
        Log.info(f"cached {dest.name} ({expected:,} bytes)")
        return

    for attempt in range(MAX_ATTEMPTS):
        have = part.stat().st_size if part.exists() else 0
        if have >= expected:
            break
        headers = {"User-Agent": USER_AGENT, "Range": f"bytes={have}-"}
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=STALL_TIMEOUT) as resp:
                # A 200 to a Range request means the server ignored it and is
                # sending from zero; appending that to what we have would produce
                # a file of the right length made of the wrong bytes.
                if have and resp.status != 206:
                    part.unlink(missing_ok=True)
                    Log.warn("server ignored Range; restarting from zero")
                    continue
                with part.open("ab" if have else "wb") as out:
                    shutil.copyfileobj(resp, out, length=1 << 20)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            got = part.stat().st_size if part.exists() else 0
            if got <= have:
                Log.warn(f"attempt {attempt + 1} made no progress at {got:,} bytes ({exc})")
            else:
                Log.info(f"  {human_bytes(got)} / {human_bytes(expected)} ({got / expected:.0%})")
            continue
        got = part.stat().st_size
        Log.info(f"  {human_bytes(got)} / {human_bytes(expected)} ({got / expected:.0%})")

    got = part.stat().st_size if part.exists() else 0
    if got != expected:
        raise BuildError(
            f"gave up after {MAX_ATTEMPTS} attempts with {got:,} of {expected:,} bytes.\n"
            f"    The partial download is kept at {part}; re-run to continue from there."
        )
    part.replace(dest)
    Log.ok(f"saved {dest.name} ({expected:,} bytes)")


def fetch(ctx, force: bool = False) -> None:
    res = ctx.meta["resource"]
    dest = ctx.raw_dir / res["filename"]
    if force:
        dest.unlink(missing_ok=True)
        dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)
    resume_download(res["url"], dest, int(res["bytes"]))
