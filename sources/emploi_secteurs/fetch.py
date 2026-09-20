"""Two INSEE cubes: residents in work by sector (census), and salaried jobs by
sector where they are located (FLORES).

Both come from Melodi's file API as zipped CSVs; `download` resumes, which
matters because Melodi stalls on long transfers.
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
            # An error page saved as a zip would sit in data/raw as cached and
            # fail every later build.
            dest.unlink()
            raise BuildError(f"{key}: {spec['filename']} is not a zip ({exc}); removed, re-run to retry") from exc
        if spec["member"] not in names:
            dest.unlink()
            raise BuildError(f"{key}: expected {spec['member']} inside the archive, found {names}")
        Log.ok(f"{key}: {spec['member']} ({dest.stat().st_size:,} bytes zipped)")
