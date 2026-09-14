# Implementation plan: France data overlay map

A brief for a coding agent. Target: a static webpage showing a map of France onto which
arbitrary public datasets can be overlaid as toggleable layers, with new data sources added
by dropping in a folder rather than editing application code.

---

## 1. Guiding principles

These override any cleverness. If a decision is unclear, pick the option that satisfies more
of these.

1. **Static site, no server.** The final artefact is HTML + JS + data files on a CDN. No
   database, no API, no runtime backend. This removes an entire class of failure and makes
   hosting free and permanent.
2. **All data work happens offline, in a build step.** The browser never downloads a
   shapefile, never reprojects, never parses a CSV from a government site. It loads
   pre-baked tiles and pre-computed JSON. Every transform is a Python script run on a
   laptop, whose output is committed or published.
3. **Raw data is immutable.** Downloaded files land in `data/raw/<source_id>/` and are never
   edited. Every transform reads from raw and writes to `data/processed/`. This means any
   step can be re-run from scratch, and a broken transform can never corrupt the source.
4. **One join key.** Nearly all French public data is published per *commune*, identified by
   its 5-character INSEE code. Standardising on that key is what makes disparate sources
   comparable. Everything else follows from it.
5. **Plain JavaScript.** No React, no bundler, no npm build for the frontend. ES modules
   loaded directly, libraries from a vendored `lib/` folder. The frontend should be
   readable and debuggable by opening dev tools.
6. **Fail loudly at build time, never at render time.** A validation step rejects
   non-conforming data before it ever reaches the map.

---

## 2. Architecture

```
┌─ data/raw/<source_id>/      downloaded originals, never modified
│
├─ sources/<source_id>/       one folder per data source
│    ├─ source.yaml           metadata: name, url, licence, CRS, join key, date fetched
│    ├─ fetch.py              downloads into data/raw/<source_id>/
│    ├─ transform.py          raw → data/processed/<source_id>.{geojson,csv}
│    └─ layer.json            how to render it: type, colour ramp, breaks, legend, default state
│
├─ pipeline/
│    ├─ build.py              runs every source's transform, then validate, then tile
│    ├─ validate.py           enforces the output contract; exits non-zero on failure
│    └─ tile.py               geojson → pmtiles via tippecanoe
│
├─ data/processed/            intermediate, normalised outputs (EPSG:4326)
│
└─ site/                      the deployable static site
     ├─ index.html
     ├─ app.js
     ├─ style.css
     ├─ layers.json           generated manifest, concatenation of every layer.json
     ├─ lib/                  maplibre-gl, pmtiles (vendored, version-pinned)
     └─ tiles/                *.pmtiles and small *.geojson
```

The whole system is: `python pipeline/build.py` regenerates `site/`, then `site/` is deployed
as-is.

---

## 3. Technology choices

| Concern | Choice | Why |
|---|---|---|
| Map renderer | **MapLibre GL JS** (v4/v5, vendored) | Open source, no API key, no usage limits, handles 35,000 polygons via vector tiles. Leaflet is simpler but will not survive a full commune layer. |
| Tile format | **PMTiles** | A single file served over HTTP range requests from any static host. No tile server. Read by `pmtiles` JS protocol plugin. |
| Basemap | **IGN Géoplateforme** raster WMTS at `data.geopf.fr` | Free, no key, French-specific, includes plain topo, orthophoto, and cadastral parcels. Fall back to a Protomaps or OSM raster style if IGN is down. |
| Data processing | **Python 3.12 + GeoPandas** | `read_file` / `to_crs` / `to_file` covers 90% of what's needed. `uv` for the environment. |
| Format wrangling | **GDAL `ogr2ogr`** | For anything GeoPandas chokes on (GML, GeoPackage layers, odd encodings). |
| Simplification | **mapshaper** CLI | `-simplify 5% keep-shapes` is more topologically robust than Shapely's simplify — it won't create gaps between adjacent communes. |
| Tiling | **tippecanoe** + `pmtiles convert` | Standard, well-documented, one command. |
| Hosting | Cloudflare Pages / Netlify / GitHub Pages | All support HTTP range requests, which PMTiles needs. |

---

## 4. The source adapter contract

This is the core of "easy to add a new source." Adding a dataset means creating one folder.
No frontend code changes.

