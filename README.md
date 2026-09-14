# France data overlay map

A static map of France onto which public datasets are overlaid as toggleable
layers. Adding a dataset means creating one folder under `sources/` — no
frontend code changes.

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
brew install tippecanoe && npm i -g mapshaper
.venv/bin/python pipeline/build.py
.venv/bin/python pipeline/serve.py --port 8000
```

What a full build produces today:

| | |
|---|---|
| Sources | 25 built, 2 fetch-only inputs (population grid, terrain), 1 manual awaiting its download |
| Layers | 52, across all four patterns |
| Communes | 34,746 (metropolitan France, ADMIN EXPRESS millésime 2026) |
| `site/tiles/` | 150 MB — a 98 MB commune tileset, a 22 MB air quality grid, river and rail tilesets, four small GeoJSONs |
| `site/stats/` | 10 MB — 67 correlatable commune columns plus a centroid index |
| Full rebuild from `data/raw` | ~35 min (139 s of it is join + tiling) |
| First fetch of everything | ~2 h, ~3.9 GB into `data/raw` |

Layer groups: **Income & property** (median standard of living, income
inequality, poverty rate, price per m², price of a house), **Population &
access** (population, density, median age, share aged 65 and over, ageing ratio,
food access, food shops, everyday-services basket, service-provision class,
health services),
**Hazards** (drought days, worst restriction level, projected summer low flow at
+2.7 °C and at +4 °C, tropical nights, summer days, area burned, fire count,
flood disaster declarations, flood PPR status, coastal hazard, clay
shrink–swell), **Air quality** (PM2.5, NO₂ and ozone per commune, weighted by
where residents live, and the same three on the raw 1 km grid), **Climate
(observed)** (rainy days, solar energy and summer afternoon highs over
2016–2025, at residents' altitude and calibrated against Météo-France
stations), **Elections** (2022 presidential second round, first-round
leader, presidential turnout; 2026 municipal turnout, lists standing, winning
nuance), **Transport** (railway lines by type and by speed, stations by annual passengers,
rail under construction and proposed, airports, aircraft noise zoning),
**Protected areas**, **Water** (rivers and canals, piezometers), **Reference**.

Several layers are deliberately paired so they can be stacked and compared: the
projected low flow at +2.7 °C against the same indicator at +4 °C, the flood
declarations that have actually happened against the flood plans that exist, and
the restrictions VigiEau imposed last year against the low flow Explore2
projects. Where two independent lines of evidence agree, that agreement is worth
more than either alone.

Income and property price are the other pair worth stacking. They are measured
completely differently — one from tax records of residents, one from the tax
authority's record of what changed hands — so where they diverge, something is
happening: second-home coasts where prices outrun local incomes, and former
industrial towns where the reverse holds.

## The correlator

Stacking two choropleths and squinting only goes so far, so the panel will do
the comparison properly. Pick any two of the 57 numeric commune statistics and
the map colours every commune by how the two relate **there**, not nationally.
Three modes, because "how do these two go together" is really three questions:

- **Local r** — Pearson r recomputed inside a disc around each commune, 10 to
  80 km wide. This is the one worth looking at. A national r of +0.54 is almost
  never +0.54 everywhere, and this says which halves of the country it is made
  of.
- **Agreement** — each commune's own contribution to the national r, the product
  of its two z-scores. Averaged over every commune it comes back to exactly that
  r, so this decomposes the headline number rather than inventing a new one.
- **Gap** — the difference of the two z-scores, z(A) − z(B): how far ahead of B
  each commune's A stands. This is the one that finds the corners of the cloud,
  which the product cannot: z(A)·z(B) gives the same answer for "high on both"
  as for "low on both", and the same answer for either kind of mismatch.

**Highlight** then picks which end of the scale is painted at all. Everything
else goes transparent and the basemap shows through, so the answer is the only
colour on the map. What the two ends mean depends on the mode, so the chips are
named for it — *Together* / *Opposed* for the first two, *A ≫ B* / *B ≫ A* for
the gap.

Median standard of living against price per m² is the demonstration. As a
correlation it is +0.54 nationally, strongly positive across most of the country
and sharply negative in the Tarentaise and Oisans ski communes. As a **Gap** it
answers the more useful question — where is housing expensive relative to what
people there earn? Set **Highlight** to *B ≫ A* and what is left is the
Mediterranean coast, Corsica, the Basque coast, the Alpine resorts and inner
Paris; the extremes are Saint-Jean-Cap-Ferrat, Val-d'Isère, Saint-Tropez and
Megève, at nine to eleven standard deviations of mismatch. Flip to *A ≫ B* and
you get the Geneva commuter belt in Haute-Savoie and the Ain instead: Swiss
salaries, French house prices.

Different pairs want different modes, which is why all three are switchable
without recomputing the columns.

Alongside the map the panel gives Pearson r, Spearman ρ on ranks, the number of
communes carrying both values, and a scatter plot with its fitted line — plus,
in the two z-score modes, the mean lines that divide the cloud into the
quadrants those maps are picking between. The scatter is not decoration: r alone
cannot tell a cloud from a wedge from one outlier doing all the work, and the
panel says out loud that communes are not people and that neighbouring communes
are not independent observations.

The whole thing runs in the browser. Selecting a pair fetches two ~150 KB
columns; the neighbourhood pass over 34,746 communes takes under a tenth of a
second. The selection lives in the URL alongside the view and the layer stack,
so a correlation is a link someone else can open.

## How it fits together

```
data/raw/<source_id>/     downloaded originals — immutable, never edited
sources/<source_id>/      source.yaml · fetch.py · transform.py · layer.json
pipeline/                 build.py · validate.py · tile.py
data/processed/           normalised outputs, EPSG:4326
site/                     the deployable static site
site/stats/               one JSON column per correlatable stat, plus an index
```

`python pipeline/build.py` runs every source's fetch and transform, validates
the results against the output contract, joins every commune table into the
commune geometry, tiles it, cuts the correlator's columns from that same joined
table, and writes `site/layers.json`.

The columns are cut from the joined table rather than read back off the map for
a reason: tippecanoe drops features at low zoom, so anything the page computed
from what was on screen would be computed from a biased sample of France. A
numeric field opts out of the correlator with `"correlate": false` in its
`layer.json`, which is right for a year or a grid resolution — numeric, but not
a statistic anyone should correlate.

A full rebuild takes about half an hour, and all but twenty-six seconds of it is
transforms re-streaming the same large inputs — 326 MB of DVF across 279 gzipped
files, 154 MB of Explore2, 150 MB of BPE twice over, 72 MB of GASPAR. Joining
and tiling all 34,746 communes is the cheap part. While working on one source,
use `--only <id>`, which re-runs that transform and reuses every other source's
existing `data/processed` output: that is the twenty-six seconds plus one
transform, not half an hour. DVF is the heaviest single source at about six
minutes, because ten million rows have to be grouped into mutations before any
of them can be divided. Then `site/` deploys
as-is to any static host that serves HTTP range requests (Cloudflare Pages,
Netlify, GitHub Pages) — PMTiles needs those and nothing else.

The browser never downloads a shapefile, never reprojects, and never parses a
government CSV. Every transform happens offline.

## Adding a source

Create `sources/<id>/` with four files:

| File | Job |
|---|---|
| `source.yaml` | name, attribution, url, `fetched` date, `source_crs`, `kind`, `fetch: auto\|manual\|none` |
| `fetch.py` | `def fetch(ctx, force=False)` — downloads into `data/raw/<id>/`, or prints instructions for a manual source |
| `transform.py` | `def transform(ctx)` — reads raw, writes `ctx.out_path` |
| `layer.json` | how to draw it: type, colour scale, breaks, legend note, declared fields |

`kind` picks one of four patterns:

- **`commune_table`** → a CSV with a `code_insee` column and one or more value
  columns. Joined into the commune geometry at build time and drawn as a
  choropleth. Worked examples: `commune_reference`, `vigieau_secheresse`.
  `meteo_chaleur` is the awkward case: its source is a point grid rather than a
  commune table, so the transform does the nearest-point assignment itself and
  emits a commune table like everything else.
- **`polygon`** / **`point`** / **`line`** → GeoJSON in EPSG:4326. Worked
  examples: `parcs_naturels` (WFS in Lambert-93, reprojected), `hubeau_piezo`
  (REST API), `rivieres` (a paged WFS pull of 250k line segments),
  `voies_ferrees` (three SNCF exports joined on the line code),
  `projets_ferroviaires` (OpenStreetMap via Overpass, clipped to France against
  the commune geometry).
  `rivieres` is also where a layer decides its own zoom tiering: the transform
  writes a per-feature `tippecanoe: {minzoom}` member, so major rivers survive at
  zoom 4 and 5 m streams appear at zoom 8, rather than leaving
  `--drop-densest-as-needed` to thin the Loire and a Breton stream alike.

A source can also be a pure input with no layer of its own — `fetch` plus
`no_output: true` — when several transforms need the same large download:
`insee_carreaux_200m` (where people live, read through `pipeline/population.py`)
and `copernicus_dem` (terrain, read through `pipeline/elevation.py`).

A source can also skip local data entirely and point a layer at a remote
PMTiles archive with `source_url_tiles` — see `rga_argiles`, which reads the
122 MB clay-exposure tileset that Géorisques already publishes on data.gouv.fr.

Every value column must be declared in `layer.json` with a `type`
(`numeric` / `ordinal` / `categorical`) and a `unit`. `validate.py` rejects
anything that is not.

## What validation enforces

`pipeline/validate.py` exits non-zero on any of these, before anything is tiled:

- output is EPSG:4326 in lon/lat order — Lambert-93 metres fail instantly
- INSEE codes are zero-padded 5-character strings, `2A`/`2B` included, no duplicates
- geometries are valid (`make_valid` applied) and the right type
- the bounding box stays inside metropolitan France unless the source declares otherwise
- every value column is declared with a type and a unit, and every declared field exists
- commune-table column names are globally unique — they share one namespace after the join
- **join rate**: rows in source, rows matched, rows unmatched, and a sample of
  unmatched codes, printed for every table, with a configurable floor
  (`min_join_rate`). A silent 8% join failure is the likeliest bug in a project
  like this, so it is the loudest thing the build prints.

## Manual sources

`fetch: manual` sources print what to download and where to put it, then stop —
no scraping behind forms, no faked sessions. `sources/drias_chaleur/` is the
worked example: it is `optional: true`, so the build completes without it and
tells you what is missing.

It also shows what to do while a manual source is outstanding. Rather than leave
heat unmapped, `sources/meteo_chaleur/` takes the same indicators from the
Aladin-Climat files Météo-France publishes openly and ships them today. The
manual route stays in the tree because it is still strictly better — it has the
days-above-35 °C count and the newer model generation — but it is now an upgrade
rather than a blocker.

## Known data gaps

Two things asked of this map cannot currently be drawn, and the reason is the
same in both cases: the data is not published as open vector geometry.

- **Aircraft noise contours.** The DGAC publishes a national index of the 224
  aerodromes covered by a *plan d'exposition au bruit*, which is what
  `aerodromes` carries — so the map can say which airports impose legal noise
  zoning and link the decree. The PEB's A/B/C/D contour polygons are issued
  per-department by each DDT, across 122 separate data.gouv datasets in
  inconsistent formats, with no national layer. Assembling them is a real
  project, not a source adapter.
- **Flight paths.** Approach and departure procedures are published by the SIA
  as PDF charts in the AIP, not as geometry, and live ADS-B tracks are neither
  static nor open under a usable licence. Where a department does publish its
  PEB, that contour *is* the mapped footprint of the flight paths — it is
  modelled from real approach and departure tracks — which makes the PEB the
  thing worth chasing rather than the tracks.

`projets_ferroviaires` is the only source here that is not official. SNCF Réseau
publishes no open geodata for its project pipeline, so planned and
under-construction lines come from OpenStreetMap. The layer separates
`en travaux` from `projet` for that reason: the first has physical work to
survey, the second is somebody's drawing of a published scheme.

## Frontend

`site/app.js` is plain ES modules, no bundler, no npm. It reads
`site/layers.json` and does seven things: initialise the map, build the layer
panel, keep the active layers stacked in the order the user chose, generate the
legend from each layer's `paint` block, answer a click with every loaded value
at that point grouped by source and attribution, run the correlator, and encode
`#lat/lon/zoom/layers/correlation` in the URL.

