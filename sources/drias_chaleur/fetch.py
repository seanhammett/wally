"""Manual source: print what to download and where to put it, then stop.

Never scrape behind a form or fake a session. Record the instruction instead.
"""
from pipeline.common import require_manual


def fetch(ctx, force: bool = False) -> None:
    require_manual(ctx.source, ctx.meta["expected_files"])
