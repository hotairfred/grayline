# Changelog

All notable changes to Grayline are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project
uses incremental commits rather than versioned releases for now, so
everything lives under `[Unreleased]` until the first tagged release.

## [Unreleased]

### Added

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
