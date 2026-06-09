#!/usr/bin/env python3
"""fip_archiver.py - archive FIP (Radio France) webradio playlists.

FIP exposes a rolling, date-addressable history (browsable back several weeks;
reported ~60 days). This tool:
  * backfill: pull every day in the retained window via the by-date endpoint;
  * watch:    poll forward forever so the archive keeps growing.
Stored in SQLite (+ CSV export), deduped on (webradio, started_ts, title, artist).

IMPORTANT
  * Run where radiofrance.fr is reachable -- NOT inside a restricted Claude Code
    network policy (there, requests return 403 "Host not in allowlist").
  * The exact API path drifts over time; a couple of known shapes are tried and
    the first that works is cached. If both fail, adjust DATE_ENDPOINTS.
  * Backfill only reaches as far back as FIP currently retains (~60 days). It
    cannot recover years that were never published.

Deps:  pip install requests
Usage:
  python3 fip_archiver.py                       # backfill 60 days of fip_jazz
  python3 fip_archiver.py --all --days 60       # all FIP webradios
  python3 fip_archiver.py --watch 60            # then poll every 60s forever
  python3 fip_archiver.py --export out.csv      # dump the DB to CSV
"""
from __future__ import annotations
import argparse, csv, json, sqlite3, sys, time
from datetime import date, datetime, timedelta, timezone

import requests

UA = "fip-archiver/1.0 (personal playlist archive; respectful, rate-limited)"

# Known endpoint shapes (newest first). {wr}=webradio slug, {date}=YYYY-MM-DD.
# Reconstructed from real FIP clients (legzo/fip-recorder, malmstromo/fipscript).
DATE_ENDPOINTS = [
    "https://www.radiofrance.fr/api/v2.0/stations/fip/webradios/{wr}/songs?date={date}",
    "https://www.radiofrance.fr/api/v1.9/stations/fip/webradios/{wr}/songs?date={date}",
]
WEBRADIOS = ["fip", "fip_rock", "fip_jazz", "fip_groove", "fip_world",
             "fip_nouveautes", "fip_reggae", "fip_electro", "fip_metal",
             "fip_pop", "fip_hiphop", "fip_sacre_francais"]

_working = {"i": None}  # cache the endpoint shape that works


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


def fetch(session, url):
    for attempt in range(4):
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (403, 404):
                return None  # wrong shape, blocked, or no data for that day
        except (requests.RequestException, ValueError) as e:
            log(f"  warn: {e}")
        time.sleep(2 ** attempt)  # backoff
    return None


def find_tracks(obj):
    """Defensively locate the list of track dicts in an arbitrary JSON shape."""
    if isinstance(obj, list):
        return obj if obj and isinstance(obj[0], dict) else []
    if isinstance(obj, dict):
        for key in ("songs", "tracks", "steps", "data", "items", "results"):
            v = obj.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        for v in obj.values():  # fall back to first nested list of dicts
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


def parse_track(t):
    title = _first(t, "title", "firstLine", "song", "track")
    artist = _first(t, "interpreters", "performers", "artist", "secondLine", "mainArtists")
    if isinstance(artist, list):
        artist = ", ".join(_as_name(x) for x in artist)
    album = _first(t, "album", "release", "albumTitle")
    start = _first(t, "start", "startTime", "started_at", "playedAt", "start_time")
    ts = None
    if isinstance(start, (int, float)):
        ts = int(start if start < 1e12 else start / 1000)  # secs vs millis
    elif isinstance(start, str):
        try:
            ts = int(datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp())
        except ValueError:
            ts = None
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


def pull(session, cx, wr, day, delay):
    order = ([_working["i"]] if _working["i"] is not None else []) + \
            [i for i in range(len(DATE_ENDPOINTS)) if i != _working["i"]]
    data = None
    for i in order:
        data = fetch(session, DATE_ENDPOINTS[i].format(wr=wr, date=day.isoformat()))
        if data is not None:
            _working["i"] = i
            break
    if data is None:
        log(f"  {wr} {day}: no data (endpoint shape may have changed)")
        return 0
    added = store(cx, wr, find_tracks(data))
    time.sleep(delay)  # politeness / rate limit
    return added


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
    ap.add_argument("--db", default="fip_playlists.sqlite")
    ap.add_argument("--days", type=int, default=60, help="backfill window in days (default 60)")
    ap.add_argument("--watch", type=int, metavar="SEC",
                    help="after backfill, poll today's playlist every SEC seconds")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--export", metavar="CSV", help="export the DB to CSV and exit")
    args = ap.parse_args()

    if args.export:
        export_csv(args.db, args.export)
        return

    radios = WEBRADIOS if args.all else [args.webradio]
    cx = db_init(args.db)
    session = requests.Session()
    session.headers["User-Agent"] = UA

    today = date.today()
    log(f"Backfilling {args.days} day(s) for: {', '.join(radios)}")
    total = 0
    for offset in range(args.days):
        day = today - timedelta(days=offset)
        for wr in radios:
            total += pull(session, cx, wr, day, args.delay)
        log(f"  {day}: cumulative {total} new")
    log(f"Backfill done: {total} new plays in {args.db}")

    if args.watch:
        log(f"Watching every {args.watch}s (Ctrl-C to stop)...")
        try:
            while True:
                added = sum(pull(session, cx, wr, date.today(), args.delay) for wr in radios)
                if added:
                    log(f"  +{added} new")
                time.sleep(args.watch)
        except KeyboardInterrupt:
            log("stopped.")


if __name__ == "__main__":
    main()
