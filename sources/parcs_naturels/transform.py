"""Lambert-93 park perimeters → one EPSG:4326 GeoJSON layer."""
import geopandas as gpd
import pandas as pd
from shapely.geometry import mapping

from pipeline.common import METRO_BBOX, Log, human_bytes, write_geojson
from pipeline.tile import simplify

# Overseas territory codes carried in the INPN `territoire` field.
DROM = {"MTQ", "GLP", "GUF", "REU", "MYT", "SPM", "NCL", "PYF", "WLF", "BLM", "MAF", "ATF"}


def classify(props: dict, default_kind: str) -> str:
    """National parks are published as cœur (CT_*) and aire d'adhésion (AA_*)."""
    local = (props.get("id_local") or "").upper()
    if default_kind == "Parc national":
        if local.startswith("AA_"):
            return "Parc national — aire d'adhésion"
        return "Parc national — cœur"
    return default_kind


def transform(ctx) -> None:
    frames = []
    for entry in ctx.meta["wfs"]["typenames"]:
        path = ctx.raw_dir / entry["file"]
        if not path.exists():
            raise RuntimeError(f"missing {path}; run fetch first")
        gdf = gpd.read_file(path)
        gdf = gdf.set_crs(ctx.meta["source_crs"], allow_override=True)
        before = len(gdf)
        if "territoire" in gdf.columns:
            gdf = gdf[~gdf["territoire"].fillna("").str.upper().isin(DROM)]
        Log.info(f"{entry['name']}: {before} features, {len(gdf)} after dropping overseas territories")

        gdf["type_parc"] = [classify(r, entry["kind"]) for r in gdf.to_dict("records")]
        frames.append(gdf)

    gdf = pd.concat(frames, ignore_index=True)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=frames[0].crs)

    # The whole projection problem, solved in one call.
    gdf = gdf.to_crs("EPSG:4326")

    # Self-intersections here would crash tippecanoe downstream.
    gdf["geometry"] = gdf.geometry.make_valid()
    gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()]

    minx, miny, maxx, maxy = METRO_BBOX
    inside = gdf.geometry.bounds
    keep = (inside.maxx > minx) & (inside.minx < maxx) & (inside.maxy > miny) & (inside.miny < maxy)
    if (~keep).any():
        Log.warn(f"{int((~keep).sum())} park(s) fell outside metropolitan France and were dropped")
    gdf = gdf[keep]

    gdf["surface_km2"] = (gdf.to_crs("EPSG:2154").geometry.area / 1e6).round(1)

    # GDAL types the INPN date fields as timestamps; they are labels here, so
    # they become plain ISO strings rather than travelling as datetime objects.
    features = []
    for rec, geom in zip(gdf.drop(columns="geometry").to_dict("records"), gdf.geometry):
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "nom": rec.get("nom_site"),
                    "type_parc": rec.get("type_parc"),
                    "surface_km2": float(rec["surface_km2"]) if pd.notna(rec.get("surface_km2")) else None,
                    "date_creation": str(rec.get("date_crea") or "")[:10],
                    "gestionnaire": rec.get("operateur") or rec.get("gest_site"),
                    "fiche_inpn": rec.get("url_fiche"),
                },
                "geometry": mapping(geom),
            }
        )
    features.sort(key=lambda f: (f["properties"]["type_parc"] or "", f["properties"]["nom"] or ""))
    Log.info(f"{len(features)} park perimeters in metropolitan France")

    # These perimeters follow parcel boundaries and are absurdly detailed for a
    # national overlay — 39 MB for 73 polygons. Simplify here rather than at the
    # tiling step so that what validate.py checks is what actually ships. -clean
    # is off: park designations overlap by design and cleaning treats every
    # overlap as a sliver to resolve.
    full = ctx.scratch / "parcs-full.geojson"
    write_geojson(features, full)
    Log.info(f"full resolution: {human_bytes(full.stat().st_size)}")
    reduced = ctx.scratch / "parcs-simplified.geojson"
    simplify(full, reduced, ctx.meta.get("simplify", "5%"), clean=False)

    # Simplification can introduce self-intersections; repair before publishing.
    out = gpd.read_file(reduced)
    out["geometry"] = out.geometry.make_valid()
    out = out[~out.geometry.is_empty & out.geometry.notna()]
    repaired = [
        {"type": "Feature", "properties": rec, "geometry": mapping(geom)}
        for rec, geom in zip(out.drop(columns="geometry").to_dict("records"), out.geometry)
    ]
    write_geojson(repaired, ctx.out_path)
