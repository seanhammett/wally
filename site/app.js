// France data overlay — the whole frontend.
//
// It does eight things: initialise the map, build the layer panel from
// layers.json, keep the active layers stacked in the order the user chose,
// generate the legend, answer clicks with every loaded value at that point, run
// the correlator, open a commune's climate table, and keep all of that in the
// URL hash. Adding a dataset never touches this file.
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
  fieldIndex: new Map(), // property name -> { label, unit, layer }
  corr: {
    fields: [],          // manifest stats.fields, in panel order
    byName: new Map(),
    index: null,         // { codes, names, dep, lon, lat }
    columns: new Map(),  // field name -> Float64Array, fetched once and kept
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
  // ordinal and categorical are both a match on the raw value
  const match = ['match', ['to-string', value]];
  paint.stops.forEach(([value_, color]) => match.push(String(value_), color));
  match.push(noData);
  return match;
}

function legendEntries(paint) {
  if (paint.scale === 'numeric') {
    const fmt = (n) => (n >= 10000 ? n.toLocaleString('en') : String(n));
    return paint.colors.map((color, i) => {
      const lo = i === 0 ? null : paint.breaks[i - 1];
      const hi = i < paint.breaks.length ? paint.breaks[i] : null;
      const label = lo === null ? `< ${fmt(hi)}` : hi === null ? `≥ ${fmt(lo)}` : `${fmt(lo)} – ${fmt(hi)}`;
      return { color, label };
    });
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
    map.getSource('basemap').setTiles([b.tiles]);
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
    map.addLayer({
      ...common, type: 'line',
      paint: { 'line-color': color, 'line-width': layer.paint.width || 0.7, 'line-opacity': opacity },
    });
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
    for (const id of [layer.id, `${layer.id}__outline`]) {
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
}

// ----------------------------------------------------------- stack order
// The active list is drawn top-first, which is the opposite of the draw order.
function activeIds() {
  return state.order.filter((id) => state.visible.has(id));
}

function restack(map) {
  // Moving each layer to the top in bottom-to-top order leaves the stack in
  // exactly `state.order`. Outlines ride immediately above their own fill.
  for (const id of state.order) {
    if (map.getLayer(id)) map.moveLayer(id);
    if (map.getLayer(`${id}__outline`)) map.moveLayer(`${id}__outline`);
  }
  // The country border and the city names read over every data layer, or a
  // choropleth hides them.
  for (const id of [OUTLINE.line, ...cityLayerIds()]) {
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
// Every active row carries its own scale, so which values a colour stands for
// is readable without scrolling down to the legend.

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
    const tick = el('span', 'tick', shortNumber(b, decimals));
    tick.style.left = `${((i + 1) / paint.colors.length) * 100}%`;
    ticks.appendChild(tick);
  });
  wrap.appendChild(ticks);
  return wrap;
}

// Ordinal and categorical scales name their classes instead. A handful fit as
// labelled swatches; a long list (the municipal nuances run past twenty) is
// shown as the bare colour bar with a pointer to the legend.
function classScale(entries) {
  if (entries.length > 6) {
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

function scaleBlock(layer) {
  const paint = layer.paint || {};
  if (paint.scale === 'numeric' && paint.breaks && paint.colors) return numericScale(paint, unitOf(layer));
  return classScale(legendEntries(paint));
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

    const up = el('button', 'act-btn', '▲');
    up.title = 'Move up (draw above)';
    up.disabled = i === 0;
    up.addEventListener('click', () => moveLayerBy(map, id, -1));

    const down = el('button', 'act-btn', '▼');
    down.title = 'Move down (draw below)';
    down.disabled = i === ids.length - 1;
    down.addEventListener('click', () => moveLayerBy(map, id, +1));

    const off = el('button', 'act-btn off', '×');
    off.title = 'Switch this layer off';
    off.addEventListener('click', () => toggleLayer(map, id, false));

    head.append(name, up, down, off);

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

    card.append(head, scaleBlock(layer), ctl);

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

    row.append(card, grip);
    host.appendChild(row);
  });
}

// ----------------------------------------------------------------- panel
function buildPanel(map) {
  const host = $('#layers');
  host.innerHTML = '';
  const groups = new Map();
  for (const layer of state.layers) {
    if (!groups.has(layer.group)) groups.set(layer.group, []);
    groups.get(layer.group).push(layer);
  }

  for (const [name, layers] of groups) {
    if (layers.every((l) => l.panel === false)) continue;
    const block = el('div', 'group');
    block.appendChild(el('h3', null, name));
    for (const layer of layers) {
      if (layer.panel === false) continue;   // the correlator owns its own switch
      const item = el('div', 'layer');
      item.dataset.id = layer.id;

      const row = el('div', 'row');
      const box = el('input');
      box.type = 'checkbox';
      box.id = `chk-${layer.id}`;
      box.checked = state.visible.has(layer.id);
      box.addEventListener('change', () => toggleLayer(map, layer.id, box.checked));

      const label = el('label', null, layer.label);
      label.htmlFor = box.id;
      row.append(box, label, el('span', 'badge', layer.type === 'choropleth' ? 'commune' : layer.type));

      const note = el('p', 'note');
      note.textContent = layer.legend_note || '';
      item.append(row, note);
      block.appendChild(item);
    }
    host.appendChild(block);
  }
  syncPanel();
}

function syncPanel() {
  for (const item of document.querySelectorAll('.layer')) {
    const on = state.visible.has(item.dataset.id);
    item.classList.toggle('on', on);
    item.querySelector('input[type=checkbox]').checked = on;
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
    block.appendChild(el('div', 'legend-src', layer.attribution));
    host.appendChild(block);
  }
  // Licence attribution for every visible layer, as Licence Ouverte requires.
  $('#attribution').textContent = [...new Set(shown.map((l) => l.attribution))].join(' · ');
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

function inspect(map, point, lngLat) {
  const ids = state.layers.filter((l) => state.visible.has(l.id)).map((l) => l.id);
  if (map.getLayer('__commune_probe')) ids.push('__commune_probe');
  const hits = map.queryRenderedFeatures([[point.x - 3, point.y - 3], [point.x + 3, point.y + 3]],
    { layers: ids.filter((id) => map.getLayer(id)) });

  const body = $('#inspect-body');
  body.innerHTML = '';

  // The commune is the join key for everything, so it leads.
  const communeFeature = hits.find((f) => f.layer.id === '__commune_probe')
    || hits.find((f) => (f.properties || {}).code_insee && (f.properties || {}).nom);
  const props = communeFeature ? communeFeature.properties : {};
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
      const hit = hits.find((f) => f.layer.id === layer.id);
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
    const hit = hits.find((f) => f.layer.id === layer.id);
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
    body.appendChild(head);
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

  for (const { layer, rows } of bySource.values()) {
    const block = el('div', 'insp-group');
    block.appendChild(el('h3', null, layer.source_name || layer.label));
    for (const { k, v } of rows) {
      const row = el('div', 'insp-row');
      row.append(el('span', 'k', k), el('span', 'v', v));
      block.appendChild(row);
    }
    const attr = el('div', 'insp-attr');
    attr.textContent = layer.attribution + (layer.fetched ? ` · fetched ${layer.fetched}` : '');
    block.appendChild(attr);
    body.appendChild(block);
  }

  if (!bySource.size && !props.code_insee) {
    body.appendChild(el('p', 'insp-empty', 'Nothing loaded at this point. Switch on a layer and click again.'));
  }
  $('#inspect').hidden = false;
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
  const fx = corrField(state.corr.x), fy = corrField(state.corr.y);
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

const corrField = (name) => state.corr.byName.get(name) || null;

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
  const c = state.corr;
  if (c.columns.has(name)) return c.columns.get(name);
  const field = corrField(name);
  const res = await fetch(field.file);
  if (!res.ok) throw new Error(`${field.file} → HTTP ${res.status}`);
  // NaN rather than null, so a commune with no published figure drops out of
  // every sum without a branch inside the inner loop.
  const col = Float64Array.from(await res.json(), (v) => (v === null ? NaN : v));
  c.columns.set(name, col);
  return col;
}

async function loadCorrIndex() {
  const c = state.corr;
  if (c.index) return c.index;
  const res = await fetch(state.manifest.stats.index);
  if (!res.ok) throw new Error(`${state.manifest.stats.index} → HTTP ${res.status}`);
  const raw = await res.json();
  c.index = {
    codes: raw.codes, names: raw.names, dep: raw.dep,
    lon: Float64Array.from(raw.lon), lat: Float64Array.from(raw.lat),
    row: new Map(raw.codes.map((code, i) => [code, i])),
  };
  return c.index;
}

// ---------------------------------------------------------------- compute
function computeCorrelation() {
  const c = state.corr;
  const x = c.columns.get(c.x);
  const y = c.columns.get(c.y);
  const rows = Correlate.finitePairs(x, y);
  const result = {
    rows,
    global: Correlate.pearson(x, y, rows),
    spearman: Correlate.spearman(x, y, rows),
  };

  if (c.mode === 'local') {
    Object.assign(result, Correlate.localCorrelation({
      x, y, lon: c.index.lon, lat: c.index.lat, rows,
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
function paintCorrelation(map) {
  const c = state.corr;
  const layer = state.byId.get(CORR_ID);
  if (!layer || !map.getSource(sourceKeyFor(layer))) return;
  const target = { source: sourceKeyFor(layer), sourceLayer: layer.source_layer };
  map.removeFeatureState(target);
  if (!c.result) return;

  const values = c.result.values;
  const codes = c.index.codes;
  for (let i = 0; i < values.length; i++) {
    if (Number.isFinite(values[i])) map.setFeatureState({ ...target, id: codes[i] }, { v: values[i] });
  }
}

// The paint block is the single source of truth for the map, the in-row scale
// and the legend, so the mode, the highlighted side and the two chosen stats all
// land here and nowhere else.
function corrPaint() {
  const c = state.corr;
  const layer = state.byId.get(CORR_ID);
  const mode = corrMode();
  const fx = corrField(c.x), fy = corrField(c.y);
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
    await loadCorrIndex();
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

  const x = c.columns.get(c.x), y = c.columns.get(c.y);
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
  const fi = c.focus != null ? c.index.row.get(c.focus) : undefined;
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
      `${shortLabel(corrField(c.x).label)} →    ↑ ${shortLabel(corrField(c.y).label)}`);
  svg.setAttribute('aria-label',
    `Scatter plot of ${corrField(c.x).label} against ${corrField(c.y).label} over `
    + `${rows.length.toLocaleString('en')} communes. Pearson r is ${fmtR(global.r)}.`);
}

// ------------------------------------------------------------------- UI
function fillStatSelect(sel, value, onChange) {
  sel.innerHTML = '';
  const blank = el('option', null, '— choose a statistic —');
  blank.value = '';
  sel.appendChild(blank);

  const groups = new Map();
  for (const f of state.corr.fields) {
    if (!groups.has(f.group)) groups.set(f.group, []);
    groups.get(f.group).push(f);
  }
  for (const [group, fields] of groups) {
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
       `Communes where both statistics are published, out of ${c.index.codes.length.toLocaleString('en')}.`);
  fact('On the map', c.result.mapped.toLocaleString('en'),
       'Communes the current mode could give a value to.');

  head.append(big, facts, el('p', 'corr-read', `${strength(global.r)}.`));
}

function corrCaveats() {
  const c = state.corr;
  const { rows } = c.result;
  const share = rows.length / c.index.codes.length;
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
  const i = c.index.row.get(code);
  if (i === undefined) return null;

  const rows = [];
  for (const name of [c.x, c.y]) {
    const f = corrField(name);
    const v = c.columns.get(name)[i];
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
    const zx = (c.columns.get(c.x)[i] - c.result.statsX.mean) / c.result.statsX.sd;
    const zy = (c.columns.get(c.y)[i] - c.result.statsY.mean) / c.result.statsY.sd;
    rows.push({ k: 'Standard deviations from the mean', v: `${fmtR(zx)} and ${fmtR(zy)}` });
    rows.push({ k: 'Gap, z(A) − z(B)', v: Number.isFinite(v) ? fmtR(v) : '—' });
  } else {
    rows.push({ k: 'Agreement (z × z)', v: Number.isFinite(v) ? v.toFixed(2) : '—' });
  }
  return rows;
}

// The synthetic layer, built to look exactly like a published one so that the
// rest of this file can go on not knowing it is different.
function correlationLayer() {
  const commune = state.layers.find((l) => l.type === 'choropleth');
  if (!commune) return null;               // no commune tileset, nothing to correlate
  return {
    id: CORR_ID,
    label: 'Correlation',
    group: 'Correlation',
    type: 'choropleth',
    panel: false,                          // the correlator owns its switch
    z: 12,                                 // above the published choropleths, below the rest
    fields: [],
    source_id: CORR_ID,
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
    paint: {
      source: 'feature-state', property: 'v', scale: 'numeric',
      breaks: CORR_BREAKS, colors: CORR_COLORS, no_data_color: BLANK, unit: 'r',
    },
  };
}

function buildCorrelator(map) {
  const stats = state.manifest.stats;
  const block = $('#corr-block');
  if (!stats || !(stats.fields || []).length || !state.byId.has(CORR_ID)) {
    block.hidden = true;                   // built without stat columns; nothing to offer
    return;
  }
  state.corr.fields = stats.fields;
  state.corr.byName = new Map(stats.fields.map((f) => [f.name, f]));

  // Collapsing only folds the panel section; a correlation already on the map
  // stays there, and its r stays readable in the badge on the header.
  const toggle = $('#corr-toggle');
  const setOpen = (open) => {
    toggle.setAttribute('aria-expanded', String(open));
    $('#corr-body').hidden = !open;
    block.classList.toggle('collapsed', !open);
    try { localStorage.setItem('corr-open', open ? '1' : '0'); } catch (err) { /* storage blocked */ }
  };
  let open = true;
  try { open = localStorage.getItem('corr-open') !== '0'; } catch (err) { /* storage blocked */ }
  setOpen(open);
  toggle.addEventListener('click', () => setOpen(toggle.getAttribute('aria-expanded') !== 'true'));

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

// ------------------------------------------------------------------ hash
// #lat/lon/zoom/layer1,layer2 — shareable and bookmarkable, and the only map
// state this app persists (the correlator's folded/unfolded panel is kept in
// localStorage, as a preference of this browser rather than of the link).
function readHash() {
  const raw = location.hash.replace(/^#/, '');
  if (!raw) return null;
  const [lat, lon, zoom, layers, corr] = raw.split('/');
  const view = (lat && lon && zoom)
    ? { center: [Number(lon), Number(lat)], zoom: Number(zoom) }
    : null;
  const [x, y, mode, radius, side] = (corr || '').split(':');
  return {
    view,
    layers: layers ? layers.split(',').filter(Boolean) : null,
    corr: x && y ? { x, y, mode, radius: Number(radius), side } : null,
  };
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
    if (corr) parts.push(corr);
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

  const overlayHost = $('#overlays');
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
  // The correlator's output is a layer nothing published. It joins the list here,
  // at the position its own z asks for, and from this point on every part of the
  // file treats it exactly as it treats a layer that came out of the pipeline.
  const computed = ((manifest.stats || {}).fields || []).length ? correlationLayer() : null;
  if (computed) {
    const at = state.layers.findIndex((l) => (l.z != null ? l.z : 20) > computed.z);
    state.layers.splice(at < 0 ? state.layers.length : at, 0, computed);
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

  const hash = readHash();
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
  if (hash && hash.layers) {
    const wanted = hash.layers
      .filter((id) => state.byId.has(id))
      .filter((id) => id !== CORR_ID || (state.corr.x && state.corr.y));
    state.visible = new Set(wanted);
    // The hash lists the stack bottom-first; hidden layers keep their manifest
    // order underneath it.
    state.order = state.order.filter((id) => !state.visible.has(id)).concat(wanted);
  }

  const protocol = new pmtiles.Protocol();
  maplibregl.addProtocol('pmtiles', protocol.tile);

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
    restack(map);                       // honour an order restored from the hash
    buildBasemaps(map);
    buildPanel(map);
    buildCorrelator(map);
    buildActive(map);
    renderLegend();
    writeHash(map);
    boot.remove();
    // A shared link carrying a correlation recomputes it here rather than
    // shipping 35,000 results in the URL.
    if (state.corr.x && state.corr.y) {
      runCorrelation(map, { show: !(hash && hash.layers) || hash.layers.includes(CORR_ID) });
    }
  });

  map.on('moveend', () => writeHash(map));
  map.on('click', (e) => inspect(map, e.point, e.lngLat));
  map.on('mousemove', (e) => {
    const ids = state.layers.filter((l) => state.visible.has(l.id) && l.type !== 'choropleth').map((l) => l.id);
    const over = ids.length && map.queryRenderedFeatures(e.point, { layers: ids.filter((id) => map.getLayer(id)) }).length;
    map.getCanvas().style.cursor = over ? 'pointer' : '';
  });
  map.on('error', (e) => console.warn('[map]', e && e.error ? e.error.message : e));

  $('#inspect-close').addEventListener('click', () => { $('#inspect').hidden = true; });
  $('#climate-close').addEventListener('click', closeClimateTable);
  $('#climate').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeClimateTable(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeClimateTable(); });
  $('#panel-toggle').addEventListener('click', () => $('#panel').classList.toggle('hidden'));
}

main();
