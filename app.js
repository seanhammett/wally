// France data overlay — the whole frontend.
//
// It does eight things: initialise the map, build the layer panel from
// layers.json, keep the active layers stacked in the order the user chose,
// generate the legend, answer clicks with every loaded value at that point, run
// the correlator and the optimiser, open a commune's climate table, and keep all
// of that in the URL hash. Adding a dataset never touches this file.
//
// Any number of layers can be on at once. `state.order` is the draw order,
// bottom first, and is the single source of truth for the map stack, the active
// list and the legend — so what the panel says is on top really is on top.

const FRANCE = { center: [2.45, 46.6], zoom: 5.2 };
// Metropolitan France, used to frame the first view around the layer panel.
const FRANCE_BOUNDS = [[-5.3, 41.2], [9.7, 51.2]];
const PANEL_W = 330;
const COMMUNE_SOURCE_ID = 'communes';

const state = {
  manifest: null,
  layers: [],            // manifest entries, as published
  order: [],             // layer ids, bottom of the stack first
  byId: new Map(),
  visible: new Set(),
  opacity: new Map(),
  basemap: null,
  overlays: new Set(),
  selected: null,        // INSEE code of the commune in the inspect pane, outlined on the map
  fieldIndex: new Map(), // property name -> { label, unit, layer }
  // The published commune stat columns, shared by the correlator and the
  // optimiser so a column either of them fetched is never fetched twice.
  stats: {
    fields: [],          // manifest stats.fields, in panel order
    byName: new Map(),
    index: null,         // { codes, names, dep, lon, lat, row }
    columns: new Map(),  // field name -> Float64Array, fetched once and kept
  },
  corr: {
    x: null, y: null,    // the two chosen stats
    mode: 'local',       // 'local' (r in a disc) | 'agree' (z-score product)
    radius: 25,          // km, for 'local'
    side: 'both',        // which end of the scale is highlighted
    result: null,        // { values, counts, radii, rows, global, spearman }
    focus: null,         // INSEE code ringed on the scatter, from the last click
    busy: false,
    error: null,
    run: 0,              // guards against a slow request overwriting a newer one
  },
  opt: {
    criteria: [],        // { name, dir: 'up'|'down', weight: 1–5, bad, ideal }, in panel order
    scoring: 'abs',      // 'abs' (each statistic on its natural scale) | 'rank' (percentile when no ramp)
    result: null,        // Optimise.combine output plus the per-criterion desirabilities
    busy: false,
    error: null,
    run: 0,
  },
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};

// ---------------------------------------------------------------- paint
// A layer's `paint` block is the single source of truth for both the map style
// and the legend, so the two can never disagree.

function colorExpression(paint) {
  const prop = paint.property;
  const noData = paint.no_data_color || '#e5e7eb';
  if (!prop || paint.scale === 'none') return paint.color || '#3b82f6';
  // Almost every layer colours by a value baked into the tiles. The correlator's
  // layer colours by one computed in the browser and written to feature state, so
  // the value is read from there instead — everything downstream, the legend and
  // the in-row scale included, is the same `paint` block either way.
  const value = paint.source === 'feature-state' ? ['feature-state', prop] : ['get', prop];

  if (paint.scale === 'numeric') {
    const step = ['step', ['to-number', value], paint.colors[0]];
    paint.breaks.forEach((b, i) => step.push(b, paint.colors[i + 1]));
    return ['case', ['==', ['typeof', value], 'number'], step, noData];
  }
  if (paint.scale === 'continuous') {
    const ramp = ['interpolate', ['linear'], ['to-number', value]];
    paint.stops.forEach(([at, color]) => ramp.push(at, color));
    return ['case', ['==', ['typeof', value], 'number'], ramp, noData];
  }
  // ordinal and categorical are both a match on the raw value
  const match = ['match', ['to-string', value]];
  paint.stops.forEach(([value_, color]) => match.push(String(value_), strengthRamp(paint, color)));
  match.push(noData);
  return match;
}

// A class colour on its own, or — when the paint block has a `strength` — a ramp
// from a neutral gray to that colour, driven by a second property on a log
// scale. A feature with no strength value sits at the gray end.
function strengthRamp(paint, color) {
  const s = paint.strength;
  if (!s) return color;
  const get = paint.source === 'feature-state' ? 'feature-state' : 'get';
  const x = ['ln', ['max', ['to-number', [get, s.property], s.from], s.from]];
  return ['interpolate', ['linear'], x,
    Math.log(s.from), mixHex(s.neutral, color, s.floor || 0),
    Math.log(s.to), color];
}

function mixHex(a, b, t) {
  const hex = (c) => [1, 3, 5].map((k) => parseInt(c.slice(k, k + 2), 16));
  const [x, y] = [hex(a), hex(b)];
  return `#${x.map((c, i) => Math.round(c + (y[i] - c) * t).toString(16).padStart(2, '0')).join('')}`;
}

// The legend's key to a strength ramp: one bar from the gray end to full colour.
// A label under a colour bar, at `pct` along it and never past either end: it
// slides back by its own width in proportion to how far along it sits, so the
// one at 0% starts at the bar's left edge and the one at 100% ends at its right.
function placeOnBar(label, pct, host) {
  const at = Math.max(0, Math.min(100, pct));
  label.style.left = `${at}%`;
  label.style.transform = `translateX(${-at}%)`;
  host.appendChild(label);
  return label;
}

function strengthBar(paint) {
  const s = paint.strength;
  const wrap = el('div', 'act-scale');
  const bar = el('div', 'act-bar act-gradient');
  bar.style.background = `linear-gradient(to right, ${s.neutral}, ${s.legend_color || '#555555'})`;
  const ticks = el('div', 'act-ticks');
  [[0, s.label_from || `${s.from}`], [1, s.label_to || `${s.to}`]].forEach(([at, text]) => {
    // Pinned to the ends rather than centred on them, so neither label runs
    // out past the bar.
    const tick = el('span', 'tick', text);
    tick.style[at ? 'right' : 'left'] = '0';
    tick.style.transform = 'none';
    ticks.appendChild(tick);
  });
  if (s.label) wrap.appendChild(el('div', 'act-unit', s.label));
  wrap.append(bar, ticks);
  return wrap;
}

// A paint block with a `no_data_label` names its no-data colour as one more
// entry — for a layer that paints the communes it has no value for rather than
// leaving them see-through.
function legendEntries(paint) {
  const entries = scaleEntries(paint);
  if (paint.no_data_label) entries.push({ color: paint.no_data_color, label: paint.no_data_label });
  return entries;
}

function scaleEntries(paint) {
  if (paint.scale === 'numeric') {
    const fmt = (n) => (n >= 10000 ? n.toLocaleString('en') : String(n));
    return paint.colors.map((color, i) => {
      const lo = i === 0 ? null : paint.breaks[i - 1];
      const hi = i < paint.breaks.length ? paint.breaks[i] : null;
      const label = lo === null ? `< ${fmt(hi)}` : hi === null ? `≥ ${fmt(lo)}` : `${fmt(lo)} – ${fmt(hi)}`;
      return { color, label };
    });
  }
  if (paint.scale === 'continuous') {
    // The stops past the best value reached are not on the map, so they are not
    // in the legend either; the best value itself closes it.
    const max = Number.isFinite(paint.max) ? paint.max : Infinity;
    const shown = paint.stops.filter(([at]) => at < max)
      .map(([at, color]) => ({ color, label: shortNumber(at, decimalsIn(paint.stops.map(([v]) => v))) }));
    if (Number.isFinite(paint.max)) {
      shown.push({ color: continuousColor(paint.stops, paint.max), label: `${shortNumber(paint.max, 2)} — best reached` });
    }
    return shown;
  }
  if (paint.stops) return paint.stops.map(([value, color, label]) => ({ color, label: label || String(value) }));
  return [{ color: paint.color || '#3b82f6', label: paint.legend_label || 'shown' }];
}

// ------------------------------------------------------------- map style
// Basemap tint is declared per basemap, not hard-coded: the grayscale option is
// the IGN Plan tiles with `saturation: -1`, which is why it needs no separate
// tile provider and no API key.
// Property → the renderer's own default, so switching away from grayscale puts
// the colour back rather than relying on setPaintProperty(…, undefined).
const RASTER_DEFAULTS = {
  saturation: 0, contrast: 0, 'brightness-min': 0, 'brightness-max': 1, 'hue-rotate': 0, opacity: 1,
};

function rasterPaint(b) {
  const declared = (b && b.raster) || {};
  const paint = {};
  for (const [key, fallback] of Object.entries(RASTER_DEFAULTS)) {
    paint[`raster-${key}`] = key in declared ? declared[key] : fallback;
  }
  return paint;
}

// A basemap without `tiles` is the "No map" option: the raster is hidden and an
// outline of France (land fill below the data, border line above it) stands in,
// with city dots and names on top of everything.
const OUTLINE = { source: 'france-outline', land: 'france-land', line: 'france-line', cities: 'france-cities' };
const BACKGROUND = { tiled: '#eef2f5', bare: '#dde5ec' };
// Which towns are named at which zoom: [minzoom, smallest population]. The
// biggest band goes on top, and MapLibre places the top layer's labels first, so
// when names collide it is the smaller town that gives way.
const CITY_BANDS = [[7, 10000], [6, 20000], [5, 50000], [0, 100000]];
const cityLayerIds = () => CITY_BANDS.map(([, pop]) => `${OUTLINE.cities}-${pop}`);
const bareLayerIds = () => [OUTLINE.land, OUTLINE.line, ...cityLayerIds()];

function rasterStyle(basemaps) {
  const chosen = basemaps.find((x) => x.id === state.basemap) || basemaps[0];
  // The raster source needs tiles even while hidden, so a tile-less choice
  // borrows the first tiled basemap for it.
  const b = chosen.tiles ? chosen : basemaps.find((x) => x.tiles);
  return {
    version: 8,
    glyphs: 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf',
    sources: { basemap: { type: 'raster', tiles: [b.tiles], tileSize: 256, attribution: b.attribution, maxzoom: b.maxzoom || 19 } },
    layers: [
      { id: 'background', type: 'background', paint: { 'background-color': chosen.tiles ? BACKGROUND.tiled : BACKGROUND.bare } },
      // Hidden from the start under "No map", so its tiles are never requested.
      { id: 'basemap', type: 'raster', source: 'basemap', paint: rasterPaint(b),
        layout: { visibility: chosen.tiles ? 'visible' : 'none' } },
    ],
  };
}

function applyBasemapPaint(map, b) {
  // Every property is written every time, so switching away from grayscale
  // clears the desaturation instead of leaving it on the next basemap.
  for (const [key, value] of Object.entries(rasterPaint(b))) {
    map.setPaintProperty('basemap', key, value);
  }
}

// Added the first time "No map" is picked, so nobody else fetches the files.
function addOutline(map, b) {
  if (map.getSource(OUTLINE.source)) return;
  map.addSource(OUTLINE.source, { type: 'geojson', data: b.outline, attribution: b.attribution });
  const style = map.getStyle().layers;
  const after = style[style.findIndex((l) => l.id === 'basemap') + 1];
  map.addLayer({ id: OUTLINE.land, type: 'fill', source: OUTLINE.source,
                 paint: { 'fill-color': '#fbfbfa' } }, after && after.id);
  map.addLayer({ id: OUTLINE.line, type: 'line', source: OUTLINE.source,
                 paint: { 'line-color': '#475569', 'line-width': ['interpolate', ['linear'], ['zoom'], 4, 0.8, 10, 1.6] } });
  if (b.cities) addCities(map, b.cities);
}

// The dot is the symbol's icon rather than a separate circle layer, so a dot and
// its name are placed or dropped together — never a dot left without a name.
function cityDot() {
  const size = 16;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext('2d');
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, size / 2 - 2.5, 0, Math.PI * 2);
  ctx.fillStyle = '#1f2937';
  ctx.fill();
  ctx.lineWidth = 2.5;
  ctx.strokeStyle = '#ffffff';
  ctx.stroke();
  return ctx.getImageData(0, 0, size, size);
}

function addCities(map, url) {
  map.addSource(OUTLINE.cities, { type: 'geojson', data: url });
  if (!map.hasImage('city-dot')) map.addImage('city-dot', cityDot(), { pixelRatio: 2 });
  const pop = ['get', 'population'];
  CITY_BANDS.forEach(([minzoom, least], i) => {
    const next = CITY_BANDS[i + 1];
    map.addLayer({
      id: `${OUTLINE.cities}-${least}`, type: 'symbol', source: OUTLINE.cities, minzoom,
      filter: next ? ['all', ['>=', pop, least], ['<', pop, next[1]]] : ['>=', pop, least],
      layout: {
        'icon-image': 'city-dot',
        'icon-size': ['step', pop, 0.75, 50000, 0.9, 200000, 1.1],
        'text-field': ['get', 'nom'],
        'text-font': ['Open Sans Semibold'],
        'text-size': ['step', pop, 10, 100000, 11, 500000, 12.5],
        'text-variable-anchor': ['left', 'right', 'top', 'bottom'],
        'text-radial-offset': 0.55,
        'text-justify': 'auto',
        'symbol-sort-key': ['-', pop],
      },
      paint: {
        'text-color': '#1f2937',
        'text-halo-color': 'rgba(255, 255, 255, 0.9)',
        'text-halo-width': 1.2,
      },
    });
  });
}

function applyBasemap(map, b) {
  const tiled = Boolean(b.tiles);
  if (tiled) {
    // setTiles reloads the source from its options, so the credit and the zoom
    // limit are swapped in with the tiles — otherwise the first basemap's
    // attribution stays on screen and a zoom-17 service is asked for zoom 19.
    const source = map.getSource('basemap');
    Object.assign(source._options, { attribution: b.attribution, maxzoom: b.maxzoom || 19 });
    source.setTiles([b.tiles]);
    map.style.sourceCaches.basemap.clearTiles();
    map.style.sourceCaches.basemap.update(map.transform);
    applyBasemapPaint(map, b);
  } else if (b.outline) {
    addOutline(map, b);
    restack(map);                       // the outline and cities go above the data
  }
  map.setLayoutProperty('basemap', 'visibility', tiled ? 'visible' : 'none');
  map.setPaintProperty('background', 'background-color', tiled ? BACKGROUND.tiled : BACKGROUND.bare);
  for (const id of bareLayerIds()) {
    if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', tiled ? 'none' : 'visible');
  }
  map.triggerRepaint();
}

// GitHub Pages refuses files of 100 MiB or more, so the build cuts a large
// tileset into consecutive byte slices (tile.py split_parts) and lists them as
// `source_parts`. This hands pmtiles those slices as the one archive they came
// from; a range that straddles a boundary is read from both parts and joined.
class PartsSource {
  constructor(url, parts) {
    this.url = url;
    let start = 0;
    this.parts = parts.map(([file, bytes]) => {
      const part = { fetcher: new pmtiles.FetchSource(`tiles/${file}`), start, bytes };
      start += bytes;
      return part;
    });
  }

  getKey() {
    return this.url;
  }

  async getBytes(offset, length, signal) {
    const end = offset + length;
    const reads = [];
    for (const p of this.parts) {
      const lo = Math.max(offset, p.start);
      const hi = Math.min(end, p.start + p.bytes);
      if (lo < hi) reads.push(p.fetcher.getBytes(lo - p.start, hi - lo, signal));
    }
    const got = await Promise.all(reads);
    if (got.length === 1) return { data: got[0].data };
    const joined = new Uint8Array(got.reduce((n, g) => n + g.data.byteLength, 0));
    let at = 0;
    for (const g of got) {
      joined.set(new Uint8Array(g.data), at);
      at += g.data.byteLength;
    }
    return { data: joined.buffer };
  }
}

function registerSplitArchives(protocol, layers) {
  for (const layer of layers) {
    if (layer.remote || !layer.source_parts) continue;
    const url = `tiles/${layer.source_file}`;
    if (protocol.get(url)) continue;
    protocol.add(new pmtiles.PMTiles(new PartsSource(url, layer.source_parts)));
  }
}

function sourceKeyFor(layer) {
  return layer.remote ? layer.source_file : `local:${layer.source_file}`;
}

function addSources(map) {
  const added = new Set();
  for (const layer of state.layers) {
    const key = sourceKeyFor(layer);
    if (added.has(key)) continue;
    added.add(key);
    const url = layer.remote ? layer.source_file : `tiles/${layer.source_file}`;
    if (layer.format === 'pmtiles') {
      const spec = { type: 'vector', url: `pmtiles://${url}`, attribution: layer.attribution };
      // Feature state needs a feature id and the commune tileset carries none.
      // Promoting the INSEE code — the join key every commune table already uses
      // — gives the correlator somewhere to put its per-commune result without
      // re-tiling 35,000 polygons every time the user changes a dropdown.
      if (layer.type === 'choropleth' && layer.source_layer) {
        spec.promoteId = { [layer.source_layer]: 'code_insee' };
      }
      map.addSource(key, spec);
    } else {
      map.addSource(key, { type: 'geojson', data: url, attribution: layer.attribution });
    }
  }
}

// A layer may give `radius` either as a plain number — in which case it gets the
// standard zoom ramp — or as a MapLibre expression, to size circles by a value
// (station footfall, airport category). Arithmetic on an expression yields NaN
// and silently removes every circle, so the two cases have to be kept apart.
function radiusExpression(paint) {
  const radius = paint && paint.radius;
  if (Array.isArray(radius)) return radius;
  const r = radius || 3;
  return ['interpolate', ['linear'], ['zoom'], 5, r * 0.6, 11, r * 1.8];
}

function addLayer(map, layer) {
  const source = sourceKeyFor(layer);
  const common = {
    id: layer.id,
    source,
    layout: { visibility: state.visible.has(layer.id) ? 'visible' : 'none' },
  };
  if (layer.source_layer) common['source-layer'] = layer.source_layer;
  if (layer.minzoom) common.minzoom = layer.minzoom;
  const opacity = state.opacity.get(layer.id);
  const color = colorExpression(layer.paint || {});

  if (layer.type === 'point') {
    map.addLayer({
      ...common, type: 'circle',
      paint: {
        'circle-color': color,
        'circle-radius': radiusExpression(layer.paint),
        'circle-stroke-color': '#ffffff', 'circle-stroke-width': 0.6,
        'circle-opacity': opacity, 'circle-stroke-opacity': opacity,
      },
    });
    return;
  }
  if (layer.type === 'line') {
    const line = {
      ...common, type: 'line',
      paint: { 'line-color': color, 'line-width': layer.paint.width || 0.7, 'line-opacity': opacity },
    };
    // A line layer with `areas` also carries polygons in the same tileset — the
    // rivers' lakes. They are filled beneath the lines under the same switch, and
    // each MapLibre layer is filtered to its own geometry, or the line layer would
    // trace every lake shore as if it were a river.
    const areas = layer.paint.areas;
    if (areas) {
      map.addLayer({
        ...common, id: `${layer.id}__areas`, type: 'fill', filter: POLYGONS,
        paint: { 'fill-color': color, 'fill-outline-color': areas.outline_color || color, 'fill-opacity': opacity },
      });
      line.filter = ['!', POLYGONS];
    }
    map.addLayer(line);
    return;
  }
  // choropleth and polygon are both fills; polygons additionally get an outline
  map.addLayer({ ...common, type: 'fill', paint: { 'fill-color': color, 'fill-opacity': opacity } });
  const outline = layer.paint && layer.paint.outline_color;
  if (outline && outline !== 'none') {
    map.addLayer({
      id: `${layer.id}__outline`, source, type: 'line',
      ...(layer.source_layer ? { 'source-layer': layer.source_layer } : {}),
      ...(layer.minzoom ? { minzoom: layer.minzoom } : {}),
      layout: common.layout,
      paint: { 'line-color': outline, 'line-width': 0.8, 'line-opacity': opacity },
    });
  }
}

