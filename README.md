# Grayline

A web-based DX cluster consumer with award-aware worked/needed lookup, built for
the serious DXer who wants finer per-band/per-award granularity than a general
spotting tool provides. It runs headless (a container, a Pi, any always-on Linux
box), serves a self-contained dark-mode web page to any browser on your LAN or
over Tailscale, and keeps the heavy lifting — cluster ingestion, WSJT-X
handling, and FlexRadio control — off your operating PC.

Grayline pulls validated spots from a DX cluster, enriches them with
DXCC / continent / zone data via `cty.dat`, cross-references them against your
own logbook (QRZ Logbook + LoTW + a local WSJT-X/N1MM feed) for
worked-and-confirmed status, and renders a GridTracker-style call roster plus a
full **award scoreboard** (DXCC and its variants, WAS, VUCC, FFMA, and more).

> Built for one operator's shack first, then generalized. It pairs naturally
> with **[GoCluster](https://github.com/N2WQ/GoCluster)** as the upstream
> validator, but speaks standard DX-cluster telnet and will point at any cluster.

**New here?** Read **[docs/theory-of-operation.md](docs/theory-of-operation.md)**
— it explains the design: why distance is measured from the *spotter* not the
DX, why QRZ lookups are what make that work, the strict/lenient spotter gate, and
how confirmation-source tracking keeps each award counting by its own rules.

## Features

- **Award scoreboard** — DXCC (Mixed/CW/Phone/Digital/Satellite), DXCC
  Challenge, per-band DXCC + the N-band milestones (5BDXCC → 8BDXCC → 10BDXCC),
  DXCC Honor Roll standing, WAS + 5BWAS + per-mode WAS, **Triple Play**, **WAC**,
  **FFMA**, **VUCC** — plus CQ **WAZ** and JARL **WAJA** (with a kanji prefecture
  grid). Each award toggles on/off in a "Scores setup" panel (ARRL on by
  default; CQ/JARL opt-in).
- **Confirmation-source aware** — distinguishes LoTW / card / eQSL per QSO, so
  award counts respect each program's rules (e.g. Triple Play is LoTW-only;
  ARRL awards don't accept eQSL).
- **Per-band award scopes** — HF chases DXCC×band; 6m chases DXCC *and* grids
  (FFMA); 2m+ chases grids (VUCC). Spots highlight against the awards that
  actually matter on that band.
- **Local-spotter filter** — distance is measured from each *spotter* to your
  QTH, not to the DX. A nearby station hearing it means *you* probably can too
  — the right propagation signal, especially on 6m (tiered radius: HF vs VHF+).
- **Live logging integration** — listens to WSJT-X and N1MM/SDC UDP, flips
  worked/needed in real time, writes a local ADIF, and (optionally) fan-out
  uploads to QRZ / ClubLog / eQSL / LoTW.
- **FlexRadio integration** (optional) — slice tracking, panadapter spot
  inject, click-to-tune. Click-to-tune routes a WSJT-X Reply to the instance
  whose passband actually contains the signal, so it works correctly with two
  slices on one band (e.g. SliceA + SliceB both on 6m).
- **Rotor control + signal radar** (optional) — a phone-friendly `/rotor`
  page: manual beam aim and one-tap **click-to-aim** great-circle bearings
  (Hamlib `rotctld`), true-north calibration for a slipped indicator, and a
  **PPI radar scope** that plots live signals on the compass by bearing and
  distance — red for needed, brightness for SNR, pile-ups clustered, your beam
  heading drawn as a sweep wedge, and out-of-range openings pinned to the rim
  as chevrons. Filter to what *you* can actually hear (mine / local / wanted)
  and scale the range from a tight 6m Es ring out to worldwide.
- **FFMA grid map** (optional) — a `/ffma_map` wall map of all 488 FFMA
  grids laid out geographically: green confirmed, amber worked-pending, red
  needed (brighter red = rarer). Status is read from your **LoTW mirror**, not
  QRZ flags, so it reflects award truth. Hover or tap a pending grid for the
  per-op re-work detail — every path worked there tagged hot / ghost / dead,
  plus a confirm-prediction.
- **Re-broadcast** (optional) — serves its filtered, annotated spots back out
  as a standard DX-Spider telnet node for other logging tools.

## Quick start

**Requirements:** an always-on Linux host — a Raspberry Pi is plenty — plus
Python 3 and a browser. It lives *off* your operating PC by design (that's the
point: keep the UDP/compute load away from the machine running your radio audio).

```bash
git clone <your-fork-url> grayline
cd grayline

# 1. Operator settings (callsign, grid, cluster host, which features are on)
cp config.json.example config.json
$EDITOR config.json          # set "callsign" and "home_grid" at minimum

# 2. API credentials (only needed for the services you enable)
cp secrets.json.example secrets.json
$EDITOR secrets.json         # QRZ / LoTW / etc.

# 3. Run
python3 grayline_server.py
```

Then open `http://<host>:8080/` (the `http_port` from `config.json`) from any
browser on your network. Stdlib-only — no `pip install` required for the core
server.

### What you'll see on first run

Out of the box the example config points at a public cluster (`ve7cc.net:23`)
and uses the lenient spotter gate, so **just set your `callsign` and run** —
you'll get a live, populated roster immediately (a worldwide all-band feed you
filter by band/mode in the UI). Everything credential- or hardware-dependent
(Flex, the telnet feed, logbook uploads, LoTW fetch) defaults **off**.

