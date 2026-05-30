"""
Logbook upload chain for Grayline.

On every WSJT-X QSO Logged event, Grayline writes a local ADIF record and
then kicks off parallel uploads to QRZ Logbook, ClubLog Realtime, eQSL, and
LoTW. LoTW is special: it shells out to `tqsl` (with xvfb-run for headless
GTK on .101) to sign the ADIF with the operator's certificate, then upload.
The other three are direct HTTPS POSTs.

All four uploads are fire-and-forget — they run in a background thread,
log success/failure to qso_uploads.log, and never block the WSJT-X handler.
Credentials missing in secrets.json => the corresponding service is skipped
silently with a "not configured" message.

QRZ response parsing edge cases (RESULT vs STATUS field, AUTH/FAIL/EXTENDED
distinction, "outside date range" friendly message) and the one-retry on
timeout pattern are lifted from GridTracker 2 (BSD-3-Clause, attributed
in NOTICE). The TQSL invocation flags + Final-Status-Success success check
are also from GT2 (adif.js TQSLLogger).

API references:
- QRZ:     https://www.qrz.com/docs/logbook30/api
- ClubLog: https://clublog.org/loginhelp.php (realtime.php endpoint)
- eQSL:    https://www.eqsl.cc/qslcard/ImportADIF.cfm
- LoTW:    https://lotw.arrl.org/lotw-help/  (tqsl(1) man page)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("grayline.uploads")

SECRETS_PATH = Path("/home/fred/grayline/secrets.json")
UPLOAD_LOG_PATH = Path("/home/fred/grayline/qso_uploads.log")
QRZ_LOGBOOK_URL = "https://logbook.qrz.com/api"
CLUBLOG_URL = "https://clublog.org/realtime.php"
EQSL_URL = "https://www.eqsl.cc/qslcard/ImportADIF.cfm"
HTTP_TIMEOUT = 30
USER_AGENT = "Grayline/0.1"

# TQSL configuration — values overridable via secrets.json keys
# `tqsl_station_location` and `tqsl_passphrase` (passphrase only if cert
# is encrypted; we currently don't set one).
TQSL_STATION_DEFAULT = "Home"
TQSL_TIMEOUT_SEC = 60
TQSL_BIN = shutil.which("tqsl")
XVFB_RUN_BIN = shutil.which("xvfb-run")


def _http_post(url: str, body_dict: dict, retry_on_timeout: bool = True) -> tuple[int, str]:
    """POST to url with form-encoded body. Returns (status_code, body_text).
    On urllib timeout/network error, retries ONCE if retry_on_timeout is True
    (the GT2 postRetryErrorCallaback pattern). Re-raises any other exception
    so callers see real failures."""
    body = urllib.parse.urlencode(body_dict).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
    )
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
            return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 1 and retry_on_timeout:
                continue  # one retry, GT2 pattern
            raise


def _load_secrets() -> dict:
    try:
        return json.loads(SECRETS_PATH.read_text())
    except Exception as e:
        log.warning("Failed to read secrets.json for upload credentials: %s", e)
        return {}


def _log_upload(service: str, dx_call: str, success: bool, message: str):
    """Append upload result to the audit log. Format is one line per attempt:
    ISO-timestamp service status dx_call message"""
    try:
        with open(UPLOAD_LOG_PATH, "a") as f:
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            status = "OK" if success else "FAIL"
            f.write(f"{ts}  {service:8s}  {status:4s}  {dx_call:12s}  {message}\n")
    except Exception as e:
        log.warning("Failed to append to %s: %s", UPLOAD_LOG_PATH, e)


# ------------------------------------------------------------------ #
#  Upload functions — synchronous, return (success: bool, msg: str)   #
# ------------------------------------------------------------------ #

def upload_qrz(adif_record: str, api_key: str) -> tuple[bool, str]:
    """POST a single ADIF record to QRZ Logbook via the INSERT action.

    Response is x-www-form-urlencoded. Possible fields:
      RESULT=OK / RESULT=FAIL / RESULT=AUTH (newer API)
      STATUS=OK / STATUS=FAIL  (older API, fallback)
      LOGID=<numeric>  (on success)
      REASON=<text>    (on FAIL)
      EXTENDED=<text>  (richer error info, sometimes present)

    Edge-case handling lifted from GT2 qrzSendLogResult (BSD-3-Clause attributed
    in NOTICE): RESULT-first/STATUS-fallback parsing, AUTH-as-distinct-from-FAIL,
    EXTENDED preferred when present, "outside date range" friendly translation.
    """
    if not api_key:
        return False, "no api_key"
    try:
        code, text = _http_post(QRZ_LOGBOOK_URL, {
            "KEY": api_key,
            "ACTION": "INSERT",
            "ADIF": adif_record,
        })
    except Exception as e:
        return False, f"network: {e}"

    if code != 200:
        return False, f"HTTP {code}: {text[:200]}"

    params = dict(urllib.parse.parse_qsl(text, keep_blank_values=True))
    if not params:
        return False, f"no params in response: {text[:200]}"

    # GT2 pattern: prefer RESULT field, fall back to STATUS
    if "RESULT" in params:
        result = params.get("RESULT", "").upper()
        if result == "OK":
            return True, f"LOGID={params.get('LOGID', '?')}"
        if result == "AUTH":
            return False, "QRZ Invalid Auth"
        if result == "FAIL":
            reason = params.get("REASON") or params.get("EXTENDED") or text[:200]
            if "outside date range" in reason.lower():
                return False, "Logbook Date Range!"
            return False, f"QRZ FAIL: {reason}"
        return False, f"QRZ unknown RESULT={result}: {text[:200]}"

    if "STATUS" in params:
        status = params.get("STATUS", "").upper()
        if status == "OK":
            return True, f"LOGID={params.get('LOGID', '?')}"
        reason = params.get("EXTENDED") or params.get("REASON") or text[:200]
        if "outside date range" in reason.lower():
            return False, "Logbook Date Range!"
        return False, f"QRZ STATUS={status}: {reason}"

    return False, f"QRZ unknown response shape: {text[:200]}"


def upload_clublog(adif_record: str, email: str, password: str,
                   callsign: str, api_key: str = "") -> tuple[bool, str]:
    """POST a single ADIF record to ClubLog's realtime.php endpoint.

    Per GT2's clubLogQsoResult (BSD-3-Clause attributed in NOTICE), ClubLog's
    success signal is just HTTP 200. No body parsing required — empty 200 = OK.
    Non-2xx HTTP = fail. Realtime API accepts ~20k QSOs/hour and dedupes by
    content, so retries on transient errors are safe.
    """
    if not (email and password and callsign):
        return False, "missing creds (need email + password + callsign)"
    body_dict = {
        "email": email,
        "password": password,
        "callsign": callsign,
        "adif": adif_record,
    }
    if api_key:
        body_dict["api"] = api_key
    try:
        code, text = _http_post(CLUBLOG_URL, body_dict)
    except Exception as e:
        return False, f"network: {e}"

    if code == 200:
        # ClubLog success = HTTP 200 (GT2 pattern). Body usually empty.
        return True, text[:80].strip() if text.strip() else "OK"
    return False, f"HTTP {code}: {text[:200]}"


def upload_eqsl(adif_record: str, user: str, password: str) -> tuple[bool, str]:
    """POST a single ADIF record to eQSL's ImportADIF.cfm.

    NOTE: this is from-scratch — GT2 doesn't include eQSL upload, so we don't
    have battle-tested edge-case handling for eQSL responses. May need
    iteration once we see real responses in production.

    eQSL embeds the result in an HTML page. Known success markers from the
    eQSL docs / community: "QSOs Added: 1", "Result: 1 out of 1 records
    added", or HTTP 200 with no error string. Known failure markers: "ERROR",
    "Authentication failed", "Bad password".
    """
    if not (user and password):
        return False, "missing creds (need user + password)"
    try:
        code, text = _http_post(EQSL_URL, {
            "EQSL_USER": user,
            "EQSL_PSWD": password,
            "ADIFData": adif_record,
        })
    except Exception as e:
        return False, f"network: {e}"

    if code != 200:
        return False, f"HTTP {code}"

    upper = text.upper()
    # Failure markers first — explicit error wins over inferred success
    for fail_token in ("ERROR", "AUTHENTICATION FAIL", "BAD PASSWORD", "INVALID"):
        if fail_token in upper:
            start = max(0, upper.find(fail_token) - 40)
            return False, text[start:start+200].strip().replace("\n", " ")
    # Success markers
    if "ADDED" in upper or "RECORDS" in upper:
        for line in text.split("\n"):
            l = line.strip()
            if "added" in l.lower() and l:
                return True, l[:160]
        return True, "added (success marker present)"
    return False, f"ambiguous eQSL response: {text[:200]}"


# ------------------------------------------------------------------ #
#  LoTW (TQSL shell-out)                                              #
# ------------------------------------------------------------------ #

def upload_lotw(adif_record: str, station_location: str = TQSL_STATION_DEFAULT,
                passphrase: str = "") -> tuple[bool, str]:
    """Sign + upload a single QSO to LoTW via tqsl.

    Writes the ADIF record to a temp file (with a minimal <EOH> header so
    tqsl accepts it), then runs:

        tqsl -a all -l <station> [-p <passphrase>] -q -x -d -u <input>

    Flags:
      -a all  process every record
      -l      station location (must already exist in ~/.tqsl/station_data)
      -p      passphrase for the signing key (omit if cert has no passphrase)
      -q -x   quiet + exit after upload
      -d      suppress date-range dialog
      -u      upload after signing

    Wrapped in xvfb-run because Ubuntu's trustedqsl 2.5.x is GUI-built and
    initializes GTK even for CLI batch mode. Success indicator: stderr
    (or stdout — tqsl mixes them under xvfb) contains "Final Status: Success".
    """
    if not TQSL_BIN:
        return False, "tqsl not installed"
    if not XVFB_RUN_BIN:
        return False, "xvfb-run not installed (needed for headless tqsl on Linux)"

    # Write ADIF to a temp file; tqsl needs a real file path on disk.
    header = ("Generated by Grayline\n"
              "<PROGRAMID:8>Grayline\n"
              "<PROGRAMVERSION:3>0.1\n"
              "<EOH>\n")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".adi", delete=False,
                                     prefix="grayline_lotw_") as f:
        f.write(header)
        f.write(adif_record.strip())
        f.write("\n")
        adif_path = f.name

    try:
        cmd = [XVFB_RUN_BIN, "-a", TQSL_BIN,
               "-a", "all",
               "-l", station_location,
               "-q", "-x", "-d", "-u",
               adif_path]
        if passphrase:
            # Insert -p <passphrase> before the input file
            cmd[-1:] = ["-p", passphrase, adif_path]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=TQSL_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            return False, f"tqsl timeout after {TQSL_TIMEOUT_SEC}s"

        # tqsl emits its status banner to both stdout and stderr depending
        # on phase; check both. The success token is exact: "Final Status: Success".
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if "Final Status: Success" in combined:
            return True, "uploaded to LoTW"
        # Surface whatever tqsl said about the failure — last informative line
        # is usually the "Final Status: ...(N)" with the error code.
        last_status_line = ""
        for line in combined.splitlines():
            if "Final Status:" in line:
                last_status_line = line.strip()
        if not last_status_line:
            last_status_line = (combined.strip().splitlines() or ["unknown tqsl error"])[-1]
        return False, f"tqsl: {last_status_line}"
    finally:
        try:
            os.unlink(adif_path)
        except OSError:
            pass


# ------------------------------------------------------------------ #
#  Dispatcher — parallel fire-and-forget                              #
# ------------------------------------------------------------------ #

def upload_qso_to_all(adif_record: str, dx_call: str = "?"):
    """Spawn a background thread that uploads to all three services in
    parallel sub-threads. Fire-and-forget: returns immediately. Each
    service's result lands in qso_uploads.log."""
    threading.Thread(
        target=_run_uploads_in_parallel,
        args=(adif_record, dx_call),
        daemon=True,
        name=f"upload-{dx_call}",
    ).start()


