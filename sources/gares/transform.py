"""Passenger stations → data/processed/gares.geojson, plus the timetabled network.

Two exports joined on the UIC code: the station reference supplies position and
SNCF's size segment, the footfall file supplies how many people use it. The GTFS
timetable, keyed on the same code, supplies what actually calls there — per
station as trains a day, and as a network file the page draws a clicked
station's routes from.
"""
from __future__ import annotations

import collections
import csv
import datetime
import io
import json
import re
import shutil
import zipfile

from pipeline.common import METRO_BBOX, BuildError, Log, human_bytes, normalise_insee, write_geojson


def load(path):
    if not path.exists():
        raise BuildError(f"missing {path}; run fetch first")
    return json.loads(path.read_text(encoding="utf-8"))


def uic8(value) -> str | None:
    """Both files key on the UIC code, but only one of them zero-pads it."""
    if value is None:
        return None
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    text = "".join(ch for ch in text if ch.isdigit())
    if not text:
        return None
    return text.zfill(8)[-8:]



def best_segment(raw) -> str | None:
    """A handful of stations arrive with their segment repeated or merged.

    The export concatenates multiple reference rows, so a station can come back
    as "A;A" or "B;A". A is the most important of the grades, so the strongest
    one present is the station's real segment.
    """
    if not raw:
        return None
    parts = {p.strip().upper() for p in str(raw).split(";") if p.strip()}
    for grade in ("A", "B", "C"):
        if grade in parts:
            return grade
    return None


# ------------------------------------------------------------- timetable
# A GTFS stop point is "StopPoint:OCE<product>-<code>": the product the train is
# sold as, then the station's UIC code (or an eight-digit coach stop code).
STOP_POINT = re.compile(r"^StopPoint:OCE(.+)-(\d+)$")
NO_PICKUP, NO_DROP_OFF = 1, 2


def gtfs_rows(archive: zipfile.ZipFile, name: str):
    with archive.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))


def gtfs_minutes(text: str) -> int:
    """GTFS times run past 24:00 for a trip that crosses midnight."""
    h, m, _ = text.split(":")
    return int(h) * 60 + int(m)


def typical_week(feed_start: str, pinned) -> list[str]:
    if pinned:
        monday = datetime.date.fromisoformat(str(pinned))
        if monday.weekday() != 0:
            raise BuildError(f"semaine_type {pinned} is not a Monday")
    else:
        start = datetime.datetime.strptime(feed_start, "%Y%m%d").date() + datetime.timedelta(days=7)
        monday = start + datetime.timedelta(days=-start.weekday() % 7)
    return [(monday + datetime.timedelta(days=d)).strftime("%Y%m%d") for d in range(7)]


