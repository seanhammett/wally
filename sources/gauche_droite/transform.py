"""Left–right index per commune → data/processed/gauche_droite.csv.

On each election in source.yaml every candidate or list sits at their party's
Chapel Hill Expert Survey position; a commune's index for that election is its
voters' mean position, the published index is the mean over elections, shrunk
toward the department's where few votes make it noisy. Positions, ballots and
blocs are all declared in source.yaml.
"""
from __future__ import annotations

import csv
from collections import defaultdict

import numpy as np
import pandas as pd

from pipeline.common import BuildError, Log, is_metropolitan, normalise_insee


def resolve_positions(ches: pd.DataFrame, meta: dict) -> dict[str, float]:
    """Named position → mean over its parties of each party's mean over the waves."""
    variable, default_waves = meta["ches"]["variable"], meta["ches"]["waves"]
    france = ches[ches["country"] == meta["ches"]["country"]]
    resolved = {}
    for name, spec in meta["positions"].items():
        waves = spec.get("waves", default_waves)
        per_party = []
        for party_id in spec["party_id"]:
            rows = france[(france["party_id"] == party_id) & france["year"].isin(waves)].dropna(subset=[variable])
            if rows.empty:
                raise BuildError(f"position {name}: CHES has no {variable} for party {party_id} in {waves}")
            per_party.append((rows["party"].iloc[-1], rows[variable].mean(),
                              ", ".join(f"{int(y)} {v:.2f}" for y, v in zip(rows["year"], rows[variable]))))
        resolved[name] = float(np.mean([v for _, v, _ in per_party]))
        Log.info(f"{name:<9} {resolved[name]:.2f}  " + " · ".join(f"{p} ({w})" for p, _, w in per_party))
    return resolved


def ballot_votes(ctx, election: dict) -> tuple[pd.DataFrame, pd.Series]:
    """(commune × option votes, votes cast) for one election."""
    if "input" in election:
        table = pd.read_csv(ctx.processed(election["input"]), dtype={"code_insee": str}).set_index("code_insee")
        cast = pd.to_numeric(table["pres22_exprimes_t1"], errors="coerce")
        columns = {opt: f"pres22_t1_{opt}_pct" for opt in election["options"]}
        missing = [c for c in columns.values() if c not in table.columns]
        if missing:
            raise BuildError(f"{election['input']} lacks {missing}")
        votes = pd.DataFrame({opt: table[col] / 100 * cast for opt, col in columns.items()})
        return votes, cast

    path = ctx.raw_dir / election["filename"]
    if not path.exists():
        raise BuildError(f"missing {path.name}; run fetch first")
    lay = election["layout"]
    known = election["options"]
    overrides = election.get("overrides", {})
    for key, option in overrides.items():
        if option not in known:
            raise BuildError(f"override {key} → '{option}' is not an option of {path.name}")
    moved: dict[str, float] = defaultdict(float)
    votes: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    cast: dict[str, float] = defaultdict(float)
    unknown: dict[str, float] = defaultdict(float)
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh, delimiter=";")
        next(reader)
        for row in reader:
            code = normalise_insee(row[lay["commune"]])
            if code is None or not is_metropolitan(code):
                continue
            cast[code] += float(row[lay["expressed"]] or 0)
            for start in range(lay["prefix_columns"], len(row) - lay["block_size"] + 1, lay["block_size"]):
                option, count = row[start + lay["option"]].strip(), row[start + lay["votes"]].strip()
                if not option and not count:
                    continue
                if overrides and "name" in lay:
                    who = f"{row[lay['dept']].strip().zfill(2)}:{row[start + lay['name']].strip().upper()}"
                    if who in overrides:
                        option = overrides[who]
                        moved[who] += float(count or 0)
                if option not in known:
                    unknown[option] += float(count or 0)
                    continue
                votes[code][option] += float(count or 0)
    for who in overrides:
        if not moved[who]:
            raise BuildError(f"override {who} matched no candidate in {path.name}")
        Log.info(f"override {who} → {overrides[who]}: {moved[who]:,.0f} votes")
    if unknown:
        raise BuildError(f"{path.name}: options not declared in source.yaml: "
                         + ", ".join(f"{k or '(blank)'} {v:,.0f} votes" for k, v in unknown.items()))
    frame = pd.DataFrame.from_dict(votes, orient="index").reindex(columns=list(known)).fillna(0.0)
    return frame, pd.Series(cast).reindex(frame.index)