### `source.yaml`

```yaml
id: rga_argiles
name: "Retrait-gonflement des argiles (exposition 2026)"
attribution: "BRGM / Géorisques — Licence Ouverte 2.0"
url: "https://www.georisques.gouv.fr/donnees/bases-de-donnees/retrait-gonflement-des-argiles-version-2026"
fetched: 2026-09-10
source_crs: "EPSG:2154"          # Lambert-93; ALWAYS read from the .prj, never assume
kind: polygon                     # one of: commune_table | polygon | point
```

### `transform.py` output contract

`validate.py` enforces all of these and exits non-zero on any failure:

- Output CRS is **EPSG:4326**, longitude/latitude order.
- If `kind: commune_table` → a CSV with a `code_insee` column of **zero-padded 5-character
  strings** (including `2A…`/`2B…` for Corsica), plus one or more value columns. No geometry.
- If `kind: polygon` or `point` → GeoJSON with valid geometries (`.make_valid()` applied),
  bounded within metropolitan France unless the source declares otherwise.
- Every value column is declared in `layer.json` with a type (`numeric`, `ordinal`,
  `categorical`) and a unit.
- No column names longer than 10 characters if the data passed through a shapefile — they'll
  have been truncated already, so rename explicitly.

### `layer.json`

```json
{
  "id": "rga_argiles",
  "label": "Clay shrink–swell exposure",
  "group": "Hazards",
  "type": "polygon",
  "source_file": "rga_argiles.pmtiles",
  "source_layer": "rga_argiles",
  "paint": {
    "property": "niveau",
    "scale": "ordinal",
    "stops": [["faible", "#fde725"], ["moyen", "#fd8d3c"], ["fort", "#bd0026"]]
  },
  "default_visible": false,
  "default_opacity": 0.6,
  "legend_note": "Arrêté of 9 Jan 2026. Moderate/high = 55% of metropolitan territory."
}
```

`pipeline/build.py` concatenates every `layer.json` into `site/layers.json`. The frontend
reads that manifest and builds the entire layer control from it.

---

## 5. The three layer patterns

Every dataset you'll meet falls into one of three shapes. Implement one worked example of
each in Phase 2 and everything after is a variation.

### Pattern A — Commune choropleth (most common)

A table of numbers keyed on INSEE code, joined to a single canonical commune geometry.

- **Canonical geometry:** IGN ADMIN EXPRESS COG (`geoservices.ign.fr`), or for a smaller,
  pre-simplified version, the `france-geojson` repository. Record the *millésime* (year) —
  communes merge and split annually, so a 2024 table joined to 2026 geometry will lose rows.
- **Build-time join, not runtime.** Merge every commune table into the geometry as
  attributes, then tile once. Re-tiling 35,000 communes takes seconds, so the flexibility of
  runtime `setFeatureState` isn't worth the complexity.
- **Join diagnostics are the single most valuable debugging output.** `validate.py` must
  print, for each table: rows in source, rows matched, rows unmatched, and a sample of
  unmatched codes. A silent 8% join failure is the most likely bug in this entire project.

Examples: drought restriction days per commune, population trend, GP density, distance to
emergency care.

### Pattern B — Polygon overlay

Geometry that exists in its own right and isn't keyed to communes.

- Read the shapefile/GeoPackage, reproject to 4326, simplify with mapshaper, tile.
- Some of these are large. The clay exposure map is already published as **PMTiles** on
  data.gouv.fr — check for that before generating your own.

Examples: regional and national parks, flood zoning, clay exposure, Cerema low-lying coastal
zones, Natura 2000, zones de répartition des eaux.

### Pattern C — Points

- Usually a CSV with lat/lon columns, or an API returning GeoJSON.
- If under ~5,000 points, skip tiling entirely and load as a plain GeoJSON file. Simpler.
- **Hub'eau** (`hubeau.eaufrance.fr`) is a genuine REST API for water data — piezometers,
  river flow stations, water quality. Paginated JSON, no key. It's the friendliest French
  source there is and a good first Pattern C implementation.

Examples: piezometer stations, hospitals, train stations, trailheads.

---

## 6. On projections and "extracting an existing map"

A correction to the framing worth stating explicitly for whoever builds this, because it
determines a lot of effort.

