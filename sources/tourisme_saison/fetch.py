"""INSEE's monthly frequentation survey — nights and arrivals by department."""
import zipfile

from pipeline.common import BuildError, Log, download


def fetch(ctx, force: bool = False) -> None:
    spec = ctx.meta["resource"]
    dest = download(spec["url"], ctx.raw_dir / spec["filename"], force=force)
    try:
        with zipfile.ZipFile(dest) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile as exc:
        dest.unlink()
        raise BuildError(f"{spec['filename']} is not a zip ({exc}); removed, re-run to retry") from exc
    if spec["member"] not in names:
        dest.unlink()
        raise BuildError(f"expected {spec['member']} inside the archive, found {names}")
    Log.ok(f"{spec['member']} ({dest.stat().st_size:,} bytes zipped)")
