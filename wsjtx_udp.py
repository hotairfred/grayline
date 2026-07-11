"""
WSJT-X UDP Protocol Encoder/Decoder

Encodes and decodes messages in the WSJT-X UDP binary format (QDataStream)
so that applications like GridTracker 2 can receive and send them.

Message types implemented:
  Encode: 0 - Heartbeat, 1 - Status, 2 - Decode, 5 - QSO Logged
  Decode: 4 - Reply (sent by GridTracker when a callsign is clicked)

Reference: WSJT-X NetworkMessage.hpp
"""

import struct
import time

WSJTX_MAGIC = 0xADBCCBDA
WSJTX_SCHEMA = 2  # schema 2 is widely compatible


def _encode_utf8_string(s):
    """Encode a string as QDataStream QString-like: 4-byte length + UTF-8 bytes.

    py-wsjtx and many implementations use UTF-8 with a 4-byte length prefix
    rather than true UTF-16BE. GridTracker and other consumers accept this.
    A None/null string is encoded as 0xFFFFFFFF.
    """
    if s is None:
        return struct.pack('>I', 0xFFFFFFFF)
    encoded = s.encode('utf-8')
    return struct.pack('>I', len(encoded)) + encoded


def _encode_quint32(val):
    return struct.pack('>I', val)


def _encode_qint32(val):
    return struct.pack('>i', val)


def _encode_quint64(val):
    return struct.pack('>Q', val)


def _encode_quint8(val):
    return struct.pack('>B', val)


def _encode_bool(val):
    return struct.pack('>?', val)


def _encode_qint64(val):
    return struct.pack('>q', val)


def _encode_double(val):
    return struct.pack('>d', val)


