"""
worked_state — fast in-memory worked/needed lookup against qrz_logbook.json.

Pattern lifted from GridTracker 2's GT.tracker.{worked,confirmed} dicts:
key-presence checks against pre-computed sets. O(1) lookups, no database.

Usage:
    ws = WorkedState("/home/fred/gtbridge/qrz_logbook.json")
    ws.is_worked("W3LPL")                 # bool — ever worked this call?
    ws.is_confirmed("W3LPL")              # bool — confirmed via LoTW or paper?
    ws.is_dxcc_worked(dxcc_id, "20m")     # bool — worked this DXCC on this band?
    ws.is_grid_worked("FN20", "6m")       # bool — worked this grid on this band?
    ws.summary()                          # dict — totals for status header

Confirmation source: a QSO is "confirmed" if `lotw_qsl_rcvd == "Y"` OR
`qsl_rcvd == "Y"` OR `eqsl_qsl_rcvd == "Y"`. The paper-QSL path means the
"stack of shame" (cards Fred has but hasn't uploaded to LoTW) still counts
as confirmed for filtering purposes.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger("worked_state")

_DATA_DIR = Path(__file__).parent / "data"

# Path to the Grayline local ADIF (written on every WSJT-X QSO Logged event).
# Merged into worked-state on reload() so freshly-logged QSOs survive the
# 5-min reload cycle even before QRZ logbook sync catches up.
_LOCAL_ADIF_PATH = Path(__file__).parent / "qso_logged.adi"

# Path to the LoTW confirmation ADIF (appended to by lotw_fetch.py on each
# incremental pull). LoTW returns confirmed-only records, and per GT2
# (adifWorker.js:131-143) the presence of APP_LOTW_RXQSL alone marks a
# record as LoTW-confirmed even if LOTW_QSL_RCVD isn't set.
_LOTW_ADIF_PATH = Path(__file__).parent / "lotw_qsl.adi"

# Country-name aliases: QRZ logbook uses legacy names (lumping European Russia
# under "Russia", etc.) while cty.dat uses ARRL's canonical DXCC entity names.
# When the two disagree on a label, the lookup at ingest time misses the
# historical QSO and a long-worked entity shows as "needed."
#
# We normalize QRZ's name to cty.dat's name during reload() so worked-state
# sets are keyed consistently with what cty.dat will hand us at spot ingest.
# Add entries here as discovered. DXCC IDs in comments are the ARRL canonical
# numbers (the `dxcc` field in QRZ records, also AD1C's country code).
QRZ_TO_CTY_COUNTRY = {
    "Russia": "European Russia",   # DXCC 54 — QRZ's legacy label
    # "South Korea": "Republic of Korea",   # DXCC 137 — uncomment if needed
    # "Czech Republic": "Czech Republic",   # DXCC 503 — both seem to agree, no alias
    # "Slovak Republic": "Slovak Republic", # DXCC 504 — likewise
    # Add more aliases as actual mismatches surface from operating.
}

# ADIF field-tag pattern: <FIELD:LEN[:type]>VALUE
_ADIF_FIELD_RE = re.compile(r"<([A-Za-z_][A-Za-z0-9_]*):(\d+)(?::[^>]*)?>", re.I)


def _parse_adif_qsos(path: Path) -> list[dict]:
    """Parse an ADIF file (qso_logged.adi or lotw_qsl.adi) and return QSOs
    in the same dict shape reload() expects from qrz_logbook.json.

    Records are split on <EOR> (case-insensitive). Header (everything
    before <EOH>) is skipped. Empty records are dropped.

    Confirmation fields (lotw_qsl_rcvd, qsl_rcvd, eqsl_qsl_rcvd) are
    populated when present so downstream _is_confirmed_record() resolves
    correctly. APP_LOTW_RXQSL presence is treated as LoTW-confirmed
    (GT2 adifWorker.js:131-143) even when LOTW_QSL_RCVD is omitted.
    """
    text = path.read_text()
    eoh = re.search(r"<EOH>", text, re.I)
    if eoh:
        text = text[eoh.end():]
    out = []
    for raw in re.split(r"<EOR>", text, flags=re.I):
        rec = {}
        for m in _ADIF_FIELD_RE.finditer(raw):
            field = m.group(1).upper()
            length = int(m.group(2))
            value_start = m.end()
            value = raw[value_start:value_start + length].strip()
            rec[field] = value
        if not rec.get("CALL"):
            continue
        lotw_confirmed = (
            rec.get("LOTW_QSL_RCVD", "").upper() in ("Y", "V")
            or bool(rec.get("APP_LOTW_RXQSL"))
        )
        out.append({
            "call": rec.get("CALL", ""),
            "band": rec.get("BAND", ""),
            "mode": rec.get("MODE", ""),
            "submode": rec.get("SUBMODE", ""),
            "qso_date": rec.get("QSO_DATE", ""),
            "time_on": rec.get("TIME_ON", ""),
            "freq": rec.get("FREQ", ""),
            "grid": rec.get("GRIDSQUARE", ""),
            "dxcc": rec.get("DXCC", ""),
            "country": rec.get("COUNTRY", ""),
            "state": rec.get("STATE", ""),
            "cqz": rec.get("CQZ", ""),
            "ituz": rec.get("ITUZ", ""),
            "prop_mode": rec.get("PROP_MODE", ""),
            "lotw_qsl_rcvd": "Y" if lotw_confirmed else "",
            "qsl_rcvd": rec.get("QSL_RCVD", ""),
            "eqsl_qsl_rcvd": rec.get("EQSL_QSL_RCVD", ""),
        })
    return out


# Back-compat alias — previously _parse_local_adif_qsos; kept in case anything
# external still references it.
_parse_local_adif_qsos = _parse_adif_qsos


def _load_mode_tables() -> tuple[dict, dict]:
    try:
        modes = json.loads((_DATA_DIR / "modes.json").read_text())
        modes_phone = json.loads((_DATA_DIR / "modes-phone.json").read_text())
    except Exception as e:
        log.warning("mode tables unavailable (%s); falling back to minimal classifier", e)
        modes, modes_phone = {}, {}
    return modes, modes_phone


_MODES, _MODES_PHONE = _load_mode_tables()

# WAS = the 50 US states. The three DXCC entities that carry US states:
# 291 (contiguous 48), 6 (Alaska), 110 (Hawaii). DC, PR, territories, and
# Canadian provinces are NOT WAS states.
_US_STATE_ENTITIES = frozenset({"291", "6", "110"})
_US_STATES_50 = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
})


def mode_class(mode: str) -> str:
    """Classify an ADIF mode into one of: CW, Phone, Digital, Other.

    Mirrors GT2's getTypeFromMode() so a Mixed/CW/Phone/Digital/Other
    breakdown of our logbook agrees with what GT would compute.
    """
    if not mode:
        return "Other"
    m = mode.strip().upper()
    if m == "CW":
        return "CW"
    if _MODES.get(m) is True:
        return "Digital"
    if _MODES_PHONE.get(m) is True:
        return "Phone"
    return "Other"


_WPX_DIGIT_RE = re.compile(r"\d")


def wpx_prefix(callsign: str) -> str | None:
    """WPX prefix per CQ-WPX rules — port of GT2 getWpx() in gtCommon.js.

    Returns None for callsigns containing '/' (GT defers these too) or for
    callsigns with no digit. Otherwise: scan from index 1, find the first
    digit, extend through consecutive digits, return prefix up to and
    including that digit run.

        K5XYZ   -> K5
        WB6ABC  -> WB6
        8N0AAB  -> 8N0
        Q70G    -> Q70
        LU9DCE  -> LU9
    """
    if not callsign or "/" in callsign:
        return None
    if not _WPX_DIGIT_RE.search(callsign):
        return None
    end = len(callsign)
    prefix_end = 1
    found = False
    while prefix_end != end:
        if callsign[prefix_end].isdigit():
            while prefix_end + 1 != end and callsign[prefix_end + 1].isdigit():
                prefix_end += 1
            found = True
            break
        prefix_end += 1
    if not found:
        return None
    return callsign[: prefix_end + 1]


# Country-name → continent map, populated lazily from cty.dat. cty.dat
# doesn't carry ADIF DXCC IDs, so we key by entity name and resolve via
# the QSO's `country` field (with the same cty/QRZ alias dance the rest
# of worked_state already does).
_COUNTRY_TO_CONTINENT: dict[str, str] | None = None


def _load_country_to_continent() -> dict[str, str]:
    global _COUNTRY_TO_CONTINENT
    if _COUNTRY_TO_CONTINENT is not None:
        return _COUNTRY_TO_CONTINENT
    out: dict[str, str] = {}
    try:
        from ctydat import CtyDat
        cty = CtyDat(str(Path(__file__).parent / "cty.dat"))
        for entity in cty._entities.values():
            if entity.entity and entity.continent:
                out[entity.entity] = entity.continent
                # Also register the QRZ-aliased name so we can look up
                # either form
                alias = CTY_TO_LOGBOOK_NAME.get(entity.entity)
                if alias:
                    out[alias] = entity.continent
    except Exception as e:
        log.warning("country->continent map unavailable (%s); WAC axis will be empty", e)
    _COUNTRY_TO_CONTINENT = out
    return out


# Hand-curated cty.dat-name → QRZ-logbook-country-name aliases.
# These are the common cases where the two databases use different names
# for the same DXCC entity. Without this, e.g. "Fed. Rep. of Germany" from
# cty.dat fails to match any of Fred's "Germany" QSOs and Germany shows
# as needed-on-every-band even after dozens of QSOs.
CTY_TO_LOGBOOK_NAME = {
    "Fed. Rep. of Germany": "Germany",
    "Republic of Korea": "South Korea",
    "DPR of Korea": "North Korea",
    "Reunion Island": "Reunion",
    "Bouvet": "Bouvet Island",
    "Antigua & Barbuda": "Antigua and Barbuda",
    "Ceuta & Melilla": "Ceuta and Melilla",
    "Madeira Islands": "Madeira Island",
    "Saba & St. Eustatius": "Saba, St Eustatius",
    "Pr. Edward & Marion Is.": "Prince Edward and Marion Island",
    "Kingdom of Eswatini": "Eswatini",
    "Republic of South Sudan": "South Sudan",
    "Pitcairn Island": "Pitcairn Islands",
    "Andaman & Nicobar Is.": "Andaman and Nicobar Islands",
    "Cocos (Keeling) Islands": "Cocos Keeling Islands",
    "North Macedonia": "North Macedonia (Republic of)",
    "Trinidad & Tobago": "Trinidad and Tobago",
    "Sao Tome & Principe": "Sao Tome and Principe",
    "St. Kitts & Nevis": "St Kitts and Nevis",
    "St. Pierre & Miquelon": "St Pierre and Miquelon",
    "St. Vincent": "St Vincent",
    "St. Lucia": "Saint Lucia",
    "St. Helena": "Saint Helena",
    "Turks & Caicos Is.": "Turks and Caicos Islands",
    "Falkland Is.": "Falkland Islands",
    "Solomon Islands": "Solomon Is.",
    "Cayman Islands": "Cayman Is.",
    "British Virgin Is.": "British Virgin Islands",
    "U.S. Virgin Is.": "Virgin Islands",
    "Marshall Islands": "Marshall Is.",
    "Cook Is.": "Cook Islands",
    "Faroe Islands": "Faroe Is.",
    "Canary Is.": "Canary Islands",
    "Balearic Is.": "Balearic Islands",
    "Galapagos Is.": "Galapagos Islands",
    "Easter Island": "Easter Is.",
    "Mariana Is.": "Mariana Islands",
}


def _qrz_country(cty_name: str) -> str:
    """Translate a cty.dat entity name to the equivalent QRZ logbook country name.
    Identity if no alias is registered."""
    return CTY_TO_LOGBOOK_NAME.get(cty_name, cty_name)


# Resolve a callsign to its canonical cty.dat entity name — the SAME name spot
# ingest uses. Keying worked-state by this collapses the per-source country-label
# variants ("Bosnia and Herzegovina" from QRZ, "BOSNIA-HERZEGOVINA" from LoTW,
# "Bosnia-Herzegovina" from cty.dat) that otherwise scatter a worked entity across
# three keys and produce false "needed" flags on bands the spot lookup can't find.
_CTY_FOR_NAMES = None
def _cty_entity(call: str) -> str:
    global _CTY_FOR_NAMES
    if _CTY_FOR_NAMES is None:
        try:
            from ctydat import CtyDat
            _CTY_FOR_NAMES = CtyDat(str(Path(__file__).parent / "cty.dat"))
        except Exception as e:
            log.warning("cty.dat unavailable for country canonicalization: %s", e)
            _CTY_FOR_NAMES = False
    if not _CTY_FOR_NAMES or not call:
        return ""
    try:
        e = _CTY_FOR_NAMES.lookup(call)
        return (e.entity or "") if e else ""
    except Exception:
        return ""


def _norm_band(s: str) -> str:
    return (s or "").strip().lower()


def _norm_call(s: str) -> str:
    return (s or "").strip().upper()


def _norm_grid4(s: str) -> str:
    return (s or "").strip().upper()[:4]


def _is_confirmed_record(rec: dict) -> bool:
    return (
        rec.get("lotw_qsl_rcvd", "").upper() == "Y"
        or rec.get("qsl_rcvd", "").upper() == "Y"
        or rec.get("eqsl_qsl_rcvd", "").upper() == "Y"
    )


class WorkedState:
    def __init__(self, logbook_path: str | Path):
        self.logbook_path = Path(logbook_path)
        self._mtime: float | None = None

        # Sets keyed for fast `in` checks (mirrors GT.tracker shape):
        self.worked_calls: set[str] = set()
        self.confirmed_calls: set[str] = set()

        # Indexed by ADIF DXCC ID (the number QRZ uses)
        self.worked_dxcc_band: set[tuple[str, str]] = set()  # (dxcc_id, band)
        self.confirmed_dxcc_band: set[tuple[str, str]] = set()

        # Indexed by country/entity name (matches cty.dat's `entity` field)
        self.worked_country_band: set[tuple[str, str]] = set()  # (country, band)
        self.confirmed_country_band: set[tuple[str, str]] = set()

        # Per-band-per-mode (for mode-specific awards like DXCC-CW, DXCC-FT8, etc.)
        self.worked_country_band_mode: set[tuple[str, str, str]] = set()  # (country, band, mode)
        self.confirmed_country_band_mode: set[tuple[str, str, str]] = set()

        # Per-band-per-modeclass (CW / Phone / Digital / Other) — for ARRL DXCC
        # variants. Mixed is NOT stored here; it's equivalent to country_band_status.
        # ARRL recognizes DXCC-Mixed, DXCC-CW, DXCC-Phone, DXCC-Digital as the four
        # tracked variants; this set powers the per-band scope highlighting.
        self.worked_country_band_modeclass: set[tuple[str, str, str]] = set()  # (country, band, class)
        self.confirmed_country_band_modeclass: set[tuple[str, str, str]] = set()
        # Entity-level (any band) modeclass — the ACTUAL grain of the ARRL mode
        # DXCCs (CW/Phone/Digital are entity-count, NOT band-split).
        self.worked_country_modeclass: set[tuple[str, str]] = set()  # (country, class)
        self.confirmed_country_modeclass: set[tuple[str, str]] = set()

        # DXCC-ID-keyed mode-class sets — authoritative for award counts.
        # The country-name version above can over-count when QRZ and cty.dat
        # disagree on labels (e.g. "Russia" vs "European Russia").
        self.worked_dxcc_modeclass: set[tuple[str, str]] = set()  # (dxcc_id, class)
        self.confirmed_dxcc_modeclass: set[tuple[str, str]] = set()

        self.worked_grid_band: set[tuple[str, str]] = set()  # (grid4, band)
        self.confirmed_grid_band: set[tuple[str, str]] = set()

        self.worked_dxcc: set[str] = set()  # DXCC ID ever, any band/mode
        self.confirmed_dxcc: set[str] = set()
        self.worked_countries: set[str] = set()
        self.confirmed_countries: set[str] = set()

        # WAS — US states only. Two-letter postal codes ("OH", "CA").
        self.worked_states: set[str] = set()
        self.confirmed_states: set[str] = set()
        # Per-band-per-state for WAS-by-band (Five-Band WAS / 5BWAS).
        self.worked_state_band: set[tuple[str, str]] = set()
        self.confirmed_state_band: set[tuple[str, str]] = set()

        # WAZ — CQ zones 1-40.
        self.worked_cq_zones: set[str] = set()
        self.confirmed_cq_zones: set[str] = set()

        # Satellite VUCC — distinct grid4s with PROP_MODE=SAT.
        self.worked_satellite_grids: set[str] = set()
        self.confirmed_satellite_grids: set[str] = set()
        # Satellite DXCC — distinct DXCC IDs with PROP_MODE=SAT.
        self.worked_dxcc_satellite: set[str] = set()
        self.confirmed_dxcc_satellite: set[str] = set()

        # Full deduplicated record list (merged QRZ + local + LoTW) — kept for
        # the /api/log/search endpoint. Each record is the same dict-shape as
        # _parse_adif_qsos output. Dedup key is GT2's hash:
        # (call, band, mode, time_on rounded to nearest minute).
        self.qsos: list[dict] = []

        self.qso_count = 0
        self.unique_calls_count = 0
        self.confirmed_qso_count = 0

        self.reload()

    def force_reload(self) -> bool:
        """Reload unconditionally, bypassing the mtime gate. Used after an
        in-process ADIF edit (N1MM contactdelete/contactreplace) where the file
        can change within the same mtime tick as the previous reload, which the
        gate would otherwise skip."""
        self._mtime = None
        return self.reload()

    def reload(self) -> bool:
        """(Re)load worked-state from BOTH the QRZ logbook JSON (long-tail
        archive, lags by hours-to-days) AND the local Grayline ADIF (written
        on every WSJT-X QSO Logged event, real-time).

        Merging both sources ensures freshly-logged QSOs survive this reload
        cycle even before QRZ logbook sync has fetched them — otherwise the
        in-memory record_qso() additions get clobbered every 5 min.

        Returns True if anything was reloaded."""
        # mtime check on all three sources. We always re-merge if any source
        # changed, so consider all file mtimes.
        qrz_exists = self.logbook_path.exists()
        local_exists = _LOCAL_ADIF_PATH.exists()
        lotw_exists = _LOTW_ADIF_PATH.exists()
        if not qrz_exists and not local_exists and not lotw_exists:
            log.warning("no logbook source available (QRZ %s, local %s, LoTW %s) — running with empty worked state",
                        self.logbook_path, _LOCAL_ADIF_PATH, _LOTW_ADIF_PATH)
            return False
        qrz_mtime = self.logbook_path.stat().st_mtime if qrz_exists else 0
        local_mtime = _LOCAL_ADIF_PATH.stat().st_mtime if local_exists else 0
        lotw_mtime = _LOTW_ADIF_PATH.stat().st_mtime if lotw_exists else 0
        composite_mtime = max(qrz_mtime, local_mtime, lotw_mtime)
        if self._mtime is not None and composite_mtime == self._mtime:
            return False
        try:
            data = json.loads(self.logbook_path.read_text()) if qrz_exists else {"qsos": []}
        except Exception as e:
            log.warning("failed to parse logbook %s: %s", self.logbook_path, e)
            return False
        # mtime tracked as the composite so future reload cycles only fire on real change
        mtime = composite_mtime

        worked_calls: set[str] = set()
        confirmed_calls: set[str] = set()
        worked_dxcc_band: set[tuple[str, str]] = set()
        confirmed_dxcc_band: set[tuple[str, str]] = set()
        worked_country_band: set[tuple[str, str]] = set()
        confirmed_country_band: set[tuple[str, str]] = set()
        worked_country_band_mode: set[tuple[str, str, str]] = set()
        confirmed_country_band_mode: set[tuple[str, str, str]] = set()
        worked_country_band_modeclass: set[tuple[str, str, str]] = set()
        confirmed_country_band_modeclass: set[tuple[str, str, str]] = set()
        worked_dxcc_modeclass: set[tuple[str, str]] = set()
        confirmed_dxcc_modeclass: set[tuple[str, str]] = set()
        worked_grid_band: set[tuple[str, str]] = set()
        confirmed_grid_band: set[tuple[str, str]] = set()
        worked_dxcc: set[str] = set()
        confirmed_dxcc: set[str] = set()
        worked_countries: set[str] = set()
        confirmed_countries: set[str] = set()
        worked_states: set[str] = set()
        confirmed_states: set[str] = set()
        worked_state_band: set[tuple[str, str]] = set()
        confirmed_state_band: set[tuple[str, str]] = set()
        worked_cq_zones: set[str] = set()
        confirmed_cq_zones: set[str] = set()
        worked_satellite_grids: set[str] = set()
        confirmed_satellite_grids: set[str] = set()
        worked_dxcc_satellite: set[str] = set()
        confirmed_dxcc_satellite: set[str] = set()
        confirmed_qso_count = 0
        # Dedup merged QSOs by GT2-style hash so the same QSO appearing in QRZ
        # + LoTW (typical) collapses to one record. Confirmation flags are OR'd
        # across duplicates so the surviving record has the strongest evidence.
        qsos_dedup: dict[str, dict] = {}

        qsos = list(data.get("qsos", []))

        # Merge in QSOs from the local Grayline ADIF (qso_logged.adi). These
        # are real-time WSJT-X-logged QSOs that haven't necessarily synced
        # to QRZ yet, so they wouldn't be in the JSON. set semantics in the
        # worked-* fields handle dedup if QRZ later syncs the same QSO.
        if local_exists:
            try:
                local_qsos = _parse_adif_qsos(_LOCAL_ADIF_PATH)
                qsos.extend(local_qsos)
                if local_qsos:
                    log.info("merged %d QSO(s) from local ADIF %s into worked-state",
                             len(local_qsos), _LOCAL_ADIF_PATH.name)
            except Exception as e:
                log.warning("failed to parse local ADIF %s: %s", _LOCAL_ADIF_PATH, e)

        # Merge in confirmations from the LoTW ADIF (lotw_qsl.adi). lotw_fetch.py
        # downloads incrementally; every record here is LoTW-confirmed by
        # construction. Same dedup-by-set semantics — a QRZ record covering the
        # same QSO will already be in the worked-* sets, but the confirmed-*
        # sets gain LoTW's authoritative state.
        if lotw_exists:
            try:
                lotw_qsos = _parse_adif_qsos(_LOTW_ADIF_PATH)
                qsos.extend(lotw_qsos)
                if lotw_qsos:
                    log.info("merged %d LoTW confirmation(s) from %s into worked-state",
                             len(lotw_qsos), _LOTW_ADIF_PATH.name)
            except Exception as e:
                log.warning("failed to parse LoTW ADIF %s: %s", _LOTW_ADIF_PATH, e)

        for q in qsos:
            call = _norm_call(q.get("call", ""))
            band = _norm_band(q.get("band", ""))
            mode = (q.get("mode") or "").strip().upper()
            grid4 = _norm_grid4(q.get("grid", ""))
            dxcc = (q.get("dxcc") or "").strip()
            country = (q.get("country") or "").strip()
            # Normalize QRZ's legacy country labels to match cty.dat's canonical
            # entity names — otherwise the lookup at spot ingest (which uses
            # cty.dat) misses historical QSOs labeled with the older string.
            country = QRZ_TO_CTY_COUNTRY.get(country, country)
            # Authoritative: resolve the canonical entity from the callsign via
            # cty.dat (the exact name spots are keyed by). This collapses the
            # QRZ/LoTW/local label variants of the same entity onto one key, so
            # band-slot status is computed against the operator's full history
            # rather than whichever spelling happened to match. Falls back to the
            # COUNTRY-field value above if cty.dat can't resolve the call.
            canon = _cty_entity(q.get("call", ""))
            if canon:
                country = canon
            state = (q.get("state") or "").strip().upper()
            cqz = (q.get("cqz") or "").strip()
            # Normalize CQ zone: drop leading zeros, keep "0" if all-zero.
            if cqz:
                try:
                    cqz_n = int(cqz)
                    cqz = str(cqz_n) if 1 <= cqz_n <= 40 else ""
                except ValueError:
                    cqz = ""
            confirmed = _is_confirmed_record(q)
            if confirmed:
                confirmed_qso_count += 1

            # GT2-style dedup hash: same QSO from multiple sources collapses.
            # Time bucket is the minute-grain of time_on; matches GT2 unique().
            time_min = (q.get("time_on", "") or "")[:4]
            dedup_key = f"{call}|{band}|{mode}|{q.get('qso_date','')}{time_min}"
            existing = qsos_dedup.get(dedup_key)
            if existing is None:
                qsos_dedup[dedup_key] = dict(q)
                qsos_dedup[dedup_key]["country"] = country
            else:
                # OR confirmation flags — keep the strongest evidence.
                for flag in ("lotw_qsl_rcvd", "qsl_rcvd", "eqsl_qsl_rcvd"):
                    cur = (existing.get(flag) or "").upper()
                    new = (q.get(flag) or "").upper()
                    if new in ("Y", "V") and cur not in ("Y", "V"):
                        existing[flag] = q[flag]
                # Backfill missing fields. prop_mode is in the backfill set so
                # a satellite QSO logged via QRZ first (no prop_mode) gets
                # tagged correctly when the matching LoTW record arrives later.
                for k in ("grid", "dxcc", "country", "state", "cqz", "ituz", "freq", "prop_mode"):
                    if not existing.get(k) and q.get(k):
                        existing[k] = q[k]

            if call:
                worked_calls.add(call)
                if confirmed:
                    confirmed_calls.add(call)

            if dxcc:
                worked_dxcc.add(dxcc)
                if confirmed:
                    confirmed_dxcc.add(dxcc)
                if band:
                    worked_dxcc_band.add((dxcc, band))
                    if confirmed:
                        confirmed_dxcc_band.add((dxcc, band))
                if mode:
                    cls = mode_class(mode)
                    worked_dxcc_modeclass.add((dxcc, cls))
                    if confirmed:
                        confirmed_dxcc_modeclass.add((dxcc, cls))

            if country:
                worked_countries.add(country)
                if confirmed:
                    confirmed_countries.add(country)
                if band:
                    worked_country_band.add((country, band))
                    if confirmed:
                        confirmed_country_band.add((country, band))
                if band and mode:
                    worked_country_band_mode.add((country, band, mode))
                    if confirmed:
                        confirmed_country_band_mode.add((country, band, mode))
                if band and mode:
                    cls = mode_class(mode)
                    worked_country_band_modeclass.add((country, band, cls))
                    if confirmed:
                        confirmed_country_band_modeclass.add((country, band, cls))

            if grid4 and band:
                worked_grid_band.add((grid4, band))
                if confirmed:
                    confirmed_grid_band.add((grid4, band))

            # WAS — the 50 US states. Alaska (DXCC 6) and Hawaii (DXCC 110) are
            # SEPARATE DXCC entities but valid WAS states, so the gate must allow
            # all three US-state entities — NOT just 291 (which silently dropped
            # HI entirely and AK except where mis-tagged). Require a real 50-state
            # USPS code so DC / territories / Canadian "states" don't sneak in.
            if state in _US_STATES_50 and dxcc in _US_STATE_ENTITIES:
                worked_states.add(state)
                if confirmed:
                    confirmed_states.add(state)
                if band:
                    worked_state_band.add((state, band))
                    if confirmed:
                        confirmed_state_band.add((state, band))

            # WAZ — any QSO contributes its CQ zone.
            if cqz:
                worked_cq_zones.add(cqz)
                if confirmed:
                    confirmed_cq_zones.add(cqz)

            # Satellite VUCC + Satellite DXCC — PROP_MODE=SAT.
            prop_mode = (q.get("prop_mode") or "").strip().upper()
            if prop_mode == "SAT":
                if grid4:
                    worked_satellite_grids.add(grid4)
                    if confirmed:
                        confirmed_satellite_grids.add(grid4)
                if dxcc:
                    worked_dxcc_satellite.add(dxcc)
                    if confirmed:
                        confirmed_dxcc_satellite.add(dxcc)

        self.worked_calls = worked_calls
        self.confirmed_calls = confirmed_calls
        self.worked_dxcc_band = worked_dxcc_band
        self.confirmed_dxcc_band = confirmed_dxcc_band
        self.worked_country_band = worked_country_band
        self.confirmed_country_band = confirmed_country_band
        self.worked_country_band_mode = worked_country_band_mode
        self.confirmed_country_band_mode = confirmed_country_band_mode
        self.worked_country_band_modeclass = worked_country_band_modeclass
        self.confirmed_country_band_modeclass = confirmed_country_band_modeclass
        # Collapse band out → entity-level modeclass (mode DXCC award grain).
        self.worked_country_modeclass = {(co, cl) for (co, _b, cl) in worked_country_band_modeclass}
        self.confirmed_country_modeclass = {(co, cl) for (co, _b, cl) in confirmed_country_band_modeclass}
        self.worked_dxcc_modeclass = worked_dxcc_modeclass
        self.confirmed_dxcc_modeclass = confirmed_dxcc_modeclass
        self.worked_grid_band = worked_grid_band
        self.confirmed_grid_band = confirmed_grid_band
        self.worked_dxcc = worked_dxcc
        self.confirmed_dxcc = confirmed_dxcc
        self.worked_countries = worked_countries
        self.confirmed_countries = confirmed_countries
        self.worked_states = worked_states
        self.confirmed_states = confirmed_states
        self.worked_state_band = worked_state_band
        self.confirmed_state_band = confirmed_state_band
        self.worked_cq_zones = worked_cq_zones
        self.confirmed_cq_zones = confirmed_cq_zones
        self.worked_satellite_grids = worked_satellite_grids
        self.confirmed_satellite_grids = confirmed_satellite_grids
        self.worked_dxcc_satellite = worked_dxcc_satellite
        self.confirmed_dxcc_satellite = confirmed_dxcc_satellite
        self.qsos = list(qsos_dedup.values())
        self.qso_count = len(self.qsos)
        self.unique_calls_count = len(worked_calls)
        self.confirmed_qso_count = confirmed_qso_count
        self._mtime = mtime

        log.info("worked_state loaded: %d QSOs, %d unique calls, %d confirmed, "
                 "%d states, %d CQ zones",
                 self.qso_count, self.unique_calls_count, self.confirmed_qso_count,
                 len(self.confirmed_states), len(self.confirmed_cq_zones))
        return True

    # -------- in-memory QSO injection (real-time, post-load) --------

    def record_qso(self, call: str, country: str, band: str, mode: str,
                   grid: str = "", dxcc: str = "", confirmed: bool = False) -> None:
        """Push a freshly-logged QSO into the worked-state sets without an ADIF reload.

        Used by the WSJT-X QSO Logged handler so the operator sees pills flip
        from 'new' to 'worked' immediately, not after the QRZ → ADIF → reload
        roundtrip (minutes-to-hours lag). All inputs are normalized internally;
        empty fields are tolerated (the corresponding scope just isn't updated).

        Idempotent — re-calling with the same QSO is a no-op.
        """
        c = _norm_call(call)
        if c:
            self.worked_calls.add(c)
            if confirmed:
                self.confirmed_calls.add(c)
        b = _norm_band(band) if band else ""
        m = (mode or "").upper().strip()
        cls = mode_class(m) if m else ""
        d = (dxcc or "").strip()
        if d:
            self.worked_dxcc.add(d)
            if confirmed:
                self.confirmed_dxcc.add(d)
            if b:
                self.worked_dxcc_band.add((d, b))
                if confirmed:
                    self.confirmed_dxcc_band.add((d, b))
        co = (country or "").strip()
        if co:
            self.worked_countries.add(co)
            if confirmed:
                self.confirmed_countries.add(co)
            if b:
                self.worked_country_band.add((co, b))
                if confirmed:
                    self.confirmed_country_band.add((co, b))
            if b and m:
                self.worked_country_band_mode.add((co, b, m))
                if confirmed:
                    self.confirmed_country_band_mode.add((co, b, m))
            if b and cls:
                self.worked_country_band_modeclass.add((co, b, cls))
                if confirmed:
                    self.confirmed_country_band_modeclass.add((co, b, cls))
            if cls:   # entity-level (any band) — mode DXCC award grain
                self.worked_country_modeclass.add((co, cls))
                if confirmed:
                    self.confirmed_country_modeclass.add((co, cls))
        g4 = _norm_grid4(grid) if grid else ""
        if g4 and b:
            self.worked_grid_band.add((g4, b))
            if confirmed:
                self.confirmed_grid_band.add((g4, b))
        # Maintain qso_count & unique_calls_count approximately — not exact unless
        # we deduplicate on (call, band, mode), but useful for UI counters.
        if c and c not in self.worked_calls:
            self.unique_calls_count = len(self.worked_calls)
        self.qso_count += 1

    # -------- per-spot lookups (each is O(1)) --------

    def call_status(self, call: str) -> str:
        """Return one of: 'new' (never worked), 'worked' (worked but not confirmed),
        'confirmed' (worked and confirmed via LoTW or paper QSL)."""
        c = _norm_call(call)
        if not c:
            return "new"
        if c in self.confirmed_calls:
            return "confirmed"
        if c in self.worked_calls:
            return "worked"
        return "new"

    def dxcc_band_status(self, dxcc: str, band: str) -> str:
        """One of: 'new', 'worked', 'confirmed'. Empty dxcc/band returns 'new'."""
        if not dxcc or not band:
            return "new"
        key = (dxcc, _norm_band(band))
        if key in self.confirmed_dxcc_band:
            return "confirmed"
        if key in self.worked_dxcc_band:
            return "worked"
        return "new"

    def country_band_status(self, country: str, band: str) -> str:
        """Lookup by country/entity name (e.g. 'United States', 'Israel').
        Translates cty.dat names to QRZ logbook names where they differ
        (e.g. 'Fed. Rep. of Germany' -> 'Germany') before lookup."""
        if not country or not band:
            return "new"
        b = _norm_band(band)
        # Try cty.dat name first, then translated QRZ name
        for name in (country, _qrz_country(country)):
            key = (name, b)
            if key in self.confirmed_country_band:
                return "confirmed"
            if key in self.worked_country_band:
                return "worked"
        return "new"

    def country_band_mode_status(self, country: str, band: str, mode: str) -> str:
        """Per-band-per-mode status — for mode-specific awards (DXCC-CW, DXCC-FT8, etc.)."""
        if not country or not band or not mode:
            return "new"
        b = _norm_band(band)
        m = mode.strip().upper()
        for name in (country, _qrz_country(country)):
            key = (name, b, m)
            if key in self.confirmed_country_band_mode:
                return "confirmed"
            if key in self.worked_country_band_mode:
                return "worked"
        return "new"

    def country_band_modeclass_status(self, country: str, band: str, modeclass: str) -> str:
        """Per-band-per-modeclass status for ARRL DXCC variants.

        modeclass ∈ {"Mixed", "CW", "Phone", "Digital", "Other"}.

        - "Mixed" routes to country_band_status (any mode counts — DXCC-Mixed)
        - "CW" / "Phone" / "Digital" / "Other" check the modeclass set built
          from mode_class(mode) at load time

        Returns "new" / "worked" / "confirmed".
        """
        if not country or not band or not modeclass:
            return "new"
        if modeclass == "Mixed":
            return self.country_band_status(country, band)
        b = _norm_band(band)
        c = modeclass.strip()
        for name in (country, _qrz_country(country)):
            key = (name, b, c)
            if key in self.confirmed_country_band_modeclass:
                return "confirmed"
            if key in self.worked_country_band_modeclass:
                return "worked"
        return "new"

    def country_modeclass_status(self, country: str, modeclass: str) -> str:
        """Entity-level (any band) modeclass status — for the ARRL mode DXCCs
        (DXCC-CW / Phone / Digital), which are entity-count awards, NOT band-
        split. Answers "have I ever had this entity on this mode, on any band?"
        Keyed by country name (matches how spots are identified), with the same
        QRZ→cty.dat normalization as the other country lookups. Returns
        "new" / "worked" / "confirmed"."""
        if not country or not modeclass or modeclass == "Mixed":
            return "new"
        c = modeclass.strip()
        for name in (country, _qrz_country(country)):
            if (name, c) in self.confirmed_country_modeclass:
                return "confirmed"
            if (name, c) in self.worked_country_modeclass:
                return "worked"
        return "new"

    def grid_band_status(self, grid: str, band: str) -> str:
        if not grid or not band:
            return "new"
        key = (_norm_grid4(grid), _norm_band(band))
        if key in self.confirmed_grid_band:
            return "confirmed"
        if key in self.worked_grid_band:
            return "worked"
        return "new"

    def is_worked(self, call: str) -> bool:
        return _norm_call(call) in self.worked_calls

    def is_confirmed(self, call: str) -> bool:
        return _norm_call(call) in self.confirmed_calls

    def summary(self) -> dict:
        return {
            "qso_count": self.qso_count,
            "unique_calls": self.unique_calls_count,
            "confirmed_qsos": self.confirmed_qso_count,
            "worked_dxcc": len(self.worked_dxcc),
            "confirmed_dxcc": len(self.confirmed_dxcc),
            "worked_dxcc_band_combos": len(self.worked_dxcc_band),
            "confirmed_dxcc_band_combos": len(self.confirmed_dxcc_band),
        }


# ----------------------------------------------------------------------
# Stage 0: GT-style multi-axis score summary (cross-check oracle)
# ----------------------------------------------------------------------


def _new_stat_object() -> dict:
    return {
        "worked": 0, "confirmed": 0,
        "worked_bands": {}, "worked_modes": {}, "worked_types": {},
        "confirmed_bands": {}, "confirmed_modes": {}, "confirmed_types": {},
    }


def _bump(d: dict, key: str) -> None:
    d[key] = d.get(key, 0) + 1


def _work_object(obj: dict, count_for_types: bool, band: str, mode: str,
                 type_: str, did_confirm: bool) -> None:
    """Port of GT2 workObject() — increment worked/confirmed bins.

    `count_for_types=False` means this is a per-axis bin (DXCC entity etc.)
    where we DO accumulate type breakdowns; `True` means a top-level
    Mixed/CW/Phone/Digital roll-up where we DON'T (would double-count).
    Note: GT's flag semantics are inverted (their `count` param is true for
    type-rollups); we use the more readable inverse.
    """
    obj["worked"] += 1
    _bump(obj["worked_bands"], band)
    _bump(obj["worked_modes"], mode)
    if count_for_types:
        # For per-axis bins: every QSO bumps Mixed plus its specific type
        _bump(obj["worked_types"], "Mixed")
        _bump(obj["worked_types"], type_)
    if did_confirm:
        obj["confirmed"] += 1
        _bump(obj["confirmed_bands"], band)
        _bump(obj["confirmed_modes"], mode)
        if count_for_types:
            _bump(obj["confirmed_types"], "Mixed")
            _bump(obj["confirmed_types"], type_)


def compute_score_summary(logbook_path: str | Path) -> dict:
    """Per-axis QSO-log roll-up — port of GT2's renderStatsBox() core loop.

    Returns a dict shaped exactly like GT's stats output:

        { "DXCC": { entity_name: <stat>, ... },
          "GRID": { grid4: <stat>, ... },
          "CQ":   { zone: <stat>, ... },
          "ITU":  { zone: <stat>, ... },
          "WAS":  { state: <stat>, ... },
          "WAC":  { continent: <stat>, ... },
          "WPX":  { prefix: <stat>, ... },
          "USC":  { county: <stat>, ... },
          "_modet": { Mixed/CW/Phone/Digital/Other: <stat>, ... },
          "_meta": { qso_count, unique_calls, confirmed_qsos } }

    where each <stat> is the dict returned by `_new_stat_object()`.

    Used as a cross-check oracle against WorkedState's incremental sets:
    `len(summary["DXCC"])` should equal `len(ws.worked_countries)`,
    `sum band-set sizes across DXCC entries` should equal
    `len(ws.worked_country_band)`, and so on. Any disagreement is a bug
    in one of the two paths.
    """
    path = Path(logbook_path)
    data = json.loads(path.read_text())
    qsos = data.get("qsos", [])

    country_to_continent = _load_country_to_continent()

    axes = {k: {} for k in ("DXCC", "GRID", "CQ", "ITU", "WAS", "WAC", "WPX", "USC")}
    modet = {k: _new_stat_object() for k in ("Mixed", "CW", "Phone", "Digital", "Other")}

    confirmed_count = 0
    unique_calls: set[str] = set()

    for q in qsos:
        call = _norm_call(q.get("call", ""))
        band = _norm_band(q.get("band", ""))
        mode = (q.get("mode") or "").strip().upper()
        grid4 = _norm_grid4(q.get("grid", ""))
        country = (q.get("country") or "").strip()
        cqz = (q.get("cqz") or "").strip()
        ituz = (q.get("ituz") or "").strip()
        state = (q.get("state") or "").strip()
        cnty = (q.get("cnty") or "").strip()
        type_ = mode_class(mode)
        did_confirm = _is_confirmed_record(q)
        if did_confirm:
            confirmed_count += 1
        if call:
            unique_calls.add(call)

        # Top-level mode-type rollup (Mixed / CW / Phone / Digital / Other).
        # GT increments Mixed on every QSO and the specific type bucket too.
        # Pass count_for_types=False because these ARE the type buckets —
        # GT doesn't recurse type-tracking into them (would loop forever).
        _work_object(modet["Mixed"], False, band, mode, type_, did_confirm)
        if type_ in modet:
            _work_object(modet[type_], False, band, mode, type_, did_confirm)

        # DXCC by entity name (GT keys by dxccToAltName; we use the country
        # field from the logbook directly — same identity, different lookup
        # path).
        if country:
            axes["DXCC"].setdefault(country, _new_stat_object())
            _work_object(axes["DXCC"][country], True, band, mode, type_, did_confirm)

            # WAC: country -> continent
            cont = country_to_continent.get(country)
            if cont is None:
                # Try the cty.dat alias direction too — logbook uses "Germany",
                # cty.dat says "Fed. Rep. of Germany"
                for cty_name, qrz_name in CTY_TO_LOGBOOK_NAME.items():
                    if qrz_name == country:
                        cont = country_to_continent.get(cty_name)
                        break
            if cont:
                axes["WAC"].setdefault(cont, _new_stat_object())
                _work_object(axes["WAC"][cont], True, band, mode, type_, did_confirm)

        if grid4:
            axes["GRID"].setdefault(grid4, _new_stat_object())
            _work_object(axes["GRID"][grid4], True, band, mode, type_, did_confirm)

        if cqz:
            axes["CQ"].setdefault(cqz, _new_stat_object())
            _work_object(axes["CQ"][cqz], True, band, mode, type_, did_confirm)

        if ituz:
            axes["ITU"].setdefault(ituz, _new_stat_object())
            _work_object(axes["ITU"][ituz], True, band, mode, type_, did_confirm)

        if state:
            axes["WAS"].setdefault(state, _new_stat_object())
            _work_object(axes["WAS"][state], True, band, mode, type_, did_confirm)

        if cnty:
            axes["USC"].setdefault(cnty, _new_stat_object())
            _work_object(axes["USC"][cnty], True, band, mode, type_, did_confirm)

        if call:
            px = wpx_prefix(call)
            if px:
                axes["WPX"].setdefault(px, _new_stat_object())
                _work_object(axes["WPX"][px], True, band, mode, type_, did_confirm)

    return {
        **axes,
        "_modet": modet,
        "_meta": {
            "qso_count": len(qsos),
            "unique_calls": len(unique_calls),
            "confirmed_qsos": confirmed_count,
        },
    }


def cross_check(logbook_path: str | Path) -> dict:
    """Run compute_score_summary() against a fresh WorkedState load and
    diff the headline totals. Returns a dict with `agree` and per-axis
    deltas — used to verify Stage 0 correctness.
    """
    ws = WorkedState(logbook_path)
    summary = compute_score_summary(logbook_path)

    # (country, band) tuples GT would count: sum band-set sizes per entity
    gt_country_band = sum(len(s["worked_bands"]) for s in summary["DXCC"].values())
    gt_grid_band = sum(len(s["worked_bands"]) for s in summary["GRID"].values())

    deltas = {
        "qso_count": ws.qso_count - summary["_meta"]["qso_count"],
        "unique_calls": ws.unique_calls_count - summary["_meta"]["unique_calls"],
        "confirmed_qsos": ws.confirmed_qso_count - summary["_meta"]["confirmed_qsos"],
        "country_count_ws": len(ws.worked_countries),
        "country_count_gt": len(summary["DXCC"]),
        "country_band_combos_ws": len(ws.worked_country_band),
        "country_band_combos_gt": gt_country_band,
        "grid_band_combos_ws": len(ws.worked_grid_band),
        "grid_band_combos_gt": gt_grid_band,
        "wpx_count": len(summary["WPX"]),
        "was_states": len(summary["WAS"]),
        "wac_continents": len(summary["WAC"]),
        "usc_counties": len(summary["USC"]),
        "cq_zones": len(summary["CQ"]),
        "itu_zones": len(summary["ITU"]),
    }
    agree = (
        deltas["qso_count"] == 0
        and deltas["unique_calls"] == 0
        and deltas["confirmed_qsos"] == 0
        and deltas["country_count_ws"] == deltas["country_count_gt"]
        and deltas["country_band_combos_ws"] == deltas["country_band_combos_gt"]
        and deltas["grid_band_combos_ws"] == deltas["grid_band_combos_gt"]
    )
    return {"agree": agree, **deltas, "modet": {
        k: {"worked": v["worked"], "confirmed": v["confirmed"]}
        for k, v in summary["_modet"].items()
    }}


if __name__ == "__main__":
    import pprint
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "qrz_logbook.json"
    pprint.pp(cross_check(path))
