#!/usr/bin/env python3
"""fip_archiver.py - archive FIP (Radio France) webradio playlists.

FIP exposes a rolling, date-addressable history (browsable back several weeks;
reported ~60 days). This tool:
  * backfill: pull every day in the retained window via the by-date endpoint;
  * watch:    poll forward forever so the archive keeps growing.
Stored in SQLite (+ CSV export), deduped on (webradio, started_ts, title, artist).

Two known data sources (no API key needed):
  * date-REST (Module 1): .../api/v2.0/stations/fip/webradios/<wr>/songs?date=YYYY-MM-DD
    -> deep, day-by-day. Best for the 60-day BACKFILL.
  * livemeta (Module 3):  https://api.radiofrance.fr/livemeta/pull/<stationId>
    -> live + a few recent "steps". Robust/key-less. Best for forward WATCH.
  (A fip.fr GraphQL endpoint also exists but uses a rotating persisted-query hash
   and returns only previousTrackLimit items, so it is intentionally not used.)

IMPORTANT
  * Run where radiofrance.fr is reachable -- NOT inside a restricted Claude Code
    network policy (there, requests return 403 "Host not in allowlist").
  * Exact API paths drift; use --probe to dump raw JSON and confirm shapes.
  * Backfill only reaches as far back as FIP currently retains (~60 days); it
    cannot recover years that were never published.

Deps:  pip install requests
Usage:
  python3 fip_archiver.py                         # backfill 60 days of fip_jazz (date-REST)
  python3 fip_archiver.py --all --days 60         # all FIP webradios
  python3 fip_archiver.py --source livemeta --watch 60   # live archiving via livemeta
  python3 fip_archiver.py --probe                 # dump raw API shapes (needs network)
  python3 fip_archiver.py --export out.csv        # dump the DB to CSV
"""
from __future__ import annotations
import argparse, csv, json, sqlite3, sys, time
from datetime import date, datetime, timedelta, timezone

import requests

UA = "fip-archiver/1.0 (personal playlist archive; respectful, rate-limited)"

# date-REST (Module 1). {wr}=webradio slug, {date}=YYYY-MM-DD. Newest shape first.
# Reconstructed from real FIP clients (legzo/fip-recorder, malmstromo/fipscript).
DATE_ENDPOINTS = [
    "https://www.radiofrance.fr/api/v2.0/stations/fip/webradios/{wr}/songs?date={date}",
    "https://www.radiofrance.fr/api/v1.9/stations/fip/webradios/{wr}/songs?date={date}",
]
# livemeta (Module 3) - live + recent, key-less. {id}=numeric station id.
LIVEMETA_URL = "https://api.radiofrance.fr/livemeta/pull/{id}"

WEBRADIOS = ["fip", "fip_rock", "fip_jazz", "fip_groove", "fip_world",
             "fip_nouveautes", "fip_reggae", "fip_electro", "fip_metal"]

# Numeric station ids for livemeta/GraphQL, extracted from community code
# (jcoin/fipradio-playlist). reggae id inferred; confirm with --probe.
STATION_IDS = {
    "fip": 7, "fip_rock": 64, "fip_jazz": 65, "fip_groove": 66,
    "fip_world": 69, "fip_nouveautes": 70, "fip_reggae": 71,
    "fip_electro": 74, "fip_metal": 77,
}

_working = {"i": None}  # cache the date-endpoint shape that works


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def db_init(path):
    cx = sqlite3.connect(path)
    cx.execute("""CREATE TABLE IF NOT EXISTS plays(
        webradio   TEXT,
        started_ts INTEGER,
        started_at TEXT,
        title      TEXT,
        artist     TEXT,
        album      TEXT,
        raw        TEXT,
        PRIMARY KEY (webradio, started_ts, title, artist))""")
    cx.commit()
    return cx


def get_json(session, url):
    for attempt in range(4):
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (403, 404):
                return None  # wrong shape, blocked, or no data
        except (requests.RequestException, ValueError) as e:
            log(f"  warn: {e}")
        time.sleep(2 ** attempt)  # backoff
    return None


def find_tracks(obj):
    """Locate the list of track dicts across the known JSON shapes."""
    if isinstance(obj, list):
        return obj if obj and isinstance(obj[0], dict) else []
    if isinstance(obj, dict):
        if isinstance(obj.get("steps"), dict):            # livemeta: steps is a dict
            vals = [v for v in obj["steps"].values() if isinstance(v, dict)]
            if vals:
                return vals
        if isinstance(obj.get("edges"), list):            # GraphQL Relay cursor
            nodes = [e.get("node", e) for e in obj["edges"] if isinstance(e, dict)]
            if nodes:
                return nodes
        for key in ("songs", "tracks", "data", "items", "results"):
            v = obj.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        for v in obj.values():                            # recurse (e.g. data.previousTracks)
            t = find_tracks(v)
            if t:
                return t
    return []


def _first(d, *keys):
    for k in keys:
        if d.get(k) not in (None, "", [], {}):
            return d[k]
    return None


def _as_name(x):
    return x.get("name", x.get("title", "")) if isinstance(x, dict) else str(x)