def read_timetable(path, services: dict, pinned_week) -> dict:
    """Every trip in one ordinary week, folded into its distinct stop pattern.

    38,000 trips collapse to under 5,000 patterns: the same TER calling at the
    same stations at every hour of the day is one pattern run many times. Each
    keeps how many times it runs that week and the timings of its fastest run.
    """
    if not path.exists():
        raise BuildError(f"missing {path}; run fetch first")
    with zipfile.ZipFile(path) as archive:
        feed = next(gtfs_rows(archive, "feed_info.txt"))
        week = typical_week(feed["feed_start_date"], pinned_week)
        days = set(week)
        runs = collections.Counter(r["service_id"] for r in gtfs_rows(archive, "calendar_dates.txt")
                                   if r["date"] in days and r["exception_type"] == "1")
        trip_runs = {r["trip_id"]: runs[r["service_id"]] for r in gtfs_rows(archive, "trips.txt")}
        areas = {r["stop_id"]: r for r in gtfs_rows(archive, "stops.txt") if r["location_type"] == "1"}

        calls = collections.defaultdict(list)
        for r in gtfs_rows(archive, "stop_times.txt"):
            if trip_runs.get(r["trip_id"]):
                calls[r["trip_id"]].append(r)

    unknown = collections.Counter()
    patterns: dict[tuple, dict] = {}
    for trip, rows in calls.items():
        rows.sort(key=lambda r: int(r["stop_sequence"]))
        points = [STOP_POINT.match(r["stop_id"]) for r in rows]
        if len(rows) < 2 or not all(points):
            continue
        product = points[0].group(1)
        if product not in services:
            unknown[product] += trip_runs[trip]
            continue
        stops = tuple(p.group(2) for p in points)
        # Only the calls in between say anything: nobody boards at the terminus
        # or alights at the origin. A TGV that sets down only, on its way into
        # Paris, must not list Paris's suburbs as places to go from there.
        flags = tuple((NO_PICKUP if r["pickup_type"] == "1" else 0) | (NO_DROP_OFF if r["drop_off_type"] == "1" else 0)
                      for r in rows[1:-1])
        t0 = gtfs_minutes(rows[0]["departure_time"])
        times = [gtfs_minutes(r["departure_time"]) - t0 for r in rows[:-1]]
        times.append(gtfs_minutes(rows[-1]["arrival_time"]) - t0)
        key = (product, stops, flags)
        entry = patterns.setdefault(key, {"runs": 0, "times": times})
        entry["runs"] += trip_runs[trip]
        if times[-1] < entry["times"][-1]:
            entry["times"] = times
    if unknown:
        raise BuildError("GTFS product(s) not in `services` in source.yaml: "
                         + ", ".join(f"{k!r} ({n} trips)" for k, n in unknown.most_common()))
    return {"feed": feed, "week": week, "areas": areas, "patterns": patterns}


def station_service(timetable: dict, services: dict) -> dict[str, dict]:
    """Per stop code: train departures a day, stations reached without changing, the services that call."""
    departures = collections.Counter()
    reach: dict[str, set] = collections.defaultdict(set)
    products: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for (product, stops, flags), entry in timetable["patterns"].items():
        rail = services[product]["categorie"] != "car"
        allowed = (0,) + flags + (NO_PICKUP,)   # the origin always boards, the terminus never
        alight = [not f & NO_DROP_OFF for f in (NO_DROP_OFF,) + flags + (0,)]
        for i, (code, flag) in enumerate(zip(stops, allowed)):
            if flag & NO_PICKUP:
                continue
            products[code][services[product]["nom"]] += entry["runs"]
            if rail:
                departures[code] += entry["runs"]
                reach[code].update(s for s, ok in zip(stops[i + 1:], alight[i + 1:]) if ok)
    return {code: {"trains_jour": round(departures[code] / 7, 1),
                   "destinations": len(reach[code] - {code}),
                   "services": ", ".join(n for n, _ in products[code].most_common())}
            for code in products}


# --------------------------------------------------------- track routing
# The timetable says a train goes from A to B, not which way. The rail network
# layer does: each hop is routed along it by the quickest way, so the page can
# draw a clicked station's routes on the track instead of as straight lines.

class Nodes:
    """Union-find over metre-grid points. Two line ends 30 m apart are one junction."""

    def __init__(self):
        self.parent: dict = {}

    def find(self, k):
        self.parent.setdefault(k, k)
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)


def grid_key(pt) -> tuple[int, int]:
    return (round(pt.x), round(pt.y))


