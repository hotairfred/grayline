"""
FlexRadio SmartSDR TCP Client

Connects to a FlexRadio 6000 series radio via the SmartSDR TCP API
(port 4992) to monitor slice status and tune slices.  Used by GTBridge
to tune the radio when a spot is clicked in GridTracker's call roster.

Only tunes existing slices that already match the band and mode of the
clicked spot -- never creates or removes slices.

Protocol: line-oriented text over TCP.
  Send:   C<seq>|<command>\\n
  Recv:   R<seq>|<status>|<message>      (command response)
          S<handle>|<object> <id> <k=v>  (async status update)
"""

import asyncio
import logging
from typing import Optional

import dxcluster

log = logging.getLogger(__name__)

# Map GTBridge spot modes to sets of compatible SmartSDR slice modes.
# SmartSDR has no FT8/FT4 mode; digital modes use DIGU/DIGL.
_COMPATIBLE_MODES = {
    'CW':   {'CW'},
    'SSB':  {'USB', 'LSB'},
    'FT8':  {'DIGU', 'DIGL'},
    'FT4':  {'DIGU', 'DIGL'},
    'RTTY': {'DIGU', 'DIGL', 'RTTY'},
    'PSK':  {'DIGU', 'DIGL'},
    'JS8':  {'DIGU', 'DIGL'},
}


def _spot_to_sdr_mode(spot_mode: str, freq_mhz: float) -> str:
    """Map a GTBridge spot mode to a SmartSDR slice mode string."""
    m = (spot_mode or '').upper()
    if m == 'CW':
        return 'CW'
    if m == 'SSB':
        # LSB below 10 MHz, USB at 10 MHz and above; 60m exception (USB)
        if 5.0 <= freq_mhz <= 5.5:
            return 'USB'
        return 'LSB' if freq_mhz < 10.0 else 'USB'
    if m == 'RTTY':
        return 'RTTY'
    if m in ('FT8', 'FT4', 'PSK', 'JS8'):
        return 'DIGU'
    return 'USB'