// Every MapLibre layer drawn for one manifest layer, bottom first: a line layer's
// lake fill, the layer itself, a polygon's outline. Whatever applies to a layer —
// switching, opacity, draw order, clicks — applies to all of them.
const POLYGONS = ['match', ['geometry-type'], ['Polygon', 'MultiPolygon'], true, false];
const drawnIds = (id) => [`${id}__areas`, id, `${id}__outline`];

// A zero-opacity fill over the commune tileset, always present, so a click can
// report commune attributes even when no choropleth is switched on.
function addProbeLayer(map) {
  const commune = state.layers.find((l) => l.type === 'choropleth');
  if (!commune) return;
  map.addLayer({
    id: '__commune_probe', type: 'fill', source: sourceKeyFor(commune),
    'source-layer': commune.source_layer,
    paint: { 'fill-opacity': 0 },
  }, state.layers[0] ? state.layers[0].id : undefined);
}

function applyVisibility(map) {
  for (const layer of state.layers) {
    const on = state.visible.has(layer.id) ? 'visible' : 'none';
    for (const id of drawnIds(layer.id)) {
      if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', on);
    }
  }
}

function applyOpacity(map, layer) {
  const o = state.opacity.get(layer.id);
  const props = { point: ['circle-opacity', 'circle-stroke-opacity'], line: ['line-opacity'] };
  for (const prop of props[layer.type] || ['fill-opacity']) {
    if (map.getLayer(layer.id)) map.setPaintProperty(layer.id, prop, o);
  }
  if (map.getLayer(`${layer.id}__outline`)) map.setPaintProperty(`${layer.id}__outline`, 'line-opacity', o);
  if (map.getLayer(`${layer.id}__areas`)) map.setPaintProperty(`${layer.id}__areas`, 'fill-opacity', o);
}

// ----------------------------------------------------------- stack order
// The active list is drawn top-first, which is the opposite of the draw order.
function activeIds() {
  return state.order.filter((id) => state.visible.has(id));
}

function restack(map) {
  // Moving each layer to the top in bottom-to-top order leaves the stack in
  // exactly `state.order`. Outlines ride immediately above their own fill, lake
  // fills immediately below their own lines.
  for (const id of state.order) {
    for (const part of drawnIds(id)) if (map.getLayer(part)) map.moveLayer(part);
  }
  // The selected commune's border, the country border and the city names read
  // over every data layer, or a choropleth hides them.
  // A clicked station's routes go over those, so their names win a collision.
  for (const id of [SELECTED.casing, SELECTED.line, OUTLINE.line, ...cityLayerIds(), ...routeLayerIds()]) {
    if (map.getLayer(id)) map.moveLayer(id);
  }
}

// Rewrite the draw order of the visible layers while leaving every hidden layer
// exactly where it sat, so switching a layer off and on again does not move it.
function setVisibleOrder(bottomFirst) {
  const slots = [];
  state.order.forEach((id, i) => { if (state.visible.has(id)) slots.push(i); });
  slots.forEach((slot, i) => { state.order[slot] = bottomFirst[i]; });
}

// Where a newly switched-on layer lands. Straight to the top would be wrong:
// turning on a choropleth would bury the piezometer points under an opaque
// fill. Instead it goes to the top of its own band — above every fill, still
// below the outlines and points that have to stay readable over it. `z` comes
// from the manifest, which is where draw order is decided in the first place.
function bandOf(id) {
  const layer = state.byId.get(id);
  return layer && layer.z != null ? layer.z : 20;
}

function insertByBand(id) {
  const rest = state.order.filter((x) => x !== id);
  let at = 0;
  for (let i = rest.length - 1; i >= 0; i--) {
    if (bandOf(rest[i]) <= bandOf(id)) { at = i + 1; break; }
  }
  rest.splice(at, 0, id);
  state.order = rest;
}

// Both the grip drag and the arrow keys land here: `to` is an index in the
// active list, which reads top-first.
function moveLayerTo(map, id, to) {
  const top = activeIds().reverse();
  const from = top.indexOf(id);
  if (from < 0 || to < 0 || to >= top.length || to === from) return;
  top.splice(to, 0, top.splice(from, 1)[0]);
  setVisibleOrder(top.reverse());
  applyStack(map);
}

function moveLayerBy(map, id, delta) {
  moveLayerTo(map, id, activeIds().reverse().indexOf(id) + delta);
}

// One call for everything that has to agree after a change.
function applyStack(map) {
  restack(map);
  buildActive(map);
  renderLegend();
  writeHash(map);
}

// ---------------------------------------------------- active layer stack UI
// A layer that fills areas carries its own scale in its row, so which values a
// colour stands for is readable without scrolling down to the legend. Lines
// and points are left to the legend: a railway reads as a railway.

function unitOf(layer) {
  const paint = layer.paint || {};
  if (paint.unit != null) return paint.unit;   // computed layers have no field to read
  const field = (layer.fields || []).find((f) => f.name === paint.property);
  return (field && field.unit) || '';
}

// Break labels have to sit under a six-class bar inside a 330px panel, so
// anything past ten thousand is abbreviated.
function shortNumber(n, decimals) {
  const a = Math.abs(n);
  if (a >= 1e6) return `${+(n / 1e6).toFixed(1)}M`;
  if (a >= 1e4) return `${+(n / 1e3).toFixed(a >= 1e5 ? 0 : 1)}k`;
  return decimals ? n.toFixed(decimals) : String(n);
}

// One decimal count for the whole scale, so 2.4 / 2.7 / 3.0 don't read as
// 2.4 / 2.7 / 3.
function decimalsIn(values) {
  return values.reduce((max, v) => {
    const dot = String(v).indexOf('.');
    return Math.max(max, dot < 0 ? 0 : String(v).length - dot - 1);
  }, 0);
}

// A class the correlator has suppressed paints nothing at all, so its swatch is
// hatched rather than white — white is a colour the scales genuinely use.
const BLANK = 'rgba(0,0,0,0)';
const swatch = (color) => {
  const chip = el('i');
  if (color === BLANK) chip.className = 'blank';
  else chip.style.background = color;
  return chip;
};

function colorBar(colors) {
  const bar = el('div', 'act-bar');
  for (const color of colors) bar.appendChild(swatch(color));
  return bar;
}

// A numeric scale is the bar plus its break values, each one placed on the
// boundary it belongs to — the classes are equal width, so the position is
// simply the boundary index over the class count.
function numericScale(paint, unit) {
  const wrap = el('div', 'act-scale');
  if (unit) wrap.appendChild(el('div', 'act-unit', unit));
  wrap.appendChild(colorBar(paint.colors));
  const ticks = el('div', 'act-ticks');
  const decimals = decimalsIn(paint.breaks);
  paint.breaks.forEach((b, i) => {
    placeOnBar(el('span', 'tick', shortNumber(b, decimals)), ((i + 1) / paint.colors.length) * 100, ticks);
  });
  wrap.appendChild(ticks);
  return wrap;
}

// Ordinal and categorical scales name their classes instead. A handful fit as
// labelled swatches; a long list (the municipal nuances run past twenty) is
// shown as the bare colour bar with a pointer to the legend.
function classScale(entries) {
  if (entries.length > 7) {
    const wrap = el('div', 'act-scale');
    wrap.append(colorBar(entries.map((e) => e.color)),
                el('div', 'act-more', `${entries.length} categories — see legend`));
    return wrap;
  }
  const list = el('div', 'act-keys');
  for (const entry of entries) {
    const key = el('span', 'act-key');
    const chip = swatch(entry.color);
    const label = el('span', 'act-key-label', entry.label);
    label.title = entry.label;
    key.append(chip, label);
    list.appendChild(key);
  }
  return list;
}

// The colour a continuous scale gives one value, for the legend's closing
// swatch: the same linear interpolation MapLibre does, in sRGB.
function continuousColor(stops, v) {
  const hex = (c) => [1, 3, 5].map((k) => parseInt(c.slice(k, k + 2), 16));
  let k = 0;
  while (k < stops.length - 2 && v > stops[k + 1][0]) k++;
  const [a, ca] = stops[k], [b, cb] = stops[k + 1];
  const t = Math.min(1, Math.max(0, (v - a) / (b - a)));
  const [x, y] = [hex(ca), hex(cb)];
  return `#${x.map((c, i) => Math.round(c + (y[i] - c) * t).toString(16).padStart(2, '0')).join('')}`;
}

// A continuous scale is one gradient bar over the whole range. Past the best
// value reached, the bar is washed out and a marker names the best, so a run
// whose best is 0.45 visibly never gets near the dark end.
function continuousScale(paint, unit) {
  const wrap = el('div', 'act-scale');
  if (unit) wrap.appendChild(el('div', 'act-unit', unit));
  const [lo, hi] = [paint.stops[0][0], paint.stops[paint.stops.length - 1][0]];
  const at = (v) => ((v - lo) / (hi - lo)) * 100;
  const bar = el('div', 'act-bar act-gradient');
  bar.style.background = `linear-gradient(to right, ${paint.stops.map(([v, c]) => `${c} ${at(v)}%`).join(', ')})`;
  const ticks = el('div', 'act-ticks');
  const decimals = decimalsIn(paint.stops.map(([v]) => v));
  for (const [v] of paint.stops) {
    placeOnBar(el('span', 'tick', shortNumber(v, decimals)), at(v), ticks);
  }
  if (Number.isFinite(paint.max)) {
    const past = el('i', 'act-past');
    past.style.left = `${at(paint.max)}%`;
    bar.appendChild(past);
    const head = el('div', 'act-maxrow');
    placeOnBar(el('span', 'act-max', `best ${shortNumber(paint.max, 2)}`), at(paint.max), head);
    wrap.appendChild(head);
  }
  wrap.append(bar, ticks);
  if (paint.no_data_label) {
    const key = el('span', 'act-key');
    key.append(swatch(paint.no_data_color), el('span', 'act-key-label', paint.no_data_label));
    wrap.appendChild(key);
  }
  return wrap;
}

function scaleBlock(layer) {
  const paint = layer.paint || {};
  if (paint.scale === 'continuous' && paint.stops) return continuousScale(paint, unitOf(layer));
  if (paint.scale === 'numeric' && paint.breaks && paint.colors) return numericScale(paint, unitOf(layer));
  const block = classScale(legendEntries(paint));
  if (!paint.strength) return block;
  const wrap = el('div');
  wrap.append(block, strengthBar(paint));
  return wrap;
}

const GRIP_DOTS = [3, 8, 13]
  .map((y) => `<circle cx="3" cy="${y}" r="1.5"/><circle cx="8" cy="${y}" r="1.5"/>`)
  .join('');
const GRIP_SVG = `<svg viewBox="0 0 11 16" fill="currentColor" aria-hidden="true">${GRIP_DOTS}</svg>`;

// Reordering by the grip, and only by the grip: the row follows the pointer on
// the Y axis alone, clamped to the list, and the rows it passes slide out of
// the way. Nothing moves sideways and nothing inside the card — the opacity
// slider above all — has its own drag stolen by the row.
function startReorder(map, host, row, ev) {
  if (ev.button != null && ev.button !== 0) return;
  ev.preventDefault();
  const rows = Array.from(host.children);
  const from = rows.indexOf(row);
  if (from < 0 || rows.length < 2) return;

  const rects = rows.map((r) => r.getBoundingClientRect());
  const gap = rects[1].top - rects[0].bottom;
  const shift = rects[from].height + gap;       // what a passed row steps by
  const minDy = rects[0].top - rects[from].top;
  const maxDy = rects[rects.length - 1].bottom - rects[from].bottom;
  const others = rows.filter((_, k) => k !== from);
  const centers = rects.filter((_, k) => k !== from).map((r) => r.top + r.height / 2);
  const startY = ev.clientY;
  const handle = ev.currentTarget;
  let to = from;

  row.classList.add('dragging');
  document.body.classList.add('reordering');
  // Capture keeps the drag alive when the pointer leaves the 20px handle;
  // not every browser grants it, and a drag works without it.
  try { handle.setPointerCapture(ev.pointerId); } catch (err) { /* not fatal */ }

  const move = (e) => {
    const dy = Math.max(minDy, Math.min(maxDy, e.clientY - startY));
    row.style.transform = `translateY(${dy}px)`;
    const center = rects[from].top + rects[from].height / 2 + dy;
    to = centers.filter((c) => c < center).length;
    others.forEach((other, j) => {
      const k = rows.indexOf(other);
      const d = k < from && j >= to ? shift : k > from && j < to ? -shift : 0;
      other.style.transform = d ? `translateY(${d}px)` : '';
    });
  };

  const end = () => {
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', end);
    window.removeEventListener('pointercancel', end);
    document.body.classList.remove('reordering');
    row.classList.remove('dragging');
    for (const r of rows) r.style.transform = '';
    moveLayerTo(map, row.dataset.id, to);       // redraws the list if it changed
  };

  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', end);
  window.addEventListener('pointercancel', end);
}

function buildActive(map) {
  const host = $('#active');
  host.innerHTML = '';
  const ids = activeIds().reverse();          // topmost first
  $('#active-count').textContent = ids.length ? `${ids.length}` : '0';
  if (!ids.length) {
    host.appendChild(el('p', 'muted', 'No layers on. Tick one below — several can be stacked, and the sliders here blend them.'));
    return;
  }

  ids.forEach((id, i) => {
    const layer = state.byId.get(id);
    const row = el('div', 'act-row');
    row.dataset.id = id;

    const card = el('div', 'act');
    const head = el('div', 'act-head');

    const name = el('span', 'act-name', layer.label);
    name.title = layer.label;

    // Dragging the grip restacks; arrow keys on it do the same a step at a
    // time, which is what the pair of arrow buttons used to be for.
    const grip = el('button', 'grip');
    grip.type = 'button';
    grip.innerHTML = GRIP_SVG;
    grip.title = 'Drag to restack (↑ ↓ when focused)';
    grip.setAttribute('aria-label', `Reorder ${layer.label}, ${i + 1} of ${ids.length}`);
    grip.addEventListener('pointerdown', (e) => startReorder(map, host, row, e));
    grip.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
      e.preventDefault();
      moveLayerBy(map, id, e.key === 'ArrowUp' ? -1 : +1);
      const moved = host.querySelector(`.act-row[data-id="${CSS.escape(id)}"] .grip`);
      if (moved) moved.focus();
    });

    const off = el('button', 'act-btn off', '×');
    off.title = 'Switch this layer off';
    off.addEventListener('click', () => toggleLayer(map, id, false));

    head.append(name, grip, off);

    const ctl = el('div', 'act-ctl');
    const slider = el('input');
    slider.type = 'range';
    slider.min = 0; slider.max = 1; slider.step = 0.05;
    slider.value = state.opacity.get(id);
    slider.title = 'Opacity';
    const pct = el('span', 'act-pct', `${Math.round(state.opacity.get(id) * 100)}%`);
    slider.addEventListener('input', () => {
      state.opacity.set(id, Number(slider.value));
      pct.textContent = `${Math.round(slider.value * 100)}%`;
      applyOpacity(map, layer);
    });
    ctl.append(slider, pct);

    const fills = layer.type === 'choropleth' || layer.type === 'polygon';
    card.append(head, ...(fills ? [scaleBlock(layer)] : []), ctl);

    row.append(card);
    host.appendChild(row);
  });
}

// ----------------------------------------------------------------- panel
// The start view's top items open the layer list and every statistic menu,
// taken out of their own groups so that each is listed once.
function topItems() {
  const top = (state.manifest.start || {}).top_items;
  const layers = ((top && top.layers) || []).map((id) => state.byId.get(id)).filter(Boolean);
  return {
    label: (top && top.label) || 'Top items',
    layers,
    // A layer's statistic is the field it draws.
    stats: layers.map((l) => l.paint && l.paint.property).filter((name) => state.stats.byName.has(name)),
  };
}

// Two switches per row, both reading as tickboxes: the square puts the layer
// itself on the map, the circle in the optimiser's own colour hands the
// statistic it draws to the optimiser. Each is its own letter rather than a
// tick — the square dips at the top into an M, the filled circle carries an O
// — so a row says at a glance where that statistic is already in play.
const TICKBOX_MAP_SVG = '<svg viewBox="0 0 16 16" aria-hidden="true">'
  + '<path class="tb-shape" d="M2.5 2.9V13.1h11V2.9L8 6.2Z"/>'
  + '<path class="tb-mark" d="M5.3 11.7V7.1L8 9.7l2.7-2.6v4.6"/></svg>';
const TICKBOX_OPT_SVG = '<svg viewBox="0 0 16 16" aria-hidden="true">'
  + '<circle class="tb-shape" cx="8" cy="8" r="5.7"/>'
  + '<circle class="tb-mark" cx="8" cy="8" r="3.2"/></svg>';

function tickbox(cls, svg, label) {
  const button = el('button', `tickbox ${cls}`);
  button.type = 'button';
  button.innerHTML = svg;
  button.setAttribute('aria-pressed', 'false');
  button.setAttribute('aria-label', label);
  button.title = label;
  return button;
}

// The statistic a layer draws, when the optimiser can score it.
function optimisable(layer) {
  if (!state.byId.has(OPT_ID)) return null;
  const name = layer.paint && layer.paint.property;
  return name && state.stats.byName.has(name) ? name : null;
}

// What the circle does: exactly the add and the remove that the optimiser's own
// dropdown and card crosses do.
function toggleCriterion(map, name) {
  const o = state.opt;
  if (o.criteria.some((c) => c.name === name)) o.criteria = o.criteria.filter((c) => c.name !== name);
  else o.criteria.push({ name, dir: 'up', weight: 3, bad: null, ideal: null });
  renderOptimiser(map);
  runOptimiser(map);
}