def election_index(votes: pd.DataFrame, cast: pd.Series, election: dict, x: dict[str, float],
                   blocs: list[str]) -> pd.DataFrame:
    """Per commune: mean position, spread, placed votes, bloc and unplaced shares."""
    options = election["options"]
    placed = [o for o, (pos, _) in options.items() if pos is not None]
    xs = np.array([x[options[o][0]] for o in placed])
    placed_votes = votes[placed]
    n = placed_votes.sum(axis=1)
    ok = (cast > 0) & (n > 0)
    w = placed_votes.div(n.where(ok), axis=0)
    mean = (w * xs).sum(axis=1).where(ok)
    spread = np.sqrt((w * (xs - mean.to_numpy()[:, None]) ** 2).sum(axis=1)).where(ok)
    out = pd.DataFrame({"mean": mean, "spread": spread, "n": n.where(ok), "cast": cast})
    total = cast.where(cast > 0)
    for bloc in blocs:
        members = [o for o, (_, b) in options.items() if b == bloc]
        out[bloc] = votes[members].sum(axis=1) / total * 100
    out["unplaced"] = votes[[o for o in options if o not in placed]].sum(axis=1) / total * 100
    national = np.average(mean[ok], weights=n[ok])
    unplaced = 100 - n[ok].sum() / cast[ok].sum() * 100
    Log.info(f"{election['label']}: {int(ok.sum()):,} communes · national index {national:.2f} · "
             f"unplaced {unplaced:.1f}% of votes · blocs " + " · ".join(
                 f"{b} {np.average(out.loc[ok, b], weights=cast[ok]):.1f}%" for b in blocs))
    return out


def transform(ctx) -> None:
    meta = ctx.meta
    ches_path = ctx.raw_dir / meta["ches"]["filename"]
    if not ches_path.exists():
        raise BuildError(f"missing {ches_path.name}; run fetch first")
    x = resolve_positions(pd.read_csv(ches_path, low_memory=False), meta)
    blocs = meta["blocs"]
    for key, election in meta["elections"].items():
        for option, (pos, bloc) in election["options"].items():
            if (pos is None) != (bloc is None) or (pos is not None and (pos not in x or bloc not in blocs)):
                raise BuildError(f"{key}.{option}: position '{pos}' / bloc '{bloc}' is not declared")

    results = {}
    for key, election in meta["elections"].items():
        votes, cast = ballot_votes(ctx, election)
        results[key] = election_index(votes, cast, election, x, blocs)

    codes = sorted(set().union(*(r.index for r in results.values())))
    field = lambda name: pd.DataFrame({k: r[name] for k, r in results.items()}).reindex(codes)
    means, spreads, n = field("mean"), field("spread"), field("n")
    available = means.notna()
    k = available.sum(axis=1)
    raw = means.mean(axis=1)                                     # elections count equally
    noise = (spreads ** 2 / n).where(available).sum(axis=1) / k ** 2
    Log.info("elections per commune: " + " · ".join(f"{int(c)} → {int((k == c).sum()):,}" for c in sorted(k.unique())))
    big = (n >= 200).all(axis=1)
    corr = means[big].corr().round(3)
    Log.info(f"agreement between elections (Pearson, {int(big.sum()):,} communes with ≥ 200 placed votes in each): "
             + " · ".join(f"{a}/{b} {corr.loc[a, b]:.3f}" for i, a in enumerate(corr.columns) for b in corr.columns[i + 1:]))

    index = raw.copy()
    if meta.get("shrink", True):
        ok = raw.notna()
        frame = pd.DataFrame({"raw": raw, "n": n.mean(axis=1), "noise": noise,
                              "dept": [c[:2] for c in codes]}, index=raw.index)[ok]
        by = frame.groupby("dept")
        prior = (frame["raw"] * frame["n"]).groupby(frame["dept"]).sum() / by["n"].sum()
        variance = by["raw"].var()
        tau2 = (variance - by["noise"].mean()).clip(lower=0.1 * variance)   # between-commune variance
        t = tau2.reindex(frame["dept"]).to_numpy()
        w = pd.Series(t / (t + frame["noise"].to_numpy()), index=frame.index)
        w = w.fillna(1.0)                                         # a one-commune department (Paris) keeps its own
        index.loc[frame.index] = w * frame["raw"] + (1 - w) * prior.reindex(frame["dept"]).to_numpy()
        Log.info(f"shrinkage: median weight on the commune's own figure {w.median():.2f} · "
                 f"{((index - raw).abs() > 0.25).sum():,} communes moved by more than 0.25 · "
                 f"between-commune sd within departments, median {np.sqrt(tau2).median():.2f}")

    out = pd.DataFrame(index=pd.Index(codes, name="code_insee"))
    out["gd_indice"] = index.round(2)
    out["gd_indice_brut"] = raw.round(2)
    for key in results:
        out[f"gd_{key}"] = means[key].round(2)
    out["gd_dispersion"] = spreads.mean(axis=1).round(2)
    for bloc in blocs:
        out[f"gd_{bloc}_pct"] = field(bloc).mean(axis=1).round(1)
    out["gd_non_classe_pct"] = field("unplaced").mean(axis=1).round(1)

    Log.info(f"{int(index.notna().sum()):,} communes · index p10 {index.quantile(0.1):.2f} · "
             f"median {index.median():.2f} · p90 {index.quantile(0.9):.2f}")
    cast_mean = field("cast").mean(axis=1)
    towns = pd.DataFrame({"i": raw, "n": cast_mean})[cast_mean >= 20000].sort_values("i")
    Log.info("most left-leaning towns (≥ 20,000 votes): " + ", ".join(f"{c} {v:.2f}" for c, v in towns["i"].head(6).items()))
    Log.info("most right-leaning towns (≥ 20,000 votes): " + ", ".join(f"{c} {v:.2f}" for c, v in towns["i"].tail(6).items()))
    out.reset_index().to_csv(ctx.out_path, index=False)