To unlock the rest, in order of payoff:

1. **`home_grid`** — anchors the spotter-distance ("local spotters") filter.
2. **QRZ credentials** (`secrets.json`) — **effectively required for the spotting
   features to be worth anything.** Resolving spotter callsigns → grids is what
   powers the local-spotter distance filter (the whole point of Grayline), and
   *only QRZ does that*. Without a QRZ XML subscription you're left with a plain
   band/mode firehose — no distance awareness, no "who near me hears this." Also
   backfills DX grids for grid awards and pulls worked/needed status. Get this
   first if you intend to use the spotting side at all.
3. **`lotw_fetch_enabled` + LoTW creds** — confirmations for the award
   scoreboard, the FFMA grid map, and the re-work engine. Your **first**
   fetch pulls your entire LoTW history (a full sync); after that it's a
   quick incremental download of new confirmations. Run it once up front so
   the awards have something to score.
4. **`require_spotter_grid: true`** — once the QRZ cache is warm, for the clean
   verified-spotter feed.

## Configuration

`config.json` (gitignored; copy from `config.json.example`) holds your
operator-specific settings:

| key | meaning |
|---|---|
| `callsign` | your callsign — identifies your own spots/decodes as "local" |
| `home_grid` | 6-char Maidenhead locator — anchors the spotter-distance filter |
| `gocluster_host` / `gocluster_port` | the DX cluster to consume (defaults to the public `ve7cc.net:23`) |
| `login_commands` | commands sent after login (default none). Put GoCluster/DXSpider filter dialect here for those servers |
| `require_spotter_grid` | strict spotter gate — drop spots whose spotter can't be placed. Default `false` (see below) |
| `http_port` | web UI port (default 8080) |
| `spot_ttl_sec` | how long a CW/Phone/other spot stays before purge (default 600) |
| `spot_ttl_digital_sec` | how long a digital (FT8/FT4) spot stays — shorter, since stale decodes are dead frequencies (default 180) |
| `flex_enabled` / `flex_host` / `flex_inject_enabled` | FlexRadio 6000-series TCP API |
| `telnet_feed_enabled` / `telnet_feed_port` | re-broadcast as a DX-Spider node |
| `wsjtx_enabled` / `wsjtx_forward_targets` | WSJT-X UDP listen + optional mirror to e.g. GridTracker |
| `n1mm_enabled` | listen for N1MM/SDC `<contactinfo>` UDP |
| `logbook_upload_enabled` | live QRZ/ClubLog/eQSL/LoTW upload on each logged QSO |
| `lotw_fetch_enabled` | periodic incremental LoTW confirmation download |

`secrets.json` (gitignored; copy from `secrets.json.example`) holds API
credentials. Only the keys for the services you enable are required; missing
credentials simply skip that service.

### Why QRZ credentials matter (the local-spotter filter)

Grayline's signature feature measures distance from each **spotter** to *your*
QTH — a nearby station hearing a signal means you probably can too. Computing
that needs the spotter's grid, which Grayline resolves from its callsign via the
**QRZ XML API** (cached on disk). **Without QRZ credentials that resolution
can't happen, so the local-spotter distance filter is unavailable** — you'd
filter by band and mode only, on the full firehose.