`site/correlate.js` is the correlator's arithmetic and nothing else — Pearson,
Spearman, a uniform grid for the neighbourhood search, and the local-r pass. No
DOM, no map, no fetch, so it can be read and tested on its own.

The correlation result is drawn by a layer called `__correlation` that no source
published. It is a manifest entry in every other respect, which is what buys it
the stack, the opacity slider, the in-row colour scale, the legend, the
attribution line and the shareable hash without a special case in any of them.
Its values reach the map through `setFeatureState` keyed on the INSEE code,
promoted to a feature id on the commune source, so changing a dropdown never
re-parses the style.

Libraries are vendored and version-pinned in `site/lib/`: MapLibre GL JS 5.6.0
and pmtiles 4.3.0. `app.js` is ~1,500 lines of plain JavaScript and
`correlate.js` ~240; there is no build step for the frontend and nothing is
minified, so dev tools show you the real source.

Local preview must use `pipeline/serve.py`, not `python -m http.server` — the
standard library server ignores `Range`, and PMTiles depends on it. See
`DEPLOY.md`.

## Gotchas this pipeline already handles

Latin-1 and CP1252 encodings, `;` separators with `,` decimal marks, INSEE codes
read as integers, Corsica's `2A`/`2B`, shapefile 10-character field truncation,
invalid geometries that crash tippecanoe, overseas territories silently
destroying the bounding box, and Shapely simplification opening gaps between
neighbouring communes (mapshaper is used instead — it is topology-aware).

