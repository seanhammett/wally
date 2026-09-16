// The optimiser's arithmetic. No DOM, no map, no fetch — app.js owns all three.
//
// Every chosen statistic becomes a desirability between 0 and 1 for each
// commune, and the desirabilities are multiplied together — a weighted geometric
// mean — into one score. Multiplying rather than adding is the point: a commune
// that is perfect on four counts and unacceptable on the fifth scores zero, where
// an average would call it 80% good. That is how people actually rule places out.
//
// A desirability comes from one of two places:
//   • no ramp set — the commune's percentile rank in the chosen direction, which
//     copes with skewed columns (population, prices) that a straight min–max
//     stretch would crush against zero;
//   • a ramp set — a straight line from the "unacceptable" value (0) to the
//     "ideal" value (1), clamped at both ends, in the units of the statistic.
//
// Percentiles spread every column evenly from 0 to 1, so some commune always
// looks excellent even when nothing fits. The absolute mode, the default,
// replaces them with a straight line in the statistic's own units instead: the
// field's declared natural range when it has one (0–100 for a score), else the
// column's 1st to 99th percentile values — on a log scale when the column is
// heavily skewed (density, prices, population), where a straight line would
// put nearly every commune at the cheap or sparse end. How far apart communes
// are survives, and so does a best match that is not very good.
//
// Columns arrive as Float64Array with NaN for "no value", as for the correlator.

