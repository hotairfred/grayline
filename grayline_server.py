#!/usr/bin/env python3
"""
Grayline Server v0 — read-only web view of GoCluster spots.

Phase 2 starter from project_grayline_dashboard.md. Stdlib only.
Runs on .101, serves HTML to any browser on the LAN. No UDP at the
workstation, no Flex panadapter inject, no audio-critical interference.
"""

import asyncio
import socket
import gzip
import json
import logging
import math
import os
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

import dxcluster
import flexradio
import logbook_uploads
import lotw_fetch
import lotw_activity
import dxmarathon
import psk_heard
import peer_copies
import telnet_server
from ctydat import CtyDat
import worked_state
from worked_state import WorkedState, mode_class, resolve_prefecture, _US_STATES_50

log = logging.getLogger("grayline")

# ---------------- config ----------------
# Operator-specific settings live in config.json (gitignored — copy
# config.json.example and edit). Any key not present falls back to the
# genericized default below, so a fresh clone still starts; hardware- and
# credential-dependent features (Flex, telnet feed, uploads, LoTW fetch) default
# OFF until enabled in config.json. API credentials live separately in
# secrets.json (see secrets.json.example).
_BASE_DIR = Path(__file__).parent
# Config path is overridable via $GRAYLINE_CONFIG (handy for testing / multiple
# instances); defaults to config.json next to this script.
_CONFIG_PATH = Path(os.environ.get("GRAYLINE_CONFIG") or (_BASE_DIR / "config.json"))
def _load_config() -> dict:
    try:
        cfg = json.loads(_CONFIG_PATH.read_text())
        log.info("loaded config.json (%d keys)", len(cfg))
        return cfg
    except FileNotFoundError:
        log.warning("no config.json found — using built-in defaults. Set callsign / "
                    "home_grid / hosts in config.json (see config.json.example).")
        return {}
    except Exception as e:
        log.warning("config.json unreadable (%s) — using built-in defaults", e)
        return {}
CONFIG = _load_config()

GOCLUSTER_HOST = CONFIG.get("gocluster_host", "ve7cc.net")   # a public DX cluster by default
GOCLUSTER_PORT = CONFIG.get("gocluster_port", 23)
CALLSIGN = CONFIG.get("callsign", "N0CALL")
HOME_GRID = CONFIG.get("home_grid", "")  # 6-char Maidenhead (e.g. "FN31pr"); needed for distance filtering
PEER_RADIUS_MI = CONFIG.get("peer_radius_mi", 60)  # "who near me also copied this?" — local-peer radius for peer_copies
# Commands sent after login. Empty by default — standard DX clusters (ve7cc.net
# etc.) stream spots immediately with no setup. GoCluster / DXSpider filter
# dialects (PASS ..., SET GRID ...) go here via config.json for those servers.
LOGIN_COMMANDS = CONFIG.get("login_commands", [])
# When True, drop any spot whose spotter we can't place (no grid in the QRZ
# cache) — the strict "only verifiable spotters" policy. Requires QRZ creds +
# a warm cache to show anything, so it defaults False: a fresh install with a
# default public cluster shows spots immediately; distance is just blank until
# the spotter resolves. Set True (with QRZ creds) to filter unverifiable feeds.
REQUIRE_SPOTTER_GRID = CONFIG.get("require_spotter_grid", False)

# Per-band award scopes. HF chases DXCC×band (ARRL Challenge / 5BDXCC). 6m chases
# BOTH DXCC×band AND grids (FFMA + 6m DXCC are both meaningful awards). 2m and up
# chase grids only (VUCC is grid-based; DXCC on VHF/UHF is real but extremely
# specialized, and most operators just want to know "is this a new grid").
DXCC_BANDS = {"160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"}
GRID_BANDS = {"6m", "2m", "1.25m", "70cm", "33cm", "23cm", "13cm", "9cm", "6cm", "3cm"}
HTTP_PORT = CONFIG.get("http_port", 8080)
# Spot lifetimes, by mode class. CW/Phone/other spots stay useful for a while —
# the station tends to sit on frequency calling CQ. Digital (FT8/FT4) is "while
# the iron is hot": a decode minutes old usually means the station finished its
# QSO or moved, so clicking it just tunes you to a dead frequency. Hence a much
# shorter default for digital. Both configurable.
SPOT_TTL = CONFIG.get("spot_ttl_sec", 600)                  # CW / Phone / other (seconds)
SPOT_TTL_DIGITAL = CONFIG.get("spot_ttl_digital_sec", 180)  # FT8 / FT4 — short; stale decodes = dead click-to-tune
# How long a higher-priority (e.g. local WSJT-X) cache entry stays authoritative
# without its OWN source updating it. Past this, its freq/audio-offset/tune data
# are assumed frozen (station moved or faded locally), so a lower-priority spot
# is allowed to take it over rather than leave a zombie local entry whose freq
# and click-to-tune target are stale. ~4 FT8 cycles.
LOCAL_SPOT_FRESH_SEC = 60
MAX_SPOTS = 5000      # hard cap
PURGE_INTERVAL = 30   # seconds
REGION = 2            # ARRL band plan

DEFAULT_RADIUS_MI = 300
QRZ_CACHE_PATH = _BASE_DIR / "qrz_cache.json"
CTY_DAT_PATH = _BASE_DIR / "cty.dat"
LOGBOOK_PATH = _BASE_DIR / "qrz_logbook.json"
FLEX_HOST = CONFIG.get("flex_host", "")
FLEX_PORT = CONFIG.get("flex_port", 4992)
FLEX_ENABLED = CONFIG.get("flex_enabled", False)

# Phase 2 panadapter injection tuning
FLEX_INJECT_ENABLED = CONFIG.get("flex_inject_enabled", False)
FLEX_INJECT_RATE_SEC = 0.5         # max 2 spots/sec to the radio (gentle on the API)
FLEX_INJECT_LIFETIME_SEC = 600     # how long Flex keeps each spot before auto-expiring it
FLEX_INJECT_DEDUP_SEC = 300        # don't re-inject the same (band, freq, call) within this window
FLEX_INJECT_SKIP_MODES = {"FT8", "FT4"}  # WSJT-X handles digital modes natively via DAX

# ---------------- Local skimmer suppression ----------------
# A home CW skimmer typically spots under your callsign (e.g. CALL and CALL-1).
# During contests it busts a high volume of calls and can flood the feed with
# junk. Flip EXCLUDE_LOCAL_SKIMMER True to drop those at ingest — kills them
# everywhere downstream (web UI, Flex inject, telnet feed). WSJT-X local decodes
# (source WSJTX-LOCAL) are NOT skimmer spots and are always kept. Override the
# matched spotter callsigns with local_skimmer_spotters in config.json; the
# default is [CALL, CALL-1].
EXCLUDE_LOCAL_SKIMMER = CONFIG.get("exclude_local_skimmer", False)   # set True during contests to drop the home skimmer's busted spots
LOCAL_SKIMMER_SPOTTERS = set(CONFIG.get("local_skimmer_spotters", [CALLSIGN, f"{CALLSIGN}-1"]))

# ---------------- SDC / DX-cluster telnet feed ----------------
# Re-broadcast GrayLine's filtered, annotated spots as a standard DX Spider
# telnet node so SDC-Connectors (or any DX cluster client) can consume them.
# Feed policy: LOCAL-spotter spots only — the same tiered radius as the web
# UI's "Local spotters only" toggle (HF <=300 mi, VHF+ <=150 mi of HOME_GRID).
# The per-band radius overrides in the browser are localStorage-only; the feed
# applies the fixed tiered default server-side.
TELNET_FEED_ENABLED = CONFIG.get("telnet_feed_enabled", False)
TELNET_FEED_PORT = CONFIG.get("telnet_feed_port", 7374)              # NOT 7301 (dxfilter) / 7373 (SDC's own server)
TELNET_FEED_NODE = CONFIG.get("telnet_feed_node", f"{CALLSIGN}-2")   # DX Spider node call advertised to clients
TELNET_FEED_RADIUS_HF_MI = 300
TELNET_FEED_RADIUS_VHF_MI = 150
# 6m and up are "local-signal" bands — nearer spotters are the meaningful ones.
# Mirrors the browser's VHF_PLUS_BANDS set so the feed and the UI agree.
TELNET_FEED_VHF_PLUS_BANDS = frozenset(
    {"6m", "2m", "1.25m", "70cm", "33cm", "23cm", "13cm", "9cm", "6cm", "3cm", "1.25cm"})

# WSJT-X UDP integration
# Listen for WSJT-X broadcasts (heartbeat, status, decode) so Grayline
# (a) ingests our own real-time decodes as local-source spots, and
# (b) knows the current dial frequency for click-to-tune audio-offset math.
WSJTX_ENABLED = CONFIG.get("wsjtx_enabled", True)
WSJTX_LISTEN_HOST = "0.0.0.0"
WSJTX_LISTEN_PORT = CONFIG.get("wsjtx_listen_port", 2237)             # WSJT-X default UDP server port
# Mirror every received WSJT-X UDP datagram (verbatim) to these hosts, so other
# consumers (e.g. GridTracker on another machine) see the same live stream and
# you can compare worked/needed status side-by-side. List of [host, port].
WSJTX_FORWARD_TARGETS = [tuple(t) for t in CONFIG.get("wsjtx_forward_targets", [])]
_wsjtx_fwd_sock = None                # lazily created in _forward_wsjtx
WSJTX_AUDIO_MIN_HZ = 200             # WSJT-X passband minimum (below this, decoder doesn't see signal)
WSJTX_AUDIO_MAX_HZ = 3000            # WSJT-X passband maximum
WSJTX_STATE_TTL_SEC = 60             # forget WSJT-X state if no heartbeat/status for this long

# N1MM / SDC-Connectors QSO logging
# Listen for N1MM-compatible <contactinfo> UDP broadcasts (from N1MM Logger+ or
# SDC-Connectors) and mark the station worked in real time — award pills flip
# 'new'->'worked' on the next /spots.json poll, reusing the same ingest pipeline
# as WSJT-X logging (ADIF append + worked-state + logbook upload). Useful when
# running a contest in N1MM/SDC instead of WSJT-X.
N1MM_ENABLED = CONFIG.get("n1mm_enabled", True)
N1MM_LISTEN_HOST = "0.0.0.0"
N1MM_LISTEN_PORT = CONFIG.get("n1mm_listen_port", 12060)             # N1MM "Contact" broadcast port (matches GTBridge)

# Master switch for real-time logbook uploads (QRZ/ClubLog/eQSL) fired on every
# QSO logged via WSJT-X or N1MM. When True, every logged QSO appears on QRZ
# almost immediately — which during a contest is a non-radio confirmation
# channel (a station could verify a QSO via your QRZ page instead of off the
# air), something most contest rules prohibit. Keep it FALSE while contesting
# and batch-upload the clean local ADIF afterward; flip True for everyday ops.
LOGBOOK_UPLOAD_ENABLED = CONFIG.get("logbook_upload_enabled", False)   # uploads each logged QSO to QRZ + LoTW. Keep False while contesting (avoids live off-air QSO confirmation).

# Modes operated through WSJT-X (or a JTDX/MSHV equivalent that speaks the same
# UDP protocol). When clicking a spot in any of these modes, click-to-tune
# routes EXCLUSIVELY to WSJT-X — never to the Flex slice — and only fires if
# a WSJT-X instance is currently tuned to that spot's band. Otherwise no
# action is taken (don't surprise the operator with an unexpected Flex retune
# during a digital-mode QSO).
WSJTX_MODES = {"FT8", "FT4", "JT65", "JT9", "MSK144", "Q65",
               "FST4", "FST4W", "WSPR", "JT4"}

# Local ADIF file written on every WSJT-X QSO Logged broadcast. This is the
# canonical real-time log fed by Grayline directly from WSJT-X UDP — separate
# from QRZ logbook (which lags by minutes-to-hours via cron-pulled fetch).
# Future steps: parallel uploads to QRZ / ClubLog / eQSL / LoTW from this file.
QSO_LOG_PATH = _BASE_DIR / "qso_logged.adi"

# LoTW incremental download. Pulls confirmations from ARRL LoTW directly
# rather than waiting for them to surface via QRZ logbook (which lags by
# operator manual processing). Closes the Aruba/EU-Russia confirmation gap
# observed when QRZ hadn't yet reflected LoTW state. See lotw_fetch.py for
# the cursor + auth model (lifted from GT2 adif.js).
LOTW_FETCH_ENABLED = CONFIG.get("lotw_fetch_enabled", False)
LOTW_FETCH_INTERVAL_SEC = 3600   # 1 hr — LoTW pull is incremental; hourly is plenty

# Roster LoTW-user badge: a station that uploaded to LoTW within this many days
# is a "fresh" / good-bet target (the QSO will confirm). Stale users get a dimmed
# badge; non-users get none. Default 365 (Fred's "within the past year" rule of
# thumb). Mirror this into the frontend so the slider can tune it later.
LOTW_FRESH_DAYS = int(CONFIG.get("lotw_fresh_days", 365))

# Source priority for spot dedup. Higher numbers win when the same
# (band, mode, freq, call) tuple arrives from multiple sources within
# the spot TTL. Local sources (our own WSJT-X, our skimmer) always
# beat external cluster aggregators because we trust our own RX path
# more than third-party propagation.
SOURCE_PRIORITY = {
    "WSJTX-LOCAL": 100,    # our running WSJT-X — highest fidelity
    "SPARKGAP-LOCAL": 90,  # a local CW skimmer at the home station
    "GOCLUSTER": 50,       # local validated aggregator (default external feed)
    # everything else (RBN, DXSummit, PSKR ingest via GoCluster) defaults to 10
}
SOURCE_PRIORITY_DEFAULT = 10

# Loaded at startup
_cty: CtyDat | None = None
_worked: WorkedState | None = None
_flex: flexradio.FlexRadioClient | None = None
_flex_inject_queue: asyncio.Queue | None = None
_flex_recent_injects: dict[tuple, float] = {}
# Main asyncio event loop reference, captured at start of main(). Threads
# (HTTP handler) use this with asyncio.run_coroutine_threadsafe to call into
# async-only APIs like FlexRadioClient.tune().
_main_loop: asyncio.AbstractEventLoop | None = None
# DX-cluster telnet feed server (SDC-Connectors et al.), started in main().
_telnet_feed: "telnet_server.TelnetServer | None" = None

# Highest-freq band first, descending. Matches SmartSDR / HRD convention.
# Includes microwave bands so they have tab slots if spots ever arrive (rare via
# cluster but possible during contests / activations).
BAND_ORDER = ["3cm", "6cm", "9cm", "13cm", "23cm", "33cm", "70cm", "1.25m",
              "2m", "6m", "10m", "12m", "15m", "17m",
              "20m", "30m", "40m", "60m", "80m", "160m"]

# In-memory copy of GTBridge's QRZ cache. Loaded at startup; reloaded
# periodically to pick up new entries gtbridge has added live.
_qrz_cache: dict[str, str] = {}
_qrz_cache_lock = threading.Lock()


def load_qrz_cache():
    global _qrz_cache
    try:
        data = json.loads(QRZ_CACHE_PATH.read_text())
        with _qrz_cache_lock:
            _qrz_cache = data
        log.info("QRZ cache loaded: %d entries", len(data))
    except Exception as e:
        log.warning("QRZ cache load failed: %s", e)


def qrz_cache_reload_loop():
    while True:
        time.sleep(600)  # reload every 10 minutes
        load_qrz_cache()


def normalize_spotter(spotter: str) -> str:
    """Strip suffix (e.g. -#, -@) and operator-status notation (/4, /M, /P, /QRP)
    to get the base licensed callsign for QRZ lookup."""
    if not spotter:
        return ""
    s = spotter.upper()
    # Strip on first / first — handles FG1G/4/30, K3LR/M, W1AW/4, etc.
    if "/" in s:
        s = s.split("/")[0]
    if "-" in s:
        s = s.split("-")[0]
    return s


# ---- Active QRZ lookup queue ----
# When add_spot sees an unknown spotter, we enqueue it. A background worker
# pops from the queue, calls QRZ's XML API (rate-limited), and writes the
# result back to qrz_cache.json so GTBridge benefits from the same lookups.
_lookup_queue: "OrderedDict[str, None]" = OrderedDict()  # used as ordered set
_lookup_queue_lock = threading.Lock()
_negative_cache: dict[str, float] = {}  # callsign -> ts of failed lookup; skip for a while
_NEGATIVE_TTL = 86400  # don't re-look-up the same unresolvable call for 24h
_LOOKUP_RATE_SEC = 2.0  # min seconds between QRZ API calls


def _enqueue_lookup(callsign: str):
    if not callsign:
        return
    with _qrz_cache_lock:
        if callsign in _qrz_cache:
            return  # already resolved
    now = time.time()
    last_failed = _negative_cache.get(callsign)
    if last_failed and (now - last_failed) < _NEGATIVE_TTL:
        return  # skip recently-failed lookups
    with _lookup_queue_lock:
        if callsign in _lookup_queue:
            return
        _lookup_queue[callsign] = None
        # Cap queue size — drop oldest if we ever runaway
        while len(_lookup_queue) > 5000:
            _lookup_queue.popitem(last=False)


def _qrz_writeback(callsign: str, grid: str):
    """Atomically merge a single (callsign, grid) into qrz_cache.json on disk
    so GTBridge picks it up too. Re-reads current disk state to avoid clobbering
    entries GTBridge wrote concurrently."""
    try:
        if QRZ_CACHE_PATH.exists():
            disk = json.loads(QRZ_CACHE_PATH.read_text())
        else:
            disk = {}
        if disk.get(callsign) == grid:
            # In-memory cache is also kept in sync below, no disk change needed.
            return
        disk[callsign] = grid
        tmp = QRZ_CACHE_PATH.with_suffix(QRZ_CACHE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(disk, indent=1, sort_keys=True))
        tmp.replace(QRZ_CACHE_PATH)
    except Exception as e:
        log.warning("QRZ cache writeback failed for %s: %s", callsign, e)


# ---- JA prefecture cache (for the WAJA spot pill) ----
# Maps a JA callsign -> prefecture code ("01".."47"), or "" when QRZ knows the
# call but we couldn't resolve a prefecture from its addr2 (cache the negative so
# we don't keep re-querying). Resolved best-effort from QRZ addr2 by the same
# lookup worker, on a separate lower-priority queue. Advisory only — never award
# credit (that comes from the logged QSO's STATE/CNTY). Written only by us, so no
# external-reload loop is needed (unlike the GTBridge-shared grid cache).
JA_PREF_CACHE_PATH = _BASE_DIR / "ja_pref_cache.json"
_ja_pref_cache: dict[str, str] = {}
_ja_pref_cache_lock = threading.Lock()
_pref_queue: "OrderedDict[str, None]" = OrderedDict()  # JA calls awaiting prefecture lookup
_pref_queue_lock = threading.Lock()


def load_ja_pref_cache():
    global _ja_pref_cache
    try:
        data = json.loads(JA_PREF_CACHE_PATH.read_text())
        with _ja_pref_cache_lock:
            _ja_pref_cache = data
        log.info("JA prefecture cache loaded: %d entries", len(data))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("JA prefecture cache load failed: %s", e)


def _ja_pref_writeback(callsign: str, code: str):
    """Persist a single (callsign, code) to ja_pref_cache.json. code may be ""
    (negative cache: call exists but no resolvable prefecture)."""
    try:
        with _ja_pref_cache_lock:
            snapshot = dict(_ja_pref_cache)
        tmp = JA_PREF_CACHE_PATH.with_suffix(JA_PREF_CACHE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(snapshot, indent=1, sort_keys=True))
        tmp.replace(JA_PREF_CACHE_PATH)
    except Exception as e:
        log.warning("JA prefecture cache writeback failed for %s: %s", callsign, e)


def _enqueue_pref(callsign: str):
    """Queue a JA callsign for a prefecture (addr2) lookup, if not already
    cached or queued. Lower priority than grid lookups."""
    if not callsign:
        return
    with _ja_pref_cache_lock:
        if callsign in _ja_pref_cache:
            return
    with _pref_queue_lock:
        if callsign in _pref_queue:
            return
        _pref_queue[callsign] = None
        while len(_pref_queue) > 5000:
            _pref_queue.popitem(last=False)


def _ja_pref_for_spot(call: str):
    """Return (pref_code, waja_status) for a JA spot. Enqueues a lookup on a
    cache miss. ("","") forms: unknown-yet or unresolved -> ("", None) -> no pill;
    a resolved code -> (code, "new"/"worked"/"confirmed")."""
    with _ja_pref_cache_lock:
        code = _ja_pref_cache.get(call)
    if code is None:
        _enqueue_pref(call)      # not looked up yet
        return "", None
    if not code:
        return "", None          # looked up, no resolvable prefecture
    return code, (_worked.prefecture_status(code) if _worked else "new")


def _apply_prefecture_to_cache(call: str, code: str):
    """After a prefecture lookup resolves, stamp it onto any cached JA spots for
    that call so the pill appears immediately (not just on the next refresh)."""
    if not _worked:
        return
    status = _worked.prefecture_status(code)
    with _lock:
        for s in _cache.values():
            if s.get("dx_call") == call and s.get("country") == "Japan":
                s["waja_pref"] = code
                s["waja_status"] = status


def qrz_lookup_worker():
    """Background worker: pops from queue, calls QRZ XML API, caches result."""
    import qrz as qrz_module  # use existing GTBridge QRZ client for auth + parse
    try:
        secrets = json.loads((_BASE_DIR / "secrets.json").read_text())
        client = qrz_module.QRZLookup(
            username=secrets["qrz_user"],
            password=secrets["qrz_password"],
            cache_file=str(QRZ_CACHE_PATH),  # share cache with GTBridge
        )
    except Exception as e:
        log.warning("QRZ lookup worker init failed (no active lookups): %s", e)
        return

    while True:
        # Grid lookups take priority — in strict mode they gate spot ingest.
        with _lookup_queue_lock:
            callsign = _lookup_queue.popitem(last=False)[0] if _lookup_queue else None
        if callsign:
            # Skip if it's been resolved between enqueue and pop
            with _qrz_cache_lock:
                if callsign in _qrz_cache:
                    continue
            try:
                grid = client._fetch_grid(callsign)  # synchronous; blocks for HTTP
            except Exception as e:
                log.warning("QRZ lookup error for %s: %s", callsign, e)
                grid = None
                _negative_cache[callsign] = time.time()
            if grid and isinstance(grid, str) and len(grid) >= 4:
                with _qrz_cache_lock:
                    _qrz_cache[callsign] = grid
                _qrz_writeback(callsign, grid)
                log.info("QRZ lookup: %s -> %s", callsign, grid)
            else:
                _negative_cache[callsign] = time.time()
            time.sleep(_LOOKUP_RATE_SEC)
            continue

        # Then JA prefecture lookups (advisory WAJA pill, lower priority).
        with _pref_queue_lock:
            pcall = _pref_queue.popitem(last=False)[0] if _pref_queue else None
        if pcall:
            with _ja_pref_cache_lock:
                if pcall in _ja_pref_cache:
                    continue  # resolved between enqueue and pop
            try:
                addr2 = client.fetch_addr2(pcall)
            except Exception as e:
                log.warning("QRZ addr2 lookup error for %s: %s", pcall, e)
                addr2 = None
            if addr2 is None:
                # Transient failure — don't cache; it'll re-enqueue on the next
                # JA spot for this call (naturally rate-limited by spot arrival).
                pass
            else:
                code = resolve_prefecture(pcall, addr2) or ""
                with _ja_pref_cache_lock:
                    _ja_pref_cache[pcall] = code
                _ja_pref_writeback(pcall, code)
                if code:
                    log.info("JA prefecture: %s -> %s", pcall, code)
                    _apply_prefecture_to_cache(pcall, code)
            time.sleep(_LOOKUP_RATE_SEC)
            continue

        time.sleep(5)


def _should_inject_to_flex(record: dict, active_bands: set[str]) -> bool:
    """True if this cached spot record should be queued for panadapter injection."""
    if not FLEX_INJECT_ENABLED or not _flex or not _flex.connected:
        return False
    if record["band"] not in active_bands:
        return False
    mode = (record.get("mode") or "").upper()
    if mode in FLEX_INJECT_SKIP_MODES:
        return False
    key = (record["band"], round(record["freq_khz"], 1), record["dx_call"])
    last = _flex_recent_injects.get(key)
    if last and (time.time() - last) < FLEX_INJECT_DEDUP_SEC:
        return False
    return True


def _maybe_queue_flex_inject(record: dict):
    """Best-effort enqueue for Flex injection. Called from add_spot. Non-blocking."""
    if _flex_inject_queue is None:
        return
    snap = active_bands_snapshot()
    if not snap.get("connected"):
        return
    active = set(snap.get("bands", []))
    if not active:
        return
    if not _should_inject_to_flex(record, active):
        return
    try:
        _flex_inject_queue.put_nowait(record)
    except asyncio.QueueFull:
        pass  # queue saturated; skip rather than block ingest


async def flex_inject_worker():
    """Drain the inject queue at rate limit, push each spot to the Flex panadapter.

    Per-spot rate limit (FLEX_INJECT_RATE_SEC = 0.5s) keeps API command load
    well below the level that competed with SmartSDR's audio DPCs in the
    earlier GTBridge-firehose era. With a 600s spot lifetime on the Flex
    side, we re-inject each spot at most once per 5 minutes (dedup window),
    so a typical cache of ~50 active-band non-FT spots produces ~10
    injects/min steady-state — orders of magnitude below the breaking point."""
    global _flex_inject_queue
    while True:
        try:
            record = await _flex_inject_queue.get()
        except asyncio.CancelledError:
            break
        try:
            if not _flex or not _flex.connected:
                continue
            await _flex.spot_add(
                callsign=record["dx_call"],
                freq_mhz=record["freq_khz"] / 1000.0,
                mode=(record.get("mode") or ""),
                comment=(record.get("spotter") or ""),
                lifetime_seconds=FLEX_INJECT_LIFETIME_SEC,
            )
            key = (record["band"], round(record["freq_khz"], 1), record["dx_call"])
            _flex_recent_injects[key] = time.time()
            # Periodic cleanup of stale dedup entries
            if len(_flex_recent_injects) > 2000:
                cutoff = time.time() - FLEX_INJECT_DEDUP_SEC
                stale = [k for k, t in _flex_recent_injects.items() if t < cutoff]
                for k in stale:
                    _flex_recent_injects.pop(k, None)
        except Exception as e:
            log.warning("Flex inject failed for %s: %s", record.get("dx_call"), e)
        await asyncio.sleep(FLEX_INJECT_RATE_SEC)


# ---------------- WSJT-X UDP integration ----------------
# Per-client WSJT-X state. Keyed by client_id so multiple WSJT-X instances
# can be tracked independently (typical in SO2R: one WSJT-X per radio).
# Each entry carries the latest dial freq + mode + the source addr so
# Reply UDP packets can be sent back to the right WSJT-X.
_wsjtx_state: dict[str, dict] = {}
_wsjtx_state_lock = threading.Lock()
_wsjtx_transport = None  # set by the listener task on startup; reused for sends

# FT8 message text → spotted callsign + grid extractor.
# Standard message forms:
#   "CQ <call> <grid>"              — call is the transmitter
#   "CQ DX <call> <grid>"           — same, with directional prefix
#   "CQ TEST <call> <grid>"         — contest CQ (Field Day, etc.)
#   "<callee> <transmitter> <msg>"  — directed message; transmitter is the SECOND call
#                                     per the FT8 protocol convention. <msg> may be a
#                                     grid (AB12), a signal report (-15), an ack (R-15 / RR73),
#                                     or a 73.
_GRID_RE = re.compile(r'^[A-R]{2}[0-9]{2}([a-x]{2})?$')
_CALL_RE = re.compile(r'^[A-Z0-9]{1,3}[0-9][A-Z0-9]*[A-Z](?:/[A-Z0-9]+)?$')
_CQ_PREFIX_WORDS = {"DX", "NA", "EU", "AS", "AF", "OC", "SA", "JA", "TEST", "FD", "WW"}
# FT8 reserved tokens that can collide with the grid regex (RR73 in particular —
# RR is in A-R range and 73 looks like grid digits, but the token is the
# final-confirmation marker, never an actual grid).
_FT8_RESERVED_TOKENS = {"RR73", "RRR", "73"}


def _is_grid(s: str) -> bool:
    if not s or s in _FT8_RESERVED_TOKENS:
        return False
    return bool(_GRID_RE.match(s))


def _looks_like_call(s: str) -> bool:
    """Heuristic: starts with letter or digit, contains a digit, looks like a callsign.
    Tolerates portable/modifier suffixes (/P, /M, /<region>).
    """
    return bool(s and _CALL_RE.match(s.upper()))


def parse_ft8_message(message: str) -> tuple[str | None, str | None]:
    """Extract (transmitting_call, grid) from a WSJT-X-decoded FT8/FT4 message text.

    Returns (call, grid) where either may be None if the form isn't recognized.
    The *call* returned is the station that TRANSMITTED the signal — the one
    we'd want to spot on the cluster. Per FT8 protocol, in directed messages
    that's the SECOND call ("<being_called> <transmitter> <message>").
    """
    if not message:
        return None, None
    parts = message.strip().split()
    if not parts:
        return None, None

    if parts[0] == "CQ":
        # "CQ <call> [<grid>]" or "CQ <prefix> <call> [<grid>]"
        if len(parts) >= 2 and parts[1] in _CQ_PREFIX_WORDS:
            call_idx = 2
        else:
            call_idx = 1
        if len(parts) > call_idx and _looks_like_call(parts[call_idx]):
            call = parts[call_idx]
            grid = parts[call_idx + 1] if len(parts) > call_idx + 1 and _is_grid(parts[call_idx + 1]) else None
            return call, grid
        return None, None

    # Directed exchange: "<callee> <transmitter> <message>"
    # Skip if first token isn't a call (might be a non-standard format).
    if len(parts) >= 2 and _looks_like_call(parts[0]) and _looks_like_call(parts[1]):
        transmitter = parts[1]
        grid = None
        if len(parts) >= 3 and _is_grid(parts[2]):
            grid = parts[2]
        return transmitter, grid

    return None, None


def _wsjtx_spotter_label(client_id: str) -> str:
    """Extract a short instance label from a WSJT-X client_id.

    WSJT-X title format is typically 'WSJT-X - <InstanceName>' (e.g.
    'WSJT-X - SliceA' for the SO2R instance Fred runs on his Flex SliceA).
    This returns the part after the last ' - ' separator, which becomes
    the displayed spotter on WSJTX-LOCAL spots — visual confirmation
    that the spot came from the operator's running WSJT-X right now.

    Falls back to the whole client_id if no separator, or 'WSJTX' if empty.
    """
    cid = (client_id or "").strip()
    if not cid:
        return "WSJTX"
    if " - " in cid:
        label = cid.rsplit(" - ", 1)[-1].strip()
        return label or "WSJTX"
    return cid


def _wsjtx_state_update(client_id: str, parsed: dict, source_addr: tuple):
    """Refresh per-client WSJT-X state from a parsed Status message."""
    with _wsjtx_state_lock:
        _wsjtx_state[client_id] = {
            "ts": time.time(),
            "client_id": client_id,
            "dial_freq_hz": parsed.get("dial_freq_hz", 0),
            "mode": parsed.get("mode", "") or "",
            "sub_mode": parsed.get("sub_mode", "") or "",
            "rx_df": parsed.get("rx_df", 0) or 0,
            "tx_df": parsed.get("tx_df", 0) or 0,
            "de_call": parsed.get("de_call", "") or "",
            "de_grid": parsed.get("de_grid", "") or "",
            "tx_enabled": parsed.get("tx_enabled", False),
            "transmitting": parsed.get("transmitting", False),
            "decoding": parsed.get("decoding", False),
            "source_addr": source_addr,  # (host, port) — where to send Reply UDP back to
        }


def _wsjtx_state_get_latest() -> dict | None:
    """Return the most recently-updated WSJT-X state entry, or None if empty/stale."""
    cutoff = time.time() - WSJTX_STATE_TTL_SEC
    with _wsjtx_state_lock:
        live = [s for s in _wsjtx_state.values() if s["ts"] >= cutoff]
        if not live:
            return None
        return max(live, key=lambda s: s["ts"])


def _wsjtx_state_for_band(band: str) -> dict | None:
    """Return the most-recent live WSJT-X state whose dial frequency falls
    on the given band, or None if no WSJT-X instance is currently tuned to
    that band. Used by click-to-tune to gate WSJT-X-mode routing on actual
    band match — clicking an FT8 spot when no WSJT-X is on that band does
    nothing (intentionally, per operator-respect rule)."""
    if not band:
        return None
    cutoff = time.time() - WSJTX_STATE_TTL_SEC
    matches = []
    with _wsjtx_state_lock:
        for state in _wsjtx_state.values():
            if state["ts"] < cutoff:
                continue
            state_band = dxcluster.freq_to_band(state["dial_freq_hz"] / 1000.0)
            if state_band == band:
                matches.append(state)
    if not matches:
        return None
    return max(matches, key=lambda s: s["ts"])


def _ingest_wsjtx_decode(parsed: dict, source_addr: tuple):
    """Convert a WSJT-X Decode message into a DXSpot and feed it into add_spot()
    with cluster_name='WSJTX-LOCAL' so source-precedence dedup gives our local
    decodes priority over external cluster duplicates of the same signal.
    """
    client_id = parsed.get("client_id") or "WSJTX"
    state = None
    with _wsjtx_state_lock:
        state = _wsjtx_state.get(client_id)
    if state is None:
        # No Status received yet for this client_id — we don't know the dial freq,
        # so we can't compute the RF frequency. Skip until first Status arrives.
        return
    dial_hz = state.get("dial_freq_hz", 0)
    if not dial_hz:
        return

    delta_freq = parsed.get("delta_freq", 0) or 0
    rf_hz = dial_hz + delta_freq
    freq_khz = rf_hz / 1000.0

    message = parsed.get("message", "") or ""
    call, grid = parse_ft8_message(message)
    if not call:
        return  # message format we don't recognize as a spottable signal

    # Filter our own transmissions if WSJT-X loops them back as decodes.
    de_call = (state.get("de_call") or "").upper()
    if de_call and call.upper() == de_call:
        return

    # "Calling me": a directed message whose CALLEE (the FIRST token) is my own
    # callsign — i.e. some station is transmitting toward ME this cycle (saw me
    # working someone and clicked me to start a QSO, or is replying to my CQ).
    # These should jump out and bypass the roster's band/mode/wanted/radius
    # filters, since the whole point is "don't miss someone trying to work me."
    mp = message.strip().split()
    calling_me = bool(len(mp) >= 2 and mp[0].upper() == CALLSIGN.upper()
                      and _looks_like_call(mp[1]))

    # WSJT-X mode glyph maps to a printable mode string. ~ = FT8, + = FT4.
    glyph = parsed.get("mode", "") or ""
    mode_str = {"~": "FT8", "+": "FT4"}.get(glyph, "FT8")

    spot = dxcluster.DXSpot(
        spotter=_wsjtx_spotter_label(client_id),  # 'SliceA' from 'WSJT-X - SliceA'
        freq_khz=freq_khz,
        dx_call=call,
        comment=message[:60],
        time_utc=time.strftime("%H%M", time.gmtime()),
        mode=mode_str,
        snr=parsed.get("snr"),
        grid=grid,
        audio_offset=delta_freq,
    )
    add_spot(spot, "WSJTX-LOCAL", calling_me=calling_me)

    # Stash WSJT-X-specific fields needed to construct a Reply that matches
    # the original decode in WSJT-X's recent-decode list. Reply matching
    # uses time_ms and delta_time as lookup keys (not just tolerance), so
    # current_time_ms() at click time wouldn't match — we need to replay
    # the same time the decode reported.
    cache_key = _spot_dedup_key(dxcluster.freq_to_band(freq_khz), mode_str, freq_khz, call)
    with _lock:
        cached = _cache.get(cache_key)
        if cached and cached.get("source") == "WSJTX-LOCAL":
            cached["wsjtx_time_ms"] = parsed.get("time_ms", 0) or 0
            cached["wsjtx_delta_time"] = parsed.get("delta_time", 0.0) or 0.0
            cached["wsjtx_glyph"] = parsed.get("mode", "~") or "~"


DXCC_CHALLENGE_BANDS = ("160m", "80m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m")
DXCC_VHF_BANDS = ("2m", "1.25m", "70cm", "33cm", "23cm", "13cm", "9cm", "6cm", "3cm", "1.25cm")
# Five-Band DXCC / Five-Band WAS use exactly the five classic HF bands —
# NOT 160/30/17/12/6. Both awards require the per-band target confirmed on
# each of these five (100 entities for 5BDXCC, 50 states for 5BWAS).
FIVE_BAND_BANDS = ("80m", "40m", "20m", "15m", "10m")
DXCC_HONOR_ROLL_TOTAL = 340   # current ARRL DXCC entity count (changes over time)
VUCC_BANDS = ("6m", "2m", "1.25m", "70cm", "33cm", "23cm", "13cm", "9cm", "6cm", "3cm", "1.25cm")
WAS_TARGET = 50
WAJA_TARGET = 47   # JARL Worked All Japan prefectures
WAZ_TARGET = 40
WPX_TARGET = 400   # CQ WPX Award (Mixed) basic level


# CQ WPX prefix designators that DON'T count as a prefix on their own (treated
# as droppable suffixes): portable/mobile/QRP etc.
_WPX_SUFFIX = {"P", "M", "MM", "AM", "A", "E", "J", "QRP", "R", "B", "LH", "N", "T", "Y"}


def _wpx_prefix(call: str) -> str:
    """CQ WPX prefix for a callsign. Handles the common case (letters/numerals
    up to and including the last digit) plus portable designators per the WPX
    rules: a portable region-digit (W3GRF/4 -> W4), a portable prefix
    (KH6XX/W0 -> W0, N8BJQ/KP2 -> KP2), and a no-digit portable prefix
    (PA/N8BJQ -> PA0). Non-counting suffixes (/P, /M, /QRP, bare letters) are
    dropped. Exotic interior-subdivision designators aren't special-cased —
    fine for a worked-prefix tally, where the vast majority of calls are plain."""
    call = (call or "").strip().upper()
    if not call:
        return ""
    parts = [p for p in call.split("/") if p]
    # drop trailing non-counting suffix designators (/P, /M, /QRP, single letters)
    while len(parts) > 1 and (parts[-1] in _WPX_SUFFIX
                              or (len(parts[-1]) == 1 and parts[-1].isalpha())):
        parts.pop()
    if len(parts) >= 2:
        a, b = parts[0], parts[1]
        desig, base = (a, b) if len(a) <= len(b) else (b, a)   # shorter = designator
        if desig.isdigit():                       # region change: home letters + new digit
            m = re.match(r"([A-Z]+)", base)
            return (m.group(1) if m else base[:1]) + desig[-1]
        if re.search(r"\d", desig):               # designator carries a digit
            return re.match(r".*\d", desig).group(0)
        return desig + "0"                         # no-digit designator -> append 0
    m = re.search(r".*\d", parts[0])               # plain call: through the last digit
    return m.group(0) if m else parts[0] + "0"