Four more that the election and equipment sources ran into:

- **A commune key that is only half a key.** The Interior Ministry's commune-level
  files carry `Code de la commune` as a three-digit slice; the INSEE code is the
  department code concatenated with it. Read on its own it matches nothing.
- **Repeating column blocks.** Candidate and list results repeat in fixed-width
  blocks to the end of the row — seven columns per candidate, thirteen per
  municipal list — so the row length decides how many there are, and a fixed
  column map breaks between a twelve-candidate first round and a two-candidate
  second.
- **Absence that means zero.** The BPE only publishes rows for facilities that
  exist, so a commune with no food shop is missing rather than zero. The
  transform starts from the commune list and fills the gaps, which is what turns
  18,669 silent absences into a mapped class.
- **Absence that does not mean zero.** BDIFF reports an unknown fire count for
  Paris and the inner suburbs, so those communes stay blank instead of being
  drawn as "no fires". Same-looking hole in the data, opposite correct treatment.

Four more from the hazard and services sources:

- **An index that is not what its name suggests.** The Aladin files carry
  "heatwave days" and "abnormally hot days", both defined against *local*
  percentiles. Briançon scores 39 heatwave days to Nice's 8 — correct as
  defined, and a lie on any colour ramp where darker reads as hotter. Only
  absolute thresholds (nights above 20 °C, days above 25 °C) are mapped.
