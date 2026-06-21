"""
PSKReporter "who heard me" lookup for Grayline.

Polls PSKReporter for reception reports where MY callsign was the station HEARD
(senderCallsign=MY_CALL, rronly=1) and builds a set of stations that decoded me
in the last ~15 minutes. The roster puts an eye next to any spotted call in that
set — "this station can hear you" — so when you're chasing a rare grid you know
whose path is open both ways before you waste calls on a station that can't hear
you. Exactly the "was I getting out to that guy?" question, answered at a glance.

Only populates while you're transmitting FT8/digital and getting decoded. A station
that doesn't upload to PSKReporter (CW-only, non-reporter) never shows an eye even
if it hears you — so eye = definitely-hears-you, no-eye = unknown (not a no).

PSKReporter rate-limits hard: query no more than once per ~5 min for the same params.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.request

log = logging.getLogger("grayline.psk_heard")

# rronly=1 -> reception reports only (skip the multi-MB active-monitor firehose).
URL = ("https://retrieve.pskreporter.info/query"
       "?senderCallsign={call}&flowStartSeconds=-900&rronly=1")
HTTP_TIMEOUT = 30
POLL_SEC = 300   # PSKReporter asks for >=5 min between queries — respect it.

_RR = re.compile(r'<receptionReport\b([^>]*)/>')
_ATTR = re.compile(r'(\w+)="([^"]*)"')

_HEARD: dict[str, dict] = {}   # RECEIVER_CALL -> {grid, freq, age}
_LOCK = threading.Lock()


def _fetch(call: str) -> str | None:
    try:
        req = urllib.request.Request(
            URL.format(call=urllib.request.quote(call)),
            headers={"User-Agent": "Grayline/1.0 (ham dashboard)"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        log.warning("PSKReporter fetch failed: %s", e)
        return None


def _parse(xml: str) -> dict:
    now = time.time()
    out: dict[str, dict] = {}
    for m in _RR.finditer(xml):
        a = dict(_ATTR.findall(m.group(1)))
        rc = (a.get("receiverCallsign") or "").strip().upper()
        if not rc:
            continue
        fs = a.get("flowStartSeconds", "")
        age = int(now - int(fs)) if fs.isdigit() else None
        prev = out.get(rc)
        # keep the freshest report per receiving station
        if prev and prev.get("age") is not None and age is not None and prev["age"] <= age:
            continue
        out[rc] = {"grid": (a.get("receiverLocator") or "")[:6],
                   "freq": a.get("frequency", ""), "age": age}
    return out


def refresh(call: str) -> None:
    """Pull the latest 'who heard MY_CALL' set. Cheap; call on the POLL_SEC cadence."""
    global _HEARD
    xml = _fetch(call)
    if xml is None:
        return
    parsed = _parse(xml)
    with _LOCK:
        _HEARD = parsed
    log.info("PSKReporter heard-me: %d station(s) decoded %s in the last 15 min",
             len(parsed), call)


def heard(call: str):
    """Reception info if `call` reported hearing me recently, else None."""
    if not call:
        return None
    with _LOCK:
        return _HEARD.get(call.strip().upper())