function buildPanel(map) {
  const host = $('#layers');
  host.innerHTML = '';
  const top = topItems();
  const groups = new Map();
  if (top.layers.length) groups.set(top.label, top.layers);
  for (const layer of state.layers) {
    if (top.layers.includes(layer)) continue;
    if (!groups.has(layer.group)) groups.set(layer.group, []);
    groups.get(layer.group).push(layer);
  }

  for (const [name, all] of groups) {
    // Not listed: the computed layers, whose tools own their switch, and data
    // layers retired from the list. A hidden data layer still reports in the
    // inspect panel, feeds the stat tools, and turns on from a shared link.
    const listed = all.filter((l) => l.panel !== false);
    if (!listed.length) continue;
    // `detail` layers are listed after the rest.
    const isTop = name === top.label;
    const layers = isTop ? listed : listed.filter((l) => !l.detail).concat(listed.filter((l) => l.detail));
    const block = el('div', 'group');
    // Each group folds to its heading. The badge counts the group's layers that
    // are switched on, so a folded group still says it has something on the map.
    const heading = el('h3');
    const toggle = el('button', 'block-toggle');
    toggle.type = 'button';
    const chev = el('span', 'chev');
    chev.setAttribute('aria-hidden', 'true');
    const badge = el('span', 'count group-count');
    badge.hidden = true;
    toggle.append(chev, name, badge);
    heading.appendChild(toggle);
    const body = el('div', 'group-body');
    block.append(heading, body);
    for (const layer of layers) {
      const item = el('div', 'layer');
      item.dataset.id = layer.id;

      const row = el('div', 'row');
      const ticks = el('div', 'tickboxes');

      const onMap = tickbox('tickbox-map', TICKBOX_MAP_SVG, `Draw ${layer.label} on the map`);
      onMap.addEventListener('click', () => toggleLayer(map, layer.id, !state.visible.has(layer.id)));
      ticks.appendChild(onMap);

      // The circle is offered only where the layer draws a commune statistic:
      // the optimiser scores columns, not point or line layers.
      const stat = optimisable(layer);
      if (stat) {
        const inOpt = tickbox('tickbox-opt', TICKBOX_OPT_SVG, `Add ${layer.label} to Find a commune`);
        inOpt.dataset.stat = stat;
        inOpt.addEventListener('click', () => toggleCriterion(map, stat));
        ticks.appendChild(inOpt);
      } else {
        ticks.appendChild(el('span', 'tickbox tickbox-gap'));  // keeps the names in one column
      }

      const label = el('span', 'layer-name', layer.label);
      label.addEventListener('click', () => onMap.click());
      row.append(ticks, label, el('span', 'badge', layer.type === 'choropleth' ? 'commune' : layer.type));

      const about = [layer.legend_note, layer.attribution && `Source: ${layer.attribution}`].filter(Boolean);
      item.appendChild(row);
      if (about.length) item.appendChild(fold('notes & sources', about, 'note'));
      body.appendChild(item);
    }
    // Folded by default except the top items: the whole list open at once is
    // ninety rows.
    collapsible(block, toggle, body, `layers-group:${name}`, isTop);
    host.appendChild(block);
  }
  syncPanel();
}

// A one-line "Show …" link over text most people do not need every time. The
// text is in the page either way; only its display is folded.
function fold(what, paragraphs, cls) {
  const details = el('details', `fold ${cls || ''}`.trim());
  const summary = el('summary', null, `Show ${what}`);
  details.appendChild(summary);
  for (const text of paragraphs) details.appendChild(el('p', null, text));
  details.addEventListener('toggle', () => {
    summary.textContent = `${details.open ? 'Hide' : 'Show'} ${what}`;
  });
  return details;
}

function syncPanel() {
  const criteria = new Set(state.opt.criteria.map((c) => c.name));
  for (const item of document.querySelectorAll('.layer')) {
    const on = state.visible.has(item.dataset.id);
    item.classList.toggle('on', on);
    item.querySelector('.tickbox-map').setAttribute('aria-pressed', String(on));
    const inOpt = item.querySelector('.tickbox-opt');
    if (inOpt) inOpt.setAttribute('aria-pressed', String(criteria.has(inOpt.dataset.stat)));
  }
  for (const group of document.querySelectorAll('#layers .group')) {
    const n = group.querySelectorAll('.layer.on').length;
    const badge = group.querySelector('.group-count');
    badge.textContent = n;
    badge.hidden = !n;
  }
}

function toggleLayer(map, id, on) {
  // Any number of layers can be on together. Commune choropleths all render from
  // the same tileset, so stacking two of them is a blend of two fills — which is
  // what the opacity slider in the active list is for.
  if (on) {
    state.visible.add(id);
    insertByBand(id);
  } else {
    state.visible.delete(id);
  }
  applyVisibility(map);
  syncPanel();
  applyStack(map);
}

// ---------------------------------------------------------------- legend
function renderLegend() {
  const host = $('#legend');
  host.innerHTML = '';
  // Top of the stack first, so the legend reads in the same order as the map.
  const shown = activeIds().reverse().map((id) => state.byId.get(id));
  if (!shown.length) {
    host.appendChild(el('p', 'muted', 'No layers visible.'));
    $('#attribution').textContent = '';
    $('#attribution-fold').hidden = true;
    return;
  }
  for (const layer of shown) {
    const block = el('div', 'legend-block');
    const field = (layer.fields || []).find((f) => f.name === (layer.paint || {}).property);
    const unit = field && field.unit ? ` (${field.unit})` : '';
    block.appendChild(el('h4', null, layer.label + unit));
    const sw = el('div', 'swatches');
    for (const entry of legendEntries(layer.paint || {})) {
      const row = el('div', layer.type === 'point' ? 'sw dot' : 'sw');
      row.append(swatch(entry.color), el('span', null, entry.label));
      sw.appendChild(row);
    }
    block.appendChild(sw);
    if ((layer.paint || {}).strength) block.appendChild(strengthBar(layer.paint));
    host.appendChild(block);
  }
  // Licence attribution for every visible layer, as Licence Ouverte requires.
  $('#attribution').textContent = [...new Set(shown.map((l) => l.attribution).filter(Boolean))].join(' · ');
  $('#attribution-fold').hidden = !$('#attribution').textContent;
}

// --------------------------------------------------------------- inspect
// Undeclared source attributes arrive as snake_case column names.
function prettify(key) {
  const text = key.replace(/_/g, ' ').trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function formatValue(value, field) {
  if (value === null || value === undefined || value === '') return '—';
  if (field && field.value_labels && field.value_labels[value]) return field.value_labels[value];
  if (field && field.type === 'numeric' && typeof value === 'number') {
    const n = value.toLocaleString('en', { maximumFractionDigits: 2 });
    return field.unit ? `${n} ${field.unit}` : n;
  }
  return String(value);
}

function inspect(map, point, lngLat, { code = null } = {}) {
  const ids = state.layers.filter((l) => state.visible.has(l.id)).flatMap((l) => drawnIds(l.id));
  if (map.getLayer('__commune_probe')) ids.push('__commune_probe');
  // Where points crowd — a city's stations — the one nearest the click, not
  // whichever happens to be drawn on top.
  const away = (f) => {
    if (f.geometry.type !== 'Point') return 0;
    const p = map.project(f.geometry.coordinates);
    return (p.x - point.x) ** 2 + (p.y - point.y) ** 2;
  };
  const hitOf = (layer) => hits
    .filter((f) => f.layer.id === layer.id || f.layer.id === `${layer.id}__areas`)
    .reduce((best, f) => (!best || away(f) < away(best) ? f : best), null);
  const hits = map.queryRenderedFeatures([[point.x - 3, point.y - 3], [point.x + 3, point.y + 3]],
    { layers: ids.filter((id) => map.getLayer(id)) });

  const body = $('#inspect-body');
  body.innerHTML = '';

  // The commune is the join key for everything, so it leads. A commune picked
  // from the search is looked up by its code: its centre point can sit in a
  // neighbour's shape when the commune is a crescent or a ring.
  const isCommune = (f) => (f.properties || {}).code_insee && (f.properties || {}).nom;
  let communeFeature = code
    ? hits.find((f) => isCommune(f) && f.properties.code_insee === code) || communeByCode(map, code)
    : hits.find((f) => f.layer.id === '__commune_probe') || hits.find(isCommune);
  const props = communeFeature ? communeFeature.properties : {};
  highlightCommune(map, props.code_insee || null);
  $('#inspect-title').textContent = props.nom
    ? `${props.nom} (${props.code_insee})`
    : `${lngLat.lat.toFixed(4)}, ${lngLat.lng.toFixed(4)}`;

  // Group every loaded attribute by the source that published it.
  const bySource = new Map();
  const push = (layer, rows) => {
    const key = layer.source_id;
    if (!bySource.has(key)) bySource.set(key, { layer, rows: [] });
    bySource.get(key).rows.push(...rows);
  };

  for (const layer of state.layers) {
    const fields = layer.fields || [];
    if (!fields.length) continue;
    let source;
    if (layer.type === 'choropleth') {
      source = communeFeature ? communeFeature.properties : null;
    } else {
      const hit = hitOf(layer);
      source = hit ? hit.properties : null;
    }
    if (!source) continue;
    const rows = fields
      .filter((f) => source[f.name] !== undefined)
      .map((f) => ({ k: f.label || f.name, v: formatValue(source[f.name], f) }));
    if (rows.length) push(layer, rows);
  }

  // Identity attributes of any non-commune feature under the cursor — the park's
  // name and management body, the station's BSS code. Anything already declared
  // as a field by any layer, and the commune identity keys, are skipped: they are
  // reported above by whichever source published them.
  const declaredAnywhere = new Set(state.layers.flatMap((l) => (l.fields || []).map((f) => f.name)));
  const identityKeys = new Set(['code_insee', 'nom', 'dep', 'reg']);
  for (const layer of state.layers) {
    if (layer.type === 'choropleth' || layer.type === 'line' || !state.visible.has(layer.id)) continue;
    const hit = hitOf(layer);
    if (!hit) continue;
    const extra = Object.entries(hit.properties || {})
      .filter(([k, v]) => !declaredAnywhere.has(k) && !identityKeys.has(k) && v !== null && v !== '')
      .map(([k, v]) => ({ k: prettify(k), v: formatValue(v) }));
    if (extra.length) push(layer, extra);
  }

  if (props.code_insee) {
    const head = el('div', 'insp-group');
    head.appendChild(el('h3', null, 'Commune'));
    for (const [k, v] of [['INSEE code', props.code_insee], ['Department', props.dep], ['Region', props.reg]]) {
      if (!v) continue;
      const row = el('div', 'insp-row');
      row.append(el('span', 'k', k), el('span', 'v', String(v)));
      head.appendChild(row);
    }
    if (climateFiles()) {
      const code = props.code_insee;
      const name = props.nom;
      const button = el('button', 'insp-action', 'Show climate table');
      button.type = 'button';
      button.addEventListener('click', () => openClimateTable(code, name, button));
      head.appendChild(button);
    }
    head.appendChild(communeLinks(props.code_insee, props.nom, lngLat));
    body.appendChild(head);
  }

  // A station under the cursor: where its trains go, drawn on the map and listed.
  const gares = state.layers.find((l) => l.source_id === 'gares' && state.visible.has(l.id));
  const station = gares && networkFiles() ? hitOf(gares) : null;
  if (station) {
    const [lng, lat] = station.geometry.coordinates;
    body.appendChild(stationBlock(map, station.properties, { lng, lat }));
  } else {
    clearRoutes(map);
  }

  // The correlator's own reading for this commune. It cannot come from the
  // feature's properties — nothing computed it until the user asked — so it is
  // looked up by INSEE code in the columns the correlator already has.
  state.corr.focus = props.code_insee || null;
  const corrRows = state.visible.has(CORR_ID) ? corrInspectRows(props.code_insee) : null;
  if (corrRows) {
    const block = el('div', 'insp-group');
    block.appendChild(el('h3', null, state.byId.get(CORR_ID).label));
    for (const { k, v } of corrRows) {
      const row = el('div', 'insp-row');
      row.append(el('span', 'k', k), el('span', 'v', v));
      block.appendChild(row);
    }
    block.appendChild(el('div', 'insp-attr', 'Computed in your browser from the commune stat columns.'));
    body.appendChild(block);
    renderScatter();                 // ring this commune in the scatter above
  }

  const optRows = state.visible.has(OPT_ID) ? optInspectRows(props.code_insee) : null;
  if (optRows) {
    const block = el('div', 'insp-group');
    block.appendChild(el('h3', null, state.byId.get(OPT_ID).label));
    for (const { k, v } of optRows) {
      const row = el('div', 'insp-row');
      row.append(el('span', 'k', k), el('span', 'v', v));
      block.appendChild(row);
    }
    block.appendChild(el('div', 'insp-attr', 'Computed in your browser from the commune stat columns.'));
    body.appendChild(block);
  }

  for (const { layer, rows } of bySource.values()) {
    const { block, inner } = foldedGroup(layer.source_id, layer.source_name || layer.label, rows.length);
    for (const { k, v } of rows) {
      const row = el('div', 'insp-row');
      row.append(el('span', 'k', k), el('span', 'v', v));
      inner.appendChild(row);
    }
    const attr = el('div', 'insp-attr');
    attr.textContent = layer.attribution + (layer.fetched ? ` · fetched ${layer.fetched}` : '');
    inner.appendChild(attr);
    body.appendChild(block);
  }

  if (!bySource.size && !props.code_insee) {
    body.appendChild(el('p', 'insp-empty', 'Nothing loaded at this point. Switch on a layer and click again.'));
  }
  $('#inspect').hidden = false;
}

// A source's rows in the inspect panel fold under its heading, closed until
// opened. What is open is remembered for the session, so reading one source
// commune after commune does not mean reopening it on every click.
const inspectOpen = new Set();

function foldedGroup(key, title, count) {
  const block = el('div', 'insp-group');
  const heading = el('h3');
  const toggle = el('button', 'block-toggle');
  toggle.type = 'button';
  const chev = el('span', 'chev');
  chev.setAttribute('aria-hidden', 'true');
  toggle.append(chev, title, el('span', 'insp-n', String(count)));
  heading.appendChild(toggle);
  const inner = el('div');
  const setOpen = (open) => {
    toggle.setAttribute('aria-expanded', String(open));
    inner.hidden = !open;
    block.classList.toggle('collapsed', !open);
    if (open) inspectOpen.add(key); else inspectOpen.delete(key);
  };
  setOpen(inspectOpen.has(key));
  toggle.addEventListener('click', () => setOpen(inner.hidden));
  block.append(heading, inner);
  return { block, inner };
}

// Outbound searches for the commune under the cursor. Both are centred on the
// commune's own point from the stat index, so a click near a border still
// searches the commune named in the title; until that index has loaded, the
// clicked point stands in.
const LBC_RADIUS_M = 5000;

function communeLinks(code, name, lngLat) {
  const row = el('div', 'insp-links');
  const maps = el('a', 'insp-action', 'Google Maps ↗');
  const lbc = el('a', 'insp-action', `Leboncoin +${LBC_RADIUS_M / 1000} km ↗`);
  for (const a of [maps, lbc]) {
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
  }
  lbc.title = `Property for sale within ${LBC_RADIUS_M / 1000} km of ${name}`;
  maps.title = `Search Google Maps for ${name}`;

  const point = (lat, lon) => {
    const la = lat.toFixed(5), lo = lon.toFixed(5);
    maps.href = `https://www.google.com/maps/search/${encodeURIComponent(name)}/@${la},${lo},13z`;
    // Leboncoin's location token: name _ postcode (left empty) __ lat _ lng _ radius _ radius.
    const where = `${name}__${la}_${lo}_${LBC_RADIUS_M}_${LBC_RADIUS_M}`;
    lbc.href = `https://www.leboncoin.fr/recherche?category=9&locations=${encodeURIComponent(where)}`;
  };
  const fromIndex = (index) => {
    const i = index ? index.row.get(code) : undefined;
    if (i === undefined || !Number.isFinite(index.lat[i])) return false;
    point(index.lat[i], index.lon[i]);
    return true;
  };

  if (!fromIndex(state.stats.index)) {
    point(lngLat.lat, lngLat.lng);
    if (state.manifest.stats) loadStatIndex().then(fromIndex).catch(() => {});
  }
  row.append(maps, lbc);
  return row;
}

// ------------------------------------------------- commune selection
// The commune being inspected gets a heavy border: a white casing under a dark
// line, so it reads over a dark choropleth as well as over the bare map.
const SELECTED = { casing: '__commune_selected_casing', line: '__commune_selected' };
const NO_COMMUNE = ['==', ['get', 'code_insee'], ''];

function addSelectionLayers(map) {
  const commune = state.layers.find((l) => l.type === 'choropleth');
  if (!commune) return;
  const base = { source: sourceKeyFor(commune), 'source-layer': commune.source_layer, filter: NO_COMMUNE };
  map.addLayer({
    ...base, id: SELECTED.casing, type: 'line',
    layout: { 'line-join': 'round' },
    paint: { 'line-color': '#ffffff', 'line-opacity': 0.9,
             'line-width': ['interpolate', ['linear'], ['zoom'], 5, 3.5, 10, 7] },
  });
  map.addLayer({
    ...base, id: SELECTED.line, type: 'line',
    layout: { 'line-join': 'round' },
    paint: { 'line-color': '#111827',
             'line-width': ['interpolate', ['linear'], ['zoom'], 5, 1.8, 10, 3.5] },
  });
}

function highlightCommune(map, code) {
  state.selected = code;
  const filter = code ? ['==', ['get', 'code_insee'], code] : NO_COMMUNE;
  for (const id of [SELECTED.casing, SELECTED.line]) {
    if (map.getLayer(id)) map.setFilter(id, filter);
  }
}

// A commune feature from the loaded tiles, wherever it is drawn. Tiles cut a
// commune into pieces, but every piece carries the same properties.
function communeByCode(map, code) {
  const commune = state.layers.find((l) => l.type === 'choropleth');
  if (!commune || !map.getSource(sourceKeyFor(commune))) return null;
  const found = map.querySourceFeatures(sourceKeyFor(commune), {
    sourceLayer: commune.source_layer, filter: ['==', ['get', 'code_insee'], code],
  });
  return found[0] || null;
}

function closeInspect(map) {
  $('#inspect').hidden = true;
  highlightCommune(map, null);
  clearRoutes(map);
}

// ---------------------------------------------------------- station routes
// Click a station and the map draws everywhere its trains go without changing,
// each hop as thick as the number of this station's trains that make it. The
// pipeline folds SNCF's timetable into distinct stop patterns — one file for the
// whole country, fetched the first time a station is clicked.

const ROUTES = {
  source: '__station_routes',
  casing: '__station_routes_casing', coach: '__station_routes_coach', rail: '__station_routes_rail',
  stops: '__station_routes_stops', origin: '__station_routes_origin', labels: '__station_routes_labels',
};
const routeLayerIds = () => [ROUTES.casing, ROUTES.coach, ROUTES.rail, ROUTES.stops, ROUTES.origin, ROUTES.labels];
// Red for high speed, as on the railway layer's LGV; coaches dashed and grey.
const ROUTE_CATS = [
  ['tgv', '#dc2626', 'High-speed'],
  ['intercites', '#7c3aed', 'Intercités'],
  ['ter', '#2563eb', 'TER'],
  ['car', '#6b7280', 'Coach'],
];
const ROUTE_COLOUR = ['match', ['get', 'cat'], ...ROUTE_CATS.flatMap(([id, c]) => [id, c]), '#2563eb'];
const ROUTE_LIST = 12;
// Coaches are off until asked for: they run on roads, so they can only be drawn
// as straight lines, and those cut across everything the rails show.
const routes = { index: null, loading: null, network: null, run: 0, coaches: false };
const NO_ROUTES = { type: 'FeatureCollection', features: [] };

function networkFiles() {
  return ((state.manifest || {}).files || {}).gares || null;
}

function addRouteLayers(map) {
  if (!networkFiles()) return;
  map.addSource(ROUTES.source, { type: 'geojson', data: NO_ROUTES });
  const line = ['==', ['geometry-type'], 'LineString'];
  // Trains a day along a hop → width, so the main line reads as the main line.
  const width = (scale) => ['*', scale, ['interpolate', ['linear'], ['get', 'day'], 0, 1.2, 4, 2, 16, 3.2, 48, 5, 120, 7]];
  map.addLayer({
    id: ROUTES.casing, type: 'line', source: ROUTES.source, filter: line,
    layout: { 'line-join': 'round', 'line-cap': 'round' },
    paint: { 'line-color': '#ffffff', 'line-opacity': 0.85, 'line-width': ['+', width(1), 2.5] },
  });
  map.addLayer({
    id: ROUTES.coach, type: 'line', source: ROUTES.source, filter: ['all', line, ['==', ['get', 'cat'], 'car']],
    layout: { 'line-join': 'round' },
    paint: { 'line-color': ROUTE_COLOUR, 'line-width': width(0.8), 'line-dasharray': [2, 1.5] },
  });
  map.addLayer({
    id: ROUTES.rail, type: 'line', source: ROUTES.source, filter: ['all', line, ['!=', ['get', 'cat'], 'car']],
    layout: { 'line-join': 'round', 'line-cap': 'round', 'line-sort-key': ['get', 'rank'] },
    paint: { 'line-color': ROUTE_COLOUR, 'line-width': width(1) },
  });
  map.addLayer({
    id: ROUTES.stops, type: 'circle', source: ROUTES.source, filter: ['==', ['get', 'kind'], 'stop'],
    paint: {
      'circle-color': '#ffffff', 'circle-stroke-color': ROUTE_COLOUR, 'circle-stroke-width': 1.8,
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 5, 2.5, 10, 4.5],
    },
  });
  map.addLayer({
    id: ROUTES.origin, type: 'circle', source: ROUTES.source, filter: ['==', ['get', 'kind'], 'origin'],
    paint: {
      'circle-color': '#111827', 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 2.5,
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 5, 5, 10, 8],
    },
  });
  // Busiest destinations are named first; the rest appear as the map zooms in
  // and there is room.
  map.addLayer({
    id: ROUTES.labels, type: 'symbol', source: ROUTES.source, filter: ['!=', ['geometry-type'], 'LineString'],
    layout: {
      'text-field': ['get', 'name'],
      'text-font': ['Open Sans Semibold'],
      'text-size': ['case', ['==', ['get', 'kind'], 'origin'], 13, 11],
      'text-variable-anchor': ['left', 'right', 'top', 'bottom'],
      'text-radial-offset': 0.7,
      'text-justify': 'auto',
      'symbol-sort-key': ['case', ['==', ['get', 'kind'], 'origin'], -1e9, ['-', ['get', 'day']]],
    },
    paint: { 'text-color': '#111827', 'text-halo-color': 'rgba(255, 255, 255, 0.95)', 'text-halo-width': 1.4 },
  });
}

