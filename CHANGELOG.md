# Changelog

All notable changes to Grayline are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project
uses incremental commits rather than versioned releases for now, so
everything lives under `[Unreleased]` until the first tagged release.

## [Unreleased]

### Added

- **Peer-spots — local-peer PSKReporter receptions become Live-view spots.**
  When a station within `peer_radius_mi` hears a DX (via the PSKReporter MQTT
  firehose), Grayline now synthesizes a spot with the *closest* peer as the
  spotter (source `PEER`, priority above external cluster feeds, below your own
  WSJT-X). It slots into the local-spotter view like any other spot — so a
  needed grid your neighbors are hearing stays visible *between* the 3-minute
  FT8 cluster blinks (proactive, not reactive), and gracefully falls back to a
  live cluster spot the moment the peer stops hearing it. Reuses the existing
  source-precedence dedup, so `PEER` beats a far cluster re-spot for display but
  never overwrites your own decode. Toggle: `peer_spots_enabled` (default on).
- **One-command install (`install.sh` / `run.sh`).** `install.sh` finds a
  Python 3.8+ interpreter, builds an isolated venv, installs the lone
  dependency (paho-mqtt) *into it*, and seeds config.json / secrets.json
  from the examples; `run.sh` launches the server with the venv's
  interpreter. Kills the "which pip fed which python" / "No module named
  paho" mismatch (and Homebrew's externally-managed-pip block on macOS)
  that trips fresh crew installs. Verified end-to-end on a clean checkout.
- **FFMA 488-grid wall map** (`/ffma_map`). Every FFMA grid drawn
  geographically as a CONUS lattice — green confirmed, amber
  worked-pending, red needed (brightness scaled by rarity). Status is
  read from the **LoTW mirror only** (never QRZ flags), so it reflects
  award truth rather than optimistic logbook flags. Hover or tap a
  pending cell for its per-op re-work detail: every path worked there
  tagged hot / ghost / dead, plus a confirm-prediction (likely /
  possible / needs a fresh re-work).
- **Per-band award scope routing.** HF spots route to DXCC×band
  (ARRL DXCC Challenge / 5BDXCC). 6m routes to BOTH DXCC×band AND
  grid×band (FFMA + 6m DXCC are both meaningful awards). 2m and up
  route to grid×band only (VUCC). UI cell-position becomes
  semantic — orange in Country cell = DXCC need, orange in Grid
  cell = grid need.
- **Flex Phase 2 panadapter inject.** Cached spots queue to
  `spot_add` at 2/sec with 5-min dedup. Skips FT8/FT4 (WSJT-X
  handles those natively via DAX). Rate stays orders of magnitude
  below the saturation point that competed with SmartSDR audio
  DPCs in the GTBridge era.
- **Drop-unverifiable-spotter ingest gate.** Spots whose spotter
  has no resolvable QRZ grid get dropped at ingest. Junk like
  `FG1G/4/30` spotting EU on 2m disappears cleanly while real
  spotters still pass once their QRZ lookup resolves.
- **Microwave bands in `BAND_ORDER`.** 3cm through 70cm now have
  tab slots so spots arriving during contests / activations
  surface in the UI.
- **GridTracker 2 mode classification tables.** `data/modes.json`
  and `data/modes-phone.json` ported verbatim from GT2 (BSD
  3-Clause). 122 ADIF modes classified into Digital / Phone /
  CW / Other via the same lookup tables GT has used in production
  for years. See `NOTICE` for full attribution.
- **`mode_class(mode)` classifier.** Python port of GT2's
  `getTypeFromMode()` (gtCommon.js) — returns `CW`, `Phone`,
  `Digital`, or `Other` from any ADIF mode string.
- **`wpx_prefix(call)`.** Python port of GT2's `getWpx()` —
  WPX prefix derivation matching CQ-WPX rules. Returns `None`
  for portable callsigns (`/X` suffix) or callsigns without
  digits, mirroring GT's behavior.
- **`compute_score_summary(logbook_path)` cross-check oracle.**
  Multi-axis QSO roll-up matching GT's `renderStatsBox()`
  output shape: `DXCC | GRID | CQ | ITU | WAS | WAC | WPX | USC`
  plus `Mixed/CW/Phone/Digital/Other` mode-type rollup. Used to
  verify our incremental `WorkedState` sets agree with GT's
  full-pass algorithm. Live logbook (8,768 QSOs) reports
  `agree=True`.
