"""
DX Cluster Telnet Client

Connects to DX cluster servers via TCP, logs in with a callsign,
and parses incoming DX spot lines into structured data.

Typical spot format:
  DX de W3LPL:     14074.0  JA1ABC       FT8 -15dB                1234Z
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# Regex to parse standard DX cluster spot lines
# DX de <spotter>:  <freq>  <dx_call>  <comment>  <time>Z
SPOT_RE = re.compile(
    r'^DX\s+de\s+'
    r'(?P<spotter>[A-Z0-9/\-#@]+):\s*'
    r'(?P<freq>[\d.]+)\s+'
    r'(?P<dx_call>[A-Z0-9/]+)\s+'
    r'(?P<comment>.*?)\s+'
    r'(?P<time>\d{4})Z\s*$',
    re.IGNORECASE
)

# Try to extract mode from the comment field
MODE_PATTERNS = [
    (re.compile(r'\bFT8\b', re.I), 'FT8'),
    (re.compile(r'\bFT4\b', re.I), 'FT4'),
    (re.compile(r'\bCW\b', re.I), 'CW'),
    (re.compile(r'\bSSB\b', re.I), 'SSB'),
    (re.compile(r'\bRTTY\b', re.I), 'RTTY'),
    (re.compile(r'\bPSK\b', re.I), 'PSK'),
    (re.compile(r'\bJS8\b', re.I), 'JS8'),
    (re.compile(r'\bMSK144\b', re.I), 'MSK144'),
    (re.compile(r'\bJT65\b', re.I), 'JT65'),
    (re.compile(r'\bJT9\b', re.I), 'JT9'),
]

# Try to extract SNR from comment (e.g. "-15 dB" or "-15dB")
SNR_RE = re.compile(r'([+-]?\d{1,3})\s*dB', re.I)

# Try to extract grid square from comment
GRID_RE = re.compile(r'\b([A-R]{2}\d{2}(?:[a-x]{2})?)\b')

# Try to extract audio offset from Aggregator FT8 spots (last number in comment, 100-3500 Hz range)
# Format: "-6 dB   6 FT8          2171" — the 2171 is the audio offset
OFFSET_RE = re.compile(r'\b(\d{3,4})\s*$')


@dataclass
class DXSpot:
    """Parsed DX cluster spot."""
    spotter: str
    freq_khz: float
    dx_call: str
    comment: str
    time_utc: str  # "HHMM" format
    mode: Optional[str] = None
    snr: Optional[int] = None
    grid: Optional[str] = None
    audio_offset: int = 0  # Hz offset from dial for FT8/FT4

    @property
    def freq_hz(self) -> int:
        """Frequency in Hz."""
        return int(self.freq_khz * 1000)


def parse_spot(line: str) -> Optional[DXSpot]:
    """Parse a DX cluster spot line. Returns DXSpot or None if not a spot."""
    m = SPOT_RE.match(line.strip())
    if not m:
        return None

    comment = m.group('comment').strip()

    # Extract mode from comment
    mode = None
    for pattern, mode_name in MODE_PATTERNS:
        if pattern.search(comment):
            mode = mode_name
            break

    # Extract SNR from comment
    snr = None
    snr_match = SNR_RE.search(comment)
    if snr_match:
        snr = int(snr_match.group(1))

    # Extract grid from comment
    grid = None
    grid_match = GRID_RE.search(comment)
    if grid_match:
        grid = grid_match.group(1)

    # Extract audio offset from Aggregator FT8 spots (Hz, 100-3500 range)
    freq_khz = float(m.group('freq'))
    audio_offset = 0
    if mode in ('FT8', 'FT4'):
        offset_match = OFFSET_RE.search(comment)
        if offset_match:
            offset_hz = int(offset_match.group(1))
            if 100 <= offset_hz <= 3500:
                audio_offset = offset_hz

    return DXSpot(
        spotter=m.group('spotter').upper(),
        freq_khz=freq_khz,
        dx_call=m.group('dx_call').upper(),
        comment=comment,
        time_utc=m.group('time'),
        mode=mode,
        snr=snr,
        grid=grid,
        audio_offset=audio_offset,
    )


# Standard FT8 dial frequencies (kHz) — signals occupy dial to dial+3 kHz
_FT8_DIAL = [1840, 3573, 5357, 7074, 10136, 14074, 18100, 21074, 24915, 28074, 50313]

# DXpedition F/H FT8 frequencies (kHz) — used by rare DX
# 3Y0K Bouvet: 3567, 7090, 10131, 14090, 18095, 21090, 24911, 28090
# CY0S Sable:  1836, 3567, 5357, 7056, 10131, 14091, 18091, 21091, 24911, 28091
_FT8_DX_DIAL = [1836, 1840, 3567, 5357, 7056, 7090, 10131, 14090, 14091,
                18091, 18095, 21090, 21091, 24911, 28090, 28091]

# Standard FT4 dial frequencies (kHz) — signals occupy dial to dial+3 kHz
_FT4_DIAL = [3575.5, 7047.5, 10140, 14080, 18104, 21140, 24919, 28180, 50318]

# Approximate audio bandwidth for FT8/FT4 (kHz)
_DIGI_BW = 3.0

# Band plan sub-band allocations per ITU region.
# Each entry: (low_khz, high_khz, mode) — only CW and SSB ranges.
# FT8/FT4 windows are checked first (see infer_mode), so band plan
# boundaries don't need to carve around every digital sub-band.
_BAND_PLAN = {
    1: [  # Region 1: Europe, Africa, Middle East (IARU R1)
        # 160m
        (1810, 1838, 'CW'),
        (1843, 2000, 'SSB'),
        # 80m
        (3500, 3570, 'CW'),
        (3580, 3600, 'RTTY'),
        (3600, 3800, 'SSB'),
        # 60m (channelized USB)
        (5330, 5410, 'SSB'),
        # 40m
        (7000, 7040, 'CW'),
        (7040, 7060, 'RTTY'),
        (7060, 7200, 'SSB'),
        # 30m (CW/digital only, no phone)
        (10100, 10130, 'CW'),
        # 20m
        (14000, 14070, 'CW'),
        (14080, 14112, 'RTTY'),
        (14112, 14350, 'SSB'),
        # 17m
        (18068, 18095, 'CW'),
        (18100, 18109, 'RTTY'),
        (18111, 18168, 'SSB'),
        # 15m
        (21000, 21070, 'CW'),
        (21080, 21120, 'RTTY'),
        (21151, 21450, 'SSB'),
        # 12m
        (24890, 24915, 'CW'),
        (24920, 24929, 'RTTY'),
        (24931, 24990, 'SSB'),
        # 10m
        (28000, 28070, 'CW'),
        (28080, 28150, 'RTTY'),
        (28300, 29700, 'SSB'),
        # 6m
        (50000, 50100, 'CW'),
        (50400, 52000, 'SSB'),
    ],
    2: [  # Region 2: Americas (ARRL band plan)
        # 160m
        (1800, 1840, 'CW'),
        (1850, 2000, 'SSB'),
        # 80m
        (3500, 3570, 'CW'),
        (3580, 3600, 'RTTY'),
        (3600, 4000, 'SSB'),
        # 60m (channelized USB)
        (5330, 5410, 'SSB'),
        # 40m
        (7000, 7070, 'CW'),
        (7080, 7125, 'RTTY'),
        (7125, 7300, 'SSB'),
        # 30m (CW/digital only, no phone)
        (10100, 10130, 'CW'),
        # 20m
        (14000, 14070, 'CW'),
        (14080, 14100, 'RTTY'),
        (14150, 14350, 'SSB'),
        # 17m
        (18068, 18100, 'CW'),
        (18100, 18109, 'RTTY'),
        (18110, 18168, 'SSB'),
        # 15m
        (21000, 21070, 'CW'),
        (21080, 21120, 'RTTY'),
        (21150, 21450, 'SSB'),
        # 12m
        (24890, 24920, 'CW'),
        (24920, 24929, 'RTTY'),
        (24930, 24990, 'SSB'),
        # 10m
        (28000, 28070, 'CW'),
        (28080, 28150, 'RTTY'),
        (28300, 29700, 'SSB'),
        # 6m
        (50000, 50100, 'CW'),
        (50400, 54000, 'SSB'),
    ],
    3: [  # Region 3: Asia-Pacific (IARU R3)
        # 160m
        (1800, 1838, 'CW'),
        (1843, 2000, 'SSB'),
        # 80m
        (3500, 3570, 'CW'),
        (3580, 3600, 'RTTY'),
        (3600, 3900, 'SSB'),
        # 60m (channelized USB)
        (5330, 5410, 'SSB'),
        # 40m
        (7000, 7040, 'CW'),
        (7040, 7060, 'RTTY'),
        (7060, 7300, 'SSB'),
        # 30m (CW/digital only, no phone)
        (10100, 10130, 'CW'),
        # 20m
        (14000, 14070, 'CW'),
        (14080, 14112, 'RTTY'),
        (14112, 14350, 'SSB'),
        # 17m
        (18068, 18095, 'CW'),
        (18100, 18109, 'RTTY'),
        (18110, 18168, 'SSB'),
        # 15m
        (21000, 21070, 'CW'),
        (21080, 21120, 'RTTY'),
        (21150, 21450, 'SSB'),
        # 12m
        (24890, 24920, 'CW'),
        (24920, 24929, 'RTTY'),
        (24930, 24990, 'SSB'),
        # 10m
        (28000, 28070, 'CW'),
        (28080, 28150, 'RTTY'),
        (28300, 29700, 'SSB'),
        # 6m
        (50000, 50100, 'CW'),
        (50400, 54000, 'SSB'),
    ],
}


def infer_mode(freq_khz: float, region: int = 2) -> Optional[str]:
    """Infer mode from frequency using standard digital windows + band plan.

    Checks FT8/FT4 windows first (dial to dial+3 kHz), then falls through
    to ITU region band plan for CW/SSB. Frequencies in the gray area
    between CW and SSB sub-bands (e.g. 7070-7125 on 40m) default to SSB
    so untagged spots still pass through the mode filter — the operator
    can identify the actual mode from the spot on their radio.

    Only called when the cluster spot has no mode tag. If the spot already
    specifies a mode, that mode is used as-is.

    Returns 'FT8', 'FT4', 'CW', 'RTTY', 'SSB', or None (not in any amateur band).
    """
    # Check FT4 first (narrower windows, overlaps FT8 on 80m)
    for dial in _FT4_DIAL:
        if dial <= freq_khz <= dial + _DIGI_BW:
            return 'FT4'
    # Check FT8 windows
    for dial in _FT8_DIAL:
        if dial <= freq_khz <= dial + _DIGI_BW:
            return 'FT8'
    # Check DXpedition F/H FT8 windows
    for dial in _FT8_DX_DIAL:
        if dial <= freq_khz <= dial + _DIGI_BW:
            return 'FT8'
    # Fall through to band plan for CW/SSB
    plan = _BAND_PLAN.get(region, _BAND_PLAN[2])
    for low, high, mode in plan:
        if low <= freq_khz <= high:
            return mode
    # Gray area: within an amateur band but between defined CW/SSB
    # sub-bands (e.g. digital segments). Default to SSB.
    if freq_to_band(freq_khz):
        return 'SSB'
    return None


def freq_to_band(freq_khz: float) -> Optional[str]:
    """Map frequency in kHz to amateur band name."""
    bands = [
        (1800, 2000, '160m'),
        (3500, 4000, '80m'),
        (5330, 5410, '60m'),
        (7000, 7300, '40m'),
        (10100, 10150, '30m'),
        (14000, 14350, '20m'),
        (18068, 18168, '17m'),
        (21000, 21450, '15m'),
        (24890, 24990, '12m'),
        (28000, 29700, '10m'),
        (50000, 54000, '6m'),
        (144000, 148000, '2m'),
    ]
    for low, high, name in bands:
        if low <= freq_khz <= high:
            return name
    return None


class DXClusterClient:
    """Async TCP client for DX cluster telnet connections."""

    def __init__(self, host: str, port: int, callsign: str,
                 on_spot=None, name: str = None, login_commands=None):
        """
        Args:
            host: Cluster server hostname
            port: Cluster server port (typically 7300 or 8000)
            callsign: Your amateur callsign for login
            on_spot: Async callback called with (DXSpot, cluster_name) for each spot
            name: Friendly name for this cluster connection
            login_commands: List of commands to send after login (e.g. filters)
        """
        self.host = host
        self.port = port
        self.callsign = callsign.upper()
        self.on_spot = on_spot
        self.name = name or f"{host}:{port}"
        self.login_commands = login_commands or []
        self._reader = None
        self._writer = None
        self._running = False

    async def connect(self):
        """Connect to the cluster and begin reading spots."""
        self._running = True
        retry_delay = 5

        while self._running:
            try:
                log.info("[%s] Connecting to %s:%d...", self.name, self.host, self.port)
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port),
                    timeout=30
                )
                log.info("[%s] Connected.", self.name)
                retry_delay = 5  # reset on successful connect

                await self._login()
                await self._read_loop()

            except asyncio.CancelledError:
                log.info("[%s] Connection cancelled.", self.name)
                break
            except Exception as e:
                log.warning("[%s] Connection error: %s", self.name, e)

            if self._running:
                log.info("[%s] Reconnecting in %ds...", self.name, retry_delay)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 120)

        self._close()

    async def _login(self):
        """Wait for login prompt and send callsign."""
        # Read initial banner/prompt, then send callsign
        # Clusters typically prompt with "login:" or "call:" or "Please enter your call:"
        login_sent = False
        deadline = asyncio.get_event_loop().time() + 15  # 15s timeout for login

        while asyncio.get_event_loop().time() < deadline:
            try:
                data = await asyncio.wait_for(self._reader.read(4096), timeout=5)
            except asyncio.TimeoutError:
                if not login_sent:
                    # Some clusters don't prompt, just send callsign
                    break
                continue

            if not data:
                raise ConnectionError("Connection closed during login")

            text = data.decode('latin-1', errors='replace')
            log.debug("[%s] <<< %s", self.name, text.strip())

            # Check for login prompt
            lower = text.lower()
            if any(kw in lower for kw in ['login', 'call', 'your call', 'enter']):
                self._writer.write((self.callsign + '\r\n').encode())
                await self._writer.drain()
                log.info("[%s] Sent callsign: %s", self.name, self.callsign)
                login_sent = True
                # Read a bit more to clear the post-login banner
                await asyncio.sleep(1)
                await self._send_startup_commands()
                return

        # If no prompt was detected, try sending callsign anyway
        if not login_sent:
            self._writer.write((self.callsign + '\r\n').encode())
            await self._writer.drain()
            log.info("[%s] Sent callsign (no prompt detected): %s", self.name, self.callsign)
            await asyncio.sleep(1)
        await self._send_startup_commands()

    async def _send_startup_commands(self):
        """Send post-login commands (filters, sh/dx) to cluster."""
        for cmd in self.login_commands:
            self._writer.write((cmd + '\r\n').encode())
            await self._writer.drain()
            log.info("[%s] Sent: %s", self.name, cmd)
            await asyncio.sleep(0.5)
        self._writer.write(b'sh/dx\r\n')
        await self._writer.drain()
        log.info("[%s] Sent sh/dx", self.name)

    async def _read_loop(self):
        """Read lines from the cluster and parse spots."""
        buffer = ''
        while self._running:
            try:
                data = await asyncio.wait_for(self._reader.read(4096), timeout=120)
            except asyncio.TimeoutError:
                # Send a keepalive
                try:
                    self._writer.write(b'\r\n')
                    await self._writer.drain()
                except Exception:
                    break
                continue

            if not data:
                log.warning("[%s] Connection closed by server.", self.name)
                break

            buffer += data.decode('latin-1', errors='replace')
            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                line = line.strip()
                if not line:
                    continue

                log.debug("[%s] %s", self.name, line)

                # Strip ANSI escape codes (some clusters send color codes)
                clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', line)
                # Strip other control characters
                clean = ''.join(c for c in clean if c >= ' ' or c == '\t')
                clean = clean.strip()

                spot = parse_spot(clean)
                if spot:
                    log.debug("[%s] PARSED: %s on %.1f", self.name, spot.dx_call, spot.freq_khz)
                elif clean.startswith('DX de') or clean.startswith('DX De') or clean.startswith('DX DE'):
                    log.warning("[%s] UNPARSED DX line: %r", self.name, clean)

                if spot and self.on_spot:
                    try:
                        await self.on_spot(spot, self.name)
                    except Exception as e:
                        log.error("[%s] Spot callback error: %s", self.name, e)

    def _close(self):
        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None
        self._reader = None

    def stop(self):
        """Signal the client to stop."""
        self._running = False
        self._close()