def _to_ts(v):
    if isinstance(v, (int, float)):
        return int(v if v < 1e12 else v / 1000)          # seconds vs millis
    if isinstance(v, str):
        try:
            return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def parse_track(t):
    # GraphQL TimelineItem uses title=ARTIST, subtitle=SONG (no interpreters/authors).
    if t.get("subtitle") and not any(t.get(k) for k in ("interpreters", "authors", "performers")):
        title, artist = t.get("subtitle"), t.get("title")
    else:
        title = _first(t, "title", "firstLine", "song", "track", "titre")
        artist = _first(t, "interpreters", "performers", "authors", "artist",
                        "secondLine", "mainArtists", "auteur")
    if isinstance(artist, list):
        artist = ", ".join(_as_name(x) for x in artist)
    album = _first(t, "album", "release", "albumTitle", "titreAlbum")
    ts = _to_ts(_first(t, "start", "startTime", "start_time", "started_at",
                       "playedAt", "start_time_ts", "debut"))
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None
    return title, artist, album, ts, iso


def store(cx, wr, tracks):
    added = 0
    for t in tracks:
        if not isinstance(t, dict):
            continue
        title, artist, album, ts, iso = parse_track(t)
        if not title or ts is None:
            continue
        cur = cx.execute(
            "INSERT OR IGNORE INTO plays VALUES (?,?,?,?,?,?,?)",
            (wr, ts, iso, str(title), str(artist or ""), str(album or ""),
             json.dumps(t, ensure_ascii=False)[:4000]))
        if cur.rowcount == 1:
            added += 1
    cx.commit()
    return added


def fetch(session, wr, day, source):
    """Return raw JSON for a webradio, via the chosen source."""
    if source == "livemeta":
        sid = STATION_IDS.get(wr)
        if sid is None:
            log(f"  {wr}: no station id for livemeta")
            return None
        return get_json(session, LIVEMETA_URL.format(id=sid))
    # date-REST: try known shapes, cache the one that works
    order = ([_working["i"]] if _working["i"] is not None else []) + \
            [i for i in range(len(DATE_ENDPOINTS)) if i != _working["i"]]
    for i in order:
        data = get_json(session, DATE_ENDPOINTS[i].format(wr=wr, date=day.isoformat()))
        if data is not None:
            _working["i"] = i
            return data
    return None


def pull(session, cx, wr, day, source, delay):
    data = fetch(session, wr, day, source)
    if data is None:
        log(f"  {wr} {day if source != 'livemeta' else 'live'}: no data "
            f"(endpoint/shape may have changed - try --probe)")
        return 0
    added = store(cx, wr, find_tracks(data))
    time.sleep(delay)
    return added


def probe(session, wr):
    """Dump raw JSON heads so the exact shapes can be confirmed."""
    today = date.today().isoformat()
    targets = [("livemeta", LIVEMETA_URL.format(id=STATION_IDS.get(wr, "?")))] + \
              [("date-REST", e.format(wr=wr, date=today)) for e in DATE_ENDPOINTS]
    for label, url in targets:
        log(f"\n### {label}: {url}")
        data = get_json(session, url)
        if data is None:
            log("  (no data / blocked / wrong shape)")
            continue
        tracks = find_tracks(data)
        log(f"  top-level type: {type(data).__name__}; tracks found: {len(tracks)}")
        if tracks:
            log("  sample parsed: " + repr(parse_track(tracks[0])))
        log("  raw head: " + json.dumps(data, ensure_ascii=False)[:600])


def export_csv(db, out):
    cx = sqlite3.connect(db)
    rows = cx.execute("SELECT webradio,started_at,title,artist,album "
                      "FROM plays ORDER BY webradio,started_ts").fetchall()
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["webradio", "started_at", "title", "artist", "album"])
        w.writerows(rows)
    log(f"exported {len(rows)} rows -> {out}")


def main():
    ap = argparse.ArgumentParser(description="Archive FIP webradio playlists.")
    ap.add_argument("--webradio", default="fip_jazz", help="slug, e.g. fip_jazz (default)")
    ap.add_argument("--all", action="store_true", help="archive all FIP webradios")
    ap.add_argument("--source", choices=["date", "livemeta"], default="date",
                    help="date-REST backfill (default) or livemeta live snapshots")
    ap.add_argument("--db", default="fip_playlists.sqlite")
    ap.add_argument("--days", type=int, default=60, help="backfill window in days (default 60)")
    ap.add_argument("--watch", type=int, metavar="SEC",
                    help="after backfill, poll every SEC seconds")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--export", metavar="CSV", help="export the DB to CSV and exit")
    ap.add_argument("--probe", action="store_true", help="dump raw API shapes and exit")
    args = ap.parse_args()

    if args.export:
        export_csv(args.db, args.export)
        return

    session = requests.Session()
    session.headers["User-Agent"] = UA

    if args.probe:
        probe(session, args.webradio)
        return

    radios = WEBRADIOS if args.all else [args.webradio]
    cx = db_init(args.db)
    today = date.today()

    if args.source == "date":
        log(f"Backfilling {args.days} day(s) via date-REST for: {', '.join(radios)}")
        total = 0
        for offset in range(args.days):
            day = today - timedelta(days=offset)
            for wr in radios:
                total += pull(session, cx, wr, day, "date", args.delay)
            log(f"  {day}: cumulative {total} new")
        log(f"Backfill done: {total} new plays in {args.db}")
    else:
        log("livemeta source: no by-date history; capturing current snapshot then watching.")
        for wr in radios:
            pull(session, cx, wr, today, "livemeta", args.delay)

    if args.watch:
        log(f"Watching every {args.watch}s via {args.source} (Ctrl-C to stop)...")
        try:
            while True:
                added = sum(pull(session, cx, wr, date.today(), args.source, args.delay)
                            for wr in radios)
                if added:
                    log(f"  +{added} new")
                time.sleep(args.watch)
        except KeyboardInterrupt:
            log("stopped.")


if __name__ == "__main__":
    main()
