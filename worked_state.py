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


def _load_mode_tables() -> tuple[dict, dict]:
    try:
        modes = json.loads((_DATA_DIR / "modes.json").read_text())
        modes_phone = json.loads((_DATA_DIR / "modes-phone.json").read_text())
    except Exception as e:
        log.warning("mode tables unavailable (%s); falling back to minimal classifier", e)
        modes, modes_phone = {}, {}
    return modes, modes_phone


_MODES, _MODES_PHONE = _load_mode_tables()


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

        self.worked_grid_band: set[tuple[str, str]] = set()  # (grid4, band)
        self.confirmed_grid_band: set[tuple[str, str]] = set()

        self.worked_dxcc: set[str] = set()  # DXCC ID ever, any band/mode
        self.confirmed_dxcc: set[str] = set()
        self.worked_countries: set[str] = set()
        self.confirmed_countries: set[str] = set()

        self.qso_count = 0
        self.unique_calls_count = 0
        self.confirmed_qso_count = 0

        self.reload()

    def reload(self) -> bool:
        """(Re)load the logbook JSON from disk if mtime changed. Returns True if reloaded."""
        if not self.logbook_path.exists():
            log.warning("logbook not found: %s — running with empty worked state", self.logbook_path)
            return False
        mtime = self.logbook_path.stat().st_mtime
        if self._mtime is not None and mtime == self._mtime:
            return False
        try:
            data = json.loads(self.logbook_path.read_text())
        except Exception as e:
            log.warning("failed to parse logbook %s: %s", self.logbook_path, e)
            return False

        worked_calls: set[str] = set()
        confirmed_calls: set[str] = set()
        worked_dxcc_band: set[tuple[str, str]] = set()
        confirmed_dxcc_band: set[tuple[str, str]] = set()
        worked_country_band: set[tuple[str, str]] = set()
        confirmed_country_band: set[tuple[str, str]] = set()
        worked_country_band_mode: set[tuple[str, str, str]] = set()
        confirmed_country_band_mode: set[tuple[str, str, str]] = set()
        worked_grid_band: set[tuple[str, str]] = set()
        confirmed_grid_band: set[tuple[str, str]] = set()
        worked_dxcc: set[str] = set()
        confirmed_dxcc: set[str] = set()
        worked_countries: set[str] = set()
        confirmed_countries: set[str] = set()
        confirmed_qso_count = 0

        qsos = data.get("qsos", [])
        for q in qsos:
            call = _norm_call(q.get("call", ""))
            band = _norm_band(q.get("band", ""))
            mode = (q.get("mode") or "").strip().upper()
            grid4 = _norm_grid4(q.get("grid", ""))
            dxcc = (q.get("dxcc") or "").strip()
            country = (q.get("country") or "").strip()
            confirmed = _is_confirmed_record(q)
            if confirmed:
                confirmed_qso_count += 1

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

            if grid4 and band:
                worked_grid_band.add((grid4, band))
                if confirmed:
                    confirmed_grid_band.add((grid4, band))

        self.worked_calls = worked_calls
        self.confirmed_calls = confirmed_calls
        self.worked_dxcc_band = worked_dxcc_band
        self.confirmed_dxcc_band = confirmed_dxcc_band
        self.worked_country_band = worked_country_band
        self.confirmed_country_band = confirmed_country_band
        self.worked_country_band_mode = worked_country_band_mode
        self.confirmed_country_band_mode = confirmed_country_band_mode
        self.worked_grid_band = worked_grid_band
        self.confirmed_grid_band = confirmed_grid_band
        self.worked_dxcc = worked_dxcc
        self.confirmed_dxcc = confirmed_dxcc
        self.worked_countries = worked_countries
        self.confirmed_countries = confirmed_countries
        self.qso_count = len(qsos)
        self.unique_calls_count = len(worked_calls)
        self.confirmed_qso_count = confirmed_qso_count
        self._mtime = mtime

        log.info("worked_state loaded: %d QSOs, %d unique calls, %d confirmed",
                 self.qso_count, self.unique_calls_count, self.confirmed_qso_count)
        return True

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
