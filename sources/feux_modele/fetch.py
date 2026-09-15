"""The merged-commune list from geo.api.gouv.fr. Fire records and the SIM2 grid
are fetched by their own sources (bdiff_incendies, meteo_climat)."""
from pipeline.common import download


def fetch(ctx, force: bool = False) -> None:
    cfg = ctx.meta["merged_communes"]
    download(cfg["url"], ctx.raw_dir / cfg["filename"], force=force)
