#!/usr/bin/env node
// Rank against absolute scoring, on the built site: make scoring-report
//
// Runs every criteria set in scoring_scenarios.json through the optimiser both
// ways, using site/optimise.js itself so the arithmetic is exactly the page's:
//
//   rank   percentile rank when no ramp is typed
//   abs    the default — each statistic on its own scale, following any
//          abs_column
//
// then compares the culture scores with the positions they replaced, and the
// published (absolute) habitability score with the positional one. Reads
// site/layers.json and site/stats/ only, so run it after a build.
// `--json <path>` also writes the numbers out.
'use strict';

const fs = require('fs');
const path = require('path');
const Optimise = require('../site/optimise.js');

const ROOT = path.resolve(__dirname, '..');
const SITE = path.join(ROOT, 'site');
const scenarios = JSON.parse(fs.readFileSync(path.join(__dirname, 'scoring_scenarios.json'), 'utf8'));
const manifest = JSON.parse(fs.readFileSync(path.join(SITE, 'layers.json'), 'utf8'));
const index = JSON.parse(fs.readFileSync(path.join(SITE, manifest.stats.index), 'utf8'));
const fields = new Map(manifest.stats.fields.map((f) => [f.name, f]));
const n = index.count;
const rowOf = new Map(index.codes.map((code, i) => [code, i]));

const columns = new Map();
function column(name) {
  if (!columns.has(name)) {
    const f = fields.get(name);
    if (!f) throw new Error(`${name} is not a published stat column — rebuild first?`);
    const raw = JSON.parse(fs.readFileSync(path.join(SITE, f.file), 'utf8'));
    columns.set(name, Float64Array.from(raw, (v) => (v === null ? NaN : v)));
  }
  return columns.get(name);
}

// ------------------------------------------------------------ small helpers
const finite = (col) => [...col].filter(Number.isFinite).sort((a, b) => a - b);
const q = (sorted, p) => Optimise.quantile(sorted, p);
const f2 = (v) => (Number.isFinite(v) ? v.toFixed(2) : '—');
const f0 = (v) => (Number.isFinite(v) ? String(Math.round(v)) : '—');
const pct = (part, whole) => `${((100 * part) / Math.max(1, whole)).toFixed(1)}%`;
const pad = (s, w) => String(s).padEnd(w);
const lpad = (s, w) => String(s).padStart(w);
const place = (i) => `${index.names[i]} (${index.dep[i]})`;

function table(head, rows) {
  const widths = head.map((h, k) => Math.max(h.length, ...rows.map((r) => String(r[k]).length)));
  const line = (cells) => cells.map((c, k) => (k ? lpad(c, widths[k]) : pad(c, widths[k]))).join('  ');
  return [line(head), widths.map((w) => '─'.repeat(w)).join('  '), ...rows.map(line)].join('\n');
}

// Average ranks, ties shared, over the rows where both columns are finite.
function spearman(a, b) {
  const rows = [];
  for (let i = 0; i < a.length; i++) if (Number.isFinite(a[i]) && Number.isFinite(b[i])) rows.push(i);
  const rank = (col) => {
    const order = [...rows].sort((x, y) => col[x] - col[y]);
    const r = new Map();
    for (let k = 0; k < order.length;) {
      let j = k;
      while (j + 1 < order.length && col[order[j + 1]] === col[order[k]]) j++;
      for (let t = k; t <= j; t++) r.set(order[t], (k + j) / 2);
      k = j + 1;
    }
    return rows.map((i) => r.get(i));
  };
  const ra = rank(a), rb = rank(b);
  const m = (rows.length - 1) / 2;
  let sab = 0, saa = 0, sbb = 0;
  for (let k = 0; k < rows.length; k++) {
    sab += (ra[k] - m) * (rb[k] - m);
    saa += (ra[k] - m) ** 2;
    sbb += (rb[k] - m) ** 2;
  }
  return sab / Math.sqrt(saa * sbb);
}

// ------------------------------------------------------------ the optimiser
const METHODS = [
  { id: 'rank', label: 'rank', absolute: false },
  { id: 'abs', label: 'abs', absolute: true },
];