function clearRoutes(map) {
  routes.run++;
  const source = map.getSource(ROUTES.source);
  if (source) source.setData(NO_ROUTES);
}

async function loadNetwork() {
  if (!routes.loading) {
    const files = networkFiles();
    routes.loading = (async () => {
      const index = await fetchJson(files.index);
      const [net, tracks] = await Promise.all([
        fetchJson(`${files.dir}/${index.network}`),
        index.tracks ? fetchJson(`${files.dir}/${index.tracks}`) : null,
      ]);
      // Which patterns call at each stop, and where in them.
      const through = net.stops.map(() => []);
      net.patterns.forEach((p, k) => p[1].forEach((stop, pos) => through[stop].push([k, pos])));
      routes.index = index;
      routes.network = {
        ...net, through, at: new Map(net.stops.map((s, i) => [s[0], i])),
        ...(tracks ? decodeTracks(tracks) : { pieces: [], hopPath: new Map(), snap: {} }),
      };
      return routes.network;
    })().catch((err) => {
      routes.loading = null;       // let the next click try again
      throw err;
    });
  }
  return routes.loading;
}

// The track between consecutive stops, routed by the pipeline: pieces of the
// rail network stored once as delta-encoded 1e-5° integers, and per hop the
// pieces it runs along — negative, as ~id, where it runs a piece backwards.
function decodeTracks(tracks) {
  const pieces = tracks.pieces.map((flat) => {
    const coords = [];
    let x = 0, y = 0;
    for (let i = 0; i < flat.length; i += 2) {
      x += flat[i];
      y += flat[i + 1];
      coords.push([x / 1e5, y / 1e5]);
    }
    return coords;
  });
  const hopPath = new Map(tracks.hops.map(([group, a, b, path]) => [`${group} ${a} ${b}`, path]));
  return { pieces, hopPath, snap: tracks.snap };
}

const trackGroup = (cat) => (cat === 'tgv' ? 'tgv' : 'rail');

// Everything reachable from one stop without changing. A pattern is only
// followed from the point this station is in it, and only where the train lets
// passengers board here and alight there: a TGV that sets down only on its way
// into Paris does not make the suburbs a destination.
function stationReach(net, code, coaches = true) {
  const from = net.at.get(code);
  if (from === undefined) return null;
  const { no_pickup: noPickup, no_drop_off: noDrop } = routes.index.flags;
  const products = routes.index.products;
  const dests = new Map();         // stop -> { runs, fastest, cat, names }
  const hops = new Map();          // "cat a b" -> { cat, a, b, runs }
  const lines = new Map();         // terminus -> { runs, cats, names, calls: stop -> { runs, fastest }, next }
  let departures = 0;
  for (const [k, pos] of net.through[from]) {
    const [product, stops, times, runs, flags] = net.patterns[k];
    if (pos === stops.length - 1 || (flags && flags[pos] & noPickup)) continue;
    const { categorie: cat, nom } = products[product];
    if (cat === 'car' && !coaches) continue;
    if (cat !== 'car') departures += runs;
    const end = stops[stops.length - 1];
    const line = lines.get(end) || { runs: 0, cats: new Set(), names: new Set(), calls: new Map(), next: new Map() };
    line.runs += runs;
    line.cats.add(cat);
    line.names.add(nom);
    lines.set(end, line);
    let prev = null;
    for (let j = pos + 1; j < stops.length; j++) {
      const a = Math.min(stops[j - 1], stops[j]);
      const b = Math.max(stops[j - 1], stops[j]);
      const key = `${cat} ${a} ${b}`;
      const hop = hops.get(key) || { cat, a, b, runs: 0 };
      hop.runs += runs;
      hops.set(key, hop);
      if ((flags && flags[j] & noDrop) || stops[j] === from) continue;
      const call = line.calls.get(stops[j]) || { runs: 0, fastest: Infinity };
      call.runs += runs;
      call.fastest = Math.min(call.fastest, times[j] - times[pos]);
      line.calls.set(stops[j], call);
      // Which call follows which, to put every train's calls in one order.
      if (prev !== null) {
        if (!line.next.has(prev)) line.next.set(prev, new Set());
        line.next.get(prev).add(stops[j]);
      }
      prev = stops[j];
      const d = dests.get(stops[j]) || { runs: 0, fastest: Infinity, cats: new Set(), names: new Set() };
      d.runs += runs;
      d.fastest = Math.min(d.fastest, times[j] - times[pos]);
      d.cats.add(cat);
      d.names.add(nom);
      dests.set(stops[j], d);
    }
  }
  lines.delete(from);              // a loop back to where it started is not a destination
  return { from, dests, hops, departures, lines };
}

// The calls of every train to one terminus, in running order. Trains to the same
// place mostly call at the same stations, some skipping a few, so their orders
// agree and merge into one sequence; where they do not (a train routed another
// way), the stop reached sooner goes first.
function callOrder(line) {
  const waiting = new Map([...line.calls.keys()].map((s) => [s, 0]));
  for (const nexts of line.next.values()) for (const n of nexts) waiting.set(n, waiting.get(n) + 1);
  const order = [];
  const left = new Set(line.calls.keys());
  const ready = (s) => waiting.get(s) <= 0;
  while (left.size) {
    let pick = null;
    for (const s of left) {
      if (pick === null || (ready(s) && !ready(pick))) pick = s;
      else if (ready(s) === ready(pick) && line.calls.get(s).fastest < line.calls.get(pick).fastest) pick = s;
    }
    left.delete(pick);
    order.push(pick);
    for (const n of line.next.get(pick) || []) waiting.set(n, waiting.get(n) - 1);
  }
  return order;
}

// The fastest kind of train that goes there, for the colour of its row.
const bestCat = (cats) => (ROUTE_CATS.find(([id]) => cats.has(id)) || ROUTE_CATS[2])[0];

// Each piece of track is drawn once per kind of train, as thick as the number of
// this station's trains that run along it — so where two routes share a line,
// the line carries both. A hop with no routed track is drawn straight.
function drawRoutes(map, net, reach) {
  const point = (i) => [net.stops[i][2], net.stops[i][3]];
  const has = (i) => net.stops[i][2] != null;
  const rank = new Map(ROUTE_CATS.map(([id], i) => [id, ROUTE_CATS.length - i]));
  const line = (cat, runs, coordinates) => ({
    type: 'Feature',
    properties: { cat, day: runs / 7, rank: rank.get(cat) },
    geometry: { type: 'LineString', coordinates },
  });
  const onTrack = new Map();       // "cat piece" -> runs
  const spurs = new Map();         // "cat stop" -> runs: station dot to where it meets the track
  const features = [];
  for (const hop of reach.hops.values()) {
    if (!has(hop.a) || !has(hop.b)) continue;
    const path = hop.cat === 'car' ? null : net.hopPath.get(`${trackGroup(hop.cat)} ${hop.a} ${hop.b}`);
    if (!path) {
      features.push(line(hop.cat, hop.runs, [point(hop.a), point(hop.b)]));
      continue;
    }
    for (const p of path) {
      const key = `${hop.cat} ${p < 0 ? ~p : p}`;
      onTrack.set(key, (onTrack.get(key) || 0) + hop.runs);
    }
    // The busiest hop at a stop, not their sum: a train calling there uses both.
    for (const stop of [hop.a, hop.b]) {
      spurs.set(`${hop.cat} ${stop}`, Math.max(spurs.get(`${hop.cat} ${stop}`) || 0, hop.runs));
    }
  }
  for (const [key, runs] of onTrack) {
    const [cat, piece] = key.split(' ');
    features.push(line(cat, runs, net.pieces[Number(piece)]));
  }
  for (const [key, runs] of spurs) {
    const [cat, stop] = key.split(' ');
    const snap = net.snap[stop];
    if (snap) features.push(line(cat, runs, [point(Number(stop)), snap]));
  }
  for (const [stop, d] of reach.dests) {
    if (!has(stop)) continue;
    features.push({
      type: 'Feature',
      properties: { kind: 'stop', name: net.stops[stop][1], cat: bestCat(d.cats), day: d.runs / 7 },
      geometry: { type: 'Point', coordinates: point(stop) },
    });
  }
  if (has(reach.from)) {
    features.push({
      type: 'Feature',
      properties: { kind: 'origin', name: net.stops[reach.from][1], day: 0 },
      geometry: { type: 'Point', coordinates: point(reach.from) },
    });
  }
  map.getSource(ROUTES.source).setData({ type: 'FeatureCollection', features });
  return features;
}

function fmtDuration(min) {
  if (min < 60) return `${min} min`;
  return `${Math.floor(min / 60)}h${String(min % 60).padStart(2, '0')}`;
}

// A weekly count as a traveller would say it: 40 a day, or 3 a week.
function fmtFrequency(runs) {
  return runs >= 7 ? `${Math.round(runs / 7)}/day` : `${runs}/week`;
}

function stationBlock(map, props, lngLat) {
  const block = el('div', 'insp-group');
  block.appendChild(el('h3', null, `Direct trains from ${props.nom}`));
  const body = block.appendChild(el('div', 'route-body'));
  body.appendChild(el('p', 'insp-empty', 'Loading the timetable…'));

  // Google's page for the station carries its live departure board.
  const links = el('div', 'insp-links');
  const maps = el('a', 'insp-action', 'Departures (Google) ↗');
  maps.target = '_blank';
  maps.rel = 'noopener noreferrer';
  maps.title = `Google Maps' page for ${props.nom} station, with its departure board`;
  maps.href = `https://www.google.com/maps/search/${encodeURIComponent(`Gare ${props.nom}`)}`
    + `/@${lngLat.lat.toFixed(5)},${lngLat.lng.toFixed(5)},15z`;
  links.appendChild(maps);
  block.appendChild(links);
  block.appendChild(el('div', 'insp-attr route-notes'));

  const run = ++routes.run;
  loadNetwork().then((net) => {
    if (run !== routes.run) return;              // another click has moved on
    renderStation(map, net, props, body);
  }).catch((err) => {
    if (run !== routes.run) return;
    body.innerHTML = '';
    body.appendChild(el('p', 'insp-empty', `Could not load the timetable: ${err.message}`));
  });
  return block;
}

function renderStation(map, net, props, body) {
  body.innerHTML = '';
  const links = body.nextElementSibling;
  const notes = links.nextElementSibling;
  notes.textContent = routes.index.notes.join(' ');
  const reach = props.code_uic ? stationReach(net, props.code_uic) : null;
  if (!reach || !reach.dests.size) {
    clearRoutes(map);
    body.appendChild(el('p', 'insp-empty', reach
      ? 'Trains stop here but nothing boards in the timetable week.'
      : 'Not in SNCF\'s TER, TGV and Intercités timetable. Île-de-France suburban stations are timetabled by IDFM.'));
    return;
  }
  // Drawn and listed without coaches unless they are switched on; the summary
  // still counts the places only a coach reaches.
  const shown = routes.coaches ? reach : stationReach(net, props.code_uic, false);
  const features = drawRoutes(map, net, shown);

  const byRail = [...reach.dests.values()].filter((d) => [...d.cats].some((c) => c !== 'car')).length;
  const summary = el('div', 'route-summary');
  summary.append(
    el('strong', null, reach.departures >= 7 ? `${Math.round(reach.departures / 7)} trains a day` : `${reach.departures} trains a week`),
    document.createTextNode(` to ${byRail} station${byRail === 1 ? '' : 's'}`
      + (reach.dests.size > byRail ? `, plus ${reach.dests.size - byRail} by coach only` : '')),
  );
  body.appendChild(summary);

  const present = new Set([...reach.hops.values()].map((h) => h.cat));
  const key = body.appendChild(el('div', 'route-key'));
  for (const [id, colour, label] of ROUTE_CATS) {
    if (!present.has(id)) continue;
    const coach = id === 'car';
    const item = key.appendChild(el(coach ? 'button' : 'span', `route-key-item ${id}`));
    item.appendChild(el('i')).style.setProperty('--c', colour);
    item.appendChild(document.createTextNode(label));
    if (coach) {
      item.type = 'button';
      item.setAttribute('aria-pressed', String(routes.coaches));
      item.title = routes.coaches ? 'Hide coach routes' : 'Show coach routes — drawn as straight lines';
      item.addEventListener('click', () => {
        routes.coaches = !routes.coaches;
        renderStation(map, net, props, body);
      });
    }
  }

  // One entry per terminus, busiest first: where the trains are going, then
  // everywhere they call on the way, in order.
  const colour = new Map(ROUTE_CATS.map(([id, c]) => [id, c]));
  const dot = (cats) => {
    const i = el('i', 'route-dot');
    i.style.setProperty('--c', colour.get(bestCat(cats)));
    if (!(cats.size === 1 && cats.has('car'))) i.classList.add('rail');
    return i;
  };
  const lines = [...shown.lines.entries()]
    .filter(([end, line]) => line.calls.has(end))
    .sort((a, b) => b[1].runs - a[1].runs || a[1].calls.get(a[0]).fastest - b[1].calls.get(b[0]).fastest);
  const list = body.appendChild(el('div', 'route-lines'));
  const open = lines.length <= 3;
  lines.forEach(([end, line], i) => {
    const item = list.appendChild(el('div', 'route-line'));
    if (i >= ROUTE_LIST) item.hidden = true;
    const head = item.appendChild(el('button', 'route-line-head'));
    head.type = 'button';
    head.title = [...line.names].join(', ');
    const name = head.appendChild(el('span', 'route-line-name'));
    name.append(dot(line.cats), document.createTextNode(net.stops[end][1]));
    head.appendChild(el('span', 'num', fmtDuration(line.calls.get(end).fastest)));
    head.appendChild(el('span', 'num', fmtFrequency(line.runs)));

    const order = callOrder(line).filter((stop) => stop !== end);
    const via = item.appendChild(el('div', 'route-line-via'));
    // Only stops every one of these trains makes are "via"; the rest are
    // counted, so "via Mâcon" never hides that most Paris trains run non-stop.
    const every = order.filter((stop) => line.calls.get(stop).runs === line.runs);
    const names = (stops) => stops.slice(0, 3).map((stop) => net.stops[stop][1]).join(', ')
      + (stops.length > 3 ? ` +${stops.length - 3}` : '');
    const some = order.length - every.length;
    if (!order.length) via.textContent = 'non-stop';
    else if (!every.length) via.textContent = `some via ${names(order)}`;
    else via.textContent = `via ${names(every)}${some ? ` · some also call at ${some} more` : ''}`;

    const table = item.appendChild(el('table', 'route-list'));
    const tbody = table.appendChild(el('tbody'));
    for (const stop of [...order, end]) {
      const call = line.calls.get(stop);
      const tr = tbody.appendChild(el('tr', stop === end ? 'terminus' : null));
      // Fewer than all of this terminus's trains stop here.
      if (call.runs < line.runs) {
        tr.classList.add('partial');
        tr.title = `${fmtFrequency(call.runs)} of the ${fmtFrequency(line.runs)} to ${net.stops[end][1]} stop here`;
      }
      tr.append(el('td', null, net.stops[stop][1]), el('td', 'num', fmtDuration(call.fastest)),
                el('td', 'num', fmtFrequency(call.runs)));
    }
    const setOpen = (on) => {
      item.classList.toggle('open', on);
      head.setAttribute('aria-expanded', String(on));
    };
    if (order.length) {
      setOpen(open);
      head.addEventListener('click', () => setOpen(!item.classList.contains('open')));
    } else {
      item.classList.add('nonstop');   // nothing to unfold
      head.tabIndex = -1;
    }
  });
  if (lines.length > ROUTE_LIST) {
    const more = body.appendChild(el('button', 'insp-action', `All ${lines.length} destinations`));
    more.type = 'button';
    more.addEventListener('click', () => {
      for (const item of list.children) item.hidden = false;
      more.remove();
    });
  }

  const fit = links.insertBefore(el('button', 'insp-action', 'Zoom to routes'), links.firstChild);
  fit.type = 'button';
  fit.addEventListener('click', () => {
    const bounds = new maplibregl.LngLatBounds();
    for (const f of features) {
      const c = f.geometry.coordinates;
      if (f.geometry.type === 'Point') bounds.extend(c); else c.forEach((p) => bounds.extend(p));
    }
    map.fitBounds(bounds, { padding: { top: 40, bottom: 40, left: PANEL_W + 40, right: 380 }, maxZoom: 11 });
  });
}

