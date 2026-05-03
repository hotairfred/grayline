#!/usr/bin/env python3
"""
Grayline Server v0 — read-only web view of GoCluster spots.

Phase 2 starter from project_grayline_dashboard.md. Stdlib only.
Runs on .101, serves HTML to any browser on the LAN. No UDP at the
workstation, no Flex panadapter inject, no audio-critical interference.
"""

import asyncio
import json
import logging
import math
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

import dxcluster
import flexradio
from ctydat import CtyDat
from worked_state import WorkedState, mode_class

log = logging.getLogger("grayline")

# ---------------- config ----------------
GOCLUSTER_HOST = "192.168.1.103"
GOCLUSTER_PORT = 8300
CALLSIGN = "WF8Z"
LOGIN_COMMANDS = [
    "SET GRID EM79sm",
    "PASS BAND ALL",       # clear any stale BAND blocks left over from prior sessions
    "PASS NEARBY OFF",
    "PASS CONFIDENCE V P S C",
    "PASS MODE FT8 FT4",
    "PASS SOURCE ALL",
    "PASS DECONT NA",      # only North American spotters reach us — server-side first stage
]

# Per-band award scopes. HF chases DXCC×band (ARRL Challenge / 5BDXCC). 6m chases
# BOTH DXCC×band AND grids (FFMA + 6m DXCC are both meaningful awards). 2m and up
# chase grids only (VUCC is grid-based; DXCC on VHF/UHF is real but extremely
# specialized, and most operators just want to know "is this a new grid").
DXCC_BANDS = {"160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"}
GRID_BANDS = {"6m", "2m", "1.25m", "70cm", "33cm", "23cm", "13cm", "9cm", "6cm", "3cm"}
HTTP_PORT = 8080
SPOT_TTL = 600        # seconds
MAX_SPOTS = 5000      # hard cap
PURGE_INTERVAL = 30   # seconds
REGION = 2            # ARRL band plan

HOME_GRID = "EM79sm"  # Cincinnati area (WF8Z QTH)
DEFAULT_RADIUS_MI = 300
QRZ_CACHE_PATH = Path("/home/fred/grayline/qrz_cache.json")
CTY_DAT_PATH = Path("/home/fred/grayline/cty.dat")
LOGBOOK_PATH = Path("/home/fred/grayline/qrz_logbook.json")
FLEX_HOST = "192.168.1.238"
FLEX_PORT = 4992
FLEX_ENABLED = True

# Phase 2 panadapter injection tuning
FLEX_INJECT_ENABLED = True
FLEX_INJECT_RATE_SEC = 0.5         # max 2 spots/sec to the radio (gentle on the API)
FLEX_INJECT_LIFETIME_SEC = 600     # how long Flex keeps each spot before auto-expiring it
FLEX_INJECT_DEDUP_SEC = 300        # don't re-inject the same (band, freq, call) within this window
FLEX_INJECT_SKIP_MODES = {"FT8", "FT4"}  # WSJT-X handles digital modes natively via DAX

# Loaded at startup
_cty: CtyDat | None = None
_worked: WorkedState | None = None
_flex: flexradio.FlexRadioClient | None = None
_flex_inject_queue: asyncio.Queue | None = None
_flex_recent_injects: dict[tuple, float] = {}

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


def qrz_lookup_worker():
    """Background worker: pops from queue, calls QRZ XML API, caches result."""
    import qrz as qrz_module  # use existing GTBridge QRZ client for auth + parse
    try:
        secrets = json.loads(Path("/home/fred/grayline/secrets.json").read_text())
        client = qrz_module.QRZLookup(
            username=secrets["qrz_user"],
            password=secrets["qrz_password"],
            cache_file=str(QRZ_CACHE_PATH),  # share cache with GTBridge
        )
    except Exception as e:
        log.warning("QRZ lookup worker init failed (no active lookups): %s", e)
        return

    while True:
        with _lookup_queue_lock:
            if _lookup_queue:
                callsign, _ = _lookup_queue.popitem(last=False)
            else:
                callsign = None
        if not callsign:
            time.sleep(5)
            continue
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

# ---------------- spot cache ----------------
_lock = threading.Lock()
_cache: "OrderedDict[tuple, dict]" = OrderedDict()


