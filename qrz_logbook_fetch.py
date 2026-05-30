#!/usr/bin/env python3
"""
qrz_logbook_fetch — pull your QRZ Logbook QSOs and parse to JSON.

Pattern lifted from GridTracker 2's adif.js (BSD 3-Clause):
single HTTPS GET to logbook.qrz.com/api?KEY=...&ACTION=FETCH returns
your complete QSO history as ADIF text.

Output: /home/fred/grayline/qrz_logbook.json
  - meta: { fetched_at, qso_count, callsign_count }
  - qsos: [ { call, band, mode, time, grid, qsl_received, ... }, ... ]

Auth: requires `qrz_logbook_api_key` in secrets.json. Get the key from
QRZ Logbook settings (separate from your XML lookup credentials).

Usage:
  python3 qrz_logbook_fetch.py            # full fetch
  python3 qrz_logbook_fetch.py --test     # validate API key only

This script does NOT touch QRZ until invoked. It's not on a cron.
Run on demand or schedule daily later when we want continuous sync.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SECRETS_PATH = Path("/home/fred/grayline/secrets.json")
OUTPUT_PATH = Path("/home/fred/grayline/qrz_logbook.json")
RAW_ADIF_PATH = Path("/home/fred/grayline/qrz_logbook.adi")
QRZ_API_URL = "https://logbook.qrz.com/api"

# ADIF field regex: <FIELD:LENGTH:TYPE>VALUE  (TYPE optional)
ADIF_FIELD_RE = re.compile(r"<([A-Za-z0-9_]+):(\d+)(?::[A-Za-z])?>", re.IGNORECASE)


def load_api_key() -> str:
    secrets = json.loads(SECRETS_PATH.read_text())
    key = secrets.get("qrz_logbook_api_key")
    if not key:
        raise RuntimeError(
            "qrz_logbook_api_key not in secrets.json. "
            "Get one from https://www.qrz.com/logbook → Settings → API Key. "
            "Format: XXXX-XXXX-XXXX-XXXX (19 chars, 3 dashes)."
        )
    if len(key) != 19 or key.count("-") != 3:
        raise RuntimeError(f"qrz_logbook_api_key looks malformed (got len={len(key)})")
    return key


def fetch_logbook(api_key: str, action: str = "FETCH") -> str:
    url = f"{QRZ_API_URL}?KEY={urllib.parse.quote(api_key)}&ACTION={action}"
    print(f"GET {QRZ_API_URL}?KEY=<redacted>&ACTION={action}", file=sys.stderr)
    resp = urllib.request.urlopen(url, timeout=120)
    body = resp.read().decode("utf-8", errors="replace")
    if "invalid api key" in body.lower():
        raise RuntimeError("QRZ rejected the API key as invalid")
    return body


def parse_adif(adif_text: str) -> list[dict]:
    """Parse ADIF into a list of QSO record dicts.

    ADIF format: each record is a series of <FIELD:LEN>VALUE pairs,
    terminated by <EOR>. Header (if present) ends at <EOH>. We split
    on <EOR> case-insensitively and parse each record's fields.
    """
    # QRZ wraps response in HTML-style; strip the part before the first ADIF
    # tag if present. Also normalize HTML entities (GT2 does the same).
    text = adif_text.replace("&lt;", "<").replace("&gt;", ">")

    # Drop everything up to (and including) <EOH> if there's a header
    eoh = re.search(r"<EOH>", text, re.IGNORECASE)
    if eoh:
        text = text[eoh.end():]

    # Split on <EOR>
    records = re.split(r"<EOR>", text, flags=re.IGNORECASE)
    qsos = []
    for raw in records:
        rec = {}
        pos = 0
        for m in ADIF_FIELD_RE.finditer(raw):
            field_name = m.group(1).upper()
            field_len = int(m.group(2))
            value_start = m.end()
            value = raw[value_start:value_start + field_len]
            rec[field_name] = value.strip()
        if rec:
            qsos.append(rec)
    return qsos


def normalize_qso(rec: dict) -> dict:
    """Pick the fields we care about for worked/needed tracking. Keep
    raw record around as `_raw` for any future fields we want to extract."""
    out = {
        "call": rec.get("CALL", "").upper(),
        "band": rec.get("BAND", "").lower(),
        "mode": rec.get("MODE", "").upper(),
        "submode": rec.get("SUBMODE", "").upper(),
        "qso_date": rec.get("QSO_DATE", ""),
        "time_on": rec.get("TIME_ON", ""),
        "freq": rec.get("FREQ", ""),
        "grid": rec.get("GRIDSQUARE", "").upper(),
        "dxcc": rec.get("DXCC", ""),
        "country": rec.get("COUNTRY", ""),
        "cqz": rec.get("CQZ", ""),
        "ituz": rec.get("ITUZ", ""),
        "state": rec.get("STATE", ""),
        "cnty": rec.get("CNTY", ""),
        "qsl_rcvd": rec.get("QSL_RCVD", ""),  # Y/N — paper QSL received
        "lotw_qsl_rcvd": rec.get("LOTW_QSL_RCVD", ""),  # LoTW confirmation
        "eqsl_qsl_rcvd": rec.get("EQSL_QSL_RCVD", ""),
        "prop_mode": rec.get("PROP_MODE", ""),  # SAT, MS, AUR, etc. (often missing from QRZ)
        "app_qrzlog_logid": rec.get("APP_QRZLOG_LOGID", ""),
    }
    return out


def summarize(qsos: list[dict]) -> dict:
    callsigns = set()
    bands = set()
    modes = set()
    confirmed_lotw = 0
    confirmed_paper = 0
    for q in qsos:
        if q["call"]: callsigns.add(q["call"])
        if q["band"]: bands.add(q["band"])
        if q["mode"]: modes.add(q["mode"])
        if q["lotw_qsl_rcvd"] == "Y": confirmed_lotw += 1
        if q["qsl_rcvd"] == "Y": confirmed_paper += 1
    return {
        "qso_count": len(qsos),
        "unique_callsigns": len(callsigns),
        "bands_worked": sorted(bands),
        "modes_worked": sorted(modes),
        "confirmed_lotw": confirmed_lotw,
        "confirmed_paper_qsl": confirmed_paper,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--test", action="store_true", help="Validate the API key without fetching")
    args = ap.parse_args()

    try:
        api_key = load_api_key()
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.test:
        try:
            body = fetch_logbook(api_key, action="STATUS")
            print(f"STATUS response (first 200 chars):\n  {body[:200]!r}")
            print("API key looks valid.")
            return
        except Exception as e:
            print(f"key test failed: {e}", file=sys.stderr)
            sys.exit(2)

    try:
        adif = fetch_logbook(api_key, action="FETCH")
    except Exception as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        sys.exit(2)

    # Save raw ADIF for debugging / re-parse
    RAW_ADIF_PATH.write_text(adif)
    print(f"raw ADIF saved: {RAW_ADIF_PATH} ({len(adif):,} bytes)", file=sys.stderr)

    raw_qsos = parse_adif(adif)
    qsos = [normalize_qso(r) for r in raw_qsos]
    qsos = [q for q in qsos if q["call"]]  # drop records with no callsign

    summary = summarize(qsos)
    payload = {
        "meta": {
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "QRZ Logbook API",
            **summary,
        },
        "qsos": qsos,
    }

    OUTPUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"wrote {OUTPUT_PATH}: {summary['qso_count']:,} QSOs, "
          f"{summary['unique_callsigns']:,} unique calls, "
          f"{summary['confirmed_lotw']:,} LoTW-confirmed, "
          f"{summary['confirmed_paper_qsl']:,} paper-QSL-confirmed",
          file=sys.stderr)


if __name__ == "__main__":
    main()