**Do not trace or reshape existing map images.** Almost every French map you will find
published as a picture is derived from a vector dataset that is itself downloadable under an
open licence. Find the vector source; it will be more accurate than any georeferencing you
could do, and it takes one command instead of an afternoon.

French official vector data is nearly always in **Lambert-93 (EPSG:2154)**. Converting is a
one-liner:

```python
gdf = gpd.read_file("raw/thing.shp").to_crs("EPSG:4326")
```

or

```bash
ogr2ogr -t_srs EPSG:4326 out.geojson in.shp
```

MapLibre then renders in Web Mercator automatically. There is no manual reshaping,
rubber-sheeting, or alignment step. The projection problem is entirely solved by reading the
source's `.prj` file and calling `to_crs` once.

**Only if there is genuinely no vector source** — a scanned historical map, a PDF figure from
a report with no data behind it — fall back to georeferencing:

1. QGIS → Raster → Georeferencer.
2. Place 8–12 ground control points on unambiguous features (river confluences, coastline
   headlands, town centres), spread across the whole sheet including corners.
3. Thin plate spline transform for distorted originals, polynomial 2 for clean ones.
4. Export as GeoTIFF in EPSG:4326, then `gdal2tiles` or serve as a raster layer.
5. **Label it as approximate in the UI.** A georeferenced raster carries error the user
   cannot see, and it should never be treated as equivalent to vector data.

For tabular data on web pages with no download link, `pandas.read_html()` handles most
government HTML tables in one call. Save the raw HTML to `data/raw/` first so the parse is
reproducible when the page changes.

---

## 7. Frontend behaviour

Keep `app.js` under ~400 lines. It should do six things:

1. **Initialise the map.** MapLibre, IGN basemap, centred on metropolitan France, with a
   basemap selector (plain / topo / satellite).
2. **Read `layers.json` and build the panel.** Layers grouped by `group`, each with a
   checkbox and an opacity slider. Order in the manifest = draw order.
3. **Enforce one choropleth at a time.** Choropleths are opaque and stack meaninglessly.
   Turning one on turns the previous one off; polygon and point overlays stack freely. This
   is a small rule that prevents most confusing screenshots.
4. **Auto-generate the legend** from the active layers' `paint` blocks. Never hand-write a
   legend.
5. **Click-to-inspect.** Clicking anywhere queries every loaded layer at that point and opens
   a side panel listing the commune name, INSEE code, and every attribute currently loaded,
   grouped by source with its attribution. *This is the feature that answers "how does the
   data interact" — prioritise it over any styling work.*
6. **Encode state in the URL hash:** `#lat/lon/zoom/layer1,layer2`. Makes findings shareable
   and bookmarkable, and costs about 20 lines.

Explicitly out of scope for v1: drawing tools, user accounts, saved views, printing,
mobile-optimised layout, animated time sliders.

---

## 8. Build phases

Each phase ends with something that runs.

**Phase 0 — Skeleton.** Static page, MapLibre, IGN basemap, one hardcoded GeoJSON layer
(national parks — small, fast, visually obvious). Deployed. *Done when: a park polygon is
visible on a public URL.*

**Phase 1 — Manifest-driven layers.** Move the hardcoded layer into a `layer.json`, write the
manifest reader, layer panel, toggles, opacity, auto-legend. *Done when: adding a second
layer requires zero changes to `app.js`.*

