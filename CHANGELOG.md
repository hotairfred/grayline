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
