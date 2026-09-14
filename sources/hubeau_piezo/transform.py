"""Hub'Eau station records → point GeoJSON, active metropolitan stations only."""
import json

from pipeline.common import Log, is_metropolitan, normalise_insee, write_geojson


def transform(ctx) -> None:
    files = sorted(ctx.raw_dir.glob("stations-*.json"))
    if not files:
        raise RuntimeError(f"no station files in {ctx.raw_dir}; run fetch first")
    stations = [st for path in files for st in json.loads(path.read_text(encoding="utf-8"))]

    cutoff = str(ctx.meta.get("active_since", "1900-01-01"))
    features = []
    dropped_inactive = dropped_overseas = dropped_nogeom = 0

    for st in stations:
        code = normalise_insee(st.get("code_commune_insee"))
        if code is not None and not is_metropolitan(code):
            dropped_overseas += 1
            continue
        lon, lat = st.get("x"), st.get("y")
        if lon is None or lat is None:
            dropped_nogeom += 1
            continue
        end = st.get("date_fin_mesure") or ""
        if end < cutoff:
            dropped_inactive += 1
            continue
        depth = st.get("profondeur_investigation")
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "code_bss": st.get("code_bss"),
                    "commune": st.get("nom_commune"),
                    "code_insee": code,
                    "departement": st.get("nom_departement"),
                    "profondeur_m": round(float(depth), 1) if depth is not None else None,
                    "nb_mesures": st.get("nb_mesures_piezo"),
                    "derniere_mesure": end,
                    "fiche_ades": st.get("urn_bss"),
                },
                "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
            }
        )

    Log.info(
        f"{len(features):,} active stations kept · {dropped_inactive:,} inactive since {cutoff} · "
        f"{dropped_overseas:,} overseas · {dropped_nogeom:,} without coordinates"
    )
    write_geojson(features, ctx.out_path)