def _build_ffma_chase(grid_qsos: dict) -> dict:
    """FFMA chase intel for the dedicated FFMA tab, all derived LIVE from the
    6m FFMA QSO detail so entries self-clear as confirmations arrive:
      - pending: worked-but-unconfirmed grids + who-to-nudge (multi-op grids
        confirm easier; flagged). Sorted rarest-first, then most recent.
      - recent_atno: the grid whose first-ever 6m QSO is the most recent
        (your latest All-Time-New-One), with confirm status.
      - rares_worked: rare/uncommon-tier grids already in the log (the
        trophies — the ones usually only a rover/portable puts on the air).
    """
    def tier_of(g):
        info = _FFMA_RARITY.get(g) or {}
        return (info.get("tier", "common"), float(info.get("pct_needed", 0.0)),
                int(info.get("leaders_needing", 0)))

    pending, rares, atno_list = [], [], []
    for g, recs in grid_qsos.items():
        confirmed = any(r[3] for r in recs)
        tier, pct, leaders = tier_of(g)
        # unique calls that could confirm this grid, most-recent QSO first
        calls = []
        for d, t, c, cf in sorted(recs, reverse=True):
            if c and c not in calls:
                calls.append(c)
        fd, ft = min((r[0], r[1]) for r in recs)   # first-ever QSO for this grid
        first_call = sorted(recs)[0][2]            # the call that opened the grid
        if not confirmed:
            pending.append({"grid": g, "date": fd, "calls": calls,
                            "multi_op": len(calls) > 1, "tier": tier, "pct": pct})
        if tier in ("rare", "uncommon"):
            rep = sorted([r for r in recs if r[3]] or recs)[0]   # prefer a confirmed QSO
            rares.append({"grid": g, "tier": tier, "pct": pct, "leaders": leaders,
                          "confirmed": confirmed, "call": rep[2], "date": rep[0]})
        atno_list.append({"grid": g, "date": fd, "time": ft, "call": first_call,
                          "confirmed": confirmed, "tier": tier, "pct": pct})

    pending.sort(key=lambda p: (p["pct"], p["date"]), reverse=True)   # rarest, then newest
    rares.sort(key=lambda r: (-r["pct"], r["grid"]))                  # rarest first
    atno_list.sort(key=lambda a: (a["date"], a["time"]), reverse=True)  # newest grids first
    recent_atnos = atno_list[:5]
    return {"pending": pending, "rares_worked": rares,
            "recent_atno": recent_atnos[0] if recent_atnos else None,  # back-comat
            "recent_atnos": recent_atnos}


def _build_grid_discrepancies() -> dict:
    """Grid-award QSOs where YOUR logged grid != the grid LoTW actually credited.
    Rover/portable mismatches (the op's TQSL station location left on home), a
    gridless LoTW upload, or your own mis-log — each silently costs FFMA/VUCC
    credit (Grayline now counts only LoTW's grid, matching the award/Gridzilla).
    6m+ only (grid matters there) and terrestrial only; matched by
    (call, band, qso_date) against the authoritative LoTW download. This view
    doesn't exist in Gridzilla — it's the actionable "who needs to re-upload" list."""
    if not _worked:
        return {"count": 0, "items": []}
    try:
        lotw_recs = worked_state._parse_adif_qsos(worked_state._LOTW_ADIF_PATH)
    except Exception as e:
        log.warning("grid-discrepancy: LoTW ADIF unavailable (%s)", e)
        return {"count": 0, "items": []}
    lotw_grid = {}
    for r in lotw_recs:
        key = ((r.get("call") or "").upper(),
               (r.get("band") or "").lower(),
               r.get("qso_date", ""))
        lotw_grid[key] = (r.get("grid") or "").upper()[:4]
    items, seen = [], set()
    for q in _worked.qsos:
        band = (q.get("band") or "").lower()
        if band not in VUCC_BANDS:
            continue
        if (q.get("prop_mode") or "").upper() == "SAT":
            continue
        logged = (q.get("grid") or "").upper()[:4]
        if not logged:
            continue
        call = (q.get("call") or "").upper()
        date = q.get("qso_date", "")
        credited = lotw_grid.get((call, band, date))
        if credited is None or credited == logged:
            continue   # QSO isn't LoTW-confirmed, or the grids agree — no issue
        dk = (call, band, date, logged)
        if dk in seen:
            continue
        seen.add(dk)
        # Does this actually cost an award? The advertised grid affects an award
        # only if YOU don't already have it confirmed via some other QSO — i.e.
        # the mismatch is the sole reason that grid isn't in the bank.
        # Only surface discrepancies that actually COST a grid — i.e. you don't
        # already hold the advertised grid another way. No point flagging a
        # mismatch on a grid that's confirmed elsewhere.
        if (logged, band) in _worked.confirmed_grid_band:
            continue
        rar = _FFMA_RARITY.get(logged) or {}
        items.append({
            "band": band, "call": call, "date": date,
            "logged": logged, "credited": credited or None,
            "ffma": band == "6m" and logged in _FFMA_GRID_SET,
            "tier": rar.get("tier"),            # FFMA grid rarity (None for non-FFMA grids)
            "pct": rar.get("pct_needed"),       # % of FFMA leaders still needing it
            "kind": "no_grid" if not credited else "mismatch",
        })
    # Rarest first — the worth-an-email ones on top; common grids sink.
    items.sort(key=lambda x: (-(x["pct"] or 0), not x["ffma"], x["band"], x["call"], x["date"]))
    return {"count": len(items), "items": items}


def _wae_key(e):
    """WAE country key for a cty.dat entity. The five WAE split entities
    (cty.dat '*' prefix — Sicily, Shetland, Bear Island, European Turkey,
    Vienna Intl Ctr) share a DXCC id with their parent, so they key by prefix;
    every other WAE country keys by its DXCC id."""
    if e is None:
        return None
    return e.prefix if e.prefix.startswith("*") else e.dxcc


_WAE_ENTITIES_CACHE = None


def _wae_entity_keys() -> dict:
    """WAE country key -> name, built from cty.dat continent==EU entities.
    Validated exact-match (73 entities, zero diff) against DARC's official
    Worked All Europe Country List, so this is the governing-body set, not a
    continent approximation that happens to be close."""
    global _WAE_ENTITIES_CACHE
    if _WAE_ENTITIES_CACHE is None and _cty:   # don't cache empty before cty loads
        out: dict = {}
        for store in (_cty._prefixes, _cty._entities):
            for e in store.values():
                if e.continent == "EU":
                    out.setdefault(_wae_key(e), e.entity)
        _WAE_ENTITIES_CACHE = out
    return _WAE_ENTITIES_CACHE or {}


def _build_scores_payload() -> dict:
    """ARRL-default award rollup. Per the per-band-scope memory rule, this
    surfaces only ARRL-tracked awards by default (DXCC variants, Challenge,
    WAS, WAZ, VUCC). Personal goals via user extension is a future feature."""
    if not _worked:
        return {"error": "worked_state not loaded"}

    # DXCC by mode class — confirmed entities (set of DXCC IDs). DXCC-ID
    # keyed (not country-name) so QRZ/cty.dat label drift can't double-count.
    # Mixed = any mode class. Includes a small "Other" bucket (digital voice,
    # etc. classified as Other) but ARRL only awards the four named variants.
    dxcc_w = {"Mixed": set(_worked.worked_dxcc), "CW": set(), "Phone": set(), "Digital": set()}
    dxcc_c = {"Mixed": set(_worked.confirmed_dxcc), "CW": set(), "Phone": set(), "Digital": set()}
    for (d, cls) in _worked.worked_dxcc_modeclass:
        if cls in dxcc_w:
            dxcc_w[cls].add(d)
    for (d, cls) in _worked.confirmed_dxcc_modeclass:
        if cls in dxcc_c:
            dxcc_c[cls].add(d)
    dxcc = {
        cls: {"worked": len(dxcc_w[cls]), "confirmed": len(dxcc_c[cls])}
        for cls in ("Mixed", "CW", "Phone", "Digital")
    }
    # Satellite is a fifth DXCC variant ARRL recognizes (PROP_MODE=SAT).
    dxcc["Satellite"] = {
        "worked": len(_worked.worked_dxcc_satellite),
        "confirmed": len(_worked.confirmed_dxcc_satellite),
    }

    # DXCC Challenge — sum of confirmed (dxcc_id, band) slots on the 10
    # Challenge bands (160-6m). Bands above 6m do NOT count toward Challenge.
    # Computed AFTER dxcc_by_band below so it pulls from the same per-band
    # counts (band_w / band_c).

    # Per-band DXCC entity counts. Count from the worked-state (dxcc_id, band)
    # sets — the SAME mirror-merged sets that drive the spot panel's band-slot
    # coloring — so the scores panel agrees with the spots AND with LoTW's
    # authoritative per-band totals.
    #
    # The previous path recomputed from self.qsos with QRZ flags only (missing
    # the LoTW-mirror merge → under-counted by every LoTW-confirmed-but-QRZ-
    # unsynced QSO) and applied a satellite exclusion. But LoTW credits a
    # satellite-band-tagged QSO ON that band — its per-band totals include them
    # — so the exclusion made us disagree with the authoritative ledger. Using
    # the sets matches LoTW exactly (verified against the live DXCC account) and
    # keeps the scores panel consistent with the spot coloring.
    band_w: dict[str, set[str]] = {}
    band_c: dict[str, set[str]] = {}
    for (d, b) in _worked.worked_dxcc_band:
        band_w.setdefault(b, set()).add(d)
    for (d, b) in _worked.confirmed_dxcc_band:
        band_c.setdefault(b, set()).add(d)
    dxcc_by_band: dict[str, dict] = {}
    for b in DXCC_CHALLENGE_BANDS:
        dxcc_by_band[b] = {"worked": len(band_w.get(b, set())),
                           "confirmed": len(band_c.get(b, set()))}
    for b in DXCC_VHF_BANDS:
        w = len(band_w.get(b, set()))
        c = len(band_c.get(b, set()))
        if w or c:
            dxcc_by_band[b] = {"worked": w, "confirmed": c}

    # Derive Challenge from the per-band counts so it agrees with the per-band
    # rows above and with LoTW's Challenge total.
    challenge_worked = sum(dxcc_by_band[b]["worked"] for b in DXCC_CHALLENGE_BANDS)
    challenge_confirmed = sum(dxcc_by_band[b]["confirmed"] for b in DXCC_CHALLENGE_BANDS)

    # FFMA + per-band VUCC — terrestrial only (PROP_MODE != SAT). Same
    # rationale as DXCC by band: satellite QSOs frequently carry an HF/VHF
    # band tag (uplink), and ARRL counts them under Satellite VUCC, not the
    # per-band award. Computed from the deduped qsos list so backfilled
    # prop_mode is honored.
    ffma_target = len(_FFMA_GRID_SET) or 488
    vucc_band_w: dict[str, set[str]] = {}
    vucc_band_c: dict[str, set[str]] = {}
    ffma_worked_set: set[str] = set()
    ffma_confirmed_set: set[str] = set()
    # Per-grid 6m FFMA QSO detail, for the chase panel (pending list / ATNO /
    # rares worked). grid4 -> list of (qso_date, time_on, call, confirmed, exch_grid)
    ffma_grid_qsos: dict[str, list] = {}
    for q in _worked.qsos:
        pm = (q.get("prop_mode") or "").strip().upper()
        if pm == "SAT":
            continue
        b = (q.get("band") or "").strip().lower()
        g = (q.get("grid") or "").strip().upper()[:4]
        if not b or not g:
            continue
        # Grid-award confirmation is LoTW-authoritative AND grid-specific. LoTW
        # credits the grid on its OWN confirmation record, which can differ from
        # the grid you logged on rover/portable mismatches (e.g. a rover whose
        # TQSL station location was left set to home — the QSO confirms, but at
        # the wrong grid). worked_state.confirmed_grid_band holds the LoTW grids
        # (QRZ/eQSL flags are stripped at ingest), so a logged grid counts as
        # confirmed only when LoTW actually confirmed THAT grid. This is what
        # makes FFMA/VUCC match LoTW (= what Gridzilla's direct LoTW sync shows).
        confirmed = (g, b) in _worked.confirmed_grid_band
        if b in VUCC_BANDS:
            vucc_band_w.setdefault(b, set()).add(g)
            if confirmed:
                vucc_band_c.setdefault(b, set()).add(g)
        if b == "6m" and g in _FFMA_GRID_SET:
            ffma_worked_set.add(g)
            if confirmed:
                ffma_confirmed_set.add(g)
            ffma_grid_qsos.setdefault(g, []).append((
                q.get("qso_date", ""), (q.get("time_on") or "").zfill(6),
                (q.get("call") or "").strip().upper(), confirmed))

    ffma_chase = _build_ffma_chase(ffma_grid_qsos)

    # CQ WPX — unique worked prefixes (Mixed). Own pass: WPX counts every QSO
    # (grid not required), so it can't piggyback the grid-gated FFMA/VUCC loop.
    wpx_worked: set[str] = set()
    wpx_confirmed: set[str] = set()
    for q in _worked.qsos:
        px = _wpx_prefix(q.get("call"))
        if not px:
            continue
        wpx_worked.add(px)
        if ((q.get("lotw_qsl_rcvd") or "").upper() in ("Y", "V")
                or (q.get("qsl_rcvd") or "").upper() in ("Y", "V")
                or (q.get("eqsl_qsl_rcvd") or "").upper() in ("Y", "V")):
            wpx_confirmed.add(px)

    # WAE (Worked All Europe, DARC) — worked/confirmed WAE countries + per band.
    # Resolves dxcc=False so the WAE split entities (Sicily, Shetland, ...) count
    # separately from their DXCC parent.
    wae_keys = _wae_entity_keys()
    wae_target = len(wae_keys) or 73
    wae_worked: set = set()
    wae_confirmed: set = set()
    wae_conf_by_band: dict[str, set] = {}
    if _cty:
        for q in _worked.qsos:
            k = _wae_key(_cty.lookup(q.get("call") or "", dxcc=False))
            if k is None or k not in wae_keys:
                continue
            wae_worked.add(k)
            if ((q.get("lotw_qsl_rcvd") or "").upper() in ("Y", "V")
                    or (q.get("qsl_rcvd") or "").upper() in ("Y", "V")
                    or (q.get("eqsl_qsl_rcvd") or "").upper() in ("Y", "V")):
                wae_confirmed.add(k)
                b = (q.get("band") or "").strip().lower()
                if b:
                    wae_conf_by_band.setdefault(b, set()).add(k)
    wae_by_band_detail = {}
    for b, ks in wae_conf_by_band.items():
        missing = sorted(name for k, name in wae_keys.items() if k not in ks)
        wae_by_band_detail[b] = {"confirmed": len(ks),
                                 "missing": missing if len(missing) <= 10 else []}
    # overall (Mixed) chase lists — the actionable cut
    wae_missing = sorted(name for k, name in wae_keys.items() if k not in wae_confirmed)
    wae_unworked = sorted(name for k, name in wae_keys.items() if k not in wae_worked)

    # WAS-by-band: confirmed states per band + the missing-state list, so the UI
    # can surface single-band WAS endorsements (e.g. 160m / 6m WAS) and show
    # exactly which states remain. 5BWAS uses the 5 contest bands separately.
    was_states_by_band: dict[str, set] = {}
    for (_st, b) in _worked.confirmed_state_band:
        if _st in _US_STATES_50:
            was_states_by_band.setdefault(b, set()).add(_st)
    was_by_band = {b: len(s) for b, s in was_states_by_band.items()}
    was_by_band_detail = {
        b: {"confirmed": len(s), "missing": sorted(_US_STATES_50 - s)}
        for b, s in was_states_by_band.items()
    }

    # VUCC — confirmed grid count per VHF/UHF band, terrestrial only
    # (computed above into vucc_band_c with the SAT filter applied).
    vucc: dict[str, int] = {b: len(vucc_band_c.get(b, set())) for b in VUCC_BANDS
                             if vucc_band_c.get(b)}

    # Five-Band DXCC — 100 confirmed entities on EACH of the five classic bands.
    # Reuses band_c (confirmed dxcc_id per band) so it agrees with DXCC-by-band.
    five_dxcc_by_band = {b: len(band_c.get(b, set())) for b in FIVE_BAND_BANDS}
    five_dxcc_complete = sum(1 for b in FIVE_BAND_BANDS if five_dxcc_by_band[b] >= 100)

    # N-Band DXCC — 5BDXCC is the BASE award (the five classic bands); each
    # additional band at 100 confirmed is an ENDORSEMENT on it, so the milestone
    # shorthand is "NBDXCC" where N = total bands at 100. Classic + the 3 WARC
    # bands = 8BDXCC; + 160m + 6m = 10BDXCC. The level is only meaningful once
    # 5BDXCC itself is earned (all five classic bands), since the WARC/etc. bands
    # are endorsements ON 5BDXCC, not standalone here.
    _band_order = list(DXCC_CHALLENGE_BANDS) + list(DXCC_VHF_BANDS)
    nb_dxcc_bands = sorted(
        (b for b, d in dxcc_by_band.items() if d["confirmed"] >= 100),
        key=lambda b: _band_order.index(b) if b in _band_order else 99,
    )
    nb_dxcc_has_base = five_dxcc_complete == 5
    nb_dxcc_level = len(nb_dxcc_bands) if nb_dxcc_has_base else 0
    # Bands still short of 100 on the Challenge bands — the path to the next level.
    nb_dxcc_short = {b: dxcc_by_band[b]["confirmed"]
                     for b in DXCC_CHALLENGE_BANDS
                     if dxcc_by_band[b]["confirmed"] < 100}

    # Five-Band WAS — 50 confirmed states on EACH of the five classic bands.
    state_band_c: dict[str, set[str]] = {}
    for (_st, _b) in _worked.confirmed_state_band:
        state_band_c.setdefault(_b, set()).add(_st)
    five_was_by_band = {b: len(state_band_c.get(b, set())) for b in FIVE_BAND_BANDS}
    five_was_complete = sum(1 for b in FIVE_BAND_BANDS if five_was_by_band[b] >= 50)

    # DXCC Honor Roll — standing off confirmed Mixed entities. Honor Roll =
    # within 9 of the current total (>= TOTAL-9); #1 = all TOTAL. NOTE: this
    # counts every confirmed entity incl. any deleted ones, so it can read a
    # touch high vs ARRL's strict current-entity Honor Roll — informational.
    honor_confirmed = len(dxcc_c["Mixed"])

    # WAS by mode (per-mode WAS endorsements) — ARRL-eligible confirmations
    # (LoTW or card, no eQSL), keyed by (state, modeclass).
    was_by_mode = {}
    for _cls in ("CW", "Phone", "Digital"):
        was_by_mode[_cls] = {
            "worked": len({s for (s, c) in _worked.worked_state_modeclass if c == _cls}),
            "confirmed": len({s for (s, c) in _worked.confirmed_state_modeclass if c == _cls}),
        }

    # Triple Play — earn WAS three times: all 50 states on CW, on Phone, AND on
    # Digital, LoTW-confirmed ONLY (no paper, no eQSL). It's three sub-awards
    # (the three "legs"), so progress is naturally legs-complete out of 3, with
    # each leg's LoTW state count for detail.
    tp_legs = {cls: len({s for (s, c) in _worked.lotw_state_modeclass if c == cls})
               for cls in ("CW", "Phone", "Digital")}
    triple_play = {
        "legs": tp_legs,                                              # {"CW": 48, ...} LoTW state counts
        "legs_complete": sum(1 for n in tp_legs.values() if n >= 50),  # X of 3
        "target_legs": 3,
        "per_leg_target": 50,
    }

    # WAC — Worked All Continents (6). Continent derived from the canonical
    # entity via cty.dat.
    wac = {
        "worked": len(_worked.worked_continents),
        "confirmed": len(_worked.confirmed_continents),
        "target": 6,
        "continents": sorted(_worked.confirmed_continents),
    }

    # Contacts by year × mode-class — for the Scores bar chart. Pure-stdlib walk
    # over the deduped QSO list; no new deps and no external charting/Python
    # packages (rendered client-side as a vanilla CSS bar). mode_class() is our
    # own GT2-ported classifier, already imported.
    _ym: dict[str, dict[str, int]] = {}
    for q in _worked.qsos:
        d = q.get("qso_date") or ""
        if len(d) < 4 or not d[:4].isdigit():
            continue
        cls = mode_class(q.get("mode") or "")
        if cls not in ("CW", "Phone", "Digital"):
            cls = "Other"
        _ym.setdefault(d[:4], {"CW": 0, "Phone": 0, "Digital": 0, "Other": 0})[cls] += 1
    qso_by_year_mode = {yr: _ym[yr] for yr in sorted(_ym)}

    # Last-5 DXCC ATNOs (All-Time-New-Ones): the 5 most-recently first-worked
    # DXCC entities. Keyed by DXCC id; the ATNO QSO = earliest contact with that
    # entity in the log. Recent ATNOs are reliable (their QSO is in the log);
    # deep-past ATNO dates depend on log completeness. Pure stdlib.
    _first_dxcc: dict = {}
    for q in _worked.qsos:
        d = q.get("dxcc")
        if not d:
            continue
        k = (q.get("qso_date") or "", q.get("time_on") or "")
        cur = _first_dxcc.get(d)
        if cur is None or k < cur[0]:
            _first_dxcc[d] = (k, q)
    last5_atno = [
        {"date": r.get("qso_date", ""), "country": r.get("country", ""),
         "call": r.get("call", ""), "band": r.get("band", ""), "mode": r.get("mode", "")}
        for (_k, r) in sorted(_first_dxcc.values(), key=lambda x: x[0], reverse=True)[:5]
    ]

    # Most Wanted progress — every ranked entity, worked/confirmed per band + mode,
    # from the number-keyed worked-state (your complete QRZ + LoTW history). Rank
    # order, rarest first. Pairs with the Club Log rarity list already loaded.
    _wb = _worked.worked_dxcc_band; _cb = _worked.confirmed_dxcc_band
    _wm = _worked.worked_dxcc_modeclass; _cm = _worked.confirmed_dxcc_modeclass
    _sc = getattr(_worked, "slot_calls", {})  # (dxcc,band) -> {calls worked there}
    def _slot(w, c):
        return "confirmed" if c else ("worked" if w else "new")
    rare_progress = []
    oqrs_claimable = []   # rares with a matched, not-yet-confirmed slot (do OQRS)
    oqrs_suspect = []     # rares logged on a band that is NOT in the DX's log (verify)
    for _num, _rank in sorted(_DXCC_RARITY.items(), key=lambda kv: kv[1]):
        bands = {}
        claim, suspect = [], []
        for b in DXCC_CHALLENGE_BANDS:
            if (_num, b) in _cb:
                bands[b] = "confirmed"
            elif (_num, b) in _wb:
                here = _sc.get((_num, b), set())  # calls you actually worked on this slot
                if (_num, b) in _MATCHED_SLOTS:
                    bands[b] = "claimable"; claim.append({"band": b, "call": _MATCHED_SLOTS[(_num, b)]})
                elif here & _MATCHED_CALLS:
                    # The exact station you logged here is in Club Log's match set on
                    # some other band, but not this slot -> genuine "they don't have me
                    # on this band." A different op of the same entity does NOT count.
                    bands[b] = "suspect"
                    suspect.append({"band": b, "calls": sorted(here & _MATCHED_CALLS)})
                else:
                    bands[b] = "worked"
            else:
                bands[b] = "new"
        modes = {m: _slot((_num, m) in _wm, (_num, m) in _cm) for m in ("CW", "Phone", "Digital")}
        nm = _DXCC_NAME.get(_num, _num)
        rare_progress.append({
            "rank": _rank, "dxcc": _num, "name": nm, "bands": bands, "modes": modes,
            "worked": any(s != "new" for s in bands.values()),
            "confirmed": any(s == "confirmed" for s in bands.values()),
        })
        if claim:
            oqrs_claimable.append({"rank": _rank, "dxcc": _num, "name": nm, "slots": claim})
        if suspect:
            oqrs_suspect.append({"rank": _rank, "dxcc": _num, "name": nm, "slots": suspect})

    # M0OXO OQRS — entities whose M0OXO-managed logs hold your call (separate
    # confirmation channel from Club Log). Entity-level only; new-entity hits
    # (worked but confirmed nowhere) float to the top — those are the prizes.
    m0oxo = []
    if _cty and _M0OXO_CALLS:
        groups: dict = {}
        for c in _M0OXO_CALLS:
            e = _cty.lookup(c)
            if not e:
                continue
            num = str(e.dxcc)
            groups.setdefault(num, {"name": _DXCC_NAME.get(num, e.entity), "calls": []})["calls"].append(c)
        for num, g in groups.items():
            conf_ent = num in _worked.confirmed_dxcc
            open_slots = sorted({b for (d, b) in _wb if d == num} - {b for (d, b) in _cb if d == num})
            # new = entity unconfirmed (the real prize); partial = entity confirmed
            # but some worked bands aren't; done = fully confirmed (no action). Being
            # in an M0OXO log says nothing about needing M0OXO — most go to LoTW free.
            status = "new" if not conf_ent else ("partial" if open_slots else "done")
            m0oxo.append({
                "dxcc": num, "name": g["name"],
                "rank": _DXCC_RARITY.get(num, 9999),
                "calls": sorted(g["calls"]),
                "new_entity": not conf_ent, "status": status, "open_slots": open_slots,
            })
        _ord = {"new": 0, "partial": 1, "done": 2}
        m0oxo.sort(key=lambda x: (_ord[x["status"]], x["rank"]))

    return {
        "as_of": time.time(),
        "qso_by_year_mode": qso_by_year_mode,
        "last5_atno": last5_atno,
        "rare_progress": rare_progress,
        "oqrs_claimable": oqrs_claimable,
        "oqrs_suspect": oqrs_suspect,
        "m0oxo": m0oxo,
        "totals": {
            "qsos": _worked.qso_count,
            "unique_calls": _worked.unique_calls_count,
            "confirmed_qsos": _worked.confirmed_qso_count,
        },
        "dxcc": dxcc,
        "challenge": {
            "worked": challenge_worked,
            "confirmed": challenge_confirmed,
            "bands": list(DXCC_CHALLENGE_BANDS),
        },
        "was": {
            "worked": len(_worked.worked_states),
            "confirmed": len(_worked.confirmed_states),
            "target": WAS_TARGET,
            "by_band": was_by_band,
            "by_band_detail": was_by_band_detail,
        },
        "waja": {
            "worked": len(_worked.worked_prefectures),
            "confirmed": len(_worked.confirmed_prefectures),
            "target": WAJA_TARGET,
            "worked_codes": sorted(_worked.worked_prefectures),
            "confirmed_codes": sorted(_worked.confirmed_prefectures),
        },
        "waz": {
            "worked": len(_worked.worked_cq_zones),
            "confirmed": len(_worked.confirmed_cq_zones),
            "target": WAZ_TARGET,
        },
        "wpx": {
            "worked": len(wpx_worked),
            "confirmed": len(wpx_confirmed),
            "target": WPX_TARGET,
        },
        "wae": {
            "worked": len(wae_worked),
            "confirmed": len(wae_confirmed),
            "target": wae_target,
            "by_band_detail": wae_by_band_detail,
            "missing": wae_missing,      # WAE countries not yet confirmed (Mixed)
            "unworked": wae_unworked,    # not even worked — the true needs
        },
        "vucc": vucc,
        "vucc_satellite": {
            "worked": len(_worked.worked_satellite_grids),
            "confirmed": len(_worked.confirmed_satellite_grids),
            "target": 100,
        },
        "ffma": {
            "worked": len(ffma_worked_set),
            "confirmed": len(ffma_confirmed_set),
            "target": ffma_target,
        },
        "ffma_chase": ffma_chase,
        "grid_discrepancies": _build_grid_discrepancies(),
        "five_band_dxcc": {
            "by_band": five_dxcc_by_band,
            "bands_complete": five_dxcc_complete,
            "target_bands": len(FIVE_BAND_BANDS),
            "per_band_target": 100,
        },
        "nb_dxcc": {
            "level": nb_dxcc_level,          # 8 -> "8BDXCC"; 0 until 5BDXCC base earned
            "has_base": nb_dxcc_has_base,
            "bands": nb_dxcc_bands,
            "short": nb_dxcc_short,          # {160m: 70, 6m: 28} — path to next level
        },
        "five_band_was": {
            "by_band": five_was_by_band,
            "bands_complete": five_was_complete,
            "target_bands": len(FIVE_BAND_BANDS),
            "per_band_target": 50,
        },
        "honor_roll": {
            "confirmed": honor_confirmed,
            "honor_roll_at": DXCC_HONOR_ROLL_TOTAL - 9,
            "number_one_at": DXCC_HONOR_ROLL_TOTAL,
        },
        "was_by_mode": was_by_mode,
        "triple_play": triple_play,
        "wac": wac,
        "dxcc_by_band": dxcc_by_band,
    }


# Bumped every time worked-state-derived fields are re-evaluated (a QSO logged,
# LoTW/QRZ pull, N1MM/SDC mutation). Surfaced in /spots.json so the 5s spot poll
# can tell the scorecard to re-fetch /api/scores immediately — keeps the FFMA /
# award panels in lockstep with the liveview instead of lagging the 5-min timer.
_WORKED_REV = 0


def _refresh_cache_worked_status():
    """Re-evaluate worked-state-derived fields on every cached spot. Called
    after a fresh QSO is logged so the spot panel reflects the new worked /
    confirmed status immediately, not after the QRZ → ADIF → reload roundtrip.
    Cheap: at most MAX_SPOTS=5000 entries, each lookup is O(1)."""
    if not _worked:
        return
    global _WORKED_REV
    _WORKED_REV += 1
    with _lock:
        for s in _cache.values():
            s["call_status"] = _worked.call_band_status(s["dx_call"], s["band"])
            country = s.get("country", "")
            band = s["band"]
            mode = s.get("mode") or ""
            modeclass = s.get("modeclass") or (mode_class(mode) if mode else "")
            if country:
                s["dxcc_band_status"] = _worked.entity_band_status(s.get("dxcc", ""), country, band)
                if mode:
                    s["dxcc_band_mode_status"] = _worked.country_band_mode_status(country, band, mode)
                if modeclass:
                    s["dxcc_band_modeclass_status"] = _worked.country_band_modeclass_status(country, band, modeclass)
                    # entity-level (any band) — the ARRL mode DXCC grain
                    s["dxcc_modeclass_status"] = _worked.country_modeclass_status(country, modeclass)
            grid = s.get("grid", "")
            if grid:
                s["grid_band_status"] = _worked.grid_band_status(grid, band)
            if s.get("waja_pref"):
                s["waja_status"] = _worked.prefecture_status(s["waja_pref"])
            if _cty:
                s["marathon"] = _marathon_status(dxmarathon.entity_id(s["dx_call"], _cty),
                                                 dxmarathon.zone(s["dx_call"], _cty))
                s["wae_status"], s["wae_name"] = _wae_spot_status(s["dx_call"])


def _build_adif_record(parsed: dict, country: str = "", dxcc: str = "", band: str = "") -> str:
    """Build a single ADIF record string from a parsed QSO Logged message.
    Returns the record text (one line, ending with `<EOR>`). No newline.
    Used both for appending to the local log file AND for uploading to QRZ /
    ClubLog / eQSL — keeps a single source of truth for the field set."""
    fields = []
    def add(tag: str, val):
        if val is None:
            return
        s = str(val).strip()
        if not s:
            return
        fields.append(f"<{tag}:{len(s)}>{s}")

    add("CALL", parsed.get("dx_call"))
    add("BAND", band)
    add("MODE", parsed.get("mode"))
    freq_hz = parsed.get("freq_hz", 0) or 0
    if freq_hz:
        add("FREQ", f"{freq_hz/1e6:.6f}")
    add("QSO_DATE", parsed.get("date_on"))
    add("TIME_ON", parsed.get("time_on"))
    add("QSO_DATE_OFF", parsed.get("date_off"))
    add("TIME_OFF", parsed.get("time_off"))
    add("RST_SENT", parsed.get("report_sent"))
    add("RST_RCVD", parsed.get("report_rcvd"))
    add("TX_PWR", parsed.get("tx_power"))
    add("STATION_CALLSIGN", parsed.get("my_call"))
    add("OPERATOR", parsed.get("operator_call"))
    add("MY_GRIDSQUARE", parsed.get("my_grid"))
    add("GRIDSQUARE", parsed.get("dx_grid"))
    add("NAME", parsed.get("name"))
    add("COMMENT", parsed.get("comments"))
    if country:
        add("COUNTRY", country)
    if dxcc:
        add("DXCC", dxcc)
    add("PROP_MODE", parsed.get("adif_prop_mode"))
    add("APP_N1MM_ID", parsed.get("app_n1mm_id"))

    return " ".join(fields) + " <EOR>"


def _append_to_qso_log_adif(adif_record: str):
    """Append the given ADIF record to the local log file. Creates the file
    with an ADIF header on first write."""
    try:
        if not QSO_LOG_PATH.exists():
            with open(QSO_LOG_PATH, "w") as f:
                f.write("Grayline real-time WSJT-X QSO log\n")
                f.write("<ADIF_VER:5>3.1.4 <PROGRAMID:8>Grayline <EOH>\n")
        with open(QSO_LOG_PATH, "a") as f:
            f.write(adif_record + "\n")
    except Exception as e:
        log.warning("Failed to append to QSO log %s: %s", QSO_LOG_PATH, e)


def _adif_field(record: str, tag: str) -> str:
    """Extract one field's value from a single ADIF record line. '' if absent."""
    m = re.search(rf"<{re.escape(tag)}:(\d+)(?::[^>]*)?>", record, re.I)
    if not m:
        return ""
    start = m.end()
    return record[start:start + int(m.group(1))]


def _remove_qso_from_adif(n1mm_id: str) -> bool:
    """Rewrite qso_logged.adi without the record whose APP_N1MM_ID == n1mm_id.

    ADIF is RECORD-delimited by <EOR>, NOT line-oriented — multiple records can
    share a physical line. So we split on the header (<EOH>) and then on <EOR>
    and operate on whole records, never whole lines (a line-based delete could
    take an unrelated record that happens to share the line). The rewrite also
    re-normalizes the body to one record per line so concatenation can't recur.
    Atomic (temp file + rename). Returns True if a record was removed."""
    if not n1mm_id or not QSO_LOG_PATH.exists():
        return False
    try:
        text = QSO_LOG_PATH.read_text()
    except Exception as e:
        log.warning("ADIF edit: failed reading %s: %s", QSO_LOG_PATH, e)
        return False
    m = re.search(r"<EOH>", text, re.I)
    header, body = (text[:m.end()], text[m.end():]) if m else ("", text)
    kept, removed = [], 0
    # re.split keeps everything between <EOR> delimiters; the final element is
    # the trailing remainder (whitespace) after the last record — dropped.
    chunks = re.split(r"(?i)<EOR>", body)
    for chunk in chunks[:-1]:
        record = chunk.strip()
        if not record:
            continue
        full = record + " <EOR>"
        if _adif_field(full, "APP_N1MM_ID") == n1mm_id:
            removed += 1
            continue
        kept.append(full)
    if not removed:
        return False
    try:
        out = header.rstrip("\n") + "\n" + ("\n".join(kept) + "\n" if kept else "")
        tmp = QSO_LOG_PATH.with_suffix(".adi.tmp")
        tmp.write_text(out)
        tmp.replace(QSO_LOG_PATH)
    except Exception as e:
        log.warning("ADIF edit: failed rewriting %s: %s", QSO_LOG_PATH, e)
        return False
    return True


def _grid_from_spot_cache(dx_call: str) -> str:
    """Most-recent decoded grid for a call from the live spot cache, preferring
    our OWN local decode (a *-LOCAL source). Used to backfill a QSO Logged that
    arrived without a grid: the decode is the only CORRECT grid for portables
    (e.g. KH6XX/W0), where a QRZ lookup would wrongly return the home-call grid
    (KH6XX's Hawaii BL11) instead of the grid actually transmitted (EN08)."""
    if not dx_call:
        return ""
    target = dx_call.strip().upper()
    best, best_local = "", False
    with _lock:
        for rec in _cache.values():
            if (rec.get("dx_call", "") or "").upper() != target:
                continue
            g = (rec.get("grid", "") or "").strip()
            if not g:
                continue
            is_local = (rec.get("source", "") or "").endswith("-LOCAL")
            if is_local:
                return g            # our own decode wins outright
            if not best:
                best = g
    return best


def _ingest_wsjtx_qso_logged(parsed: dict):
    """Handle WSJT-X QSO Logged (type 5): push QSO into worked-state in-memory,
    append to local ADIF, refresh cache statuses so the UI flips pills from
    'new' to 'worked' immediately on next refresh."""
    dx_call = (parsed.get("dx_call") or "").strip()
    dx_grid = (parsed.get("dx_grid") or "").strip()
    mode = (parsed.get("mode") or "").strip().upper()
    freq_hz = parsed.get("freq_hz", 0) or 0
    freq_khz = freq_hz / 1000.0 if freq_hz else 0
    band = dxcluster.freq_to_band(freq_khz) if freq_khz else ""

    if not dx_call:
        log.warning("QSO Logged with empty dx_call — ignoring")
        return

    # Backfill a missing DX grid from our own live decode cache — NEVER from QRZ.
    # WSJT-X sometimes logs without the grid (it wasn't captured during the
    # exchange); for a portable (/W0 etc.) QRZ would return the WRONG grid (the
    # home-call location), so the decode we copied is the only correct source.
    # This protects grid-based awards (FFMA / VUCC) on portable contacts.
    if not dx_grid:
        cached = _grid_from_spot_cache(dx_call)
        if cached:
            dx_grid = cached
            parsed["dx_grid"] = cached   # so the ADIF record carries it too
            log.info("QSO Logged %s: no grid from WSJT-X — backfilled %r from local decode",
                     dx_call, cached)

    # cty.dat enrichment for country / DXCC entity
    country = ""
    dxcc = ""
    if _cty:
        e = _cty.lookup(dx_call)
        if e:
            country = e.entity or ""
            dxcc_val = getattr(e, "dxcc", None) or getattr(e, "dxcc_id", None)
            if dxcc_val:
                dxcc = str(dxcc_val)

    # Push to in-memory worked-state
    if _worked:
        _worked.record_qso(call=dx_call, country=country, dxcc=dxcc,
                           band=band, mode=mode, grid=dx_grid)

    # Build the ADIF record once, use for both local-log append and uploads
    adif_record = _build_adif_record(parsed, country=country, dxcc=dxcc, band=band)
    _append_to_qso_log_adif(adif_record)

    # Refresh worked-state-derived fields on every cached spot so award pills
    # update immediately on the next /spots.json poll (5s default)
    _refresh_cache_worked_status()

    # Fire parallel uploads to QRZ / ClubLog / eQSL. Background thread,
    # fire-and-forget, never blocks the WSJT-X UDP handler. Results land
    # in qso_uploads.log (alongside the app) per service per QSO.
    if LOGBOOK_UPLOAD_ENABLED:
        logbook_uploads.upload_qso_to_all(adif_record, dx_call=dx_call)
    else:
        log.info("QSO %s: logbook uploads DISABLED (LOGBOOK_UPLOAD_ENABLED=False)", dx_call)

    log.info("QSO Logged: %s on %s %s (country=%r grid=%r) — ADIF appended, "
             "worked-state updated, uploads dispatched",
             dx_call, band or '?', mode, country, dx_grid)


