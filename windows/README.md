# Grayline TX-frequency helper (Windows)

Lets Grayline drive a few WSJT-X actions from outside the app via Windows UIAutomation —
**no WSJT-X rebuild required.** Runs alongside WSJT-X Improved. Two features use it: the
**Clear TX** button (sets the Tx audio offset) and the per-station **Enable TX** button in
the live-view explode-down (WSJT-X has no UDP command to enable Tx, so the helper clicks it).

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
- **`grayline_txhelper.ps1`** — a session-local TCP listener on `:2299`. Token-authed
  line protocol:
  - `SETTX <hz> <token>` / `GETTX <token>` — set/get the Tx audio offset (`TxFreqSpinBox`,
    RangeValuePattern, 200–5000 Hz) — the Clear-TX feature.
  - `ENABLETX <token>` — clicks WSJT-X's **Enable Tx** button. It's a checkable button, so
    a *real mouse click* is used (a programmatic toggle sets the checkbox but doesn't fire
    WSJT-X's handler, so Tx never engages). The cursor is saved/restored.
  - `GENMSGS <token>` — clicks **Generate Std Msgs** (WSJT-X doesn't rebuild Tx1-6 off a
    UDP Configure, so Enable-TX regenerates them here first).
  - `LISTBTN <token>` — dumps all control names/AutomationIds (discovery/debug).

  **Self-exits when WSJT-X exits**; must run in the interactive session with WSJT-X
  (UIAutomation is session-isolated). Control lookups retry — the Qt UIAutomation tree
  is racy and returns partial results intermittently.
- **`restart-wsjtx.ps1`** — locates `wsjtx.exe`, stops WSJT-X + any old helper,
  restarts WSJT-X, relaunches the helper — all co-located in the current session.

## Notes
- No helper installed? Grayline still shows the clearest-TX **readout** for you to
  set by hand; the one-tap set just lights up when `txhelper_host` is configured.
- **The per-station Enable/Halt TX buttons auto-hide unless `txhelper_host` is set**, so
  cloning Grayline without the helper simply shows no TX buttons — nothing to break. To
  turn them off even with the helper installed, set `station_tx_controls_enabled: false`
  in `config.json`.
- Requires WSJT-X Improved (DG2YCB) — its right-click-set-Tx-offset feature is what
  makes `TxFreqSpinBox` externally settable.