// ---------------------------------------------------------- commune search
// Picks from the list of communes and nothing else: typing only filters, and a
// name that was typed but not chosen is cleared. Matching ignores case, accents,
// hyphens and apostrophes, and a word can match anywhere in the name, so
// "nazaire" finds Saint-Nazaire-le-Désert and "st etienne" finds Saint-Étienne.
const SEARCH_LIMIT = 60;
const SEARCH_ALIASES = { st: 'saint', ste: 'sainte' };
const search = { map: null, entries: null, loading: null, results: [], more: 0, active: -1, run: 0 };

// Lower case, no accents, anything that is not a letter or digit as a space —
// plus, for each character of the folded text, where it came from in the name,
// so a match can be marked in the name as written.
function foldName(name) {
  let text = '';
  const from = [];
  for (let i = 0; i < name.length; i++) {
    let f = name[i].normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
    f = f.replace(/œ/g, 'oe').replace(/æ/g, 'ae').replace(/[^a-z0-9]/g, ' ');
    for (const ch of f) { text += ch; from.push(i); }
  }
  return { text, from };
}

async function loadSearchEntries() {
  if (search.entries) return search.entries;
  if (!search.loading) {
    search.loading = (async () => {
      const index = await loadStatIndex();
      // Population orders the matches, so "saint" offers the big places first.
      let population = null;
      try { if (statField('population')) population = await loadColumn('population'); } catch { /* order by name */ }
      search.entries = index.codes.map((code, i) => {
        const { text, from } = foldName(index.names[i]);
        return { i, code, name: index.names[i], text, from, pop: population && Number.isFinite(population[i]) ? population[i] : 0 };
      });
      return search.entries;
    })().catch((err) => { search.loading = null; throw err; });
  }
  return search.loading;
}

function queryTokens(query) {
  return foldName(query).text.split(' ').filter(Boolean);
}

// One commune against the typed words: null when a word is missing, otherwise a
// rank (lower is better) and the stretches of the name to mark.
function matchEntry(entry, tokens, digits) {
  if (digits) {
    if (!entry.code.startsWith(digits)) return null;
    return { rank: entry.code === digits ? 0 : 1, marks: [] };
  }
  const marks = [];
  let wordStarts = true;
  let first = -1;
  for (const token of tokens) {
    let at = -1, len = token.length;
    for (const alt of [token, SEARCH_ALIASES[token]]) {
      if (!alt) continue;
      // Prefer the word start: "ain" should mark Ain, not the middle of Saint.
      const re = new RegExp(`(^| )${alt}`);
      const m = re.exec(entry.text);
      if (m) { at = m.index + m[1].length; len = alt.length; break; }
    }
    if (at < 0) {
      at = entry.text.indexOf(token);
      if (at < 0) return null;
      wordStarts = false;
    }
    if (first < 0) first = at;
    marks.push([entry.from[at], entry.from[at + len - 1] + 1]);
  }
  const whole = entry.text.trim() === tokens.join(' ');
  const rank = whole ? 0 : first === 0 ? 1 : wordStarts ? 2 : 3;
  return { rank, marks };
}

function markedName(name, marks) {
  const span = el('span', 'cs-name');
  const merged = marks.slice().sort((a, b) => a[0] - b[0]).reduce((out, m) => {
    const last = out[out.length - 1];
    if (last && m[0] <= last[1]) last[1] = Math.max(last[1], m[1]);
    else out.push([...m]);
    return out;
  }, []);
  let pos = 0;
  for (const [a, b] of merged) {
    if (a > pos) span.append(name.slice(pos, a));
    span.appendChild(el('mark', null, name.slice(a, b)));
    pos = b;
  }
  if (pos < name.length) span.append(name.slice(pos));
  return span;
}

function renderSearchResults(input, list, note) {
  list.replaceChildren();
  if (note) {
    list.appendChild(el('li', 'cs-note', note));
  } else {
    search.results.forEach(({ entry, marks }, k) => {
      const li = el('li', 'cs-opt');
      li.id = `cs-opt-${k}`;
      li.setAttribute('role', 'option');
      li.setAttribute('aria-selected', String(k === search.active));
      li.classList.toggle('active', k === search.active);
      li.append(markedName(entry.name, marks), el('span', 'cs-code', `(${entry.code})`));
      li.addEventListener('mousemove', () => setSearchActive(input, list, k));
      li.addEventListener('click', () => chooseSearchResult(input, list, k));
      list.appendChild(li);
    });
    if (search.more) list.appendChild(el('li', 'cs-note', `${search.more.toLocaleString('en')} more — keep typing to narrow the list`));
  }
  list.hidden = false;
  input.setAttribute('aria-expanded', 'true');
}

function setSearchActive(input, list, k) {
  if (k === search.active) return;
  const items = list.querySelectorAll('.cs-opt');
  if (!items.length) return;
  search.active = Math.max(0, Math.min(items.length - 1, k));
  items.forEach((li, j) => {
    li.classList.toggle('active', j === search.active);
    li.setAttribute('aria-selected', String(j === search.active));
  });
  const current = items[search.active];
  input.setAttribute('aria-activedescendant', current.id);
  current.scrollIntoView({ block: 'nearest' });
}

function closeSearchList(input, list) {
  list.hidden = true;
  input.setAttribute('aria-expanded', 'false');
  input.removeAttribute('aria-activedescendant');
  search.active = -1;
}

async function updateSearch(input, list) {
  const query = input.value;
  const tokens = queryTokens(query);
  if (!tokens.length) { closeSearchList(input, list); return; }
  const run = ++search.run;
  if (!search.entries) renderSearchResults(input, list, 'Loading communes…');
  let entries;
  try {
    entries = await loadSearchEntries();
  } catch (err) {
    if (run === search.run) renderSearchResults(input, list, `Could not load the commune list: ${err.message}`);
    return;
  }
  if (run !== search.run) return;

  const digits = /^\d[\dab]*$/i.test(query.trim()) ? query.trim().toUpperCase() : null;
  const found = [];
  for (const entry of entries) {
    const m = matchEntry(entry, tokens, digits);
    if (m) found.push({ entry, ...m });
  }
  found.sort((a, b) => a.rank - b.rank || b.entry.pop - a.entry.pop || a.entry.name.localeCompare(b.entry.name, 'fr'));
  search.results = found.slice(0, SEARCH_LIMIT);
  search.more = Math.max(0, found.length - SEARCH_LIMIT);
  search.active = search.results.length ? 0 : -1;
  if (!search.results.length) { renderSearchResults(input, list, 'No commune matches'); return; }
  renderSearchResults(input, list);
  input.setAttribute('aria-activedescendant', 'cs-opt-0');
}

function chooseSearchResult(input, list, k) {
  const hit = search.results[k];
  if (!hit) return;
  input.value = '';
  closeSearchList(input, list);
  input.blur();
  goToCommune(search.map, hit.entry.i);
}

let goToken = 0;

// Frame the commune roughly by its size — area from population ÷ density — so a
// village is not a dot and Arles is not cut off, then inspect it once the tiles
// under it have loaded.
async function goToCommune(map, i) {
  const index = state.stats.index;
  const code = index.codes[i];
  const center = [index.lon[i], index.lat[i]];
  const token = ++goToken;

  $('#inspect-title').textContent = `${index.names[i]} (${code})`;
  $('#inspect-body').replaceChildren(el('p', 'insp-empty', 'Loading…'));
  $('#inspect').hidden = false;
  highlightCommune(map, code);

  let zoom = 11;
  try {
    const [pop, dens] = await Promise.all([loadColumn('population'), loadColumn('densite_hab_km2')]);
    const areaKm2 = pop[i] / dens[i];
    if (Number.isFinite(areaKm2) && areaKm2 > 0) {
      const diameterM = 2 * Math.sqrt(areaKm2 / Math.PI) * 1000;
      const visiblePx = map.getCanvas().clientWidth - leftInset() - rightInset();
      const metresPerPx = (diameterM * 2.2) / Math.max(visiblePx, 200);
      zoom = Math.log2((78271.517 * Math.cos(center[1] * Math.PI / 180)) / metresPerPx);
      zoom = Math.max(8, Math.min(14, zoom));
    }
  } catch { /* keep zoom 11 */ }
  if (token !== goToken) return;

  map.flyTo({ center, zoom, padding: { left: leftInset(), right: rightInset(), top: 0, bottom: 0 } });
  map.once('moveend', () => {
    if (token !== goToken) return;
    map.once('idle', () => {
      if (token !== goToken) return;
      inspect(map, map.project(center), maplibregl.LngLat.convert(center), { code });
    });
  });
}

const leftInset = () => ($('#panel').classList.contains('hidden') ? 0 : PANEL_W);
const rightInset = () => ($('#inspect').hidden ? 0 : $('#inspect').offsetWidth);

function buildCommuneSearch(map) {
  search.map = map;
  const input = $('#commune-search');
  const list = $('#commune-results');

  input.addEventListener('focus', () => { loadSearchEntries().catch(() => {}); if (input.value) updateSearch(input, list); });
  input.addEventListener('input', () => updateSearch(input, list));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (list.hidden) { updateSearch(input, list); return; }
      setSearchActive(input, list, search.active + (e.key === 'ArrowDown' ? 1 : -1));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (!list.hidden && search.active >= 0) chooseSearchResult(input, list, search.active);
    } else if (e.key === 'Escape') {
      e.stopPropagation();
      if (!list.hidden) closeSearchList(input, list);
      else { input.value = ''; input.blur(); }
    }
  });
  // Nothing typed stands on its own: leaving the box without choosing clears it.
  input.addEventListener('blur', () => { input.value = ''; closeSearchList(input, list); });
  // Keep focus in the box while a result is clicked, or blur would clear the list first.
  list.addEventListener('mousedown', (e) => e.preventDefault());

  $('#search-open').addEventListener('click', () => {
    const pane = $('#inspect');
    if (pane.hidden) {
      $('#inspect-title').textContent = 'Communes';
      $('#inspect-body').replaceChildren(el('p', 'insp-empty', 'Search for a commune above, or click anywhere on the map.'));
      pane.hidden = false;
    }
    input.focus();
  });
}

// -------------------------------------------------------- climate table
// A month-by-month climate table for one commune, laid out like the ones on
// Wikipedia. Twelve rows of thirteen values for 35,000 communes is too much to
// ride in the tiles, so the pipeline writes one file per department and a
// department is fetched the first time one of its communes is opened.

const climate = { index: null, depts: new Map(), code: null, opener: null };

function climateFiles() {
  return ((state.manifest || {}).files || {}).meteo_climat || null;
}