const Optimise = (() => {
  'use strict';

  // The rank classes painted on the map, best first: the top 1% of communes that
  // pass, the next 4%, and so on. The last class is every passing commune below
  // the median. Excluded communes — a zero on any count — get no class at all.
  const CLASS_CUTS = [0.01, 0.05, 0.10, 0.25, 0.50, 1];

  // Where the absolute mode's colour ramp and the panel's summary draw lines.
  const SUMMARY_CUTS = [0.5, 0.8];

  // A column is scored on a log scale when it has no negative values and its
  // upper tail is this many times longer than its lower one:
  // (p99 − p50) / (p50 − p1). Sunshine is about 1, density about 60.
  const SKEW_FOR_LOG = 3;

  // Without a typed ramp, the worst end of a scale is heavily penalised but
  // never zero: only a ramp the user typed can rule a commune out, as with
  // percentiles. Otherwise the least sunny 1% of France would be excluded by a
  // criterion that asked for nothing more than "sunnier is better".
  const ABS_FLOOR = 0.001;
  const floored = (values) => values.map((v) => (Number.isFinite(v) ? Math.max(v, ABS_FLOOR) : v));

  function range(col) {
    let min = Infinity, max = -Infinity, n = 0;
    for (let i = 0; i < col.length; i++) {
      const v = col[i];
      if (!Number.isFinite(v)) continue;
      n++;
      if (v < min) min = v;
      if (v > max) max = v;
    }
    return n ? { n, min, max } : { n: 0, min: NaN, max: NaN };
  }

  // Mid-rank percentile, (rank + 0.5) / n, with ties sharing their average rank.
  // It never reaches 0 or 1, so the worst commune on one count is heavily
  // penalised but not excluded outright — only a ramp can exclude.
  function percentile(col, dir) {
    const out = new Float64Array(col.length).fill(NaN);
    const rows = [];
    for (let i = 0; i < col.length; i++) if (Number.isFinite(col[i])) rows.push(i);
    const n = rows.length;
    if (!n) return out;
    rows.sort((a, b) => col[a] - col[b]);
    for (let k = 0; k < n;) {
      let j = k;
      while (j + 1 < n && col[rows[j + 1]] === col[rows[k]]) j++;
      const p = ((k + j) / 2 + 0.5) / n;
      for (let t = k; t <= j; t++) out[rows[t]] = dir === 'down' ? 1 - p : p;
      k = j + 1;
    }
    return out;
  }

  // What a criterion's ramp actually is once the blanks are filled in. With both
  // ends set, their order is the direction; with one, the other end is the data's
  // own extreme on the good side; with neither, there is no ramp.
  function resolveRamp(crit, r) {
    const has = (v) => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
    const hasBad = has(crit.bad), hasIdeal = has(crit.ideal);
    if (!hasBad && !hasIdeal) return null;
    let dir = crit.dir === 'down' ? 'down' : 'up';
    const bad0 = hasBad ? Number(crit.bad) : null;
    const ideal0 = hasIdeal ? Number(crit.ideal) : null;
    if (hasBad && hasIdeal && bad0 !== ideal0) dir = ideal0 > bad0 ? 'up' : 'down';
    const bad = hasBad ? bad0 : (dir === 'up' ? r.min : r.max);
    const ideal = hasIdeal ? ideal0 : (dir === 'up' ? r.max : r.min);
    return { bad, ideal, dir };
  }

  function ramp(col, { bad, ideal, dir }) {
    const out = new Float64Array(col.length);
    const span = ideal - bad;
    for (let i = 0; i < col.length; i++) {
      const v = col[i];
      if (!Number.isFinite(v)) { out[i] = NaN; continue; }
      if (span === 0) {
        // A ramp with no length is a hard line: at or past it on the good side.
        out[i] = (dir === 'down' ? v <= ideal : v >= ideal) ? 1 : 0;
        continue;
      }
      const d = (v - bad) / span;
      out[i] = d <= 0 ? 0 : d >= 1 ? 1 : d;
    }
    return out;
  }

  // One criterion → one desirability column, plus the ramp that was used (null
  // for a percentile) so the panel can say what the blanks resolved to.
  function desirability(col, crit) {
    const r = range(col);
    const resolved = r.n ? resolveRamp(crit, r) : null;
    return {
      values: resolved ? ramp(col, resolved) : percentile(col, crit.dir),
      ramp: resolved,
      range: r,
    };
  }

  // Linear interpolation between the order statistics of a sorted array.
  function quantile(sorted, q) {
    if (!sorted.length) return NaN;
    const at = (sorted.length - 1) * q;
    const lo = Math.floor(at), hi = Math.ceil(at);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (at - lo);
  }

  const declaredRange = (field) => {
    const abs = field && field.abs;
    if (!Array.isArray(abs) || abs.length !== 2) return null;
    const [a, b] = abs.map(Number);
    return Number.isFinite(a) && Number.isFinite(b) && a !== b ? [Math.min(a, b), Math.max(a, b)] : null;
  };

  // The absolute mode's counterpart to `desirability`. A typed ramp still wins;
  // otherwise the ramp runs across the field's declared range, or failing that
  // across the column's p1–p99, in the chosen direction. `basis` says which.
  function absoluteDesirability(col, crit, field) {
    const r = range(col);
    if (!r.n) return { values: new Float64Array(col.length).fill(NaN), ramp: null, range: r, basis: 'empty' };
    const typed = resolveRamp(crit, r);
    if (typed) return { values: ramp(col, typed), ramp: typed, range: r, basis: 'ramp' };

    const dir = crit.dir === 'down' ? 'down' : 'up';
    let lo, hi, basis;
    const declared = declaredRange(field);
    if (declared) {
      [lo, hi] = declared;
      basis = 'declared';
    } else {
      const sorted = [];
      for (let i = 0; i < col.length; i++) if (Number.isFinite(col[i])) sorted.push(col[i]);
      sorted.sort((a, b) => a - b);
      [lo, hi] = [quantile(sorted, 0.01), quantile(sorted, 0.99)];
      if (lo === hi) [lo, hi] = [r.min, r.max];     // a column that is nearly all one value
      const mid = quantile(sorted, 0.5);
      basis = r.min >= 0 && mid > lo && (hi - mid) / (mid - lo) > SKEW_FOR_LOG ? 'log' : 'stretch';
    }
    const resolved = dir === 'up' ? { bad: lo, ideal: hi, dir } : { bad: hi, ideal: lo, dir };
    if (basis !== 'log') return { values: floored(ramp(col, resolved)), ramp: resolved, range: r, basis };
    // The same ramp, drawn through log(1 + v); the ends are reported in the
    // statistic's own units.
    const logged = col.map(Math.log1p);
    const inLogs = { ...resolved, bad: Math.log1p(resolved.bad), ideal: Math.log1p(resolved.ideal) };
    return { values: floored(ramp(logged, inLogs)), ramp: resolved, range: r, basis };
  }

  // Weighted geometric mean over the criteria, then rank classes over the
  // communes that pass. `ds` are desirability columns, `weights` their weights.
  function combine(ds, weights, n) {
    const score = new Float64Array(n);
    const W = weights.reduce((s, w) => s + w, 0) || 1;
    let missing = 0, excluded = 0;
    for (let i = 0; i < n; i++) {
      let logSum = 0, zero = false, gap = false;
      for (let c = 0; c < ds.length; c++) {
        const d = ds[c][i];
        if (!Number.isFinite(d)) { gap = true; break; }
        if (d <= 0) { zero = true; continue; }
        logSum += weights[c] * Math.log(d);
      }
      if (gap) { score[i] = NaN; missing++; }
      else if (zero) { score[i] = 0; excluded++; }
      else score[i] = Math.exp(logSum / W);
    }

    const order = [];
    for (let i = 0; i < n; i++) if (score[i] > 0) order.push(i);
    order.sort((a, b) => score[b] - score[a]);
    const passing = order.length;

    // Class 0 is the best. Ties take the class of the first commune they tie
    // with, so two communes with the same score are never painted differently.
    const cls = new Int8Array(n).fill(-1);
    const rank = new Int32Array(n).fill(-1);
    let prevScore = NaN, prevClass = 0, prevRank = 0;
    for (let k = 0; k < passing; k++) {
      const i = order[k];
      if (score[i] === prevScore) {
        cls[i] = prevClass;
        rank[i] = prevRank;
        continue;
      }
      const frac = (k + 1) / passing;
      let c = 0;
      while (c < CLASS_CUTS.length - 1 && frac > CLASS_CUTS[c]) c++;
      cls[i] = c;
      rank[i] = k;
      prevScore = score[i];
      prevClass = c;
      prevRank = k;
    }
    return { score, cls, rank, order: Int32Array.from(order), passing, missing, excluded };
  }

  // The headline of an absolute run: the best score, and how many communes
  // clear each of SUMMARY_CUTS. Excluded (0) and unscored (NaN) communes count
  // for nothing.
  function summarise(score) {
    let best = 0;
    const above = SUMMARY_CUTS.map(() => 0);
    for (let i = 0; i < score.length; i++) {
      const s = score[i];
      if (!(s > 0)) continue;
      if (s > best) best = s;
      SUMMARY_CUTS.forEach((cut, k) => { if (s >= cut) above[k]++; });
    }
    return { best, above: SUMMARY_CUTS.map((cut, k) => ({ cut, count: above[k] })) };
  }

  return { CLASS_CUTS, SUMMARY_CUTS, SKEW_FOR_LOG, ABS_FLOOR, range, percentile, resolveRamp, ramp, desirability,
           quantile, absoluteDesirability, combine, summarise };
})();

if (typeof module !== 'undefined') module.exports = Optimise;