def build_track_graph(lines_path, stops: dict[int, tuple[float, float]], cfg: dict):
    """Cut the live network at every junction and every station → pieces and a graph.

    The SNCF export is not noded: two thirds of its line ends stop short of, or
    on no vertex of, the line they join. So each loose end is joined to the
    nearest other line within `jonction_m`, cut there. Crossings are left alone
    on purpose: two lines crossing on a bridge are not a junction.
    """
    import geopandas as gpd
    from shapely import STRtree
    from shapely.geometry import Point
    from shapely.ops import substring

    gdf = gpd.read_file(lines_path)
    gdf = gdf[gdf["exploitee"] != False].to_crs("EPSG:2154").reset_index(drop=True)  # noqa: E712
    lines = list(gdf.geometry)
    tree = STRtree(lines)
    nodes = Nodes()
    cuts: list[dict[float, tuple]] = [{0.0: grid_key(Point(g.coords[0])), g.length: grid_key(Point(g.coords[-1]))}
                                      for g in lines]

    def nearest(pt, limit, skip=None):
        best, best_d = None, limit
        for j in tree.query(pt.buffer(limit)):
            if j == skip:
                continue
            d = lines[j].distance(pt)
            if d <= best_d:
                best, best_d = int(j), d
        return best

    def cut(j, pt):
        s = lines[j].project(pt)
        at = next((t for t in cuts[j] if abs(t - s) < 1.0), None)
        if at is None:
            cuts[j][s] = grid_key(lines[j].interpolate(s))
            at = s
        return cuts[j][at]

    joined = 0
    for i, g in enumerate(lines):
        for end in (Point(g.coords[0]), Point(g.coords[-1])):
            j = nearest(end, cfg["jonction_m"], skip=i)
            if j is not None:
                nodes.union(grid_key(end), cut(j, end))
                joined += 1

    from pyproj import Transformer
    to_l93 = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    # A station is where lines meet, and the export often has them meet only
    # there: Colmar sits 1 m from the Metzeral branch and 45 m from the main line
    # it is on. So a station is joined to every classic line near it, and cut into
    # each. A passing LGV is only its line if nothing else is closer.
    lgv = list(gdf["type_ligne"] == "lgv")
    at_stop: dict[int, tuple] = {}
    for stop, (lon, lat) in stops.items():
        pt = Point(*to_l93.transform(lon, lat))
        near = sorted((d, int(j)) for j in tree.query(pt.buffer(cfg["gare_m"]))
                      if (d := lines[j].distance(pt)) <= cfg["gare_m"])
        if not near:
            continue
        at_stop[stop] = cut(near[0][1], pt)
        for d, j in near[1:]:
            if d <= near[0][0] + cfg["gare_lignes_m"] and not lgv[j]:
                nodes.union(cut(j, pt), at_stop[stop])

    pieces, adjacency = [], collections.defaultdict(list)
    for j, g in enumerate(lines):
        props = gdf.iloc[j]
        lgv = props["type_ligne"] == "lgv"
        kmh = props["v_max"] if props["v_max"] == props["v_max"] and props["v_max"] else cfg["vitesse_defaut"][props["type_ligne"]]
        marks = sorted(cuts[j].items())
        for (s0, k0), (s1, k1) in zip(marks, marks[1:]):
            if s1 - s0 < 0.5:
                nodes.union(k0, k1)
                continue
            a, b = nodes.find(k0), nodes.find(k1)
            pid = len(pieces)
            geom = substring(g, s0, s1)
            pieces.append(geom)
            seconds = geom.length / (kmh / 3.6)
            adjacency[a].append((b, pid, True, seconds, lgv, geom.length))
            adjacency[b].append((a, pid, False, seconds, lgv, geom.length))
    at_stop = {stop: nodes.find(k) for stop, k in at_stop.items()}
    Log.info(f"track graph: {len(pieces):,} pieces between {len(adjacency):,} nodes · {joined:,} loose line ends joined · "
             f"{len(at_stop):,}/{len(stops):,} stops on the network")
    return pieces, adjacency, at_stop


def quickest(adjacency, source, targets: set, lgv_penalty: float) -> dict:
    """Dijkstra from one node until every target is settled → target → (signed pieces, metres)."""
    import heapq

    best = {source: 0.0}
    back: dict = {}
    todo, found = set(targets), {}
    heap = [(0.0, source)]
    while heap and todo:
        cost, node = heapq.heappop(heap)
        if cost > best.get(node, float("inf")):
            continue
        if node in todo:
            todo.discard(node)
            path, metres, at = [], 0.0, node
            while at != source:
                prev, pid, forward, length = back[at]
                path.append(pid if forward else ~pid)
                metres += length
                at = prev
            found[node] = (path[::-1], metres)
        for nxt, pid, forward, seconds, lgv, length in adjacency[node]:
            c = cost + seconds * (lgv_penalty if lgv else 1)
            if c < best.get(nxt, float("inf")):
                best[nxt] = c
                back[nxt] = (node, pid, forward, length)
                heapq.heappush(heap, (c, nxt))
    return found