def _forward_wsjtx(data: bytes):
    """Mirror a raw WSJT-X UDP datagram to WSJTX_FORWARD_TARGETS (e.g. GridTracker
    on the workstation). Sent FROM the listener socket (bound to :2237) so a
    downstream consumer sees the decodes arriving from :2237 and sends its replies
    (click-to-tune) BACK to :2237 — where the relay path catches them. Sending
    from a throwaway ephemeral socket would make the consumer reply to that
    ephemeral port, which nothing reads, so click-to-tune would be lost."""
    if not WSJTX_FORWARD_TARGETS or _wsjtx_transport is None:
        return
    try:
        for tgt in WSJTX_FORWARD_TARGETS:
            _wsjtx_transport.sendto(data, tgt)
    except Exception as e:
        log.debug("WSJT-X forward failed: %s", e)


# WSJT-X message types a CLIENT sends TO WSJT-X (never sent *by* it): Reply(4),
# HaltTx(8), FreeText(9), Location(11), HighlightCallsign(13),
# SwitchConfiguration(14), Configure(15). Receiving one on our listen port means
# it came from a downstream consumer we forward decodes to (e.g. GridTracker) and
# is headed UPSTREAM — relaying it to WSJT-X is what makes that consumer's
# click-to-tune (Reply) work while Grayline is the WSJT-X receiver. Identified by
# type/direction, not source address, so it's robust even if WSJT-X and the
# consumer share a host.
_WSJTX_CLIENT_MSG_TYPES = frozenset({4, 8, 9, 11, 13, 14, 15})


def _wsjtx_peek_type(data: bytes):
    """Message type of a raw WSJT-X datagram (magic + schema + type@offset 8),
    or None if it doesn't look like one."""
    if len(data) >= 12 and data[:4] == b"\xad\xbc\xcb\xda":
        return int.from_bytes(data[8:12], "big")
    return None


def _relay_to_wsjtx(data: bytes):
    """Relay a client→WSJT-X control packet (e.g. a GridTracker click-to-tune
    Reply) to every known WSJT-X instance. The instance whose Id matches acts on
    it; the others ignore it — so broadcasting is safe and needs no Id parsing.
    Makes Grayline a bidirectional WSJT-X UDP hub: WSJT-X ⇄ Grayline ⇄ {GT, ...}."""
    global _wsjtx_fwd_sock
    with _wsjtx_state_lock:
        addrs = {s["source_addr"] for s in _wsjtx_state.values() if s.get("source_addr")}
    if not addrs:
        return
    try:
        if _wsjtx_fwd_sock is None:
            _wsjtx_fwd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for a in addrs:
            _wsjtx_fwd_sock.sendto(data, a)
    except Exception as e:
        log.debug("WSJT-X reply relay failed: %s", e)


def _wsjtx_handle_datagram(data: bytes, addr: tuple):
    """Top-level WSJT-X UDP message dispatcher. Called from the asyncio
    DatagramProtocol on each received packet. Errors are caught here so
    one malformed packet can't kill the listener task."""
    # A client→WSJT-X control packet (click-to-tune Reply, Halt, etc.) from a
    # downstream consumer we forward to is headed upstream — relay it to WSJT-X
    # and stop. Don't mirror it back out (would echo to the consumer) or try to
    # parse it as a decode.
    _mt = _wsjtx_peek_type(data)
    if _mt in _WSJTX_CLIENT_MSG_TYPES:
        log.debug("relaying downstream WSJT-X control packet (type %s) from %s upstream", _mt, addr)
        _relay_to_wsjtx(data)
        return
    _forward_wsjtx(data)   # mirror raw datagram to GridTracker / other consumers
    try:
        from wsjtx_udp import (parse_message, MSG_HEARTBEAT, MSG_STATUS,
                               MSG_DECODE, MSG_QSO_LOGGED)
        msg_type, parsed = parse_message(data)
        if msg_type is None or parsed is None:
            return
        if msg_type == MSG_STATUS:
            _wsjtx_state_update(parsed["client_id"], parsed, addr)
        elif msg_type == MSG_DECODE:
            _ingest_wsjtx_decode(parsed, addr)
        elif msg_type == MSG_QSO_LOGGED:
            _ingest_wsjtx_qso_logged(parsed)
        # Heartbeat: no-op (the source addr is already captured via Status).
    except Exception as e:
        log.warning("WSJT-X handler error: %s (data=%d bytes)", e, len(data))