async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} → HTTP ${res.status}`);
  return res.json();
}

// Colour stops per kind of row, close to the scales Wikipedia's tables use.
// Rows that sum over the year are coloured by their monthly average, so the
// Year cell sits on the same scale as the months beside it.
const CLIMATE_SCALES = {
  temp: [[-25, '#2c2c8c'], [-15, '#5555d8'], [-5, '#a8a8fa'], [0, '#eeeeff'], [5, '#fff3e3'], [10, '#ffdcb0'],
         [15, '#ffc07c'], [20, '#ff9c48'], [25, '#f47b2b'], [30, '#e3521b'], [35, '#c42e10'], [40, '#8c180a']],
  precip: [[0, '#ffffff'], [25, '#e8fae8'], [50, '#c2f0c2'], [100, '#86dc86'], [200, '#40b048'], [300, '#1f7a2a']],
  days: [[0, '#ffffff'], [5, '#e8e8fa'], [10, '#c9c9f4'], [15, '#a2a2ea'], [20, '#7a7adc'], [25, '#5252c8']],
  snow: [[0, '#ffffff'], [2, '#eaf4fb'], [5, '#cde5f6'], [10, '#98c8ec'], [15, '#66abdf'], [25, '#3583c6']],
  humidity: [[40, '#ffffff'], [60, '#e0e8ff'], [75, '#a3bbf5'], [85, '#5f82e6'], [95, '#2d4fbf']],
  solar: [[0, '#9a9a8a'], [20, '#d6d6c4'], [60, '#efefb4'], [120, '#f7f06c'], [180, '#ffe414'], [240, '#ffc400']],
};

function hexRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function climateColour(scale, value) {
  const stops = CLIMATE_SCALES[scale];
  let rgb;
  if (value <= stops[0][0]) rgb = hexRgb(stops[0][1]);
  else if (value >= stops[stops.length - 1][0]) rgb = hexRgb(stops[stops.length - 1][1]);
  else {
    const i = stops.findIndex(([v]) => v > value);
    const [v0, c0] = stops[i - 1];
    const [v1, c1] = stops[i];
    const t = (value - v0) / (v1 - v0);
    const a = hexRgb(c0);
    const b = hexRgb(c1);
    rgb = a.map((x, k) => Math.round(x + (b[k] - x) * t));
  }
  const luminance = (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255;
  return { bg: `rgb(${rgb.join(',')})`, fg: luminance < 0.5 ? '#fff' : '#111' };
}

function climateNumber(value, decimals) {
  if (value === null || value === undefined) return '—';
  const text = Math.abs(value).toLocaleString('en', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  return value < 0 && Number(text.replace(/,/g, '')) !== 0 ? `\u2212${text}` : text;
}

function renderClimateTable(index, data) {
  const table = el('table', 'climate');
  const head = el('tr');
  head.appendChild(el('th', null, 'Month'));
  index.columns.forEach((c, i) => head.appendChild(el('th', i === 12 ? 'year' : null, c)));
  table.appendChild(el('thead')).appendChild(head);

  const body = table.appendChild(el('tbody'));
  index.rows.forEach((row, r) => {
    const tr = body.appendChild(el('tr'));
    const label = el('th', null, row.label);
    label.scope = 'row';
    label.appendChild(el('span', 'unit', ` ${row.unit === 'days' ? '' : row.unit}`.trimEnd()));
    if (row.check) label.title = row.check;
    tr.appendChild(label);
    const summed = ['precip', 'days', 'snow', 'solar'].includes(row.scale);
    data.rows[r].forEach((value, i) => {
      const td = el('td', i === 12 ? 'year' : null, climateNumber(value, row.decimals));
      if (value !== null) {
        const { bg, fg } = climateColour(row.scale, i === 12 && summed ? value / 12 : value);
        td.style.background = bg;
        td.style.color = fg;
      }
      tr.appendChild(td);
    });
  });

  const wrap = el('div');
  wrap.appendChild(el('div', 'climate-scroll')).appendChild(table);
  const notes = wrap.appendChild(el('div', 'climate-notes'));
  for (const text of index.notes) notes.appendChild(el('p', null, text));
  const details = notes.appendChild(el('details'));
  details.appendChild(el('summary', null, 'How accurate each row is'));
  const list = details.appendChild(el('dl'));
  for (const row of index.rows) {
    list.append(el('dt', null, row.label), el('dd', null, row.check));
  }
  notes.appendChild(el('p', null, index.attribution));
  return wrap;
}

async function openClimateTable(code, name, opener) {
  const files = climateFiles();
  const modal = $('#climate');
  const body = $('#climate-body');
  climate.code = code;
  climate.opener = opener || null;
  $('#climate-title').textContent = `Climate data for ${name || code}`;
  $('#climate-sub').textContent = '';
  body.replaceChildren(el('p', 'muted', 'Loading…'));
  modal.hidden = false;
  $('#climate-close').focus();

  const dep = code.slice(0, 2);
  try {
    if (!climate.index) climate.index = fetchJson(files.index);
    if (!climate.depts.has(dep)) climate.depts.set(dep, fetchJson(`${files.dir}/${dep}.json`));
    const [index, communes] = await Promise.all([climate.index, climate.depts.get(dep)]);
    if (climate.code !== code || modal.hidden) return;      // closed, or another commune opened meanwhile
    const data = communes[code];
    if (!data) {
      body.replaceChildren(el('p', 'muted', 'No climate data for this commune.'));
      return;
    }
    const alt = data.alt == null ? '' : ` · where residents live, ${data.alt.toLocaleString('en')} m up`;
    $('#climate-sub').textContent = `Averages of ${index.period}${alt}`;
    body.replaceChildren(renderClimateTable(index, data));
  } catch (err) {
    // A failed request is not cached, so opening the table again retries it.
    climate.index = null;
    climate.depts.delete(dep);
    if (climate.code === code) body.replaceChildren(el('p', 'muted', `Could not load the climate table: ${err.message}`));
  }
}

function closeClimateTable() {
  const modal = $('#climate');
  if (modal.hidden) return;
  modal.hidden = true;
  climate.code = null;
  if (climate.opener && document.contains(climate.opener)) climate.opener.focus();
  climate.opener = null;
}

// ----------------------------------------------------------- correlator
// Pick two commune statistics and the map answers where they move together.
//
// The arithmetic is in correlate.js. What lives here is the plumbing: fetching
// the two columns, handing the result to the map, and drawing the scatter that
// stops a single r from being read as the whole story.
//
// The result rides on a synthetic layer, `__correlation`, which looks like a
// manifest entry in every respect except that nothing published it. That is
// deliberate: it buys the stack, the opacity slider, the in-row scale, the
// legend, the attribution line and the shareable hash without one special case
// in any of them. Its `paint` block is rewritten in place when the selection
// changes, and everything that reads a paint block follows.

const CORR_ID = '__correlation';

// PRGn, nine classes. Diverging, colourblind-safe, and used by no published
// layer — a correlation map must not be mistakable for a data map.
const CORR_COLORS = ['#762a83', '#9970ab', '#c2a5cf', '#e7d4e8', '#f4f1f4',
                     '#d9f0d3', '#a6dba0', '#5aae61', '#1b7837'];
const CORR_BREAKS = [-0.75, -0.5, -0.25, -0.05, 0.05, 0.25, 0.5, 0.75];
// z-score products and differences are unbounded and heavily peaked at zero, so
// their classes are tight in the middle and open-ended at both ends.
const AGREE_BREAKS = [-2, -1, -0.4, -0.1, 0.1, 0.4, 1, 2];
const GAP_BREAKS = [-2, -1, -0.5, -0.15, 0.15, 0.5, 1, 2];

// PuOr, nine classes. A second diverging scheme, because the gap map answers a
// different question from the other two and must not be mistaken for them:
// purple is B running ahead of A, orange is A running ahead of B.
const GAP_COLORS = ['#542788', '#8073ac', '#b2abd2', '#d8daeb', '#f5f2ef',
                    '#fee0b6', '#fdb863', '#e08214', '#b35806'];

const CORR_MODES = [
  { id: 'local', label: 'Local r', colors: CORR_COLORS, breaks: CORR_BREAKS,
    hint: 'Pearson r recomputed inside a disc around every commune. Green: here the two rise and fall together. Purple: here one rises as the other falls.' },
  { id: 'agree', label: 'Agreement', colors: CORR_COLORS, breaks: AGREE_BREAKS,
    hint: 'Each commune’s own contribution to the national r — the product of its two z-scores, which averages back to exactly that r. Green: high on both, or low on both. Purple: high on one and low on the other.' },
  { id: 'gap', label: 'Gap', colors: GAP_COLORS, breaks: GAP_BREAKS,
    hint: 'How far ahead of {B} each commune’s {A} stands, as a difference of z-scores. Orange: {A} is high where {B} is low. Purple: the reverse. A commune that is high on both, or low on both, sits in the middle — being rich and expensive is not a mismatch, and neither is being poor and cheap.' },
];

// What the two ends of the scale mean depends on what is being mapped, so the
// highlight chips are named per mode rather than once for all of them. The ids
// are shared, because the class ranges they keep are the same either way.
const SIDE_IDS = ['both', 'pos', 'neg'];
const SIDE_LABELS = {
  local: { pos: ['Together', 'Highlight only where the two move together'],
           neg: ['Opposed', 'Highlight only where the two move in opposite directions'] },
  agree: { pos: ['Together', 'Highlight only the communes that are high on both or low on both'],
           neg: ['Opposed', 'Highlight only the communes that are high on one and low on the other'] },
  gap:   { pos: ['A ≫ B', 'Highlight only where {A} is high and {B} is low'],
           neg: ['B ≫ A', 'Highlight only where {B} is high and {A} is low'] },
};

// {A} and {B} stand for the two chosen statistics. Placeholders rather than bare
// letters, so substituting them cannot also swallow the article in "A commune".
function namePair(text) {
  const fx = statField(state.corr.x), fy = statField(state.corr.y);
  if (!fx || !fy) return text.replace(/\{A\}/g, 'A').replace(/\{B\}/g, 'B');
  return text.replace(/\{A\}/g, shortLabel(fx.label)).replace(/\{B\}/g, shortLabel(fy.label));
}

// The gap chips read "A" and "B"; spelling both out in the tooltip is what makes
// them readable without counting dropdowns.
function sidesFor(mode) {
  const names = SIDE_LABELS[mode] || SIDE_LABELS.local;
  return [
    { id: 'both', label: 'Both', title: 'Show the whole scale' },
    ...['pos', 'neg'].map((id) => ({ id, label: names[id][0], title: namePair(names[id][1]) })),
  ];
}

// `both` keeps every class. The other two blank out the middle class and
// everything beyond it on the side not asked for, so the highlighted end is the
// only colour on the map and the basemap shows through the rest.
const SIDE_KEEP = { both: [0, 8], pos: [5, 8], neg: [0, 3] };

const MIN_NEIGHBOURS = 12;   // below this a local r is noise, so it is left blank

function corrPalette(side, colors) {
  const [lo, hi] = SIDE_KEEP[side] || SIDE_KEEP.both;
  return colors.map((c, i) => (i >= lo && i <= hi ? c : BLANK));
}

const corrMode = () => CORR_MODES.find((m) => m.id === state.corr.mode) || CORR_MODES[0];

const statField = (name) => state.stats.byName.get(name) || null;

// Field labels are full sentences, because the inspect panel wants the whole
// gloss. A dropdown inside a 330px panel wants the head of it.
const shortLabel = (text) => String(text).split(' — ')[0].split(' (')[0];

const fmtR = (r) => (Number.isFinite(r) ? (r < 0 ? '−' : '+') + Math.abs(r).toFixed(2) : '—');

// How to read an r, in words, because "0.41" means nothing on its own.
function strength(r) {
  if (!Number.isFinite(r)) return 'no relationship could be computed';
  const a = Math.abs(r);
  if (a < 0.1) return 'no linear relationship nationally';
  const how = a < 0.3 ? 'a weak' : a < 0.5 ? 'a moderate' : a < 0.7 ? 'a strong' : 'a very strong';
  return `${how} ${r > 0 ? 'positive' : 'negative'} relationship nationally`;
}

// ------------------------------------------------------------ data loading
// The choropleths render from a tileset that drops features at low zoom, so a
// correlation read off the screen would be a correlation over a biased sample of
// France. These columns are the same table the tiles were cut from, published
// whole — one file per stat, so a pair costs two fetches and not the lot.
async function loadColumn(name) {
  if (state.stats.columns.has(name)) return state.stats.columns.get(name);
  const field = statField(name);
  const res = await fetch(field.file);
  if (!res.ok) throw new Error(`${field.file} → HTTP ${res.status}`);
  // NaN rather than null, so a commune with no published figure drops out of
  // every sum without a branch inside the inner loop.
  const col = Float64Array.from(await res.json(), (v) => (v === null ? NaN : v));
  state.stats.columns.set(name, col);
  return col;
}

async function loadStatIndex() {
  if (state.stats.index) return state.stats.index;
  const res = await fetch(state.manifest.stats.index);
  if (!res.ok) throw new Error(`${state.manifest.stats.index} → HTTP ${res.status}`);
  const raw = await res.json();
  state.stats.index = {
    codes: raw.codes, names: raw.names, dep: raw.dep,
    lon: Float64Array.from(raw.lon), lat: Float64Array.from(raw.lat),
    row: new Map(raw.codes.map((code, i) => [code, i])),
  };
  return state.stats.index;
}

// ---------------------------------------------------------------- compute
function computeCorrelation() {
  const c = state.corr;
  const x = state.stats.columns.get(c.x);
  const y = state.stats.columns.get(c.y);
  const rows = Correlate.finitePairs(x, y);
  const result = {
    rows,
    global: Correlate.pearson(x, y, rows),
    spearman: Correlate.spearman(x, y, rows),
  };

  if (c.mode === 'local') {
    Object.assign(result, Correlate.localCorrelation({
      x, y, lon: state.stats.index.lon, lat: state.stats.index.lat, rows,
      radiusKm: c.radius, minN: MIN_NEIGHBOURS,
    }));
  } else {
    const z = c.mode === 'gap' ? Correlate.gap(x, y, rows) : Correlate.agreement(x, y, rows);
    result.values = z.values;
    result.statsX = z.statsX;       // the scatter draws its quadrant lines from these
    result.statsY = z.statsY;
    result.counts = null;
    result.radii = null;
  }
  result.mapped = result.values.reduce((n, v) => n + (Number.isFinite(v) ? 1 : 0), 0);
  c.result = result;
}

// ------------------------------------------------------------ map painting
// One setFeatureState per commune that got a value: a few thousand to 35,000
// calls, costing about a tenth of a second. The alternative — a match expression
// with 35,000 branches — has to be re-parsed by the style on every change, and
// is both slower and unreadable.
//
// Both computed layers paint the one commune source, and feature state belongs
// to the source rather than to a layer. So each tool writes under its own key
// and clears only the communes it wrote last time — a blanket removeFeatureState
// here would wipe the other tool's map as a side effect.
const paintedCodes = new Map();   // feature-state key -> INSEE codes written last time

function paintFeatureState(map, layerId, key, values) {
  const layer = state.byId.get(layerId);
  if (!layer || !map.getSource(sourceKeyFor(layer))) return;
  const target = { source: sourceKeyFor(layer), sourceLayer: layer.source_layer };
  for (const code of paintedCodes.get(key) || []) {
    map.setFeatureState({ ...target, id: code }, { [key]: null });
  }
  const written = [];
  if (values) {
    const codes = state.stats.index.codes;
    for (let i = 0; i < values.length; i++) {
      if (!Number.isFinite(values[i])) continue;
      map.setFeatureState({ ...target, id: codes[i] }, { [key]: values[i] });
      written.push(codes[i]);
    }
  }
  paintedCodes.set(key, written);
}

function paintCorrelation(map) {
  const c = state.corr;
  paintFeatureState(map, CORR_ID, 'v', c.result ? c.result.values : null);
}

// The paint block is the single source of truth for the map, the in-row scale
// and the legend, so the mode, the highlighted side and the two chosen stats all
// land here and nowhere else.
function corrPaint() {
  const c = state.corr;
  const layer = state.byId.get(CORR_ID);
  const mode = corrMode();
  const fx = statField(c.x), fy = statField(c.y);
  const unit = { local: `r within ${c.radius} km`, agree: 'z × z', gap: 'z(A) − z(B)' };

  layer.paint = {
    source: 'feature-state',
    property: 'v',
    scale: 'numeric',
    breaks: mode.breaks,
    colors: corrPalette(c.side, mode.colors),
    no_data_color: BLANK,
    unit: unit[c.mode],
  };
  const join = c.mode === 'gap' ? ' − ' : ' × ';
  layer.label = fx && fy ? `${shortLabel(fx.label)}${join}${shortLabel(fy.label)}` : 'Correlation';
  layer.legend_note = namePair(mode.hint);
  layer.attribution = fx && fy
    ? [...new Set([fx.attribution, fy.attribution].filter(Boolean))].join(' · ')
    : '';
}

function applyCorrColors(map) {
  if (map.getLayer(CORR_ID)) {
    map.setPaintProperty(CORR_ID, 'fill-color', colorExpression(state.byId.get(CORR_ID).paint));
  }
}

// ------------------------------------------------------------------- run
async function runCorrelation(map, { recolourOnly = false, show = true } = {}) {
  const c = state.corr;
  if (!c.x || !c.y || c.x === c.y) {
    c.result = null;
    if (state.visible.has(CORR_ID)) toggleLayer(map, CORR_ID, false);
    paintCorrelation(map);
    renderCorrelator(map);
    return;
  }

  // Changing which end is highlighted moves no numbers, so it must not pay for a
  // recompute or for re-writing 35,000 feature states.
  if (recolourOnly && c.result) {
    corrPaint();
    applyCorrColors(map);
    applyStack(map);
    renderCorrelator(map);
    return;
  }

  // A second selection while the first is still fetching must not be overwritten
  // by whichever request happens to land last.
  const token = (c.run = (c.run || 0) + 1);
  c.busy = true;
  c.error = null;
  renderCorrelator(map);
  try {
    await loadStatIndex();
    await Promise.all([loadColumn(c.x), loadColumn(c.y)]);
  } catch (err) {
    if (token !== c.run) return;
    c.busy = false;
    c.error = err.message;
    renderCorrelator(map);
    return;
  }
  if (token !== c.run) return;
  // Let the browser paint "Working…" before the main thread goes away for the
  // tenth of a second the neighbourhood pass takes.
  await new Promise((done) => requestAnimationFrame(done));
  if (token !== c.run) return;

  computeCorrelation();
  corrPaint();
  c.busy = false;

  applyCorrColors(map);
  paintCorrelation(map);
  // `show` is false only when a shared link restored a selection the user had
  // deliberately switched off, so reopening it must not switch it back on.
  if (show && !state.visible.has(CORR_ID)) toggleLayer(map, CORR_ID, true);
  else applyStack(map);
  renderCorrelator(map);
}

// --------------------------------------------------------------- scatter
// r is one number and it hides everything: a wedge, a floor, two clouds, one
// outlier doing all the work. The scatter is small, but it is the honest half of
// this panel, so it is not optional.
const SC = { w: 300, h: 182, l: 38, r: 8, t: 8, b: 30 };
const MAX_POINTS = 3500;
const SVG_NS = 'http://www.w3.org/2000/svg';

function fmtAxis(n) {
  const a = Math.abs(n);
  if (a >= 1e6) return `${+(n / 1e6).toFixed(1)}M`;
  if (a >= 1e4) return `${Math.round(n / 1e3)}k`;
  if (a >= 100) return String(Math.round(n));
  return String(+n.toFixed(a >= 1 ? 1 : 2));
}

function renderScatter() {
  const svg = $('#corr-scatter');
  const c = state.corr;
  svg.innerHTML = '';
  if (!c.result) return;

  const x = state.stats.columns.get(c.x), y = state.stats.columns.get(c.y);
  const { rows, global } = c.result;
  // Axes are trimmed to the 1st–99th percentile. One commune with a €14,000/m²
  // flat would otherwise squash the rest of France into the bottom-left pixel.
  const sx = Correlate.sortedValues(x, rows), sy = Correlate.sortedValues(y, rows);
  const x0 = Correlate.quantile(sx, 0.01), x1 = Correlate.quantile(sx, 0.99);
  const y0 = Correlate.quantile(sy, 0.01), y1 = Correlate.quantile(sy, 0.99);
  const spanX = (x1 - x0) || 1, spanY = (y1 - y0) || 1;
  const px = (v) => SC.l + ((v - x0) / spanX) * (SC.w - SC.l - SC.r);
  const py = (v) => SC.h - SC.b - ((v - y0) / spanY) * (SC.h - SC.t - SC.b);

  const add = (tag, attrs, text) => {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text != null) node.textContent = text;
    svg.appendChild(node);
    return node;
  };

  add('rect', { x: SC.l, y: SC.t, width: SC.w - SC.l - SC.r, height: SC.h - SC.t - SC.b,
                fill: '#fbfbfc', stroke: '#e5e7eb' });

  // Agreement and Gap both divide the cloud at its two means, so draw them: the
  // four corners of this cross are the four things those maps can highlight.
  if (c.mode !== 'local' && c.result.statsX) {
    const guide = { stroke: '#9ca3af', 'stroke-width': 0.8, 'stroke-dasharray': '2 2' };
    const mx = px(c.result.statsX.mean), my = py(c.result.statsY.mean);
    if (mx > SC.l && mx < SC.w - SC.r) add('line', { x1: mx, y1: SC.t, x2: mx, y2: SC.h - SC.b, ...guide });
    if (my > SC.t && my < SC.h - SC.b) add('line', { x1: SC.l, y1: my, x2: SC.w - SC.r, y2: my, ...guide });
  }

  // Every point in one path: 3,500 <circle> elements would cost the browser more
  // to lay out than the whole correlation costs to compute.
  const stride = Math.max(1, Math.ceil(rows.length / MAX_POINTS));
  let d = '', shown = 0;
  for (let k = 0; k < rows.length; k += stride) {
    const i = rows[k];
    if (x[i] < x0 || x[i] > x1 || y[i] < y0 || y[i] > y1) continue;
    d += `M${px(x[i]).toFixed(1)} ${py(y[i]).toFixed(1)}h0.01`;
    shown++;
  }
  add('path', { d, stroke: '#1d4ed8', 'stroke-width': 2.1, 'stroke-linecap': 'round',
                'stroke-opacity': shown > 1500 ? 0.2 : 0.4, fill: 'none' });

  // The least-squares line, fitted to the whole column rather than to the points
  // that happen to fall inside the trimmed axes, then clipped to the box.
  if (Number.isFinite(global.slope)) {
    const ya = global.intercept + global.slope * x0;
    const dy = global.slope * spanX;
    let t0 = 0, t1 = 1;
    if (dy !== 0) {
      const ta = (y0 - ya) / dy, tb = (y1 - ya) / dy;
      t0 = Math.max(0, Math.min(ta, tb));
      t1 = Math.min(1, Math.max(ta, tb));
    } else if (ya < y0 || ya > y1) {
      t1 = t0 - 1;                                  // the line misses the box entirely
    }
    if (t1 > t0) {
      add('line', {
        x1: px(x0 + t0 * spanX), y1: py(ya + t0 * dy),
        x2: px(x0 + t1 * spanX), y2: py(ya + t1 * dy),
        stroke: '#b91c1c', 'stroke-width': 1.4, 'stroke-dasharray': '4 3',
      });
    }
  }

  // The commune from the last click, so the inspect panel and the scatter are
  // looking at the same place.
  const fi = c.focus != null ? state.stats.index.row.get(c.focus) : undefined;
  if (fi !== undefined && Number.isFinite(x[fi]) && Number.isFinite(y[fi])) {
    add('circle', {
      cx: Math.max(SC.l, Math.min(SC.w - SC.r, px(x[fi]))),
      cy: Math.max(SC.t, Math.min(SC.h - SC.b, py(y[fi]))),
      r: 4.5, fill: 'none', stroke: '#111827', 'stroke-width': 1.6,
    });
  }

  const tick = { 'font-size': 8.5, fill: '#6b7280' };
  add('text', { x: SC.l, y: SC.h - SC.b + 10, ...tick }, fmtAxis(x0));
  add('text', { x: SC.w - SC.r, y: SC.h - SC.b + 10, 'text-anchor': 'end', ...tick }, fmtAxis(x1));
  add('text', { x: SC.l - 4, y: SC.h - SC.b, 'text-anchor': 'end', ...tick }, fmtAxis(y0));
  add('text', { x: SC.l - 4, y: SC.t + 7, 'text-anchor': 'end', ...tick }, fmtAxis(y1));
  add('text', { x: (SC.l + SC.w - SC.r) / 2, y: SC.h - 4, 'text-anchor': 'middle',
                'font-size': 9, fill: '#374151' },
      `${shortLabel(statField(c.x).label)} →    ↑ ${shortLabel(statField(c.y).label)}`);
  svg.setAttribute('aria-label',
    `Scatter plot of ${statField(c.x).label} against ${statField(c.y).label} over `
    + `${rows.length.toLocaleString('en')} communes. Pearson r is ${fmtR(global.r)}.`);
}

// ------------------------------------------------------------------- UI
function fillStatSelect(sel, value, onChange, { blank: blankLabel = '— choose a statistic —', exclude = null } = {}) {
  sel.innerHTML = '';
  const blank = el('option', null, blankLabel);
  blank.value = '';
  sel.appendChild(blank);

  const top = topItems();
  const groups = new Map([[top.label, top.stats.map((name) => state.stats.byName.get(name))]]);
  for (const f of state.stats.fields) {
    if (top.stats.includes(f.name)) continue;
    if (!groups.has(f.group)) groups.set(f.group, []);
    groups.get(f.group).push(f);
  }
  for (const [group, all] of groups) {
    const fields = all.filter((f) => !(exclude && exclude.has(f.name)));
    if (!fields.length) continue;
    const og = document.createElement('optgroup');
    og.label = group;
    for (const f of fields) {
      const opt = el('option', null, shortLabel(f.label) + (f.unit ? ` (${f.unit})` : ''));
      opt.value = f.name;
      opt.title = f.description || f.label;
      og.appendChild(opt);
    }
    sel.appendChild(og);
  }
  sel.value = value || '';
  if (!sel.dataset.bound) {
    sel.dataset.bound = '1';
    sel.addEventListener('change', () => onChange(sel.value || null));
  }
}

function chipRow(host, options, current, onPick) {
  host.innerHTML = '';
  for (const o of options) {
    const chip = el('button', 'chip', o.label);
    chip.type = 'button';
    chip.title = o.title || o.hint || '';
    chip.setAttribute('aria-pressed', String(o.id === current));
    chip.addEventListener('click', () => onPick(o.id));
    host.appendChild(chip);
  }
}

function corrHeadline() {
  const c = state.corr;
  const { global, rows } = c.result;
  const head = $('#corr-head');
  head.innerHTML = '';

  const big = el('div', 'corr-r');
  big.append(el('span', 'corr-r-val', fmtR(global.r)), el('span', 'corr-r-cap', 'Pearson r'));

  const facts = el('div', 'corr-facts');
  const fact = (k, v, title) => {
    const row = el('div', 'corr-fact');
    row.append(el('span', 'k', k), el('span', 'v', v));
    if (title) row.title = title;
    facts.appendChild(row);
  };
  fact('Spearman ρ', fmtR(c.result.spearman),
       'The same correlation computed on ranks. A long way from Pearson r means the relationship is real but not a straight line.');
  fact('Communes', rows.length.toLocaleString('en'),
       `Communes where both statistics are published, out of ${state.stats.index.codes.length.toLocaleString('en')}.`);
  fact('On the map', c.result.mapped.toLocaleString('en'),
       'Communes the current mode could give a value to.');

  head.append(big, facts, el('p', 'corr-read', `${strength(global.r)}.`));
}

function corrCaveats() {
  const c = state.corr;
  const { rows } = c.result;
  const share = rows.length / state.stats.index.codes.length;
  const parts = [namePair(corrMode().hint)];
  if (c.mode === 'local') {
    const blank = rows.length - c.result.mapped;
    parts.push(`A commune with fewer than ${MIN_NEIGHBOURS} neighbours carrying both values is left blank`
               + (blank > 0 ? ` — ${blank.toLocaleString('en')} of them here.` : '.'));
  }
  if (share < 0.35) {
    parts.push(`Only ${Math.round(share * 100)}% of communes publish both, so this describes part of France, not all of it.`);
  }
  parts.push('Communes are not people: a pattern across communes need not hold across the households inside them, '
             + 'and neighbouring communes are not independent observations. This describes, it does not explain.');
  return parts.join(' ');
}

function renderCorrelator(map) {
  const c = state.corr;
  const out = $('#corr-out');
  const hint = $('#corr-hint');
  const badge = $('#corr-badge');

  fillStatSelect($('#corr-x'), c.x, (v) => { c.x = v; c.focus = null; runCorrelation(map); });
  fillStatSelect($('#corr-y'), c.y, (v) => { c.y = v; c.focus = null; runCorrelation(map); });

  const say = (text) => {
    out.hidden = true;
    badge.hidden = true;
    hint.hidden = false;
    hint.textContent = text;
  };
  if (c.error) return say(`Could not load the stat columns: ${c.error}`);
  if (c.busy) { badge.hidden = true; hint.hidden = false; hint.textContent = 'Working…'; return; }
  if (c.x && c.y && c.x === c.y) return say('Pick two different statistics.');
  if (!c.result) {
    return say('Pick two commune statistics. The map then shows where they move together and where they don’t.');
  }

  hint.hidden = true;
  out.hidden = false;
  badge.hidden = false;
  badge.textContent = fmtR(c.result.global.r);

  corrHeadline();
  chipRow($('#corr-mode'), CORR_MODES, c.mode, (id) => {
    if (id === c.mode) return;
    c.mode = id;
    runCorrelation(map);
  });
  chipRow($('#corr-side'), sidesFor(c.mode), c.side, (id) => {
    if (id === c.side) return;
    c.side = id;
    runCorrelation(map, { recolourOnly: true });
  });

  $('#corr-radius-row').hidden = c.mode !== 'local';
  $('#corr-radius').value = c.radius;
  $('#corr-radius-val').textContent = `${c.radius} km`;
  $('#corr-note').textContent = corrCaveats();
  renderScatter();
}

// What the inspect panel adds for the commune under the cursor: the two raw
// values, and the local r of the disc this commune sits in.
function corrInspectRows(code) {
  const c = state.corr;
  if (!c.result || code == null) return null;
  const i = state.stats.index.row.get(code);
  if (i === undefined) return null;

  const rows = [];
  for (const name of [c.x, c.y]) {
    const f = statField(name);
    const v = state.stats.columns.get(name)[i];
    rows.push({ k: shortLabel(f.label), v: Number.isFinite(v) ? formatValue(v, f) : '—' });
  }
  const v = c.result.values[i];
  if (c.mode === 'local') {
    rows.push({ k: `r within ${Number.isFinite(c.result.radii[i]) ? c.result.radii[i] : c.radius} km`,
                v: fmtR(v) });
    if (c.result.counts[i]) rows.push({ k: 'Communes in that disc', v: String(c.result.counts[i]) });
  } else if (c.mode === 'gap') {
    // The two z-scores as well as their difference: "+1.8 against −0.9" says
    // which of the pair is doing the work, where the gap alone does not.
    const zx = (state.stats.columns.get(c.x)[i] - c.result.statsX.mean) / c.result.statsX.sd;
    const zy = (state.stats.columns.get(c.y)[i] - c.result.statsY.mean) / c.result.statsY.sd;
    rows.push({ k: 'Standard deviations from the mean', v: `${fmtR(zx)} and ${fmtR(zy)}` });
    rows.push({ k: 'Gap, z(A) − z(B)', v: Number.isFinite(v) ? fmtR(v) : '—' });
  } else {
    rows.push({ k: 'Agreement (z × z)', v: Number.isFinite(v) ? v.toFixed(2) : '—' });
  }
  return rows;
}

// A synthetic layer, built to look exactly like a published one so that the
// rest of this file can go on not knowing it is different. The correlator and
// the optimiser each own one.
function computedLayer({ id, label, z, paint }) {
  const commune = state.layers.find((l) => l.type === 'choropleth');
  if (!commune) return null;               // no commune tileset, nothing to paint onto
  return {
    id,
    label,
    group: label,
    type: 'choropleth',
    panel: false,                          // the tool that computes it owns its switch
    z,                                     // above the published choropleths, below the rest
    fields: [],
    source_id: id,
    source_name: 'Computed in your browser',
    source_url: '',
    fetched: '',
    attribution: '',
    source_file: commune.source_file,      // the same commune tileset, recoloured
    format: commune.format,
    source_layer: commune.source_layer,
    default_visible: false,
    default_opacity: 0.8,
    legend_note: '',
    paint,
  };
}

const correlationLayer = () => computedLayer({
  id: CORR_ID, label: 'Correlation', z: 12,
  paint: { source: 'feature-state', property: 'v', scale: 'numeric',
           breaks: CORR_BREAKS, colors: CORR_COLORS, no_data_color: BLANK, unit: 'r' },
});

// A section of the panel whose body folds away under its header. Folding only
// hides the controls; whatever the tool has put on the map stays there, and the
// header badge keeps its headline readable.
function collapsible(block, toggle, body, storageKey, openByDefault = true) {
  const setOpen = (open) => {
    toggle.setAttribute('aria-expanded', String(open));
    body.hidden = !open;
    block.classList.toggle('collapsed', !open);
    try { localStorage.setItem(storageKey, open ? '1' : '0'); } catch (err) { /* storage blocked */ }
  };
  let open = openByDefault;
  try {
    const saved = localStorage.getItem(storageKey);
    if (saved !== null) open = saved !== '0';
  } catch (err) { /* storage blocked */ }
  setOpen(open);
  toggle.addEventListener('click', () => setOpen(toggle.getAttribute('aria-expanded') !== 'true'));
}

function buildCorrelator(map) {
  const stats = state.manifest.stats;
  const block = $('#corr-block');
  if (!stats || !(stats.fields || []).length || !state.byId.has(CORR_ID)) {
    block.hidden = true;                   // built without stat columns; nothing to offer
    return;
  }
  collapsible(block, $('#corr-toggle'), $('#corr-body'), 'corr-open');

  $('#corr-swap').addEventListener('click', () => {
    const c = state.corr;
    [c.x, c.y] = [c.y, c.x];
    runCorrelation(map);
  });
  // Recompute on release, not on every pixel of the drag: each radius costs a
  // fresh neighbourhood pass over every commune.
  const slider = $('#corr-radius');
  slider.addEventListener('input', () => {
    state.corr.radius = Number(slider.value);
    $('#corr-radius-val').textContent = `${slider.value} km`;
  });
  slider.addEventListener('change', () => runCorrelation(map));

  renderCorrelator(map);
}

// ------------------------------------------------------------ optimiser
// Pick the statistics that matter, say which way is better and how much each
// counts, and the map shades the communes that fit best.
//
// The arithmetic is in optimise.js. As with the correlator, the result rides on
// a computed layer, `__optimiser`, so the stack, opacity, in-row scale, legend
// and hash all come for free. It paints under its own feature-state key, `opt`,
// so it can sit on the map alongside a correlation without either erasing the
// other.

const OPT_ID = '__optimiser';
const OPT_KEY = 'opt';
const OPT_MAX_WEIGHT = 5;
const OPT_TOP_N = 10;

// Plasma, best first. Used by no published layer, so a match map cannot be
// mistaken for a data map; the lowest passing class is a pale wash, distinct
// from the grey of a commune that is ruled out.
const OPT_CLASSES = [
  { color: '#0d0887', label: 'Top 1%' },
  { color: '#7e03a8', label: 'Top 5%' },
  { color: '#cc4778', label: 'Top 10%' },
  { color: '#f89540', label: 'Top 25%' },
  { color: '#f0f921', label: 'Top 50%' },
  { color: '#d8d0e6', label: 'Passes, lower half' },
];

// The absolute mode's ramp, on the match score itself: one hue, light to dark,
// ending on the rank classes' best colour. The pale end is meant to sink into
// the basemap — a near-zero match should barely show.
const OPT_RAMP = [
  [0, '#eceaf8'],
  [0.2, '#c3bdec'],
  [0.4, '#978fdc'],
  [0.6, '#6a5fc8'],
  [0.8, '#4131a8'],
  [1, '#0d0887'],
];

const OPT_SCORING = [
  { id: 'abs', label: 'Absolute', title: 'Without a ramp, each statistic scores on its natural scale (0–100 for a score) or between its 1st and 99th percentile values, on a log scale when the column is heavily skewed. The map shades the score itself, so weak matches look weak.' },
  { id: 'rank', label: 'Rank', title: 'Without a ramp, each statistic scores its percentile among communes, so some commune always looks excellent. The map shades by rank class.' },
];
const optAbs = () => state.opt.scoring === 'abs';

const OPT_DIRS = [
  { id: 'up', label: 'More is better', title: 'Higher values score better' },
  { id: 'down', label: 'Less is better', title: 'Lower values score better' },
];

const optimiserLayer = () => ({
  ...computedLayer({ id: OPT_ID, label: 'Best match', z: 13, paint: optPaintBlock() }),
  default_opacity: 1,                      // opaque, so no layer beneath shows through
});

// Communes the optimiser does not score — ruled out by a ramp, or missing a
// statistic — are painted rather than left clear, so the layers beneath never
// show through the match map. Grey, darker than the ramp's pale end, so a
// ruled-out commune is not read as a weak match.
const OPT_OUT_COLOR = '#b8b8b8';
const OPT_OUT_LABEL = 'Ruled out or no data';

function optPaintBlock() {
  if (optAbs()) {
    const r = state.opt.result;
    return {
      source: 'feature-state',
      property: OPT_KEY,
      scale: 'continuous',
      stops: OPT_RAMP,
      max: r && r.summary ? r.summary.best : NaN,
      no_data_color: OPT_OUT_COLOR,
      no_data_label: OPT_OUT_LABEL,
      unit: 'match score',
    };
  }
  return {
    source: 'feature-state',
    property: OPT_KEY,
    scale: 'ordinal',
    stops: OPT_CLASSES.map((c, i) => [i, c.color, c.label]),
    no_data_color: OPT_OUT_COLOR,
    no_data_label: OPT_OUT_LABEL,
  };
}

const fmtNum = (n) => (Number.isFinite(n) ? Number(n.toPrecision(4)).toLocaleString('en') : '—');
const fmtPct = (d) => `${Math.round(d * 100)}%`;
const fmtScore = (d) => d.toFixed(2);
const optNumber = (text) => (text === '' || text == null || !Number.isFinite(Number(text)) ? null : Number(text));

function computeOptimiser() {
  const o = state.opt;
  const n = state.stats.index.codes.length;
  const parts = o.criteria.map((crit) => {
    let col = state.stats.columns.get(crit.name);
    let d;
    if (optAbs()) {
      // A field published as a position points at the same thing on a natural
      // scale; absolute mode reads that instead.
      const via = optAbsColumn(crit.name);
      const scored = via ? state.stats.columns.get(via) : col;
      d = { ...Optimise.absoluteDesirability(scored, crit, statField(via || crit.name)), via };
      col = scored;
    } else {
      d = Optimise.desirability(col, crit);
    }
    // A ramp typed with both ends decides the direction; the chips follow it.
    if (d.ramp) crit.dir = d.ramp.dir;
    let present = 0;
    for (let i = 0; i < col.length; i++) if (Number.isFinite(col[i])) present++;
    const sorted = Correlate.sortedValues(col, Correlate.finitePairs(col, col));
    return { ...d, present, p5: Correlate.quantile(sorted, 0.05), p95: Correlate.quantile(sorted, 0.95) };
  });
  const combined = Optimise.combine(parts.map((p) => p.values), o.criteria.map((c) => c.weight), n);
  o.result = { ...combined, parts, names: optNames(), scoring: o.scoring,
               summary: Optimise.summarise(combined.score) };
}

// The column absolute mode reads for a criterion, when it is not the criterion's own.
function optAbsColumn(name) {
  const via = (statField(name) || {}).abs_column;
  return via && statField(via) ? via : null;
}

// Which criteria a result was computed for. The cards redraw the moment one is
// added or removed, before the recompute lands, and a result for a different
// list must not be read against them.
const optNames = () => `${state.opt.scoring}:${state.opt.criteria.map((c) => c.name).join(',')}`;

function optPaint() {
  const o = state.opt;
  const layer = state.byId.get(OPT_ID);
  const fields = o.criteria.map((c) => statField(c.name)).filter(Boolean);
  const names = fields.map((f) => shortLabel(f.label));
  layer.paint = optPaintBlock();
  layer.label = names.length > 2
    ? `Best match: ${names.slice(0, 2).join(' · ')} +${names.length - 2}`
    : `Best match: ${names.join(' · ')}`;
  layer.legend_note = (optAbs()
    ? 'Communes shaded by their match score, a weighted geometric mean of how well each meets every criterion on '
      + 'its natural scale. The colours are fixed from 0 to 1, so a pale map means no commune fits well. '
    : 'Communes ranked by a weighted geometric mean of how well each meets every criterion. ')
    + 'A commune at the unacceptable end of any ramp is ruled out and shown grey, as is one missing any of the statistics.';
  layer.attribution = [...new Set(fields.map((f) => f.attribution).filter(Boolean))].join(' · ');
}

function paintOptimiser(map) {
  const r = state.opt.result;
  // Switching between rank classes and the continuous ramp changes the colour
  // expression itself, not just the values behind it.
  if (map.getLayer(OPT_ID)) map.setPaintProperty(OPT_ID, 'fill-color', colorExpression(state.byId.get(OPT_ID).paint));
  if (!r) paintFeatureState(map, OPT_ID, OPT_KEY, null);
  else if (r.scoring === 'abs') paintFeatureState(map, OPT_ID, OPT_KEY, Float64Array.from(r.score, (s) => (s > 0 ? s : NaN)));
  else paintFeatureState(map, OPT_ID, OPT_KEY, Float64Array.from(r.cls, (c) => (c < 0 ? NaN : c)));
}

async function runOptimiser(map, { show = true } = {}) {
  const o = state.opt;
  clearTimeout(o.timer);
  if (!o.criteria.length) {
    o.run++;
    o.result = null;
    o.busy = false;
    o.error = null;
    paintOptimiser(map);
    if (state.visible.has(OPT_ID)) toggleLayer(map, OPT_ID, false);
    else writeHash(map);
    renderOptimiser(map);
    return;
  }

  const token = ++o.run;
  o.busy = true;
  o.error = null;
  renderOptimiserResult(map);
  try {
    await loadStatIndex();
    const names = o.criteria.map((c) => c.name);
    if (optAbs()) names.push(...names.map(optAbsColumn).filter(Boolean));
    await Promise.all(names.map((name) => loadColumn(name)));
  } catch (err) {
    if (token !== o.run) return;
    o.busy = false;
    o.error = err.message;
    renderOptimiserResult(map);
    return;
  }
  if (token !== o.run) return;

  computeOptimiser();
  optPaint();
  o.busy = false;
  paintOptimiser(map);
  if (show && !state.visible.has(OPT_ID)) toggleLayer(map, OPT_ID, true);
  else applyStack(map);
  renderOptimiserResult(map);
}

// Typing into a ramp box recomputes once the typing stops, not on every key.
function runOptimiserSoon(map) {
  clearTimeout(state.opt.timer);
  state.opt.timer = setTimeout(() => runOptimiser(map), 350);
}

// The cards are rebuilt only when a criterion is added or removed. Everything a
// recompute changes is written into the existing cards, so a ramp box being
// typed into never loses focus underneath the cursor.
function renderOptimiser(map) {
  const o = state.opt;
  fillStatSelect($('#opt-add'), '', (name) => {
    if (!name || o.criteria.some((c) => c.name === name)) return;
    o.criteria.push({ name, dir: 'up', weight: 3, bad: null, ideal: null });
    $('#opt-add').value = '';
    renderOptimiser(map);
    runOptimiser(map);
  }, { blank: '+ Add a statistic…', exclude: new Set(o.criteria.map((c) => c.name)) });

  const scoring = $('#opt-scoring');
  chipRow(scoring, OPT_SCORING, o.scoring, (id) => {
    if (id === o.scoring) return;
    o.scoring = id;
    renderOptimiser(map);
    runOptimiser(map);
  });

  const host = $('#opt-criteria');
  host.innerHTML = '';
  o.criteria.forEach((crit) => host.appendChild(optCard(map, crit)));
  syncPanel();                    // the layer list's circles track this list
  renderOptimiserResult(map);
}

function optCard(map, crit) {
  const o = state.opt;
  const f = statField(crit.name);
  const card = el('div', 'opt-card');
  card.dataset.name = crit.name;

  const head = el('div', 'opt-head');
  const name = el('span', 'opt-name', shortLabel(f.label));
  name.title = f.description || f.label;
  if (f.unit) name.appendChild(el('span', 'opt-unit', ` ${f.unit}`));
  const remove = el('button', 'act-btn off', '×');
  remove.type = 'button';
  remove.title = 'Remove this criterion';
  remove.setAttribute('aria-label', `Remove ${f.label}`);
  remove.addEventListener('click', () => {
    o.criteria = o.criteria.filter((c) => c !== crit);
    renderOptimiser(map);
    runOptimiser(map);
  });
  head.append(name, remove);

  const dirRow = el('div', 'corr-row');
  const dirChips = el('div', 'chips opt-dir');
  dirRow.append(el('span', 'corr-lbl', 'Better'), dirChips);
  const drawDir = () => chipRow(dirChips, OPT_DIRS, crit.dir, (id) => {
    if (id === crit.dir) return;
    crit.dir = id;
    // A two-ended ramp carries its own direction, so flipping the direction
    // flips the ramp rather than being silently overruled by it.
    if (crit.bad !== null && crit.ideal !== null) {
      [crit.bad, crit.ideal] = [crit.ideal, crit.bad];
      bad.value = crit.bad;
      ideal.value = crit.ideal;
    }
    drawDir();
    runOptimiser(map);
  });
  card.drawDir = drawDir;
  drawDir();

  const weightRow = el('div', 'corr-row');
  const weight = el('input', 'opt-weight');
  Object.assign(weight, { type: 'range', min: 1, max: OPT_MAX_WEIGHT, step: 1, value: crit.weight });
  weight.setAttribute('aria-label', `Weight of ${f.label}`);
  const weightVal = el('span', 'act-pct', `×${crit.weight}`);
  weight.addEventListener('input', () => {
    crit.weight = Number(weight.value);
    weightVal.textContent = `×${crit.weight}`;
  });
  weight.addEventListener('change', () => runOptimiser(map));
  weightRow.append(el('span', 'corr-lbl', 'Weight'), weight, weightVal);

  const rampRow = el('div', 'corr-row opt-ramp');
  const box = (placeholder, key, label) => {
    const input = el('input', 'opt-num');
    Object.assign(input, { type: 'number', step: 'any', placeholder });
    input.setAttribute('aria-label', `${label} value of ${f.label}`);
    input.value = crit[key] ?? '';
    input.addEventListener('input', () => {
      crit[key] = optNumber(input.value);
      runOptimiserSoon(map);
    });
    return input;
  };
  const bad = box('Unacceptable', 'bad', 'Unacceptable');
  const ideal = box('Ideal', 'ideal', 'Ideal');
  rampRow.append(el('span', 'corr-lbl', 'Ramp'), bad, el('span', 'opt-arrow', '→'), ideal);

  card.append(head, dirRow, weightRow, rampRow, el('p', 'opt-note'));
  return card;
}

// What each card's ramp resolved to, the summary, the top list and the badge.
function renderOptimiserResult(map) {
  const o = state.opt;
  const hint = $('#opt-hint');
  const summary = $('#opt-summary');
  const top = $('#opt-top');
  const badge = $('#opt-badge');

  const say = (text) => {
    hint.hidden = false;
    hint.textContent = text;
    summary.hidden = true;
    top.hidden = true;
    badge.hidden = true;
  };
  if (!o.criteria.length) {
    return say('Add the statistics that matter. For each, say whether more or less is better, how much it counts, '
               + 'and optionally the values that are unacceptable and ideal. The map then shades the communes that fit best.');
  }
  if (o.error) return say(`Could not load the stat columns: ${o.error}`);
  if (o.busy || !o.result || o.result.names !== optNames()) return say('Working…');

  const r = o.result;
  const unit = (crit) => (statField(crit.name).unit ? ` ${statField(crit.name).unit}` : '');
  o.criteria.forEach((crit, k) => {
    const card = [...$('#opt-criteria').children].find((c) => c.dataset.name === crit.name);
    const part = r.parts[k];
    if (!card || !part) return;
    card.drawDir();
    const scale = part.via ? statField(part.via) : statField(crit.name);
    const scaleUnit = scale.unit ? ` ${scale.unit}` : '';
    const via = part.via ? `Read as ${shortLabel(scale.label)}: ` : '';
    const ends = part.ramp ? `0 at ${fmtNum(part.ramp.bad)} → 1 at ${fmtNum(part.ramp.ideal)}${scaleUnit}` : '';
    const note = {
      ramp: `${ends}.`,
      declared: `${via}${via ? 'its' : 'Its'} natural scale, ${ends}.`,
      stretch: `No natural scale: ${ends}, the 1st to 99th percentile values.`,
      log: `No natural scale, and heavily skewed: ${ends} on a log scale, the 1st to 99th percentile values.`,
    }[part.basis] || (part.ramp ? `${ends}.` : 'No ramp: scored by percentile rank.');
    card.querySelector('.opt-note').textContent =
      `${note} Most communes: ${fmtNum(part.p5)}–${fmtNum(part.p95)}${scaleUnit}.`;
  });

  hint.hidden = true;
  summary.hidden = false;
  const total = state.stats.index.codes.length;
  const lines = [];
  if (r.scoring === 'abs') {
    const [half, strong] = r.summary.above;
    const count = (a) => `${a.count.toLocaleString('en')} ${a.count === 1 ? 'commune' : 'communes'}`;
    if (r.summary.best < half.cut) {
      lines.push(`No commune scores above ${half.cut}: the best match is ${fmtScore(r.summary.best)}.`);
    } else {
      lines.push(`Best match ${fmtScore(r.summary.best)}. ${count(strong)} at ${strong.cut} or more, `
                 + `${count(half)} at ${half.cut} or more.`);
    }
  }
  lines.push(`${r.passing.toLocaleString('en')} communes pass (${fmtPct(r.passing / total)}).`);
  if (r.excluded) lines.push(`${r.excluded.toLocaleString('en')} ruled out by a ramp.`);
  if (r.missing) {
    // Name the thinnest column: it is nearly always the one doing the excluding.
    const thin = o.criteria.map((c, k) => ({ c, present: r.parts[k].present }))
      .sort((a, b) => a.present - b.present)[0];
    lines.push(`${r.missing.toLocaleString('en')} lack data`
      + (thin.present < total * 0.9
        ? ` — ${shortLabel(statField(thin.c.name).label)} is published for only ${fmtPct(thin.present / total)} of communes.`
        : '.'));
  }
  summary.textContent = lines.join(' ');

  badge.hidden = false;
  badge.textContent = r.passing.toLocaleString('en');

  top.innerHTML = '';
  top.hidden = !r.passing;
  const index = state.stats.index;
  for (let k = 0; k < Math.min(OPT_TOP_N, r.order.length); k++) {
    const i = r.order[k];
    const li = el('li');
    const go = el('button', 'opt-top-item');
    go.type = 'button';
    go.title = 'Zoom to this commune';
    go.append(el('span', 'opt-top-rank', `${r.rank[i] + 1}`),
              el('span', 'opt-top-name', `${index.names[i]} (${index.dep[i]})`),
              el('span', 'opt-top-score', r.scoring === 'abs' ? fmtScore(r.score[i]) : fmtPct(r.score[i])));
    go.addEventListener('click', () => {
      map.flyTo({ center: [index.lon[i], index.lat[i]], zoom: 10,
                  padding: { left: $('#panel').classList.contains('hidden') ? 0 : PANEL_W } });
    });
    li.appendChild(go);
    top.appendChild(li);
  }
}

// The inspect panel's reading for one commune: each statistic with the score it
// earned, then the combined score and where that ranks.
function optInspectRows(code) {
  const o = state.opt;
  if (!o.result || code == null) return null;
  const i = state.stats.index.row.get(code);
  if (i === undefined) return null;
  const r = o.result;
  const rows = o.criteria.map((crit, k) => {
    const f = statField(crit.name);
    const v = state.stats.columns.get(crit.name)[i];
    const d = r.parts[k].values[i];
    const via = r.parts[k].via;
    const shown = via && Number.isFinite(v)
      ? `${formatValue(v, f)} (${formatValue(state.stats.columns.get(via)[i], statField(via))} absolute)`
      : formatValue(v, f);
    return { k: shortLabel(f.label), v: Number.isFinite(v) ? `${shown} → ${fmtPct(d)}` : 'no data' };
  });
  const score = r.score[i];
  if (!Number.isFinite(score)) rows.push({ k: 'Match', v: 'Not scored — missing data' });
  else if (score <= 0) rows.push({ k: 'Match', v: 'Ruled out by a ramp' });
  else {
    rows.push({ k: 'Match score', v: r.scoring === 'abs' ? `${fmtScore(score)} (best ${fmtScore(r.summary.best)})` : fmtPct(score) });
    rows.push({ k: 'Rank', v: `${(r.rank[i] + 1).toLocaleString('en')} of ${r.passing.toLocaleString('en')}` });
  }
  return rows;
}

function buildOptimiser(map) {
  const stats = state.manifest.stats;
  const block = $('#opt-block');
  if (!stats || !(stats.fields || []).length || !state.byId.has(OPT_ID)) {
    block.hidden = true;
    return;
  }
  collapsible(block, $('#opt-toggle'), $('#opt-body'), 'opt-open');
  $('#opt-clear').addEventListener('click', () => {
    state.opt.criteria = [];
    renderOptimiser(map);
    runOptimiser(map);
  });
  renderOptimiser(map);
}

// ------------------------------------------------------------------ hash
// #lat/lon/zoom/layer1,layer2/corr/opt/scoring — shareable and bookmarkable, and the only map
// state this app persists (the correlator's folded/unfolded panel is kept in
// localStorage, as a preference of this browser rather than of the link).
function readHash() {
  const raw = location.hash.replace(/^#/, '');
  if (!raw) return null;
  const [lat, lon, zoom, layers, corr, opt, scoring] = raw.split('/');
  const view = (lat && lon && zoom)
    ? { center: [Number(lon), Number(lat)], zoom: Number(zoom) }
    : null;
  const [x, y, mode, radius, side] = (corr || '').split(':');
  return {
    view,
    layers: layers ? layers.split(',').filter(Boolean) : null,
    corr: x && y ? { x, y, mode, radius: Number(radius), side } : null,
    opt: (opt || '').split(',').filter(Boolean).map((part) => {
      const [name, dir, weight, bad, ideal] = part.split(':');
      return { name, dir, weight: Number(weight), bad: optNumber(bad), ideal: optNumber(ideal) };
    }),
    scoring: scoring === 'rank' ? 'rank' : 'abs',  // absolute unless the link says rank
  };
}

// The view a visitor with no link lands on (pipeline/start.json), in the shape
// readHash returns so that one restore path serves both. Opacity is the one
// thing a link does not carry, so the start view's is applied here.
function startView(start) {
  if (!start) return null;
  for (const [id, value] of Object.entries(start.opacity || {})) {
    if (state.byId.has(id)) state.opacity.set(id, value);
  }
  return {
    view: null,
    layers: start.layers || null,
    corr: null,
    opt: (start.optimiser || []).map((c) => ({ bad: null, ideal: null, ...c })),
    scoring: start.scoring === 'rank' ? 'rank' : 'abs',
  };
}

// name:dir:weight:bad:ideal per criterion, comma-separated, blanks left blank.
function optHash() {
  const o = state.opt;
  if (!o.criteria.length) return '';
  return o.criteria.map((c) => [c.name, c.dir, c.weight, c.bad ?? '', c.ideal ?? ''].join(':')).join(',');
}

// x:y:mode:radius:side — enough to rebuild a correlation exactly, and short
// enough that the link still fits in a message.
function corrHash() {
  const c = state.corr;
  if (!c.x || !c.y || !c.result) return '';
  return [c.x, c.y, c.mode, c.radius, c.side].join(':');
}

let hashTimer = null;
function writeHash(map) {
  clearTimeout(hashTimer);
  hashTimer = setTimeout(() => {
    const c = map.getCenter();
    // Bottom of the stack first, so a shared link restores the same stacking.
    const parts = [c.lat.toFixed(4), c.lng.toFixed(4), map.getZoom().toFixed(2), activeIds().join(',')];
    const corr = corrHash();
    const opt = optHash();
    // Positional, so an optimiser with no correlation still leaves corr's slot.
    if (corr || opt) parts.push(corr);
    if (opt) parts.push(opt);
    if (opt && state.opt.scoring === 'rank') parts.push('rank');
    history.replaceState(null, '', `#${parts.join('/')}`);
  }, 200);
}

