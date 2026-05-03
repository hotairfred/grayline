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
from pathlib import Path

log = logging.getLogger("worked_state")


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
