# Grayline TX-frequency helper (Windows)

Lets Grayline set WSJT-X's **Tx audio offset** (the "Clear TX" button in Live view)
from outside the app, via Windows UIAutomation — **no WSJT-X rebuild required.**
Runs alongside WSJT-X Improved.

## What it does
Grayline sees every WSJT-X decode's audio offset, so it holds the whole waterfall
as data. On an operator tap, it computes the clearest audio slot (widest empty gap,
or the weakest-SNR occupant to co-exist on) and tells this helper to set WSJT-X's Tx
offset there. **Human-triggered only** — never fired autonomously.

## Install
1. Copy `grayline_txhelper.ps1` and `restart-wsjtx.ps1` into your user folder (`%USERPROFILE%`).
2. In Grayline's `config.json`, set:
   - `txhelper_host` — this PC's LAN IP
   - `txhelper_port` — 2299 (default)
   - `txhelper_token` — must match the token in `grayline_txhelper.ps1`
3. Launch via `restart-wsjtx.ps1` (double-click it, or the desktop shortcut it makes).
   It restarts WSJT-X **and** the helper together, in the same session — run it after
   RDP-ing in, since WSJT-X handles the console→RDP transition poorly.

## How it works
- **`grayline_txhelper.ps1`** — a session-local TCP listener on `:2299`. Protocol:
  `SETTX <hz> <token>` / `GETTX <token>`. Sets `TxFreqSpinBox` via UIAutomation's
  RangeValuePattern (range 200–5000 Hz). **Self-exits when WSJT-X exits**, so no
  stale copy lingers in a dead/old session after an RDP session shift. Must run in
  the interactive session with WSJT-X (UIAutomation is session-isolated).
- **`restart-wsjtx.ps1`** — locates `wsjtx.exe`, stops WSJT-X + any old helper,
  restarts WSJT-X, relaunches the helper — all co-located in the current session.

## Notes
- No helper installed? Grayline still shows the clearest-TX **readout** for you to
  set by hand; the one-tap set just lights up when `txhelper_host` is configured.
- Requires WSJT-X Improved (DG2YCB) — its right-click-set-Tx-offset feature is what
  makes `TxFreqSpinBox` externally settable.