// ------------------------------------------------------------ basemap UI
function buildBasemaps(map) {
  const select = $('#basemap-select');
  const basemaps = state.manifest.basemaps;
  select.innerHTML = '';
  for (const b of basemaps) {
    const option = el('option', null, b.label);
    option.value = b.id;
    option.title = b.note || b.attribution || '';
    select.appendChild(option);
  }
  const current = () => basemaps.find((b) => b.id === state.basemap) || basemaps[0];
  const describe = () => { select.title = current().note || current().attribution || ''; };
  select.value = current().id;
  describe();
  select.addEventListener('change', () => {
    state.basemap = select.value;
    applyBasemap(map, current());
    describe();
  });
  if (!current().tiles) applyBasemap(map, current());

  // The cadastre and contour-line toggles are off the panel. The code below
  // still builds them if a #overlays row is put back into index.html.
  const overlayHost = $('#overlays');
  if (!overlayHost) return;
  overlayHost.innerHTML = '';
  for (const o of state.manifest.overlays || []) {
    const chip = el('button', 'chip overlay-chip', `+ ${o.label}`);
    chip.setAttribute('aria-pressed', 'false');
    chip.title = `${o.note || o.label} · ${o.attribution}`;
    chip.addEventListener('click', () => {
      const on = state.overlays.has(o.id);
      if (on) {
        if (map.getLayer(`ov-${o.id}`)) map.removeLayer(`ov-${o.id}`);
        if (map.getSource(`ov-${o.id}`)) map.removeSource(`ov-${o.id}`);
        state.overlays.delete(o.id);
      } else {
        map.addSource(`ov-${o.id}`, { type: 'raster', tiles: [o.tiles], tileSize: 256, attribution: o.attribution, minzoom: o.minzoom || 0, maxzoom: o.maxzoom || 19 });
        map.addLayer({ id: `ov-${o.id}`, type: 'raster', source: `ov-${o.id}`, paint: { 'raster-opacity': 0.85 } },
          state.layers.length ? state.layers[0].id : undefined);
        state.overlays.add(o.id);
      }
      chip.setAttribute('aria-pressed', String(!on));
    });
    overlayHost.appendChild(chip);
  }
}