- **Near-universal presence.** 34,455 of 34,746 communes have had at least one
  flood declared a natural disaster since 1982, so the binary "has flooded"
  carries no information and the map uses the count. Check the base rate before
  choosing between a flag and a measure.
- **Point data on an 8 km grid, communes averaging 15 km².** Most communes
  contain no climate grid point at all, so values are assigned by nearest point
  and neighbouring communes often share one. The distance is written to the
  output rather than hidden — Belle-Île is 35 km from the nearest land point.
- **A slow server is not a broken one.** files.georisques.fr serves the 8 MB
  GASPAR export at around 20 KB/s. The fetch passes a 900-second timeout instead
  of retrying into the same wall; a truncated download of the Aladin RCP4.5 file
  was caught the same way, by checking the byte count against what the server
  advertised. The opposite mistake is just as easy: DVF's per-department files
  are 1 MB, so a 300-second socket timeout means five minutes sitting on a dead
  connection before the first retry. Size the timeout to the file.

And four from air quality:

- **A commune average is the wrong average for exposure.** It counts forest,
  motorway verge and mountain top as much as the village. `eea_qualite_air`
  weights the EEA's 1 km cells by the INSEE 200 m population grid, so the value
  is what the commune's residents breathe; `eea_qualite_air_grille` publishes
  the unaveraged 1 km squares for reading a single spot. Ozone shows why both
  exist: it peaks on empty high ground, so the grid runs well above the
  population-weighted commune value in the mountains.