def add_spot(spot, _cluster_name):
    band = dxcluster.freq_to_band(spot.freq_khz)
    if not band:
        return
    # Drop spots from misconfigured-skimmer placeholder callsigns. N0CALL is the
    # convention-standard "I forgot to set my callsign" string. Anything containing
    # it is unverifiable — no real grid, no real location, no way to filter on distance.
    if "N0CALL" in (spot.spotter or "").upper():
        return
    mode = spot.mode or dxcluster.infer_mode(spot.freq_khz, REGION) or "UNK"
    key = (band, mode, round(spot.freq_khz, 1), spot.dx_call)

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
    distance_mi = spotter_distance_mi(spot.spotter)
    if distance_mi is None:
        return

    # cty.dat enrichment for the DX call (entity name doubles as the
    # bridge to your logbook's `country` field)
    country = continent = cq_zone = itu_zone = ""
    if _cty:
        e = _cty.lookup(spot.dx_call)
        if e:
            country = e.entity or ""
            continent = e.continent or ""
            cq_zone = str(e.cq_zone) if e.cq_zone is not None else ""
            itu_zone = str(e.itu_zone) if e.itu_zone is not None else ""

    # Worked/needed status (against your QRZ logbook)
    call_status = "new"
    dxcc_band_status = "new"
    dxcc_band_mode_status = "new"
    dxcc_band_modeclass_status = "new"
    modeclass = mode_class(mode) if mode else ""
    grid_band_status = "new"
    if _worked:
        call_status = _worked.call_status(spot.dx_call)
        if country:
            dxcc_band_status = _worked.country_band_status(country, band)
            if mode:
                dxcc_band_mode_status = _worked.country_band_mode_status(country, band, mode)
                dxcc_band_modeclass_status = _worked.country_band_modeclass_status(country, band, modeclass)
        if spot.grid:
            grid_band_status = _worked.grid_band_status(spot.grid, band)

    with _lock:
        _cache[key] = {
            "ts": time.time(),
            "band": band,
            "mode": mode,
            "freq_khz": spot.freq_khz,
            "dx_call": spot.dx_call,
            "spotter": spot.spotter or "",
            "snr": spot.snr,
            "grid": spot.grid or "",
            "distance_mi": distance_mi,
            "country": country,
            "continent": continent,
            "cq_zone": cq_zone,
            "itu_zone": itu_zone,
            "call_status": call_status,                     # 'new' | 'worked' | 'confirmed'
            "dxcc_band_status": dxcc_band_status,           # same enum, scoped to country+band (mixed-mode, ARRL Challenge / DXCC-Mixed)
            "dxcc_band_mode_status": dxcc_band_mode_status, # scoped to country+band+literal-mode (e.g. country+band+FT8)
            "dxcc_band_modeclass_status": dxcc_band_modeclass_status,  # scoped to country+band+ARRL-class (CW/Phone/Digital/Other) for DXCC variants
            "modeclass": modeclass,                         # CW | Phone | Digital | Other — derived from mode_class(mode)
            "grid_band_status": grid_band_status,           # scoped to grid×band (FFMA on 6m, VUCC on 2m+)
            "comment": spot.comment[:60] if spot.comment else "",
            "time_utc": spot.time_utc,
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


def purge_loop():
    while True:
        time.sleep(PURGE_INTERVAL)
        cutoff = time.time() - SPOT_TTL
        with _lock:
            stale = [k for k, v in _cache.items() if v["ts"] < cutoff]
            for k in stale:
                del _cache[k]


def snapshot():
    with _lock:
        rows = list(_cache.values())
    return rows


# ---------------- HTML ----------------
HTML_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Grayline — live spots</title>
<style>
:root { color-scheme: dark; }
body { font-family: -apple-system, system-ui, sans-serif; margin: 0.8em; background: #000; color: #eee; }
h1 { font-size: 1.1em; margin: 0 0 0.3em; color: #0f0; }
.status { color: #888; font-size: 0.85em; margin-bottom: 0.8em; }
.status .count { color: #ff0; font-weight: bold; }
.status .wanted { color: #ff5; font-weight: bold; }
.bands { display: flex; flex-wrap: wrap; gap: 1em; }
.band { min-width: 480px; flex: 1; }
.band h2 { font-size: 0.95em; margin: 0 0 0.2em; color: #ff0; border-bottom: 1px solid #444; padding-bottom: 2px; }
.mode-block { margin-bottom: 0.6em; }
.mode-hdr { font-size: 0.8em; color: #bcf; margin: 0.4em 0 0.1em; }
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
.band > details > summary { font-size: 0.95em; color: #ff0; border-bottom: 1px solid #444; padding-bottom: 2px; margin-bottom: 0.2em; font-weight: bold; }
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
  grid-template-columns: 4em repeat(5, minmax(5em, auto));
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
      <div class="actions">
        <button id="settings_all_bands">All bands</button>
        <button id="settings_no_bands">No bands</button>
        <button id="settings_all_modes">All modes</button>
        <button id="settings_no_modes">No modes</button>
        <button id="settings_reset_scopes">Reset scopes to defaults</button>
      </div>
    </div>
  </details>
</div>
<div class="status" id="status">Loading…</div>
<div class="controls">
  <label><input type="checkbox" id="show_wanted"> Show wanted only</label>
  <label style="margin-left:1em"><input type="checkbox" id="filter300"> Spotters within 300 mi of EM79sm only</label>
  <span class="legend">
    <span style="color:#f0f">callsign new</span> ·
    <span style="color:#ff5">worked</span> ·
    <span style="color:#5c5">confirmed</span> ·
    <span style="color:#000;background:#ffa500;padding:0 4px;border-radius:2px">scope needed</span>
  </span>
</div>
<div class="tab-strip" id="tab_strip"></div>
<div class="band-mode-toggles" id="band_mode_toggles"></div>
<div class="band-content" id="band_content"></div>
<script>
const BAND_ORDER = ["3cm","6cm","9cm","13cm","23cm","33cm","70cm","1.25m","2m","6m","10m","12m","15m","17m","20m","30m","40m","60m","80m","160m"];
// Per-band award scopes — drives which cells get the orange highlight treatment.
const DXCC_BANDS = new Set(["160m","80m","60m","40m","30m","20m","17m","15m","12m","10m","6m"]);
const GRID_BANDS = new Set(["6m","2m","1.25m","70cm","33cm","23cm","13cm","9cm","6cm","3cm"]);
const RADIUS_MI = 300;
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

// ---- Per-band award scope settings (replaces mode_aware + needed_only) ----
//
// Each band has a set of available scopes; each scope is independently toggleable.
// Defaults follow the ARRL-tracked-only principle: only ship scopes ARRL actually
// issues as awards, with sane defaults that match what most operators chase out
// of the box. Personal goals (DXCC-FT8-only, etc.) live in a future Custom
// Goals section — not in default settings.
//
// Available scopes per band:
//   HF (160m–10m):     DXCC-Mixed, DXCC-CW, DXCC-Phone, DXCC-Digital
//   6m:                DXCC-Mixed, DXCC-CW, DXCC-Phone, DXCC-Digital, VUCC
//   2m and above:      VUCC
//
// FFMA (6m CONUS-48 grids) is intentionally not yet exposed — needs the
// strict-grid-list constraint wired up first.
const ALL_DXCC_SCOPES = ["DXCC-Mixed", "DXCC-CW", "DXCC-Phone", "DXCC-Digital"];
const ALL_GRID_SCOPES = ["VUCC"];

function availableScopesForBand(band) {
  const scopes = [];
  if (DXCC_BANDS.has(band)) scopes.push(...ALL_DXCC_SCOPES);
  if (GRID_BANDS.has(band)) scopes.push(...ALL_GRID_SCOPES);
  return scopes;
}

function defaultScopesForBand(band) {
  const out = {};
  if (DXCC_BANDS.has(band)) out["DXCC-Mixed"] = true;
  if (GRID_BANDS.has(band)) out["VUCC"] = true;
  return out;
}

function loadAwardScopes() {
  try {
    const raw = localStorage.getItem("grayline_award_scopes");
    if (raw) return JSON.parse(raw);
  } catch (e) { /* fall through to defaults */ }
  // First load — populate per-band defaults
  const out = {};
  for (const b of BAND_ORDER) out[b] = defaultScopesForBand(b);
  return out;
}
function saveAwardScopes(scopes) {
  localStorage.setItem("grayline_award_scopes", JSON.stringify(scopes));
}
let awardScopes = loadAwardScopes();

function isScopeEnabled(band, scope) {
  return !!(awardScopes[band] && awardScopes[band][scope]);
}

function setScopeEnabled(band, scope, enabled) {
  if (!awardScopes[band]) awardScopes[band] = {};
  if (enabled) awardScopes[band][scope] = true;
  else delete awardScopes[band][scope];
  saveAwardScopes(awardScopes);
}

function resetAwardScopesToDefaults() {
  awardScopes = {};
  for (const b of BAND_ORDER) awardScopes[b] = defaultScopesForBand(b);
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
    case "DXCC-CW":
      return DXCC_BANDS.has(s.band) && s.country && s.modeclass === "CW"
        ? (s.dxcc_band_modeclass_status || "new") : null;
    case "DXCC-Phone":
      return DXCC_BANDS.has(s.band) && s.country && s.modeclass === "Phone"
        ? (s.dxcc_band_modeclass_status || "new") : null;
    case "DXCC-Digital":
      return DXCC_BANDS.has(s.band) && s.country && s.modeclass === "Digital"
        ? (s.dxcc_band_modeclass_status || "new") : null;
    case "VUCC":
      return GRID_BANDS.has(s.band) && s.grid ? (s.grid_band_status || "new") : null;
    default:
      return null;
  }
}

// Award pills for a spot. Returns only the scopes that (a) the user has
// enabled for this band AND (b) this spot can actually advance.
function scopeTags(s) {
  const out = [];
  for (const scope of availableScopesForBand(s.band)) {
    if (!isScopeEnabled(s.band, scope)) continue;
    const status = scopeStatus(s, scope);
    if (status === null) continue;
    out.push({ label: scope, status });
  }
  return out;
}

// True if any enabled scope for this spot is currently "new" (unworked).
// Drives the Show-wanted filter and the per-band wanted/total counter.
function anyScopeNeeded(s) {
  return scopeTags(s).some(t => t.status === "new");
}

// Status enum order — used for picking the "weakest" (most-needed) status.
const STATUS_ORDER = { new: 0, worked: 1, confirmed: 2 };

// Cell color status — weakest enabled scope status in the relevant family.
// Drives the orange treatment of the Country and Grid cells. Returns null
// if no scope from that family is enabled (cell stays plain).
function effectiveDxccStatus(s) {
  let weakest = null;
  for (const t of scopeTags(s)) {
    if (!t.label.startsWith("DXCC")) continue;
    if (weakest === null || STATUS_ORDER[t.status] < STATUS_ORDER[weakest]) {
      weakest = t.status;
    }
  }
  return weakest;
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

  // Modes: dynamic from current spots so we don't list ones that aren't here
  const modesSeen = new Set(spots.map(s => s.mode));
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
}

function renderScopeGrid() {
  const grid = document.getElementById("settings_scopes");
  if (!grid) return;
  const allScopes = [...ALL_DXCC_SCOPES, ...ALL_GRID_SCOPES];
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
  const filterOn = filterCB.checked;
  const showWanted = showWantedCB.checked;
  let filteredOut = 0;

  // Re-render settings panel with current modes-seen
  renderSettingsPanel(spots);

  // Apply filters in order: band/mode visibility, show-wanted (any enabled scope is new), 300mi
  spots = spots.filter(s => {
    if (disabledBands.has(s.band)) { filteredOut++; return false; }
    if (disabledModes.has(s.mode)) { filteredOut++; return false; }
    if (showWanted && !anyScopeNeeded(s)) { filteredOut++; return false; }
    if (filterOn) {
      if (s.distance_mi !== null && s.distance_mi !== undefined && s.distance_mi > RADIUS_MI) {
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

  // ---- Tab strip: one button per active band, with count ----
  let activeBand = getActiveBand();
  // If active band is no longer in the visible set, default to first available
  if (!bands.includes(activeBand) && bands.length) {
    activeBand = bands[0];
    setActiveBand(activeBand);
  }
  const tabStrip = document.getElementById("tab_strip");
  if (bands.length === 0) {
    tabStrip.innerHTML = '<span class="empty">No spots match current filters.</span>';
  } else {
    tabStrip.innerHTML = bands.map(b => {
      let total = 0, wanted = 0;
      for (const list of Object.values(byBand[b])) {
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
    tabStrip.querySelectorAll("button[data-band]").forEach(btn => {
      btn.addEventListener("click", () => {
        setActiveBand(btn.dataset.band);
        refresh();
      });
    });
  }

  // ---- Per-band mode toggles for the active band ----
  const modeTogglesBox = document.getElementById("band_mode_toggles");
  if (!activeBand || !byBand[activeBand]) {
    modeTogglesBox.innerHTML = '<span class="empty">Select a band to view spots.</span>';
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

  // ---- Render ONLY the active band's tables ----
  let html = "";
  if (activeBand && byBand[activeBand]) {
    const b = activeBand;
    const modes = Object.keys(byBand[b]).sort().filter(m => !isBandModeDisabled(b, m));
    if (modes.length === 0) {
      html = '<div class="empty">All modes for this band are toggled off.</div>';
    }
    for (const m of modes) {
      const rows = byBand[b][m].sort((x,y) => x.freq_khz - y.freq_khz);
      let bandHTML = `<div class="mode-block"><div class="mode-hdr">${escapeHTML(m)} · ${rows.length}</div>`;
      bandHTML += '<table><tr><th>Callsign</th><th>DXCC</th><th>Cont</th><th>Grid</th><th>Award</th><th>Freq</th><th>dB</th><th>Spotter</th><th>Spotter mi</th><th>Age</th></tr>';
      for (const s of rows) {
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
          if (s.distance_mi > RADIUS_MI) distClass += " far";
        }
        const callStatus = s.call_status || "new";
        // DXCC and Grid cell highlights are driven by the *enabled* award scopes
        // for this band — weakest (most-needed) status wins. Cell stays plain
        // when no scope from that family is enabled.
        const dxccEff = effectiveDxccStatus(s);
        const dxccCellClass = "country" + (dxccEff ? " " + dxccEff : "");
        const gridEff = effectiveGridStatus(s);
        const gridCellClass = "grid" + (gridEff ? " g" + gridEff : "");
        const isUs = (s.spotter || "").toUpperCase().startsWith("WF8Z");
        const rowClass = isUs ? "us-spotted" : "";
        const spotterClass = isUs ? "spotter us" : "spotter";
        const awardCell = scopeTags(s).map(t =>
          `<span class="pill ${t.status}">${escapeHTML(t.label)}</span>`
        ).join("");
        bandHTML += `<tr class="${rowClass}">
          <td class="dx ${callStatus}">${escapeHTML(s.dx_call)}</td>
          <td class="${dxccCellClass}">${escapeHTML(s.country || "")}</td>
          <td class="cont">${escapeHTML(s.continent || "")}</td>
          <td class="${gridCellClass}">${escapeHTML(s.grid)}</td>
          <td class="awards">${awardCell}</td>
          <td class="freq">${s.freq_khz.toFixed(1)}</td>
          <td class="${snrClass}">${snrCell}</td>
          <td class="${spotterClass}">${escapeHTML(s.spotter)}</td>
          <td class="${distClass}">${distCell}</td>
          <td class="age">${fmtAge(age)}</td>
        </tr>`;
      }
      bandHTML += "</table></div>";
      html += bandHTML;
    }
  }
  // Status counts: total / wanted (any enabled scope is new) / new+confirmed calls / spots we heard
  let wantedCount = 0, newCallCount = 0, confirmedCount = 0, usCount = 0;
  for (const s of spots) {
    if (anyScopeNeeded(s)) wantedCount++;
    if (s.call_status === "new") newCallCount++;
    if (s.call_status === "confirmed") confirmedCount++;
    if ((s.spotter || "").toUpperCase().startsWith("WF8Z")) usCount++;
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
</script>
</body></html>
"""


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet access log
        return

    def _send(self, body, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/spots.json":
            payload = {"spots": snapshot(), "now": time.time()}
            self._send(json.dumps(payload).encode(), "application/json")
        elif self.path == "/active_bands":
            payload = active_bands_snapshot()
            self._send(json.dumps(payload).encode(), "application/json")
        else:
            self._send(b"not found", "text/plain", 404)


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

    global _cty, _worked, _flex
    load_qrz_cache()  # initial load before any spots come in
    try:
        _cty = CtyDat(str(CTY_DAT_PATH))
        log.info("cty.dat loaded: %d entities, %d prefixes", _cty.entity_count, _cty.prefix_count)
    except Exception as e:
        log.warning("cty.dat load failed (DXCC enrichment disabled): %s", e)
    try:
        _worked = WorkedState(str(LOGBOOK_PATH))
    except Exception as e:
        log.warning("worked_state load failed (worked-status disabled): %s", e)

    def worked_state_reload_loop():
        while True:
            time.sleep(300)  # check every 5 min for fresher logbook
            if _worked:
                _worked.reload()

    threading.Thread(target=serve_http, daemon=True).start()
    threading.Thread(target=purge_loop, daemon=True).start()
    threading.Thread(target=qrz_cache_reload_loop, daemon=True).start()
    threading.Thread(target=qrz_lookup_worker, daemon=True).start()
    threading.Thread(target=worked_state_reload_loop, daemon=True).start()

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

    try:
        await client.connect()
    finally:
        for t in (flex_task, flex_inject_task):
            if t and not t.done():
                t.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
