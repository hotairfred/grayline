"""
CQ DX Marathon resolver for Grayline.

Maps a callsign to its CQ DX Marathon entity (the official "Combined ADIF Code")
and CQ zone, against the OFFICIAL DX Marathon list (346 entities, 40 zones) rather
than the ARRL DXCC list — because the audience that runs the Marathon competitively
would catch a DXCC-based count diverging from the official standings by ~6 entities.

Data: dxmarathon_entities.json / dxmarathon_zones.json, ingested verbatim from
dxmarathon.com/resources/countries (DX Marathon Entities/Zones v1.1.csv). Refresh
annually (the list is versioned).

Resolution uses Grayline's existing WAE-aware cty.dat (CtyDat.lookup(call, dxcc=False)),
which already distinguishes the prefix-detectable WAE separations (Sicily/IT9,
European Turkey/TA1, African Italy/IG9-IH9) and, via per-call overrides, the active
ops on Shetland/Bear Island. The only thing cty.dat folds away is 4U1VIC (→ Austria),
handled here as a one-prefix special case.

Both the live spot roster and the year-bounded worked-state use this one resolver, so
spot-needed and score always agree. The same entity layer also feeds a future WAE
award scope (worked-all-WAE-countries) — it's the WAE-aware entity that's shared.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("grayline.dxmarathon")
_BASE_DIR = Path(__file__).resolve().parent

# cty.dat entity-name -> Marathon Combined ADIF Code, for the WAE separations
# cty.dat can produce (names taken from cty.dat's dxcc=False output). Standard
# DXCC entities don't need this — their Combined ADIF Code IS the DXCC number.
_WAE_NAME_TO_ID = {
    "sicily": "904",
    "european turkey": "906",
    "african italy": "903",
    "shetland is.": "902",
    "shetland": "902",
    "bear i.": "905",
    "bear is.": "905",
    "bear island": "905",
}
# Prefixes cty.dat folds into a parent entity but the Marathon counts separately.
_PREFIX_SPECIAL = [("4U1V", "901")]   # UN Vienna Int'l Centre -> 901 (cty.dat says Austria)

_ENTITIES: dict[str, dict] = {}   # Combined ADIF Code -> entity record (the official 346)
_ZONES: dict[str, dict] = {}      # "1".."40" -> zone record


def load() -> None:
    global _ENTITIES, _ZONES
    try:
        _ENTITIES = json.loads((_BASE_DIR / "dxmarathon_entities.json").read_text(encoding="utf-8"))
        _ZONES = json.loads((_BASE_DIR / "dxmarathon_zones.json").read_text(encoding="utf-8"))
        log.info("DX Marathon list loaded: %d entities, %d zones", len(_ENTITIES), len(_ZONES))
    except Exception as e:
        log.warning("DX Marathon list load failed: %s", e)


def entity_count() -> int:
    return len(_ENTITIES)


def entity_id(call: str, cty) -> str | None:
    """Official Marathon entity id (Combined ADIF Code) for a callsign, or None.

    `cty` is a Grayline CtyDat instance. Uses dxcc=False for WAE-aware resolution.
    """
    if not call:
        return None
    cu = call.strip().upper()
    for pfx, mid in _PREFIX_SPECIAL:
        if cu.startswith(pfx):
            return mid if mid in _ENTITIES else None
    e = cty.lookup(call, dxcc=False)
    if e is None:
        return None
    wae = _WAE_NAME_TO_ID.get((e.entity or "").strip().lower())
    if wae and wae in _ENTITIES:
        return wae
    code = str(e.dxcc)            # standard entity: Combined ADIF Code == DXCC number
    return code if code in _ENTITIES else None


def zone(call: str, cty) -> str | None:
    """CQ zone (1..40 as string) for a callsign, WAE-aware (African Italy -> 33), or None."""
    if not call:
        return None
    e = cty.lookup(call, dxcc=False)
    if e is None or not e.cq_zone:
        return None
    z = str(e.cq_zone)
    return z if z in _ZONES else None


def entity_name(mid: str | None) -> str:
    rec = _ENTITIES.get(mid or "")
    return rec.get("name", "") if rec else ""