// ------------------------------------------------------------------ boot
async function main() {
  const boot = $('#boot');
  let manifest;
  try {
    const res = await fetch(`layers.json?v=${Date.now()}`);
    if (!res.ok) throw new Error(`layers.json → HTTP ${res.status}`);
    manifest = await res.json();
  } catch (err) {
    boot.className = 'boot error';
    boot.textContent = `Could not load layers.json.\n\n${err.message}\n\nRun: python pipeline/build.py`;
    return;
  }

  state.manifest = manifest;
  state.layers = manifest.layers.slice();
  // The correlator's and optimiser's outputs are layers nothing published. They
  // join the list here, at the position their own z asks for, and from this
  // point on every part of the file treats them exactly as it treats a layer that
  // came out of the pipeline.
  const statFields = (manifest.stats || {}).fields || [];
  state.stats.fields = statFields;
  state.stats.byName = new Map(statFields.map((f) => [f.name, f]));
  const computed = statFields.length ? correlationLayer() : null;
  for (const layer of statFields.length ? [computed, optimiserLayer()] : []) {
    if (!layer) continue;
    const at = state.layers.findIndex((l) => (l.z != null ? l.z : 20) > layer.z);
    state.layers.splice(at < 0 ? state.layers.length : at, 0, layer);
  }
  state.order = state.layers.map((l) => l.id);      // manifest order = initial draw order
  for (const layer of state.layers) {
    state.byId.set(layer.id, layer);
    state.opacity.set(layer.id, layer.default_opacity ?? 0.75);
    if (layer.default_visible) state.visible.add(layer.id);
    for (const f of layer.fields || []) state.fieldIndex.set(f.name, { ...f, layer });
  }
  state.basemap = (manifest.basemaps.find((b) => b.default) || manifest.basemaps[0]).id;
  $('#built').textContent = (manifest.generated || '').replace('T', ' ').replace('Z', ' UTC');

  const hash = readHash() || startView(manifest.start);
  if (hash && hash.corr && computed) {
    const c = state.corr;
    const known = new Set(((manifest.stats || {}).fields || []).map((f) => f.name));
    if (known.has(hash.corr.x) && known.has(hash.corr.y) && hash.corr.x !== hash.corr.y) {
      c.x = hash.corr.x;
      c.y = hash.corr.y;
      if (CORR_MODES.some((m) => m.id === hash.corr.mode)) c.mode = hash.corr.mode;
      if (SIDE_IDS.includes(hash.corr.side)) c.side = hash.corr.side;
      if (hash.corr.radius >= 10 && hash.corr.radius <= 80) c.radius = hash.corr.radius;
    }
  }
  if (hash && hash.opt.length && state.byId.has(OPT_ID)) {
    state.opt.scoring = hash.scoring;
    const seen = new Set();
    state.opt.criteria = hash.opt.filter((c) => {
      if (!state.stats.byName.has(c.name) || seen.has(c.name)) return false;
      seen.add(c.name);
      return true;
    }).map((c) => ({
      name: c.name,
      dir: c.dir === 'down' ? 'down' : 'up',
      weight: Number.isInteger(c.weight) && c.weight >= 1 && c.weight <= OPT_MAX_WEIGHT ? c.weight : 3,
      bad: c.bad,
      ideal: c.ideal,
    }));
  }
  if (hash && hash.layers) {
    const wanted = hash.layers
      .filter((id) => state.byId.has(id))
      .filter((id) => id !== CORR_ID || (state.corr.x && state.corr.y))
      .filter((id) => id !== OPT_ID || state.opt.criteria.length);
    state.visible = new Set(wanted);
    // The hash lists the stack bottom-first; hidden layers keep their manifest
    // order underneath it.
    state.order = state.order.filter((id) => !state.visible.has(id)).concat(wanted);
  }

  const protocol = new pmtiles.Protocol();
  maplibregl.addProtocol('pmtiles', protocol.tile);
  registerSplitArchives(protocol, manifest.layers);

  const map = new maplibregl.Map({
    container: 'map',
    style: rasterStyle(manifest.basemaps),
    center: (hash && hash.view) ? hash.view.center : FRANCE.center,
    zoom: (hash && hash.view) ? hash.view.zoom : FRANCE.zoom,
    maxZoom: 18,
    hash: false,
    attributionControl: { compact: true },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
  map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: 'metric' }), 'bottom-right');

  map.on('load', () => {
    // The panel covers the left third of the window, so frame France in what is
    // actually visible rather than in the whole canvas.
    if (!(hash && hash.view)) {
      map.fitBounds(FRANCE_BOUNDS, {
        padding: { left: PANEL_W + 20, right: 30, top: 30, bottom: 30 },
        animate: false,
      });
    }
    addSources(map);
    for (const layer of state.layers) addLayer(map, layer);
    addProbeLayer(map);
    addSelectionLayers(map);
    addRouteLayers(map);
    restack(map);                       // honour an order restored from the hash
    buildBasemaps(map);
    collapsible($('#active-block'), $('#active-toggle'), $('#active-body'), 'active-open');
    buildPanel(map);
    buildCorrelator(map);
    buildOptimiser(map);
    buildActive(map);
    renderLegend();
    writeHash(map);
    boot.remove();
    // A shared link carrying a correlation recomputes it here rather than
    // shipping 35,000 results in the URL.
    if (state.corr.x && state.corr.y) {
      runCorrelation(map, { show: !(hash && hash.layers) || hash.layers.includes(CORR_ID) });
    }
    if (state.opt.criteria.length) {
      runOptimiser(map, { show: !(hash && hash.layers) || hash.layers.includes(OPT_ID) });
    }
  });

  map.on('moveend', () => writeHash(map));
  map.on('click', (e) => inspect(map, e.point, e.lngLat));
  map.on('mousemove', (e) => {
    const ids = state.layers.filter((l) => state.visible.has(l.id) && l.type !== 'choropleth').flatMap((l) => drawnIds(l.id));
    const over = ids.length && map.queryRenderedFeatures(e.point, { layers: ids.filter((id) => map.getLayer(id)) }).length;
    map.getCanvas().style.cursor = over ? 'pointer' : '';
  });
  map.on('error', (e) => console.warn('[map]', e && e.error ? e.error.message : e));

  $('#inspect-close').addEventListener('click', () => closeInspect(map));
  buildCommuneSearch(map);
  $('#climate-close').addEventListener('click', closeClimateTable);
  $('#climate').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeClimateTable(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeClimateTable(); });
  $('#panel-toggle').addEventListener('click', () => $('#panel').classList.toggle('hidden'));
}

main();