def route_hops(timetable: dict, services: dict, stop_xy: dict[int, tuple], at: dict, lines_path, cfg: dict) -> dict:
    """Every rail hop between consecutive stops, along the track. Coaches stay straight: they use roads.

    High-speed trains take the quickest way, LGV included. Everything else pays a
    penalty on an LGV, so a TER keeps to the classic line beside it unless the
    LGV is the only sensible way — as for the TER that do run on one. A route
    much longer than the crow flies means the network is not joined up there
    (or the stop is abroad), and that hop is drawn straight instead.
    """
    import math

    wanted: dict[str, dict[int, set]] = {"tgv": collections.defaultdict(set), "rail": collections.defaultdict(set)}
    for (product, stops, _), _entry in timetable["patterns"].items():
        cat = services[product]["categorie"]
        if cat == "car":
            continue
        group = "tgv" if cat == "tgv" else "rail"
        for a, b in zip(stops, stops[1:]):
            a, b = at[a], at[b]
            if a != b and a in stop_xy and b in stop_xy:
                wanted[group][min(a, b)].add(max(a, b))

    from pyproj import Transformer

    pieces, adjacency, node_of = build_track_graph(lines_path, stop_xy, cfg)
    to_l93 = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    xy = {s: to_l93.transform(*ll) for s, ll in stop_xy.items()}

    hops, straight = [], collections.Counter()
    for group, penalty in (("tgv", 1.0), ("rail", cfg["penalite_lgv"])):
        for a, bs in wanted[group].items():
            if a not in node_of:
                straight["stop off the network"] += len(bs)
                continue
            targets = {node_of[b] for b in bs if b in node_of}
            found = quickest(adjacency, node_of[a], targets, penalty)
            for b in bs:
                route = found.get(node_of.get(b))
                if b not in node_of:
                    straight["stop off the network"] += 1
                elif route is None:
                    straight["no connected track"] += 1
                elif not route[0]:
                    straight["both stops on one point"] += 1
                else:
                    crow = math.dist(xy[a], xy[b])
                    if route[1] > max(cfg["detour_max"] * crow, crow + 20000):
                        straight["route far longer than the crow flies"] += 1
                    else:
                        hops.append([group, a, b, route[0]])
    total = len(hops) + sum(straight.values())
    Log.info(f"{len(hops):,} of {total:,} rail hops routed along the track ({len(hops) / total:.1%}); drawn straight:")
    for why, n in straight.most_common():
        Log.info(f"  {why:<38} {n:>5,}")

    # Only the track some train uses, thinned to what a screen can show.
    used = sorted({p if p >= 0 else ~p for *_, path in hops for p in path})
    renumber = {p: i for i, p in enumerate(used)}
    hops = [[g, a, b, [renumber[p] if p >= 0 else ~renumber[~p] for p in path]] for g, a, b, path in hops]
    import geopandas as gpd
    geoms = gpd.GeoSeries([pieces[p].simplify(cfg["simplification_m"]) for p in used], crs="EPSG:2154").to_crs("EPSG:4326")
    encoded = []
    for g in geoms:
        # Delta-encoded 1e-5° integers: a third the size of plain coordinates.
        flat, px, py = [], 0, 0
        for x, y in g.coords:
            ix, iy = round(x * 1e5), round(y * 1e5)
            flat += [ix - px, iy - py]
            px, py = ix, iy
        encoded.append(flat)
    # Where each routed stop meets the track, so the page can join the station's
    # dot to the line — GTFS positions are the building, not the platform.
    to_wgs = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True)
    routed = {a for _, a, _, _ in hops} | {b for _, _, b, _ in hops}
    snap = {str(s): [round(v, 5) for v in to_wgs.transform(*node_of[s])] for s in sorted(routed)}
    return {"pieces": encoded, "hops": hops, "snap": snap}