def _encode_qdatetime(year, month, day, hour=0, minute=0, second=0):
    """Encode date/time as QDataStream QDateTime (UTC).

    Args: year, month, day, hour, minute, second (integers).
    """
    # QDate: Julian Day Number as qint64
    a = (14 - month) // 12
    y = year + 4800 - a
    m = month + 12 * a - 3
    jdn = (day + (153 * m + 2) // 5 + 365 * y
           + y // 4 - y // 100 + y // 400 - 32045)
    buf = _encode_qint64(jdn)
    # QTime: milliseconds since midnight as quint32
    buf += _encode_quint32((hour * 3600 + minute * 60 + second) * 1000)
    # Timespec: 1 = UTC
    buf += _encode_quint8(1)
    return buf


def _header(msg_type, client_id, schema=WSJTX_SCHEMA):
    """Build the common WSJT-X UDP message header. schema overridable — Configure
    (type 15) is a schema-3-era message and won't be honored at schema 2."""
    buf = b''
    buf += _encode_quint32(WSJTX_MAGIC)
    buf += _encode_quint32(schema)
    buf += _encode_quint32(msg_type)
    buf += _encode_utf8_string(client_id)
    return buf


def heartbeat(client_id="GTBRIDGE", max_schema=3, version="2.6.1", revision=""):
    """Build a Heartbeat message (type 0)."""
    buf = _header(0, client_id)
    buf += _encode_quint32(max_schema)
    buf += _encode_utf8_string(version)
    buf += _encode_utf8_string(revision)
    return buf


def status(client_id="GTBRIDGE", dial_freq=14074000, mode="FT8",
           dx_call="", report="", tx_mode="FT8", tx_enabled=False,
           transmitting=False, decoding=True, rx_df=1500, tx_df=1500,
           de_call="", de_grid="", dx_grid="", tx_watchdog=False,
           sub_mode="", fast_mode=False, special_op=0,
           freq_tolerance=0, tr_period=15, config_name="Default"):
    """Build a Status message (type 1)."""
    buf = _header(1, client_id)
    buf += _encode_quint64(dial_freq)
    buf += _encode_utf8_string(mode)
    buf += _encode_utf8_string(dx_call)
    buf += _encode_utf8_string(report)
    buf += _encode_utf8_string(tx_mode)
    buf += _encode_bool(tx_enabled)
    buf += _encode_bool(transmitting)
    buf += _encode_bool(decoding)
    buf += _encode_quint32(rx_df)
    buf += _encode_quint32(tx_df)
    buf += _encode_utf8_string(de_call)
    buf += _encode_utf8_string(de_grid)
    buf += _encode_utf8_string(dx_grid)
    buf += _encode_bool(tx_watchdog)
    buf += _encode_utf8_string(sub_mode)
    buf += _encode_bool(fast_mode)
    buf += _encode_quint8(special_op)
    buf += _encode_quint32(freq_tolerance)
    buf += _encode_quint32(tr_period)
    buf += _encode_utf8_string(config_name)
    return buf


def decode(client_id="GTBRIDGE", is_new=True, time_ms=0, snr=-10,
           delta_time=0.0, delta_freq=1500, mode="~", message="",
           low_confidence=False, off_air=False):
    """Build a Decode message (type 2).

    Args:
        client_id: WSJT-X instance identifier
        is_new: True for new decode, False for replay
        time_ms: Milliseconds since midnight UTC
        snr: Signal-to-noise ratio in dB
        delta_time: Time offset in seconds (float)
        delta_freq: Audio frequency offset in Hz
        mode: Decode mode character (~ for FT8, + for FT4, etc.)
        message: The decoded message text (e.g. "CQ K1ABC FN42")
        low_confidence: Low confidence flag
        off_air: Off-air (playback) flag
    """
    buf = _header(2, client_id)
    buf += _encode_bool(is_new)
    buf += _encode_quint32(time_ms)
    buf += _encode_qint32(snr)
    buf += _encode_double(delta_time)
    buf += _encode_quint32(delta_freq)
    buf += _encode_utf8_string(mode)
    buf += _encode_utf8_string(message)
    buf += _encode_bool(low_confidence)
    buf += _encode_bool(off_air)
    return buf


def qso_logged(client_id="GTBRIDGE", dx_call="", dx_grid="", freq_hz=0,
               mode="", report_sent="", report_rcvd="",
               tx_power="", comments="", name="",
               date_time_off=None, date_time_on=None,
               operator_call="", my_call="", my_grid="",
               exchange_sent="", exchange_rcvd="", adif_prop_mode=""):
    """Build a QSO Logged message (type 5).

    Sent to GridTracker so it can mark the station as worked.

    Args:
        date_time_off: Tuple (year, month, day, hour, min, sec) or None for now.
        date_time_on: Tuple (year, month, day, hour, min, sec) or None for now.
    """
    now = time.gmtime()
    now_dt = (now.tm_year, now.tm_mon, now.tm_mday,
              now.tm_hour, now.tm_min, now.tm_sec)

    buf = _header(5, client_id)
    buf += _encode_qdatetime(*(date_time_off or now_dt))
    buf += _encode_utf8_string(dx_call)
    buf += _encode_utf8_string(dx_grid)
    buf += _encode_quint64(freq_hz)
    buf += _encode_utf8_string(mode)
    buf += _encode_utf8_string(report_sent)
    buf += _encode_utf8_string(report_rcvd)
    buf += _encode_utf8_string(tx_power)
    buf += _encode_utf8_string(comments)
    buf += _encode_utf8_string(name)
    buf += _encode_qdatetime(*(date_time_on or date_time_off or now_dt))
    buf += _encode_utf8_string(operator_call)
    buf += _encode_utf8_string(my_call)
    buf += _encode_utf8_string(my_grid)
    buf += _encode_utf8_string(exchange_sent)
    buf += _encode_utf8_string(exchange_rcvd)
    buf += _encode_utf8_string(adif_prop_mode)
    return buf


def reply(client_id="GRAYLINE", time_ms=0, snr=-15, delta_time=0.0,
          delta_freq=1500, mode="~", message="GRAYLINE-CLICK",
          low_confidence=False, modifiers=0):
    """Encode a Reply message (type 4).

    Sent back TO a running WSJT-X instance to ask it to tune its decoder
    to a specific signal. WSJT-X matches time_ms + snr + mode + delta_freq
    against its recent decode list to find the signal we mean, and centers
    the cursor on it (auto-reply if enabled in WSJT-X settings).

    For Grayline click-to-tune: pass current_time_ms() and the audio-offset
    Hz in delta_freq. WSJT-X tolerates approximate matches on snr/delta_time.
    """
    body = (_encode_quint32(time_ms)
            + _encode_qint32(snr)
            + _encode_double(delta_time)
            + _encode_quint32(delta_freq)
            + _encode_utf8_string(mode)
            + _encode_utf8_string(message)
            + _encode_bool(low_confidence)
            + _encode_quint8(modifiers))
    return _header(4, client_id) + body


def halt_tx(client_id="GRAYLINE", auto_only=False):
    """Encode a Halt Tx message (type 8).

    auto_only=False halts transmission immediately (like clicking Halt Tx).
    auto_only=True only disables auto-sequencing (like un-checking Enable Tx).
    WSJT-X has no 'enable Tx' message — TX is enabled by a Reply (type 4) that
    answers a decode (see reply())."""
    return _header(8, client_id) + _encode_bool(auto_only)


def configure(client_id="GRAYLINE", mode="", freq_tolerance=0xFFFFFFFF,
              submode="", fast_mode=False, tr_period=0xFFFFFFFF,
              rx_df=0xFFFFFFFF, dx_call="", dx_grid="", generate_messages=True):
    """Encode a Configure message (type 15).

    Unlike reply(), this preloads a DX station into WSJT-X WITHOUT needing a
    local decode: set dx_call + dx_grid and generate_messages=True, and WSJT-X
    stages the call and builds the standard Tx1-Tx6 sequence — ready to pounce
    the moment the signal arrives. Fields left "" or at 0xFFFFFFFF mean "leave
    unchanged," so only DX call/grid + message generation are touched. Needs a
    schema-3 header (Configure didn't exist at schema 2)."""
    body = (_encode_utf8_string(mode)
            + _encode_quint32(freq_tolerance)
            + _encode_utf8_string(submode)
            + _encode_bool(fast_mode)
            + _encode_quint32(tr_period)
            + _encode_quint32(rx_df)
            + _encode_utf8_string(dx_call)
            + _encode_utf8_string(dx_grid)
            + _encode_bool(generate_messages))
    return _header(15, client_id, schema=3) + body


def current_time_ms():
    """Return milliseconds since midnight UTC (for decode time field)."""
    now = time.gmtime()
    return ((now.tm_hour * 3600) + (now.tm_min * 60) + now.tm_sec) * 1000


# ------------------------------------------------------------------ #
#  Decoders                                                            #
# ------------------------------------------------------------------ #

def _decode_utf8_string(data, offset):
    """Decode a length-prefixed UTF-8 string. Returns (string, new_offset)."""
    length = struct.unpack_from('>I', data, offset)[0]
    offset += 4
    if length == 0xFFFFFFFF:
        return None, offset
    s = data[offset:offset + length].decode('utf-8')
    return s, offset + length


def _decode_quint32(data, offset):
    return struct.unpack_from('>I', data, offset)[0], offset + 4


def _decode_qint32(data, offset):
    return struct.unpack_from('>i', data, offset)[0], offset + 4


def _decode_quint8(data, offset):
    return struct.unpack_from('>B', data, offset)[0], offset + 1


def _decode_bool(data, offset):
    return struct.unpack_from('>?', data, offset)[0], offset + 1


def _decode_double(data, offset):
    return struct.unpack_from('>d', data, offset)[0], offset + 8


def _decode_quint64(data, offset):
    return struct.unpack_from('>Q', data, offset)[0], offset + 8


def _decode_qint64(data, offset):
    return struct.unpack_from('>q', data, offset)[0], offset + 8


def _decode_qdatetime(data, offset):
    """Decode QDataStream QDateTime: qint64 jdn + qint32 ms + qint8 spec.
    Returns ((year, month, day, hour, minute, second), new_offset).
    Inverts the formula used by _encode_qdatetime.
    """
    jdn = struct.unpack_from('>q', data, offset)[0]
    offset += 8
    ms_of_day = struct.unpack_from('>i', data, offset)[0]
    offset += 4
    _spec = struct.unpack_from('>b', data, offset)[0]  # 1 = UTC; we don't branch on it
    offset += 1
    # JDN → calendar date (Gregorian, valid from 1 March 4801 BC onward, plenty for ham radio)
    a = jdn + 32044
    b = (4 * a + 3) // 146097
    c = a - (146097 * b) // 4
    d = (4 * c + 3) // 1461
    e = c - (1461 * d) // 4
    mm = (5 * e + 2) // 153
    day = e - (153 * mm + 2) // 5 + 1
    month = mm + 3 - 12 * (mm // 10)
    year = 100 * b + d - 4800 + (mm // 10)
    # ms-of-day → h:m:s
    if ms_of_day < 0:
        ms_of_day = 0
    hour, rem = divmod(ms_of_day, 3600 * 1000)
    minute, rem = divmod(rem, 60 * 1000)
    second = rem // 1000
    return (year, month, day, hour, minute, second), offset


def parse_header(data):
    """Parse the common WSJT-X message header.

    Returns (msg_type, client_id, payload_offset) or None on error.
    """
    if len(data) < 12:
        return None
    magic = struct.unpack_from('>I', data, 0)[0]
    if magic != WSJTX_MAGIC:
        return None
    schema = struct.unpack_from('>I', data, 4)[0]
    msg_type = struct.unpack_from('>I', data, 8)[0]
    client_id, offset = _decode_utf8_string(data, 12)
    return msg_type, client_id, offset


def parse_reply(data):
    """Parse a Reply message (type 4) from GridTracker.

    Returns a dict with: client_id, time_ms, snr, delta_time, delta_freq,
    mode, message, low_confidence, modifiers.  Or None on error.
    """
    hdr = parse_header(data)
    if hdr is None or hdr[0] != 4:
        return None
    _, client_id, off = hdr
    try:
        time_ms, off = _decode_quint32(data, off)
        snr, off = _decode_qint32(data, off)
        delta_time, off = _decode_double(data, off)
        delta_freq, off = _decode_quint32(data, off)
        mode, off = _decode_utf8_string(data, off)
        message, off = _decode_utf8_string(data, off)
        low_confidence, off = _decode_bool(data, off)
        modifiers, off = _decode_quint8(data, off)
    except (struct.error, IndexError):
        return None
    return {
        'client_id': client_id,
        'time_ms': time_ms,
        'snr': snr,
        'delta_time': delta_time,
        'delta_freq': delta_freq,
        'mode': mode,
        'message': message,
        'low_confidence': low_confidence,
        'modifiers': modifiers,
    }


def parse_status(data):
    """Parse a Status message (type 1) broadcast by WSJT-X.

    Returns a dict with the fields present (older WSJT-X versions truncate
    after various points). Always returns at least client_id, dial_freq_hz,
    mode if the magic + header parsed cleanly. None on header error.

    Used by Grayline to track WSJT-X's current dial frequency so click-to-tune
    can compute the audio offset for a given RF frequency.
    """
    hdr = parse_header(data)
    if hdr is None or hdr[0] != 1:
        return None
    _, client_id, off = hdr
    out = {'client_id': client_id}
    try:
        out['dial_freq_hz'], off = _decode_quint64(data, off)
        out['mode'], off = _decode_utf8_string(data, off)
        out['dx_call'], off = _decode_utf8_string(data, off)
        out['report'], off = _decode_utf8_string(data, off)
        out['tx_mode'], off = _decode_utf8_string(data, off)
        out['tx_enabled'], off = _decode_bool(data, off)
        out['transmitting'], off = _decode_bool(data, off)
        out['decoding'], off = _decode_bool(data, off)
        out['rx_df'], off = _decode_qint32(data, off)
        out['tx_df'], off = _decode_qint32(data, off)
        out['de_call'], off = _decode_utf8_string(data, off)
        out['de_grid'], off = _decode_utf8_string(data, off)
        out['dx_grid'], off = _decode_utf8_string(data, off)
        out['tx_watchdog'], off = _decode_bool(data, off)
        out['sub_mode'], off = _decode_utf8_string(data, off)
        out['fast_mode'], off = _decode_bool(data, off)
        out['special_op_mode'], off = _decode_quint8(data, off)
        out['frequency_tolerance'], off = _decode_quint32(data, off)
        out['tr_period'], off = _decode_quint32(data, off)
        out['config_name'], off = _decode_utf8_string(data, off)
        out['tx_message'], off = _decode_utf8_string(data, off)
    except (struct.error, IndexError):
        # WSJT-X versions older than 2.4 truncate after various fields.
        # Return what we got — dial_freq + mode are the load-bearing fields.
        pass
    return out


def parse_decode(data):
    """Parse a Decode message (type 2) broadcast by WSJT-X for each decoded signal.

    Returns a dict with: client_id, is_new, time_ms, snr, delta_time, delta_freq,
    mode (the WSJT-X mode glyph like '~' for FT8), message (the decoded payload
    string like 'CQ N4DWD EM86'), low_confidence, off_air. Or None on error.

    Caller is responsible for parsing the message text into spotted-call + grid
    if it wants to ingest the decode as a spot.
    """
    hdr = parse_header(data)
    if hdr is None or hdr[0] != 2:
        return None
    _, client_id, off = hdr
    try:
        is_new, off = _decode_bool(data, off)
        time_ms, off = _decode_qint32(data, off)
        snr, off = _decode_qint32(data, off)
        delta_time, off = _decode_double(data, off)
        delta_freq, off = _decode_quint32(data, off)
        mode, off = _decode_utf8_string(data, off)
        message, off = _decode_utf8_string(data, off)
        low_confidence, off = _decode_bool(data, off)
        off_air, off = _decode_bool(data, off)
    except (struct.error, IndexError):
        return None
    return {
        'client_id': client_id,
        'is_new': is_new,
        'time_ms': time_ms,
        'snr': snr,
        'delta_time': delta_time,
        'delta_freq': delta_freq,
        'mode': mode,
        'message': message,
        'low_confidence': low_confidence,
        'off_air': off_air,
    }


# Message type constants — see WSJT-X NetworkMessage.hpp
MSG_HEARTBEAT = 0
MSG_STATUS = 1
MSG_DECODE = 2
MSG_CLEAR = 3
MSG_REPLY = 4
MSG_QSO_LOGGED = 5
MSG_CLOSE = 6


def parse_qso_logged(data):
    """Parse a QSO Logged message (type 5) broadcast by WSJT-X when the operator
    clicks 'Log QSO' (or auto-log on RR73 in some setups).

    Returns dict with the load-bearing fields:
      client_id, dx_call, dx_grid, freq_hz, mode, report_sent, report_rcvd,
      tx_power, comments, name, operator_call, my_call, my_grid,
      exchange_sent, exchange_rcvd, adif_prop_mode,
      date_off, time_off, date_on, time_on (each as 'YYYYMMDD' / 'HHMMSS' strings
      ready to drop into ADIF).

    Falls back gracefully on truncated older-version messages — returns whatever
    decoded cleanly up to the failure point.
    """
    hdr = parse_header(data)
    if hdr is None or hdr[0] != 5:
        return None
    _, client_id, off = hdr
    out = {'client_id': client_id}
    try:
        dt_off, off = _decode_qdatetime(data, off)
        out['date_off'] = '{:04d}{:02d}{:02d}'.format(dt_off[0], dt_off[1], dt_off[2])
        out['time_off'] = '{:02d}{:02d}{:02d}'.format(dt_off[3], dt_off[4], dt_off[5])
        out['dx_call'], off = _decode_utf8_string(data, off)
        out['dx_grid'], off = _decode_utf8_string(data, off)
        out['freq_hz'], off = _decode_quint64(data, off)
        out['mode'], off = _decode_utf8_string(data, off)
        out['report_sent'], off = _decode_utf8_string(data, off)
        out['report_rcvd'], off = _decode_utf8_string(data, off)
        out['tx_power'], off = _decode_utf8_string(data, off)
        out['comments'], off = _decode_utf8_string(data, off)
        out['name'], off = _decode_utf8_string(data, off)
        dt_on, off = _decode_qdatetime(data, off)
        out['date_on'] = '{:04d}{:02d}{:02d}'.format(dt_on[0], dt_on[1], dt_on[2])
        out['time_on'] = '{:02d}{:02d}{:02d}'.format(dt_on[3], dt_on[4], dt_on[5])
        out['operator_call'], off = _decode_utf8_string(data, off)
        out['my_call'], off = _decode_utf8_string(data, off)
        out['my_grid'], off = _decode_utf8_string(data, off)
        out['exchange_sent'], off = _decode_utf8_string(data, off)
        out['exchange_rcvd'], off = _decode_utf8_string(data, off)
        out['adif_prop_mode'], off = _decode_utf8_string(data, off)
    except (struct.error, IndexError):
        pass  # truncated — return what we got
    return out


def parse_message(data):
    """Generic dispatcher — peek at message type and route to the appropriate parser.

    Returns (msg_type, parsed_dict_or_None). Unknown message types return
    (msg_type, None) so the caller can log/skip. Bad headers return (None, None).
    """
    hdr = parse_header(data)
    if hdr is None:
        return None, None
    msg_type = hdr[0]
    if msg_type == MSG_STATUS:
        return msg_type, parse_status(data)
    if msg_type == MSG_DECODE:
        return msg_type, parse_decode(data)
    if msg_type == MSG_REPLY:
        return msg_type, parse_reply(data)
    if msg_type == MSG_QSO_LOGGED:
        return msg_type, parse_qso_logged(data)
    return msg_type, None
