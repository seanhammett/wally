"""Commune reference table (no geometry) from geo.api.gouv.fr."""
from pipeline.common import download

URL = (
    "https://geo.api.gouv.fr/communes"
    "?fields=code,nom,codeDepartement,codeRegion,population,surface&format=json"
)


def fetch(ctx, force: bool = False) -> None:
    download(URL, ctx.raw_dir / "communes.json", force=force)
