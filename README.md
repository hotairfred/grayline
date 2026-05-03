# Grayline

Web-based DX cluster consumer with worked/needed lookup for the WF8Z shack.

Pulls validated spots from a local **GoCluster** instance, enriches them with
DXCC / continent / CQ-zone data via cty.dat, cross-references against the
operator's QRZ Logbook for worked-and-confirmed status, and serves a
GridTracker-style call roster as a self-contained web page over the LAN.

## What it does

- Connects to GoCluster (`192.168.1.103:8300` by default) with operator-tuned
  cluster filters (`PASS NEARBY OFF`, `PASS CONFIDENCE V P S C`, `PASS MODE
  FT8 FT4`, `PASS SOURCE ALL`, `PASS DECONT NA`).
- Maintains an in-memory spot cache with a 10-minute TTL.
- Resolves spotter callsigns to grids via QRZ XML (active background lookup
  worker that writes back to the shared `qrz_cache.json`).
- Computes the **spotter-to-home** distance for each spot — the proper
  *"propagation reachability"* metric (if a nearby skimmer hears it, you
  probably can too) rather than DX-distance.
- Tags each spot with **call-status** (new / worked / confirmed) and
  **DXCC-band-status** (and optionally DXCC-band-mode-status for
  award-specific filtering).
- Serves an auto-refreshing dark-mode HTML roster with per-band tabs,
  per-band-mode toggles, settings gear for global band/mode visibility,
  needed-only filter, mode-aware filter, 300mi spotter-distance filter,
  and persistent localStorage state.

## Phase 1 Flex integration

Connects to the FlexRadio 6000-series radio's TCP API (`192.168.1.238:4992`),
subscribes to slice updates, and exposes the current radio state at
`/active_bands`. Phase 2 (band-filtered panadapter spot inject) and Phase 3
(click-to-tune the active slice to a spot's frequency) are planned but not
yet wired.

## Architecture

```
[skimmer1] --raw spots--> [GoCluster] --validated--> [Grayline] --HTTP--> [browser]
                                                          |
                                                          +--> [QRZ XML lookup]
                                                          +--> [cty.dat enrichment]
                                                          +--> [worked-state lookup]
                                                          +--> [Flex TCP 4992 (slice tracking)]
```

## Files

- `grayline_server.py` — main HTTP + cluster client + Flex client
- `worked_state.py` — in-memory worked/confirmed lookup against `qrz_logbook.json`
- `qrz_logbook_fetch.py` — one-shot fetcher for the QRZ Logbook API
- `dxcluster.py`, `flexradio.py`, `ctydat.py`, `qrz.py` — supporting libs
  (copied from gtbridge; this project owns its versions from here)
- `cty.dat` — AD1C DXCC country file
- `secrets.json` (symlink) — shared with gtbridge; contains QRZ creds
- `qrz_cache.json` (symlink) — shared callsign-to-grid cache; both projects feed it
- `qrz_logbook.json`, `qrz_logbook.adi` — your QRZ Logbook in JSON + ADIF form

## Running

```
cd /home/fred/grayline
python3 grayline_server.py
```

Then open `http://192.168.1.101:8080/` from anywhere on the LAN.

## Why a separate project from gtbridge

Different consumer model, different architecture, different roadmap. gtbridge
is the *cluster → UDP → GridTracker* path for non-Flex users on a remote
machine. Grayline is the *cluster → web UI + worked-state + Flex integration*
path for the Flex shack.

The shared state (`secrets.json`, `qrz_cache.json`) is symlinked between the
two projects to maintain a single source of truth.

The shared libraries (`dxcluster.py`, `flexradio.py`, `ctydat.py`, `qrz.py`)
are copied — each project owns its evolution from here. Backporting fixes
across the two is a manual but cheap operation.
