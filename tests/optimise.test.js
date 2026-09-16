// The optimiser's arithmetic, without a browser: make test
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const Optimise = require('../site/optimise.js');

const col = (...values) => Float64Array.from(values, (v) => (v === null ? NaN : v));
const close = (actual, expected, eps = 1e-9) =>
  assert.ok(Math.abs(actual - expected) < eps, `${actual} is not ${expected}`);
const up = (extra = {}) => ({ dir: 'up', weight: 1, bad: null, ideal: null, ...extra });

test('percentile: ties share their mid-rank, blanks stay blank, direction flips', () => {
  const p = Optimise.percentile(col(10, 20, 20, 30, null), 'up');
  close(p[0], 0.5 / 4);
  close(p[1], 2 / 4);
  close(p[2], 2 / 4);
  close(p[3], 3.5 / 4);
  assert.ok(Number.isNaN(p[4]));
  close(Optimise.percentile(col(10, 20, 20, 30), 'down')[0], 1 - 0.5 / 4);
});

test('ramp: clamped at both ends, zero-length ramp is a hard line', () => {
  const r = Optimise.ramp(col(-5, 0, 5, 10, 15, null), { bad: 0, ideal: 10, dir: 'up' });
  assert.deepEqual([...r.slice(0, 5)], [0, 0, 0.5, 1, 1]);
  assert.ok(Number.isNaN(r[5]));
  const line = Optimise.ramp(col(4, 5, 6), { bad: 5, ideal: 5, dir: 'down' });
  assert.deepEqual([...line], [1, 1, 0]);
});

test('combine: a zero excludes, a blank leaves unscored, weights are a geometric mean', () => {
  const a = col(1, 0.25, 0, 1);
  const b = col(0.25, 1, 1, null);
  const r = Optimise.combine([a, b], [1, 3], 4);
  close(r.score[0], Math.exp((Math.log(1) + 3 * Math.log(0.25)) / 4));
  close(r.score[1], Math.exp((Math.log(0.25) + 3 * Math.log(1)) / 4));
  assert.equal(r.score[2], 0);
  assert.ok(Number.isNaN(r.score[3]));
  assert.deepEqual({ passing: r.passing, excluded: r.excluded, missing: r.missing },
                   { passing: 2, excluded: 1, missing: 1 });
  assert.deepEqual([...r.order], [1, 0]);
});

test('absoluteDesirability: declared range, in either direction and either order', () => {
  const c = col(0, 25, 50, 100, 120);
  const upward = Optimise.absoluteDesirability(c, up(), { abs: [0, 100] });
  assert.equal(upward.basis, 'declared');
  assert.deepEqual([...upward.values], [Optimise.ABS_FLOOR, 0.25, 0.5, 1, 1]);
  const downward = Optimise.absoluteDesirability(c, up({ dir: 'down' }), { abs: [100, 0] });
  assert.deepEqual([...downward.values], [1, 0.75, 0.5, Optimise.ABS_FLOOR, Optimise.ABS_FLOOR]);
  assert.deepEqual(downward.ramp, { bad: 100, ideal: 0, dir: 'down' });
});

test('absoluteDesirability: without a declared range, p1–p99 of the column, linear in value', () => {
  // 1..101: p1 = 2, p99 = 100. A skewed tail stays a tail rather than being
  // spread evenly the way a percentile would.
  const values = Array.from({ length: 101 }, (_, i) => i + 1);
  const d = Optimise.absoluteDesirability(col(...values), up(), {});
  assert.equal(d.basis, 'stretch');
  close(d.ramp.bad, 2);
  close(d.ramp.ideal, 100);
  assert.equal(d.values[0], Optimise.ABS_FLOOR);
  close(d.values[50], (51 - 2) / 98);
  assert.equal(d.values[100], 1);
  // An invalid declaration is ignored rather than trusted.
  assert.equal(Optimise.absoluteDesirability(col(...values), up(), { abs: [5, 5] }).basis, 'stretch');
});