- **`country_band_modeclass_status(country, band, modeclass)`
  data model.** WorkedState now tracks per-DXCC-mode-class
  (CW / Phone / Digital / Other) status alongside the existing
  per-band and per-literal-mode statuses. Powers DXCC-Mixed /
  DXCC-CW / DXCC-Phone / DXCC-Digital scope routing as ARRL
  actually issues those awards. `Mixed` is a pseudo-class that
  routes to `country_band_status` (any mode counts).
- **`dxcc_band_modeclass_status` field on cached spots.** Each
  spot record now carries the per-class status alongside the
  existing four status fields. The JSON feed exposes it ready
  for UI surfacing in later stages. `modeclass` derived from
  `mode_class(spot.mode)` is stashed alongside.
- **Award column in the spot table.** Each row now shows a
  short pill list — `DXCC` (Mixed status), `DXCC-CW`/`DXCC-Phone`/
  `DXCC-Digital` when the class status differs from Mixed, and
  `Grid` for VHF+ (FFMA on 6m, VUCC on 2m+). Pills color-code
  by status: orange-fill for needed, orange-outline for
  worked-not-confirmed, dim for confirmed. Live cluster confirms
  the differentiation works — e.g., 17m Peru CW shows
  `DXCC: confirmed` + `DXCC-CW: new`.
- **Per-band tab counter shows `wanted/total`.** Each band tab's
  count splits into a red "wanted" prefix (spots where any
  applicable scope is `new`) and a neutral total. Tabs with
  zero wanted spots fall back to a plain count.
- **Per-band award scope settings panel.** New "Award scopes per
  band" section in the gear dropdown — one row per band, columns
  for DXCC-Mixed / DXCC-CW / DXCC-Phone / DXCC-Digital / VUCC.
  Cells are blank where the scope doesn't apply (DXCC on 2m,
  VUCC on 80m, etc.). Defaults follow the ARRL-tracked-only
  principle: DXCC-Mixed on for HF + 6m, VUCC on for 6m + VHF/UHF.
  Settings persist via localStorage; a Reset-to-defaults button
  is available.
- **`Show wanted only` toggle replaces `needed_only` + `mode_aware`.**
  Single checkbox; filters spots where any *enabled* scope is
  currently `new`. The mode-aware toggle's behavior is now
  expressed as "enable DXCC-CW / DXCC-Phone / DXCC-Digital
  on the bands you care about."
- **Country and Grid cell highlighting now scope-driven.** The
  orange DXCC and Grid cell fills/outlines reflect the *weakest
  enabled* DXCC-family or Grid-family scope status — so if you
  enable both DXCC-Mixed and DXCC-CW on 17m and a CW spot from
  Peru shows up where you've worked Peru-Mixed but not Peru-CW,
  the Country cell goes orange (most-needed scope wins).
- **Award pills only show on relevant spots.** A 17m FT8 spot
  no longer shows a `DXCC-CW` pill even if you have DXCC-CW
  enabled — working FT8 doesn't earn DXCC-CW credit, so the
  scope is irrelevant to that spot. CW spots show DXCC-CW; SSB
  shows DXCC-Phone; FT8/RTTY/etc. show DXCC-Digital. Mixed-mode
  scope (DXCC-Mixed) shows on every spot since any mode counts.
- **Pre-seeded mode list in gear-panel settings.** The Modes
  section now starts with 17 common modes pre-loaded
  (CW / SSB / USB / LSB / AM / FM / FT8 / FT4 / RTTY / PSK31 /
  JS8 / MSK144 / JT65 / JT9 / Q65 / FST4 / WSPR) so users can
  configure visibility (e.g., uncheck WSPR or SSTV) before the
  first spot of that mode arrives. Rare modes still appear
  dynamically as their traffic lands. Modes already in
  `disabledModes` always remain visible so they can be
  re-enabled after their spots have aged out.
- **FFMA award scope, CONUS-48 grid filter wired up.** The
  Fred Fish Memorial Award is now a proper scope: 488 canonical
  grid squares from `data/ffma_grids.json` (sourced from the
  ARRL FFMA program page, attribution in NOTICE). FFMA scope is
  6m-only and only emits a pill on CONUS-48 grids — a 6m EU or
  Hawaiian grid spot won't show FFMA even if FFMA is enabled,
  because those grids don't count toward the award. Default-on
  for 6m alongside DXCC-Mixed and VUCC.
