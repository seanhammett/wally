"""INSEE commune tourist-accommodation capacity, and census dwelling categories.

Both arrive as zipped CSVs from INSEE's Melodi file API. The capacity file is
7 MB zipped and 130 MB open; the census file is 98 MB zipped, and both servers
drop long transfers often enough that `download`'s resume matters here.
"""
import zipfile

from pipeline.common import BuildError, Log, download


def fetch(ctx, force: bool = False) -> None:
    for key, spec in ctx.meta["resources"].items():
        dest = download(spec["url"], ctx.raw_dir / spec["filename"], force=force)
        try:
            with zipfile.ZipFile(dest) as zf:
                names = zf.namelist()
        except zipfile.BadZipFile as exc:
            # A truncated or error-page download is worse than no download: it
            # would sit in data/raw as cached and fail every later build.
            dest.unlink()
            raise BuildError(f"{key}: {spec['filename']} is not a zip ({exc}); removed, re-run to retry") from exc
        if spec["member"] not in names:
            dest.unlink()
            raise BuildError(f"{key}: expected {spec['member']} inside the archive, found {names}")
        Log.ok(f"{key}: {spec['member']} ({dest.stat().st_size:,} bytes zipped)")
