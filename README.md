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
| Sources | 36 built (one from a manual DRIAS order, six derived from the others), 2 fetch-only inputs (population grid, terrain) |
| Layers | 76, across all four patterns |
| Communes | 34,746 (metropolitan France, ADMIN EXPRESS millésime 2026) |
| `site/tiles/` | 255 MB — a 183 MB commune tileset, a 38 MB river and lake tileset, a 23 MB air quality grid, rail tilesets, small GeoJSONs |
| `site/stats/` | 22 MB — 154 correlatable commune columns plus a centroid index |
| `site/files/` | 25 MB — the monthly climate tables, one JSON per department, fetched on demand |
| Full rebuild from `data/raw` | ~35 min (139 s of it is join + tiling) |
| First fetch of everything | ~2 h, ~4.5 GB into `data/raw`, plus the DRIAS order (~215 MB) |

Layer groups: **Income & property** (median standard of living, income
inequality, poverty rate, price per m², price of a house), **Population &
access** (population, density, median age, share aged 65 and over, ageing ratio,
food access, food shops, everyday-services basket, service-provision class,
health services),
**Hazards** (drought days, worst restriction level, projected summer low flow at
+2.7 °C and at +4 °C, longer summer low water, river flood peaks at +2.7 °C,
tropical nights, days reaching 35 °C, dry-soil days and fire-weather days at
+2.7 °C (DRIAS TRACC-2023), wildfire risk at +2.7 °C (a model of large fires
calibrated on observed ones, below), summer days, area burned, fire count,
flood disaster declarations, flood PPR status, coastal hazard, clay
shrink–swell zones and the share of residents living on them), **Air quality** (PM2.5, NO₂ and ozone per commune, weighted by
where residents live, and the same three on the raw 1 km grid), **Climate
(observed)** (rainy days, solar energy and summer afternoon highs over
2016–2025, at residents' altitude and calibrated against Météo-France
stations; any commune's full month-by-month climate table opens from the
click panel), **Elections** (a left–right index over three elections, below; 2022 presidential
second round, first-round leader with every candidate's share, presidential
turnout; 2026 municipal turnout, lists standing, winning nuance), **Transport** (railway lines by type and by speed, stations by annual passengers,
rail under construction and proposed, airports, aircraft noise zoning),
**Protected areas**, **Water** (rivers and canals as lines with lakes and reservoirs filled beneath them, piezometers), **Reference**,
**Habitability 2050** (Wally's 2050 habitability score, below), **Culture**
(reach of labelled performing arts and music, of museums and contemporary art
and of festivals, a combined score, and the venues and festivals as points,
below), **Tourism** (tourist beds per resident, total beds, second homes, the
commercial share, how much of what is bookable shuts out of season, and the
department's measured hotel season, below).

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

Tourist beds per resident against price per m² is the third. Nationally the two
correlate *negatively*, which is Simpson's paradox and not a finding: the
tourism-heavy communes are overwhelmingly tiny rural and mountain ones, and the
rural–urban gradient swamps everything. Hold density constant and the expected
relationship appears sharply — among communes of similar density, median price
runs from about 1,540 €/m² where second homes are under a tenth of the housing
to about 3,740 €/m² where they are over half. It is a good illustration of why
the correlator needs a third variable held still before a pair means anything.

## The 2050 habitability score

`sources/score_habitabilite/` builds a habitability score for 2050 from the
other sources' outputs, following the method Projet Celsius published for its
own map (their note, not their data): 11 indicators in six equally weighted
categories — heat, fire, drought and clay, floods, coast, lack of services —
each turned into a 0–100 exposure, and the score is 100 minus the average of
the six. Every indicator, scale and direction is declared in its `source.yaml`.

Celsius scores its continuous indicators as positions among communes ("more
exposed than X% of them"). Wally's published score, `habitabilite_2050`, scores
each on its own scale instead, so a commune's score does not depend on the rest
of France. Open-ended indicators rise on a saturating curve that is half
exposed at a declared value — 20 tropical nights, 5 days at 35 °C, 2 large fires
per 1,000 km² per decade — and approaches full exposure beyond; the services
basket, register counts and the clay class are scaled linearly. The median
commune scores 63, the Aude and Roussillon coast about 25, inland Normandy,
Picardy and Artois about 91. The positional score is kept, for comparison with
Celsius, as `habitabilite_2050_pos` (a folded layer); the two rank communes
almost alike (rank correlation 0.95), and Mont-de-Marsan scores 73 on the
published score against 63 on the positional one.

Every build prints Celsius's worked example next to Wally's positional
indicators: for Mont-de-Marsan the heat, drought and flood indicators land
within a point or two of theirs.
Fire follows the model Celsius adopted in September 2026, rebuilt in
`sources/feux_modele/`: a Poisson regression that predicts fires of 10 ha or
more per commune. Its inputs are the large-fire record of communes within 20 km
(the commune itself excluded), fire-weather days, the fire regime, windy
fire-season days and population density. It learns from BDIFF 2006–2021 and is
then tested on 2022–2025, seasons it never saw. On 2022 it ranks the communes hit
with an AUC of 0.72, against 0.67 for fire weather alone (0.80 against 0.68 in
2025), and the build fails if that margin shrinks below 0.02. The published
model is refitted on 2006–2025. The step to 2050 multiplies by the rise in
fire-weather days from +2.0 °C to +2.7 °C, an extrapolation rather than a model
output. It does not see fuel, firefighting or vegetation change.

The model moves the Landes from the 18th to the 68th percentile and the Nord
from the 35th to the 13th, the same direction as Celsius's own revision.
Mont-de-Marsan rises from 26 to 61, still short of Celsius's 82: few large fires
have started within 20 km of it. Services are the other deliberate difference:
they use Wally's everyday-services basket rather than facilities per 1,000
residents. The curves' half-exposure values are a judgement, tuned by eye
against the data, and a quarter of the score rests on administrative flood and
coastal registers.

## The left–right index

`sources/gauche_droite/` places every commune on one left–right axis, 0 (far
left) to 10 (far right), averaged over three first-round national elections:
the 2022 presidential, the 2024 European and the 2024 legislative. On each
ballot every candidate or list sits at their party's position in the Chapel
Hill Expert Survey (`lrgen`, the mean of its 2019 and 2024 waves, read from the
CHES trend file at build time); a commune's index for one election is its
voters' mean position, and the published index is the mean of the three.
Communes with few voters are shrunk toward their department (empirical Bayes)
so a single family cannot colour a hamlet. The popup has each election's own
index, the unshrunk mean, average bloc shares and the spread of positions (a
mean hides polarisation).

Every option on every ballot is mapped in `source.yaml`, and an option the file
has that the mapping lacks fails the build. The NFP's single candidates sit at
the mean of its four parties, Ciotti's UXD between LR and RN; the Ministry's
catch-all nuances (divers gauche, divers droite …) at the nearest rated party;
regionalists, miscellaneous lists and Lassalle, whom CHES never rated, are
reported as unplaced. A short `overrides:` list moves a legislative candidate
whose nuance contradicts the alliance they stood for (one so far: Falorni, filed
DVG but backed by Ensemble).

Why three: the exact positions hardly matter — swapping survey waves or using a
crude left −1 / centre 0 / right +1 coding ranks communes with a correlation of
0.99 — but each ballot has its own distortion. The presidential vote follows
candidates, the European vote is the purest party vote on a low turnout, and
the legislative one depends on who stood where. The presidential and European
indexes agree at r = 0.94; the legislative agrees with each at 0.88, the gap
being two-way NFP–RN races with no centrist on the ballot and strong divers
droite incumbents, exactly the effects averaging is meant to absorb. The 2017
presidential first round would be the next election to add. A second axis
would also be honest: on CHES's economic scale the RN sits near Renaissance
(6.4), on the cultural GAL–TAN scale at 8.2.

## The culture scores

`sources/score_culture/` scores how much labelled culture each commune can
reach, in three scores kept apart because they add different things to a place:
performing arts and music, museums and contemporary art, and festivals. The
venues come from the Ministry of Culture's Basilic database
(`sources/culture_lieux/`) and the festivals from its 2019 national map
(`sources/festivals/`); both are also layers of their own.

The Ministry's labels are the curation. Each label carries a weight in
`culture_lieux/source.yaml`: 10 for a national opera or theatre, 6 for a scène
nationale or centre dramatique national, 4 for a SMAC or a Zénith, 1.5 for a
city theatre. A Musée de France weighs 1 + log10(visitors / 10,000), so the
Louvre and a village museum do not count the same. Festivals have no attendance,
so their weight comes from the reach they declare and how long they have run.
Heritage, cinemas, libraries and bookshops are left out for now.

Every venue counts for the communes around it: fully on the spot, half at
15 km, a quarter at 30 km, nothing beyond 45 km, in straight lines. Each score is
0–100 on a fixed, saturating scale: `100 × (1 − e^(−reach / (anchor / 3)))`,
which reaches 95 at a per-category `anchor` in `source.yaml`, roughly a regional
capital's reach, and flattens beyond it. Beside each score is the distance to
the nearest major venue of that kind. The build logs a few reference towns:
Avignon and Aix score 98–99 on festivals, Arles 53 on the stage and 84 on
festivals, Mont-de-Marsan 31, 25 and 27. The labels follow public subsidy, so
private halls, clubs and galleries are missing, and a well-funded scène
nationale in a mid-sized town counts for more than a lively private scene.

The scores used to be positions among communes, which hid how much: Paris and
Lyon both scored about 99, although Paris reaches five times as much, and
Mont-de-Marsan scored 54 on the stage with almost exactly the median commune's
reach. A log scale was tried as well; it left the countryside around 50 and tied
2–3% of communes at 100, so the saturating one was kept.

## Tourism, and the one thing it cannot tell you

`sources/tourisme_capacite/` counts tourist beds per commune;
`sources/tourisme_saison/` says when the season is. Together they answer "how
much tourism is here, and when" — but not, anywhere, "how many people came".

**No commune-level visit count exists in French open data, and none is
estimated here.** INSEE's frequentation survey is the only source of nights and
arrivals, and it is a sample of establishments, so publishing it per commune
would expose individual hotels. It stops at the department: 134,485 department
rows, 0 commune rows. Taxe de séjour looks like the obvious way round it, but
DGFiP's DELTA dataset publishes only the tariffs communes *set*, never the
receipts they collect. It would be easy to multiply beds by a departmental
occupancy rate and call the result "annual visits"; that number would add
nothing to the bed count while laundering a department figure into something
that looks commune-level, so it is deliberately not built.

What is commune-level is **capacity**, which is a census of establishments
rather than a survey and is published for all 34,746 communes with nothing
suppressed. INSEE counts rooms, pitches and dwellings, not beds, so the
conventional observatory coefficients turn them into one comparable number — a
hotel room is 2 beds, a camping pitch 3, a second home 5 — declared in
`tourisme_capacite/source.yaml` so they can be argued with. Beds per resident is
the number worth reading: above 1 the commune's water, roads, waste and shops
are sized for a population it only has for part of the year. The median commune
is at 0.21; about 17% are above 1. The extremes are all real and recognisable —
Germ in the Pyrenees at 179 beds per resident, Le Mont-Saint-Michel at 46 with
91% of it commercial, Bonnal at 42 because one of France's largest campsites is
there.

**Second homes are most of the capacity and the shakiest part of it.** In
Brétignolles-sur-Mer and Chamonix they are about two-thirds of the beds, so
omitting them would miss the point, but the census counts dwellings and the
5-beds-each coefficient is a convention rather than a measurement. It is also
clearly too generous in cities: a Paris pied-à-terre is not five beds, which is
why Paris reads 891,000 beds. `lits_marchands` is the firmer half if you want to
avoid the assumption entirely.

**Seasonality is two columns, on purpose.** The measured part is departmental:
INSEE publishes a full monthly series of nights for *hotels only*, so
`saison_hotel_*` carries the department's curve, averaged over 2022–2025 and
labelled as departmental. It is genuinely informative — Savoie takes 60% of its
hotel year in December–March and peaks in February, Vendée peaks in August,
Paris sits within a point or two of flat all year, and Haute-Savoie is honestly
twin-peaked. The local part is structural: `part_lits_saisonniers` is the share
of a commune's *bookable* beds that are campsite pitches, which in France means
beds that physically shut out of season. That is a fact about the building
stock, which is exactly why it can be stated per commune when nights cannot.

The two are never multiplied together. An earlier version of this source blended
them into a single commune-level monthly curve, and it was circular: campsites
have no published monthly series anywhere, so their shape had to be assumed, and
for the 42% of communes whose beds are mostly campsites the output was
determined entirely by that assumption — all 14,219 of them came out "Summer",
with a standard deviation of 4.7 points. A column that restates its own input
while looking like a measurement is worse than two honest columns, so the blend
was removed. Read them together and the answer is still there:
Brétignolles-sur-Mer is 95% seasonal beds in an August-peaking department;
Chamonix is 28% seasonal in a twin-peaked one, which is to say it trades all
year.

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

## The optimiser

"Find matching areas" turns the same columns into a shortlist. Add the
statistics that matter; for each, say whether more or less is better, give it a
weight from 1 to 5, and optionally a ramp — the value that is unacceptable and
the value that is ideal. With a ramp a statistic scores 0 at the unacceptable
end, 1 at the ideal end, and a straight line between. With no ramp it scores on
its own scale (below).

The scores are combined as a weighted geometric mean — multiplied, not averaged
— so a commune at the unacceptable end of any one ramp is ruled out however well
it does on the rest, each criterion added makes a high score harder to reach,
and a commune missing any of the chosen statistics is left unscored rather than
guessed at. The map shades each commune by its score on a fixed 0–1 ramp, with
the legend cut off at the best score reached and a summary that says when
nothing reaches 0.5, so a pale map means there is no good match. The panel lists
the top ten (click to fly there), and clicking a commune shows each value, the
score it earned, and its rank. The criteria live in the URL like the
correlation does.

### Absolute and rank scoring

**Absolute**, the default, scores a statistic without a ramp on its own scale:
the natural range a field declares with `"abs": [lo, hi]` in its `layer.json`
(0–100 for a score, 0–10 for the left–right index, 0–20 for the services
basket), or, where it declares none, a line between its 1st and 99th
percentile values. That line is drawn on a log scale when the column is heavily
skewed (upper tail more than three times the lower, no negatives: density,
prices, population, fire rates), since a straight one would put nearly every
commune at the cheap or sparse end. A field published as a position can point at
the same thing on an absolute scale with `"abs_column"`; none needs to now,
since the habitability and culture scores are published on absolute scales.

**Rank**, the original method, scores a statistic without a ramp by its
percentile among communes instead, and shades the map by rank class (top 1%,
5%, 10%, 25%, 50%, the rest). Percentiles spread every statistic evenly from 0
to 1, so some commune always scores near 1 even when nothing fits well. A link
ending in `/rank` opens in Rank; any other opens in Absolute.

`make scoring-report` runs the criteria sets in `pipeline/scoring_scenarios.json`
through both methods, using `site/optimise.js` itself. For each method it prints
the best and typical scores, the top ten, how far the top 100 overlap, and
reference towns. It also sets the culture scores beside the positions they
replaced, and the published habitability score beside the positional one.
`make test` runs the optimiser's unit tests.

## How it fits together

```
data/raw/<source_id>/     downloaded originals — immutable, never edited
sources/<source_id>/      source.yaml · fetch.py · transform.py · layer.json
pipeline/                 build.py · validate.py · tile.py
data/processed/           normalised outputs, EPSG:4326
site/                     the deployable static site
site/stats/               one JSON column per correlatable stat, plus an index
site/files/<name>/        static files a source publishes for the page to fetch on demand
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

`"detail": true` on a layer folds it away under its group's **Show N more**
link: still listed, just not by default. It is set on the indicators behind the
habitability score (grouped under it), secondary cuts of a statistic and the
point layers behind a score. A detail layer that is switched on, from the
list or a link, stays in view.

`"panel": false` on a layer keeps it out of the layer list without removing
anything: its fields still show in the inspect panel, its stats still feed the
correlator and optimiser, and a shared link naming it still turns it on. It is
set on layers that repeat another — the DRIAS-2014 heat runs superseded by the
TRACC ones, class cuts of a count, the km² air grids, line speed.

`kind` picks one of four patterns:

- **`commune_table`** → a CSV with a `code_insee` column and one or more value
  columns. Joined into the commune geometry at build time and drawn as a
  choropleth. Worked examples: `commune_reference`, `vigieau_secheresse`.
  `meteo_chaleur` is the awkward case: its source is a point grid rather than a
  commune table, so the transform does the nearest-point assignment itself and
  emits a commune table like everything else.
- **`polygon`** / **`point`** / **`line`** → GeoJSON in EPSG:4326. Worked
  examples: `parcs_naturels` (WFS in Lambert-93, reprojected), `hubeau_piezo`
  (REST API), `rivieres` (a paged WFS pull of 250k line segments, plus 94k lake and
  reservoir polygons from a second table — a line layer's `paint.areas` fills a
  source's polygons beneath its lines under the same switch, and
  `also_geometry` in source.yaml lets validation accept them),
  `voies_ferrees` (three SNCF exports joined on the line code),
  `projets_ferroviaires` (OpenStreetMap via Overpass, clipped to France against
  the commune geometry).
  `rivieres` is also where a layer decides its own zoom tiering: the transform
  writes a per-feature `tippecanoe: {minzoom}` member from `zoom_tiers` in
  source.yaml, rather than leaving `--drop-densest-as-needed` to thin the Loire
  and a Breton stream alike. Every tier is currently 4, so the whole network is
  drawn with all of France in view.

A source can also be a pure input with no layer of its own — `fetch` plus
`no_output: true` — when several transforms need the same large download:
`insee_carreaux_200m` (where people live, read through `pipeline/population.py`)
and `copernicus_dem` (terrain, read through `pipeline/elevation.py`).

A source can be built from other sources' outputs instead of raw files: list
them under `inputs:` in `source.yaml` and read each with
`ctx.processed(source_id)`. `--only` then re-runs the dependent whenever it
re-runs one of its inputs, and the build skips it if an input failed, so it can
never describe an older version of them. `score_habitabilite` is the example.

A source can publish static files beside its layers with `site_files: <name>`:
its transform writes them to `ctx.files_dir`, including an `index.json`, and
the build copies them to `site/files/<name>/` and lists them under `files` in
`site/layers.json`. This is for data read for one place at a time and too big
to carry in every tile — `meteo_climat`'s month-by-month climate tables,
twelve rows of thirteen values for every commune, one file per department.

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

DRIAS needs a personal account, so its archives are ordered by hand and dropped
in `data/raw/drias_chaleur/`; the transform reads them straight out of the tar
files. The export has three defects — day counts written as pandas time spans,
the dry-soil files stacked as a values table above a coordinates table, and
median files headed "MAX" — which the transform repairs only after checking
them (land masks position for position, min ≤ median ≤ max at every point).
At the grid point containing Mont-de-Marsan its three values match those in
Projet Celsius's methodology note exactly.

`sources/meteo_chaleur/` is what shipped while that order was outstanding: the
same families of indicator from the older Aladin-Climat files Météo-France
publishes openly. It is kept for its 1976–2005 reference and RCP4.5 comparison.

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

A third gap is not about geometry: **how many tourists actually visit a
commune** is not published by anyone, because INSEE's frequentation survey is a
sample of establishments and commune figures would expose individual hotels. It
stops at the department, and taxe de séjour receipts are not published either.
`tourisme_capacite` maps beds instead and says so; the reasoning is under
"Tourism, and the one thing it cannot tell you" above. Airbnb and other
unclassified furnished lets are missing from the bed count for a related reason
— they appear in no open register — so a commune whose visitors mostly arrive
through a platform is under-counted.

`projets_ferroviaires` is the only source here that is not official. SNCF Réseau
publishes no open geodata for its project pipeline, so planned and
under-construction lines come from OpenStreetMap. The layer separates
`en travaux` from `projet` for that reason: the first has physical work to
survey, the second is somebody's drawing of a published scheme.

## Frontend

`site/app.js` is plain ES modules, no bundler, no npm. It reads
`site/layers.json` and does eight things: initialise the map, build the layer
panel, keep the active layers stacked in the order the user chose, generate the
legend from each layer's `paint` block, answer a click with every loaded value
at that point grouped by source and attribution (the chosen commune outlined on
the map, and a commune search pinned with its name above the values), run the correlator and the
optimiser, open a commune's climate table (laid out like Wikipedia's, fetched per
department from `site/files/climate/`), and encode
`#lat/lon/zoom/layers/correlation/optimiser` in the URL.

`site/correlate.js` is the correlator's arithmetic and nothing else — Pearson,
Spearman, a uniform grid for the neighbourhood search, and the local-r pass. No
DOM, no map, no fetch, so it can be read and tested on its own. `site/optimise.js`
is the same for the optimiser: percentile and ramp desirabilities, the weighted
geometric mean, and the rank classes.

The correlation result is drawn by a layer called `__correlation` that no source
published. It is a manifest entry in every other respect, which is what buys it
the stack, the opacity slider, the in-row colour scale, the legend, the
attribution line and the shareable hash without a special case in any of them.
Its values reach the map through `setFeatureState` keyed on the INSEE code,
promoted to a feature id on the commune source, so changing a dropdown never
re-parses the style. The optimiser's `__optimiser` layer works the same way on the
same source, under its own feature-state key, so the two can be on the map
together without either clearing the other.

Basemaps and the overlay chips are declared in `pipeline/basemaps.json` and
`pipeline/overlays.json`, not in code: IGN Plan, grayscale, IGN satellite,
OpenTopoMap (topographic, to zoom 17), OpenStreetMap and "No map"; the cadastre
from zoom 14 and IGN contour lines from zoom 11 stack over any of them.

The view a visitor with no link lands on is `pipeline/start.json`: the layers
switched on (bottom of the stack first, spelled as in a shared link), their
opacities, the optimiser's criteria, and the "Top items" that open the layer
list and every statistic menu. It overrides each layer's `default_visible`; a
link overrides it in turn.

Libraries are vendored and version-pinned in `site/lib/`: MapLibre GL JS 5.6.0
and pmtiles 4.3.0. `app.js` is ~2,700 lines of plain JavaScript,
`correlate.js` ~250 and `optimise.js` ~150; there is no build step for the frontend and nothing is
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

Three from the tourism sources:

- **A cube whose totals are a dimension, not a row.** INSEE's Melodi cubes carry
  every breakdown and the total in the same column, so filtering for communes
  and summing is wrong twice over: the sub-breakdowns double-count, and the
  "total" must be selected as `_T` on *every* dimension at once. The census
  file has eight such dimensions beside the one being read, so
  `tourisme_capacite` derives them from the header rather than naming them, and
  keeps working when INSEE adds a ninth.
- **Nights split by visitor origin.** The frequentation cube publishes resident
  and non-resident nights alongside the total, all in the same measure. Adding
  the rows up gives double the year.
- **A large download that the server cuts.** INSEE's Melodi file endpoint drops
  the 98 MB census transfer roughly every 30 MB. `download` resumes with a
  ranged request and gets there, but note that its resume only works *within*
  one call: a `.part` left by a killed run is deleted on the next attempt, by
  design, so an interrupted fetch of that file restarts from zero.

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

And five from observed climate:

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
- **The climate table is calibrated month by month.** Lapse rates and SAFRAN's
  bias change with the season, so the table's temperatures and humidity get
  their own correction per month, and the extremes (mean maximum, mean minimum)
  a further one fitted on stations' hottest and coldest days. Snowy days are
  not SAFRAN's own snowfall, which splits rain from snow at the cell's mean
  altitude and in the Alps counted ~70% more snowy days than valley stations
  logged: they are wet days whose low, corrected to residents' altitude,
  reaches 0 °C. The build prints each row's error against stations and the
  page shows it under the table.
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