- **Award scope storage now uses explicit true/false.** Previously
  unchecked scopes were deleted from localStorage; now they're
  stored as `false`. This lets future scope additions fill in
  defaults for genuinely-new scopes without re-enabling things
  the user explicitly turned off. Existing settings are migrated
  on first load — any scope not in storage gets its default
  state.
- **WSJT-X UDP integration.** Listens for WSJT-X broadcasts
  (heartbeat / status / decode / QSO-logged) and ingests decodes
  as local-source spots (`WSJTX-LOCAL`, highest precedence). FT8
  message text is parsed to the transmitting call + grid. Forwards
  to additional WSJT-X targets when configured.
- **Click-to-tune.** Click a spot to tune the rig there — to a
  running WSJT-X instance via a `Reply` message (matches the audio
  offset), or to a Flex slice via the SmartSDR API.
- **Bidirectional WSJT-X UDP hub.** Grayline already mirrors WSJT-X
  broadcasts to `wsjtx_forward_targets` (e.g. GridTracker); it now also
  relays the *reverse* direction — a client→WSJT-X control packet
  (click-to-tune `Reply`, Halt Tx, free text, location, callsign
  highlight) arriving from a downstream consumer is forwarded **up** to
  WSJT-X. So you can run GridTracker for contesting (its click-to-tune
  works) while Grayline stays the always-on receiver flagging FFMA/award
  priorities — `WSJT-X ⇄ Grayline ⇄ {GridTracker, …}`. Packets are routed
  by message type/direction (not source address, so it's correct even if
  WSJT-X and the consumer share a host) and broadcast to all known
  instances (the matching Id acts, others ignore — no Id parsing needed).
- **N1MM / SDC contest integration.** Live QSO logging over the
  N1MM UDP protocol, with delete/replace sync (contactdelete /
  contactreplace) and own-skimmer spot suppression during contests.
- **LoTW + logbook-upload integration.** Hourly LoTW confirmation
  fetch merges into worked-state; optional real-time upload of each
  logged QSO to QRZ/ClubLog/eQSL/LoTW. Manual "Log Sync" buttons
  (QRZ + LoTW) in settings.
- **DX-cluster telnet re-broadcast feed.** Grayline re-serves its
  filtered, distance-aware stream as a telnet node (with `sh/dx`
  backlog) so SDC / other clients can consume it.
- **Full ARRL log-derivable award set.** 5BWAS, NBDXCC (5BDXCC base
  + WARC/160/6m endorsements → 8BDXCC / 10BDXCC), DXCC Honor Roll,
  per-mode WAS `(state × mode-class)`, Triple Play (LoTW-only, all
  50 states × CW+Phone+Digital), and WAC (6 continents). Built on
  **confirmation-source tracking** — LoTW vs paper card vs eQSL are
  distinguished per QSO, so ARRL awards count LoTW/card only (not
  eQSL), Triple Play counts LoTW only, and CQ awards count all three.
- **WAJA (JARL Worked All Japan Prefectures) tracker.** 47-prefecture
  award as an opt-in Scores row, keyed on the ADIF `STATE` code
  (= WAJA reference number; ADIF scheme, Tokyo=10, not ISO 13), with
  LoTW carrying the prefecture so worked→confirmed closes
  automatically. Includes a **kanji prefecture grid** drill-in
  (green = confirmed, amber = worked, dim = needed; hover for romaji).
- **WAJA prefecture spot pill.** Flags needed JA prefectures live in
  the spot roster. The prefecture is resolved best-effort from the
  station's QRZ `addr2`, **validated against the JA call-area digit**
  (declines a mailing-address mismatch — e.g. a JE6/Kyushu op whose
  address reads "Tokyo" — and resolves JA8 = Hokkaido with certainty
  when the address names nothing). Silent when unsure; advisory only.
  Hover shows the English prefecture name. Off by default (JARL
  opt-in) — enable in Scores setup.
- **Scores-setup award toggle panel.** Per-award on/off in a
  collapsible Scores-setup panel. ARRL awards default-on; CQ (WAZ)
  and JARL (WAJA) are opt-in (off). Persists to localStorage.
- **Per-mode spot lifetime (TTL).** Digital (FT8/FT4) spots expire
  on a much shorter clock than CW/Phone — a stale FT8 decode just
  tunes you to a dead frequency. Configurable: `spot_ttl_sec`
  (default 600) and `spot_ttl_digital_sec` (default 180).
