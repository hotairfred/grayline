"""
LoTW user-activity lookup for Grayline.

Downloads ARRL's public LoTW user-activity list — every callsign that uses
Logbook of the World and the date of its most recent upload — and exposes a
fast `days_since_upload(call)` lookup. Powers the roster's LoTW badge: a
station that uploads to LoTW recently is a good bet to work (the QSO will
confirm); one that's never used LoTW (or hasn't in years) is a coin toss.

This is a DIFFERENT dataset from lotw_fetch.py, which pulls Fred's OWN
confirmations. This one is the all-users activity roster (callsign,date).

CSV format (one row per call):   CALLSIGN,YYYY-MM-DD,HH:MM:SS
Source + refresh cadence (>7 days) mirror GridTracker 2's approach
(vendor/gridtracker2/src/renderer/lib/callsigns.js).
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("grayline.lotw_activity")

URL = "https://lotw.arrl.org/lotw-user-activity.csv"
_BASE_DIR = Path(__file__).resolve().parent
CACHE_PATH = _BASE_DIR / "lotw_activity.json"   # {"fetched": epoch, "calls": {CALL: "YYYY-MM-DD"}}
REFRESH_AGE_SEC = 7 * 86400                     # re-download when older than a week (GT2 cadence)
HTTP_TIMEOUT = 45

_CALLS: dict[str, str] = {}     # CALL -> "YYYY-MM-DD" (last upload)
_FETCHED: float = 0.0           # epoch of the cached data
_LOCK = threading.Lock()


def _parse_csv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        # CALLSIGN,YYYY-MM-DD,HH:MM:SS  — we only need call + date
        c1 = line.find(",")
        if c1 <= 0:
            continue
        call = line[:c1].strip().upper()
        date = line[c1 + 1:c1 + 11]   # the 10 chars after the first comma
        if call and len(date) == 10 and date[4] == "-":
            out[call] = date
    return out


def _load_cache() -> bool:
    global _CALLS, _FETCHED
    try:
        d = json.loads(CACHE_PATH.read_text())
        calls = d.get("calls") or {}
        if calls:
            _CALLS = calls
            _FETCHED = float(d.get("fetched") or 0.0)
            return True
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("LoTW activity cache load failed: %s", e)
    return False


def _save_cache() -> None:
    try:
        tmp = CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"fetched": _FETCHED, "calls": _CALLS}))
        tmp.replace(CACHE_PATH)
    except Exception as e:
        log.warning("LoTW activity cache save failed: %s", e)


def _download_and_parse() -> bool:
    global _CALLS, _FETCHED
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": "Grayline/1.0"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            text = r.read().decode("utf-8", errors="replace")
        calls = _parse_csv(text)
        if len(calls) < 1000:   # sanity: the real list is ~230k; a tiny result = bad fetch
            log.warning("LoTW activity download looked too small (%d) — keeping old cache", len(calls))
            return False
        with _LOCK:
            _CALLS = calls
            _FETCHED = time.time()
        _save_cache()
        log.info("LoTW activity refreshed: %d callsigns", len(calls))
        return True
    except Exception as e:
        log.warning("LoTW activity download failed: %s", e)
        return False


def refresh_if_stale(force: bool = False) -> None:
    """Load cache, and re-download if missing or older than REFRESH_AGE_SEC."""
    if not _CALLS:
        _load_cache()
    if force or not _CALLS or (time.time() - _FETCHED) > REFRESH_AGE_SEC:
        _download_and_parse()


def start_background_refresh() -> None:
    """Kick a non-blocking refresh at startup (and the cache load is instant)."""
    _load_cache()

    def _worker():
        try:
            refresh_if_stale()
        except Exception as e:
            log.warning("LoTW activity background refresh error: %s", e)

    threading.Thread(target=_worker, name="lotw-activity-refresh", daemon=True).start()


def last_upload(call: str) -> str | None:
    """Most-recent LoTW upload date 'YYYY-MM-DD' for a call, or None if not a LoTW user."""
    if not call:
        return None
    return _CALLS.get(call.strip().upper())


def days_since_upload(call: str) -> int | None:
    """Whole days since the call's last LoTW upload, or None if not a LoTW user."""
    d = last_upload(call)
    if not d:
        return None
    try:
        dt = datetime.date.fromisoformat(d)
    except ValueError:
        return None
    return (datetime.date.today() - dt).days
