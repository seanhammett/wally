# Deploying

`site/` is the whole deployable artefact. It needs a static host that answers
**HTTP range requests** — PMTiles reads slices of `communes.pmtiles` rather than
downloading all 68 MB. Cloudflare Pages, Netlify and GitHub Pages all do.

```bash
python pipeline/build.py          # regenerates site/ from data/raw
npx wrangler pages deploy site    # or: netlify deploy --dir=site --prod
```

Nothing in `site/` is generated at runtime, and there is no backend, no API key
and no database — the correlator included, which computes in the browser from
the plain columns in `site/stats/`. The only network calls the page makes are
for the IGN basemap tiles, its own files, and the one remote PMTiles archive
that Géorisques hosts.

## Local preview

```bash
python pipeline/serve.py --port 8000
```

Use this rather than `python -m http.server`: the standard library server
ignores `Range` and returns the whole file with a 200, so the commune tileset
downloads in full before anything draws.

## Cache headers

`site/tiles/*` are content-addressed by the build, not by filename, so serve
them with a short max-age (or revalidate) rather than immutable caching —
otherwise a rebuild leaves viewers on stale tiles. `layers.json` is fetched with
a cache-busting query string by `app.js` already. `site/stats/*` and
`site/files/*` follow the same rule as the tiles: they are rewritten by every
build, so no immutable caching.

Serve `site/stats/*.json` and `site/files/**/*.json` gzipped if the host lets
you choose — they are digit strings and compress to roughly a fifth of their
size.

## Size budget

| File | Size |
|---|---|
| `tiles/communes.pmtiles` | ~68 MB (34,746 communes, z4–11, all commune tables joined in) |
| `tiles/parcs_naturels.geojson` | ~1.7 MB |
| `tiles/hubeau_piezo.geojson` | ~1.5 MB |
| `stats/` (correlator columns) | ~6.7 MB total; the page fetches `index.json` (1.4 MB) plus two ~150 KB columns |
| `files/climate/` (climate tables) | ~25 MB total; opening a table fetches `index.json` (4 KB) and one department file, at most 660 KB (130 KB gzipped) |
| `lib/` (MapLibre + pmtiles) | ~1 MB |
| clay exposure | 0 — read from data.gouv.fr over range requests |

Adding commune tables costs almost nothing: they are attributes on a tileset
that already exists. Adding a new polygon layer is what grows the deploy.
