// The correlator's arithmetic. No DOM, no map, no fetch — app.js owns all three.
//
// Two numbers come out of a pair of stats. The global one is a single Pearson r
// over every commune that has both values, and it is the number people quote. The
// interesting one is local: r recomputed inside a disc around each commune, which
// is what turns a correlation into something a map can show. A national r of 0.4
// is usually 0.8 across half the country and −0.2 across the other half, and only
// the local map says which half is which.
//
// Columns arrive as Float64Array with NaN for "no value", which is how a commune
// with no published figure stays out of every sum without a branch in the way.

const Correlate = (() => {
  'use strict';

  // Degrees → km. The meridian figure is constant; the parallel one is scaled by
  // the latitude of whichever commune is asking, so a 25 km disc is 25 km in
  // Dunkerque as well as in Perpignan.
  const KM_PER_DEG_LAT = 110.574;
  const KM_PER_DEG_LON = 111.320;
  // cos(51.2°), the northernmost latitude in metropolitan France: the narrowest a
  // degree of longitude ever gets here, and therefore the safe figure for sizing
  // grid cells so that a cell is never smaller than the radius it has to cover.
  const COS_MAX_LAT = 0.6266;

  // ------------------------------------------------------------- basics
  function finitePairs(x, y) {
    const rows = [];
    for (let i = 0; i < x.length; i++) {
      if (Number.isFinite(x[i]) && Number.isFinite(y[i])) rows.push(i);
    }
    return Int32Array.from(rows);
  }

  function describe(v, rows) {
    let n = 0, sum = 0;
    for (let k = 0; k < rows.length; k++) { sum += v[rows[k]]; n++; }
    if (!n) return { n: 0, mean: NaN, sd: NaN, min: NaN, max: NaN };
    const mean = sum / n;
    let ss = 0, min = Infinity, max = -Infinity;
    for (let k = 0; k < rows.length; k++) {
      const d = v[rows[k]] - mean;
      ss += d * d;
      if (v[rows[k]] < min) min = v[rows[k]];
      if (v[rows[k]] > max) max = v[rows[k]];
    }
    return { n, mean, sd: Math.sqrt(ss / n), min, max };
  }

  // Pearson r plus the least-squares line, in one pass over the given rows.
  // Values are shifted by the first observation before they are summed: r and the
  // slope are both shift-invariant, and working near zero keeps the sums of
  // squares from losing precision on columns like population, where the raw
  // squares run past 10^12.
  function pearson(x, y, rows) {
    const n = rows.length;
    if (n < 3) return { r: NaN, n, slope: NaN, intercept: NaN };
    const x0 = x[rows[0]], y0 = y[rows[0]];
    let sx = 0, sy = 0, sxx = 0, syy = 0, sxy = 0;
    for (let k = 0; k < n; k++) {
      const a = x[rows[k]] - x0, b = y[rows[k]] - y0;
      sx += a; sy += b; sxx += a * a; syy += b * b; sxy += a * b;
    }
    const cov = sxy - (sx * sy) / n;
    const vx = sxx - (sx * sx) / n;
    const vy = syy - (sy * sy) / n;
    if (!(vx > 0) || !(vy > 0)) return { r: NaN, n, slope: NaN, intercept: NaN };
    const slope = cov / vx;
    return {
      r: Math.max(-1, Math.min(1, cov / Math.sqrt(vx * vy))),
      n,
      slope,
      // Back out of the shift: the line through (mean x, mean y) with that slope.
      intercept: (y0 + sy / n) - slope * (x0 + sx / n),
    };
  }

  // Spearman is Pearson on ranks, so it survives the skew that population,
  // price and burnt hectares all have, and it is the honest number to read
  // beside Pearson when the scatter is a wedge rather than a cloud.
  function ranks(v, rows) {
    const order = Array.from(rows).sort((a, b) => v[a] - v[b]);
    const out = new Float64Array(v.length).fill(NaN);
    let i = 0;
    while (i < order.length) {
      let j = i;
      while (j + 1 < order.length && v[order[j + 1]] === v[order[i]]) j++;
      const tied = (i + j) / 2 + 1;                 // mean rank across the tie
      for (let k = i; k <= j; k++) out[order[k]] = tied;
      i = j + 1;
    }
    return out;
  }

  function spearman(x, y, rows) {
    if (rows.length < 3) return NaN;
    return pearson(ranks(x, rows), ranks(y, rows), rows).r;
  }

  function quantile(sorted, p) {
    if (!sorted.length) return NaN;
    const pos = (sorted.length - 1) * p;
    const lo = Math.floor(pos), hi = Math.ceil(pos);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
  }

  function sortedValues(v, rows) {
    const out = new Float64Array(rows.length);
    for (let k = 0; k < rows.length; k++) out[k] = v[rows[k]];
    return out.sort();
  }

  // ------------------------------------------------------- spatial index
  // A flat uniform grid in CSR layout: `start` gives each cell's slice of
  // `items`. Cells are one radius across, so every commune within the radius is
  // in the 3×3 block around the query — no tree, no allocation per query, and
  // the whole index rebuilds in a few milliseconds when the radius changes.
  function buildGrid(lon, lat, rows, radiusKm) {
    const cellLat = radiusKm / KM_PER_DEG_LAT;
    const cellLon = radiusKm / (KM_PER_DEG_LON * COS_MAX_LAT);

    let minLon = Infinity, minLat = Infinity, maxLon = -Infinity, maxLat = -Infinity;
    for (let k = 0; k < rows.length; k++) {
      const i = rows[k];
      if (lon[i] < minLon) minLon = lon[i];
      if (lon[i] > maxLon) maxLon = lon[i];
      if (lat[i] < minLat) minLat = lat[i];
      if (lat[i] > maxLat) maxLat = lat[i];
    }
    const cols = Math.max(1, Math.floor((maxLon - minLon) / cellLon) + 1);
    const rowsN = Math.max(1, Math.floor((maxLat - minLat) / cellLat) + 1);
    const cells = cols * rowsN;

    const cellOf = new Int32Array(rows.length);
    const counts = new Int32Array(cells + 1);
    for (let k = 0; k < rows.length; k++) {
      const i = rows[k];
      const gx = Math.min(cols - 1, Math.floor((lon[i] - minLon) / cellLon));
      const gy = Math.min(rowsN - 1, Math.floor((lat[i] - minLat) / cellLat));
      const c = gy * cols + gx;
      cellOf[k] = c;
      counts[c + 1]++;
    }
    for (let c = 0; c < cells; c++) counts[c + 1] += counts[c];
    const start = counts;
    const cursor = Int32Array.from(start.subarray(0, cells));
    const items = new Int32Array(rows.length);
    for (let k = 0; k < rows.length; k++) items[cursor[cellOf[k]]++] = rows[k];

    return { start, items, cols, rows: rowsN, minLon, minLat, cellLon, cellLat, radiusKm };
  }

  // --------------------------------------------------- local correlation
  // r inside a disc around every commune. Where the disc is too empty to mean
  // anything — a sparse column, or the Alps — it widens once, then twice, and
  // gives up rather than reporting r from four communes.
  const MAX_RING = 3;

  function localCorrelation(opts) {
    const { x, y, lon, lat, rows, radiusKm, minN } = opts;
    const grid = buildGrid(lon, lat, rows, radiusKm);
    const values = new Float32Array(x.length).fill(NaN);
    const counts = new Int32Array(x.length);
    const used = new Float32Array(x.length).fill(NaN);   // radius each row settled on

    // Scratch, reused across every query so the loop allocates nothing.
    const cap = Math.max(64, rows.length);
    const nx = new Float64Array(cap);
    const ny = new Float64Array(cap);

    for (let k = 0; k < rows.length; k++) {
      const i = rows[k];
      const kx = KM_PER_DEG_LON * Math.cos(lat[i] * Math.PI / 180);
      const gx = Math.min(grid.cols - 1, Math.floor((lon[i] - grid.minLon) / grid.cellLon));
      const gy = Math.min(grid.rows - 1, Math.floor((lat[i] - grid.minLat) / grid.cellLat));

      let n = 0, radius = radiusKm;
      for (let ring = 1; ring <= MAX_RING; ring++) {
        radius = radiusKm * ring;
        const r2 = radius * radius;
        n = 0;
        const x0 = Math.max(0, gx - ring), x1 = Math.min(grid.cols - 1, gx + ring);
        const y0 = Math.max(0, gy - ring), y1 = Math.min(grid.rows - 1, gy + ring);
        for (let cy = y0; cy <= y1; cy++) {
          const base = cy * grid.cols;
          for (let cx = x0; cx <= x1; cx++) {
            const c = base + cx;
            for (let p = grid.start[c]; p < grid.start[c + 1]; p++) {
              const j = grid.items[p];
              const dx = (lon[j] - lon[i]) * kx;
              const dy = (lat[j] - lat[i]) * KM_PER_DEG_LAT;
              if (dx * dx + dy * dy > r2) continue;
              nx[n] = x[j]; ny[n] = y[j]; n++;
            }
          }
        }
        if (n >= minN) break;
      }
      if (n < minN) continue;

      // Same shifted one-pass Pearson as above, over the disc.
      const ax = nx[0], ay = ny[0];
      let sx = 0, sy = 0, sxx = 0, syy = 0, sxy = 0;
      for (let t = 0; t < n; t++) {
        const a = nx[t] - ax, b = ny[t] - ay;
        sx += a; sy += b; sxx += a * a; syy += b * b; sxy += a * b;
      }
      const vx = sxx - (sx * sx) / n;
      const vy = syy - (sy * sy) / n;
      counts[i] = n;
      used[i] = radius;
      // A disc where one of the two stats is flat has no correlation to report —
      // not a correlation of zero. Leaving it NaN keeps it off the map.
      if (vx > 1e-12 && vy > 1e-12) {
        values[i] = Math.max(-1, Math.min(1, (sxy - (sx * sy) / n) / Math.sqrt(vx * vy)));
      }
    }
    return { values, counts, radii: used };
  }

  // ------------------------------------------- agreement and the z-score gap
  // Both put the two columns on the same scale first. Standardising is what lets
  // "high" and "low" mean anything across a pair measured in euros and in days.
  function zCombine(x, y, rows, combine) {
    const sx = describe(x, rows), sy = describe(y, rows);
    const values = new Float32Array(x.length).fill(NaN);
    if (!(sx.sd > 0) || !(sy.sd > 0)) return { values, statsX: sx, statsY: sy };
    for (let k = 0; k < rows.length; k++) {
      const i = rows[k];
      values[i] = combine((x[i] - sx.mean) / sx.sd, (y[i] - sy.mean) / sy.sd);
    }
    return { values, statsX: sx, statsY: sy };
  }

  // Each commune's own contribution to the national r: the product of the two
  // z-scores. Positive means the commune is high on both or low on both, and the
  // sum of the column over every commune, divided by n, is exactly that r — so
  // this map is a decomposition of the headline number rather than a new
  // statistic. It answers "which places make this correlation" where the local
  // map answers "where does this correlation hold".
  const agreement = (x, y, rows) => zCombine(x, y, rows, (a, b) => a * b);

  // The difference of the two z-scores: how far ahead of the second each
  // commune's first statistic stands. This is the one that finds the corners of
  // the cloud — high on one and low on the other — which the product above
  // cannot, because it gives the same answer for "high on both" as for "low on
  // both", and the same answer for either kind of mismatch.
  const gap = (x, y, rows) => zCombine(x, y, rows, (a, b) => a - b);

  return { finitePairs, describe, pearson, spearman, quantile, sortedValues,
           buildGrid, localCorrelation, agreement, gap };
})();