def write_network(folder, timetable: dict, services: dict, names: dict[str, str], attribution: str,
                  lines_path, track_cfg: dict) -> None:
    """index.json (what the page needs to label it) and network.json (stops, patterns).

    One file for the whole country: 5,000 patterns are ~400 KB, ~90 KB over the
    wire, and a click on any station needs patterns from anywhere in France.
    """
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    patterns = timetable["patterns"]
    codes = sorted({code for _, stops, _ in patterns for code in stops})
    at = {code: i for i, code in enumerate(codes)}
    products = sorted({product for product, _, _ in patterns})
    product_at = {p: i for i, p in enumerate(products)}

    stops, missing = [], 0
    for code in codes:
        area = timetable["areas"].get(f"StopArea:OCE{code}")
        if area is None:
            missing += 1
            stops.append([code, code, None, None])
            continue
        stops.append([code, names.get(code) or area["stop_name"],
                      round(float(area["stop_lon"]), 5), round(float(area["stop_lat"]), 5)])
    if missing:
        Log.warn(f"{missing} GTFS stop(s) have no stop area, so no name or position")

    rows = []
    for (product, seq, flags), entry in sorted(patterns.items(), key=lambda kv: -kv[1]["runs"]):
        row = [product_at[product], [at[c] for c in seq], entry["times"], entry["runs"]]
        if any(flags):
            row.append([0, *flags, 0])
        rows.append(row)
    network = {"stops": stops, "patterns": rows}
    stop_xy = {i: (s[2], s[3]) for i, s in enumerate(stops) if s[2] is not None}
    tracks = route_hops(timetable, services, stop_xy, at, lines_path, track_cfg)
    (folder / "tracks.json").write_text(json.dumps(tracks, separators=(",", ":")), encoding="utf-8")
    (folder / "network.json").write_text(json.dumps(network, ensure_ascii=False, separators=(",", ":")),
                                         encoding="utf-8")

    week = timetable["week"]
    fmt = lambda d: datetime.datetime.strptime(d, "%Y%m%d").date().isoformat()
    index = {
        "network": "network.json",
        "tracks": "tracks.json",
        "week": [fmt(week[0]), fmt(week[-1])],
        "feed_version": timetable["feed"].get("feed_version"),
        "feed_end": fmt(timetable["feed"]["feed_end_date"]),
        "products": [{"id": p, **services[p]} for p in products],
        "flags": {"no_pickup": NO_PICKUP, "no_drop_off": NO_DROP_OFF},
        "notes": [
            "Direct trains only — every place reachable without changing, from SNCF's national timetable "
            f"for the week of {fmt(week[0])}. Times are the fastest run that week; a day is the week's total ÷ 7.",
            "Routes follow the quickest way along the track between consecutive stops — the timetable says where a "
            "train stops, not which line it takes. Coaches, and hops the track network cannot join up (mostly "
            "abroad), are drawn as straight lines.",
            "Île-de-France suburban trains (Transilien, RER) are timetabled by IDFM and are not in this feed.",
        ],
        "attribution": attribution,
    }
    (folder / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    size = sum(p.stat().st_size for p in folder.iterdir())
    Log.ok(f"network: {len(rows):,} stop patterns over {len(stops):,} stops, {human_bytes(size)}")


def transform(ctx) -> None:
    ex = ctx.meta["exports"]
    year = int(ctx.meta["annee"])
    prev = int(ctx.meta["annee_precedente"])

    gares = load(ctx.raw_dir / ex["gares"]["filename"])
    freq = load(ctx.raw_dir / ex["frequentation"]["filename"])
    Log.info(f"{len(gares):,} stations · {len(freq):,} footfall records")

    col_now, col_prev = f"total_voyageurs_{year}", f"total_voyageurs_{prev}"
    sample = freq[0] if freq else {}
    for col in (col_now, col_prev):
        if col not in sample:
            raise BuildError(
                f"the footfall export has no column '{col}'. Available year columns: "
                + ", ".join(sorted(k for k in sample if k.startswith("total_voyageurs_")))
                + " — update `annee` / `annee_precedente` in source.yaml."
            )

    services = ctx.meta["services"]
    timetable = read_timetable(ctx.raw_dir / ctx.meta["gtfs"]["filename"], services, ctx.meta.get("semaine_type"))
    served = station_service(timetable, services)
    Log.info(f"timetable {timetable['feed'].get('feed_version')}: {len(timetable['patterns']):,} stop patterns, "
             f"{sum(p['runs'] for p in timetable['patterns'].values()):,} trips in the week of {timetable['week'][0]}")

    by_uic = {}
    for row in freq:
        code = uic8(row.get("code_uic_complet"))
        if code:
            by_uic[code] = row

    minx, miny, maxx, maxy = METRO_BBOX
    features, no_pos, outside, matched = [], 0, 0, 0

    for g in gares:
        pos = g.get("position_geographique") or {}
        lon, lat = pos.get("lon"), pos.get("lat")
        if lon is None or lat is None:
            no_pos += 1
            continue
        if not (minx <= lon <= maxx and miny <= lat <= maxy):
            outside += 1
            continue

        code = uic8(g.get("codes_uic"))
        f = by_uic.get(code)
        if f:
            matched += 1

        def count(col):
            if not f:
                return None
            v = f.get(col)
            try:
                return int(float(v)) if v is not None else None
            except (TypeError, ValueError):
                return None

        now, before = count(col_now), count(col_prev)
        trend = None
        if now is not None and before:
            trend = round(100 * (now - before) / before, 1)

        features.append(
            {
                "type": "Feature",
                "properties": {
                    "nom": g.get("nom"),
                    "code_insee": normalise_insee(g.get("codeinsee")),
                    "code_uic": code,
                    "segment": best_segment(g.get("segment_drg")),
                    "voyageurs": now,
                    "voyageurs_2019": before,
                    "tendance_pct": trend,
                    # Absent from the timetable is unknown, not zero: most such
                    # stations are Transilien/RER, which IDFM timetables.
                    **(served.get(code) or {"trains_jour": None, "destinations": None, "services": None}),
                },
                "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
            }
        )

    if no_pos:
        Log.info(f"{no_pos:,} station(s) had no coordinates")
    if outside:
        Log.info(f"{outside:,} station(s) outside metropolitan France dropped")

    with_count = [f for f in features if f["properties"]["voyageurs"] is not None]
    Log.info(f"{len(features):,} stations · {matched:,} matched to footfall ({matched / len(features):.1%})")

    if with_count:
        counts = sorted((f["properties"]["voyageurs"] for f in with_count), reverse=True)
        total = sum(counts)
        top50 = sum(counts[:50])
        Log.info(f"  {total:,.0f} passengers in {year} across {len(with_count):,} stations")
        Log.info(f"  the 50 busiest carry {top50 / total:.1%} of all journeys")
        Log.info(f"  median station {counts[len(counts) // 2]:,} · quietest {counts[-1]:,}")
        busiest = max(with_count, key=lambda f: f["properties"]["voyageurs"])
        Log.info(f"  busiest: {busiest['properties']['nom']} ({busiest['properties']['voyageurs']:,})")

    seg = collections.Counter(f["properties"]["segment"] for f in features)
    for s, n in sorted(seg.items(), key=lambda kv: str(kv[0])):
        Log.info(f"  segment {str(s):<5} {n:>6,}")

    timed = [f for f in features if f["properties"]["trains_jour"] is not None]
    Log.info(f"{len(timed):,} stations in the timetable ({len(timed) / len(features):.1%}); "
             f"busiest by trains: " + ", ".join(
                 f"{f['properties']['nom']} ({f['properties']['trains_jour']:.0f}/day)"
                 for f in sorted(timed, key=lambda f: -f["properties"]["trains_jour"])[:3]))
    names = {f["properties"]["code_uic"]: f["properties"]["nom"] for f in features}
    write_network(ctx.files_dir, timetable, services, names, ctx.meta["attribution"],
                  ctx.processed("voies_ferrees"), ctx.meta["voies"])

    features.sort(key=lambda f: -(f["properties"]["voyageurs"] or 0))
    write_geojson(features, ctx.out_path)
    Log.info(f"output {human_bytes(ctx.out_path.stat().st_size)}")