- **One year is weather, not place.** The national median SOMO35 rose by half
  from 2021 to 2022 on the hot summer alone, and PM2.5 fell a fifth from 2022 to
  2024, partly a wet 2024. Four validated years (2021–2024) are averaged, and
  each commune carries its cleanest and dirtiest year so the spread is visible.
  2020 is left out because lockdown depressed NO₂.
- **The same share, two speeds.** The EEA's public Nextcloud share serves the
  same GeoTIFF at ~20 KB/s through its download link and ~2 MB/s through its
  WebDAV endpoint (the share token as username). And on Python 3.9 a read that
  stalls mid-transfer raises `socket.timeout`, which is not yet a
  `TimeoutError`, so `download()` catches `OSError` and resumes from the
  partial file with a `Range` request instead of starting over.
- **A zip holding a 7z.** INSEE's 200 m grid is a `.7z` inside a `.zip`, which
  the standard library cannot open. `pipeline/population.py` streams the CSV
  out through `bsdtar` (libarchive: built into macOS, `libarchive-tools` on
  Linux).

And four from observed climate:

- **A grid cell's temperature is for its average altitude.** SAFRAN's 8 km
  cells in the Alps average far above the valley towns: at stations 300 m or
  more off their cell's mean altitude, raw summer highs were 5.6 °C out.
  `meteo_climat` computes each cell's mean altitude from
  the Copernicus 90 m terrain model and fits a correction, a + b × (height
  difference), on ~1,500 Météo-France stations, applied at the altitude of
  each populated 200 m cell. Cross-validated, that takes the error in summer
  highs from 2.6 °C to 0.7 °C.
- **Calibrate in the build, not once in a notebook.** The station records are a
  raw input like any other. Every build refits the correction, prints its
  cross-validated error, and fails if that error passes `max_cv_rmse`, so a
  new year of data or a change to the transform cannot quietly make it worse.
- **Thresholds go on the days, not the totals.** Days above 30 °C are counted
  after correcting each day's temperature, from per-cell tables of counts at
  0.25 °C shifts. Shifting the ten-year count afterwards would not be the same
  thing, and the raw grid undercounts hot days by 13 a year.
- **A download can end short without raising anything.** Two of the ten
  137 MB SAFRAN files were saved truncated: the server closed the connection,
  the read simply ended, and nothing raised. `download()` now compares the
  bytes received with Content-Length (or the Content-Range total when
  resuming) and resumes until they match. Every archive already in
  `data/raw` was checked with `gzip -t` / `unzip -t` after the fix; only the
  climate files had been affected.

And three from income and property prices:

- **The three biggest cities key differently from everywhere else.** DVF records
  Paris, Lyon and Marseille by *arrondissement municipal* (75101–75120), while
  ADMIN EXPRESS has only the parent commune (75056) and no arrondissement
  geometry at all. Join the two naively and France's three largest cities are
  silently blank. `dvf_prix` rolls arrondissements up to the parent and then
  asserts all three have a price, so the bug cannot come back quietly.
- **Suppression is not zero, and it is not blank either.** INSEE writes a
  literal `s` — *secret statistique* — where publishing would identify
  households. It bites unevenly: median income is published for 99.6% of the
  population but the inequality ratio for only 76.8%, and the poverty rate for
  73.0%. A grey commune on the inequality map is unpublished, not equal.
- **One sale is many rows.** A DVF mutation spans a row per parcel and per lot
  with `valeur_fonciere` repeated in full on each. Count rows and volumes
  inflate several-fold; average them and multi-parcel sales dominate. Group by
  `id_mutation` before doing anything else — and then discard the ~70% of
  mutations that are bare land, or bundle a house with land or outbuildings,
  because they have one price and no surface worth dividing by.

See `france-map-tool-plan.md` for the full brief, the source catalogue, and the
reasoning behind each of these.