That's also why `require_spotter_grid` defaults to **`false`**: on a fresh
install (no QRZ creds yet, cold cache) the strict gate would drop *every* spot
it can't place and you'd see an empty page. Lenient keeps spots with unknown
distance so the roster populates immediately. Once you've added QRZ credentials
and the cache has warmed, flip it to `true` for the cleaner, verified-spotter
feed. See [docs/theory-of-operation.md](docs/theory-of-operation.md) for the
full rationale.

### Club Log integration (optional — bring your own account)

Grayline can talk to [Club Log](https://clublog.org) for three things: real-time
QSO upload, the Most Wanted **rarity badge** on spots, and the **Most Wanted tab**
(your worked/confirmed-by-band-and-mode matrix plus an OQRS "confirmable" list and
"suspect / not-in-their-log" flags). All of it reads *your* Club Log account, so
it's per-operator — set these in `secrets.json`:

| key | what it is / where to get it |
|---|---|
| `clublog_email` | the email you log into Club Log with |
| `clublog_password` | an **Application Password** — Club Log → Settings → App Passwords (**not** your login password) |
| `clublog_callsign` | your callsign (defaults to `qrz_user` if blank) |
| `clublog_api_key` | **request your own** at [clublog.org/requestapikey.php](https://clublog.org/requestapikey.php) — free, but hand-reviewed, so allow ~a day. Describe it as e.g. *"Grayline — personal ham dashboard, realtime upload + DXCC charts."* |

Leave any of these blank to disable the Club Log features — Grayline runs fine
without them. (The Most Wanted **rarity ranking** on spots needs *no* credentials
at all — it uses Club Log's public list.)

> Club Log issues API keys per-application, so you request your own rather than
> sharing one — it keeps your traffic on your own account, and it's a one-time
> setup. On any credentials error Grayline latches the Club Log calls off (Club
> Log firewalls IPs on repeated 403s), so a wrong key fails safe.

## Architecture

```
[skimmer] --raw--> [DX cluster] --validated--> [Grayline] --HTTP--> [browser]
                                                   |
                  WSJT-X / N1MM UDP ----------->   +--> worked/needed + award engine
                                                   +--> cty.dat enrichment
                                                   +--> QRZ XML grid lookup
                                                   +--> LoTW incremental fetch
                                                   +--> Flex TCP 4992 (optional)
                                                   +--> DX-Spider telnet feed (optional)
```

Grayline consumes any standard DX cluster, but it's designed to sit on top of a
local aggregator ([**GoCluster**](https://github.com/N2WQ/GoCluster)) that merges
RBN + PSKReporter + your own skimmer into one validated stream. That's optional — a public node works fine — but it's
a real upgrade for digital ops (PSKReporter is only reachable through an
aggregator). See *Running your own cluster* in
[theory-of-operation.md](docs/theory-of-operation.md#7-running-your-own-cluster-and-why-gocluster-pairs-well).

## Files

- `grayline_server.py` — HTTP server, cluster client, award engine, UDP listeners
- `worked_state.py` — in-memory worked/confirmed lookup over your merged log
- `lotw_fetch.py` — incremental LoTW confirmation download
- `qrz_logbook_fetch.py` — QRZ Logbook API fetcher
- `logbook_uploads.py` — QRZ / ClubLog / eQSL / LoTW (TQSL) upload fan-out
- `dxcluster.py`, `flexradio.py`, `telnet_server.py`, `ctydat.py`, `qrz.py` — supporting libs
- `cty.dat` — AD1C DXCC country file (bundled; updates at <https://www.country-files.com/>)
- `data/` — FFMA grid list, mode classification tables
- `config.json.example`, `secrets.json.example` — copy these to get started

## Development

Grayline is developed primarily with [Claude Code](https://claude.com/claude-code)
as the agentic coding tool. Contributions and forks welcome.

## License

Copyright (c) 2026 Fred Krause (WF8Z).

Grayline is licensed under the **BSD 3-Clause License** — see [LICENSE](LICENSE).
It builds on BSD-3-Clause material from GridTracker 2 and uses AD1C's `cty.dat`;
those attributions and their license terms are in [NOTICE](NOTICE).