test('absoluteDesirability: a heavily skewed column is stretched on a log scale', () => {
  // Most values small, a long upper tail, like density: 1..100 then 100 values up to 10,000.
  const values = [
    ...Array.from({ length: 900 }, (_, i) => 1 + (99 * i) / 899),
    ...Array.from({ length: 100 }, (_, i) => 100 + (9900 * i) / 99),
  ];
  const c = col(...values);
  const d = Optimise.absoluteDesirability(c, up({ dir: 'down' }), {});
  assert.equal(d.basis, 'log');
  const sorted = [...values].sort((a, b) => a - b);
  const [lo, hi] = [Optimise.quantile(sorted, 0.01), Optimise.quantile(sorted, 0.99)];
  assert.deepEqual(d.ramp, { bad: hi, ideal: lo, dir: 'down' });
  // A commune at 50 is far from the best on a log scale; a straight line
  // would call it nearly perfect.
  const at50 = values.findIndex((v) => v >= 50);
  const expected = 1 - (Math.log1p(values[at50]) - Math.log1p(lo)) / (Math.log1p(hi) - Math.log1p(lo));
  close(d.values[at50], expected);
  assert.ok(d.values[at50] < 0.7, `log score ${d.values[at50]}`);
  // A column with negative values is never logged.
  const shifted = col(...values.map((v) => v - 50));
  assert.equal(Optimise.absoluteDesirability(shifted, up(), {}).basis, 'stretch');
});

test('absoluteDesirability: without a typed ramp nothing is ruled out', () => {
  // The bottom of a stretched scale is floored, so combine never excludes on it.
  const values = Array.from({ length: 200 }, (_, i) => i);
  const d = Optimise.absoluteDesirability(col(...values), up(), {});
  assert.ok([...d.values].every((v) => v >= Optimise.ABS_FLOOR));
  const r = Optimise.combine([d.values], [1], values.length);
  assert.equal(r.excluded, 0);
  assert.equal(r.passing, values.length);
  // A typed ramp still can.
  const typed = Optimise.absoluteDesirability(col(...values), up({ bad: 50, ideal: 150 }), {});
  assert.equal(Optimise.combine([typed.values], [1], values.length).excluded, 51);
});

test('absoluteDesirability: a typed ramp wins over the declared range', () => {
  const d = Optimise.absoluteDesirability(col(0, 50, 100), up({ bad: 40, ideal: 60 }), { abs: [0, 100] });
  assert.equal(d.basis, 'ramp');
  assert.deepEqual([...d.values], [0, 0.5, 1]);
});

test('absoluteDesirability: an all-blank column scores nothing', () => {
  const d = Optimise.absoluteDesirability(col(null, null), up(), { abs: [0, 1] });
  assert.equal(d.basis, 'empty');
  assert.ok([...d.values].every(Number.isNaN));
});

test('summarise: best score and counts above the cuts, ignoring excluded and unscored', () => {
  const s = Optimise.summarise(col(0.9, 0.55, 0.3, 0, null));
  close(s.best, 0.9);
  assert.deepEqual(s.above, [{ cut: 0.5, count: 2 }, { cut: 0.8, count: 1 }]);
  assert.equal(Optimise.summarise(col(0, null)).best, 0);
});

test('no good match: percentiles still crown a near-perfect commune, absolute scores do not', () => {
  // Every commune is weak on both counts (at most 30 of 100), independently.
  // Percentiles stretch that to 0–1 anyway, so some commune looks excellent.
  const n = 1000;
  let seed = 7;
  const rand = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const a = Float64Array.from({ length: n }, () => 30 * rand());
  const b = Float64Array.from({ length: n }, () => 30 * rand());
  const field = { abs: [0, 100] };
  const best = (ds) => Optimise.summarise(Optimise.combine(ds, [1, 1], n).score).best;
  const absolute = () => best([a, b].map((c) => Optimise.absoluteDesirability(c, up(), field).values));

  const rank = best([Optimise.percentile(a, 'up'), Optimise.percentile(b, 'up')]);
  assert.ok(rank > 0.9, `rank best ${rank}`);
  assert.ok(absolute() <= 0.3, `absolute best ${absolute()}`);

  // And when one commune really is good on both, absolute says so.
  a[0] = 95;
  b[0] = 95;
  close(absolute(), 0.95);
});