function run(criteria, method) {
  const basis = [];
  const ds = criteria.map((raw) => {
    const crit = { bad: null, ideal: null, ...raw };
    const field = fields.get(crit.name);
    if (!method.absolute) return Optimise.desirability(column(crit.name), crit).values;
    const name = field.abs_column || crit.name;
    const d = Optimise.absoluteDesirability(column(name), crit, fields.get(name));
    const via = name === crit.name ? '' : `via ${name}, `;
    basis.push(`${crit.name}: ${via}${d.basis} ${f2(d.ramp && d.ramp.bad)} → ${f2(d.ramp && d.ramp.ideal)}`);
    return d.values;
  });
  const result = Optimise.combine(ds, criteria.map((c) => c.weight), n);
  const passing = finite(Float64Array.from(result.order, (i) => result.score[i]));
  return { ...result, summary: Optimise.summarise(result.score), passing: passing.length, sorted: passing, basis };
}

const out = { scenarios: [], culture: [], habitability: null };
const print = (...lines) => console.log(lines.join('\n'));

print('SCORING REPORT — rank against absolute scoring', `${n.toLocaleString('en')} communes, built ${manifest.built || 'site/'}`, '');

for (const sc of scenarios.scenarios) {
  const runs = METHODS.map((m) => ({ m, r: run(sc.criteria, m) }));
  const base = runs[0].r;
  const topSet = (r, k) => new Set(Array.from(r.order.slice(0, k)));
  const baseTop = topSet(base, 100);

  print(`━━ ${sc.label} (${sc.id})`,
        `   ${sc.criteria.map((c) => `${c.name} ${c.dir === 'down' ? '↓' : '↑'}×${c.weight}`).join(' · ')}`,
        `   expect: ${sc.expect}`, '');
  const rows = runs.map(({ m, r }) => {
    const [a5, a8] = r.summary.above.map((a) => a.count);
    const overlap = [...topSet(r, 100)].filter((i) => baseTop.has(i)).length;
    return [m.label, f2(r.summary.best), f2(q(r.sorted, 0.99)), f2(q(r.sorted, 0.9)), f2(q(r.sorted, 0.5)),
            a5.toLocaleString('en'), a8.toLocaleString('en'), r.passing.toLocaleString('en'),
            m.id === 'rank' ? '—' : `${overlap}%`, m.id === 'rank' ? '—' : f2(spearman(base.score, r.score))];
  });
  print(table(['method', 'best', 'p99', 'p90', 'median', '≥0.5', '≥0.8', 'passing', 'top100∩rank', 'ρ vs rank'], rows), '');
  for (const { m, r } of runs) {
    print(`   ${pad(m.label, 8)} ${Array.from(r.order.slice(0, 10), (i) => `${place(i)} ${f2(r.score[i])}`).join(' · ')}`);
  }
  for (const { m, r } of runs.slice(1)) {
    for (const b of r.basis) print(`   ${pad(m.label, 8)} ${b}`);
  }
  const refs = scenarios.reference_communes.filter((c) => rowOf.has(c.code_insee));
  print('', table(['reference', ...runs.map(({ m }) => m.label)], refs.map((c) => {
    const i = rowOf.get(c.code_insee);
    return [c.name, ...runs.map(({ r }) => {
      const s = r.score[i];
      if (!Number.isFinite(s)) return 'n/a';
      return s > 0 ? `${f2(s)} #${(r.rank[i] + 1).toLocaleString('en')}` : 'out';
    })];
  })), '');

  out.scenarios.push({
    id: sc.id,
    methods: runs.map(({ m, r }) => ({
      method: m.id, best: r.summary.best, above: r.summary.above, passing: r.passing,
      p99: q(r.sorted, 0.99), p90: q(r.sorted, 0.9), median: q(r.sorted, 0.5),
      top: Array.from(r.order.slice(0, 10), (i) => ({ code: index.codes[i], name: index.names[i], score: r.score[i] })),
    })),
  });
}