class _WsjtxProtocol(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        global _wsjtx_transport
        _wsjtx_transport = transport
        sock = transport.get_extra_info("sockname")
        log.info("WSJT-X UDP listener up on %s:%d", sock[0], sock[1])

    def datagram_received(self, data, addr):
        _wsjtx_handle_datagram(data, addr)

    def error_received(self, exc):
        log.warning("WSJT-X UDP error: %s", exc)

    def connection_lost(self, exc):
        global _wsjtx_transport
        _wsjtx_transport = None
        log.info("WSJT-X UDP listener closed (%s)", exc)


async def wsjtx_listener_task():
    """Bind UDP socket on WSJTX_LISTEN_HOST:WSJTX_LISTEN_PORT and process
    incoming Heartbeat/Status/Decode messages from any WSJT-X instance
    configured to send broadcasts here. Runs for the process lifetime."""
    loop = asyncio.get_running_loop()
    transport, _protocol = await loop.create_datagram_endpoint(
        _WsjtxProtocol,
        local_addr=(WSJTX_LISTEN_HOST, WSJTX_LISTEN_PORT))
    try:
        while True:
            await asyncio.sleep(3600)  # task body — protocol handles datagrams
    except asyncio.CancelledError:
        transport.close()
        raise


# ---------------- N1MM / SDC-Connectors QSO logging ----------------
def _n1mm_contactinfo_to_parsed(root) -> dict | None:
    """Map an N1MM <contactinfo> XML element to the parsed-QSO dict that
    _ingest_wsjtx_qso_logged() consumes, so N1MM/SDC contest logging reuses the
    exact same ADIF-append + worked-state + upload pipeline as WSJT-X. Returns
    None if there's no callsign to log."""
    def t(tag: str) -> str:
        return (root.findtext(tag, "") or "").strip()

    dx_call = t("call")
    if not dx_call:
        return None

    # N1MM rxfreq is in tens of Hz (e.g. 1407400 -> 14074000 Hz). Same unit
    # GTBridge decodes from SDC-Connectors.
    try:
        freq_hz = int(t("rxfreq") or "0") * 10
    except ValueError:
        freq_hz = 0

    # timestamp "YYYY-MM-DD HH:MM:SS" -> ADIF QSO_DATE / TIME_ON (and OFF).
    date_s = time_s = ""
    ts = t("timestamp")
    if ts:
        try:
            dp, tp = ts.split()
            date_s = dp.replace("-", "")
            time_s = tp.replace(":", "")
        except ValueError:
            pass

    # Keep the contest exchange in the local log so the ADIF isn't lossy
    # (award status doesn't need it, but the operator's log should have it).
    snt_nr, rcv_nr = t("sntnr"), t("rcvnr")
    exch = " ".join(p for p in (f"snt {snt_nr}" if snt_nr else "",
                                f"rcv {rcv_nr}" if rcv_nr else "") if p)
    comment = (t("comment") + (" " + exch if exch else "")).strip()

    return {
        "dx_call": dx_call,
        "dx_grid": t("gridsquare"),
        "mode": t("mode").upper(),
        "freq_hz": freq_hz,
        "date_on": date_s, "time_on": time_s,
        "date_off": date_s, "time_off": time_s,
        "report_sent": t("snt"), "report_rcvd": t("rcv"),
        "tx_power": t("power"),
        "my_call": t("mycall") or CALLSIGN,
        "operator_call": t("operator"),
        "my_grid": HOME_GRID,
        "name": t("name"),
        "comments": comment or None,
        # N1MM's per-QSO GUID. Stored in the ADIF (APP_N1MM_ID) so a later
        # <contactdelete>/<contactreplace> can find and remove/replace this exact
        # record. contactreplace carries the same field set, so this parser
        # serves all three message types.
        "app_n1mm_id": t("ID"),
    }


def _n1mm_apply_mutation_reload():
    """After an in-process ADIF edit (delete/replace), recompute worked-state
    from scratch (force_reload, not incremental — so removals are reflected) and
    refresh the cached spot pills so the award badges revert/update live."""
    if _worked:
        _worked.force_reload()
    _refresh_cache_worked_status()


def _n1mm_delete(root):
    """Handle N1MM <contactdelete>: remove the matching QSO from the local ADIF
    (keyed on N1MM <ID>) and recompute worked-state so award pills revert.
    Does NOT delete from remote logbooks (QRZ/ClubLog/eQSL) — those can't be
    un-uploaded automatically; if real-time uploads were on, fix them by hand."""
    n1mm_id = (root.findtext("ID", "") or "").strip()
    call = (root.findtext("call", "") or "").strip() or "?"
    if not n1mm_id:
        log.warning("N1MM contactdelete for %s had no <ID> — cannot match; ignoring", call)
        return
    if _remove_qso_from_adif(n1mm_id):
        _n1mm_apply_mutation_reload()
        log.info("N1MM contactdelete: removed %s (ID=%s) from ADIF, worked-state recomputed",
                 call, n1mm_id)
    else:
        log.info("N1MM contactdelete: no local ADIF record matched ID=%s (%s) — nothing to do",
                 n1mm_id, call)


def _n1mm_replace(root):
    """Handle N1MM <contactreplace> (an edited QSO): drop the stale ADIF record
    (matched by N1MM <ID>), append the corrected one, then recompute worked-state.
    Like delete, does NOT re-push to remote logbooks."""
    parsed = _n1mm_contactinfo_to_parsed(root)  # contactreplace carries the full contact fields
    if not parsed:
        return
    n1mm_id = parsed.get("app_n1mm_id") or ""
    if n1mm_id:
        _remove_qso_from_adif(n1mm_id)  # drop the stale version (no-op if not present)

    # Enrich + append the corrected record, mirroring the contactinfo ingest path
    country = dxcc = ""
    if _cty:
        e = _cty.lookup(parsed.get("dx_call", ""))
        if e:
            country = e.entity or ""
            dxcc_val = getattr(e, "dxcc", None) or getattr(e, "dxcc_id", None)
            if dxcc_val:
                dxcc = str(dxcc_val)
    fh = parsed.get("freq_hz", 0) or 0
    band = dxcluster.freq_to_band(fh / 1000.0) if fh else ""
    _append_to_qso_log_adif(_build_adif_record(parsed, country=country, dxcc=dxcc, band=band))
    _n1mm_apply_mutation_reload()
    log.info("N1MM contactreplace: updated %s (ID=%s), worked-state recomputed",
             parsed.get("dx_call", "?"), n1mm_id or "?")


def _n1mm_handle_datagram(data: bytes, addr: tuple):
    """Parse one N1MM UDP datagram and dispatch by root tag. N1MM/SDC multiplex
    several message types on this port; we act on the three QSO-lifecycle ones
    (contactinfo / contactreplace / contactdelete) and ignore RadioInfo,
    dynamicresults (score), spot, lookupinfo, etc."""
    try:
        root = ET.fromstring(data.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return
    tag = root.tag
    if tag == "contactinfo":
        parsed = _n1mm_contactinfo_to_parsed(root)
        if parsed:
            log.info("N1MM contactinfo from %s: %s — ingesting",
                     addr[0] if addr else "?", parsed["dx_call"])
            _ingest_wsjtx_qso_logged(parsed)
    elif tag == "contactreplace":
        _n1mm_replace(root)
    elif tag == "contactdelete":
        _n1mm_delete(root)
    # else: RadioInfo / dynamicresults / spot / lookupinfo — silently ignored


class _N1mmProtocol(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        sock = transport.get_extra_info("sockname")
        log.info("N1MM UDP listener up on %s:%d", sock[0], sock[1])

    def datagram_received(self, data, addr):
        try:
            _n1mm_handle_datagram(data, addr)
        except Exception as e:
            log.warning("N1MM handler error: %s (data=%d bytes)", e, len(data))

    def error_received(self, exc):
        log.warning("N1MM UDP error: %s", exc)


async def n1mm_listener_task():
    """Bind UDP N1MM_LISTEN_HOST:N1MM_LISTEN_PORT and ingest <contactinfo> QSO
    broadcasts from N1MM Logger+ / SDC-Connectors. Runs for process lifetime."""
    loop = asyncio.get_running_loop()
    transport, _protocol = await loop.create_datagram_endpoint(
        _N1mmProtocol,
        local_addr=(N1MM_LISTEN_HOST, N1MM_LISTEN_PORT))
    try:
        while True:
            await asyncio.sleep(3600)  # task body — protocol handles datagrams
    except asyncio.CancelledError:
        transport.close()
        raise


def active_bands_snapshot() -> dict:
    """Return current Flex slice state as {bands: [list], slices: {n: {freq_mhz, mode, band}}}.
    Empty if Flex is disconnected or disabled."""
    if not _flex or not _flex.connected:
        return {"connected": False, "bands": [], "slices": {}}
    bands_set = set()
    slice_info = {}
    for sn, info in _flex.slices.items():
        if info.get("in_use") != "1":
            continue
        try:
            freq_mhz = float(info.get("RF_frequency", "0"))
        except ValueError:
            continue
        freq_khz = freq_mhz * 1000
        band = dxcluster.freq_to_band(freq_khz)
        if not band:
            continue
        bands_set.add(band)
        slice_info[str(sn)] = {
            "freq_mhz": freq_mhz,
            "freq_khz": freq_khz,
            "mode": info.get("mode", ""),
            "band": band,
            "index_letter": info.get("index_letter", ""),
        }
    # Sort bands per BAND_ORDER for stable display
    bands_sorted = sorted(bands_set, key=lambda b: BAND_ORDER.index(b) if b in BAND_ORDER else 99)
    return {"connected": True, "bands": bands_sorted, "slices": slice_info}


def spotter_distance_mi(spotter: str) -> int | None:
    """Distance from HOME_GRID to spotter's QRZ-cached grid. None if unknown.
    Side effect: enqueues active QRZ lookup if spotter is unknown."""
    if not HOME_LATLON:
        return None
    base = normalize_spotter(spotter)
    if not base:
        return None
    with _qrz_cache_lock:
        grid = _qrz_cache.get(base)
    if not grid:
        _enqueue_lookup(base)
        return None
    spotter_ll = maidenhead_to_latlon(grid)
    if not spotter_ll:
        return None
    return round(haversine_miles(HOME_LATLON[0], HOME_LATLON[1],
                                 spotter_ll[0], spotter_ll[1]))


def maidenhead_to_latlon(grid):
    """Convert a 4- or 6-char Maidenhead grid to (lat, lon) at the *center* of the square.
    Returns None if the grid is invalid or too short."""
    if not grid:
        return None
    g = grid.strip()
    if len(g) < 4:
        return None
    try:
        F0, F1 = g[0].upper(), g[1].upper()
        if not ("A" <= F0 <= "R") or not ("A" <= F1 <= "R"):
            return None
        lon = (ord(F0) - ord("A")) * 20 - 180
        lat = (ord(F1) - ord("A")) * 10 - 90
        lon += int(g[2]) * 2
        lat += int(g[3]) * 1
        if len(g) >= 6:
            S0, S1 = g[4].lower(), g[5].lower()
            if not ("a" <= S0 <= "x") or not ("a" <= S1 <= "x"):
                # fall through to 4-char center
                lon += 1
                lat += 0.5
            else:
                lon += (ord(S0) - ord("a")) * (5 / 60)
                lat += (ord(S1) - ord("a")) * (2.5 / 60)
                # center of subsquare
                lon += 2.5 / 60
                lat += 1.25 / 60
        else:
            lon += 1
            lat += 0.5
        return (lat, lon)
    except Exception:
        return None


def haversine_miles(lat1, lon1, lat2, lon2):
    R_MI = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * R_MI * math.asin(math.sqrt(a))


HOME_LATLON = maidenhead_to_latlon(HOME_GRID)


def _nearby_grid_squares(home_latlon, home_grid, radius_mi):
    """The 4-char Maidenhead squares whose center is within radius+slop of home —
    peer_copies' cheap pre-filter set (a square spans ~70mi, so +70 slop keeps
    edge receivers). Bounded to the 3x3 fields around home so it's fast at startup."""
    if not home_latlon or len(home_grid) < 2:
        return set()
    f0, f1 = home_grid[0].upper(), home_grid[1].upper()
    fields0 = [chr(o) for o in (ord(f0) - 1, ord(f0), ord(f0) + 1) if ord('A') <= o <= ord('R')]
    fields1 = [chr(o) for o in (ord(f1) - 1, ord(f1), ord(f1) + 1) if ord('A') <= o <= ord('R')]
    out = set()
    for A in fields0:
        for B in fields1:
            for x in range(10):
                for y in range(10):
                    sq = f"{A}{B}{x}{y}"
                    c = maidenhead_to_latlon(sq)
                    if c and haversine_miles(home_latlon[0], home_latlon[1], c[0], c[1]) <= radius_mi + 70:
                        out.add(sq)
    return out


# ---------------- spot cache ----------------
_lock = threading.Lock()
_cache: "OrderedDict[tuple, dict]" = OrderedDict()


def _spot_dedup_key(band: str, mode: str, freq_khz: float, dx_call: str) -> tuple:
    """Cache dedup key. WSJT-X modes use a coarser key without frequency
    because FT8/FT4 audio offsets jitter cycle-to-cycle and across reporting
    sources but represent the same station — same call + band + mode is
    enough to identify the QSO opportunity. Other modes keep freq precision
    so a station moving frequency creates a new spot entry (operator wants
    to know where to listen)."""
    if mode and mode.upper() in WSJTX_MODES:
        return (band, mode, dx_call)
    return (band, mode, round(freq_khz, 1), dx_call)


def _telnet_feed_radius_mi(band: str) -> int:
    """Tiered local-spotter radius for the telnet feed, mirroring the browser's
    radiusForBand(): VHF+ bands use the tighter 150 mi gate, HF uses 300 mi."""
    return (TELNET_FEED_RADIUS_VHF_MI if band in TELNET_FEED_VHF_PLUS_BANDS
            else TELNET_FEED_RADIUS_HF_MI)


# Contest bands for the standalone Band Activity page (FD/HF set, 160m..2m).
CONTEST_BANDS = ["160m", "80m", "40m", "20m", "15m", "10m", "6m", "2m"]


def _band_activity_payload() -> dict:
    """Local-filtered spot counts per contest band x mode-class (CW/Phone/Digital)
    for the standalone Band Activity page. 'Local' = within the same tiered
    spotter radius as the roster's local filter, so the bars reflect *workable*
    activity — where the stations you can actually reach are, not the firehose.
    Deliberately dumb: just the numbers, no current-band awareness; you decide."""
    counts = {b: {"CW": 0, "Phone": 0, "Digital": 0} for b in CONTEST_BANDS}
    with _lock:
        spots = list(_cache.values())
    for s in spots:
        b = s.get("band")
        if b not in counts:
            continue
        d = s.get("distance_mi")
        if d is None or d > _telnet_feed_radius_mi(b):
            continue
        cls = mode_class(s.get("mode") or "")
        if cls in counts[b]:
            counts[b][cls] += 1
    return {"bands": CONTEST_BANDS, "counts": counts, "ts": time.time()}


# Standalone Band Activity page — deliberately separate from the main roster UI
# (a contest tool you pop open, not main-page clutter). Horizontal stacked bars,
# one per contest band, split CW/SSB/Digital, scaled to the hottest band. Polls
# /api/band_activity. Big/glanceable for a second monitor across the trailer.
_BAND_ACTIVITY_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Band Activity</title>
<style>
  :root { --cw:#e0a83c; --ssb:#4caf50; --dig:#3fc7e0; }
  * { box-sizing:border-box; }
  body { margin:0; background:#0a0a0a; color:#ddd; font-family:system-ui,Arial,sans-serif;
         font-variant-numeric:tabular-nums; padding:1.2em 1.4em; }
  h1 { margin:0 0 .1em; font-size:1.5em; letter-spacing:.06em; color:#fff; font-weight:700; }
  .sub { color:#777; font-size:.85em; margin-bottom:1em; }
  .legend { display:flex; gap:1.3em; margin-bottom:1.2em; font-size:.95em; }
  .legend span { display:inline-flex; align-items:center; gap:.4em; }
  .leg { cursor:pointer; user-select:none; padding:.15em .35em; border-radius:3px; }
  .leg:hover { background:#1a1a1a; }
  .leg.off { opacity:.32; text-decoration:line-through; }
  .sub2 { color:#666; font-size:.78em; margin:-.7em 0 1.2em; }
  .sw { width:1em; height:1em; border-radius:2px; display:inline-block; }
  .rows { display:flex; flex-direction:column; gap:.55em; }
  .row { display:grid; grid-template-columns:3.4em 1fr 3.2em; align-items:center; gap:.8em; }
  .band { font-weight:700; color:#aaa; text-align:right; font-size:1.15em; }
  .bar { height:2.1em; background:#141414; border-radius:4px; display:flex; overflow:hidden; }
  .seg { height:100%; display:flex; align-items:center; justify-content:center;
         font-size:.82em; font-weight:800; color:#0a0a0a; overflow:hidden; white-space:nowrap; min-width:0; }
  .seg.cw  { background:var(--cw); }
  .seg.ssb { background:var(--ssb); }
  .seg.dig { background:var(--dig); }
  .total { text-align:right; color:#888; font-size:1.15em; font-weight:700; }
  .row.hot .band { color:#fff; }
  .row.hot .total { color:#fff; }
  .empty { color:#444; font-size:.85em; padding-left:.3em; align-self:center; }
</style></head>
<body>
  <h1>BAND ACTIVITY</h1>
  <div class="sub" id="sub">local-filtered spots &middot; loading&hellip;</div>
  <div class="legend" id="legend">
    <span class="leg" data-mode="CW"><i class="sw" style="background:var(--cw)"></i>CW</span>
    <span class="leg" data-mode="Phone"><i class="sw" style="background:var(--ssb)"></i>SSB</span>
    <span class="leg" data-mode="Digital"><i class="sw" style="background:var(--dig)"></i>Digital</span>
  </div>
  <div class="sub2">click a mode to show/hide it &middot; bars rescale to the shown modes</div>
  <div class="rows" id="rows"></div>
<script>
const MODES = [["CW","cw","CW"],["Phone","ssb","SSB"],["Digital","dig","Digital"]];
const LS = "grayline_bands_modes";
function loadSel(){ try{ const r=localStorage.getItem(LS); if(r) return new Set(JSON.parse(r)); }catch(e){} return new Set(["CW","Phone","Digital"]); }
let sel = loadSel(), lastData = null;
function saveSel(){ try{ localStorage.setItem(LS, JSON.stringify([...sel])); }catch(e){} }
function syncLegend(){ document.querySelectorAll("#legend .leg").forEach(el => el.classList.toggle("off", !sel.has(el.dataset.mode))); }
function render(){
  if (!lastData) return;
  const j = lastData, counts = j.counts || {};
  const active = MODES.filter(m => sel.has(m[0]));
  const bandTotal = b => { const c = counts[b]||{}; return active.reduce((s,[k]) => s+(c[k]||0), 0); };
  let maxTotal = 1;
  for (const b of j.bands){ const t = bandTotal(b); if (t > maxTotal) maxTotal = t; }
  const html = j.bands.map(b => {
    const c = counts[b]||{}, t = bandTotal(b);
    const hot = (t === maxTotal && t > 0) ? " hot" : "";
    let inner;
    if (t === 0) { inner = '<span class="empty">—</span>'; }
    else {
      const frac = (t/maxTotal)*100;
      const segs = active.map(([k,cls,lbl]) => {
        const n = c[k]||0; if (!n) return "";
        return `<div class="seg ${cls}" style="width:${(n/t)*100}%" title="${b} ${lbl}: ${n}">${n}</div>`;
      }).join("");
      inner = `<div style="display:flex;width:${frac}%;height:100%">${segs}</div>`;
    }
    return `<div class="row${hot}"><div class="band">${b}</div><div class="bar">${inner}</div><div class="total">${t||""}</div></div>`;
  }).join("");
  document.getElementById("rows").innerHTML = html;
  const modeStr = [...sel].map(m => m==="Phone"?"SSB":m).join("/") || "no modes";
  document.getElementById("sub").textContent =
    `local-filtered spots · ${modeStr} · updated ` + new Date().toLocaleTimeString();
}
async function refresh(){
  try { lastData = await (await fetch("/api/band_activity",{cache:"no-store"})).json(); }
  catch(e) { document.getElementById("sub").textContent = "fetch failed — retrying…"; return; }
  render();
}
document.getElementById("legend").addEventListener("click", e => {
  const el = e.target.closest(".leg"); if (!el) return;
  const m = el.dataset.mode;
  sel.has(m) ? sel.delete(m) : sel.add(m);
  saveSel(); syncLegend(); render();
});
syncLegend();
refresh();
setInterval(refresh, 20000);
</script>
</body></html>"""


# ---- CQ DX Marathon (annual, worked-based: entities + CQ zones for the current
# UTC year, against the OFFICIAL 346/40 list via the WAE-aware resolver). Built
# from _worked.qsos so it stays in lockstep with the logbook; rebuilt on reload.
_MARATHON_ENT: set[str] = set()    # Combined-ADIF-Code entity ids worked this year
_MARATHON_ZONE: set[str] = set()   # CQ zones ("1".."40") worked this year
_MARATHON_YEAR: str = ""           # the UTC year these sets cover (for display)


def _rebuild_marathon():
    """Rebuild the current-year Marathon worked sets from the logbook. Auto-resets
    Jan 1 UTC because the year filter changes. Resolves each QSO's call through the
    WAE-aware dxmarathon resolver so the 6 WAE entities (Sicily, etc.) count right."""
    global _MARATHON_ENT, _MARATHON_ZONE, _MARATHON_YEAR
    if not (_worked and _cty):
        return
    year = time.strftime("%Y", time.gmtime())
    ents, zones = set(), set()
    for q in getattr(_worked, "qsos", []):
        if (q.get("qso_date", "") or "")[:4] != year:
            continue
        call = q.get("call", "")
        mid = dxmarathon.entity_id(call, _cty)
        if mid:
            ents.add(mid)
        z = dxmarathon.zone(call, _cty)
        if z:
            zones.add(z)
    _MARATHON_ENT, _MARATHON_ZONE, _MARATHON_YEAR = ents, zones, year


def _marathon_status(entity_id, zone):
    """Per-spot Marathon need: 'DXCC' | 'CQz' | 'DX+CQz' | None (nothing needed)."""
    need_e = entity_id is not None and entity_id not in _MARATHON_ENT
    need_z = zone is not None and zone not in _MARATHON_ZONE
    if need_e and need_z:
        return "DX+CQz"
    if need_e:
        return "DXCC"
    if need_z:
        return "CQz"
    return None


_WAE_WORKED: set = set()      # WAE country keys ever worked (lifetime)
_WAE_CONFIRMED: set = set()   # WAE country keys confirmed


def _rebuild_wae():
    """Rebuild lifetime WAE worked/confirmed country-key sets from the logbook,
    so the roster WAE pill stays in lockstep with the Scores-tab WAE standing.
    Resolves dxcc=False so the WAE split entities (Sicily, Shetland, ...) count."""
    global _WAE_WORKED, _WAE_CONFIRMED
    if not (_worked and _cty):
        return
    keys = _wae_entity_keys()
    worked, confirmed = set(), set()
    for q in getattr(_worked, "qsos", []):
        k = _wae_key(_cty.lookup(q.get("call", "") or "", dxcc=False))
        if k is None or k not in keys:
            continue
        worked.add(k)
        if ((q.get("lotw_qsl_rcvd") or "").upper() in ("Y", "V")
                or (q.get("qsl_rcvd") or "").upper() in ("Y", "V")
                or (q.get("eqsl_qsl_rcvd") or "").upper() in ("Y", "V")):
            confirmed.add(k)
    _WAE_WORKED, _WAE_CONFIRMED = worked, confirmed


def _wae_spot_status(call):
    """WAE status for a spotted call: ('new'|'worked'|'confirmed', country_name),
    or (None, None) if the call isn't a WAE country. dxcc=False so splits resolve."""
    if not _cty:
        return None, None
    k = _wae_key(_cty.lookup(call or "", dxcc=False))
    keys = _wae_entity_keys()
    if k is None or k not in keys:
        return None, None
    name = keys.get(k, "")
    if k in _WAE_CONFIRMED:
        return "confirmed", name
    if k in _WAE_WORKED:
        return "worked", name
    return "new", name


# ============== Pushover alerts (per-band × per-mode, award-aware) ==============
# Fire a Pushover push when a spot matters — gated by a per-band×per-mode matrix
# the operator configures AND the existing per-band award scope ("needed" = an
# unworked "new" DXCC band-slot or grid band-slot, so it respects the per-band
# scope automatically). Two flavors per enabled cell: "needed" (a wanted
# entity/grid) and "open" (any local activity = the band's alive). Geographic
# scope is MODE-AWARE: Es/tropo modes gate on local-spotter distance; meteor
# scatter (MSK144) gates on DX-within-meteor-range (~1300 mi) since an MS
# report's spotter location is irrelevant. Send is ported from GridTracker2
# (BSD-3-Clause) sendAlerts.js::sendPushOverAlert — credited in NOTICE.
ALERT_BANDS = ["160m", "80m", "40m", "30m", "20m", "17m", "15m", "12m", "10m",
               "6m", "2m", "1.25m", "70cm", "23cm", "3cm"]
ALERT_MODES = ["CW", "Phone", "Digital", "MSK144"]   # Q65 when GoCluster ingests it
_MS_RANGE_MI = 1300
_ALERTS_PATH = _BASE_DIR / "alerts.json"
_alert_cooldown: dict = {}   # cooldown key -> last-sent epoch


def _load_alerts() -> dict:
    try:
        cfg = json.loads(_ALERTS_PATH.read_text())
    except Exception:
        cfg = {}
    cfg.setdefault("enabled", False)
    cfg.setdefault("cooldown_min", 30)
    cfg.setdefault("cells", {})   # {band: {mode: {"needed": bool, "open": bool}}}
    cfg.setdefault("local_only", True)   # alert only on MY OWN RX decodes (*-LOCAL), not others' spots
    return cfg


_ALERTS = _load_alerts()


def _save_alerts(cfg: dict):
    global _ALERTS
    cfg.setdefault("enabled", False)
    cfg.setdefault("cooldown_min", 30)
    cfg.setdefault("cells", {})
    cfg.setdefault("local_only", True)
    _ALERTS = cfg
    tmp = _ALERTS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=1))
    tmp.replace(_ALERTS_PATH)


def _pushover_creds() -> tuple[str, str]:
    """Read fresh each time so creds can be added to secrets.json without a restart."""
    try:
        sec = json.loads((_BASE_DIR / "secrets.json").read_text())
        return sec.get("pushover_token", "") or "", sec.get("pushover_user", "") or ""
    except Exception:
        return "", ""


def _send_pushover(title: str, message: str, priority: int = 0) -> bool:
    token, user = _pushover_creds()
    if not token or not user:
        return False
    data = urllib.parse.urlencode({
        "token": token, "user": user,
        "title": title[:250], "message": message[:1024], "priority": priority,
    }).encode()
    try:
        req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=data)
        with urllib.request.urlopen(req, timeout=8) as r:
            r.read()
        return True
    except Exception as e:
        log.warning("pushover send failed: %s", e)
        return False


def _alert_mode_token(mode: str) -> str:
    m = (mode or "").strip().upper()
    if m in ("MSK144", "Q65"):
        return m
    return mode_class(mode)   # CW / Phone / Digital / Other


def _within_ms_range(grid: str) -> bool:
    if not HOME_LATLON or not grid:
        return False
    c = maidenhead_to_latlon(grid)
    return bool(c) and haversine_miles(HOME_LATLON[0], HOME_LATLON[1], c[0], c[1]) <= _MS_RANGE_MI


def _spot_is_needed(s: dict) -> bool:
    # award-aware, per band: "new" = unworked DXCC band-slot OR grid band-slot.
    # grid_band_status is only set on grid-award bands and dxcc on DXCC bands,
    # so this honors the per-band scope without extra logic.
    return s.get("dxcc_band_status") == "new" or s.get("grid_band_status") == "new"


def _fire_alert(s: dict, reason: str, priority: int = 0):
    call = s.get("dx_call", "?")
    band = s.get("band", "")
    mode = s.get("mode", "")
    parts = [call]
    if s.get("grid"):
        parts.append(s["grid"])
    if s.get("country") and s["country"] != "United States":
        parts.append(s["country"])
    bm = f"{band} {mode}".strip()
    if bm:
        parts.append(bm)
    freq = s.get("freq_khz") or s.get("freq") or 0
    try:
        fk = float(freq)
        if fk:
            parts.append(f"{fk/1000:.3f}" if fk > 1000 else f"{fk}")
    except (TypeError, ValueError):
        pass
    # Run the HTTP POST off the ingest/event-loop thread so a slow Pushover
    # call never stalls spot processing.
    threading.Thread(target=_send_pushover,
                     args=(f"Grayline — {reason}", "  ".join(p for p in parts if p), priority),
                     daemon=True).start()


def _maybe_alert(s: dict, calling_me: bool = False):
    if not s:
        return
    band = s.get("band", "")
    call = s.get("dx_call", "")
    mt = _alert_mode_token(s.get("mode", ""))
    now = time.time()
    # Calls-you bypass: someone sending YOUR call always pings (any band/mode),
    # short cooldown so a pileup answering you doesn't spam.
    if calling_me:
        ck = ("callsme", call)
        if now - _alert_cooldown.get(ck, 0) >= 300:
            _alert_cooldown[ck] = now
            _fire_alert(s, "CALLS YOU", priority=1)
        return
    cfg = _ALERTS
    if not cfg.get("enabled"):
        return
    cell = cfg.get("cells", {}).get(band, {}).get(mt)
    if not cell or not (cell.get("needed") or cell.get("open")):
        return
    # Scope. The gold-standard "I can actually work this" signal is MY OWN
    # receiver decoding it (source *-LOCAL) — not a nearby spotter, because a
    # local BIG GUN (Jim's 130ft Long Lines superstation 6mi away) hears DX a
    # modest yard antenna can't, and the spotter-distance filter wrongly passes
    # those as "local/workable." So local_only (default) fires ONLY on my own
    # decodes; if I heard it, it's workable regardless of band/mode/range.
    if cfg.get("local_only", True):
        if not (s.get("source") or "").endswith("-LOCAL"):
            return
        in_scope = True
    elif mt == "MSK144":
        in_scope = _within_ms_range(s.get("grid", ""))      # MS: DX within meteor range
    else:
        d = s.get("distance_mi")                            # else: nearby-spotter proxy
        in_scope = d is not None and d <= _telnet_feed_radius_mi(band)
    if not in_scope:
        return
    cooldown = max(1, int(cfg.get("cooldown_min", 30))) * 60
    if cell.get("needed") and _spot_is_needed(s):
        ck = ("need", call, band)
        if now - _alert_cooldown.get(ck, 0) >= cooldown:
            _alert_cooldown[ck] = now
            _fire_alert(s, "needed")
        return
    if cell.get("open"):
        ck = ("open", band, mt)   # band-open is band/mode-level, not per-call
        if now - _alert_cooldown.get(ck, 0) >= cooldown:
            _alert_cooldown[ck] = now
            _fire_alert(s, f"{band} {mt} active")


def add_spot(spot, cluster_name, calling_me=False):
    band = dxcluster.freq_to_band(spot.freq_khz)
    if not band:
        return
    # Drop spots from misconfigured-skimmer placeholder callsigns. N0CALL is the
    # convention-standard "I forgot to set my callsign" string. Anything containing
    # it is unverifiable — no real grid, no real location, no way to filter on distance.
    if "N0CALL" in (spot.spotter or "").upper():
        return
    # Suppress our own home skimmer's cluster spots (contest junk). Scoped to
    # non-WSJTX-LOCAL sources so genuine local WSJT-X decodes are never dropped.
    if (EXCLUDE_LOCAL_SKIMMER and cluster_name != "WSJTX-LOCAL"
            and (spot.spotter or "").upper() in LOCAL_SKIMMER_SPOTTERS):
        return
    mode = spot.mode or dxcluster.infer_mode(spot.freq_khz, REGION) or "UNK"
    key = _spot_dedup_key(band, mode, spot.freq_khz, spot.dx_call)

    # Source-precedence dedup. If we already have this spot from a higher-priority
    # source (our own WSJT-X or SparkGap), don't overwrite it with a lower-priority
    # external one. Local-source spots are higher fidelity (no propagation hop, no
    # third-party decoding) and the operator should always see those instead of
    # cluster duplicates. SOURCE_PRIORITY is defined at the top of this module.
    new_priority = SOURCE_PRIORITY.get(cluster_name, SOURCE_PRIORITY_DEFAULT)
    with _lock:
        existing = _cache.get(key)
        if existing is not None:
            existing_priority = SOURCE_PRIORITY.get(
                existing.get("source", ""), SOURCE_PRIORITY_DEFAULT)
            if new_priority < existing_priority:
                # A higher-priority source (e.g. our local WSJT-X) owns this entry.
                # Keep it ONLY while that source is genuinely still producing it —
                # otherwise its freq / audio offset / click-to-tune match data are
                # frozen at the last real local decode while the station moved or
                # faded, leaving a "fresh-looking" zombie that points stale.
                #
                # Authoritative iff BOTH:
                #   (a) updated by its own source within LOCAL_SPOT_FRESH_SEC, and
                #   (b) for WSJTX-LOCAL, a WSJT-X instance is still on this band
                #       (a retuned slice — SliceB 17m -> 20m — stops decoding its
                #       old band, so those entries must hand off).
                # Note we do NOT refresh ts here: ts must track the entry's own
                # source so staleness is detectable. If it goes stale, fall through
                # and let the lower-priority spot overwrite — demoting to the
                # cluster's live freq + honest source instead of a zombie local.
                fresh = (time.time() - existing.get("ts", 0)) < LOCAL_SPOT_FRESH_SEC
                band_live = (
                    existing.get("source") != "WSJTX-LOCAL"
                    or _wsjtx_state_for_band(existing.get("band", "")) is not None
                )
                if fresh and band_live:
                    return

    # Distance is computed against the SPOTTER's QTH (where the listener is),
    # not the DX's QTH (where the rare station is). If a spotter near you
    # hears it, propagation suggests you might too — that's the useful filter
    # for "what can I work right now."
    #
    # If we can't verify the spotter's grid (not in QRZ cache, no grid set
    # in their QRZ profile, etc.), drop the spot at ingest. We can't filter
    # what we can't measure, and unverifiable spotters correlate with junk
    # (FG1G/4/30 spotting EU on 2m, etc.). spotter_distance_mi() queues the
    # callsign for active QRZ lookup as a side effect, so subsequent spots
    # from a real-but-unresolved spotter will pass once the lookup completes.
    #
    # *-LOCAL sources bypass this check — those are us by definition, the
    # "spotter" field is a slice label or skimmer ID rather than a QRZ-resolvable
    # callsign, and distance is always 0 (we're at our own QTH).
    if cluster_name.endswith("-LOCAL"):
        distance_mi = 0
    else:
        distance_mi = spotter_distance_mi(spot.spotter)
        # distance_mi is None when the spotter has no resolvable grid (cold QRZ
        # cache, no creds, or no home grid set). Only drop on that in strict mode;
        # otherwise keep the spot with unknown distance so a fresh install still
        # shows a populated roster.
        if distance_mi is None and REQUIRE_SPOTTER_GRID:
            return

    # cty.dat enrichment for the DX call (entity name doubles as the
    # bridge to your logbook's `country` field)
    country = continent = cq_zone = itu_zone = dxcc_num = ""
    if _cty:
        e = _cty.lookup(spot.dx_call)
        if e:
            country = e.entity or ""
            continent = e.continent or ""
            cq_zone = str(e.cq_zone) if e.cq_zone is not None else ""
            itu_zone = str(e.itu_zone) if e.itu_zone is not None else ""
            dxcc_num = str(e.dxcc) if e.dxcc else ""

    # CQ DX Marathon need (WAE-aware entity + zone vs this year's worked sets)
    marathon = _marathon_status(dxmarathon.entity_id(spot.dx_call, _cty) if _cty else None,
                                dxmarathon.zone(spot.dx_call, _cty) if _cty else None)

    # WAE (Worked All Europe) need — only pills when NOT yet confirmed (high-signal).
    wae_status, wae_name = _wae_spot_status(spot.dx_call)

    # Effective grid — never REGRESS to blank. FT8 carries the grid only in a
    # CQ / grid-reply; mid-QSO messages (reports, R-15, RR73, 73) have none, so a
    # later decode of the same station (same cache key) would otherwise wipe the
    # grid we already deduced from its CQ. Precedence: this decode's grid → the
    # grid we already cached for this call (a prior grid-bearing decode) → the
    # QRZ-cached grid (covers cluster spots that never carry one).
    eff_grid = spot.grid or ""
    if not eff_grid:
        with _lock:
            prev = _cache.get(key)
            if prev:
                eff_grid = prev.get("grid", "") or ""
    if not eff_grid:
        with _qrz_cache_lock:
            eff_grid = _qrz_cache.get(spot.dx_call, "") or ""

    # Worked/needed status (against your QRZ logbook)
    call_status = "new"
    dxcc_band_status = "new"
    dxcc_band_mode_status = "new"
    dxcc_band_modeclass_status = "new"
    dxcc_modeclass_status = "new"
    modeclass = mode_class(mode) if mode else ""
    grid_band_status = "new"
    if _worked:
        call_status = _worked.call_band_status(spot.dx_call, band)
        if country:
            # Number-keyed (LoTW ground truth, collapses WAE splits) UNION name.
            dxcc_band_status = _worked.entity_band_status(dxcc_num, country, band)
            if mode:
                dxcc_band_mode_status = _worked.country_band_mode_status(country, band, mode)
                dxcc_band_modeclass_status = _worked.country_band_modeclass_status(country, band, modeclass)
                dxcc_modeclass_status = _worked.country_modeclass_status(country, modeclass)
        if eff_grid:
            grid_band_status = _worked.grid_band_status(eff_grid, band)

    # WAJA (JARL) — advisory prefecture pill for JA stations. Prefecture is
    # resolved best-effort from QRZ addr2 (cached/looked-up async); unknown or
    # unresolvable -> waja_status None -> no pill. The award itself is credited
    # from the logged QSO's STATE/CNTY, not from this.
    waja_pref, waja_status = ("", None)
    if country == "Japan":
        waja_pref, waja_status = _ja_pref_for_spot(spot.dx_call)

    with _lock:
        # Nearest-spotter-wins. The local-spotter radius filter asks "did a
        # spotter NEAR me hear this?" — and once one did, that's a sticky fact for
        # the spot's life. A DX station (e.g. JA) is reported by many spotters at
        # different distances; without this, each re-spot overwrote distance_mi
        # with the latest spotter's distance, so a far re-spot would bump a
        # near-spotted station back outside the radius and it would vanish from
        # view mid-life. So: on a same-tier re-spot by a FARTHER spotter, keep the
        # nearer spotter's view (distance/spotter/snr) and just refresh recency —
        # the station is still active. A nearer (or higher-priority, i.e. local)
        # re-spot falls through and replaces the entry as before.
        existing = _cache.get(key)
        if existing is not None:
            ep = SOURCE_PRIORITY.get(existing.get("source", ""), SOURCE_PRIORITY_DEFAULT)
            ed = existing.get("distance_mi")
            if new_priority == ep and ed is not None and (distance_mi is None or distance_mi > ed):
                existing["ts"] = time.time()
                _cache.move_to_end(key)
                return
        # FFMA grid rarity (Tier 0, global): only meaningful on 6m (FFMA is a 6m award).
        ffma_rarity = _FFMA_RARITY.get(eff_grid[:4].upper()) if (band == "6m" and eff_grid) else None
        heard_me = psk_heard.heard(spot.dx_call)   # this station reported decoding ME to PSKReporter
        _cache[key] = {
            "ts": time.time(),
            "band": band,
            "mode": mode,
            "freq_khz": spot.freq_khz,
            "dx_call": spot.dx_call,
            "spotter": spot.spotter or "",
            "snr": spot.snr,
            "grid": eff_grid,
            "distance_mi": distance_mi,
            "country": country,
            "dxcc": dxcc_num,
            "dxcc_rank": (_DXCC_RARITY.get(dxcc_num) if dxcc_num else None),  # Club Log Most Wanted rank (lower=rarer) or None
            "lotw_days": lotw_activity.days_since_upload(spot.dx_call),  # days since last LoTW upload, or None if not a LoTW user
            "calling_me": calling_me,                       # this station is transmitting MY callsign right now (directed at me) — bypasses filters, red highlight
            "marathon": marathon,                           # CQ DX Marathon need this year: 'DXCC'|'CQz'|'DX+CQz'|None
            "wae_status": wae_status,                        # WAE need: 'new'|'worked'|'confirmed'|None
            "wae_name": wae_name,                            # WAE country name (pill tooltip)
            "ffma_rarity": ffma_rarity,                     # FFMA grid rarity (6m only): {pct_needed,leaders_needing,tier} or None
            "heard_me": heard_me,                           # this station reported decoding ME to PSKReporter (eye badge) or None
            "continent": continent,
            "cq_zone": cq_zone,
            "itu_zone": itu_zone,
            "call_status": call_status,                     # 'new' | 'worked' | 'confirmed'
            "dxcc_band_status": dxcc_band_status,           # same enum, scoped to country+band (mixed-mode, ARRL Challenge / DXCC-Mixed)
            "dxcc_band_mode_status": dxcc_band_mode_status, # scoped to country+band+literal-mode (e.g. country+band+FT8)
            "dxcc_band_modeclass_status": dxcc_band_modeclass_status,  # country+band+ARRL-class — personal goal (entity×band×mode), off by default
            "dxcc_modeclass_status": dxcc_modeclass_status,  # country+ARRL-class, ANY band — the real ARRL mode DXCC (CW/Phone/Digital)
            "modeclass": modeclass,                         # CW | Phone | Digital | Other — derived from mode_class(mode)
            "grid_band_status": grid_band_status,           # scoped to grid×band (FFMA on 6m, VUCC on 2m+)
            "waja_pref": waja_pref,                          # JA prefecture code ("01".."47") or "" — for the WAJA pill
            "waja_status": waja_status,                      # 'new'|'worked'|'confirmed' or None (no pill)
            "comment": spot.comment[:60] if spot.comment else "",
            "time_utc": spot.time_utc,
            "source": cluster_name,                         # WSJTX-LOCAL / SPARKGAP-LOCAL / GOCLUSTER / external — drives precedence dedup
            "audio_offset_hz": getattr(spot, "audio_offset", 0) or 0,  # baseband audio offset (set for WSJT-X-sourced FT8 spots)
        }
        # keep newest, drop oldest if over cap
        _cache.move_to_end(key)
        while len(_cache) > MAX_SPOTS:
            _cache.popitem(last=False)

    # Phase 2 Flex integration: queue this spot for panadapter injection if
    # the band has an active slice and the mode isn't FT8/FT4. The worker
    # drains the queue at a rate limit that keeps SmartSDR's API channel
    # well clear of the saturation point that caused the original audio
    # dropouts in GTBridge.
    _maybe_queue_flex_inject(_cache[key])

    # Pushover: evaluate this finalized spot against the alert matrix (no-op
    # unless configured + creds present; sends off-thread).
    _maybe_alert(_cache.get(key), calling_me)

    # Re-broadcast to the SDC / DX-cluster telnet feed, applying the same tiered
    # local-spotter radius as the browser's "Local spotters only" toggle. By
    # this point distance_mi is always numeric — 0 for *-LOCAL sources, verified
    # miles otherwise (unverifiable/too-far spotters were dropped at ingest
    # above) — so a single <= comparison reproduces the UI filter exactly.
    # Runs on the event-loop thread (add_spot is driven by the async on_spot /
    # WSJT-X paths), so the non-blocking writer.write() calls are loop-safe.
    if _telnet_feed is not None and distance_mi <= _telnet_feed_radius_mi(band):
        _telnet_feed.broadcast_spot(spot)


def purge_loop():
    while True:
        time.sleep(PURGE_INTERVAL)
        now = time.time()
        with _lock:
            stale = [
                k for k, v in _cache.items()
                if now - v["ts"] > (SPOT_TTL_DIGITAL if v.get("modeclass") == "Digital" else SPOT_TTL)
            ]
            for k in stale:
                del _cache[k]


def psk_heard_loop():
    """Poll PSKReporter for 'who heard MY_CALL' every ~5 min and refresh the eye on
    every cached spot. Only lights up while you've been transmitting and decoded."""
    while True:
        try:
            psk_heard.refresh(CALLSIGN)
            with _lock:
                for s in _cache.values():
                    s["heard_me"] = psk_heard.heard(s["dx_call"])
        except Exception as e:
            log.warning("psk_heard_loop error: %s", e)
        time.sleep(psk_heard.POLL_SEC)


def snapshot():
    with _lock:
        rows = list(_cache.values())
    return rows


# ---------------- FFMA grid list (CONUS-48, ARRL canonical) ----------------
def _load_ffma_grids() -> list[str]:
    try:
        path = Path(__file__).parent / "data" / "ffma_grids.json"
        return json.loads(path.read_text())["grids"]
    except Exception as e:
        log.warning("FFMA grid list unavailable (%s); FFMA scope will not function", e)
        return []


_FFMA_GRIDS = _load_ffma_grids()
_FFMA_GRID_SET = frozenset(g.upper() for g in _FFMA_GRIDS)
log.info("FFMA grid list loaded: %d grids", len(_FFMA_GRIDS))


def _load_ffma_rarity() -> dict:
    """Per-grid FFMA rarity (Tier 0): {GRID: {pct_needed, leaders_needing, tier}}.
    Authoritative source = N7PHY's FFMA Leader Board (% of 400+-confirmed leaders
    who still need each grid). Location-AGNOSTIC global rarity — the universal
    default everyone ships with; the HOME_GRID reachability overlay is Tier 1."""
    try:
        path = Path(__file__).parent / "data" / "ffma_rarity.json"
        return json.loads(path.read_text()).get("grids", {})
    except Exception as e:
        log.warning("FFMA rarity data unavailable (%s); rarity badge disabled", e)
        return {}


_FFMA_RARITY = {k.upper(): v for k, v in _load_ffma_rarity().items()}
log.info("FFMA rarity loaded: %d grids (%d rare, %d uncommon)",
         len(_FFMA_RARITY),
         sum(1 for v in _FFMA_RARITY.values() if v.get("tier") == "rare"),
         sum(1 for v in _FFMA_RARITY.values() if v.get("tier") == "uncommon"))

# mtime of data/ffma_rarity.json when last loaded, so the reload loop can pick up
# the daily gridzilla_ffma_fetch.py cron rewrite without a server restart.
try:
    _FFMA_RARITY_MTIME = (Path(__file__).parent / "data" / "ffma_rarity.json").stat().st_mtime
except OSError:
    _FFMA_RARITY_MTIME = 0.0


def _maybe_reload_ffma_rarity() -> bool:
    """If the daily cron (gridzilla_ffma_fetch.py) rewrote data/ffma_rarity.json,
    reload the in-memory map and recompute the HOME_GRID reachability overlay.
    mtime-gated, so a no-change tick is one stat() call. Returns True on reload."""
    global _FFMA_RARITY, _FFMA_RARITY_MTIME
    path = Path(__file__).parent / "data" / "ffma_rarity.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    if mtime == _FFMA_RARITY_MTIME:
        return False
    _FFMA_RARITY = {k.upper(): v for k, v in _load_ffma_rarity().items()}
    _augment_ffma_reachability()   # re-overlay distance/es_band/personal_tier
    _FFMA_RARITY_MTIME = mtime
    log.info("FFMA rarity reloaded from disk: %d grids (%d rare, %d uncommon)",
             len(_FFMA_RARITY),
             sum(1 for v in _FFMA_RARITY.values() if v.get("tier") == "rare"),
             sum(1 for v in _FFMA_RARITY.values() if v.get("tier") == "uncommon"))
    return True


def _es_reach(distance_mi):
    """6m Es reachability for a distance from HOME_GRID -> (band, factor).
    Single-hop Es is the easy bread-and-butter (~450-1400mi); too-close grids the
    Es SKIPS (need scatter -> harder); far needs double/multi-hop (rarer geometry).
    factor <1 = easier than the global number for your QTH, >1 = harder."""
    d = distance_mi
    if d < 450:    return ("scatter (too close)", 1.4)   # Es overshoots; scatter only
    if d <= 1400:  return ("single-hop Es", 0.7)          # the sweet spot
    if d <= 1700:  return ("single-hop edge", 1.0)
    if d <= 2600:  return ("double-hop Es", 1.4)
    return ("multi-hop", 1.8)


def _augment_ffma_reachability():
    """Tier 1: overlay HOME_GRID reachability onto the global rarity. Computed from
    CONFIG['home_grid'] (NOT hardcoded) so it personalizes for any user. Adds
    distance_mi / es_band / personal_pct / personal_tier to each grid's record."""
    if not HOME_LATLON:
        return
    hlat, hlon = HOME_LATLON
    for grid, rec in _FFMA_RARITY.items():
        c = maidenhead_to_latlon(grid)
        if not c:
            continue
        d = round(haversine_miles(hlat, hlon, c[0], c[1]))
        band, factor = _es_reach(d)
        pp = min(100.0, round((rec.get("pct_needed") or 0) * factor, 1))
        rec["distance_mi"] = d
        rec["es_band"] = band
        rec["personal_pct"] = pp
        rec["personal_tier"] = "rare" if pp >= 30 else ("uncommon" if pp >= 10 else "common")


_augment_ffma_reachability()
log.info("FFMA reachability overlay computed from %s", HOME_GRID or "(no home grid)")


def _load_dxcc_rarity() -> dict:
    """{dxcc_number(str): most-wanted rank(int)} from Club Log's public Most
    Wanted list (cached in data/dxcc_rarity.json as {rank: dxcc#}). Lower rank
    = rarer. Keyed by ARRL DXCC number so it joins straight onto spot['dxcc']."""
    try:
        path = Path(__file__).parent / "data" / "dxcc_rarity.json"
        rank2num = json.loads(path.read_text())
        return {str(num): int(rank) for rank, num in rank2num.items()}
    except Exception as e:
        log.warning("DXCC rarity list unavailable (%s); rarity badges off", e)
        return {}


_DXCC_RARITY = _load_dxcc_rarity()
log.info("DXCC rarity list loaded: %d entities", len(_DXCC_RARITY))


def _load_dxcc_names() -> dict:
    """{dxcc_number(str): entity name} from cty.csv (col0=prefix, col1=name,
    col2=dxcc code). Skips '*'-prefixed WAE/non-DXCC rows so the real DXCC
    entity name wins (Asiatic Turkey, not European Turkey, for 390)."""
    names = {}
    try:
        path = Path(__file__).parent / "cty.csv"
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            p = line.split(",")
            if len(p) >= 3 and not p[0].startswith("*"):
                try:
                    names[str(int(p[2]))] = p[1].strip()
                except ValueError:
                    pass
    except Exception as e:
        log.warning("cty.csv entity names unavailable (%s)", e)
    return names


_DXCC_NAME = _load_dxcc_names()


# ---- Club Log log/OQRS matches (weekly) -> claimable + discrepancy detection ----
# getmatches.php returns QSOs validated against uploaded logs (log search + OQRS).
# A worked-not-confirmed slot that IS matched = claimable (do OQRS). Suspect is
# keyed on the *callsign*, not the entity: a slot is suspect only when the very
# station you logged there appears in getmatches on some OTHER band but not this
# one ("you logged TT1GD on 10m, but TT1GD's own log only has you on 40m"). It is
# NOT suspect just because a *different* station of the same entity matched —
# e.g. a real TT8XX DXpedition QSO must not be flagged because TT1GD matched.
_MATCHED_SLOTS: dict = {}        # (dxcc_str, band_str) -> matched DX callsign
_MATCHED_CALL_SLOTS: set = set() # (call, band_str) confirmed in the DX's own log
_MATCHED_CALLS: set = set()      # callsigns that appear anywhere in getmatches


def _band_from_clublog(bn) -> str:
    return {"70": "70cm", "23": "23cm"}.get(str(bn), str(bn) + "m")


def _process_clublog_matches(matches) -> tuple:
    slots, call_slots, calls = {}, set(), set()
    for m in matches:
        try:
            call = str(m[0]).strip().upper(); dxcc = str(m[1]); band = _band_from_clublog(m[3])
        except (IndexError, TypeError):
            continue
        slots[(dxcc, band)] = m[0]
        call_slots.add((call, band))
        calls.add(call)
    return slots, call_slots, calls


def _load_clublog_matches():
    global _MATCHED_SLOTS, _MATCHED_CALL_SLOTS, _MATCHED_CALLS
    try:
        path = Path(__file__).parent / "data" / "clublog_matches.json"
        _MATCHED_SLOTS, _MATCHED_CALL_SLOTS, _MATCHED_CALLS = _process_clublog_matches(json.loads(path.read_text()))
    except Exception as e:
        log.warning("Club Log matches cache unavailable (%s)", e)
        _MATCHED_SLOTS, _MATCHED_CALL_SLOTS, _MATCHED_CALLS = {}, set(), set()


_load_clublog_matches()
log.info("Club Log matches loaded: %d slots, %d calls", len(_MATCHED_SLOTS), len(_MATCHED_CALLS))


# ---- M0OXO OQRS matches (manual paste) — entity-level confirmation pointer ----
# M0OXO (m0oxo.com) manages QSLs/OQRS for many DXpeditions, entirely separate
# from Club Log, and exposes no public match API — so this is hand-maintained:
# search your call at m0oxo.com OQRS and drop the DX callsigns into
# data/m0oxo_matches.json. We resolve each to an entity and flag it as
# "confirmable on M0OXO." Bands aren't available (the search returns calls only),
# so this is entity-level only — it just says "look here to confirm this one."
_M0OXO_CALLS: list = []


def _load_m0oxo_calls() -> list:
    try:
        path = Path(__file__).parent / "data" / "m0oxo_matches.json"
        d = json.loads(path.read_text())
        return [str(c).strip().upper() for c in d.get("calls", []) if str(c).strip()]
    except Exception as e:
        log.warning("M0OXO matches unavailable (%s)", e)
        return []


_M0OXO_CALLS = _load_m0oxo_calls()
log.info("M0OXO matches loaded: %d DX logs", len(_M0OXO_CALLS))


def clublog_matches_refresh_loop():
    """Weekly: fetch getmatches.php (log + OQRS matches), atomically cache, reload.
    Reuses the ClubLog 403 breaker (repeated 403s firewall the IP), skips without
    creds, keeps the last cache on any failure. Checks daily, refetches at >=7d."""
    global _MATCHED_SLOTS, _MATCHED_CALL_SLOTS, _MATCHED_CALLS
    path = Path(__file__).parent / "data" / "clublog_matches.json"
    INTERVAL = 7 * 86400
    while True:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = INTERVAL + 1
        if age >= INTERVAL:
            sec = logbook_uploads._load_secrets()
            email = sec.get("clublog_email", ""); pw = sec.get("clublog_password", "")
            api = sec.get("clublog_api_key", "")
            cs = sec.get("clublog_callsign", "") or sec.get("qrz_user", "").upper()
            if not (email and pw and api and cs):
                log.info("Club Log matches: creds incomplete, skipping refresh")
            else:
                ch = logbook_uploads._clublog_creds_hash(email, pw, api)
                if logbook_uploads._clublog_blocked(ch):
                    log.warning("Club Log matches: breaker tripped, skipping until creds change")
                else:
                    try:
                        q = urllib.parse.urlencode({"api": api, "email": email,
                                                    "password": pw, "callsign": cs})
                        with urllib.request.urlopen("https://clublog.org/getmatches.php?" + q,
                                                    timeout=60) as r:
                            data = r.read().decode("utf-8")
                        matches = json.loads(data)
                        if isinstance(matches, list):
                            tmp = path.with_suffix(".json.tmp"); tmp.write_text(data); tmp.replace(path)
                            _MATCHED_SLOTS, _MATCHED_CALL_SLOTS, _MATCHED_CALLS = _process_clublog_matches(matches)
                            log.info("Club Log matches refreshed: %d slots", len(_MATCHED_SLOTS))
                    except Exception as e:
                        code = getattr(e, "code", None)
                        if code in (401, 403):
                            logbook_uploads._trip_clublog_breaker(ch, "getmatches HTTP " + str(code))
                        log.warning("Club Log matches refresh failed (kept cache): %s", e)
        time.sleep(86400)


def dxcc_rarity_refresh_loop():
    """Re-fetch Club Log's public Most Wanted ranking every 2 weeks and atomically
    rewrite data/dxcc_rarity.json + reload the in-memory map. Public endpoint, no
    API key. Club Log re-ranks ~monthly, so a fortnightly poll catches it with
    margin. Checks daily but only re-fetches when the cache is >=14 days old, so
    it survives restarts (refreshes on boot if stale) without polling needlessly."""
    global _DXCC_RARITY
    url = "https://clublog.org/mostwanted.php?api=1"
    path = Path(__file__).parent / "data" / "dxcc_rarity.json"
    INTERVAL = 14 * 86400
    while True:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = INTERVAL + 1  # missing cache -> fetch now
        if age >= INTERVAL:
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    data = r.read().decode("utf-8")
                rank2num = json.loads(data)
                if isinstance(rank2num, dict) and len(rank2num) > 100:
                    tmp = path.with_suffix(".json.tmp")
                    tmp.write_text(data)
                    tmp.replace(path)
                    _DXCC_RARITY = {str(num): int(rank) for rank, num in rank2num.items()}
                    log.info("DXCC rarity refreshed: %d entities", len(_DXCC_RARITY))
                else:
                    log.warning("DXCC rarity refresh: unexpected payload, kept cache")
            except Exception as e:
                log.warning("DXCC rarity refresh failed (kept cache): %s", e)
        time.sleep(86400)  # re-check daily; refetch only when cache >= 14 days old


# ---------------- HTML ----------------
HTML_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grayline — live spots</title>
<style>
:root { color-scheme: dark; }
body { font-family: -apple-system, system-ui, sans-serif; margin: 0.8em; background: #000; color: #eee; }
h1 { font-size: 1.1em; margin: 0 0 0.3em; color: #0f0; }
.status { color: #888; font-size: 0.85em; margin-bottom: 0.8em; }
.status .count { color: #ff0; font-weight: bold; }
.status .wanted { color: #ff5; font-weight: bold; }
/* Legacy band-container styles deleted in Stage 8 — the <div class="band">
   layout was replaced by the flat-table-with-band-column layout. The .band
   class now applies to a <td>; styling lives further down with the rest of
   the cell rules. */
table { border-collapse: collapse; width: 100%; font-size: 0.78em; font-variant-numeric: tabular-nums; }
th, td { padding: 1px 6px; text-align: left; border-bottom: 1px dotted #1a1a1a; white-space: nowrap; }
th { color: #ff0; font-weight: normal; font-size: 0.75em; background: #1a1a00; }
/* GT-style call status colors. Apply to <td> directly so cell heights stay even. */
.dx { font-weight: 600; }
.dx.new { color: #f0f; }              /* magenta — never worked */
.dx.worked { color: #ff5; }           /* yellow text — worked, not confirmed */
.dx.confirmed { color: #5c5; }        /* dim green — confirmed */
.spotter { color: #999; font-size: 0.85em; }
.spotter.us { color: #5f5; font-weight: 600; }       /* spotter is us — we definitely heard it */
tr.us-spotted { box-shadow: inset 3px 0 0 #5f5; }     /* subtle green left edge on the row */
tr.clickable { cursor: pointer; }
tr.clickable:hover { background: #1a2a3a; }
tr.tuning { background: #2a4a6a !important; transition: background 0.6s; }
.snr { text-align: right; }
.snr.neg { color: #f99; }
.snr.pos { color: #9f9; }
.freq { color: #8fc; text-align: right; }
.age { color: #555; font-size: 0.75em; text-align: right; }
.grid { color: #bda; font-size: 0.85em; }
.country { font-size: 0.85em; }
td.country.new {                                     /* needed DXCC on this band — orange cell fill, black text */
  background: #ffa500; color: #000; font-weight: 700;
}
td.country.worked {                                  /* worked the entity, not confirmed on this band — orange outline on cell */
  box-shadow: inset 0 0 0 1.5px #ffa500;
  color: #fb6;
}
.country.confirmed { color: #888; }                  /* confirmed on this band — dim */
/* Grid×band highlighting for VHF+ (FFMA on 6m, VUCC on 2m+) — same orange convention as DXCC */
td.grid.gnew {
  background: #ffa500; color: #000; font-weight: 700;
}
td.grid.gworked {
  box-shadow: inset 0 0 0 1.5px #ffa500;
  color: #fb6;
}
.grid.gconfirmed { color: #888; }
.cont { color: #5cf; font-size: 0.78em; text-align: center; }
.band { color: #ff0; font-size: 0.85em; text-align: center; font-weight: 600; }
.mode { color: #bcf; font-size: 0.8em; text-align: center; }
.dist { color: #fa9; font-size: 0.85em; text-align: right; }
.dist.far { color: #555; }
.controls { margin-bottom: 0.8em; font-size: 0.85em; }
.controls label { color: #ccc; cursor: pointer; user-select: none; }
.controls input { margin-right: 0.4em; }
details { margin: 0; }
details > summary { cursor: pointer; user-select: none; list-style: none; outline: none; }
details > summary::-webkit-details-marker { display: none; }
details > summary::before { content: "▶ "; color: #555; font-size: 0.7em; display: inline-block; transition: transform 0.1s; }
details[open] > summary::before { transform: rotate(90deg); }
.mode-block > details > summary { font-size: 0.8em; color: #bcf; margin: 0.4em 0 0.1em; }
.legend { font-size: 0.75em; color: #666; margin-left: 2em; }
.legend span { padding: 0 0.4em; }

/* Tab strip — one tab per active band */
.tab-strip { display: flex; gap: 0.15em; flex-wrap: wrap; margin-bottom: 0.6em; }
.tab-strip button {
  background: #1a1a1a; color: #aaa; border: 1px solid #333;
  padding: 0.35em 0.8em; cursor: pointer; font-size: 0.85em;
  font-family: inherit; outline: none;
}
.tab-strip button:hover { background: #2a2a2a; color: #eee; }
.tab-strip button.active {
  background: #ff0; color: #000; font-weight: 700; border-color: #ff0;
}
.tab-strip button.active:hover { background: #ff0; color: #000; }
.tab-strip .count { opacity: 0.7; margin-left: 0.5em; }
.tab-strip .count .wanted { color: #ff5050; font-weight: 700; }   /* red wanted-count, neutral total */
.tab-strip .count .sep { color: #555; }
.tab-strip button.active .count { opacity: 1; }
.tab-strip button.active .count .wanted { color: #c00; }          /* darker red on yellow active-tab background */
.tab-strip button.active .count .sep { color: #333; }
.tab-strip .empty { color: #555; }

/* Award column — one pill per applicable scope (DXCC-Mixed / DXCC-CW etc., Grid). */
.awards { font-size: 0.7em; line-height: 1.4; }
.awards .pill {
  display: inline-block; padding: 0px 4px; margin-right: 2px;
  border-radius: 3px; border: 1px solid transparent;
  font-weight: 600; letter-spacing: 0.02em;
}
.awards .pill.new {                                                /* this scope is needed — orange fill */
  background: #ffa500; color: #000;
}
.awards .pill.worked {                                             /* worked, not confirmed — orange outline */
  border-color: #ffa500; color: #fb6;
}
.awards .pill.confirmed {                                          /* already confirmed for this scope — dim */
  color: #666; border-color: #2a2a2a;
}
/* Club Log Most Wanted rarity badge in the entity cell — lower rank = rarer. */
.mw {
  font-size: 0.72em; font-weight: 700; padding: 0 0.3em; border-radius: 3px;
  margin-left: 0.35em; vertical-align: middle; letter-spacing: 0.02em;
}
.mw-hot  { background: #c0392b; color: #fff; }     /* top 10  — grail */
.mw-warm { background: #d68910; color: #111; }     /* top 50  */
.mw-cool { background: #5d4a0a; color: #f5d76e; }  /* top 150 */
/* "CQ" tag — digital spot whose latest decode is a CQ (actively soliciting). */
.cqtag { display: inline-block; padding: 0 0.3em; margin-left: 0.35em; border-radius: 3px;
  background: #1c5c2e; color: #9f9; font-size: 0.72em; font-weight: 700;
  letter-spacing: 0.03em; vertical-align: middle; }
/* LoTW-user badge — fresh uploader (good confirm bet) vs stale. Blue (LoTW's own
   brand hue): the one strong color free on the roster — green is the CQ tag, red
   is rarity, orange is the award pills. Bold blue = fresh, muted slate = stale. */
.lotw { display: inline-block; padding: 0 0.3em; margin-left: 0.35em; border-radius: 3px;
  font-size: 0.72em; font-weight: 700; letter-spacing: 0.02em; vertical-align: middle; }
.lotw-fresh { background: #2f81f7; color: #fff; }                  /* uploaded within threshold — work it */
.lotw-stale { background: #2a3f55; color: #8fb3d9; opacity: 0.85; } /* LoTW user but quiet for a while */
/* CQ DX Marathon badge — needed entity/zone this year. Purple: a free hue, distinct
   from rarity-red, CQ-green, LoTW-blue, award-orange. */
/* FFMA grid-rarity badge — gold = grail (FFMA's the gold award). In the grid cell,
   so it reads against grid-need color, not the country-cell rarity. */
/* "They can hear you" eye — PSKReporter reported this station decoding you. Cyan
   glow so it reads as a live signal-intel cue, not just an emoji. */
.heard-eye { margin-left: 0.3em; font-size: 0.92em; vertical-align: middle; cursor: help;
  filter: drop-shadow(0 0 2px #3ff) drop-shadow(0 0 4px #1cf); }
.peer-copies { margin-left: 0.35em; font-size: 0.72em; font-weight: 800; cursor: help;
  vertical-align: middle; color: #ffd24a; white-space: nowrap; }
.ffr { display: inline-block; padding: 0 0.3em; margin-left: 0.3em; border-radius: 3px;
  font-size: 0.66em; font-weight: 800; letter-spacing: 0.02em; vertical-align: middle; }
.ffr-rare { background: #d4af37; color: #1a1a1a; }   /* top-tier rare — grail gold */
.ffr-unc  { background: #6b5a1a; color: #f5d76e; }   /* uncommon — muted gold */
/* ===== FFMA tab ===== */
#view_ffma { max-width: 56em; }
.ff-standing { text-align: center; padding: 0.8em 0 1.1em; }
.ff-big { font-size: 3.4em; font-weight: 800; color: #d4af37; line-height: 1;
  font-variant-numeric: tabular-nums; text-shadow: 0 0 12px rgba(212,175,55,0.35); }
.ff-of { font-size: 0.4em; color: #888; font-weight: 600; }
.ff-sub { color: #aaa; font-size: 0.95em; margin-top: 0.3em; }
.ff-sub b { color: #f5d76e; }
.ff-bar { height: 8px; background: #1a1a1a; border-radius: 4px; margin: 0.7em auto 0.3em;
  max-width: 34em; overflow: hidden; }
.ff-fill { height: 100%; background: linear-gradient(90deg, #6b5a1a, #d4af37); border-radius: 4px; }
.ff-pctlabel { color: #777; font-size: 0.75em; }
.ff-atno { border-color: #4a3d10; }
.ff-atno-row { display: flex; align-items: center; gap: 0.8em; }
.ff-atno-grid { font-size: 2em; font-weight: 800; color: #d4af37; font-variant-numeric: tabular-nums; }
.ff-atno-meta { font-size: 0.9em; color: #bbb; line-height: 1.5; }
.ff-newest { color: #d4af37; font-weight: 700; text-transform: uppercase; font-size: 0.85em; letter-spacing: 0.04em; }
table.ff-atno-tbl { margin-top: 0.55em; }
.ff-tier { display: inline-block; padding: 0 0.35em; border-radius: 3px;
  font-size: 0.72em; font-weight: 800; vertical-align: middle; }
.ff-rare { background: #d4af37; color: #1a1a1a; }
.ff-unc  { background: #6b5a1a; color: #f5d76e; }
.ff-common { background: #1f1f1f; color: #777; }
.ff-conf { color: #4caf50; font-weight: 700; font-size: 0.82em; }
.ff-pend { color: #e0a83c; font-weight: 700; font-size: 0.82em; }
.ff-rover { font-size: 0.9em; }
.ff-count { float: right; color: #888; font-weight: 400; }
/* selector qualified with `table.` so it outranks the later `.score-card th`
   rule (right-aligned, for Scores' numeric columns) — else headers right-align
   while data cells left-align and the columns don't line up. */
table.ff-table { width: 100%; border-collapse: collapse; font-size: 0.95em;
  font-variant-numeric: tabular-nums; }
table.ff-table th { font-weight: normal; color: #888; font-size: 0.78em; text-align: left;
  border-bottom: 1px solid #222; padding: 0.15em 0.4em; }
table.ff-table th:first-child { text-align: left; }
table.ff-table td { padding: 0.18em 0.4em; border-bottom: 1px solid #141414; text-align: left; }
table.ff-table tr.ff-disc-hot td { background: #2a1410; }  /* discrepancy that actually costs a needed grid */
.ff-g { font-weight: 700; color: #d4af37; }
.ff-when { color: #888; }
.ff-who { color: #ccc; }
.ff-odds-good { color: #4caf50; font-size: 0.8em; font-weight: 700; cursor: help; }
.ff-odds-one  { color: #888; font-size: 0.8em; cursor: help; }
.ff-empty { color: #6a8d6a; padding: 0.4em 0; font-size: 0.9em; }
/* WAS by band card */
.wasb-band { font-weight: 700; color: #ccc; }
.wasb-cnt { color: #888; font-variant-numeric: tabular-nums; }
.wasb-need { color: #aaa; font-size: 0.9em; }
.wasb-need b { color: #e0a83c; }
tr.wasb-done .wasb-band { color: #5c5; }
tr.wasb-close td { background: rgba(224,168,60,0.08); }
tr.wasb-close .wasb-band { color: #e0a83c; }
.wae-unworked { color: #e0a83c; }
.wae-leg { color: #666; font-size: 0.92em; }
.band-activity-link { margin-left: 1em; color: #3fc7e0; text-decoration: none; font-size: 0.9em;
  border: 1px solid #1f4a55; border-radius: 4px; padding: 0.1em 0.5em; white-space: nowrap; }
.band-activity-link:hover { background: #0f2a30; color: #7fe0f5; }
.mara { display: inline-block; padding: 0px 4px; margin-right: 2px; border-radius: 3px;
  border: 1px solid transparent; background: #7d3cc9; color: #fff;
  font-weight: 600; letter-spacing: 0.02em; vertical-align: middle; }
/* WAE pill — distinct sea-green so it reads apart from MARA (purple) / LoTW
   (blue). Filled = needed (not worked); outline = worked, awaiting QSL. */
.wae-pill { display: inline-block; padding: 0px 4px; margin-left: 3px; border-radius: 3px;
  font-weight: 700; font-size: 0.92em; letter-spacing: 0.02em; vertical-align: middle; }
.wae-new    { background: #2e8b57; color: #fff; }                   /* needed — solid green */
.wae-worked { background: transparent; color: #5cc98c; border: 1px solid #2e8b57; }  /* worked, unconfirmed */
/* "Calling you" — a station transmitting MY callsign right now. Red row, white
   text, floated to the top of the roster. The whole point is to not miss it. */
tr.calling-me td { background: #d11 !important; color: #fff !important; }
tr.calling-me td.dx { font-weight: 800; }
tr.calling-me:hover td { background: #e22 !important; }
.callsyou { display: inline-block; padding: 0 0.35em; margin-left: 0.4em; border-radius: 3px;
  background: #fff; color: #c0392b; font-size: 0.66em; font-weight: 800;
  letter-spacing: 0.04em; vertical-align: middle; animation: callsyou-pulse 1s ease-in-out infinite; }
@keyframes callsyou-pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.45; } }

/* Most Wanted progress matrix — full-width scoreboard below the score cards. */
.rare-matrix { margin: 1.2em 0 0; }
.rare-matrix > summary { cursor: pointer; color: #ff0; font-size: 0.85em; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.05em; padding: 0.3em 0; }
.rm-controls { margin: 0.5em 0; font-size: 0.8em; color: #888; }
.rm-controls button { background: #1a1a1a; color: #aaa; border: 1px solid #333; border-radius: 3px;
  padding: 0.15em 0.6em; margin-right: 0.3em; cursor: pointer; font-family: inherit; font-size: 0.95em; }
.rm-controls button.active { background: #ff0; color: #000; border-color: #ff0; font-weight: 700; }
.rm-scroll { max-height: 72vh; overflow: auto; border: 1px solid #1a1a1a; }
table.rm { border-collapse: collapse; width: auto; font-size: 0.85em; font-variant-numeric: tabular-nums; }
table.rm th { color: #888; font-weight: normal; padding: 0.15em 0.35em; background: #0a0a0a;
  position: sticky; top: 0; border-bottom: 1px solid #222; z-index: 1; }
table.rm td { padding: 0.05em 0.35em; border-bottom: 1px dotted #141414; white-space: nowrap; }
table.rm td.rk  { color: #777; text-align: right; }
table.rm td.ent { color: #ccc; max-width: 15em; overflow: hidden; text-overflow: ellipsis; }
table.rm td.s   { text-align: center; width: 1.6em; color: #333; }
/* Column zebra — alternating columns get a subtle gray so a center cell traces
   cleanly up to its header. Status colors below win on specificity, so the
   confirmed/claimable/etc cells stay vivid on top of the stripe. */
table.rm td:nth-child(even), table.rm th:nth-child(even) { background: #161616; }
table.rm td.s.confirmed { background: #1c5c2e; color: #9f9; }   /* confirmed — green */
table.rm td.s.worked    { background: #5c4a14; color: #fc6; }   /* worked, not confirmed — amber */
table.rm td.s.claimable { background: #0e5a5a; color: #6ff; }   /* matched on Club Log — do OQRS (cyan) */
table.rm td.s.suspect   { background: #6a1b2e; color: #f9a; }   /* logged but not in their log — verify (red) */
/* OQRS claimable / suspect punch-lists above the matrix */
.oqrs-box { margin: 0.6em 0; font-size: 0.85em; }
.oqrs-box > summary { cursor: pointer; font-weight: 600; padding: 0.25em 0; }
.oqrs-box.claim > summary { color: #6ff; }
.oqrs-box.susp  > summary { color: #f9a; }
.oqrs-box.m0oxo > summary { color: #c9f; }   /* M0OXO — separate OQRS channel (violet) */
.oqrs-box .mo-new { color: #1a1a1a; background: #fc6; font-weight: 700; padding: 0 0.35em; border-radius: 3px; font-size: 0.9em; }
.oqrs-box .mo-part { color: #6ff; font-size: 0.9em; }
.oqrs-box .mo-done { color: #6a6; font-size: 0.9em; }
.oqrs-box li.mo-done { opacity: 0.5; }   /* already confirmed — informational, no action */
.oqrs-box ul { margin: 0.3em 0 0.6em 1.2em; padding: 0; list-style: none; }
.oqrs-box li { padding: 0.12em 0; color: #ccc; }
.oqrs-box .rk { color: #777; display: inline-block; width: 3.2em; }
.oqrs-box .mwc { color: #6ad; font-weight: 600; }

/* Per-band mode toggles row */
.band-mode-toggles {
  background: #0a0a0a; padding: 0.4em 0.6em; margin-bottom: 0.4em;
  border-left: 3px solid #444; font-size: 0.85em;
}
.band-mode-toggles label {
  margin-right: 1em; cursor: pointer; user-select: none; color: #ccc;
}
.band-mode-toggles label input { margin-right: 0.3em; vertical-align: middle; }
.band-mode-toggles .empty { color: #666; }

/* Mode DXCC toggle chips (scores panel) */
.mode-chips { display: flex; gap: 0.4em; flex-wrap: wrap; margin: 0.2em 0; }
.mode-chip {
  font-size: 0.82em; padding: 0.15em 0.5em; border-radius: 0.4em;
  cursor: pointer; user-select: none; border: 1px solid #444;
}
.mode-chip.on { background: #1e3320; color: #cfc; border-color: #4a7a4a; }
.mode-chip.off { background: #1a1a1a; color: #888; }
.mode-chip input { margin-right: 0.3em; vertical-align: middle; }
.mode-hint { font-size: 0.72em; color: #777; margin-top: 0.3em; line-height: 1.3; }
.mode-hint .fb-done { color: #6c6; }
.mode-hint .fb-need { color: #c96; }
.aw-setup { cursor: default; }
.aw-setup summary { cursor: pointer; font-weight: 600; color: #cfcfcf; list-style: none; }
.aw-setup summary::-webkit-details-marker { display: none; }
.aw-setup[open] summary { margin-bottom: 0.5em; }
.aw-group { margin: 0.4em 0; }
.aw-cat { font-size: 0.7em; text-transform: uppercase; letter-spacing: 0.06em; color: #888; margin-bottom: 0.25em; }
.aw-boxes { display: flex; flex-wrap: wrap; gap: 0.25em 0.9em; }
.awtoggle { font-size: 0.8em; color: #bbb; white-space: nowrap; }
.awtoggle input { margin-right: 0.3em; vertical-align: middle; }
.waja-detail summary { cursor: pointer; font-weight: 600; color: #cfcfcf; list-style: none; }
.waja-detail summary::-webkit-details-marker { display: none; }
.waja-detail[open] summary { margin-bottom: 0.55em; }
.pref-grid { display: flex; flex-wrap: wrap; gap: 0.3em; }
.pref {
  font-size: 0.95em; padding: 0.16em 0.34em; border-radius: 3px;
  border: 1px solid transparent; line-height: 1.5; white-space: nowrap; cursor: default;
}
.pref-conf { background: #1e3320; color: #8de08d; border-color: #3f6b3f; }       /* confirmed */
.pref-work { background: #322a14; color: #e0c060; border-color: #6b5a2a; }       /* worked, unconfirmed */
.pref-new  { background: #1a1a1a; color: #5a5a5a; }                              /* needed */

/* Single-band content area */
.band-content { /* container for active band's tables */ }
.band-content .empty { color: #666; padding: 1em; }

/* Header row with gear */
.header-row { display: flex; justify-content: space-between; align-items: center; }
.gear-wrap { position: relative; }
.gear-icon {
  font-size: 1.4em; cursor: pointer; user-select: none;
  list-style: none; outline: none; padding: 0.2em 0.4em;
  color: #888;
}
.gear-icon::-webkit-details-marker { display: none; }
.gear-icon::before { content: ""; }
details[open] .gear-icon { color: #fff; }
.gear-panel {
  position: absolute; right: 0; top: 100%; z-index: 100;
  background: #0a0a0a; border: 1px solid #444; padding: 1em;
  min-width: 380px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.7);
}
.gear-panel h3 { margin: 0 0 0.4em; font-size: 0.85em; color: #ff0; font-weight: normal; }
.gear-panel .group { margin-bottom: 1em; }
.gear-panel .checkbox-grid {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.3em 0.6em;
  font-size: 0.85em;
}
.gear-panel label { color: #ccc; cursor: pointer; user-select: none; white-space: nowrap; }
.gear-panel label input { margin-right: 0.3em; vertical-align: middle; }
.gear-panel .scope-grid {
  display: grid;
  grid-template-columns: 4em repeat(3, minmax(4.5em, auto));  /* overridden inline by renderScopeGrid (data-driven) */
  gap: 0.15em 0.6em;
  font-size: 0.78em;
  align-items: center;
}
.gear-panel .scope-grid .band-label { color: #ff0; font-weight: 600; text-align: right; }
.gear-panel .scope-grid .scope-hdr { color: #aaa; font-size: 0.85em; text-align: center; padding-bottom: 0.2em; border-bottom: 1px solid #333; }
.gear-panel .scope-grid .cell { text-align: center; }
.gear-panel .scope-grid .cell label { display: inline; }
.gear-panel .scope-grid .cell input { margin: 0; }
.gear-panel .scope-grid .cell.disabled { color: #333; }
.gear-panel .scope-help { font-size: 0.7em; color: #888; margin-top: 0.4em; max-width: 36em; }
.gear-panel .actions { font-size: 0.8em; margin-top: 0.5em; }
.gear-panel .actions button {
  background: #222; color: #ccc; border: 1px solid #444;
  padding: 0.2em 0.6em; cursor: pointer; margin-right: 0.4em; font-size: 1em;
}
.gear-panel .actions button:hover { background: #333; color: #fff; }
.gear-panel .sync-row { display: flex; align-items: center; gap: 0.6em; margin: 0.3em 0; }
.gear-panel .sync-row button { background: #1a1a1a; color: #ccc; border: 1px solid #444; padding: 0.2em 0.6em; cursor: pointer; font: inherit; }
.gear-panel .sync-row button:hover:not(:disabled) { background: #333; color: #fff; }
.gear-panel .sync-row button:disabled { opacity: 0.5; cursor: wait; }
.gear-panel .sync-status { font-size: 0.78em; color: #888; }
.gear-panel .sync-status.ok { color: #5c5; }
.gear-panel .sync-status.err { color: #f88; }

/* Top-level view tabs (Live / Scores / Log search) */
.view-tabs { display: flex; gap: 0.2em; margin-bottom: 0.6em; border-bottom: 1px solid #333; padding-bottom: 0.1em; }
.view-tabs button {
  background: #0a0a0a; color: #aaa; border: 1px solid #222; border-bottom: none;
  padding: 0.4em 1.2em; cursor: pointer; font-size: 0.95em; font-family: inherit;
  outline: none;
}
.view-tabs button:hover { background: #1a1a1a; color: #eee; }
.view-tabs button.active {
  background: #ff0; color: #000; font-weight: 700; border-color: #ff0;
}
.view-section { display: none; }
.view-section.active { display: block; }

/* Scores — multi-column GT-style W/C/Goal table */
.scores-grid {
  /* Masonry-style multi-column: cards keep natural height and pack vertically,
     so short cards don't leave dead space under tall ones (CSS grid forced
     equal row heights + equal widths). Column count adapts to width. */
  column-width: 20em;
  column-gap: 1.2em;
  font-size: 0.85em;
  font-variant-numeric: tabular-nums;
}
.score-card {
  background: #0a0a0a; border: 1px solid #1a1a1a; padding: 0.4em 0.6em;
  break-inside: avoid; -webkit-column-break-inside: avoid; page-break-inside: avoid;
  margin: 0 0 0.7em;   /* vertical gap between stacked cards (column-gap is horizontal only) */
}
.score-card h3 {
  margin: 0 0 0.3em; font-size: 0.85em; color: #ff0;
  font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em;
  border-bottom: 1px solid #222; padding-bottom: 0.2em;
}
/* Collapsible FFMA-tab section headers — styled like .score-card h3 so the
   look is unchanged, but the whole section folds away to anchor a long scroll. */
.ff-collapse > summary {
  margin: 0; font-size: 0.85em; color: #ff0; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.05em; cursor: pointer;
  border-bottom: 1px solid #222; padding-bottom: 0.2em; list-style: none;
}
.ff-collapse[open] > summary { margin-bottom: 0.4em; }
.score-card table { width: 100%; border-collapse: collapse; font-size: 0.95em; }
.score-card th {
  font-weight: normal; color: #888; font-size: 0.75em; text-align: right;
  padding: 0 0.4em 0.1em; border-bottom: 1px solid #1a1a1a; background: transparent;
}
.score-card th:first-child { text-align: left; }
.score-card td { padding: 0.05em 0.4em; border-bottom: 1px dotted #111; }
.score-card td.s-name  { color: #ccc; }
.score-card td.s-w, .score-card td.s-c, .score-card td.s-g { text-align: right; }
.score-card td.s-w     { color: #fb6; }       /* worked = orange (matches scope-needed accent) */
.score-card td.s-c.complete { color: #5c5; font-weight: 600; }
.score-card td.s-c.partial  { color: #ff5; font-weight: 600; }
.score-card td.s-c.empty    { color: #555; }
.score-card td.s-g     { color: #666; }
/* ATNO table has free-text columns (entity names) so its natural min-width is
   wide; pin it to the card width and let cells wrap, so it stays inside the box
   instead of overflowing when the column is narrow. */
.score-card table.atno-tbl { table-layout: fixed; }
.score-card table.atno-tbl th, .score-card table.atno-tbl td { overflow-wrap: anywhere; white-space: normal; }
/* Contacts-by-mode-by-year stacked bars (vanilla CSS, no charting lib) */
.score-card.mychart .mylegend { font-size: 0.72em; color: #aaa; margin: 0.1em 0 0.5em; }
.mychart .myl  { margin-right: 0.9em; white-space: nowrap; }
.mychart .mysw { display: inline-block; width: 0.7em; height: 0.7em; border-radius: 2px; margin-right: 0.3em; vertical-align: middle; }
.mychart .myrow { display: flex; align-items: center; gap: 0.5em; margin: 0.18em 0; font-size: 0.8em; }
.mychart .myyr  { width: 2.7em; color: #ccc; text-align: right; flex: 0 0 auto; }
.mychart .mybar { flex: 1 1 auto; display: flex; height: 1.2em; border-radius: 2px; overflow: hidden; background: #1a1a1a; }
.mychart .myseg { display: flex; align-items: center; justify-content: center; font-size: 0.8em; color: #111; font-weight: 600; overflow: hidden; min-width: 0; }
.mychart .mytot { width: 3.6em; text-align: right; color: #999; flex: 0 0 auto; }
.scores-totals { color: #888; font-size: 0.85em; margin-top: 1em; }

.log-search-input, .log-search-select {
  background: #1a1a1a; color: #eee; border: 1px solid #333;
  padding: 0.3em 0.5em; font-family: inherit; font-size: 0.9em;
}
.log-search-input { width: 9em; }
.log-search-input.wide { width: 13em; }
.log-search-input:focus, .log-search-select:focus { outline: none; border-color: #ff0; }
.log-search-filters {
  display: flex; gap: 0.4em; flex-wrap: wrap; align-items: center; margin-bottom: 0.4em;
}
.log-search-filters label { color: #888; font-size: 0.8em; }
.log-search-meta { color: #888; font-size: 0.85em; margin-left: 0.6em; flex: 1; }
.log-search-pager { display: flex; gap: 0.4em; align-items: center; font-size: 0.85em; color: #aaa; }
.log-search-pager button {
  background: #1a1a1a; color: #ccc; border: 1px solid #333;
  padding: 0.2em 0.7em; cursor: pointer; font-family: inherit; font-size: 0.9em;
}
.log-search-pager button:hover:not(:disabled) { background: #2a2a2a; color: #fff; }
.log-search-pager button:disabled { opacity: 0.3; cursor: default; }
.log-search-results { margin-top: 0.4em; border-top: 1px solid #1a1a1a; }
.log-search-results table { font-size: 0.82em; }
.log-search-results td.q-call { color: #5cf; font-weight: 600; }
.log-search-results td.q-conf.lotw { color: #5c5; }
.log-search-results td.q-conf.unconf { color: #777; }
.log-search-results .empty { color: #666; padding: 0.5em; }
.log-search-clear {
  color: #888; font-size: 0.8em; cursor: pointer; user-select: none;
  padding: 0.2em 0.5em;
}
.log-search-clear:hover { color: #ff0; }

/* ---- Mobile / narrow screens (iPhone over Tailscale, couch tablet) ---- */
@media (max-width: 640px) {
  body { margin: 0.45em; }
  h1 { font-size: 1.25em; }
  /* bump the dense desktop table sizes up to something readable on a phone */
  table { font-size: 0.92em; }
  th { font-size: 0.82em; }
  /* the wide spots table scrolls inside its own box instead of blowing out
     the page width (which otherwise makes the whole layout pan/zoom) */
  .band-content { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  /* view tabs wrap to multiple rows with finger-sized targets */
  .view-tabs { flex-wrap: wrap; gap: 0.3em; }
  .view-tabs button { flex: 1 0 auto; padding: 0.6em 0.5em; font-size: 1em; }
  .tab-strip button { padding: 0.5em 0.7em; font-size: 0.95em; }
  /* controls: stack the checkboxes (kill the inline margin-left) + bigger boxes */
  .controls label { display: block; margin: 0.7em 0 0 !important; line-height: 1.6; }
  .controls input[type="checkbox"] { transform: scale(1.3); margin-right: 0.5em; vertical-align: middle; }
  .band-activity-link { display: inline-block; margin: 0.7em 0 0 !important; padding: 0.4em 0; }
  .legend { flex-wrap: wrap; gap: 0.5em 1em; margin-top: 0.6em; }
  /* score cards: single full-width column */
  .scores-grid { columns: 1; column-width: auto; }
  /* log search: fixed-width inputs -> fill the row; pager wraps; results scroll */
  .log-search-input, .log-search-select {
    width: auto; flex: 1 1 8em; font-size: 1em; padding: 0.45em 0.5em;
  }
  .log-search-input.wide { flex: 1 1 100%; }      /* entity field = its own row */
  .log-search-meta { flex: 1 1 100%; margin-left: 0; }
  .log-search-pager { flex-wrap: wrap; gap: 0.45em; }
  .log-search-pager button { padding: 0.45em 0.8em; font-size: 0.95em; }
  .log-search-results { overflow-x: auto; -webkit-overflow-scrolling: touch; }
}

/* Push-alerts config panel */
.alerts-panel { margin: 0.4em 0 0.6em; border: 1px solid #1a1a1a; background: #080808; padding: 0.2em 0.6em; }
.alerts-panel > summary { cursor: pointer; color: #ff0; font-size: 0.85em; font-weight: 600; list-style: none; }
.alerts-panel > summary::-webkit-details-marker { display: none; }
.alerts-body { margin: 0.5em 0; font-size: 0.85em; }
.alerts-warn { color: #fb6; margin-bottom: 0.5em; }
.alerts-warn code { color: #ffd; }
.alerts-top { margin-bottom: 0.5em; }
.alerts-saved { color: #5c5; margin-left: 0.8em; font-size: 0.85em; }
.alerts-credrow { margin-bottom: 0.4em; }
.alerts-ok { color: #5c5; font-weight: 600; }
.alerts-credrow a { color: #6cf; }
.alerts-creds { margin-bottom: 0.6em; padding: 0.4em 0.5em; background: #0d0d0d; border: 1px solid #1a1a1a; }
.alerts-creds input { background: #1a1a1a; color: #eee; border: 1px solid #333; padding: 0.3em 0.5em; margin-right: 0.4em; font-family: inherit; font-size: 0.9em; width: 14em; max-width: 45%; }
.alerts-creds button { background: #1a1a1a; color: #ccc; border: 1px solid #333; padding: 0.3em 0.8em; cursor: pointer; font-family: inherit; }
.alerts-creds button:hover { background: #2a2a2a; color: #fff; }
table.alerts-matrix { border-collapse: collapse; font-variant-numeric: tabular-nums; }
table.alerts-matrix th, table.alerts-matrix td { padding: 0.1em 0.55em; text-align: center; border-bottom: 1px solid #141414; }
table.alerts-matrix th { color: #ff0; font-size: 0.78em; }
table.alerts-matrix th.alerts-sub { color: #888; font-weight: normal; font-size: 0.68em; }
table.alerts-matrix td.alerts-band, table.alerts-matrix th:first-child { text-align: right; color: #aaa; }
table.alerts-matrix input[type="checkbox"] { margin: 0; }
</style>
</head><body>
<div class="header-row">
  <h1>Grayline — live from GoCluster</h1>
  <details class="gear-wrap">
    <summary class="gear-icon">⚙</summary>
    <div class="gear-panel">
      <div class="group">
        <h3>Bands</h3>
        <div class="checkbox-grid" id="settings_bands"></div>
      </div>
      <div class="group">
        <h3>Modes</h3>
        <div class="checkbox-grid" id="settings_modes"></div>
      </div>
      <div class="group">
        <h3>Award scopes per band</h3>
        <div class="scope-grid" id="settings_scopes"></div>
        <div class="scope-help">
          ARRL-tracked award scopes only. Personal goals
          (DXCC-FT8-only, etc.) coming as a separate Custom Goals
          section.
        </div>
      </div>
      <div class="group">
        <h3>Spotter radius per band (mi)</h3>
        <div class="checkbox-grid" id="settings_radius"></div>
        <div class="scope-help">
          Applies when "Local spotters only" is checked. Blank = tiered
          default (HF 300, 6m+ 150); enter a value to override a band.
        </div>
      </div>
      <div class="group">
        <h3>Log Sync</h3>
        <div class="sync-row">
          <button id="sync_qrz">Sync QRZ Logbook</button>
          <span class="sync-status" id="sync_qrz_status">…</span>
        </div>
        <div class="sync-row">
          <button id="sync_lotw">Sync LoTW</button>
          <span class="sync-status" id="sync_lotw_status">…</span>
        </div>
        <div class="scope-help">QRZ is a full manual pull; LoTW also auto-syncs hourly. Pills refresh on sync.</div>
      </div>
      <div class="actions">
        <button id="settings_all_bands">All bands</button>
        <button id="settings_no_bands">No bands</button>
        <button id="settings_all_modes">All modes</button>
        <button id="settings_no_modes">No modes</button>
        <button id="settings_reset_scopes">Reset scopes to defaults</button>
        <button id="settings_reset_radii">Reset radii to defaults</button>
      </div>
    </div>
  </details>
</div>
<div class="status" id="status">Loading…</div>
<div class="view-tabs">
  <button data-view="live" class="active">Live</button>
  <button data-view="scores">Scores</button>
  <button data-view="ffma">FFMA</button>
  <button data-view="rares">Most Wanted</button>
  <button data-view="log">Log search</button>
</div>
<div class="view-section active" id="view_live">
  <div class="controls">
    <label><input type="checkbox" id="show_wanted"> Show wanted only</label>
    <label style="margin-left:1em" title="CQ DX Marathon — annual, worked-based: entities + CQ zones not yet worked this year. Official 346-entity / 40-zone list."><input type="checkbox" id="marathon_on"> DX Marathon needed (official 346/40)</label>
    <label style="margin-left:1em" title="WAE (Worked All Europe, DARC) — pill only when a spotted station is a WAE country you still NEED (unconfirmed). Silent on WAE countries you've already confirmed."><input type="checkbox" id="wae_on"> WAE needed</label>
    <label style="margin-left:1em" title="Re-weight FFMA grid rarity by reachability from your QTH (HOME_GRID): globally-rare grids in your single-hop Es sweet spot get discounted; far/double-hop ones amplified. Adds the Es-hop band (1H/2H) to the badge."><input type="checkbox" id="ffma_qth"> FFMA: rare for my QTH</label>
    <label style="margin-left:1em"><input type="checkbox" id="filter300"> Local spotters only (HF &le;300 mi, VHF+ &le;150 mi of __MY_GRID__)</label>
    <a href="/bands" target="_blank" rel="noopener" class="band-activity-link" title="Open the Band Activity bar graph (CW/SSB/Digital × contest bands, local-filtered) in a new tab — a contest 'which band is hot' tool, kept off the main page">&#x1F4CA; Band Activity &#x2197;</a>
    <span class="legend">
      <span style="color:#f0f">callsign new</span> ·
      <span style="color:#ff5">worked</span> ·
      <span style="color:#5c5">confirmed</span> ·
      <span style="color:#000;background:#ffa500;padding:0 4px;border-radius:2px">scope needed</span>
    </span>
  </div>
  <details class="alerts-panel" id="alerts_panel">
    <summary>&#x1F514; Push alerts <span id="alerts_state" class="ff-count"></span></summary>
    <div id="alerts_body" class="alerts-body">Loading…</div>
  </details>
  <div class="tab-strip" id="tab_strip"></div>
  <div class="band-mode-toggles" id="band_mode_toggles"></div>
  <div class="band-content" id="band_content"></div>
</div>
<div class="view-section" id="view_scores">
  <div class="scores-grid" id="scores_grid">Loading…</div>
  <div class="scores-totals" id="scores_totals"></div>
</div>
<div class="view-section" id="view_ffma">
  <div id="ffma_panel">Loading…</div>
</div>
<div class="view-section" id="view_rares">
  <div id="rare_matrix">Loading…</div>
</div>
<div class="view-section" id="view_log">
  <div class="log-search-filters">
    <input type="text" id="lf_call" class="log-search-input" placeholder="callsign…" autocomplete="off">
    <select id="lf_band" class="log-search-select">
      <option value="">any band</option>
      <option value="160m">160m</option><option value="80m">80m</option>
      <option value="60m">60m</option><option value="40m">40m</option>
      <option value="30m">30m</option><option value="20m">20m</option>
      <option value="17m">17m</option><option value="15m">15m</option>
      <option value="12m">12m</option><option value="10m">10m</option>
      <option value="6m">6m</option><option value="2m">2m</option>
      <option value="1.25m">1.25m</option><option value="70cm">70cm</option>
      <option value="33cm">33cm</option><option value="23cm">23cm</option>
      <option value="13cm">13cm</option><option value="9cm">9cm</option>
      <option value="6cm">6cm</option><option value="3cm">3cm</option>
    </select>
    <select id="lf_mode" class="log-search-select">
      <option value="">any mode</option>
      <option value="CW">CW</option>
      <option value="FT8">FT8</option><option value="FT4">FT4</option>
      <option value="SSB">SSB</option><option value="USB">USB</option>
      <option value="LSB">LSB</option><option value="AM">AM</option>
      <option value="FM">FM</option>
      <option value="RTTY">RTTY</option><option value="PSK31">PSK31</option>
      <option value="MFSK">MFSK</option><option value="JT65">JT65</option>
      <option value="JT9">JT9</option><option value="MSK144">MSK144</option>
      <option value="Q65">Q65</option><option value="JS8">JS8</option>
    </select>
    <input type="text" id="lf_dxcc" class="log-search-input wide" placeholder="entity (e.g. Russia)" autocomplete="off">
    <input type="text" id="lf_grid" class="log-search-input" placeholder="grid (e.g. FN31)" autocomplete="off">
    <span class="log-search-clear" id="lf_clear">clear</span>
    <span class="log-search-meta" id="log_search_meta"></span>
  </div>
  <div class="log-search-pager">
    <label>per page</label>
    <select id="lf_pagesize" class="log-search-select">
      <option value="25">25</option>
      <option value="50">50</option>
      <option value="100" selected>100</option>
      <option value="200">200</option>
      <option value="500">500</option>
    </select>
    <button id="lf_first">«</button>
    <button id="lf_prev">‹ Prev</button>
    <span id="lf_pageinfo">page 1 / 1</span>
    <button id="lf_next">Next ›</button>
    <button id="lf_last">»</button>
  </div>
  <div class="log-search-results" id="log_search_results"></div>
</div>
<script>
// Operator identity, injected from config.json at serve time.
const MY_CALLSIGN = "__MY_CALLSIGN__".toUpperCase();
const PEER_RADIUS_MI = __PEER_RADIUS_MI__;
const BAND_ORDER = ["3cm","6cm","9cm","13cm","23cm","33cm","70cm","1.25m","2m","6m","10m","12m","15m","17m","20m","30m","40m","60m","80m","160m"];
// Per-band award scopes — drives which cells get the orange highlight treatment.
const DXCC_BANDS = new Set(["160m","80m","60m","40m","30m","20m","17m","15m","12m","10m","6m"]);
const GRID_BANDS = new Set(["6m","2m","1.25m","70cm","33cm","23cm","13cm","9cm","6cm","3cm"]);
// Per-band spotter radius. Tiered defaults: HF = 300 mi; 6m and up
// (VHF/UHF) = 150 mi (localized — nearer spotters are the signal).
// Unlisted bands fall back to 300. Any band can be overridden in the
// settings panel; overrides persist in localStorage and win over the
// tiered default.
const RADIUS_HF_MI = 300;
const RADIUS_VHF_MI = 150;
const VHF_PLUS_BANDS = new Set(
  ["6m","2m","1.25m","70cm","33cm","23cm","13cm","9cm","6cm","3cm","1.25cm"]);
function defaultRadiusForBand(band){
  return VHF_PLUS_BANDS.has(band) ? RADIUS_VHF_MI : RADIUS_HF_MI;
}
let bandRadiusOverride = {};
try {
  bandRadiusOverride =
    JSON.parse(localStorage.getItem("grayline_band_radius") || "{}") || {};
} catch (e) { bandRadiusOverride = {}; }
function saveBandRadius(){
  localStorage.setItem("grayline_band_radius",
    JSON.stringify(bandRadiusOverride));
}
function radiusForBand(band){
  const o = bandRadiusOverride[band];
  return (typeof o === "number" && o > 0) ? o : defaultRadiusForBand(band);
}
// Common ADIF modes — pre-seed the gear-panel "Modes" list so users can
// disable rare modes (WSPR, SSTV, etc.) BEFORE the first spot of that mode
// arrives, instead of having to wait for traffic to reveal the checkbox.
// Rare modes still appear dynamically when their spots land. Order is
// roughly: voice, CW, then digital from most-common to less. The actual
// display sort is alphabetical so this just controls which checkboxes are
// guaranteed to be present.
const COMMON_MODES = [
  "CW", "SSB", "USB", "LSB", "AM", "FM",
  "FT8", "FT4", "RTTY", "PSK31", "JS8", "MSK144",
  "JT65", "JT9", "Q65", "FST4", "WSPR"
];

// FFMA — Fred Fish Memorial Award. 488 CONUS-48 grid squares to be
// worked on 6m. The grid list is canonical from ARRL, injected at server
// render time from data/ffma_grids.json.
const FFMA_GRIDS = new Set([__FFMA_GRIDS_INJECT__]);
function isFfmaGrid(grid4) {
  if (!grid4) return false;
  return FFMA_GRIDS.has(grid4.toUpperCase().slice(0, 4));
}
function bandIdx(b) { const i = BAND_ORDER.indexOf(b); return i < 0 ? 99 : i; }
function fmtAge(s) {
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s/60) + "m";
  return Math.floor(s/3600) + "h";
}
function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// Persist the filter toggle in localStorage
const filterCB = document.getElementById("filter300");
filterCB.checked = localStorage.getItem("grayline_filter300") === "1";
filterCB.addEventListener("change", () => {
  localStorage.setItem("grayline_filter300", filterCB.checked ? "1" : "0");
  refresh();
});

// "Show wanted" filter — show only spots where any enabled scope status === 'new'
const showWantedCB = document.getElementById("show_wanted");
showWantedCB.checked = localStorage.getItem("grayline_show_wanted") === "1";
showWantedCB.addEventListener("change", () => {
  localStorage.setItem("grayline_show_wanted", showWantedCB.checked ? "1" : "0");
  refresh();
});

// DX Marathon (opt-in, CQ award — off by default like the other non-ARRL extras).
const marathonCB = document.getElementById("marathon_on");
marathonCB.checked = localStorage.getItem("grayline_marathon") === "1";
marathonCB.addEventListener("change", () => {
  localStorage.setItem("grayline_marathon", marathonCB.checked ? "1" : "0");
  refresh();
});

const waeCB = document.getElementById("wae_on");
waeCB.checked = localStorage.getItem("grayline_wae") === "1";
waeCB.addEventListener("change", () => {
  localStorage.setItem("grayline_wae", waeCB.checked ? "1" : "0");
  refresh();
});

// FFMA Tier 1: personalize grid rarity to HOME_GRID reachability (opt-in overlay).
const ffmaQthCB = document.getElementById("ffma_qth");
ffmaQthCB.checked = localStorage.getItem("grayline_ffma_qth") === "1";
ffmaQthCB.addEventListener("change", () => {
  localStorage.setItem("grayline_ffma_qth", ffmaQthCB.checked ? "1" : "0");
  refresh();
});

// ---- Per-band award scope settings (replaces mode_aware + needed_only) ----
//
// Each band has a set of available scopes; each scope is independently toggleable.
// Defaults follow the ARRL-tracked-only principle: only ship scopes ARRL actually
// issues as awards, with sane defaults that match what most operators chase out
// of the box. Personal goals (DXCC-FT8-only, etc.) live in a future Custom
// Goals section — not in default settings.
//
// Per-band scopes are band-slot awards only (one QSO per band, any mode):
//   HF (160m–10m):     DXCC-Mixed
//   6m:                DXCC-Mixed, VUCC
//   2m and above:      VUCC
// The mode DXCCs (CW/Phone/Digital) are entity-level GLOBAL awards (one QSO per
// mode, any band) — they live in the scores-panel Mode-DXCC toggles, not here.
//
// FFMA (6m CONUS-48 grids) is intentionally not yet exposed — needs the
// strict-grid-list constraint wired up first.
// Per-band grid = the band-slot award only (Challenge / 5BDXCC / DXCC-Mixed).
// "band slot is band slot" — one QSO per band, any mode, fills it.
const ALL_DXCC_SCOPES = ["DXCC-Mixed"];
// Mode DXCCs (CW/Phone/Digital) are ENTITY-level (any band) GLOBAL awards — one
// QSO per mode on any band earns the slot. They render as pills gated on the
// award still being active (<100 confirmed), not as per-band toggles. The
// entity×band×mode grain is a personal goal, off by default (the data lives in
// dxcc_band_modeclass_status, intentionally unused here).
const MODE_SCOPE_OF = {CW: "DXCC-CW", Phone: "DXCC-Phone", Digital: "DXCC-Digital"};
const MODE_DXCC_TARGET = 100;
// Grid-family scopes. FFMA is 6m only (CONUS-48 grids); VUCC is 6m+ (any grid).
const ALL_GRID_SCOPES = ["FFMA", "VUCC"];

function availableScopesForBand(band) {
  const scopes = [];
  if (DXCC_BANDS.has(band)) scopes.push(...ALL_DXCC_SCOPES);
  if (band === "6m") scopes.push("FFMA");
  if (GRID_BANDS.has(band)) scopes.push("VUCC");
  return scopes;
}

function defaultScopesForBand(band) {
  const out = {};
  if (DXCC_BANDS.has(band)) out["DXCC-Mixed"] = true;
  if (band === "6m") out["FFMA"] = true;
  if (GRID_BANDS.has(band)) out["VUCC"] = true;
  return out;
}

// Award scope storage uses explicit true/false per scope-band combo (rather
// than delete-on-off) so future scope additions can fill in defaults without
// clobbering user choices. A scope key not in stored prefs is treated as
// "never seen by this user," and gets its default state on next load.
function loadAwardScopes() {
  let stored = null;
  try {
    const raw = localStorage.getItem("grayline_award_scopes");
    if (raw) stored = JSON.parse(raw);
  } catch (e) { /* ignored */ }
  const out = {};
  for (const b of BAND_ORDER) {
    const defaults = defaultScopesForBand(b);
    const userPrefs = (stored && stored[b]) || {};
    out[b] = {};
    for (const sc of availableScopesForBand(b)) {
      if (sc in userPrefs) {
        out[b][sc] = !!userPrefs[sc];               // user has explicit choice
      } else {
        out[b][sc] = !!defaults[sc];                // new scope, take default state
      }
    }
  }
  return out;
}
function saveAwardScopes(scopes) {
  localStorage.setItem("grayline_award_scopes", JSON.stringify(scopes));
}
let awardScopes = loadAwardScopes();

// Mode DXCC (CW/Phone/Digital) — global, entity-level. Confirmed counts arrive
// from /api/scores (j.dxcc); a mode auto-retires (stops highlighting spots) at
// 100 confirmed. modeOverrides holds explicit user choices — re-arm to chase
// endorsements past 100, or mute a mode early; absent → automatic behavior.
let modeAwardConfirmed = {};   // {CW: 132, Phone: 65, Digital: 209}
function loadModeOverrides() {
  try { return JSON.parse(localStorage.getItem("grayline_mode_overrides") || "{}"); }
  catch (e) { return {}; }
}
let modeOverrides = loadModeOverrides();
function saveModeOverrides() {
  localStorage.setItem("grayline_mode_overrides", JSON.stringify(modeOverrides));
}
function modeAwardActive(key) {
  if (key in modeOverrides) return !!modeOverrides[key];   // explicit user choice
  const c = modeAwardConfirmed[key];
  if (c == null) return true;                              // unknown until scores load → show
  return c < MODE_DXCC_TARGET;                             // auto-retire at target
}

// ===== Scores-panel award visibility (the "Scores setup" toggles) =====
// Which award rows appear in the Scores panel. ARRL-tracked awards are ON by
// default; JARL / CQ / personal awards are OPT-IN (off) — the ARRL-default +
// personal-extension rule. User choices persist in localStorage and win over
// the default. Grouped by issuing org for the settings UI.
const AWARD_DEFS = [
  // key,            label,              cat,     default-on
  ["dxcc_mixed",     "DXCC Mixed",       "ARRL",  true],
  ["dxcc_cw",        "DXCC CW",          "ARRL",  true],
  ["dxcc_phone",     "DXCC Phone",       "ARRL",  true],
  ["dxcc_digital",   "DXCC Digital",     "ARRL",  true],
  ["dxcc_satellite", "DXCC Satellite",   "ARRL",  true],
  ["dxcc_by_band",   "DXCC by Band",     "ARRL",  true],
  ["challenge",      "DXCC Challenge",   "ARRL",  true],
  ["nbdxcc",         "5BDXCC / NBDXCC",  "ARRL",  true],
  ["honor_roll",     "DXCC Honor Roll",  "ARRL",  true],
  ["was",            "WAS",              "ARRL",  true],
  ["5bwas",          "5BWAS",            "ARRL",  true],
  ["was_cw",         "WAS CW",           "ARRL",  true],
  ["was_phone",      "WAS Phone",        "ARRL",  true],
  ["was_digital",    "WAS Digital",      "ARRL",  true],
  ["was_band",       "WAS by Band",      "ARRL",  true],
  ["triple_play",    "Triple Play",      "ARRL",  true],
  ["wac",            "WAC",              "ARRL",  true],
  ["ffma",           "FFMA (6m)",        "ARRL",  true],
  ["vucc",           "VUCC by band",     "ARRL",  true],
  ["vucc_satellite", "VUCC Satellite",   "ARRL",  true],
  ["waz",            "WAZ",              "CQ",    false],
  ["wpx",            "WPX (prefixes)",   "CQ",    false],
  ["wae",            "WAE (Europe)",     "DARC",  false],
  ["waja",           "WAJA (Japan)",     "JARL",  false],
];
const AWARD_DEFAULT = Object.fromEntries(AWARD_DEFS.map(d => [d[0], d[3]]));
const AWARD_LABEL   = Object.fromEntries(AWARD_DEFS.map(d => [d[0], d[1]]));
function loadAwardVis() {
  try { return JSON.parse(localStorage.getItem("grayline_award_visibility") || "{}"); }
  catch (e) { return {}; }
}
let awardVis = loadAwardVis();
function saveAwardVis() {
  localStorage.setItem("grayline_award_visibility", JSON.stringify(awardVis));
}
function awardOn(key) {
  if (key in awardVis) return !!awardVis[key];             // explicit user choice
  return AWARD_DEFAULT[key] !== false;                     // default (ARRL on, else off)
}
let lastScores = null;          // most recent /api/scores payload — for instant re-render on toggle
let lastWorkedRev = null;       // server worked-state revision from /spots.json; change => re-fetch scores
let scoresSetupOpen = false;    // keep the "Scores setup" panel open across re-renders
let wajaGridOpen = false;       // keep the WAJA prefecture grid open across re-renders
let ffAtnoOpen = true;          // FFMA tab collapsible sections — default expanded,
let ffPendOpen = true;          // collapse state persists across re-renders so the
let ffRaresOpen = true;         // headers act as anchors on a long scroll
let ffDiscOpen = true;

// WAJA — the 47 Japanese prefectures by ADIF code → [kanji (with 都/道/府/県
// suffix), romaji]. Codes are the ADIF Primary-Subdivision scheme (Tokyo=10),
// joined to authoritative kanji by NAME (not ISO number). Used to render the
// kanji prefecture grid under the WAJA award row.
const JA_PREFECTURES = [
  ["01","北海道","Hokkaido"],["02","青森県","Aomori"],["03","岩手県","Iwate"],
  ["04","秋田県","Akita"],["05","山形県","Yamagata"],["06","宮城県","Miyagi"],
  ["07","福島県","Fukushima"],["08","新潟県","Niigata"],["09","長野県","Nagano"],
  ["10","東京都","Tokyo"],["11","神奈川県","Kanagawa"],["12","千葉県","Chiba"],
  ["13","埼玉県","Saitama"],["14","茨城県","Ibaraki"],["15","栃木県","Tochigi"],
  ["16","群馬県","Gunma"],["17","山梨県","Yamanashi"],["18","静岡県","Shizuoka"],
  ["19","岐阜県","Gifu"],["20","愛知県","Aichi"],["21","三重県","Mie"],
  ["22","京都府","Kyoto"],["23","滋賀県","Shiga"],["24","奈良県","Nara"],
  ["25","大阪府","Osaka"],["26","和歌山県","Wakayama"],["27","兵庫県","Hyogo"],
  ["28","富山県","Toyama"],["29","福井県","Fukui"],["30","石川県","Ishikawa"],
  ["31","岡山県","Okayama"],["32","島根県","Shimane"],["33","山口県","Yamaguchi"],
  ["34","鳥取県","Tottori"],["35","広島県","Hiroshima"],["36","香川県","Kagawa"],
  ["37","徳島県","Tokushima"],["38","愛媛県","Ehime"],["39","高知県","Kochi"],
  ["40","福岡県","Fukuoka"],["41","佐賀県","Saga"],["42","長崎県","Nagasaki"],
  ["43","熊本県","Kumamoto"],["44","大分県","Oita"],["45","宮崎県","Miyazaki"],
  ["46","鹿児島県","Kagoshima"],["47","沖縄県","Okinawa"],
];
// code -> {romaji, kanji} for WAJA pill tooltips (English name on hover).
const JA_PREF_BY_CODE = Object.fromEntries(
  JA_PREFECTURES.map(([code, kanji, romaji]) => [code, {romaji, kanji}]));

function isScopeEnabled(band, scope) {
  return !!(awardScopes[band] && awardScopes[band][scope] === true);
}

function setScopeEnabled(band, scope, enabled) {
  if (!awardScopes[band]) awardScopes[band] = {};
  awardScopes[band][scope] = !!enabled;             // explicit true OR false
  saveAwardScopes(awardScopes);
}

function resetAwardScopesToDefaults() {
  awardScopes = {};
  for (const b of BAND_ORDER) {
    awardScopes[b] = {};
    const defaults = defaultScopesForBand(b);
    for (const sc of availableScopesForBand(b)) {
      awardScopes[b][sc] = !!defaults[sc];
    }
  }
  saveAwardScopes(awardScopes);
}

// Status of a specific (spot, scope) pair, or null if the scope doesn't apply.
// A spot only contributes to a scope it can actually advance — a 17m FT8 spot
// can advance DXCC-Mixed and DXCC-Digital but NOT DXCC-CW (working FT8 doesn't
// earn DXCC-CW credit), so DXCC-CW returns null on it.
function scopeStatus(s, scope) {
  switch (scope) {
    case "DXCC-Mixed":
      return DXCC_BANDS.has(s.band) && s.country ? (s.dxcc_band_status || "new") : null;
    // Mode DXCCs are ENTITY-level (any band) and GLOBAL — they advance only on
    // a spot of their own mode, only while the award is still active (<100
    // confirmed), and use the entity-level status (have I had this entity on
    // this mode on ANY band), NOT the per-band-mode personal-goal status.
    case "DXCC-CW":
      return DXCC_BANDS.has(s.band) && s.country && s.modeclass === "CW" && modeAwardActive("CW")
        ? (s.dxcc_modeclass_status || "new") : null;
    case "DXCC-Phone":
      return DXCC_BANDS.has(s.band) && s.country && s.modeclass === "Phone" && modeAwardActive("Phone")
        ? (s.dxcc_modeclass_status || "new") : null;
    case "DXCC-Digital":
      return DXCC_BANDS.has(s.band) && s.country && s.modeclass === "Digital" && modeAwardActive("Digital")
        ? (s.dxcc_modeclass_status || "new") : null;
    case "VUCC":
      return GRID_BANDS.has(s.band) && s.grid ? (s.grid_band_status || "new") : null;
    case "FFMA":
      // FFMA is 6m only and counts only CONUS-48 grids. A 6m spot from a
      // ZL3 or EU grid is irrelevant to FFMA progress, so the scope returns
      // null for those — no FFMA pill on those rows.
      return s.band === "6m" && s.grid && isFfmaGrid(s.grid)
        ? (s.grid_band_status || "new") : null;
    default:
      return null;
  }
}

// Award pills for a spot. Returns only the scopes that (a) the user has
// enabled for this band AND (b) this spot can actually advance.
// Club Log Most Wanted rarity badge. Shows for entities in the top
// RARITY_MAX_RANK; tiered color so the true grails stand out. Rank rides on
// s.dxcc_rank (joined by DXCC number on the backend).
const RARITY_MAX_RANK = 150;
function rarityBadge(s) {
  const r = s.dxcc_rank;
  if (!r || r > RARITY_MAX_RANK) return "";
  const tier = r <= 10 ? "mw-hot" : (r <= 50 ? "mw-warm" : "mw-cool");
  return ` <span class="mw ${tier}" title="Club Log Most Wanted #${r}">#${r}</span>`;
}

// LoTW-user badge. s.lotw_days = days since this call's last LoTW upload (null if
// they don't use LoTW). Fresh uploader (<= LOTW_FRESH_DAYS) => good bet, the QSO
// will confirm; stale user => dimmed. Non-user => no badge. Threshold is injected
// from config so a recency slider can drive it later.
const LOTW_FRESH_DAYS = parseInt("__LOTW_FRESH_DAYS__", 10) || 365;
// CQ DX Marathon badge — shows only when the Marathon toggle is on AND this spot
// is a new entity and/or new zone for the current year. s.marathon is computed
// server-side against the official 346/40 list (WAE-aware).  'DXCC' | 'CQz' | 'DX+CQz'
function marathonBadge(s) {
  if (!marathonCB.checked || !s.marathon) return "";
  const label = s.marathon === "DX+CQz" ? "MARA DX+Z" : (s.marathon === "CQz" ? "MARA Z" : "MARA");
  const title = s.marathon === "DX+CQz" ? "DX Marathon: new entity AND new zone this year"
              : (s.marathon === "CQz" ? "DX Marathon: new CQ zone this year"
              : "DX Marathon: new entity this year");
  return ` <span class="mara" title="${title}">${label}</span>`;
}

// WAE (Worked All Europe) pill — HIGH-SIGNAL: shows only when a spotted station
// is a WAE country you still NEED (not confirmed). Silent on confirmed WAE
// countries (most EU spots), so when it lights up it actually means something.
// s.wae_status is 'new'|'worked'|'confirmed'|null (server-side, dxcc=False).
function waeBadge(s) {
  if (!waeCB.checked || !s.wae_status || s.wae_status === "confirmed") return "";
  const needNew = s.wae_status === "new";
  const label = needNew ? "WAE" : "WAE?";
  const title = needNew
    ? `WAE country you still NEED: ${s.wae_name || ""} (not worked)`
    : `WAE country worked but NOT confirmed: ${s.wae_name || ""} — chase the QSL`;
  return ` <span class="wae-pill ${needNew ? "wae-new" : "wae-worked"}" title="${title}">${label}</span>`;
}

// FFMA grid-rarity badge (Tier 0, global) — % of FFMA leaders who still need this
// grid, from N7PHY's Leader Board. Only badges uncommon+rare (the 369 common grids
// stay quiet). 6m only (set server-side). The grail tier when it's also a needed grid.
// "Eye" — this station reported decoding YOU to PSKReporter (heard_me set server-side
// from the who-heard-me poll). Means the path is open both ways: worth calling.
function heardEye(s) {
  const h = s.heard_me;
  if (!h) return "";
  const ago = (h.age != null) ? (h.age < 90 ? h.age + "s" : Math.round(h.age / 60) + "m") : "";
  const title = `${escapeHTML(s.dx_call)} decoded YOU and reported it to PSKReporter`
              + (ago ? ` ${ago} ago` : "") + (h.grid ? ` (from ${escapeHTML(h.grid)})` : "")
              + " — path is open both ways, worth calling.";
  return ` <span class="heard-eye" title="${title}">\u{1F441}</span>`;
}

// "Who near me ALSO copied this?" — count of local peers (within PEER_RADIUS_MI)
// who decoded this DX call, hover for their calls + signal reports. The control
// group for "is it me or the band?": neighbors hear it and you don't => your
// station; only one does => his path.
function peerCopiesBadge(s) {
  const pc = s.peer_copies;
  if (!pc || !pc.length) return "";
  const fmt = pc.map(p => {
    const snr = (p.snr === null || p.snr === undefined) ? "" : " " + (p.snr > 0 ? "+" : "") + p.snr;
    const band = (p.band && p.band !== s.band) ? " " + p.band : "";
    return p.peer + snr + band;
  }).join(", ");
  const title = `Also copied within ${PEER_RADIUS_MI} mi by ${pc.length}: ${fmt}`;
  return ` <span class="peer-copies" title="${escapeHTML(title)}">\u{1F442}${pc.length}</span>`;
}

function ffmaRarityBadge(s) {
  const r = s.ffma_rarity;
  if (!r) return "";
  // Tier 1 overlay: when "rare for my QTH" is on, color by the reachability-adjusted
  // personal tier and append the Es-hop band; otherwise the global Tier-0 view.
  const personal = ffmaQthCB.checked && r.personal_tier;
  const tier = personal ? r.personal_tier : r.tier;
  if (tier === "common") return "";
  const pct = (r.pct_needed != null) ? Math.round(r.pct_needed) + "%" : "";
  const cls = tier === "rare" ? "ffr ffr-rare" : "ffr ffr-unc";
  let label = pct;
  let title = `FFMA: ${r.pct_needed}% of leaders still need this grid (${r.leaders_needing})`;
  if (personal && r.distance_mi != null) {
    const code = {"single-hop Es":"1H","single-hop edge":"1H","double-hop Es":"2H","multi-hop":"3H+","scatter (too close)":"SC"}[r.es_band] || "";
    label = pct + (code ? " " + code : "");
    title += ` · ${r.distance_mi} mi from you — ${r.es_band}`;
  } else if (tier === "rare") {
    title += " — top-tier rare";
  }
  return ` <span class="${cls}" title="${title}">${label}</span>`;
}

function lotwBadge(s) {
  const d = s.lotw_days;
  if (d == null) return "";   // not a LoTW user
  const fresh = d <= LOTW_FRESH_DAYS;
  const cls = fresh ? "lotw lotw-fresh" : "lotw lotw-stale";
  const ago = d < 30 ? `${d}d` : (d < 365 ? `${Math.round(d / 30)}mo` : `${(d / 365).toFixed(1)}y`);
  const title = fresh
    ? `LoTW user — uploaded ${ago} ago (good bet to confirm)`
    : `LoTW user but stale — last upload ${ago} ago`;
  return ` <span class="${cls}" title="${title}">LoTW</span>`;
}

// "CQ" tag for digital spots whose latest decode is a CQ — i.e. the station is
// actively calling, not mid-QSO. Derived from the decode message in s.comment,
// which refreshes on every WSJT-X decode, so it clears when they stop CQing.
function cqTag(s) {
  if (s.modeclass !== "Digital") return "";
  const c = (s.comment || "").trim();
  return /^CQ\b/i.test(c)
    ? ` <span class="cqtag" title="Calling CQ — ${escapeHTML(c)}">CQ</span>` : "";
}

function scopeTags(s) {
  const out = [];
  for (const scope of availableScopesForBand(s.band)) {
    if (!isScopeEnabled(s.band, scope)) continue;
    const status = scopeStatus(s, scope);
    if (status === null) continue;
    out.push({ label: scope, status });
  }
  // Global mode DXCC (entity-level, any band) — gated on the award being active,
  // not on a per-band toggle. A spot can only advance its own mode's award.
  const ms = MODE_SCOPE_OF[s.modeclass];
  if (ms) {
    const status = scopeStatus(s, ms);
    if (status !== null) out.push({ label: ms, status, mode: true });
  }
  // WAJA (JARL) — global, any band, opt-in (off by default). The prefecture is
  // resolved best-effort from QRZ addr2 on the backend; waja_status is null when
  // it couldn't be placed, so the pill is silent rather than guessing.
  if (awardOn("waja") && s.waja_status) {
    const p = JA_PREF_BY_CODE[s.waja_pref];
    const title = p ? `WAJA — ${p.romaji} ${p.kanji} (${s.waja_status})` : "WAJA";
    out.push({ label: "WAJA", status: s.waja_status, title });
  }
  return out;
}

// True if any enabled scope for this spot is unconfirmed (still needed for
// the award). Both 'new' (never worked) and 'worked' (worked but not yet
// confirmed via LoTW/QRZ/QSL) count as needed — DXCC/FFMA/VUCC require
// confirmation, not just contact, so a worked-not-confirmed entity isn't
// earned yet. Drives the Show-wanted filter and the per-band wanted/total
// counter.
function anyScopeNeeded(s) {
  return scopeTags(s).some(t => t.status === "new" || t.status === "worked");
}

// Status enum order — used for picking the "weakest" (most-needed) status.
const STATUS_ORDER = { new: 0, worked: 1, confirmed: 2 };

// Cell color status — weakest enabled scope status in the relevant family.
// Drives the orange treatment of the Country and Grid cells. Returns null
// if no scope from that family is enabled (cell stays plain).
function effectiveDxccStatus(s) {
  // The Country cell = the BAND SLOT only (DXCC-Mixed / Challenge). "Band slot
  // is band slot": the cell reflects whether you need the entity on THIS band,
  // any mode. The mode DXCCs are entity-level and show as pills — they must NOT
  // color the band cell (an entity you still need on Phone shouldn't paint the
  // 20m cell orange when the 20m slot is already confirmed).
  for (const t of scopeTags(s)) {
    if (t.label === "DXCC-Mixed") return t.status;
  }
  return null;
}
function effectiveGridStatus(s) {
  let weakest = null;
  for (const t of scopeTags(s)) {
    if (t.label !== "VUCC" && t.label !== "FFMA") continue;
    if (weakest === null || STATUS_ORDER[t.status] < STATUS_ORDER[weakest]) {
      weakest = t.status;
    }
  }
  return weakest;
}

// ---- Settings gear: per-band and per-mode visibility ----
function loadDisabledSet(key) {
  try {
    const raw = localStorage.getItem(key);
    return new Set(raw ? JSON.parse(raw) : []);
  } catch (e) { return new Set(); }
}
function saveDisabledSet(key, set) {
  localStorage.setItem(key, JSON.stringify([...set]));
}
let disabledBands = loadDisabledSet("grayline_disabled_bands");
let disabledModes = loadDisabledSet("grayline_disabled_modes");

// Per-band mode disable: { band: [mode, mode, ...] }
function loadBandModeMap() {
  try {
    const raw = localStorage.getItem("grayline_band_modes_disabled");
    return raw ? JSON.parse(raw) : {};
  } catch (e) { return {}; }
}
function saveBandModeMap(map) {
  localStorage.setItem("grayline_band_modes_disabled", JSON.stringify(map));
}
let bandModesDisabled = loadBandModeMap();
function isBandModeDisabled(band, mode) {
  return (bandModesDisabled[band] || []).includes(mode);
}
function toggleBandMode(band, mode, enabled) {
  if (!bandModesDisabled[band]) bandModesDisabled[band] = [];
  const arr = bandModesDisabled[band];
  if (enabled) {
    bandModesDisabled[band] = arr.filter(m => m !== mode);
  } else if (!arr.includes(mode)) {
    arr.push(mode);
  }
  saveBandModeMap(bandModesDisabled);
}

// Active tab — which band is currently displayed
function getActiveBand() {
  return localStorage.getItem("grayline_active_band") || "";
}
function setActiveBand(b) {
  localStorage.setItem("grayline_active_band", b);
}

function renderSettingsPanel(spots) {
  // Bands: use canonical BAND_ORDER (consistent UI even if no spots yet on a band)
  const bandsBox = document.getElementById("settings_bands");
  bandsBox.innerHTML = BAND_ORDER.map(b =>
    `<label><input type="checkbox" data-band="${b}" ${disabledBands.has(b) ? "" : "checked"}>${b}</label>`
  ).join("");
  bandsBox.querySelectorAll("input[data-band]").forEach(el => {
    el.addEventListener("change", () => {
      const b = el.dataset.band;
      if (el.checked) disabledBands.delete(b); else disabledBands.add(b);
      saveDisabledSet("grayline_disabled_bands", disabledBands);
      refresh();
    });
  });

  // Modes: pre-seed COMMON_MODES so users can configure visibility before
  // any spot of that mode arrives, then merge in any modes from current
  // spots so rare modes (FELDHELL, OLIVIA, etc.) appear when traffic does.
  // Also include any modes already in disabledModes — so an unchecked rare
  // mode keeps its checkbox visible even after traffic dies down (otherwise
  // the user couldn't re-enable it).
  const modesSeen = new Set([...COMMON_MODES, ...spots.map(s => s.mode), ...disabledModes]);
  const sortedModes = [...modesSeen].sort();
  const modesBox = document.getElementById("settings_modes");
  modesBox.innerHTML = sortedModes.map(m =>
    `<label><input type="checkbox" data-mode="${m}" ${disabledModes.has(m) ? "" : "checked"}>${m}</label>`
  ).join("");
  modesBox.querySelectorAll("input[data-mode]").forEach(el => {
    el.addEventListener("change", () => {
      const m = el.dataset.mode;
      if (el.checked) disabledModes.delete(m); else disabledModes.add(m);
      saveDisabledSet("grayline_disabled_modes", disabledModes);
      refresh();
    });
  });

  // Award scopes: per-band grid, columns are scope keys, rows are bands.
  // Cells are checkboxes for scopes that apply to that band; cells are blank
  // (not just disabled) when the scope doesn't apply (e.g. DXCC-CW on 2m).
  renderScopeGrid();
  renderRadiusGrid();
}

function renderScopeGrid() {
  const grid = document.getElementById("settings_scopes");
  if (!grid) return;
  const allScopes = [...ALL_DXCC_SCOPES, ...ALL_GRID_SCOPES];
  // Column count is data-driven (1 band-label col + one per scope) so changing
  // the scope list can't desync the layout from a hardcoded CSS column count.
  grid.style.gridTemplateColumns = `4em repeat(${allScopes.length}, minmax(4.5em, auto))`;
  // Header row: blank corner + scope labels
  let html = `<div class="scope-hdr"></div>`;
  for (const sc of allScopes) {
    // Compress for display: drop the "DXCC-" prefix on DXCC scopes (column header
    // is implicitly DXCC, the four scopes there are Mixed/CW/Phone/Digital).
    const short = sc.startsWith("DXCC-") ? sc.slice(5) : sc;
    html += `<div class="scope-hdr">${short}</div>`;
  }
  // One row per band
  for (const b of BAND_ORDER) {
    const avail = new Set(availableScopesForBand(b));
    html += `<div class="band-label">${b}</div>`;
    for (const sc of allScopes) {
      if (!avail.has(sc)) {
        html += `<div class="cell disabled">·</div>`;
        continue;
      }
      const checked = isScopeEnabled(b, sc) ? "checked" : "";
      html += `<div class="cell"><input type="checkbox" data-band="${b}" data-scope="${sc}" ${checked}></div>`;
    }
  }
  grid.innerHTML = html;
  grid.querySelectorAll("input[data-band][data-scope]").forEach(el => {
    el.addEventListener("change", () => {
      setScopeEnabled(el.dataset.band, el.dataset.scope, el.checked);
      refresh();
    });
  });
}

function renderRadiusGrid() {
  const box = document.getElementById("settings_radius");
  // Build once: a 5s refresh re-runs renderSettingsPanel; rebuilding the
  // inputs each tick would wipe a value mid-typing. Overrides only change
  // via this grid / the reset button, so a one-time build stays correct.
  if (!box || box.children.length) return;
  box.innerHTML = BAND_ORDER.map(b => {
    const def = defaultRadiusForBand(b);
    const ov = bandRadiusOverride[b];
    const val = (typeof ov === "number" && ov > 0) ? ov : "";
    return `<label><input type="number" min="1" max="9999" step="10" `
      + `data-rband="${b}" value="${val}" placeholder="${def}" `
      + `style="width:4.5em">${b}</label>`;
  }).join("");
  box.querySelectorAll("input[data-rband]").forEach(el => {
    el.addEventListener("change", () => {
      const b = el.dataset.rband;
      const n = parseInt(el.value, 10);
      if (Number.isFinite(n) && n > 0) bandRadiusOverride[b] = n;
      else { delete bandRadiusOverride[b]; el.value = ""; }
      saveBandRadius();
      refresh();
    });
  });
}

document.getElementById("settings_reset_radii").addEventListener("click", () => {
  bandRadiusOverride = {};
  saveBandRadius();
  const box = document.getElementById("settings_radius");
  if (box) box.innerHTML = "";
  renderRadiusGrid();
  refresh();
});
document.getElementById("settings_all_bands").addEventListener("click", () => {
  disabledBands.clear();
  saveDisabledSet("grayline_disabled_bands", disabledBands);
  refresh();
});
document.getElementById("settings_no_bands").addEventListener("click", () => {
  BAND_ORDER.forEach(b => disabledBands.add(b));
  saveDisabledSet("grayline_disabled_bands", disabledBands);
  refresh();
});
document.getElementById("settings_all_modes").addEventListener("click", () => {
  disabledModes.clear();
  saveDisabledSet("grayline_disabled_modes", disabledModes);
  refresh();
});
document.getElementById("settings_no_modes").addEventListener("click", () => {
  // Find all modes currently visible in the panel and disable them
  document.querySelectorAll("#settings_modes input[data-mode]").forEach(el => {
    disabledModes.add(el.dataset.mode);
  });
  saveDisabledSet("grayline_disabled_modes", disabledModes);
  refresh();
});
document.getElementById("settings_reset_scopes").addEventListener("click", () => {
  resetAwardScopesToDefaults();
  refresh();
});

async function refresh() {
  let data;
  try {
    const r = await fetch("/spots.json", { cache: "no-store" });
    data = await r.json();
  } catch (e) {
    document.getElementById("status").textContent = "fetch error: " + e.message;
    return;
  }
  let spots = data.spots, now = data.now;
  // Worked-state changed server-side (QSO logged, LoTW/QRZ pull, N1MM mutation)?
  // Pull fresh scores so the FFMA/award scorecard tracks the liveview instead of
  // waiting on the 5-min timer. Skip the first poll — no baseline rev yet.
  if (data.worked_rev !== undefined) {
    if (lastWorkedRev !== null && data.worked_rev !== lastWorkedRev) fetchScores();
    lastWorkedRev = data.worked_rev;
  }
  const filterOn = filterCB.checked;
  const showWanted = showWantedCB.checked;
  let filteredOut = 0;

  // Re-render settings panel with current modes-seen
  renderSettingsPanel(spots);

  // Apply filters in order: band/mode visibility, show-wanted (any enabled scope is new), 300mi
  spots = spots.filter(s => {
    if (s.calling_me) return true;   // someone calling ME always shows — bypass all filters
    if (disabledBands.has(s.band)) { filteredOut++; return false; }
    if (disabledModes.has(s.mode)) { filteredOut++; return false; }
    if (showWanted && !anyScopeNeeded(s)) { filteredOut++; return false; }
    if (filterOn) {
      if (s.distance_mi !== null && s.distance_mi !== undefined && s.distance_mi > radiusForBand(s.band)) {
        filteredOut++;
        return false;
      }
    }
    return true;
  });
  const byBand = {};
  for (const s of spots) {
    (byBand[s.band] = byBand[s.band] || {});
    (byBand[s.band][s.mode] = byBand[s.band][s.mode] || []).push(s);
  }
  const bands = Object.keys(byBand).sort((a,b) => bandIdx(a) - bandIdx(b));

  // ---- Tab strip: "All" pseudo-tab plus one button per active band ----
  // The "All" tab (sentinel "*") shows every band's spots in one combined
  // flat table — useful for low-activity periods where you want an overview
  // without clicking through each tab. Each band tab still drills down to a
  // single band when activity is concentrated.
  let activeBand = getActiveBand() || "*";
  // Keep the selected band STICKY even when it currently has no spots — show its
  // tab + a band-specific empty state rather than silently reverting to All.
  const tabBands = bands.slice();
  if (activeBand !== "*" && !tabBands.includes(activeBand)) {
    tabBands.push(activeBand);
    tabBands.sort((a, b) => bandIdx(a) - bandIdx(b));
  }
  const tabStrip = document.getElementById("tab_strip");
  if (tabBands.length === 0) {
    tabStrip.innerHTML = '<span class="empty">No spots match current filters.</span>';
  } else {
    // All tab — counter aggregates wanted/total across every visible band
    let allTotal = 0, allWanted = 0;
    for (const b of bands) {
      for (const list of Object.values(byBand[b])) {
        for (const s of list) {
          allTotal++;
          if (anyScopeNeeded(s)) allWanted++;
        }
      }
    }
    const allCounter = allWanted > 0
      ? `<span class="count"><span class="wanted">${allWanted}</span><span class="sep">/</span>${allTotal}</span>`
      : `<span class="count">${allTotal}</span>`;
    const allCls = (activeBand === "*") ? "active" : "";
    let tabHTML = `<button class="${allCls}" data-band="*">All${allCounter}</button>`;
    tabHTML += tabBands.map(b => {
      let total = 0, wanted = 0;
      for (const list of Object.values(byBand[b] || {})) {   // active band may be empty (0 spots)
        for (const s of list) {
          total++;
          if (anyScopeNeeded(s)) wanted++;
        }
      }
      const cls = (b === activeBand) ? "active" : "";
      const counter = wanted > 0
        ? `<span class="count"><span class="wanted">${wanted}</span><span class="sep">/</span>${total}</span>`
        : `<span class="count">${total}</span>`;
      return `<button class="${cls}" data-band="${b}">${escapeHTML(b)}${counter}</button>`;
    }).join("");
    tabStrip.innerHTML = tabHTML;
    tabStrip.querySelectorAll("button[data-band]").forEach(btn => {
      btn.addEventListener("click", () => {
        setActiveBand(btn.dataset.band);
        refresh();
      });
    });
  }

  // ---- Mode toggles row ----
  // In single-band view: per-band-mode disable (existing bandModeMap)
  // In All view: global disable (the disabledModes set, which the gear
  //              panel already drives) — this gives one place to silence
  //              a mode across all bands at once.
  const modeTogglesBox = document.getElementById("band_mode_toggles");
  if (activeBand === "*") {
    // Aggregate modes seen across every visible band, count totals
    const modeCounts = {};
    for (const b of bands) {
      for (const m of Object.keys(byBand[b])) {
        modeCounts[m] = (modeCounts[m] || 0) + byBand[b][m].length;
      }
    }
    const modesAll = Object.keys(modeCounts).sort();
    if (modesAll.length === 0) {
      modeTogglesBox.innerHTML = '<span class="empty">No modes in current view.</span>';
    } else {
      modeTogglesBox.innerHTML = `<strong style="color:#ff0;margin-right:0.8em">All modes:</strong>` +
        modesAll.map(m => {
          const enabled = !disabledModes.has(m);
          return `<label><input type="checkbox" data-allmode="${m}" ${enabled ? "checked" : ""}>${escapeHTML(m)} (${modeCounts[m]})</label>`;
        }).join("");
      modeTogglesBox.querySelectorAll("input[data-allmode]").forEach(el => {
        el.addEventListener("change", () => {
          const m = el.dataset.allmode;
          if (el.checked) disabledModes.delete(m); else disabledModes.add(m);
          saveDisabledSet("grayline_disabled_modes", disabledModes);
          refresh();
        });
      });
    }
  } else if (!byBand[activeBand]) {
    modeTogglesBox.innerHTML = `<span class="empty">No current spots on ${escapeHTML(activeBand)}.</span>`;
  } else {
    const modesInBand = Object.keys(byBand[activeBand]).sort();
    modeTogglesBox.innerHTML = `<strong style="color:#ff0;margin-right:0.8em">${escapeHTML(activeBand)} modes:</strong>` +
      modesInBand.map(m => {
        const rows = byBand[activeBand][m];
        const enabled = !isBandModeDisabled(activeBand, m);
        return `<label><input type="checkbox" data-band="${activeBand}" data-mode="${m}" ${enabled ? "checked" : ""}>${escapeHTML(m)} (${rows.length})</label>`;
      }).join("");
    modeTogglesBox.querySelectorAll("input[data-band][data-mode]").forEach(el => {
      el.addEventListener("change", () => {
        toggleBandMode(el.dataset.band, el.dataset.mode, el.checked);
        refresh();
      });
    });
  }

  // ---- Render: one flat table, sorted by band then freq.
  // Single-band view shows just that band, with Mode column.
  // All view shows every band with both Band and Mode columns. ----
  let html = "";
  let allRows = [];
  if (activeBand === "*") {
    for (const b of bands) {
      for (const m of Object.keys(byBand[b])) {
        // In All view we don't apply per-band-mode disable — that was the
        // single-band drill-down semantic. Use the global disabledModes set
        // (already filtered upstream in the spot.filter pipeline). Spots
        // that survived the filter all show here.
        allRows.push(...byBand[b][m]);
      }
    }
  } else if (byBand[activeBand]) {
    for (const m of Object.keys(byBand[activeBand])) {
      if (isBandModeDisabled(activeBand, m)) continue;
      allRows.push(...byBand[activeBand][m]);
    }
  }
  // Sort: anyone calling ME floats to the very top (time-critical — I want to
  // click them this cycle), then band order, then freq within band.
  allRows.sort((x, y) => {
    if (!!x.calling_me !== !!y.calling_me) return x.calling_me ? -1 : 1;
    const bd = bandIdx(x.band) - bandIdx(y.band);
    if (bd !== 0) return bd;
    return x.freq_khz - y.freq_khz;
  });

  if (allRows.length === 0) {
    html = (activeBand === "*")
      ? '<div class="empty">No spots in current view.</div>'
      : `<div class="empty">No current spots on ${escapeHTML(activeBand)} &mdash; holding this band; they'll show here the moment they arrive.</div>`;
  } else {
    const showBandCol = (activeBand === "*");
    let table = '<table><tr>';
    table += '<th>Callsign</th><th>DXCC</th><th>Cont</th><th>Grid</th>';
    if (showBandCol) table += '<th>Band</th>';
    table += '<th>Mode</th><th>Award</th><th>Freq</th><th>dB</th><th>Spotter</th><th>Spotter mi</th><th>Age</th></tr>';
    for (const s of allRows) {
      const age = Math.floor(now - s.ts);
      let snrCell = "";
      let snrClass = "snr";
      if (s.snr !== null && s.snr !== undefined) {
        snrCell = (s.snr > 0 ? "+" : "") + s.snr;
        snrClass += s.snr < 0 ? " neg" : " pos";
      }
      let distCell = "";
      let distClass = "dist";
      if (s.distance_mi === null || s.distance_mi === undefined) {
        distCell = "—";
      } else {
        distCell = s.distance_mi.toLocaleString() + " mi";
        if (s.distance_mi > radiusForBand(s.band)) distClass += " far";
      }
      const callStatus = s.call_status || "new";
      // DXCC and Grid cell highlights driven by the *enabled* award scopes
      // for this band — weakest (most-needed) status wins. Cell stays plain
      // when no scope from that family is enabled.
      const dxccEff = effectiveDxccStatus(s);
      const dxccCellClass = "country" + (dxccEff ? " " + dxccEff : "");
      const gridEff = effectiveGridStatus(s);
      const gridCellClass = "grid" + (gridEff ? " g" + gridEff : "");
      // Local source = us. Recognizes our skimmer/WSJT-X local sources, and the
      // operator's own callsign appearing as a cluster spotter (own-station
      // infrastructure spots) so they still highlight as "us".
      const isUs = (s.source || "").endsWith("-LOCAL")
                || (s.spotter || "").toUpperCase().startsWith(MY_CALLSIGN);
      const rowClass = (s.calling_me ? "calling-me " : "") + (isUs ? "us-spotted" : "");
      const spotterClass = isUs ? "spotter us" : "spotter";
      const callTag = s.calling_me ? ` <span class="callsyou" title="This station is calling YOU right now">CALLS YOU</span>` : "";
      const awardCell = scopeTags(s).map(t =>
        `<span class="pill ${t.status}"${t.title ? ` title="${escapeHTML(t.title)}"` : ""}>${escapeHTML(t.label)}</span>`
      ).join("") + marathonBadge(s) + waeBadge(s);   // Marathon + WAE live with the other award pills
      const bandCell = showBandCol ? `<td class="band">${escapeHTML(s.band)}</td>` : "";
      table += `<tr class="${rowClass} clickable" data-call="${escapeHTML(s.dx_call)}" data-freq="${s.freq_khz}" data-mode="${escapeHTML(s.mode)}" data-source="${escapeHTML(s.source||'')}" title="Click to tune WSJT-X / Flex to this signal">
        <td class="dx ${callStatus}">${escapeHTML(s.dx_call)}${callTag}${heardEye(s)}</td>
        <td class="${dxccCellClass}">${escapeHTML(s.country || "")}${rarityBadge(s)}${lotwBadge(s)}${cqTag(s)}</td>
        <td class="cont">${escapeHTML(s.continent || "")}</td>
        <td class="${gridCellClass}">${escapeHTML(s.grid)}${ffmaRarityBadge(s)}</td>
        ${bandCell}
        <td class="mode">${escapeHTML(s.mode)}</td>
        <td class="awards">${awardCell}</td>
        <td class="freq">${s.freq_khz.toFixed(1)}</td>
        <td class="${snrClass}">${snrCell}</td>
        <td class="${spotterClass}">${escapeHTML(s.spotter)}${peerCopiesBadge(s)}</td>
        <td class="${distClass}">${distCell}</td>
        <td class="age">${fmtAge(age)}</td>
      </tr>`;
    }
    table += '</table>';
    html = table;
  }
  // Status counts: total / wanted (any enabled scope is new) / new+confirmed calls / spots we heard
  let wantedCount = 0, newCallCount = 0, confirmedCount = 0, usCount = 0;
  for (const s of spots) {
    if (anyScopeNeeded(s)) wantedCount++;
    if (s.call_status === "new") newCallCount++;
    if (s.call_status === "confirmed") confirmedCount++;
    if ((s.spotter || "").toUpperCase().startsWith(MY_CALLSIGN)) usCount++;
  }
  const anyFilter = filterOn || showWanted || disabledBands.size > 0 || disabledModes.size > 0;
  const filterTag = anyFilter ? ` (${filteredOut} hidden)` : "";
  document.getElementById("status").innerHTML =
    `<span class="count">${spots.length}</span> spots · ` +
    `<span class="wanted">${wantedCount}</span> wanted · ` +
    `<span style="color:#5f5">${usCount} we heard</span> · ` +
    `${newCallCount} new calls · ${confirmedCount} confirmed · ` +
    `${bands.length} bands · ${new Date().toLocaleTimeString()}${filterTag}`;
  document.getElementById("band_content").innerHTML = html;
}
refresh();
setInterval(refresh, 5000);

// Click-to-tune: delegate clicks on tr.clickable rows to /api/tune
document.addEventListener("click", (ev) => {
  const tr = ev.target.closest("tr.clickable");
  if (!tr) return;
  const call = tr.dataset.call;
  const freq = parseFloat(tr.dataset.freq);
  const mode = tr.dataset.mode || "";
  if (!call || !freq) return;
  // Visual feedback — flash the row briefly so the operator sees the click registered
  tr.classList.add("tuning");
  setTimeout(() => tr.classList.remove("tuning"), 600);
  fetch("/api/tune", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({dx_call: call, freq_khz: freq, mode: mode})
  }).then(r => r.json()).then(j => {
    if (j.ok) {
      console.log(`tuned ${call} on ${freq}: ${j.message || ''}`);
    } else {
      console.warn(`tune ${call} on ${freq} failed: ${j.error || 'unknown'}`);
    }
  }).catch(e => console.error("tune fetch failed:", e));
});

// ============== View tabs (Live / Scores / Log search) ==============
function switchView(name) {
  for (const btn of document.querySelectorAll(".view-tabs button")) {
    btn.classList.toggle("active", btn.dataset.view === name);
  }
  for (const sec of document.querySelectorAll(".view-section")) {
    sec.classList.toggle("active", sec.id === "view_" + name);
  }
}
for (const btn of document.querySelectorAll(".view-tabs button")) {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
}

// ============== Scores tab ==============
function awardRow(name, worked, confirmed, target) {
  const cls = (confirmed === 0) ? "empty" : (target && confirmed >= target ? "complete" : "partial");
  return `<tr>
    <td class="s-name">${name}</td>
    <td class="s-w">${worked != null ? worked : ""}</td>
    <td class="s-c ${cls}">${confirmed}</td>
    <td class="s-g">${target || ""}</td>
  </tr>`;
}
function awardCard(title, rows) {
  return `<div class="score-card">
    <h3>${title}</h3>
    <table>
      <tr><th>Award</th><th>W</th><th>C</th><th>Goal</th></tr>
      ${rows.join("")}
    </table>
  </div>`;
}
let rareMatrixOpen = true, oqrsClaimOpen = true, oqrsSuspOpen = false, m0oxoOpen = true;
let rareSort = "rarity";   // "rarity" (rank, rarest first) | "az" (entity name)
let rareFilter = "all";    // "all" | "needed" | "worked" — persists across re-sort
// Apply the current Show filter to already-rendered rows (no re-render).
function applyRareFilter(host) {
  host.querySelectorAll("table.rm tr").forEach(tr => {
    if (tr.querySelector("th")) return;
    let show = true;
    if (rareFilter === "needed") show = tr.classList.contains("rm-needed") || tr.classList.contains("rm-worked");
    else if (rareFilter === "worked") show = tr.classList.contains("rm-worked");
    tr.style.display = show ? "" : "none";
  });
}
// Most Wanted tab: OQRS-claimable + suspect punch-lists, then the full progress
// matrix (worked/confirmed/claimable/suspect per band & mode).
function renderRareMatrix(j) {
  const host = document.getElementById("rare_matrix");
  if (!host) return;
  if (!j.rare_progress || !j.rare_progress.length) { host.innerHTML = ""; return; }
  const rp = j.rare_progress.slice();
  if (rareSort === "az") rp.sort((a, b) => a.name.localeCompare(b.name));
  else rp.sort((a, b) => a.rank - b.rank);
  const BANDS = ["160m","80m","40m","30m","20m","17m","15m","12m","10m","6m"];
  const BL = {"160m":"160","80m":"80","40m":"40","30m":"30","20m":"20","17m":"17","15m":"15","12m":"12","10m":"10","6m":"6"};
  const GLY = {confirmed:"✓", claimable:"◆", suspect:"?", worked:"·"};
  const cell = s => `<td class="s ${s}" title="${s}">${GLY[s] || ""}</td>`;
  const workedN = rp.filter(r => r.worked).length;
  let rows = "";
  for (const r of rp) {
    const cls = r.confirmed ? "rm-conf" : (r.worked ? "rm-worked" : "rm-needed");
    rows += `<tr class="${cls}"><td class="rk">${r.rank}</td>`
      + `<td class="ent" title="${escapeHTML(r.name)} (DXCC ${r.dxcc})">${escapeHTML(r.name)}</td>`
      + BANDS.map(b => cell(r.bands[b] || "new")).join("")
      + cell(r.modes.CW) + cell(r.modes.Phone) + cell(r.modes.Digital)
      + `</tr>`;
  }
  const hdr = `<tr><th>#</th><th>Entity</th>` + BANDS.map(b => `<th>${BL[b]}</th>`).join("")
    + `<th>CW</th><th>Ph</th><th>Dig</th></tr>`;
  const cl = j.oqrs_claimable || [], su = j.oqrs_suspect || [];
  const clItems = cl.map(e => `<li><span class="rk">#${e.rank}</span><b>${escapeHTML(e.name)}</b> — `
    + e.slots.map(s => `${s.band} <span class="mwc">${escapeHTML(s.call)}</span>`).join(", ") + `</li>`).join("");
  const suItems = su.map(e => `<li><span class="rk">#${e.rank}</span><b>${escapeHTML(e.name)}</b> — `
    + e.slots.map(s => `${s.band} <span class="mwc">${escapeHTML((s.calls || []).join("/"))}</span>`).join(", ") + `</li>`).join("");
  const claimBox = cl.length ? `<details class="oqrs-box claim"${oqrsClaimOpen ? " open" : ""}>`
    + `<summary>◆ Confirmable via OQRS — ${cl.length} rare${cl.length > 1 ? "s" : ""} one QSL request away</summary>`
    + `<ul>${clItems}</ul></details>` : "";
  const suspBox = su.length ? `<details class="oqrs-box susp"${oqrsSuspOpen ? " open" : ""}>`
    + `<summary>? Suspect — ${su.length} logged but not in the DX's log (verify)</summary>`
    + `<ul>${suItems}</ul></details>` : "";
  const mo = j.m0oxo || [];
  const moNew = mo.filter(e => e.status === "new"), moPart = mo.filter(e => e.status === "partial"),
        moDone = mo.filter(e => e.status === "done");
  const moTag = e => e.status === "new" ? ` <span class="mo-new">NEW ENTITY</span>`
    : e.status === "partial" ? ` <span class="mo-part">&#10003; entity &middot; ${e.open_slots.length} band${e.open_slots.length > 1 ? "s" : ""} unconfirmed</span>`
    : ` <span class="mo-done">&#10003; confirmed</span>`;
  const moLi = e => `<li class="mo-${e.status}"><span class="rk">${e.rank < 9999 ? "#" + e.rank : "—"}</span>`
    + `<b>${escapeHTML(e.name)}</b>${moTag(e)} — <span class="mwc">${e.calls.map(escapeHTML).join(", ")}</span></li>`;
  const moItems = [...moNew, ...moPart, ...moDone].map(moLi).join("");
  const moSummary = (moNew.length ? `${moNew.length} NEW ENTITY` : "nothing new")
    + (moPart.length ? `, ${moPart.length} with unconfirmed bands` : "")
    + `, ${moDone.length} already confirmed`;
  const moBox = mo.length ? `<details class="oqrs-box m0oxo"${m0oxoOpen ? " open" : ""}>`
    + `<summary>M0OXO-managed logs holding your call — ${moSummary} (m0oxo.com, separate from Club Log)</summary>`
    + `<ul>${moItems}</ul></details>` : "";
  host.innerHTML = claimBox + suspBox + moBox
    + `<details class="rare-matrix"${rareMatrixOpen ? " open" : ""}>`
    + `<summary>Most Wanted — worked / confirmed by band &amp; mode (${workedN}/${rp.length} worked)</summary>`
    + `<div class="rm-controls">Sort: `
    + `<button data-rms="rarity" class="${rareSort === "rarity" ? "active" : ""}">rarity</button>`
    + `<button data-rms="az" class="${rareSort === "az" ? "active" : ""}">A&ndash;Z</button>`
    + `&nbsp;&nbsp;Show: `
    + `<button data-rmf="all" class="${rareFilter === "all" ? "active" : ""}">all</button>`
    + `<button data-rmf="needed" class="${rareFilter === "needed" ? "active" : ""}">needed (unconfirmed)</button>`
    + `<button data-rmf="worked" class="${rareFilter === "worked" ? "active" : ""}">worked, not confirmed</button>`
    + `&nbsp;&nbsp;<span style="color:#9f9">✓</span> conf &nbsp;<span style="color:#6ff">◆</span> OQRS &nbsp;<span style="color:#f9a">?</span> verify &nbsp;<span style="color:#fc6">·</span> worked</div>`
    + `<div class="rm-scroll"><table class="rm">${hdr}${rows}</table></div></details>`;
  const det = host.querySelector("details.rare-matrix");
  if (det) det.addEventListener("toggle", () => { rareMatrixOpen = det.open; });
  const cbx = host.querySelector("details.oqrs-box.claim");
  if (cbx) cbx.addEventListener("toggle", () => { oqrsClaimOpen = cbx.open; });
  const sbx = host.querySelector("details.oqrs-box.susp");
  if (sbx) sbx.addEventListener("toggle", () => { oqrsSuspOpen = sbx.open; });
  const mbx = host.querySelector("details.oqrs-box.m0oxo");
  if (mbx) mbx.addEventListener("toggle", () => { m0oxoOpen = mbx.open; });
  host.querySelectorAll("[data-rmf]").forEach(btn => btn.addEventListener("click", () => {
    rareFilter = btn.dataset.rmf;
    host.querySelectorAll("[data-rmf]").forEach(b => b.classList.toggle("active", b === btn));
    applyRareFilter(host);
  }));
  host.querySelectorAll("[data-rms]").forEach(btn => btn.addEventListener("click", () => {
    rareSort = btn.dataset.rms;
    renderRareMatrix(lastScores);   // re-sort rows; filter state is preserved
  }));
  applyRareFilter(host);   // re-apply current Show filter after a (re-)render
}

function renderScores(j) {
  const grid = document.getElementById("scores_grid");
  if (!j || j.error) {
    grid.innerHTML = `<span style="color:#f55">${(j && j.error) || "scores unavailable"}</span>`;
    return;
  }
  lastScores = j;   // remember for instant re-render when an award toggle changes
  // Capture mode-DXCC confirmed counts so spot pills can auto-retire at target.
  if (j.dxcc) {
    for (const k of ["CW", "Phone", "Digital"]) {
      if (j.dxcc[k]) modeAwardConfirmed[k] = j.dxcc[k].confirmed;
    }
  }
  const cards = [];

  // DXCC by mode class (Mixed/CW/Phone/Digital/Satellite)
  {
    const rows = [];
    const DXCC_KEY = {Mixed:"dxcc_mixed", CW:"dxcc_cw", Phone:"dxcc_phone",
                      Digital:"dxcc_digital", Satellite:"dxcc_satellite"};
    for (const cls of ["Mixed", "CW", "Phone", "Digital", "Satellite"]) {
      if (!awardOn(DXCC_KEY[cls])) continue;
      const d = j.dxcc[cls] || {worked: 0, confirmed: 0};
      rows.push(awardRow("DXCC " + cls, d.worked, d.confirmed, 100));
    }
    if (rows.length) cards.push(awardCard("DXCC", rows));
  }

  // Mode DXCC spot-highlighting toggles — entity-level (any band), auto-retire
  // at 100 confirmed. "On" = highlight spots of entities you still need on that
  // mode; auto-off once the award is earned (re-check to chase endorsements).
  {
    const chips = [];
    for (const k of ["CW", "Phone", "Digital"]) {
      const c = (j.dxcc[k] || {confirmed: 0}).confirmed;
      const done = c >= MODE_DXCC_TARGET;
      const active = modeAwardActive(k);
      chips.push(
        `<label class="mode-chip ${active ? "on" : "off"}" `
        + `title="${active ? "highlighting spots needed for DXCC " + k : "muted — earned, not highlighting"}">`
        + `<input type="checkbox" data-modechip="${k}" ${active ? "checked" : ""}>`
        + `${k} ${c}/${MODE_DXCC_TARGET}${done ? " ✓" : ""}</label>`
      );
    }
    cards.push(`<div class="score-card"><h3>Mode DXCC — spot highlights</h3>`
      + `<div class="mode-chips">${chips.join("")}</div>`
      + `<div class="mode-hint">On = highlight spots you still need on that mode, any band. `
      + `Auto-off at ${MODE_DXCC_TARGET} confirmed; re-check to chase endorsements.</div></div>`);
  }

  // DXCC by band — 160-6m always, plus any VHF/UHF where we have entries
  if (awardOn("dxcc_by_band")) {
    const rows = [];
    const ALL_BANDS = ["160m","80m","40m","30m","20m","17m","15m","12m","10m","6m",
                      "2m","1.25m","70cm","33cm","23cm","13cm","9cm","6cm","3cm","1.25cm"];
    for (const b of ALL_BANDS) {
      const d = j.dxcc_by_band && j.dxcc_by_band[b];
      if (!d) continue;
      rows.push(awardRow("DXCC " + b, d.worked, d.confirmed, 100));
    }
    if (rows.length) cards.push(awardCard("DXCC by Band", rows));
  }

  // Worked-All & Challenge — Challenge, WAS, WAJA, WAZ, FFMA (each toggleable)
  {
    const rows = [];
    if (awardOn("challenge")) rows.push(awardRow("DXCC Challenge", j.challenge.worked, j.challenge.confirmed, 1000));
    if (awardOn("was")) rows.push(awardRow("WAS", j.was.worked, j.was.confirmed, j.was.target));
    if (j.waja && awardOn("waja")) {
      rows.push(awardRow("WAJA (JARL)", j.waja.worked, j.waja.confirmed, j.waja.target));
    }
    if (awardOn("waz")) rows.push(awardRow("WAZ", j.waz.worked, j.waz.confirmed, j.waz.target));
    if (j.wpx && awardOn("wpx")) rows.push(awardRow("WPX", j.wpx.worked, j.wpx.confirmed, j.wpx.target));
    if (j.wae && awardOn("wae")) rows.push(awardRow("WAE (Eu)", j.wae.worked, j.wae.confirmed, j.wae.target));
    if (j.ffma && awardOn("ffma")) {
      rows.push(awardRow("FFMA (6m)", j.ffma.worked, j.ffma.confirmed, j.ffma.target));
    }
    if (rows.length) cards.push(awardCard("Worked-All &amp; Challenge", rows));
  }

  // WAJA prefecture grid (kanji drill-in) — only when WAJA is enabled.
  if (awardOn("waja") && j.waja) {
    const conf = new Set(j.waja.confirmed_codes || []);
    const work = new Set(j.waja.worked_codes || []);
    const cells = JA_PREFECTURES.map(([code, kanji, romaji]) => {
      const st = conf.has(code) ? "conf" : (work.has(code) ? "work" : "new");
      return `<span class="pref pref-${st}" title="${romaji} (${code})">${kanji}</span>`;
    }).join("");
    cards.push(`<details class="score-card waja-detail" ${wajaGridOpen ? "open" : ""}>`
      + `<summary>日本 — WAJA prefectures (${j.waja.confirmed}/${j.waja.target})</summary>`
      + `<div class="pref-grid">${cells}</div>`
      + `<div class="mode-hint">Green = confirmed · amber = worked, unconfirmed · dim = needed. Hover for romaji.</div>`
      + `</details>`);
  }

  // Five-Band awards + Honor Roll — completion-style awards. The C column is
  // "bands complete" (out of 5); the hint line shows the per-band counts so the
  // lagging band is obvious. Honor Roll is a standing off confirmed entities.
  {
    const rows = [];
    const hints = [];
    const fbFmt = (byBand, tgt) => Object.keys(byBand)
      .map(b => `<span class="${byBand[b] >= tgt ? "fb-done" : "fb-need"}">${b} ${byBand[b]}</span>`)
      .join(" · ");
    if (awardOn("nbdxcc") && j.nb_dxcc && j.nb_dxcc.has_base) {
      // 5BDXCC base earned — show the N-Band milestone (8BDXCC, 10BDXCC, ...).
      const d = j.nb_dxcc;
      rows.push(awardRow(`${d.level}BDXCC`, null, d.level, 10));
      const shortStr = Object.keys(d.short).length
        ? Object.keys(d.short).map(b => `<span class="fb-need">${b} ${d.short[b]}</span>`).join(" · ")
        : "all Challenge bands done";
      hints.push(`<div class="mode-hint"><b>${d.level}BDXCC</b> = 5BDXCC (80-10m) + endorsements `
        + `(${d.bands.join(", ")}). Toward 10BDXCC: ${shortStr}.</div>`);
    } else if (awardOn("nbdxcc") && j.five_band_dxcc) {
      // Still building the 5BDXCC base — show per-band progress toward it.
      const d = j.five_band_dxcc;
      rows.push(awardRow("5BDXCC (80-10m)", null, d.bands_complete, d.target_bands));
      hints.push(`<div class="mode-hint">5BDXCC (100/band): ${fbFmt(d.by_band, d.per_band_target)}</div>`);
    }
    if (awardOn("5bwas") && j.five_band_was) {
      const d = j.five_band_was;
      rows.push(awardRow("5BWAS", null, d.bands_complete, d.target_bands));
      hints.push(`<div class="mode-hint">5BWAS (50/band): ${fbFmt(d.by_band, d.per_band_target)}</div>`);
    }
    if (awardOn("honor_roll") && j.honor_roll) {
      const d = j.honor_roll;
      rows.push(awardRow("DXCC Honor Roll", null, d.confirmed, d.honor_roll_at));
      hints.push(`<div class="mode-hint">Honor Roll at ${d.honor_roll_at}, #1 at ${d.number_one_at} `
        + `(counts all confirmed entities; may read high vs current-only).</div>`);
    }
    if (rows.length) {
      cards.push(`<div class="score-card"><h3>5-Band &amp; Honor Roll</h3>`
        + `<table><tr><th>Award</th><th>W</th><th>C</th><th>Goal</th></tr>${rows.join("")}</table>`
        + hints.join("") + `</div>`);
    }
  }

  // WAS by mode + Triple Play + WAC
  {
    const rows = [];
    const WASM_KEY = {CW:"was_cw", Phone:"was_phone", Digital:"was_digital"};
    if (j.was_by_mode) {
      for (const k of ["CW", "Phone", "Digital"]) {
        const d = j.was_by_mode[k];
        if (d && awardOn(WASM_KEY[k])) rows.push(awardRow("WAS " + k, d.worked, d.confirmed, 50));
      }
    }
    const tpOn = j.triple_play && awardOn("triple_play");
    if (tpOn) {
      const tp = j.triple_play;
      rows.push(awardRow("Triple Play", null, tp.legs_complete, tp.target_legs));
    }
    const wacOn = j.wac && awardOn("wac");
    if (wacOn) {
      rows.push(awardRow("WAC", j.wac.worked, j.wac.confirmed, j.wac.target));
    }
    if (rows.length) {
      const hints = [];
      hints.push(`<div class="mode-hint">Per-mode WAS counts LoTW or card (no eQSL).</div>`);
      if (tpOn && j.triple_play.legs) {
        const lg = j.triple_play.legs, t = j.triple_play.per_leg_target;
        const legStr = ["CW","Phone","Digital"].map(k =>
          `<span class="${lg[k] >= t ? "fb-done" : "fb-need"}">${k} ${lg[k]}/${t}</span>`).join(" · ");
        hints.push(`<div class="mode-hint"><b>Triple Play</b> = WAS earned on all three modes, `
          + `<b>LoTW only</b> — ${j.triple_play.legs_complete}/3 legs done: ${legStr}.</div>`);
      }
      if (wacOn && j.wac.continents) {
        hints.push(`<div class="mode-hint">WAC continents confirmed: ${j.wac.continents.join(", ") || "none"}.</div>`);
      }
      cards.push(`<div class="score-card"><h3>WAS by Mode / Triple Play / WAC</h3>`
        + `<table><tr><th>Award</th><th>W</th><th>C</th><th>Goal</th></tr>${rows.join("")}</table>`
        + hints.join("") + `</div>`);
    }
  }

  // WAS by Band — single-band WAS endorsements (160m..70cm). Bands within reach
  // of completion list the exact missing states so the finish line is visible.
  if (j.was && j.was.by_band_detail && awardOn("was_band")) {
    const detail = j.was.by_band_detail;
    const order = ["160m","80m","60m","40m","30m","20m","17m","15m","12m","10m","6m","2m","1.25m","70cm","33cm","23cm","13cm","6cm","3cm"];
    const present = order.filter(b => detail[b])
      .concat(Object.keys(detail).filter(b => !order.includes(b)).sort());
    const rows = present.map(b => {
      const d = detail[b];
      const togo = 50 - d.confirmed;
      let status, cls;
      if (togo <= 0) { status = `<span class="ff-conf">&#x2705; WAS</span>`; cls = "wasb-done"; }
      else if (d.missing.length <= 6) {
        status = `<span class="wasb-need"><b>${togo} to go:</b> ${d.missing.join(" ")}</span>`;
        cls = (togo <= 3) ? "wasb-close" : "";
      } else { status = `<span class="wasb-need">${togo} to go</span>`; cls = ""; }
      return `<tr class="${cls}"><td class="wasb-band">${b}</td><td class="wasb-cnt">${d.confirmed}/50</td><td>${status}</td></tr>`;
    }).join("");
    const doneN = present.filter(b => detail[b].confirmed >= 50).length;
    cards.push(`<div class="score-card was-band">
      <h3>WAS by Band <span class="ff-count">${doneN} earned</span></h3>
      <table class="ff-table"><tr><th>band</th><th>states</th><th>status</th></tr>${rows}</table>
      <div class="mode-hint">Single-band WAS endorsements. Bands within 6 of completion list the missing states; within 3 are highlighted. &#x2705; = earned.</div>
    </div>`);
  }

  // WAE by Band — single-band Worked All Europe (73 WAE countries). 6m/160m WAE
  // are the grail-tier chases; bands within reach list the missing countries.
  if (j.wae && j.wae.by_band_detail && awardOn("wae")) {
    const tgt = j.wae.target || 73;
    const detail = j.wae.by_band_detail;
    const order = ["160m","80m","60m","40m","30m","20m","17m","15m","12m","10m","6m","2m","1.25m","70cm","33cm","23cm"];
    const present = order.filter(b => detail[b])
      .concat(Object.keys(detail).filter(b => !order.includes(b)).sort());
    const rows = present.map(b => {
      const d = detail[b];
      const togo = tgt - d.confirmed;
      let status, cls;
      if (togo <= 0) { status = `<span class="ff-conf">&#x2705; WAE</span>`; cls = "wasb-done"; }
      else if (d.missing.length && d.missing.length <= 10) {
        status = `<span class="wasb-need"><b>${togo} to go:</b> ${d.missing.join(", ")}</span>`;
        cls = (togo <= 5) ? "wasb-close" : "";
      } else { status = `<span class="wasb-need">${togo} to go</span>`; cls = ""; }
      return `<tr class="${cls}"><td class="wasb-band">${b}</td><td class="wasb-cnt">${d.confirmed}/${tgt}</td><td>${status}</td></tr>`;
    }).join("");
    const doneN = present.filter(b => detail[b].confirmed >= tgt).length;
    const mixConf = j.wae.confirmed, miss = j.wae.missing || [], unw = new Set(j.wae.unworked || []);
    const mixLine = (mixConf >= tgt)
      ? `<div class="mode-hint"><span class="ff-conf">&#x2705; WAE Mixed complete (${mixConf}/${tgt})</span></div>`
      : `<div class="mode-hint"><b>WAE Mixed: ${mixConf}/${tgt}</b> &mdash; need ${tgt - mixConf}: `
        + miss.map(n => unw.has(n) ? `<b class="wae-unworked">${n}</b>` : n).join(", ")
        + ` <span class="wae-leg">(bold = not yet worked; plain = worked, awaiting QSL)</span></div>`;
    cards.push(`<div class="score-card was-band">
      <h3>WAE by Band <span class="ff-count">${doneN} earned</span></h3>
      ${mixLine}
      <table class="ff-table"><tr><th>band</th><th>countries</th><th>status</th></tr>${rows}</table>
      <div class="mode-hint">Single-band Worked All Europe (DARC, ${tgt} WAE countries). 6m &amp; 160m WAE are the grail-tier ones. &#x2705; = earned.</div>
    </div>`);
  }

  // VHF/UHF — VUCC by band + Satellite
  {
    const rows = [];
    const VUCC_TGT = {"6m":100,"2m":100,"1.25m":50,"70cm":50,"33cm":25,"23cm":25,"13cm":10,"9cm":5,"6cm":5,"3cm":5};
    if (j.vucc_satellite && awardOn("vucc_satellite")) {
      rows.push(awardRow("VUCC Satellite", j.vucc_satellite.worked, j.vucc_satellite.confirmed, j.vucc_satellite.target));
    }
    if (awardOn("vucc")) {
      for (const b of ["6m","2m","1.25m","70cm","33cm","23cm","13cm","9cm","6cm","3cm"]) {
        if (j.vucc && j.vucc[b] != null) {
          rows.push(awardRow("VUCC " + b, null, j.vucc[b], VUCC_TGT[b] || 25));
        }
      }
    }
    if (rows.length) cards.push(awardCard("VHF / UHF / Satellite VUCC", rows));
  }

  // Last 5 DXCC ATNOs (all-time-new entities) — most recent first.
  if (j.last5_atno && j.last5_atno.length) {
    const fmtd = s => (s && s.length >= 8) ? `${s.slice(0,4)}-${s.slice(4,6)}-${s.slice(6,8)}` : (s || "");
    const rows = j.last5_atno.map(a =>
      `<tr><td class="s-name">${fmtd(a.date)}</td>`
      + `<td>${escapeHTML(a.country || "?")}</td><td>${escapeHTML(a.call || "")}</td>`
      + `<td>${escapeHTML(a.band || "")}</td><td>${escapeHTML(a.mode || "")}</td></tr>`).join("");
    cards.push(`<div class="score-card"><h3>Last 5 DXCC ATNOs</h3>`
      + `<table class="atno-tbl"><colgroup><col style="width:26%"><col style="width:32%"><col style="width:16%"><col style="width:12%"><col style="width:14%"></colgroup>`
      + `<tr><th>Date</th><th>Entity</th><th>Call</th><th>Band</th><th>Mode</th></tr>${rows}</table>`
      + `<div class="mode-hint">ATNO = All-Time-New-One: your first-ever QSO with that DXCC entity.</div></div>`);
  }

  // Contacts by mode × year — vanilla CSS stacked bars, zero charting deps.
  if (j.qso_by_year_mode && Object.keys(j.qso_by_year_mode).length) {
    const MC = [["CW","#f2c14e"],["Phone","#4ea3f2"],["Digital","#5fd07a"],["Other","#8a8a8a"]];
    const years = Object.keys(j.qso_by_year_mode).sort().reverse();   // newest first
    const legend = MC.map(([k,c]) => `<span class="myl"><span class="mysw" style="background:${c}"></span>${k}</span>`).join("");
    const rows = years.map(yr => {
      const r = j.qso_by_year_mode[yr];
      const tot = MC.reduce((s,[k]) => s + (r[k]||0), 0);
      if (!tot) return "";
      const segs = MC.map(([k,c]) => {
        const n = r[k]||0; if (!n) return "";
        const pct = n/tot*100;
        return `<span class="myseg" style="width:${pct}%;background:${c}" title="${k}: ${n.toLocaleString()} (${pct.toFixed(1)}%)">${pct>=9?Math.round(pct)+"%":""}</span>`;
      }).join("");
      return `<div class="myrow"><span class="myyr">${yr}</span><span class="mybar">${segs}</span><span class="mytot">${tot.toLocaleString()}</span></div>`;
    }).join("");
    cards.push(`<div class="score-card mychart"><h3>Contacts by Mode × Year</h3>`
      + `<div class="mylegend">${legend}</div>` + rows
      + `<div class="mode-hint">Bar = share of that year's QSOs by mode class; number = year total. Hover a segment for exact count / %.</div></div>`);
  }

  // Scores setup — per-award visibility. ARRL on by default; CQ / JARL opt-in.
  {
    const CAT_LABEL = {ARRL:"ARRL", CQ:"CQ", DARC:"DARC (Europe)", JARL:"JARL (Japan)"};
    const byCat = {};
    for (const [key, label, cat] of AWARD_DEFS) (byCat[cat] = byCat[cat] || []).push([key, label]);
    const sections = ["ARRL", "CQ", "DARC", "JARL"].filter(c => byCat[c]).map(cat => {
      const boxes = byCat[cat].map(([key, label]) =>
        `<label class="awtoggle"><input type="checkbox" data-awardtoggle="${key}" ${awardOn(key) ? "checked" : ""}>${label}</label>`
      ).join("");
      return `<div class="aw-group"><div class="aw-cat">${CAT_LABEL[cat]}</div><div class="aw-boxes">${boxes}</div></div>`;
    }).join("");
    cards.push(`<details class="score-card aw-setup" ${scoresSetupOpen ? "open" : ""}>`
      + `<summary>⚙ Scores setup — choose which awards to track</summary>`
      + sections
      + `<div class="mode-hint">ARRL awards are on by default; CQ and JARL awards are opt-in. `
      + `Saved on this device.</div></details>`);
  }

  grid.innerHTML = cards.join("");
  const awSetup = grid.querySelector("details.aw-setup");
  if (awSetup) awSetup.addEventListener("toggle", () => { scoresSetupOpen = awSetup.open; });
  const wajaDet = grid.querySelector("details.waja-detail");
  if (wajaDet) wajaDet.addEventListener("toggle", () => { wajaGridOpen = wajaDet.open; });
  grid.querySelectorAll("input[data-awardtoggle]").forEach(el => {
    el.addEventListener("change", () => {
      const k = el.dataset.awardtoggle;
      // Match the default → clear the override (back to auto); else store the choice.
      if (el.checked === (AWARD_DEFAULT[k] !== false)) delete awardVis[k];
      else awardVis[k] = el.checked;
      saveAwardVis();
      if (lastScores) renderScores(lastScores);   // instant re-render, no network
    });
  });
  grid.querySelectorAll("input[data-modechip]").forEach(el => {
    el.addEventListener("change", () => {
      const k = el.dataset.modechip;
      const c = modeAwardConfirmed[k];
      const auto = (c == null) ? true : (c < MODE_DXCC_TARGET);   // automatic state
      // Match automatic → clear override (back to auto); else store the choice.
      if (el.checked === auto) delete modeOverrides[k];
      else modeOverrides[k] = el.checked;
      saveModeOverrides();
      refresh();        // re-gate spot pills immediately
      fetchScores();    // re-render this panel (chip styling)
    });
  });
  renderRareMatrix(j);
  renderFfma(j);
  const t = j.totals || {};
  document.getElementById("scores_totals").textContent =
    `${(t.qsos||0).toLocaleString()} QSOs · ${(t.unique_calls||0).toLocaleString()} unique calls · ` +
    `${(t.confirmed_qsos||0).toLocaleString()} confirmed records`;
}
// ============== FFMA tab ==============
function ffmaFmtDate(d) {
  if (!d || d.length < 8) return d || "";
  return d.slice(0,4) + "-" + d.slice(4,6) + "-" + d.slice(6,8);
}
function ffmaRover(call) {
  if (!call) return "";
  if (call.endsWith("/R")) return ` <span class="ff-rover" title="rover">🚐</span>`;
  if (call.endsWith("/P")) return ` <span class="ff-rover" title="portable">🥾</span>`;
  return "";
}
function ffmaTierBadge(tier, pct) {
  if (tier === "rare") return `<span class="ff-tier ff-rare">rare ${pct}%</span>`;
  if (tier === "uncommon") return `<span class="ff-tier ff-unc">unc ${pct}%</span>`;
  return `<span class="ff-tier ff-common">common</span>`;
}
function renderFfma(j) {
  const el = document.getElementById("ffma_panel");
  if (!el) return;
  const f = j.ffma, ch = j.ffma_chase;
  if (!f || !ch) { el.innerHTML = `<span style="color:#888">FFMA data unavailable (6m award not tracked).</span>`; return; }
  const target = f.target || 488;
  const togo = Math.max(0, target - (f.confirmed||0));
  const pct = ((f.confirmed||0) / target * 100).toFixed(1);
  const cards = [];

  // standing header — the grail at a glance
  cards.push(`<div class="ff-standing">
    <div class="ff-big">${f.confirmed}<span class="ff-of"> / ${target}</span></div>
    <div class="ff-sub">grids confirmed &middot; ${f.worked} worked &middot; <b>${togo} to go</b></div>
    <div class="ff-bar"><div class="ff-fill" style="width:${pct}%"></div></div>
    <div class="ff-pctlabel">${pct}% of the grail &middot; FFMA = all 488 CONUS grids on 6 m</div>
  </div>`);

  // recent ATNOs — newest featured big, then the previous few
  const atnos = ch.recent_atnos || (ch.recent_atno ? [ch.recent_atno] : []);
  if (atnos.length) {
    const a = atnos[0];
    const st = a.confirmed
      ? `<span class="ff-conf">CONFIRMED &#x2705;</span>`
      : `<span class="ff-pend">awaiting confirmation</span>`;
    const rest = atnos.slice(1).map(x => {
      const xs = x.confirmed ? `<span class="ff-conf">&#x2705;</span>` : `<span class="ff-pend">pending</span>`;
      return `<tr>
        <td class="ff-g">${x.grid}</td>
        <td>${ffmaTierBadge(x.tier,x.pct)}</td>
        <td class="ff-who">${x.call||"?"}${ffmaRover(x.call)}</td>
        <td class="ff-when">${ffmaFmtDate(x.date)}</td>
        <td>${xs}</td>
      </tr>`;
    }).join("");
    cards.push(`<details class="score-card ff-atno ff-collapse" ${ffAtnoOpen ? "open" : ""}>
      <summary>&#x1F195; Recent ATNOs <span class="ff-count">last ${atnos.length}</span></summary>
      <div class="ff-atno-row">
        <div class="ff-atno-grid">${a.grid}</div>
        <div class="ff-atno-meta">
          <div><span class="ff-newest">newest</span> &middot; via <b>${a.call||"?"}</b>${ffmaRover(a.call)} &middot; ${ffmaFmtDate(a.date)}</div>
          <div>${ffmaTierBadge(a.tier,a.pct)} ${st}</div>
        </div>
      </div>
      ${rest ? `<table class="ff-table ff-atno-tbl">
        <tr><th>grid</th><th>rarity</th><th>via</th><th>worked</th><th></th></tr>
        ${rest}</table>` : ""}
    </details>`);
  }

  // pending confirmation — the chase list
  {
    const rows = ch.pending.map(p => {
      const who = p.calls.map(c => `${c}${ffmaRover(c)}`).join(", ");
      const odds = p.multi_op
        ? `<span class="ff-odds-good" title="more than one op worked this grid — any one uploading to LoTW confirms it">${p.calls.length} ops &#x2713;</span>`
        : `<span class="ff-odds-one" title="only one op worked it — hinges on their LoTW upload">1 op</span>`;
      return `<tr>
        <td class="ff-g">${p.grid}</td>
        <td>${ffmaTierBadge(p.tier,p.pct)}</td>
        <td class="ff-when">${ffmaFmtDate(p.date)}</td>
        <td class="ff-who">${who}</td>
        <td>${odds}</td>
      </tr>`;
    }).join("");
    cards.push(`<details class="score-card ff-pending ff-collapse" ${ffPendOpen ? "open" : ""}>
      <summary>&#x23F3; Worked &mdash; awaiting confirmation <span class="ff-count">${ch.pending.length}</span></summary>
      ${ch.pending.length ? `<table class="ff-table">
        <tr><th>grid</th><th>rarity</th><th>worked</th><th>who to nudge</th><th>odds</th></tr>
        ${rows}</table>
        <div class="mode-hint">Rarest first. Multi-op grids confirm easier &mdash; any one of them uploading to LoTW credits it. Rows clear themselves as confirmations arrive.</div>`
        : `<div class="ff-empty">Nothing pending &mdash; every worked grid is confirmed. &#x1F389;</div>`}
    </details>`);
  }

  // rare/uncommon grids worked — the trophies (rover catches)
  {
    const rows = ch.rares_worked.map(r => {
      const st = r.confirmed ? `<span class="ff-conf">&#x2705;</span>` : `<span class="ff-pend">pending</span>`;
      return `<tr>
        <td class="ff-g">${r.grid}</td>
        <td>${ffmaTierBadge(r.tier, r.pct)}</td>
        <td class="ff-who">${r.call}${ffmaRover(r.call)}</td>
        <td class="ff-when">${ffmaFmtDate(r.date)}</td>
        <td>${st}</td>
      </tr>`;
    }).join("");
    const nRovers = ch.rares_worked.filter(r => r.call && (r.call.endsWith("/R")||r.call.endsWith("/P"))).length;
    cards.push(`<details class="score-card ff-rares ff-collapse" ${ffRaresOpen ? "open" : ""}>
      <summary>&#x1F3C6; Rare &amp; uncommon grids worked <span class="ff-count">${ch.rares_worked.length}</span></summary>
      ${ch.rares_worked.length ? `<table class="ff-table">
        <tr><th>grid</th><th>rarity</th><th>via</th><th>date</th><th></th></tr>
        ${rows}</table>
        <div class="mode-hint">The hard ones &mdash; usually only a rover (&#x1F690;) or portable (&#x1F97E;) puts these on 6 m.${nRovers ? ` ${nRovers} of these came from rovers/portables.` : ""}</div>`
        : `<div class="ff-empty">No rare/uncommon grids worked yet &mdash; the western grails are still ahead.</div>`}
    </details>`);
  }

  // grid log discrepancies — the grid the station ADVERTISED on the air (what
  // WSJT-X logged) != the grid their LoTW upload credits (6m+, terrestrial)
  const gd = j.grid_discrepancies;
  if (gd && gd.count) {
    const rows = gd.items.map(x => {
      const cr = x.credited ? `<span class="ff-g">${x.credited}</span>` : `<span class="ff-pend">no grid</span>`;
      const tag = x.kind === "no_grid"
        ? `<span class="ff-odds-one" title="they advertised this grid on the air but uploaded to LoTW without any grid — no grid credit until they re-upload with one">op: no grid</span>`
        : `<span class="ff-odds-good" title="they advertised this grid in their FT8 transmission but their LoTW upload credits a different one — classic rover with TQSL station location still set to home; nudge them to re-upload">advertised ≠ confirmed</span>`;
      const rarity = x.tier ? ffmaTierBadge(x.tier, x.pct)
                            : `<span class="ff-tier ff-common" title="not an FFMA grid (VUCC only) — no leader-board rarity">n/a</span>`;
      return `<tr>
        <td>${x.band}</td>
        <td class="ff-who">${x.call}${ffmaRover(x.call)}</td>
        <td class="ff-when">${ffmaFmtDate(x.date)}</td>
        <td class="ff-g">${x.logged}${x.ffma ? ` <span class="ff-tier ff-unc">FFMA</span>` : ""}</td>
        <td>${rarity}</td>
        <td>${cr}</td>
        <td>${tag}</td>
      </tr>`;
    }).join("");
    cards.push(`<details class="score-card ff-disc ff-collapse" ${ffDiscOpen ? "open" : ""}>
      <summary>&#x26A0;&#xFE0F; Grid log discrepancies <span class="ff-count">${gd.count}</span></summary>
      <table class="ff-table">
        <tr><th>band</th><th>station</th><th>date</th><th>advertised</th><th>rarity</th><th>LoTW credits</th><th>cause</th></tr>
        ${rows}</table>
    </details>`);
  }

  el.innerHTML = cards.join("");
  // persist collapse state across re-renders (headers act as anchors on long scroll)
  const bindFf = (cls, set) => {
    const dt = el.querySelector("details." + cls);
    if (dt) dt.addEventListener("toggle", () => set(dt.open));
  };
  bindFf("ff-atno",    o => ffAtnoOpen = o);
  bindFf("ff-pending", o => ffPendOpen = o);
  bindFf("ff-rares",   o => ffRaresOpen = o);
  bindFf("ff-disc",    o => ffDiscOpen = o);
}
function fetchScores() {
  fetch("/api/scores").then(r => r.json()).then(renderScores)
    .catch(e => console.error("scores fetch failed:", e));
}
fetchScores();
setInterval(fetchScores, 5 * 60 * 1000);  // refresh every 5 min — matches worked_state reload cadence

// ============== Push-alerts config (per-band × per-mode matrix) ==============
let alertsLoaded = false;
function loadAlerts() {
  fetch("/api/alerts").then(r => r.json()).then(renderAlerts)
    .catch(e => { const b = document.getElementById("alerts_body"); if (b) b.textContent = "load failed: " + e; });
}
function updateAlertState(cfg) {
  const el = document.getElementById("alerts_state");
  if (!el) return;
  const n = Object.values(cfg.cells || {}).reduce((a, m) => a + Object.keys(m).length, 0);
  el.textContent = cfg.enabled ? (n ? `on · ${n} cells` : "on · none set") : "off";
}
function renderAlerts(d) {
  const cfg = d.config || { enabled: false, cooldown_min: 30, cells: {} };
  const cells = cfg.cells || {};
  const credRow = `<div class="alerts-credrow" id="alerts_credrow">${d.creds
    ? `<span class="alerts-ok">&#x2705; Pushover connected</span> &middot; <a href="#" id="po_change">change creds</a>`
    : `<span class="alerts-warn">&#x26A0; Pushover not set up yet</span>`}</div>`;
  const credForm = `<div class="alerts-creds" id="po_form" ${d.creds ? 'style="display:none"' : ''}>
      <input type="text" id="po_user" placeholder="Pushover user key" autocomplete="off">
      <input type="text" id="po_token" placeholder="Pushover API token" autocomplete="off">
      <button id="po_save">Save &amp; test</button>
      <span id="po_status" class="alerts-saved"></span>
      <div class="mode-hint">Both come from <b>pushover.net</b> &mdash; your <b>User Key</b> on the dashboard, then create an <b>Application</b> for the <b>API Token</b>. Saved to the server; you'll get a test push to confirm.</div>
    </div>`;
  const head = "<tr><th>band</th>" + d.modes.map(m => `<th colspan="2">${m}</th>`).join("") + "</tr>";
  const sub = "<tr><th></th>" + d.modes.map(() => `<th class="alerts-sub">need</th><th class="alerts-sub">open</th>`).join("") + "</tr>";
  const rows = d.bands.map(b => {
    const tds = d.modes.map(m => {
      const c = (cells[b] && cells[b][m]) || {};
      return `<td><input type="checkbox" data-b="${b}" data-m="${m}" data-f="needed" ${c.needed ? "checked" : ""}></td>`
           + `<td><input type="checkbox" data-b="${b}" data-m="${m}" data-f="open" ${c.open ? "checked" : ""}></td>`;
    }).join("");
    return `<tr><td class="alerts-band">${b}</td>${tds}</tr>`;
  }).join("");
  document.getElementById("alerts_body").innerHTML = credRow + credForm + `
    <div class="alerts-top">
      <label><input type="checkbox" id="alerts_enabled" ${cfg.enabled ? "checked" : ""}> <b>Enable push alerts</b></label>
      <label style="margin-left:1.2em">cooldown <input type="number" id="alerts_cooldown" min="1" max="240" value="${cfg.cooldown_min || 30}" style="width:3.5em"> min</label>
      <label style="margin-left:1.2em" title="Alert only on spots YOUR OWN receiver decoded (WSJT-X / SparkGap), not other stations' spots. A nearby big-gun (e.g. a buddy's superstation) hears DX you can't, and those slip through the distance filter as 'local' — this gates on what you can actually hear."><input type="checkbox" id="alerts_local" ${cfg.local_only === false ? "" : "checked"}> my RX only</label>
      <button id="alerts_test" style="margin-left:1.2em">Send test</button>
      <span id="alerts_saved" class="alerts-saved"></span>
    </div>
    <table class="alerts-matrix">${head}${sub}${rows}</table>
    <div class="mode-hint">Each cell &mdash; <b>need</b>: ping on a wanted entity/grid for that band's award scope · <b>open</b>: ping on any local activity (band's alive). Es/tropo modes gate on local spotters; <b>MSK144</b> uses continental meteor-scatter range. Blank row = silence (the default). "Calls you" always pings regardless. Changes save instantly.</div>`;
  const body = document.getElementById("alerts_body");
  body.querySelectorAll('input[type=checkbox][data-b], #alerts_enabled, #alerts_cooldown, #alerts_local').forEach(el =>
    el.addEventListener("change", saveAlerts));
  document.getElementById("alerts_test").addEventListener("click", testAlerts);
  const poSave = document.getElementById("po_save");
  if (poSave) poSave.addEventListener("click", saveCreds);
  const poChange = document.getElementById("po_change");
  if (poChange) poChange.addEventListener("click", e => {
    e.preventDefault();
    const f = document.getElementById("po_form");
    if (f) f.style.display = "";
  });
  updateAlertState(cfg);
}
function saveCreds() {
  const user = document.getElementById("po_user").value.trim();
  const token = document.getElementById("po_token").value.trim();
  const st = document.getElementById("po_status");
  if (!user || !token) { if (st) st.textContent = "enter both fields"; return; }
  if (st) st.textContent = "saving…";
  fetch("/api/alerts/creds", { method: "POST", body: JSON.stringify({ user, token }) })
    .then(r => r.json()).then(d => {
      if (!d.ok) { if (st) st.textContent = d.error || "save failed"; return; }
      return fetch("/api/alerts/test", { method: "POST" }).then(r => r.json()).then(t => {
        if (st) st.textContent = t.ok ? "saved ✓ — test sent, check your phone" : ("saved, but test failed: " + (t.error || ""));
        setTimeout(loadAlerts, 3500);   // refresh to the connected state
      });
    }).catch(e => { if (st) st.textContent = "error: " + e; });
}
function gatherAlerts() {
  const cells = {};
  document.querySelectorAll("#alerts_body input[type=checkbox][data-b]").forEach(el => {
    if (!el.checked) return;
    const b = el.dataset.b, m = el.dataset.m, f = el.dataset.f;
    (cells[b] = cells[b] || {})[m] = cells[b][m] || {};
    cells[b][m][f] = true;
  });
  return {
    enabled: document.getElementById("alerts_enabled").checked,
    local_only: document.getElementById("alerts_local").checked,
    cooldown_min: parseInt(document.getElementById("alerts_cooldown").value) || 30,
    cells,
  };
}
function saveAlerts() {
  const cfg = gatherAlerts();
  fetch("/api/alerts", { method: "POST", body: JSON.stringify(cfg) })
    .then(r => r.json()).then(() => {
      const s = document.getElementById("alerts_saved");
      if (s) { s.textContent = "saved"; setTimeout(() => { s.textContent = ""; }, 1500); }
      updateAlertState(cfg);
    });
}
function testAlerts() {
  fetch("/api/alerts/test", { method: "POST" }).then(r => r.json()).then(d => {
    const s = document.getElementById("alerts_saved");
    if (s) { s.textContent = d.ok ? "test sent ✓" : (d.error || "failed"); setTimeout(() => { s.textContent = ""; }, 3500); }
  });
}
const _alertsPanel = document.getElementById("alerts_panel");
if (_alertsPanel) _alertsPanel.addEventListener("toggle", function () {
  if (this.open && !alertsLoaded) { alertsLoaded = true; loadAlerts(); }
});
loadAlerts();  // populate the summary state on load

// ============== Log Sync (manual QRZ + LoTW buttons) ==============
function fmtAgo(epoch, now){
  if(!epoch) return "never synced";
  const s = Math.max(0, now - epoch);
  if(s < 90) return "synced just now";
  if(s < 5400) return "synced " + Math.round(s/60) + "m ago";
  if(s < 172800) return "synced " + Math.round(s/3600) + "h ago";
  return "synced " + Math.round(s/86400) + "d ago";
}
async function refreshSyncStatus(){
  try{
    const d = await (await fetch("/api/sync/status", {cache:"no-store"})).json();
    const q=document.getElementById("sync_qrz_status"), l=document.getElementById("sync_lotw_status");
    if(q && !q.dataset.busy) q.textContent = fmtAgo(d.qrz, d.now);
    if(l && !l.dataset.busy) l.textContent = fmtAgo(d.lotw, d.now);
  }catch(e){}
}
function wireSync(btnId, statusId, url){
  const btn=document.getElementById(btnId), st=document.getElementById(statusId);
  if(!btn) return;
  btn.addEventListener("click", async ()=>{
    btn.disabled=true; st.dataset.busy="1"; st.className="sync-status"; st.textContent="syncing…";
    try{
      const d = await (await fetch(url,{method:"POST"})).json();
      st.className = "sync-status " + (d.ok ? "ok" : "err");
      st.textContent = (d.ok ? "✓ " : "✗ ") + (d.message || "");
      if(d.ok){ refresh(); fetchScores(); }
    }catch(e){
      st.className="sync-status err"; st.textContent="✗ " + e.message;
    }finally{
      btn.disabled=false; delete st.dataset.busy;
      setTimeout(refreshSyncStatus, 4000);
    }
  });
}
wireSync("sync_qrz","sync_qrz_status","/api/sync/qrz");
wireSync("sync_lotw","sync_lotw_status","/api/sync/lotw");
refreshSyncStatus();
setInterval(refreshSyncStatus, 60000);

// ============== Log search tab ==============
let _logSearchTimer = null;
let _logOffset = 0;
let _logTotal = 0;
let _logLastFetched = false;

function fmtQsoTime(date, time) {
  // ADIF date YYYYMMDD, time HHMM[SS]. Render as YYYY-MM-DD HH:MM.
  if (!date || date.length < 8) return date || "";
  const d = `${date.substr(0,4)}-${date.substr(4,2)}-${date.substr(6,2)}`;
  const t = (time && time.length >= 4) ? ` ${time.substr(0,2)}:${time.substr(2,2)}` : "";
  return d + t;
}
function fmtQslMethod(q) {
  const parts = [];
  if (q.lotw) parts.push("L");
  if (q.eqsl) parts.push("e");
  if (q.paper && !q.lotw && !q.eqsl) parts.push("Q");
  return parts.join("") || "—";
}

function buildLogQuery() {
  const limit = parseInt(document.getElementById("lf_pagesize").value, 10) || 100;
  const params = new URLSearchParams();
  const call = document.getElementById("lf_call").value.trim();
  const band = document.getElementById("lf_band").value;
  const mode = document.getElementById("lf_mode").value;
  const dxcc = document.getElementById("lf_dxcc").value.trim();
  const grid = document.getElementById("lf_grid").value.trim();
  if (call) params.set("call", call);
  if (band) params.set("band", band);
  if (mode) params.set("mode", mode);
  if (dxcc) params.set("dxcc", dxcc);
  if (grid) params.set("grid", grid);
  params.set("offset", String(_logOffset));
  params.set("limit", String(limit));
  return { qs: params.toString(), limit };
}

function renderLogSearch(j) {
  const meta = document.getElementById("log_search_meta");
  const out = document.getElementById("log_search_results");
  const pageinfo = document.getElementById("lf_pageinfo");
  if (!j || j.error) {
    meta.textContent = (j && j.error) || "search unavailable";
    out.innerHTML = "";
    pageinfo.textContent = "—";
    return;
  }
  _logTotal = j.count;
  const limit = j.limit || 100;
  const totalPages = Math.max(1, Math.ceil(j.count / limit));
  const curPage = Math.floor(j.offset / limit) + 1;
  const start = j.count === 0 ? 0 : j.offset + 1;
  const end = Math.min(j.offset + j.returned, j.count);
  meta.textContent = j.count === 0 ? "no QSOs match" : `${start.toLocaleString()}–${end.toLocaleString()} of ${j.count.toLocaleString()}`;
  pageinfo.textContent = `page ${curPage} / ${totalPages}`;
  document.getElementById("lf_first").disabled = curPage === 1;
  document.getElementById("lf_prev").disabled = curPage === 1;
  document.getElementById("lf_next").disabled = curPage >= totalPages;
  document.getElementById("lf_last").disabled = curPage >= totalPages;

  if (!j.qsos.length) { out.innerHTML = `<div class="empty">no matches</div>`; return; }
  const rows = [`<table><tr>
    <th>Date UTC</th><th>Call</th><th>Band</th><th>Mode</th><th>Freq</th>
    <th>Country</th><th>Grid</th><th>State</th><th>Zone</th><th>QSL</th>
  </tr>`];
  for (const q of j.qsos) {
    const qsl = fmtQslMethod(q);
    const cls = q.lotw ? "lotw" : (q.confirmed ? "" : "unconf");
    rows.push(`<tr>
      <td>${fmtQsoTime(q.qso_date, q.time_on)}</td>
      <td class="q-call">${q.call}</td>
      <td class="band">${q.band}</td>
      <td class="mode">${q.mode}</td>
      <td class="freq">${q.freq || ""}</td>
      <td>${q.country}</td>
      <td class="grid">${q.grid || ""}</td>
      <td>${q.state || ""}</td>
      <td>${q.cqz || ""}</td>
      <td class="q-conf ${cls}">${qsl}</td>
    </tr>`);
  }
  rows.push(`</table>`);
  out.innerHTML = rows.join("");
}

function fetchLog() {
  const { qs } = buildLogQuery();
  fetch("/api/log/search?" + qs)
    .then(r => r.json())
    .then(j => { _logLastFetched = true; renderLogSearch(j); })
    .catch(e => console.error("log search failed:", e));
}

function debouncedFetchLog() {
  clearTimeout(_logSearchTimer);
  _logSearchTimer = setTimeout(() => { _logOffset = 0; fetchLog(); }, 200);
}

// Wire all filter inputs
for (const id of ["lf_call","lf_dxcc","lf_grid"]) {
  document.getElementById(id).addEventListener("input", debouncedFetchLog);
}
for (const id of ["lf_band","lf_mode","lf_pagesize"]) {
  document.getElementById(id).addEventListener("change", () => { _logOffset = 0; fetchLog(); });
}
document.getElementById("lf_clear").addEventListener("click", () => {
  for (const id of ["lf_call","lf_dxcc","lf_grid"]) document.getElementById(id).value = "";
  for (const id of ["lf_band","lf_mode"]) document.getElementById(id).value = "";
  _logOffset = 0; fetchLog();
});

// Auto-refresh the log so newly-logged QSOs surface — it otherwise fetched only
// on load / filter change and never re-polled, so the view sat stale. Refresh
// only when the log tab is actually visible and has been opened at least once;
// re-running fetchLog() preserves the current filters and page.
setInterval(() => {
  const out = document.getElementById("log_search_results");
  if (_logLastFetched && out && out.offsetParent !== null) fetchLog();
}, 15000);

// Pagination buttons
document.getElementById("lf_first").addEventListener("click", () => { _logOffset = 0; fetchLog(); });
document.getElementById("lf_prev").addEventListener("click", () => {
  const limit = parseInt(document.getElementById("lf_pagesize").value, 10) || 100;
  _logOffset = Math.max(0, _logOffset - limit);
  fetchLog();
});
document.getElementById("lf_next").addEventListener("click", () => {
  const limit = parseInt(document.getElementById("lf_pagesize").value, 10) || 100;
  if (_logOffset + limit < _logTotal) { _logOffset += limit; fetchLog(); }
});
document.getElementById("lf_last").addEventListener("click", () => {
  const limit = parseInt(document.getElementById("lf_pagesize").value, 10) || 100;
  _logOffset = Math.max(0, Math.floor((_logTotal - 1) / limit) * limit);
  fetchLog();
});

// Lazy-load: fetch the whole-log view the first time the Log tab opens.
function maybeLoadLog() {
  if (!_logLastFetched) fetchLog();
}
for (const btn of document.querySelectorAll(".view-tabs button")) {
  if (btn.dataset.view === "log") btn.addEventListener("click", maybeLoadLog);
}

// Right-click any spot row → switch to Log tab and search that call
document.addEventListener("contextmenu", (ev) => {
  const row = ev.target.closest("tr.clickable");
  if (!row) return;
  const callCell = row.querySelector("td.dx");
  if (!callCell) return;
  ev.preventDefault();
  const call = callCell.textContent.trim();
  switchView("log");
  // Clear other filters so the call jump is unambiguous
  for (const id of ["lf_band","lf_mode","lf_dxcc","lf_grid"]) document.getElementById(id).value = "";
  const inp = document.getElementById("lf_call");
  inp.value = call;
  _logOffset = 0;
  fetchLog();
  inp.focus();
});
</script>
</body></html>
"""


# ---------------- HTTP ----------------
# ---------------- Manual log-sync helpers (QRZ logbook + LoTW) ----------------
def _do_lotw_fetch_once():
    """Run one incremental LoTW pull. Returns (ok: bool, n_appended: int, msg).
    Shared by the periodic loop and the manual /api/sync/lotw endpoint."""
    try:
        user, pw = lotw_fetch.load_creds()
        cursor_ms = lotw_fetch.load_cursor_ms()
        qslsince = lotw_fetch.cursor_to_lotw_string(cursor_ms)
        url = lotw_fetch.build_url(user, pw, qslsince, mode="fetch")
        body = lotw_fetch.fetch(url)
        if lotw_fetch.is_password_incorrect(body):
            return (False, 0, "LoTW auth rejected — check credentials")
        n = lotw_fetch.append_adif(body)
        if n > 0:
            max_ms = lotw_fetch.find_max_rxqsl_ms(body)
            if max_ms > 0:
                lotw_fetch.save_cursor_ms(max_ms + 1000)
        return (True, n, f"{n} new confirmation(s)")
    except Exception as e:
        return (False, 0, f"LoTW fetch failed: {e}")


def _do_qrz_fetch_once():
    """Pull the full QRZ logbook into qrz_logbook.json by running the standalone
    fetcher as a subprocess (it owns the exact file format). Returns (ok, n, msg)."""
    import subprocess
    import sys as _sys
    script = str(Path(__file__).parent / "qrz_logbook_fetch.py")
    try:
        r = subprocess.run([_sys.executable, script],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip().splitlines()
            return (False, 0, "QRZ fetch failed: " + (err[-1] if err else "unknown error"))
        d = json.loads(LOGBOOK_PATH.read_text())
        n = d.get("meta", {}).get("qso_count") or len(d.get("qsos", []))
        return (True, n, f"{n:,} QSOs")
    except Exception as e:
        return (False, 0, f"QRZ fetch failed: {e}")


def _sync_reload_after_fetch():
    """After a manual fetch updates a source file, recompute worked-state and
    refresh the cached spot pills so the UI reflects the new data immediately."""
    if _worked:
        _worked.force_reload()
    _refresh_cache_worked_status()


def _sync_status() -> dict:
    """Last-synced mtimes (epoch secs) for the two log sources, for the UI."""
    lotw_path = LOGBOOK_PATH.parent / "lotw_qsl.adi"
    def mt(p):
        try:
            return p.stat().st_mtime
        except Exception:
            return None
    return {"now": time.time(), "qrz": mt(LOGBOOK_PATH), "lotw": mt(lotw_path)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet access log
        return

    def _send(self, body, ctype, status=200):
        # Transparent gzip: huge win for /spots.json (~1.2MB JSON → ~150KB)
        # over mobile/Tailscale. Only when the client advertises gzip and the
        # body is big enough that compression overhead pays for itself.
        encoding = None
        if len(body) >= 1024 and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, compresslevel=6)
            encoding = "gzip"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if encoding:
            self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            # Inject the FFMA grid list as a JS array literal into the page so
            # the client doesn't need a second fetch + cache step. ~3KB.
            ffma_js = ",".join('"' + g + '"' for g in _FFMA_GRIDS)
            page = (HTML_PAGE.replace("__FFMA_GRIDS_INJECT__", ffma_js)
                    .replace("__MY_CALLSIGN__", CALLSIGN)
                    .replace("__PEER_RADIUS_MI__", str(int(PEER_RADIUS_MI)))
                    .replace("__MY_GRID__", HOME_GRID or "your QTH")
                    .replace("__LOTW_FRESH_DAYS__", str(LOTW_FRESH_DAYS)))
            self._send(page.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/spots.json":
            # Enrich shallow copies (never mutate the cache) with live local-peer
            # copies — only spots a neighbor actually heard carry the field, so the
            # payload stays lean.
            spots = []
            for s in snapshot():
                pc = peer_copies.copies(s.get("dx_call", ""))
                spots.append({**s, "peer_copies": pc} if pc else s)
            payload = {"spots": spots, "now": time.time(), "worked_rev": _WORKED_REV}
            self._send(json.dumps(payload).encode(), "application/json")
        elif self.path == "/active_bands":
            payload = active_bands_snapshot()
            self._send(json.dumps(payload).encode(), "application/json")
        elif self.path == "/wsjtx_state":
            # Diagnostic: current per-client WSJT-X state (dial freq, mode, last seen)
            with _wsjtx_state_lock:
                payload = {"clients": list(_wsjtx_state.values()), "now": time.time()}
            self._send(json.dumps(payload, default=str).encode(), "application/json")
        elif self.path == "/api/scores":
            self._send(json.dumps(_build_scores_payload()).encode(), "application/json")
        elif self.path == "/api/band_activity":
            self._send(json.dumps(_band_activity_payload()).encode(), "application/json")
        elif self.path in ("/bands", "/bands.html", "/band_activity"):
            self._send(_BAND_ACTIVITY_PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/sync/status":
            self._send(json.dumps(_sync_status()).encode(), "application/json")
        elif self.path == "/api/alerts":
            tok, usr = _pushover_creds()
            self._send(json.dumps({
                "config": _ALERTS, "bands": ALERT_BANDS, "modes": ALERT_MODES,
                "creds": bool(tok and usr),
            }).encode(), "application/json")
        elif self.path.startswith("/api/log/search"):
            self._handle_log_search()
        else:
            self._send(b"not found", "text/plain", 404)

    def do_POST(self):
        if self.path == "/api/tune":
            self._handle_tune()
            return
        if self.path in ("/api/sync/qrz", "/api/sync/lotw"):
            self._handle_sync("qrz" if self.path.endswith("qrz") else "lotw")
            return
        if self.path == "/api/alerts":
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0 or length > 65536:
                self._send(json.dumps({"ok": False, "error": "bad length"}).encode(),
                           "application/json", 400)
                return
            try:
                cfg = json.loads(self.rfile.read(length))
                _save_alerts(cfg)
            except Exception as e:
                self._send(json.dumps({"ok": False, "error": f"bad JSON: {e}"}).encode(),
                           "application/json", 400)
                return
            self._send(json.dumps({"ok": True}).encode(), "application/json")
            return
        if self.path == "/api/alerts/test":
            ok = _send_pushover("Grayline — test", "Push alerts are wired up. 73", 0)
            self._send(json.dumps({"ok": ok,
                                   "error": "" if ok else "no pushover creds in secrets.json or send failed"}).encode(),
                       "application/json", 200 if ok else 400)
            return
        if self.path == "/api/alerts/creds":
            # Let a non-technical user set Pushover creds from the panel (no
            # hand-editing secrets.json). Merge into secrets.json (preserving the
            # other secrets), re-lock to 0600. LAN-only single-op install; these
            # are low-stakes send-only creds. Never echoed back via GET.
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0 or length > 8192:
                self._send(json.dumps({"ok": False, "error": "bad length"}).encode(),
                           "application/json", 400)
                return
            try:
                body = json.loads(self.rfile.read(length))
                user = (body.get("user") or "").strip()
                token = (body.get("token") or "").strip()
            except Exception as e:
                self._send(json.dumps({"ok": False, "error": f"bad JSON: {e}"}).encode(),
                           "application/json", 400)
                return
            if not user or not token:
                self._send(json.dumps({"ok": False, "error": "both user key and API token are required"}).encode(),
                           "application/json", 400)
                return
            try:
                p = _BASE_DIR / "secrets.json"
                try:
                    sec = json.loads(p.read_text())
                except Exception:
                    sec = {}
                sec["pushover_user"] = user
                sec["pushover_token"] = token
                tmp = p.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(sec, indent=1))
                tmp.replace(p)
                p.chmod(0o600)
            except Exception as e:
                self._send(json.dumps({"ok": False, "error": f"save failed: {e}"}).encode(),
                           "application/json", 500)
                return
            self._send(json.dumps({"ok": True}).encode(), "application/json")
            return
        self._send(b"not found", "text/plain", 404)

    def _handle_sync(self, source):
        """POST /api/sync/{qrz,lotw} — run the fetch (blocking; the server is
        threaded so other requests aren't held up), reload worked-state on
        success, return the result + fresh status."""
        ok, n, msg = _do_qrz_fetch_once() if source == "qrz" else _do_lotw_fetch_once()
        if ok:
            _sync_reload_after_fetch()
        log.info("Manual %s sync: %s [%s]", source.upper(), msg, "ok" if ok else "FAILED")
        resp = {"ok": ok, "count": n, "message": msg, "status": _sync_status()}
        self._send(json.dumps(resp).encode(), "application/json", 200 if ok else 500)

    def _handle_log_search(self):
        """GET /api/log/search[?call=...&band=...&mode=...&dxcc=...&grid=...&offset=N&limit=M]

        All filters are optional and AND-combined. Filters:
          call    — substring match on callsign (case-insensitive)
          band    — exact match (lowercase, e.g. "20m")
          mode    — exact match (uppercase, e.g. "FT8" or "CW")
          dxcc    — substring match on country name (case-insensitive)
                    or exact DXCC ID match if numeric
          grid    — substring match on grid square (case-insensitive)

        With no filters, returns the entire log. Sorted newest-first by
        qso_date+time_on. Pagination via offset+limit; default 100/page.
        """
        if not _worked:
            self._send(json.dumps({"qsos": [], "error": "worked_state not loaded"}).encode(),
                       "application/json")
            return
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        call = (qs.get("call", [""])[0] or "").strip().upper()
        band = (qs.get("band", [""])[0] or "").strip().lower()
        mode = (qs.get("mode", [""])[0] or "").strip().upper()
        dxcc = (qs.get("dxcc", [""])[0] or "").strip()
        grid = (qs.get("grid", [""])[0] or "").strip().upper()
        try:
            limit = max(1, min(int(qs.get("limit", ["100"])[0]), 1000))
        except ValueError:
            limit = 100
        try:
            offset = max(0, int(qs.get("offset", ["0"])[0]))
        except ValueError:
            offset = 0

        dxcc_is_id = dxcc.isdigit()
        dxcc_lower = dxcc.lower()

        def matches(q: dict) -> bool:
            if call and call not in (q.get("call") or "").upper():
                return False
            if band and (q.get("band") or "").lower() != band:
                return False
            if mode and (q.get("mode") or "").upper() != mode:
                return False
            if dxcc:
                if dxcc_is_id:
                    if (q.get("dxcc") or "").strip() != dxcc:
                        return False
                else:
                    if dxcc_lower not in (q.get("country") or "").lower():
                        return False
            if grid and grid not in (q.get("grid") or "").upper():
                return False
            return True

        all_matches = [q for q in _worked.qsos if matches(q)]
        # Sort newest first (qso_date YYYYMMDD + time_on HHMM[SS] — string sort works)
        all_matches.sort(
            key=lambda q: ((q.get("qso_date") or "") + (q.get("time_on") or "")),
            reverse=True)
        page = all_matches[offset:offset + limit]

        out = []
        for q in page:
            confirmed = (q.get("lotw_qsl_rcvd") or "").upper() in ("Y", "V") or \
                        (q.get("qsl_rcvd") or "").upper() in ("Y", "V") or \
                        (q.get("eqsl_qsl_rcvd") or "").upper() in ("Y", "V")
            out.append({
                "call": q.get("call", ""),
                "qso_date": q.get("qso_date", ""),
                "time_on": q.get("time_on", ""),
                "band": q.get("band", ""),
                "mode": q.get("mode", ""),
                "freq": q.get("freq", ""),
                "country": q.get("country", ""),
                "grid": q.get("grid", ""),
                "state": q.get("state", ""),
                "cqz": q.get("cqz", ""),
                "confirmed": confirmed,
                "lotw": (q.get("lotw_qsl_rcvd") or "").upper() in ("Y", "V"),
                "eqsl": (q.get("eqsl_qsl_rcvd") or "").upper() in ("Y", "V"),
                "paper": (q.get("qsl_rcvd") or "").upper() in ("Y", "V"),
            })
        self._send(json.dumps({
            "filters": {"call": call, "band": band, "mode": mode,
                        "dxcc": dxcc, "grid": grid},
            "count": len(all_matches),
            "offset": offset,
            "limit": limit,
            "returned": len(out),
            "qsos": out,
        }).encode(), "application/json")

    def _handle_tune(self):
        """Click-to-tune endpoint. Body: JSON {dx_call, freq_khz, mode?}.

        Routing rule (operator-respect):
        - If spot mode is a WSJT-X mode (FT8/FT4/JT65/JT9/MSK144/Q65/etc.):
          route EXCLUSIVELY to WSJT-X. Send Reply UDP IFF a WSJT-X instance
          is currently tuned to the spot's band AND the audio offset falls
          within the passband (200..3000 Hz). Otherwise: do nothing.
          Never touch the Flex slice for a WSJT-X-mode click — that would
          disrupt whatever the operator has dialed in for the digital QSO.
        - If spot mode is a non-WSJT-X mode (CW/SSB/RTTY/etc.):
          retune the Flex slice on that band to the spot frequency.
        """
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0 or length > 4096:
            self._send(json.dumps({"ok": False, "error": "bad length"}).encode(),
                       "application/json", 400)
            return
        try:
            body = json.loads(self.rfile.read(length))
            dx_call = (body.get("dx_call") or "").strip()
            freq_khz = float(body.get("freq_khz") or 0)
            mode_in = (body.get("mode") or "").strip().upper()
        except (ValueError, TypeError, KeyError) as e:
            self._send(json.dumps({"ok": False, "error": f"bad JSON: {e}"}).encode(),
                       "application/json", 400)
            return
        if not dx_call or freq_khz <= 0:
            self._send(json.dumps({"ok": False, "error": "missing dx_call/freq_khz"}).encode(),
                       "application/json", 400)
            return

        spot_band = dxcluster.freq_to_band(freq_khz)
        results = {"ok": False, "route": None, "message": ""}

        # ---- WSJT-X-mode routing: exclusive, band-gated ----
        if mode_in in WSJTX_MODES:
            results["route"] = "wsjtx"
            state = _wsjtx_state_for_band(spot_band)
            if state is None:
                results["message"] = (f"no WSJT-X on {spot_band}; no action "
                                      f"(operator-respect rule for {mode_in})")
                self._send(json.dumps(results).encode(), "application/json")
                return
            dial_khz = state["dial_freq_hz"] / 1000.0
            audio_hz = round((freq_khz - dial_khz) * 1000)
            if not (WSJTX_AUDIO_MIN_HZ <= audio_hz <= WSJTX_AUDIO_MAX_HZ):
                results["message"] = (f"audio offset {audio_hz} Hz out of band "
                                      f"[{WSJTX_AUDIO_MIN_HZ}..{WSJTX_AUDIO_MAX_HZ}]; "
                                      f"WSJT-X dial would need adjusting first")
                self._send(json.dumps(results).encode(), "application/json")
                return

            # WSJT-X matches Reply packets by the original FT8 message text
            # (and approximate snr/time/delta_freq). Look up the cached spot
            # to pull the exact message + snr that were originally broadcast.
            # If the spot came from WSJT-X locally, comment IS the FT8 message
            # text. For external spots we synthesize a plausible CQ string —
            # WSJT-X's fuzzy match may or may not lock on, but we try.
            cache_key = _spot_dedup_key(spot_band, mode_in, freq_khz, dx_call)
            with _lock:
                cached = _cache.get(cache_key)
            if cached and cached.get("source") == "WSJTX-LOCAL":
                msg_text = cached.get("comment") or f"CQ {dx_call}"
                spot_snr = cached.get("snr")
                if spot_snr is None:
                    spot_snr = -15
            else:
                # External spot — best-effort synthetic CQ. Won't match if
                # WSJT-X hasn't decoded this signal in its recent list.
                msg_text = f"CQ {dx_call}"
                spot_snr = cached.get("snr", -15) if cached else -15
                if spot_snr is None:
                    spot_snr = -15

            # Use the original decode's time + delta_time + mode-glyph from cache
            # (stored at ingest). WSJT-X uses these as keys to find the matching
            # decode in its history. Falling back to current time would fail to match.
            import wsjtx_udp
            if cached and cached.get("source") == "WSJTX-LOCAL":
                reply_time_ms = cached.get("wsjtx_time_ms", 0)
                reply_delta_time = cached.get("wsjtx_delta_time", 0.0)
                reply_glyph = cached.get("wsjtx_glyph", "~")
            else:
                # External spot fallback — WSJT-X likely won't match, but try anyway.
                reply_time_ms = wsjtx_udp.current_time_ms()
                reply_delta_time = 0.0
                reply_glyph = "+" if mode_in == "FT4" else "~"

            try:
                pkt = wsjtx_udp.reply(
                    client_id=state["client_id"],
                    time_ms=reply_time_ms,
                    snr=int(spot_snr),
                    delta_time=reply_delta_time,
                    delta_freq=audio_hz,
                    mode=reply_glyph,
                    message=msg_text,
                    low_confidence=False,
                    modifiers=0,
                )
                import socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.sendto(pkt, state["source_addr"])
                sock.close()
                results["ok"] = True
                results["message"] = (f"WSJT-X reply sent: client={state['client_id']} "
                                      f"audio={audio_hz} Hz dial={dial_khz:.1f} kHz "
                                      f"snr={spot_snr} msg={msg_text!r} "
                                      f"time_ms={reply_time_ms} dt={reply_delta_time:.2f} glyph={reply_glyph!r} "
                                      f"src={cached.get('source','?') if cached else 'no-cache'}")
            except Exception as e:
                results["message"] = f"WSJT-X reply send failed: {e}"
            self._send(json.dumps(results).encode(), "application/json")
            return

        # ---- Non-WSJT-X mode: Flex slice retune ----
        results["route"] = "flex"
        if not _flex or not _flex.connected:
            results["message"] = "Flex not connected"
            self._send(json.dumps(results).encode(), "application/json")
            return
        slice_id = None
        for sn, info in _flex.slices.items():
            if info.get("in_use") != "1":
                continue
            try:
                slice_freq_mhz = float(info.get("RF_frequency", "0"))
            except ValueError:
                continue
            slice_band = dxcluster.freq_to_band(slice_freq_mhz * 1000)
            if slice_band == spot_band:
                slice_id = sn
                break
        if slice_id is None:
            results["message"] = f"no active Flex slice on {spot_band}; no action"
            self._send(json.dumps(results).encode(), "application/json")
            return
        if _main_loop is None:
            results["message"] = "main loop not available (server still starting)"
            self._send(json.dumps(results).encode(), "application/json")
            return
        try:
            asyncio.run_coroutine_threadsafe(
                _flex.tune(slice_id, freq_khz / 1000.0),
                _main_loop)
            results["ok"] = True
            results["message"] = f"Flex slice {slice_id} retuned to {freq_khz:.1f} kHz ({mode_in or 'mode unknown'})"
        except Exception as e:
            results["message"] = f"Flex tune failed: {e}"
        self._send(json.dumps(results).encode(), "application/json")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def serve_http():
    srv = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    log.info("HTTP listening on http://0.0.0.0:%d/", HTTP_PORT)
    srv.serve_forever()


# ---------------- main ----------------
async def on_spot(spot, cluster_name):
    add_spot(spot, cluster_name)


async def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    global _cty, _worked, _flex, _main_loop, _telnet_feed
    _main_loop = asyncio.get_running_loop()
    load_qrz_cache()  # initial load before any spots come in
    load_ja_pref_cache()  # JA prefecture cache for the WAJA pill
    try:
        _cty = CtyDat(str(CTY_DAT_PATH))
        log.info("cty.dat loaded: %d entities, %d prefixes", _cty.entity_count, _cty.prefix_count)
    except Exception as e:
        log.warning("cty.dat load failed (DXCC enrichment disabled): %s", e)
    try:
        dxmarathon.load()   # official CQ DX Marathon 346-entity / 40-zone list
        _worked = WorkedState(str(LOGBOOK_PATH))
        # Share Grayline's QRZ-derived JA prefecture cache so the WAJA scores grid
        # can resolve prefecture for FT8 QSOs that carry no ADIF STATE (e.g. JH0RNN
        # -> Niigata), matching what the live roster pill already shows. The ctor
        # already ran one reload(), so rebuild now that the cache is attached.
        _worked.ja_pref_lookup = _ja_pref_cache
        _worked.force_reload()
        _rebuild_marathon()   # current-year Marathon worked sets from the logbook
        _rebuild_wae()        # lifetime WAE worked/confirmed sets for the roster pill
    except Exception as e:
        log.warning("worked_state load failed (worked-status disabled): %s", e)

    def worked_state_reload_loop():
        while True:
            # Check every 30s. reload() is mtime-gated — when nothing changed it's
            # just 3 stat() calls and an early return, so frequent checks are
            # nearly free; the full re-merge only runs when a source file actually
            # changes (a QSO logged, QRZ/LoTW pull). Keeps the searchable log /
            # award sets within ~30s of a fresh QSO instead of up to 5 min.
            time.sleep(30)
            # Daily gridzilla_ffma_fetch.py cron rewrites data/ffma_rarity.json;
            # pick it up here (mtime-gated, ~free when unchanged).
            _maybe_reload_ffma_rarity()
            if _worked:
                if _worked.reload():
                    # If anything actually reloaded (mtime changed on either
                    # the QRZ JSON or the local Grayline ADIF), re-evaluate
                    # worked-state-derived fields on every cached spot so
                    # award pills reflect the freshest worked/confirmed data.
                    # Otherwise stale dxcc_band_status etc. baked in at
                    # ingest time would persist until each spot ages out.
                    _rebuild_marathon()   # keep Marathon year-sets in lockstep
                    _rebuild_wae()        # keep WAE sets in lockstep (before cache refresh uses them)
                    _refresh_cache_worked_status()
                    log.info("worked_state reloaded — cache statuses refreshed")

    def lotw_fetch_loop():
        """Periodic incremental pull of LoTW confirmations. Each tick appends
        new records to lotw_qsl.adi; the worked_state_reload_loop tick that
        follows picks up the file mtime change and re-merges into worked-state."""
        log.info("LoTW fetch loop armed (interval=%ds)", LOTW_FETCH_INTERVAL_SEC)
        # First tick: short delay so we don't fire concurrently with other
        # startup work (cty.dat load, worked-state initial merge).
        time.sleep(60)
        first = True
        while True:
            ok, n, msg = _do_lotw_fetch_once()   # shared with /api/sync/lotw
            if not ok:
                log.warning("LoTW: %s", msg)
                if "auth rejected" in msg:
                    return  # bad creds — stop the loop until restart (original behavior)
            elif n > 0:
                _sync_reload_after_fetch()        # surface new confirmations immediately
                log.info("LoTW: %s — worked-state refreshed", msg)
            elif first:
                log.info("LoTW: first tick — no new confirmations")
            first = False
            time.sleep(LOTW_FETCH_INTERVAL_SEC)

    # SDC / DX-cluster telnet feed: standard DX Spider node that re-broadcasts
    # GrayLine's filtered local spots (see add_spot). Started on the event loop
    # so broadcast_spot() writes from the same thread add_spot runs on.
    if TELNET_FEED_ENABLED:
        _telnet_feed = telnet_server.TelnetServer(
            host="0.0.0.0", port=TELNET_FEED_PORT, node_call=TELNET_FEED_NODE)
        await _telnet_feed.start()

    lotw_activity.start_background_refresh()   # loads cache instantly, refreshes if >7 days stale
    threading.Thread(target=serve_http, daemon=True).start()
    threading.Thread(target=purge_loop, daemon=True).start()
    threading.Thread(target=qrz_cache_reload_loop, daemon=True).start()
    threading.Thread(target=qrz_lookup_worker, daemon=True).start()
    threading.Thread(target=worked_state_reload_loop, daemon=True).start()
    threading.Thread(target=psk_heard_loop, daemon=True).start()   # PSKReporter "who heard me" eye
    # peer_copies: "who within PEER_RADIUS_MI also copied this?" — PSKReporter MQTT
    # firehose filtered to local receivers; the control group for "is it me or the band?"
    if HOME_LATLON:
        _peer_sq = _nearby_grid_squares(HOME_LATLON, HOME_GRID, PEER_RADIUS_MI)
        peer_copies.configure(HOME_LATLON, PEER_RADIUS_MI, CALLSIGN,
                              _peer_sq, maidenhead_to_latlon, haversine_miles)
        peer_copies.start()
        log.info("peer_copies: started — %.0fmi radius of %s, %d pre-filter squares",
                 PEER_RADIUS_MI, HOME_GRID, len(_peer_sq))
    threading.Thread(target=dxcc_rarity_refresh_loop, daemon=True).start()
    threading.Thread(target=clublog_matches_refresh_loop, daemon=True).start()
    if LOTW_FETCH_ENABLED:
        threading.Thread(target=lotw_fetch_loop, daemon=True).start()

    client = dxcluster.DXClusterClient(
        host=GOCLUSTER_HOST,
        port=GOCLUSTER_PORT,
        callsign=CALLSIGN,
        on_spot=on_spot,
        name="GOCLUSTER",
        login_commands=LOGIN_COMMANDS,
    )

    # Flex integration:
    # Phase 1 — connect, subscribe to slice updates, expose /active_bands
    # Phase 2 — band-filtered panadapter injection: any non-FT spot whose
    #   band has an active slice gets queued, then pushed to the radio at
    #   a rate limit (FLEX_INJECT_RATE_SEC) that keeps the API channel
    #   well clear of the audio-DPC saturation point.
    flex_task = None
    flex_inject_task = None
    global _flex_inject_queue
    if FLEX_ENABLED:
        _flex = flexradio.FlexRadioClient(host=FLEX_HOST, port=FLEX_PORT)
        _flex_inject_queue = asyncio.Queue(maxsize=1000)
        flex_task = asyncio.create_task(_flex.run())
        flex_inject_task = asyncio.create_task(flex_inject_worker())

    # WSJT-X UDP listener: ingests local FT8/FT4 decodes as WSJTX-LOCAL
    # spots (priority-ranked above external sources) and tracks per-client
    # dial state for click-to-tune audio-offset math.
    wsjtx_task = None
    if WSJTX_ENABLED:
        wsjtx_task = asyncio.create_task(wsjtx_listener_task())

    # N1MM / SDC-Connectors QSO-logged listener: marks stations worked in real
    # time when you log a contest QSO in N1MM or SDC (reuses the WSJT-X ingest
    # pipeline). Lets award pills flip live during a contest run.
    n1mm_task = None
    if N1MM_ENABLED:
        n1mm_task = asyncio.create_task(n1mm_listener_task())

    try:
        await client.connect()
    finally:
        for t in (flex_task, flex_inject_task, wsjtx_task, n1mm_task):
            if t and not t.done():
                t.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
