"""Where people live, at 200 m — weights for turning a grid into a commune value.

A commune's area average counts forest, motorway verge and mountain top as much
as the village. For anything describing what residents experience — the air
they breathe, the weather at their door — the right average is over where they
live. The INSEE 200 m grid (source `insee_carreaux_200m`) gives that.

    people = read_population()                    # x, y (LAEA centre), pop
    people["code"] = assign_communes(people, communes_laea)
    by_commune = weighted_by_commune(people["code"], values, people["pop"].to_numpy())
"""
from __future__ import annotations

import io
import shutil
import subprocess
import zipfile

import numpy as np
import pandas as pd

from pipeline.common import RAW_DIR, SOURCES_DIR, BuildError, Log, load_source, scratch_dir

SOURCE_ID = "insee_carreaux_200m"
LAEA = 3035


def read_population() -> pd.DataFrame:
    """Populated 200 m cells of metropolitan France: x, y (cell centre, LAEA metres) and pop.

    The INSEE zip holds a single .7z, which the standard library cannot open.
    libarchive can — it ships as `bsdtar` on macOS and as libarchive-tools on
    Linux — so the CSV is streamed out of it rather than unpacked to disk.
    """
    meta = load_source(SOURCES_DIR / SOURCE_ID).meta
    path = RAW_DIR / SOURCE_ID / meta["resource"]["filename"]
    if not path.exists():
        raise BuildError(f"{path} is missing — run the fetch stage for '{SOURCE_ID}' first")
    tar = shutil.which("bsdtar") or shutil.which("tar")
    if tar is None:
        raise BuildError("reading the INSEE 200 m grid needs bsdtar (libarchive): apt install libarchive-tools")

    with zipfile.ZipFile(path) as zf:
        inner = [n for n in zf.namelist() if n.endswith(".7z")]
        if len(inner) != 1:
            raise BuildError(f"{path.name}: expected one .7z inside, found {zf.namelist()}")
        seven = scratch_dir(SOURCE_ID) / inner[0]
        if not seven.exists() or seven.stat().st_size != zf.getinfo(inner[0]).file_size:
            with zf.open(inner[0]) as src, seven.open("wb") as out:
                shutil.copyfileobj(src, out)

    listing = subprocess.run([tar, "-tf", str(seven)], capture_output=True, text=True, check=True).stdout.split()
    members = [m for m in listing if m.lower().endswith(".csv") and "met" in m.lower()]
    if len(members) != 1:
        raise BuildError(f"{seven.name}: cannot pick the metropolitan CSV out of {listing}")
    raw = subprocess.run([tar, "-xOf", str(seven), members[0]], capture_output=True, check=True).stdout

    head = raw[:4096].decode("utf-8", "replace").splitlines()[0]
    sep = ";" if head.count(";") > head.count(",") else ","
    df = pd.read_csv(io.BytesIO(raw), sep=sep, dtype=str, usecols=lambda c: c.lower() in ("idcar_200m", "ind"))
    df.columns = [c.lower() for c in df.columns]
    for col in ("idcar_200m", "ind"):
        if col not in df.columns:
            raise BuildError(f"{members[0]} has no '{col}' column")

    # 'CRS3035RES200mN2029800E4252000' — the lower-left corner, in LAEA metres.
    corner = df["idcar_200m"].str.extract(r"N(\d+)E(\d+)")
    if corner.isna().any().any():
        raise BuildError(f"{int(corner.isna().any(axis=1).sum())} cell id(s) do not parse as CRS3035RES200mN…E…")
    out = pd.DataFrame({
        "x": corner[1].astype(float) + 100,
        "y": corner[0].astype(float) + 100,
        "pop": pd.to_numeric(df["ind"], errors="coerce"),
    })
    out = out[out["pop"] > 0].reset_index(drop=True)
    Log.info(f"{len(out):,} populated 200 m cells · {out['pop'].sum():,.0f} people")
    return out


def assign_communes(points: pd.DataFrame, communes) -> pd.Series:
    """INSEE code of the commune containing each x/y point; NaN if none does.

    `communes` must already be in LAEA, the CRS the points are in.
    """
    import geopandas as gpd

    gdf = gpd.GeoDataFrame(geometry=gpd.points_from_xy(points["x"], points["y"]), crs=LAEA)
    hit = gpd.sjoin(gdf, communes[["code_insee", "geometry"]], predicate="within", how="left")
    # A point exactly on a shared boundary matches both sides; keep one.
    hit = hit[~hit.index.duplicated(keep="first")]
    return hit["code_insee"].reindex(gdf.index)


def weighted_by_commune(codes: pd.Series, values: np.ndarray, weights: np.ndarray) -> pd.Series:
    """Weighted mean of `values` per commune code, ignoring NaN values and unassigned points."""
    ok = codes.notna().to_numpy() & ~np.isnan(values)
    frame = pd.DataFrame({"code": codes[ok].to_numpy(), "vw": values[ok] * weights[ok], "w": weights[ok]})
    g = frame.groupby("code").sum()
    return g["vw"] / g["w"]