// ------------------------------------------------------------ culture
// The published scores are on a saturating scale; the positions they replaced
// are recomputed here from the same columns, for comparison.
print('━━ Culture: saturating scores against positions among communes', '');
const refs = scenarios.reference_communes.filter((c) => rowOf.has(c.code_insee));
const cultureRows = [];
for (const name of ['cult_scene', 'cult_musee', 'cult_festival', 'culture_score']) {
  if (!fields.has(name)) { print(`   ${name}: not published, skipped`); continue; }
  const score = column(name);
  const position = Optimise.percentile(score, 'up').map((v) => v * 100);
  for (const [kind, c] of [['score', score], ['position', position]]) {
    const s = finite(c);
    cultureRows.push([`${name} ${kind}`, f0(q(s, 0.1)), f0(q(s, 0.5)), f0(q(s, 0.9)),
                      pct(s.filter((v) => v >= 90).length, s.length), pct(s.filter((v) => v < 10).length, s.length),
                      ...refs.map((r) => f0(c[rowOf.get(r.code_insee)]))]);
    out.culture.push({ column: name, kind, p10: q(s, 0.1), median: q(s, 0.5), p90: q(s, 0.9) });
  }
}
print(table(['column', 'p10', 'med', 'p90', '≥90', '<10', ...refs.map((r) => r.name.slice(0, 9))], cultureRows), '');
print('   (a single category ranks communes the same either way; only the spacing differs.',
      '    The combined score is a mean of scores, so its order can differ from a mean of positions.)', '');

// ------------------------------------------------------------ habitability
print('━━ Habitability: positional score against the absolute one', '');
if (fields.has('habitabilite_2050_pos')) {
  const a = column('habitabilite_2050_pos'), b = column('habitabilite_2050');
  const sa = finite(a), sb = finite(b);
  print(table(['score', 'min', 'p5', 'p25', 'median', 'p75', 'p95', 'max'], [
    ['positional', ...[0, 0.05, 0.25, 0.5, 0.75, 0.95, 1].map((p) => f0(q(sa, p)))],
    ['absolute', ...[0, 0.05, 0.25, 0.5, 0.75, 0.95, 1].map((p) => f0(q(sb, p)))],
  ]), '');
  print(`   rank correlation ${f2(spearman(a, b))}`);
  const both = [];
  for (let i = 0; i < n; i++) if (Number.isFinite(a[i]) && Number.isFinite(b[i])) both.push(i);
  const byB = [...both].sort((x, y) => b[x] - b[y]);
  print(`   least habitable, absolute: ${byB.slice(0, 8).map((i) => `${place(i)} ${b[i]} (was ${a[i]})`).join(' · ')}`);
  print(`   most habitable, absolute:     ${byB.slice(-8).reverse().map((i) => `${place(i)} ${b[i]} (was ${a[i]})`).join(' · ')}`);
  // Movers are measured in rank, not points: the two scales have different spreads.
  const rankIn = (col) => {
    const order = [...both].sort((x, y) => col[x] - col[y]);
    const r = new Map();
    order.forEach((i, k) => r.set(i, k / (order.length - 1)));
    return r;
  };
  const ra = rankIn(a), rb = rankIn(b);
  const moves = [...both].sort((x, y) => (rb.get(x) - ra.get(x)) - (rb.get(y) - ra.get(y)));
  const move = (i) => `${place(i)} ${Math.round(100 * ra.get(i))}→${Math.round(100 * rb.get(i))}`;
  print(`   fell furthest (percentile): ${moves.slice(0, 6).map(move).join(' · ')}`);
  print(`   rose furthest (percentile): ${moves.slice(-6).reverse().map(move).join(' · ')}`);
  print('', table(['reference', 'positional', 'absolute'], refs.map((r) => {
    const i = rowOf.get(r.code_insee);
    return [r.name, f0(a[i]), f0(b[i])];
  })), '');
  out.habitability = { spearman: spearman(a, b), positions: sa.length, median: [q(sa, 0.5), q(sb, 0.5)] };
} else {
  print('   habitabilite_2050_pos is not published — rebuild score_habitabilite.', '');
}

const jsonAt = process.argv.indexOf('--json');
if (jsonAt > 0 && process.argv[jsonAt + 1]) {
  fs.writeFileSync(process.argv[jsonAt + 1], JSON.stringify(out, null, 2));
  print(`wrote ${process.argv[jsonAt + 1]}`);
}
