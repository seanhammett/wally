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
// Columns arrive as Float64Array with NaN for "no value", as for the correlator.

const Optimise = (() => {
  'use strict';

  // The rank classes painted on the map, best first: the top 1% of communes that
  // pass, the next 4%, and so on. The last class is every passing commune below
  // the median. Excluded communes — a zero on any count — get no class at all.
  const CLASS_CUTS = [0.01, 0.05, 0.10, 0.25, 0.50, 1];

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

  return { CLASS_CUTS, range, percentile, resolveRamp, ramp, desirability, combine };
})();

if (typeof module !== 'undefined') module.exports = Optimise;