- **Clone-and-run configuration.** Operator settings extracted to
  `config.json` (+ `secrets.json`), with committed `.example`
  templates. Defaults point at a public cluster and the lenient
  spotter gate, so a fresh clone runs with just a callsign set.
- **Theory of Operation doc** (`docs/theory-of-operation.md`) —
  the local-spotter thesis, tiered radius, nearest-spotter-wins,
  the strict/lenient spotter gate, confirmation-source rules, and
  why Grayline pairs well with running your own GoCluster.

### Changed

- **Flat-table layout, mode-as-column.** Drops the per-mode
  sub-tables that grouped spots into FT8/FT4/CW chunks within
  each band tab. Now every band shows one flat table with `Mode`
  as a column. Sort order: freq within band. Cleaner read at a
  glance and reduces visual fragmentation when a band has many
  modes spotted.
- **"All" pseudo-tab as the leftmost tab.** Click it to see every
  band's spots in one combined table sorted by band then freq,
  with both `Band` and `Mode` columns. Useful for low-activity
  periods where you have ~13 wanted spots scattered across 20
  bands and want the overview without clicking through each tab.
  Click any band tab to drill back down.
- **Mode-toggle row branches on view.** In single-band view, the
  toggles drive the existing per-band-mode disable map (fast
  triage of a hot band's modes). In All view, toggling a mode
  drives the global `disabledModes` set — one place to silence
  WSPR or SSTV across all bands at once.
- **First-time-default tab is now All.** Fresh users land on the
  combined view rather than a single band, surfacing the new
  layout naturally.
- **Mode DXCC awards are now entity-level and self-retiring.**
  DXCC-CW / DXCC-Phone / DXCC-Digital are entity-count awards (any
  band), not band×mode — so the pills stop nagging you to re-work
  an entity on a mode you already have it on. Each auto-retires
  (stops highlighting) at 100 confirmed, re-armable for
  endorsements. The Country cell reflects band-slot status only.
- **Nearest-spotter-wins distance.** A spot's stored distance is the
  nearest spotter ever seen for it, sticky for its lifetime — a
  later report from a farther spotter refreshes recency but doesn't
  push the spot back outside your local-spotter radius. Fixes DX
  spots (e.g. JA) flickering in and out of view as near and far
  spotters alternately reported them.
- **Triple Play shown as legs-complete / 3.** Reframed from a raw
  QSO count to "legs done out of 3" (CW / Phone / Digital, all 50
  states each) — it's an award-for-having-awards, like 5BDXCC.
- **Log view auto-refreshes**, and the worked-state reload runs more
  frequently (mtime-gated, so idle ticks stay cheap) — logged QSOs
  flip award status live instead of after a long roundtrip.
- **Relicensed to BSD-3-Clause** (from GPL-3.0), matching the
  GridTracker family, ahead of the public release.

### Fixed

- **Grid confirmations recorded in `VUCC_GRIDS` are now credited.** A rover or
  grid-line station's LoTW confirmation can list two or more grids in
  `VUCC_GRIDS` (e.g. `DM76XR,DM86AR`) while `GRIDSQUARE` holds only one — so the
  other grid (DM76) was never counted toward FFMA/VUCC even though LoTW confirms
  it. `worked_state.py` now parses `VUCC_GRIDS` and credits every 4-char prefix,
  same as `GRIDSQUARE`. (Reported by Mitch N8XS with a full root-cause + repro.)
- **`/api/scores` and `/api/ffma_map` FFMA counts now agree.** Scores re-derived
  the confirmed set by re-iterating logged QSOs (keyed on each QSO's GRIDSQUARE),
  which could disagree with the map's direct read of `confirmed_grid_band` by a
  grid (e.g. a VUCC_GRIDS confirmation). Both endpoints now read the same
  authoritative grid-band sets, so they can't diverge.
- **Re-work engine surfaces fresh ops on ANY unconfirmed grid.** A grid
  with a HOT (recently-worked, likely-to-self-confirm) path used to
  suppress the re-work flag on *other* ops in it, hiding clean backup
  paths behind a single — possibly flaky — pending contact. Now every
  unconfirmed FFMA grid flags fresh ops as a path, so backups surface
  until the grid actually confirms; self-confirming HOT ops stay
  unflagged (no nagging to re-work something already in the pipeline).
- **Radar keeps a station on its bearing through gridless decodes.** A
  freq-independent per-call last-known-grid cache means FT8 traffic that
  carries no grid (signal reports, RR73) no longer drops a station off
  the PPI scope — it holds its last bearing instead of flickering on and
  off.
- **Downstream click-to-tune now actually reaches WSJT-X.** The bidirectional
  hub forwarded decodes from a throwaway ephemeral socket, so a downstream
  consumer (GridTracker) saw them arriving from `:<random>` and sent its
  click-to-tune `Reply` back *there* — a port nothing reads — so the relay never
  caught it. Decodes are now forwarded **from the `:2237` listener socket**, so
  the consumer replies to `:2237` where the relay picks it up and forwards it
  upstream. Verified end-to-end: GridTracker click-to-tune tunes the rig through
  Grayline. (Also confirmed the type-based relay discrimination is correct even
  when WSJT-X and the consumer share a host — they do here.)
- **Spots no longer purge while still being heard.** When a
  higher-priority source (local WSJT-X) owned a cache entry, lower-
  priority cluster re-spots weren't refreshing its timestamp, so it
  could age out on the local source's last-decode time even while
  the cluster kept spotting it. The short digital TTL exposed this.
- **Stale local-slice provenance after a band change.** A
  `WSJTX-LOCAL` decode is only "local" while a WSJT-X instance is
  still on that band. When a slice retuned (e.g. SliceB 17m → 20m),
  its old-band decodes were being kept alive by cluster re-spots and
  kept their `SliceB` local label — so a slice on 20m appeared to be
  "producing" 17m spots. Such entries are now demoted to the cluster
  source when re-spotted (same signal, correct band, honest spotter)
  instead of masquerading as a live local decode.
- **Frozen frequency / stale click-to-tune on a zombie local spot.**
  Generalizes the above. A high-priority (local WSJT-X) cache entry is
  only authoritative while its *own* source keeps updating it. If the
  station moved or faded locally but the cluster kept re-spotting it,
  the entry's timestamp was being refreshed (looked live) while its
  freq, audio offset, and click-to-tune match data stayed frozen at the
  last real local decode — so it pointed to a stale frequency. Now a
  local entry not refreshed by its own source within
  `LOCAL_SPOT_FRESH_SEC` (~4 FT8 cycles), or whose slice has left the
  band, hands off to the next cluster spot (live freq, honest source)
  rather than persisting as a frozen zombie.
- **Spot grids no longer blank mid-QSO.** FT8 carries a grid only in
  the CQ/grid-reply; mid-QSO messages have none, and a later decode
  would wipe the grid already deduced. Grid now persists (never
  regresses to blank).
- **QSO-logged grid backfill from own decode.** A logged QSO missing
  its grid is backfilled from our own decode history, never from QRZ
  (correct for portables — QRZ would give the home grid, not the
  operating one).
- **Scores panel per-band DXCC under-count.** `dxcc_by_band` now
  counts from the LoTW-mirror-merged sets (the same sets the spot
  coloring uses), matching LoTW to ±1 (the +1s being legitimate
  paper-card credits LoTW's online total doesn't show).
- **WAS Alaska/Hawaii capture.** State capture was dropping Hawaii
  and most of Alaska and miscounting DC; now gated correctly on the
  50 US states across DXCC 291/6/110.
- **False "needed" flags.** Worked-state is keyed by cty.dat entity
  rather than the source's country-name label, so QRZ/cty.dat label
  drift can't make a worked entity look needed.
- **N1MM contactdelete** removes ADIF records by `<EOR>`, not by
  line, so multi-line records delete cleanly.

### Vendor / reference

- **DXLab Suite manuals** (`docs/dxlab/DXKeeper.pdf`,
  `docs/dxlab/SpotCollector.pdf`) — local copies for award-tracking
  reference. Gitignored (re-downloadable from ambersoft.com).
- **GridTracker 2 source clone** (`vendor/gridtracker2/`) —
  reference for the per-band award scope work. Gitignored
  (re-cloneable from gitlab.com/gridtracker.org/gridtracker2).
- **`NOTICE`** — third-party attributions. Currently covers GT2's
  BSD 3-Clause license for the mode tables and algorithm ports.

### Notes

This file establishes the changelog scaffold mid-project. Earlier
work (the gtbridge → Grayline split, the initial UI build,
cty.dat enrichment, drop-unverifiable rules, etc.) is captured
in commit history rather than backfilled here. Going forward,
each commit adds an entry to the appropriate `[Unreleased]`
subsection and rolls into the next release tag when one is cut.
