"""
Telnet Server for DX Spot Re-broadcast

Provides a standard DX cluster telnet interface so that Ham Radio Deluxe
(or any DX cluster client) can connect and receive spots in real time.

This is output-only — the server does not accept DX spot commands from
clients. It emulates a DX Spider node and supports VE7CC CC11 spot
format (which HRD requests via set/ve7cc).

Standard spot format:
  DX de W3LPL:    14074.0  JA1ABC       FT8 -15dB CQ            1234Z

CC11 spot format (VE7CC):
  CC11^14074.0^JA1ABC^11-Feb-2026^1234Z^FT8 -15dB CQ^W3LPL^FN20^^0^
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict

log = logging.getLogger(__name__)


class TelnetServer:
    """Async TCP server that re-broadcasts DX spots to connected clients."""

    def __init__(self, host: str = '0.0.0.0', port: int = 7300,
                 node_call: str = 'GTB-2'):
        self.host = host
        self.port = port
        self.node_call = node_call
        # writer -> {'ve7cc': bool}
        self._clients: Dict[asyncio.StreamWriter, dict] = {}
        self._server = None

    async def start(self):
        """Start listening for connections."""
        import socket
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port,
            reuse_address=True,
        )
        log.info("Telnet server listening on %s:%d (node %s)",
                 self.host, self.port, self.node_call)

    async def stop(self):
        """Shut down the server and disconnect all clients."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for writer in list(self._clients):
            try:
                writer.close()
            except Exception:
                pass
        self._clients.clear()
        log.info("Telnet server stopped.")

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter):
        """Handle a new client connection with DX Spider-style login."""
        peer = writer.get_extra_info('peername')
        addr = f"{peer[0]}:{peer[1]}" if peer else "unknown"
        log.info("Telnet client connected: %s", addr)
        node = self.node_call

        try:
            # DX Spider login prompt
            writer.write(b"login: Please enter your call: ")
            await writer.drain()

            # Read callsign (with timeout)
            try:
                data = await asyncio.wait_for(reader.readline(), timeout=60)
            except asyncio.TimeoutError:
                writer.write(b"Timeout. Goodbye.\r\n")
                await writer.drain()
                writer.close()
                log.info("Telnet client timed out during login: %s", addr)
                return

            callsign = data.decode('latin-1', errors='replace').strip()
            if not callsign:
                callsign = "UNKNOWN"

            # DX Spider-style welcome
            writer.write(
                f"Hello {callsign}, this is {node} running DX Spider\r\n"
                f"{callsign} de {node} >\r\n".encode()
            )
            await writer.drain()
            log.info("Telnet client logged in: %s (%s)", callsign, addr)

            # Register client — starts in standard (non-ve7cc) mode
            self._clients[writer] = {'ve7cc': False}

            # Prompt format — default DX Spider, may be changed by set/prompt
            prompt = f"{callsign} de {node} >\r\n"

            # Read loop — handle commands from client
            try:
                while True:
                    data = await reader.readline()
                    if not data:
                        break
                    cmd = data.decode('latin-1', errors='replace').strip()
                    if not cmd:
                        continue
                    log.info("Telnet [%s] cmd: %s", addr, cmd)

                    parts = cmd.split(None, 1)
                    verb = parts[0].lower() if parts else ''

                    try:
                        # echo — HRD uses these as state machine markers
                        if verb == 'echo' and len(parts) > 1:
                            writer.write((parts[1] + "\r\n" + prompt).encode())

                        # set/prompt — change prompt format
                        elif verb == 'set/prompt' and len(parts) > 1:
                            fmt = parts[1]
                            prompt = fmt.replace('%M', node) + "\r\n"
                            writer.write(prompt.encode())

                        # set/ve7cc — enable CC cluster spot format
                        elif verb == 'set/ve7cc':
                            self._clients[writer]['ve7cc'] = True
                            writer.write(
                                (f"VE7CC gateway mode enabled\r\n" + prompt).encode()
                            )
                            log.info("Telnet [%s] VE7CC mode enabled", addr)

                        # sh/ commands — no history, just prompt
                        elif verb.startswith('sh/'):
                            writer.write(prompt.encode())

                        # Everything else — acknowledge with prompt
                        else:
                            writer.write(prompt.encode())

                        await writer.drain()
                    except Exception:
                        break
            except (asyncio.CancelledError, ConnectionError):
                pass

        except (ConnectionError, OSError) as e:
            log.debug("Telnet client error during login: %s (%s)", addr, e)
        finally:
            self._clients.pop(writer, None)
            try:
                writer.close()
            except Exception:
                pass
            log.info("Telnet client disconnected: %s", addr)

    def broadcast_spot(self, spot) -> None:
        """Format a DXSpot and send it to all connected clients.

        Sends CC11 format to VE7CC clients, standard format to others.
        Non-async — schedules writes without blocking the caller.
        """
        if not self._clients:
            return

        std_data = None
        cc_data = None

        dead = []
        for writer, state in self._clients.items():
            try:
                if state.get('ve7cc'):
                    if cc_data is None:
                        cc_data = (format_cc11_line(spot) + "\a\r\n").encode()
                    writer.write(cc_data)
                else:
                    if std_data is None:
                        std_data = (format_spot_line(spot) + "\a\r\n").encode()
                    writer.write(std_data)
            except Exception:
                dead.append(writer)

        for writer in dead:
            self._clients.pop(writer, None)
            try:
                writer.close()
            except Exception:
                pass


def format_spot_line(spot) -> str:
    """Format a DXSpot into standard DX Spider spot line."""
    spotter = (spot.spotter + ':')[:8]
    dx_call = spot.dx_call[:12]
    comment = (spot.comment or '')[:28]
    time_utc = spot.time_utc

    return f"DX de {spotter:<8s} {spot.freq_khz:10.1f}  {dx_call:<12s} {comment:<28s}{time_utc}Z"


def format_cc11_line(spot) -> str:
    """Format a DXSpot as VE7CC CC11 spot line.

    CC11 format: CC11^freq^dx_call^date^timeZ^comment^spotter^grid^origin^flag^
    """
    now = datetime.now(timezone.utc)
    date_str = now.strftime('%d-%b-%Y')
    freq = f"{spot.freq_khz:.1f}"
    time_utc = spot.time_utc + 'Z'
    comment = spot.comment or ''
    spotter = spot.spotter
    grid = spot.grid or ''

    return f"CC11^{freq}^{spot.dx_call}^{date_str}^{time_utc}^{comment}^{spotter}^{grid}^^0^"