def _run_uploads_in_parallel(adif_record: str, dx_call: str):
    secrets = _load_secrets()

    qrz_key = secrets.get("qrz_logbook_api_key", "")
    clublog_email = secrets.get("clublog_email", "")
    clublog_password = secrets.get("clublog_password", "")
    clublog_callsign = secrets.get("clublog_callsign", "") or secrets.get("qrz_user", "").upper()
    clublog_api = secrets.get("clublog_api_key", "")
    eqsl_user = secrets.get("eqsl_user", "") or secrets.get("qrz_user", "").upper()
    eqsl_pass = secrets.get("eqsl_password", "")
    tqsl_station = secrets.get("tqsl_station_location", TQSL_STATION_DEFAULT)
    tqsl_passphrase = secrets.get("tqsl_passphrase", "")

    targets = [
        ("QRZ", lambda: upload_qrz(adif_record, qrz_key)),
        ("CLUBLOG", lambda: upload_clublog(adif_record, clublog_email,
                                           clublog_password, clublog_callsign,
                                           clublog_api)),
        ("EQSL", lambda: upload_eqsl(adif_record, eqsl_user, eqsl_pass)),
        ("LOTW", lambda: upload_lotw(adif_record, tqsl_station, tqsl_passphrase)),
    ]

    threads = []
    results = {}

    def runner(service, fn):
        try:
            ok, msg = fn()
        except Exception as e:
            ok, msg = False, f"runner exception: {e}"
        results[service] = (ok, msg)
        _log_upload(service, dx_call, ok, msg)

    for name, fn in targets:
        t = threading.Thread(target=runner, args=(name, fn), daemon=True,
                             name=f"upload-{dx_call}-{name}")
        t.start()
        threads.append(t)

    # LoTW (tqsl shell-out) is slowest; size the join timeout for it so the
    # summary line below reflects its real result rather than reporting it
    # as FAIL just because the HTTP-only timeout elapsed.
    join_timeout = max(HTTP_TIMEOUT, TQSL_TIMEOUT_SEC) + 5
    for t in threads:
        t.join(timeout=join_timeout)

    summary = ", ".join(f"{s}={'OK' if r[0] else 'FAIL'}"
                        for s, r in sorted(results.items()))
    log.info("QSO uploads complete for %s: %s", dx_call, summary)