class FlexRadioClient:
    """Async client for the SmartSDR TCP API (port 4992).

    Subscribes to slice status on connect and keeps ``self.slices``
    up-to-date with every property change the radio reports.
    """

    def __init__(self, host: str, port: int = 4992):
        self.host = host
        self.port = port
        self.slices = {}      # {slice_num: {key: value, ...}}
        self.connected = False
        self._seq = 0
        self._reader = None
        self._writer = None

    # ------------------------------------------------------------------ #
    #  Connection lifecycle                                                #
    # ------------------------------------------------------------------ #

    async def run(self):
        """Connect (with automatic reconnect) and process status updates."""
        retry_delay = 5
        while True:
            try:
                log.info("[Flex] Connecting to %s:%d ...", self.host, self.port)
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), timeout=10)

                # Radio sends two lines on connect: version then handle
                ver = (await asyncio.wait_for(
                    self._reader.readline(), timeout=5)).decode().strip()
                handle = (await asyncio.wait_for(
                    self._reader.readline(), timeout=5)).decode().strip()
                log.info("[Flex] Connected -- %s  handle %s", ver, handle)

                self.slices.clear()
                await self._send("sub slice all")
                self.connected = True
                retry_delay = 5

                await self._read_loop()

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("[Flex] Connection error: %s", e)

            self.connected = False
            self.slices.clear()
            log.info("[Flex] Reconnecting in %ds ...", retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)

        self._close()

    async def _send(self, cmd: str) -> int:
        self._seq += 1
        self._writer.write(f"C{self._seq}|{cmd}\n".encode())
        await self._writer.drain()
        log.debug("[Flex] >>> C%d|%s", self._seq, cmd)
        return self._seq

    async def _read_loop(self):
        while True:
            line = await self._reader.readline()
            if not line:
                log.warning("[Flex] Connection closed by radio.")
                break
            text = line.decode().strip()
            if not text:
                continue
            if text[0] == 'S':
                self._on_status(text)
            elif text[0] == 'R':
                self._on_response(text)

    # ------------------------------------------------------------------ #
    #  Message handlers                                                    #
    # ------------------------------------------------------------------ #

    def _on_response(self, text: str):
        """R<seq>|<hex_status>|<message>"""
        parts = text.split('|', 3)
        if len(parts) >= 2 and parts[1] != '0':
            msg = parts[2] if len(parts) > 2 else ''
            log.warning("[Flex] Command %s error %s: %s",
                        parts[0][1:], parts[1], msg)

    def _on_status(self, text: str):
        """S<handle>|slice <n> key=val ..."""
        pipe = text.find('|')
        if pipe < 0:
            return
        body = text[pipe + 1:]
        tokens = body.split()
        if len(tokens) < 2 or tokens[0] != 'slice':
            return
        try:
            sn = int(tokens[1])
        except ValueError:
            return
        if sn not in self.slices:
            self.slices[sn] = {}
        for tok in tokens[2:]:
            eq = tok.find('=')
            if eq > 0:
                self.slices[sn][tok[:eq]] = tok[eq + 1:]

        info = self.slices[sn]
        if info.get('in_use') == '1':
            log.debug("[Flex] Slice %d (%s): %s MHz  %s",
                      sn, info.get('index_letter', '?'),
                      info.get('RF_frequency', '?'), info.get('mode', '?'))

    def _close(self):
        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None
        self._reader = None
        self.connected = False

    # ------------------------------------------------------------------ #
    #  Slice queries                                                       #
    # ------------------------------------------------------------------ #

    def find_slice(self, band: str, mode: str) -> Optional[int]:
        """Find an in-use slice on *band* whose mode is compatible with *mode*.

        Returns the slice number, or None if no match.
        """
        compat = _COMPATIBLE_MODES.get(mode)
        if not compat:
            return None
        for sn, info in self.slices.items():
            if info.get('in_use') != '1':
                continue
            try:
                freq_mhz = float(info.get('RF_frequency', 0))
            except (ValueError, TypeError):
                continue
            slice_band = dxcluster.freq_to_band(freq_mhz * 1000)  # MHz -> kHz
            if slice_band == band and info.get('mode', '').upper() in compat:
                return sn
        return None

    # ------------------------------------------------------------------ #
    #  Slice control                                                       #
    # ------------------------------------------------------------------ #

    async def tune(self, slice_num: int, freq_mhz: float):
        """Tune *slice_num* to *freq_mhz* MHz."""
        if not self.connected:
            return
        log.info("[Flex] Tune slice %d -> %.6f MHz", slice_num, freq_mhz)
        await self._send(f"slice t {slice_num} {freq_mhz:.6f}")

    async def set_mode(self, slice_num: int, mode: str):
        """Set the mode of *slice_num*."""
        if not self.connected:
            return
        log.info("[Flex] Set slice %d mode -> %s", slice_num, mode)
        await self._send(f"slice set {slice_num} mode={mode}")

    async def tune_to_spot(self, slice_num: int, freq_mhz: float, spot_mode: str):
        """Tune *slice_num* to *freq_mhz* and set the appropriate SmartSDR mode."""
        if not self.connected:
            return
        sdr_mode = _spot_to_sdr_mode(spot_mode, freq_mhz)
        # Check if mode change is needed
        current = self.slices.get(slice_num, {}).get('mode', '').upper()
        if current != sdr_mode.upper():
            await self.set_mode(slice_num, sdr_mode)
        await self.tune(slice_num, freq_mhz)

    # ------------------------------------------------------------------ #
    #  Spot injection                                                      #
    # ------------------------------------------------------------------ #

    async def spot_add(self, callsign: str, freq_mhz: float, mode: str = '',
                       source: str = 'GTBridge', lifetime_seconds: int = 900,
                       comment: str = ''):
        """Add or update a spot on the SmartSDR panadapter/bandmap.

        Duplicate callsign+freq combinations are auto-updated by the radio.
        """
        if not self.connected:
            return
        # Build the command
        cmd = f"spot add rx_freq={freq_mhz:.6f} callsign={callsign}"
        if mode:
            sdr_mode = _spot_to_sdr_mode(mode, freq_mhz).lower()
            cmd += f" mode={sdr_mode}"
        cmd += f" source={source}"
        cmd += f" lifetime_seconds={lifetime_seconds}"
        if comment:
            # Spaces in comment must be encoded as 0x7F (ASCII DEL)
            cmd += f" comment={comment.replace(' ', chr(0x7f))}"
        await self._send(cmd)

    def stop(self):
        self._close()