**Phase 2 — Pipeline and three patterns.** Build `fetch`/`transform`/`validate`/`build`.
Implement one source of each pattern: commune choropleth (a per-commune table), polygon
overlay (parks or clay), points (Hub'eau piezometers). *Done when: `python pipeline/build.py`
regenerates the whole site from raw data.*

**Phase 3 — Inspection and permalinks.** Click-to-inspect side panel, URL hash state.
*Done when: clicking a commune shows every loaded attribute for it.*

**Phase 4 — Scale.** Tile the full commune layer with tippecanoe → PMTiles. Tune zoom levels
and simplification so the file stays under ~50 MB and pans smoothly. *Done when: all 35,000
communes render without jank.*

**Phase 5 — More sources.** Add datasets one at a time. Each should take under an hour once
the pattern is established. If one takes longer, the contract is wrong — fix the contract
rather than special-casing the source.

---

## 9. Gotchas specific to French open data

The agent should read this section before writing any transform. Every one of these has cost
someone a day.

- **Encoding.** A large amount of French open data is Latin-1 / CP1252, not UTF-8. Symptom:
  `Ã©` where `é` should be. Always pass `encoding=` explicitly; try `utf-8` then `latin-1`.
- **CSV dialect.** Separator is often `;` and the decimal mark is often `,`. Use
  `pd.read_csv(f, sep=";", decimal=",")`.
- **INSEE codes must be strings.** Pandas will read `01001` as the integer `1001` and destroy
  the join. Always `dtype={"code_insee": str}`. Corsica uses `2A` and `2B` prefixes, so the
  column can never be numeric.
- **Commune millésime drift.** Communes merge and split every 1 January. Keep a reference
  table of the year each dataset uses, and maintain an old→new code mapping (INSEE publishes
  one) for anything more than a couple of years old.
- **Shapefile field names truncate to 10 characters.** `population_2024` silently becomes
  `population`. Rename explicitly in `transform.py`, never rely on the source names.
- **Invalid geometries are common.** Self-intersections in administrative boundaries will
  crash tippecanoe. Apply `.make_valid()` (or `buffer(0)`) unconditionally before writing.
- **Overseas territories destroy your bounding box.** French national datasets often include
  Guadeloupe, Réunion, Guyane. Filter to metropolitan France by department code prefix, or
  handle them as a deliberate separate view — but decide, don't let it happen by accident.
- **Simplify with mapshaper, not Shapely.** Shapely simplifies each polygon independently and
  will open visible gaps between neighbouring communes. mapshaper preserves shared edges.
- **Licence and attribution.** Most of this is Licence Ouverte / Etalab 2.0, which requires
  attribution. Carry the `attribution` field from `source.yaml` through to the UI and display
  it for every visible layer. IGN data has its own terms — check them.
- **Government URLs rot.** `fetch.py` should record the URL and date in `source.yaml` and
  keep the raw download. When a link dies you will still have the data and know what it was.

---

## 10. Baseline source catalogue

### 10.0 Foundations — build these first

Nothing else works until these three exist.

| Source | Access | Auto-fetch? | Notes |
|---|---|---|---|
| **Commune geometry** — IGN ADMIN EXPRESS COG | geoservices.ign.fr | Manual (large zip, per-millésime) | The canonical layer. Record the year. Lambert-93. |
| **Bassin de vie / aire d'attraction zoning** — INSEE | insee.fr | Manual (xlsx) | A commune→bassin lookup table. Several indicators below are only meaningful aggregated to this level, not to the commune. |
| **Commune reference & code history** — `geo.api.gouv.fr` | REST API | Yes | Names, population, department, and — crucially — the old→new code mapping for merged communes. |

### 10.1 Reproducing the Projet Celsius indicator set

Celsius built its 2050 exposure map from public data only, so the whole thing is
reproducible. Their structure is three pillars — physical exposure, access, autonomy — and
it's a good skeleton because it forces you to keep hazard separate from liveability rather
than mashing them into one score. Their aggregation within the exposure pillar is
*non-compensatory*: a very bad score in one hazard family is not redeemed by good scores
elsewhere. If you compute a composite, copy that choice; averaging hides exactly the
information you're looking for.

**Pillar 1 — Physical exposure (six hazard families)**

| Indicator | Source | Access | Pattern | Notes |
|---|---|---|---|---|
| Tropical nights/yr, days >35 °C, 2050 | DRIAS 2020 / Climadiag Commune | Manual (DRIAS portal; Climadiag is per-commune PDF, not bulk) | A | Gridded NetCDF → zonal stats to commune. Hardest source here; see below. |
| Summer low-flow and flood-flow change to 2050 | Explore2 (ecologie.gouv.fr) | Manual | A | Median commune: −14% low flow, +13% flood flow. |
| Soil dryness index | DRIAS | Manual | A | **Read the caveat:** this index is normalised by each soil's own water reserve, so it measures deviation from a *local* baseline, not absolute dryness. It makes Brittany and Pays de la Loire look like the Mediterranean. Do not put it on the same colour ramp as an absolute indicator. |
| Days under drought restriction (alerte renforcée + crise), 2013→ | Propluvia / VigiEau open data | Yes (data.gouv.fr) | A | Median commune ~23 days/yr; top decile >65; 12.5% never restricted. Actual lived constraint, not a model. |
| Zone de répartition des eaux membership | Géorisques / eaufrance | Yes | A or B | Binary, legally binding. 35% of communes entirely inside one. |
| Habitat–forest interface (% of population within 200 m of woodland) | Corine Land Cover × INSEE 200 m population grid | Yes (both on data.gouv/Copernicus) | A (computed) | Celsius substituted this for the statutory wildfire zoning, which put 79% of communes in one bucket and therefore separated nothing. Worth replicating — it's a computed indicator, not a download. |
| Flood zoning (AZI, DDRM, PPRI) | Géorisques databases | Yes, but per-department | B | 65% of communes are classified flood-prone by at least one source, so as a discriminator it's weak. Keep it for parcel-level truth, pair it with the flood-flow *change* indicator above. |
| Retrait-gonflement des argiles (2026 zoning) | Géorisques — **PMTiles already published on data.gouv.fr** | Yes | B | Fastest win in the whole catalogue. Compute % of commune surface per exposure class. Keep it as its own layer rather than folding it into a hazard score: it's a foundation-engineering cost, not a habitability constraint. |
| Coastal low-lying zones, current and +1 m | Cerema, geolittoral.developpement-durable.gouv.fr | Yes | B | Two separate rasters/vectors — load both, toggle between them. Cross with the INSEE 200 m population grid to get people, not just area. |
| Coastal erosion rate (m/yr) | Indicateur national d'érosion côtière | Yes | C or B | 640 communes documented, 326 retreating. Signed value — some places are gaining. |

**Pillar 2 — Access**

| Indicator | Source | Access | Pattern |
|---|---|---|---|
| GP accessibility (APL) | DREES | Yes | A |
| Equipment density | INSEE Base Permanente des Équipements (BPE) | Yes | A |
| 15-year population trajectory | INSEE historical population 1968–2023 | Yes | A |
| Road time to emergency care | INSEE (per 200 m grid cell, population-weighted to commune) | Yes | A |
| Share of households with 2+ cars | INSEE recensement | Yes | A |

Celsius aggregates all five to **bassin de vie**, not commune, and the reason is instructive:
kept at commune level, the pillar's correlation with commune size went from −0.13 to −0.31,
meaning it was mostly measuring urbanity rather than access. Aggregate these five.

**Pillar 3 — Autonomy**

| Indicator | Source | Access | Pattern |
|---|---|---|---|
| Agricultural surface and crop diversity | RPG (Registre Parcellaire Graphique) / Agreste | Yes | A or B |
| % organic farmland | Agence Bio | Yes | A |
| Irrigation volumes per basin | BNPE (Banque nationale des prélèvements en eau) | Yes | A |
| Drinking-water abstraction per capita vs 55 m³ reference | BNPE, at bassin de vie | Yes | A |
| Ski domain top altitude (217 communes) | IGN elevation + domain list | Partly manual | A |

The abstraction indicator is the one worth the effort. It's what reveals that the Marseille
basin draws 4.4 m³ per person per year locally against a 55 m³ reference need — the rest
arrives from the Durance and Verdon. A third of bassins de vie, 17.5 million people, are in
that position. No other indicator in the set surfaces it.

Two honest limitations Celsius documents, worth carrying into your own tool as UI text: the
% organic indicator correlates ~0.05 with the composite score (the least input-dependent
farming is where the climate will be hardest), and the ranking is stable at the extremes but
not in the middle — median rank interval 8,471 places out of 35,191. **Display an
uncertainty band or a coarse class, never a bare rank.** A precise-looking number here is a
lie.

### 10.2 Elections and local political control

Genuinely useful for a long-term move: it tells you whether the commune is likely to fund a
school, permit a renovation, invest in the water network, or fight a solar farm.

| Source | Access | Pattern | Notes |
|---|---|---|---|
| **Municipales 2026, tours 1 & 2** (15 and 22 March 2026) | data.gouv.fr, Ministère de l'Intérieur | Yes | Published as three files per scrutin: commune/secteur level, with polling-station detail. Includes turnout, abstention, blank/null votes, and the composition of the elected councils. Start at `data.gouv.fr/elections`. |
| **Répertoire National des Élus (RNE)** | data.gouv.fr | Yes | Every elected official in France, commune-keyed, with mandate dates. The cleanest way to get "who runs this village" as a joinable table. |
| Présidentielle 2022, européennes 2024, législatives 2024 | data.gouv.fr | Yes | A |
| Municipales 2020 | data.gouv.fr | Yes | A — for trend and turnout comparison |

**A caveat that determines how you should use this.** In communes under 1,000 inhabitants,
municipal elections use *panachage* and most lists carry no party label — the Ministry's
"nuance politique" field is frequently blank or meaningless. So for exactly the small rural
communes you're most likely to be looking at, municipal results tell you almost nothing about
politics. They do tell you about **turnout, contestation (was there more than one list?), and
council renewal**, which are decent proxies for civic health and whether the place is
functioning or coasting.

If you want a political read on a small commune, use **European and presidential** results
instead — those are genuinely comparable nationwide because everyone votes on the same
national lists. Build both layers and label them clearly for what each can and can't support.

Note also that the next départementales and régionales are scheduled for **March 2028**
(postponed a year from 2027 to avoid collision with the presidential and legislative
elections), so 2021 remains the most recent data at that tier for now. Build the loader so a
2028 file drops in without a rewrite.

### 10.3 Everything else, roughly by value per hour of work

| Source | Access | Pattern |
|---|---|---|
| Parcs naturels régionaux / nationaux | data.gouv.fr shapefile | B |
| Hub'eau piezometers & river flow | hubeau.eaufrance.fr REST API | C |
| DVF property transaction prices | files.data.gouv.fr | A or C |
| Natura 2000, réserves naturelles | INPN | B |
| Cadastre and PLU zoning | data.geopf.fr WMTS/WFS | basemap overlay |
| Radon potential | Géorisques / IRSN | A or B |
| Fibre / broadband coverage | ARCEP open data | A |
| Rail stations and journey times | SNCF open data | C |

### 10.4 Fetch strategy

Since you're happy to fetch manually, split the sources explicitly. In `source.yaml` add a
field `fetch: auto | manual`.

- `auto` → `fetch.py` downloads from a stable URL or API. Applies to most of data.gouv.fr,
  Hub'eau, geo.api.gouv.fr, ARCEP.
- `manual` → `fetch.py` does nothing but **print instructions and the expected filename**,
  then `transform.py` fails with a clear message if the file isn't in `data/raw/`. Applies to
  IGN ADMIN EXPRESS, DRIAS, Explore2, INSEE spreadsheets, and Géorisques per-department
  downloads.

This keeps the pipeline a single command while being honest about which steps need a human.
Never have the agent scrape behind a form or fake a session — record the instruction instead.

**On DRIAS specifically.** It's gridded NetCDF at roughly 8 km resolution, not commune-keyed,
so it needs a zonal-statistics step (`rasterstats` or `exactextract`) to aggregate cells to
commune polygons. It is by some distance the most work in this catalogue. Leave it until
everything else runs. If you want the headline numbers sooner, **Climadiag Commune** gives
per-commune values directly and is aligned to the TRACC — but it's a PDF per commune with no
bulk download, so it's a spot-check tool, not a data source. Use it to validate your DRIAS
aggregation on a handful of communes; if your computed tropical-nights figure for a commune
doesn't match Climadiag's, your zonal stats are wrong.

---

## 11. Definition of done for v1

- A public URL loads a map of France in under 3 seconds.
- At least six layers across all three patterns, toggleable independently, with opacity
  control and an auto-generated legend.
- Clicking any point shows every loaded attribute for that location, with source attribution.
- `python pipeline/build.py` rebuilds the entire site from `data/raw/` with no manual steps.
- `python pipeline/validate.py` catches a deliberately corrupted source file.
- A new commune-keyed CSV can be added end-to-end in under an hour by writing one
  `transform.py` and one `layer.json`, touching no application code.
- Every layer displays its licence attribution when visible.
- A `manual`-fetch source whose file is absent fails with a message naming the file and the
  page to download it from, not a stack trace.
- Any composite or ranked layer displays a class or an uncertainty band, never a bare rank.
