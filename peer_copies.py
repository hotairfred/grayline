"""
peer_copies — "who near me is also hearing this?" via the PSKReporter MQTT firehose.

Subscribes to PSKReporter's live MQTT stream and keeps only reception reports whose
RECEIVER is within ~radius miles of HOME_GRID, maintaining a rolling cache keyed by
the SENDER (DX) callsign:  {DXCALL: {PEER_CALL: {snr, band, ts}}}. The spot panel
then shows, per spot, how many local peers also copied that station and their signal
reports — a control group for "is it me or the band?": if your neighbors hear it and
you don't, the path exists from the area, so it's your station; if only one does,
it's his path. Radius-based, so anyone inside the circle auto-counts — no curated list.

Filtered firehose: a cheap grid-field-prefix pre-reject (the ~6 four-char squares
within radius+slop of home) drops ~all of the ~20k msgs/interval before the
haversine, so processing stays light. PSKReporter payload keys (compact JSON):
  sc=sender(DX) call, sl=sender locator, rc=receiver call, rl=receiver locator,
  rp=SNR report (may be absent), b=band, md=mode, t=timestamp, f=freq(Hz).
"""
from __future__ import annotations

import json
import logging
import threading
import time

log = logging.getLogger("grayline.peer_copies")

_BROKER, _PORT = "mqtt.pskreporter.info", 1883
_TOPIC = "pskr/filter/v2/+/+/#"     # full firehose; we filter by receiver locator
TTL_SEC = 15 * 60                   # a peer "heard it" report is fresh for 15 min

_cache: dict[str, dict] = {}        # DXCALL(upper) -> {PEER(upper): {snr, band, ts}}
_lock = threading.Lock()
_cfg = {"home": None, "radius": 60.0, "mycall": "",
        "squares": frozenset(), "latlon": None, "dist": None}
_started = False


def configure(home_latlon, radius_mi, my_call, nearby_squares, latlon_fn, dist_fn):
    """Inject Grayline's home position, radius, own call (to exclude), the cheap
    grid-square pre-filter set, and its maidenhead/haversine helpers."""
    _cfg.update(home=home_latlon, radius=float(radius_mi),
                mycall=(my_call or "").upper(),
                squares=frozenset(s.upper() for s in nearby_squares),
                latlon=latlon_fn, dist=dist_fn)


def copies(dx_call: str) -> list[dict]:
    """Local peers (within radius, excl. me) who copied dx_call in the last TTL,
    freshest first: [{'peer','snr','band','age'}]. Cheap — call at serve time."""
    if not dx_call:
        return []
    key = dx_call.strip().upper()
    now = time.time()
    out = []
    with _lock:
        peers = _cache.get(key)
        if not peers:
            return []
        for pc, d in peers.items():
            if now - d["ts"] > TTL_SEC:
                continue
            out.append({"peer": pc, "snr": d.get("snr"),
                        "band": d.get("band"), "age": int(now - d["ts"])})
    out.sort(key=lambda x: x["age"])
    return out


def all_heard(max_age_sec=None):
    """Every DX currently heard by a local peer, ONE entry per DX using the
    CLOSEST peer as the synthetic spotter — the input to peer-spot synthesis.
    Returns spot-ready dicts: {dx_call, spotter, spotter_grid, freq_khz, band,
    mode, dx_grid, snr, age}. Only receptions within max_age_sec (default TTL)
    count, so a DX drops out once every peer has stopped hearing it."""
    ttl = TTL_SEC if max_age_sec is None else max_age_sec
    now = time.time()
    out = []
    with _lock:
        for dx, peers in _cache.items():
            best = None
            for pc, d in peers.items():
                if now - d["ts"] > ttl:
                    continue
                if best is None or d.get("dist", 9e9) < best[1].get("dist", 9e9):
                    best = (pc, d)
            if best is None:
                continue
            pc, d = best
            f = d.get("freq")
            out.append({
                "dx_call": dx, "spotter": pc, "spotter_grid": d.get("peer_grid", ""),
                "freq_khz": round(f / 1000.0, 1) if f else None,
                "band": d.get("band", ""), "mode": d.get("mode", ""),
                "dx_grid": d.get("dx_grid", ""), "snr": d.get("snr"),
                "age": int(now - d["ts"]),
            })
    return out


def _on_message(client, userdata, msg):
    try:
        m = json.loads(msg.payload)
    except Exception:
        return
    rl = m.get("rl") or ""
    if rl[:4].upper() not in _cfg["squares"]:     # cheap pre-reject (~all of firehose)
        return
    rc = (m.get("rc") or "").strip().upper()
    sc = (m.get("sc") or "").strip().upper()
    if not rc or not sc or rc == _cfg["mycall"]:  # skip my own reports — I'm the control
        return
    c = _cfg["latlon"](rl)
    if not c:
        return
    pd = _cfg["dist"](_cfg["home"][0], _cfg["home"][1], c[0], c[1])
    if pd > _cfg["radius"]:
        return
    with _lock:
        # Keep enough to synthesize a full spot from a peer reception (peer-spots):
        # DX freq/grid/mode + the peer's grid and distance (to pick the closest peer
        # as the synthetic spotter). copies() still reads only snr/band/ts.
        _cache.setdefault(sc, {})[rc] = {
            "snr": m.get("rp"), "band": m.get("b") or "", "ts": time.time(),
            "freq": m.get("f"), "dx_grid": (m.get("sl") or "")[:6],
            "mode": (m.get("md") or "").upper(), "peer_grid": rl[:6], "dist": pd,
        }


def _purge_loop():
    while True:
        time.sleep(120)
        cut = time.time() - TTL_SEC
        with _lock:
            for sc in list(_cache):
                p = _cache[sc]
                for pc in list(p):
                    if p[pc]["ts"] < cut:
                        del p[pc]
                if not p:
                    del _cache[sc]


def start():
    if _started_already() or _cfg["home"] is None or not _cfg["squares"]:
        return
    # paho-mqtt is an OPTIONAL dependency — it powers only this feature (peer-copies
    # via the PSKReporter MQTT firehose). If it isn't installed, disable the feature
    # gracefully instead of crashing the whole server at startup.
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        log.warning("peer_copies: paho-mqtt not installed — peer-copies disabled. "
                    "Run 'pip install paho-mqtt' to enable it.")
        return

    def _run():
        while True:
            try:
                try:
                    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)   # paho 2.x
                except (AttributeError, TypeError):
                    cl = mqtt.Client()                                   # paho 1.x
                cl.on_message = _on_message
                # 5-arg (v2) signature with props defaulted so it also fits v1's 4-arg.
                cl.on_connect = lambda c, u, f, rc, props=None: c.subscribe(_TOPIC, qos=0)
                cl.connect(_BROKER, _PORT, keepalive=60)
                log.info("peer_copies: PSKReporter MQTT connected — radius %.0fmi, squares=%s",
                         _cfg["radius"], sorted(_cfg["squares"]))
                cl.loop_forever()
            except Exception as e:
                log.warning("peer_copies MQTT error (retry 30s): %s", e)
                time.sleep(30)

    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_purge_loop, daemon=True).start()


def _started_already():
    global _started
    if _started:
        return True
    _started = True
    return False
