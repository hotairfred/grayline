#!/usr/bin/env python3
"""
lotw_fetch — pull confirmed QSLs from ARRL Logbook of the World.

Pattern lifted from GridTracker 2's adif.js (BSD 3-Clause):
single HTTPS GET to lotw.arrl.org/lotwuser/lotwreport.adi with
login+password in the query string. Incremental: cursor is the
max APP_LOTW_RXQSL timestamp seen, persisted as lotw_state.json.
LoTW returns ADIF; we append to lotw_qsl.adi and let
worked_state.reload() merge it.

Auth: `lotw_user` (callsign) + `lotw_password` (LoTW website
password, NOT TQSL passphrase) in secrets.json.

Usage:
  python3 lotw_fetch.py            # incremental fetch (uses cursor)
  python3 lotw_fetch.py --test     # auth probe (qso_qsosince=2100-01-01)
  python3 lotw_fetch.py --reset    # wipe cursor + re-pull all confirmations
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

_BASE_DIR = Path(__file__).resolve().parent
SECRETS_PATH = _BASE_DIR / "secrets.json"
STATE_PATH = _BASE_DIR / "lotw_state.json"
ADIF_PATH = _BASE_DIR / "lotw_qsl.adi"
LOTW_URL = "https://lotw.arrl.org/lotwuser/lotwreport.adi"
TIMEOUT_SEC = 120

ADIF_FIELD_RE = re.compile(r"<([A-Za-z0-9_]+):(\d+)(?::[A-Za-z])?>", re.IGNORECASE)
EOR_RE = re.compile(r"<EOR>", re.IGNORECASE)
EOH_RE = re.compile(r"<EOH>", re.IGNORECASE)


def load_creds() -> tuple[str, str]:
    secrets = json.loads(SECRETS_PATH.read_text())
    user = secrets.get("lotw_user")
    pw = secrets.get("lotw_password")
    if not user or not pw:
        raise RuntimeError(
            "lotw_user / lotw_password missing from secrets.json. "
            "Use your LoTW website password (not TQSL passphrase)."
        )
    return user, pw


def load_cursor_ms() -> int:
    """Last-fetch cursor in epoch ms (matches GT2's adifLog.lastFetch.lotw_qsl).
    0 means 'pull everything since the dawn of time'."""
    if not STATE_PATH.exists():
        return 0
    try:
        return int(json.loads(STATE_PATH.read_text()).get("lotw_lastfetch_ms", 0))
    except Exception:
        return 0


def save_cursor_ms(ms: int) -> None:
    STATE_PATH.write_text(json.dumps({
        "lotw_lastfetch_ms": int(ms),
        "lotw_lastfetch_iso": datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ms else "",
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, indent=2))


def cursor_to_lotw_string(ms: int) -> str:
    """Format epoch-ms as 'YYYY-MM-DD HH:MM:SS' UTC for qso_qslsince."""
    if ms <= 0:
        # Pull from a sane epoch start; LoTW accepts very-old dates fine.
        return "1970-01-01 00:00:00"
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def build_url(user: str, pw: str, qslsince: str, mode: str = "fetch") -> str:
    """mode='fetch'  -> qso_qsl=yes, qso_qslsince=<cursor>
       mode='probe'  -> qso_qsl=no,  qso_qsosince=2100-01-01 (auth-only test)
    """
    base = {
        "login": user,
        "password": pw,
        "qso_query": "1",
        "qso_qsldetail": "yes",
        "qso_withown": "yes",
    }
    if mode == "probe":
        base["qso_qsl"] = "no"
        base["qso_qsosince"] = "2100-01-01"
    else:
        base["qso_qsl"] = "yes"
        base["qso_qslsince"] = qslsince
    return f"{LOTW_URL}?{urllib.parse.urlencode(base)}"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Grayline/0.1"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        return resp.read().decode("utf-8", errors="replace")


def is_password_incorrect(body: str) -> bool:
    return "password incorrect" in body.lower()


def find_max_rxqsl_ms(adif_text: str) -> int:
    """Walk all records, parse APP_LOTW_RXQSL ('YYYY-MM-DD HH:MM:SS'),
    return the max as epoch ms. Bumped +1s by caller (GT2 pattern in
    adifWorker.js:131-143) to avoid re-pulling the same boundary record."""
    max_ms = 0
    # Scan for the field directly without splitting into records — faster
    # and avoids regex backtracking on huge bodies.
    for m in ADIF_FIELD_RE.finditer(adif_text):
        if m.group(1).upper() != "APP_LOTW_RXQSL":
            continue
        length = int(m.group(2))
        value = adif_text[m.end():m.end() + length].strip()
        # 'YYYY-MM-DD HH:MM:SS'
        try:
            dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            ms = int(dt.timestamp() * 1000)
            if ms > max_ms:
                max_ms = ms
        except ValueError:
            continue
    return max_ms


def count_records(adif_text: str) -> int:
    return len(EOR_RE.findall(adif_text))


def append_adif(body: str) -> int:
    """Append the QSO portion (everything after <EOH>, if present) to
    lotw_qsl.adi. Returns count of <EOR> records appended.
    Returns 0 if the body has no records (LoTW returns header-only when
    nothing is new since the cursor).
    """
    text = body
    eoh = EOH_RE.search(text)
    if eoh:
        text = text[eoh.end():]
    n_records = count_records(text)
    if n_records == 0:
        return 0
    with ADIF_PATH.open("a") as f:
        if ADIF_PATH.stat().st_size > 0:
            f.write("\n")
        f.write(text.strip())
        f.write("\n")
    return n_records


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true", help="Auth probe only (no records fetched)")
    ap.add_argument("--reset", action="store_true", help="Wipe cursor + lotw_qsl.adi and re-pull everything")
    args = ap.parse_args()

    try:
        user, pw = load_creds()
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.reset:
        if STATE_PATH.exists():
            STATE_PATH.unlink()
        if ADIF_PATH.exists():
            ADIF_PATH.unlink()
        print("reset: cleared lotw_state.json + lotw_qsl.adi", file=sys.stderr)

    cursor_ms = load_cursor_ms()
    qslsince = cursor_to_lotw_string(cursor_ms)
    mode = "probe" if args.test else "fetch"
    url = build_url(user, pw, qslsince, mode=mode)
    redacted_url = url.replace(urllib.parse.quote(pw), "<redacted>").replace(pw, "<redacted>")
    print(f"GET {redacted_url}", file=sys.stderr)

    try:
        body = fetch(url)
    except Exception as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        sys.exit(2)

    if is_password_incorrect(body):
        print("auth failed: LoTW says 'password incorrect' (use website password, not TQSL passphrase)", file=sys.stderr)
        sys.exit(3)

    if args.test:
        print(f"auth probe ok ({len(body):,} bytes returned, no records expected)")
        return

    n = append_adif(body)
    if n == 0:
        print(f"no new confirmations since {qslsince}", file=sys.stderr)
        return

    max_ms = find_max_rxqsl_ms(body)
    if max_ms > 0:
        # +1 second past the boundary (GT2 pattern: adifWorker.js dRXQSL += 1000)
        save_cursor_ms(max_ms + 1000)
    print(f"appended {n:,} confirmation(s) to {ADIF_PATH.name}; "
          f"cursor → {cursor_to_lotw_string(max_ms + 1000) if max_ms else '(unchanged)'}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
