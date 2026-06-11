# Grayline Award Tracking Specification

**Status:** design spec — this is the research pass that *drove* the award-engine
build, not a description of current state. The gaps called out in §6 (the
LoTW / card / eQSL confirmation-source distinction, Triple Play, per-mode WAS,
the WAS Alaska/Hawaii fix, 5BWAS, WAC, the N-band DXCC milestones) were
**subsequently implemented**. Read this for the *per-award grain rationale*
(why each award counts what it counts); for current behavior see the README and
[theory-of-operation.md](theory-of-operation.md).
**Last updated:** 2026-06-07 (spec); implemented 2026-06.
**Author:** Grayline (research pass against official ARRL / CQ / IARU / RSGB rule sources)

---

## 1. Purpose and motivating bug

Grayline ingests a logbook plus a live DX-spot stream and, for each spot, shows whether the
operator has worked / confirmed the spotted entity, grid, state, zone, etc. on the award scopes
that apply to that band. The product is intended for public release, so the tracking grain has
to be exactly right: **track precisely what each award counts — no more (over-tracking nags the
user for QSOs no award requires) and no less (under-tracking silently misses award progress).**

The motivating defect: Grayline tracked the ARRL mode DXCCs (CW / Phone / Digital) at the grain
`(entity, band, mode)`. The real ARRL mode DXCCs are **entity-count, ANY band** — grain
`(entity, mode)`. The `(entity, band, mode)` grain corresponds to **no ARRL award at all**; it is
a personal completionist goal. Grayline therefore highlighted spots and showed "needed" pills for
band/mode slots no award rewards. This spec exists to lock down the correct grain for every award.

A second class of the same error is the inverse: tracking `(entity, band)` mixed-mode as if it
were an award when in fact **single-band DXCC, 5BDXCC and DXCC Challenge all consume that grain** —
so that one IS load-bearing. The lesson is per-award: derive the grain from the rule, not from
intuition.

---

## 2. Current Grayline tracking model (Step 1 findings)

Read from `worked_state.py` (`WorkedState.__init__` / `reload`) and `grayline_server.py`
(`_build_scores_payload`, `scopeStatus`, `availableScopesForBand` / `defaultScopesForBand`).

### 2.1 Tracking sets maintained today

| Set (worked_ / confirmed_) | Indexed key | Notes |
|---|---|---|
| `*_calls` | `call` | ever worked; drives call_status pills |
| `*_dxcc` | `dxcc_id` | entity any band/mode |
| `*_dxcc_band` | `(dxcc_id, band)` | DXCC-ID keyed |
| `*_country_band` | `(country, band)` | entity-name keyed (cty.dat canonical) |
| `*_country_band_mode` | `(country, band, mode)` | raw ADIF mode (FT8, RTTY…) |
| `*_country_band_modeclass` | `(country, band, class)` | class ∈ CW/Phone/Digital/Other |
| `*_country_modeclass` | `(country, class)` | **collapsed from band_modeclass** — mode-DXCC grain |
| `*_dxcc_modeclass` | `(dxcc_id, class)` | authoritative mode-DXCC counts |
| `*_grid_band` | `(grid4, band)` | VUCC / FFMA |
| `*_states` | `state` | WAS (DXCC 291 only) |
| `*_state_band` | `(state, band)` | 5BWAS / band WAS |
| `*_cq_zones` | `cq_zone` | WAZ |
| `*_satellite_grids` | `grid4` (PROP_MODE=SAT) | Satellite VUCC |
| `*_dxcc_satellite` | `dxcc_id` (PROP_MODE=SAT) | Satellite DXCC |

Plus the `compute_score_summary` cross-check oracle, which additionally bins **ITU zone**,
**WAC continent**, **WPX prefix**, and **US county (USC)** — but these are oracle-only; they are
**not** surfaced as award scopes in `_build_scores_payload` or `scopeStatus`.

### 2.2 What is surfaced to the user (scopes)

- `availableScopesForBand`: `DXCC-Mixed` (all DXCC bands), `FFMA` (6m only), `VUCC` (grid bands).
- Global entity-level mode DXCCs `DXCC-CW / DXCC-Phone / DXCC-Digital` via `MODE_SCOPE_OF`,
  gated on `< 100 confirmed` (auto-retire), using `dxcc_modeclass_status` (entity-level — correct).
- `_build_scores_payload` reports: DXCC by modeclass (Mixed/CW/Phone/Digital + Satellite),
  DXCC Challenge (sum of `(dxcc,band)` over 160–6m), per-band DXCC counts, WAS + WAS-by-band,
  WAZ, VUCC per band, Satellite VUCC, FFMA.

### 2.3 Current scope/band constants

- `DXCC_BANDS` = 160,80,**60**,40,30,20,17,15,12,10,6 m
- `DXCC_CHALLENGE_BANDS` = 160,80,40,30,20,17,15,12,10,6 m (no 60m — correct)
- `GRID_BANDS` / `VUCC_BANDS` = 6,2,1.25m,70,33,23,13,9,6,3 cm (+1.25cm in VUCC_BANDS)
- `WAS_TARGET`=50, `WAZ_TARGET`=40, `MODE_DXCC_TARGET`=100

---

## 3. ARRL core awards (per-award specification)

Threshold/endorsement and QSL columns are from the official ARRL rules pages cited in §7.
**ARRL QSL rule (applies to all ARRL/IARU awards below unless noted):** only **paper QSL cards**
sent to ARRL **or LoTW** confirmations are accepted. **eQSL is NOT accepted** by ARRL (LoTW is the
only electronic path). This is a gotcha — see §6.

### 3.1 DXCC family

| Award | Unit | Band-split | Mode | Threshold / endorsements | Date floor | Deleted entities | Tracking key |
|---|---|---|---|---|---|---|---|
| **DXCC Mixed** | DXCC entity | any band | any mode | 100, then +50 to 250, +25 to 300, +5 above | 1945-11-15 | **count** | `(entity)` any band/mode = `worked_dxcc` |
| **DXCC Phone** | entity | any band | radiotelephone (USB/LSB/AM/FM/SSTV) | 100 + same ladder | 1945-11-15 | count | `(entity, Phone)` any band |
| **DXCC CW** | entity | any band | CW only | 100 + same ladder | **1975-01-01** (earlier CW counts as Mixed) | count | `(entity, CW)` any band |
| **DXCC Digital** | entity | any band | machine-readable digital (FT8/FT4/RTTY/PSK/JT65…); digital-voice excluded (→Phone) | 100 + same ladder | 1945-11-15 | count | `(entity, Digital)` any band |
| **Single-band DXCC** (160,80,40,30,20,17,15,12,10,6,2m,70,23,13,3cm) | entity | **per band** | any mode on that band | 100 per band, +25 to 200, +10 to 250, +5 above | 1945-11-15 | count | `(entity, band)` |
| **DXCC Satellite** | entity | n/a (sat) | any via satellite; QSO must indicate satellite | 100, +25/+10/+5 ladder | 1965-03-01 | count | `(entity)` where PROP_MODE=SAT |
| **5-Band DXCC (5BDXCC)** | entity | 80/40/20/15/10 **required**; 160/30/17/12/6/2 endorsable | any mode | 100 entities on EACH of the 5 bands | 1945-11-15 | **do NOT count** (current only) | `(entity, band)` over the 5 bands |
| **DXCC Challenge** | **band-entity slot** | 160,80,40,30,20,17,15,12,10,6m | any mode | **1000** band-entity slots min, plaque +500 increments; band with <100 still counts | 1945-11-15 | **do NOT count** | `(entity, band)` over 160–6m |
| **DXCC Honor Roll** | entity | any band | per the four modes | top 10% of current list (e.g. ≥331 of 340) | per mode floor | **do NOT count** | `(entity)` confirmed, current-list only |
| **#1 Honor Roll** | entity | any band | per mode | ALL current entities (e.g. 340/340) | per mode floor | do NOT count | `(entity)` confirmed = full current list |

Notes / gotchas:
- **Cross-mode**: Phone/CW cross-mode confirmations are only allowed if dated **1981-09-30 or
  earlier**. Modern logs are single-mode, so for practical tracking each QSO contributes only its
  own mode class.
- **Honor Roll / #1** are **current-list, confirmed-only** measures. They are derived from the
  same `(entity, mode)` confirmed sets used for the mode DXCCs — they need a **current-DXCC-list
  membership filter** (deleted entities excluded) and the live "current entity count" (≈340) to
  compute the top-10% threshold. Grayline does not currently model the current-list filter.
- **Challenge** and **single-band/5BDXCC** all consume the **`(entity, band)`** grain — this grain
  is real and load-bearing (unlike `(entity, band, mode)`).
- **Satellite DXCC does not count toward any other DXCC award** and vice versa; PROP_MODE=SAT
  QSOs must be excluded from per-band/Challenge counts (Grayline already does this).

### 3.2 WAS family

ARRL WAS — counted unit = **50 US states** (DC counts as Maryland). 60m / 5MHz excluded from all
endorsements. Repeater / ship / aircraft contacts prohibited.

| Award | Band-split | Mode | Threshold | QSL methods | Tracking key |
|---|---|---|---|---|---|
| **WAS (Basic/Mixed)** | any band | any mode | 50 states | paper **or** LoTW | `(state)` |
| **WAS Phone / CW / Digital / RTTY** | any band | the named mode/class | 50 states each | paper or LoTW | `(state, class)` |
| **WAS band endorsements** (each band, excl. 60m) | per band | any | 50 per band | paper or LoTW | `(state, band)` |
| **5-Band WAS (5BWAS)** | 80/40/20/15/10 required | any | 50 on each of 5 bands | paper or LoTW | `(state, band)` over 5 bands |
| **Triple Play WAS** | any band (excl. 60m) | 50 on Phone AND 50 on CW AND 50 on Digital | all 150 slots | **LoTW ONLY — paper not accepted** | `(state, class)` for class∈{Phone,CW,Digital} |
| **WAS Satellite** | satellite | any | 50 via satellite | paper or LoTW | `(state)` where PROP_MODE=SAT |
| **WAS EME** | n/a | any via moonbounce | 50 via EME | paper or LoTW | `(state)` where PROP_MODE=EME |
| **Band-specific** (2190m, 630m, 160m, 50/144/222/432MHz, 70/33/23/13/3cm, SSTV) | per band/mode | per band | 50 | paper or LoTW | subsumed by `(state, band)` |

Gotchas:
- **Triple Play is LoTW-only.** A paper-confirmed state does NOT count for Triple Play even though
  it counts for ordinary WAS. Grayline's current confirmation model OR's LoTW+paper+eQSL into one
  "confirmed" flag, so it **cannot** distinguish Triple Play eligibility from generic confirmed.
- WAS is **DXCC entity 291 only** (Grayline filters on this — correct; excludes Alaska/Hawaii? No —
  AK/HI are DXCC 6/110 but are valid WAS states. See §6 gotcha: the current `dxcc=="291"` filter
  **drops Alaska and Hawaii**, which are separate DXCC entities but ARE WAS states).

### 3.3 VUCC, FFMA, WAC

| Award | Unit | Bands | Mode | Initial / endorsement | QSL | Tracking key |
|---|---|---|---|---|---|---|
| **VUCC 50MHz / 144MHz** | grid square | 6m / 2m | any | **100** init, +25 increments | paper or LoTW | `(grid4, band)` |
| **VUCC 222 / 432 MHz** ("Half Century") | grid | 1.25m / 70cm | any | **50** init, +10 | paper or LoTW | `(grid4, band)` |
| **VUCC 902 / 1296 MHz** ("Quarter Century") | grid | 33cm / 23cm | any | **25** init, +5 | paper or LoTW | `(grid4, band)` |
| **VUCC higher microwave** (2.3GHz+ / 13cm and up) | grid | 13/9/6/3cm… | any | typically 5–10 init (band-specific) | paper or LoTW | `(grid4, band)` |
| **VUCC Satellite** | grid | satellite | any | **100** init, +25 | paper or LoTW | `(grid4)` where PROP_MODE=SAT |
| **FFMA** | grid | **6m only** | any | **all 488** CONUS-48 grids — all-or-nothing, no endorsements | paper or LoTW | `(grid4, 6m)` ∩ 488-grid set |
| **WAC (basic)** | continent (6) | any | any | all 6: NA, SA, EU, AS, AF, OC | paper or LoTW (IARU) | `(continent)` |
| **WAC band endorsements** (excl. 60m) | continent | per band | any | 6 per band | paper or LoTW | `(continent, band)` |
| **WAC mode endorsements** (CW, Phone, Image/SSTV, RTTY/Digital, FT8) | continent | any | per mode | 6 per mode | paper or LoTW | `(continent, class)` |
| **5-Band WAC (5BWAC)** | continent | 80/40/20/15/10 only — **WARC (30/17/12) and satellite VOID** | any (band-only, no mode endorse) | 6 on each of 5 bands | paper or LoTW | `(continent, band)` over 5 bands |
| **6-Band WAC** | continent | 5BWAC + one more | any | 6 on 6 bands | paper or LoTW | `(continent, band)` |
| **WAC QRP endorsement** | continent | any | any, ≤5W out | on/after 1985-01-01 | paper or LoTW | needs power filter |

Notes:
- **VUCC Rule 6 / FFMA 200km circle**: all QSOs submitted for a single VUCC/FFMA award must be
  made from within the same 200 km (2°×4° area) circle. Grayline tracks grids globally per
  operator; for a single-location operator this is a non-issue, but the rule means VUCC is
  **per-location**, not strictly per-operator. Flag for multi-QTH users.
- **FFMA date floor 1983-01-01**; counts only the **488 CONUS-48 grids** (Grayline already gates
  on `_FFMA_GRID_SET`).
- **WAC continent = 6** (Antarctica is NOT a WAC continent). Grayline's cty.dat continent map may
  emit "AN" for Antarctica — must be excluded/ignored for WAC, or mapped appropriately.

---

## 4. CQ / IARU / RSGB optional extensions

**Clearly non-ARRL.** Researched with the same rigor; mark as optional in the product. WAZ is
already shipped by Grayline, so treat it as near-core.

| Award | Sponsor | Unit | Band-split | Mode | Threshold | QSL methods | Tracking key |
|---|---|---|---|---|---|---|---|
| **WAZ (basic)** | CQ | CQ zone (1–40) | any | any | 40 zones | **LoTW, paper, or CQ-approved electronic** | `(cq_zone)` |
| **WAZ CW / SSB / Phone / RTTY / Digital** | CQ | zone | any | per mode (same emission both ways) | 40 each | LoTW/paper/CQ-elec | `(cq_zone, class)` |
| **WAZ band endorsements** | CQ | zone | per band | any | 40 per band | LoTW/paper | `(cq_zone, band)` |
| **5-Band WAZ (5BWAZ)** | CQ | zone | 80/40/20/15/10 | **mixed mode only** (any combination) | 40 on each (200 total); prereq = hold any 40-zone WAZ | LoTW/paper | `(cq_zone, band)` over 5 bands |
| **CQ WPX Mixed** | CQ | WPX prefix | any | CW+SSB+Digital | **400** prefixes, +50 increments | **LoTW, eQSL (CQ interface), paper, printout** | `(prefix)` |
| **CQ WPX SSB / CW / Digital** | CQ | prefix | any | per mode | **300** each, +50 | LoTW/eQSL/paper | `(prefix, class)` |
| **CQ WPX band endorsements** | CQ | prefix | per band | any | per band | LoTW/eQSL/paper | `(prefix, band)` |
| **CQ DX Award** | CQ | DXCC entity (CQ's list) | any | SSB/CW/RTTY/Digital/Mixed certs | 100 entities | LoTW/paper | `(entity)` per mode class — near-identical to DXCC |
| **CQ DX Marathon** | CQ | entity + CQ zone, **per calendar year** | 160–6m incl. 60m + WARC | any | annual score (work-once per yr) | annual log submission | `(entity, year)` + `(cq_zone, year)` — **year-scoped, different grain** |
| **IOTA** | RSGB | **island-group reference** (e.g. NA-065) | any | any | 100 island groups (then 750, 1000…) | IOTA's own system / checkpoints | `(iota_ref)` — **needs island-reference data, not in ADIF DXCC** |

Gotchas:
- **WAZ accepts CQ-approved electronic verifications and LoTW**; this differs from ARRL (no eQSL).
- **WPX accepts eQSL** (via the CQ eQSL interface) — one of the few awards where eQSL is valid.
- **CQ DX Marathon is calendar-year-scoped and work-once-per-year** — a fundamentally different
  grain (`(unit, year)`); it is a contest-style annual chase, not a lifetime award. Probably out
  of scope for a lifetime-award dashboard; flag explicitly.
- **IOTA counts island references**, not DXCC entities or grids. ADIF carries an `IOTA` field
  (`IOTA` / `IOTA_ISLAND_ID`) but most logs don't populate it; requires the IOTA directory to map
  a callsign/QSO to a reference. This is a genuinely new counted-unit Grayline does not model.

---

## 5. Canonical minimal tracking schema (Step 4 synthesis)

The smallest set of indexed keys that covers every award above. Each key is maintained as a
**worked** set and a **confirmed** set (and, where a LoTW-only award exists, a separate
**lotw-confirmed** set — see gotcha G1).

| # | Tracking key | Awards served |
|---|---|---|
| K1 | `(call)` | call roster / worked-before (UX, not an award) |
| K2 | `(entity)` any band/mode | DXCC Mixed, Honor Roll/#1 (with current-list filter), CQ DX Mixed |
| K3 | `(entity, modeclass)` any band | DXCC CW/Phone/Digital, Honor Roll per-mode, CQ DX per-mode |
| K4 | `(entity, band)` mixed-mode | Single-band DXCC, 5BDXCC, **DXCC Challenge** |
| K5 | `(entity)` where PROP_MODE=SAT | DXCC Satellite |
| K6 | `(grid4, band)` | VUCC (all bands), FFMA (band=6m ∩ 488-set) |
| K7 | `(grid4)` where PROP_MODE=SAT | VUCC Satellite |
| K8 | `(state)` | WAS basic |
| K9 | `(state, modeclass)` | WAS Phone/CW/Digital/RTTY, **Triple Play** (LoTW-confirmed variant) |
| K10 | `(state, band)` | 5BWAS, band WAS |
| K11 | `(cq_zone)` and `(cq_zone, modeclass)` and `(cq_zone, band)` | WAZ basic / mode / band / 5BWAZ |
| K12 | `(continent)` and `(continent, band)` and `(continent, modeclass)` | WAC basic / band / mode / 5BWAC |
| K13 | `(prefix)` and `(prefix, modeclass)` *(optional CQ)* | CQ WPX Mixed / mode |
| K14 | `(iota_ref)` *(optional RSGB, needs island data)* | IOTA |

`modeclass` ∈ {CW, Phone, Digital} (Other excluded from award counting). All "any band" keys
**exclude PROP_MODE=SAT and PROP_MODE=EME** from terrestrial awards where the rules separate them.

**Notably absent (intentionally): `(entity, band, modeclass)`.** No award counts at that grain.
It is the over-tracked dimension that caused the motivating bug.

---

## 6. Gap analysis vs current Grayline model

### 6.1 What Grayline has RIGHT
- `(entity)` / `worked_dxcc` → DXCC Mixed ✓
- `(entity, modeclass)` via `dxcc_modeclass` (entity-level, any band) → mode DXCCs ✓ (this is the
  fix that motivated the spec — the surfaced scope uses `dxcc_modeclass_status`, correct grain)
- `(entity, band)` → Challenge + single-band, with PROP_MODE=SAT excluded ✓
- `(grid4, band)` → VUCC/FFMA ✓; FFMA gated on 488 set and band=6m ✓
- Satellite DXCC + Satellite VUCC split out by PROP_MODE=SAT ✓
- `(state)`, `(state, band)` → WAS + 5BWAS ✓
- `(cq_zone)` → WAZ basic ✓
- Challenge band set correctly excludes 60m ✓

### 6.2 What Grayline is MISSING (under-tracking — awards it can't currently support)
1. **WAS per-mode** `(state, modeclass)` — needed for WAS-CW/Phone/Digital and **Triple Play**.
   Currently only `(state)` and `(state, band)` exist. Triple Play in particular is unsupported.
2. **LoTW-only confirmation distinction** — Triple Play (and any future LoTW-only award) requires
   a separate LoTW-confirmed set. Grayline collapses LoTW+paper+eQSL into one `confirmed` flag.
3. **WAC as a surfaced scope** — `(continent)` / `(continent, band)` / `(continent, modeclass)`.
   The oracle (`compute_score_summary`) bins WAC continents but `_build_scores_payload` and
   `scopeStatus` never surface it. 5BWAC needs the WARC/satellite-void rule.
4. **WAZ by mode and by band** — only basic `(cq_zone)` exists; WAZ-CW/SSB/RTTY and 5BWAZ need
   `(cq_zone, modeclass)` and `(cq_zone, band)`.
5. **Honor Roll / #1 Honor Roll** — needs a **current-DXCC-list membership filter** (deleted
   entities excluded) plus the live current-entity count to compute the top-10% threshold.
   Grayline counts all worked entities without the current-list filter.
6. **CQ WPX** — `wpx_prefix()` exists and the oracle bins WPX, but it is not surfaced as an award
   scope with the 400/300 thresholds and eQSL eligibility.
7. **IOTA** — entirely unmodeled; requires island-reference data (new counted unit).
8. **VUCC microwave bands above 23cm** and the **per-band initial thresholds** (100/50/25) —
   Grayline reports a raw confirmed count per band but does not encode the differing initial
   credit (100 vs 50 vs 25) or endorsement increments per band.
9. **5BDXCC as a distinct award** — the `(entity, band)` data exists but 5BDXCC's
   "100 on EACH of 80/40/20/15/10, current entities only" completion logic isn't computed; only
   Challenge (sum) and per-band counts are.

### 6.3 What Grayline OVER-tracks (dimensions no award needs)
1. **`(country, band, mode)` — `worked_country_band_mode`** (raw ADIF mode, e.g. FT8/RTTY split).
   **No award counts at `(entity, band, rawmode)`.** This is the clearest over-track — a personal
   completionist grain. It is maintained and exposed via `country_band_mode_status` but no real
   award consumes it.
2. **`(country, band, modeclass)` — `worked_country_band_modeclass`.** Also no award. It exists
   only as the source the code collapses to derive `(country, modeclass)` (K3, which IS real).
   The collapsed form is needed; the band-split form is not an award grain and should not drive
   user-facing "needed" pills. (This is the exact shape of the motivating bug — keep it internal
   as a derivation source, never surface it.)
3. **ITU zone, US county (USC)** in the oracle — no ARRL/CQ award in the target set uses these
   (CQ WW uses ITU in contest scoring, not an award; county = USA-CA, a CQ award not in scope).
   Harmless as oracle bins but should not become scopes without an explicit award behind them.

### 6.4 Outright bugs surfaced by this research
- **WAS Alaska/Hawaii drop**: `reload()` gates WAS state capture on `dxcc == "291"`
  (`worked_state.py` ~line 595). Alaska (DXCC 6) and Hawaii (DXCC 110) are **separate DXCC
  entities** but **valid WAS states**. The current filter silently excludes AK and HI QSOs from
  WAS, making WAS unattainable. WAS should key on a valid-US-state postal code (with AK/HI
  included), not on DXCC 291. **Flag — likely real bug.**

---

## 7. Ambiguities, recent changes, and confirmation gotchas

- **G1 — eQSL acceptance varies by sponsor (biggest gotcha).**
  - **ARRL (DXCC, WAS, VUCC, FFMA, WAC):** LoTW or paper only. **eQSL NOT accepted.**
  - **Triple Play WAS:** **LoTW ONLY** — paper not accepted either.
  - **CQ WAZ:** LoTW, paper, or CQ-approved electronic verifications.
  - **CQ WPX:** LoTW, paper, **and eQSL** (via CQ's eQSL interface) — eQSL IS valid here.
  - Grayline's `_is_confirmed_record` treats `lotw_qsl_rcvd | qsl_rcvd | eqsl_qsl_rcvd` as
    equivalent. This **over-counts** confirmations for ARRL awards (eQSL shouldn't count) and
    **cannot represent** Triple Play's LoTW-only requirement. Recommend tracking the confirmation
    *source* (LoTW / paper / eQSL) per QSO and filtering per award.
- **G2 — DXCC current count drifts.** Honor Roll's top-10% threshold and #1's full-list target
  depend on the live current-entity count (≈340, changes when entities are added/deleted).
  Hard-coding 340 will silently rot. Pull from cty.dat or the ARRL list version.
- **G3 — Deleted entities differ by award.** Mixed/Phone/CW/Digital/single-band **count** deleted
  entities; Challenge, 5BDXCC, Honor Roll do **NOT**. Grayline must know deleted-entity status per
  entity to compute Honor Roll / Challenge / 5BDXCC correctly.
- **G4 — VUCC/FFMA 200km-circle (Rule 6).** Awards are per-location, not per-operator. Fine for a
  single-QTH user; ambiguous for roving/multi-QTH logs. Could not find a per-QSO ADIF field that
  encodes the submitting circle; this is an application-time constraint, flag for multi-QTH users.
- **G5 — WAS Digital vs RTTY.** ARRL lists both a Digital and a (historical) RTTY WAS. Modern
  Digital WAS subsumes RTTY/FT8/PSK; treat RTTY as part of the Digital class for tracking, but
  note separate certificates historically existed.
- **G6 — CW DXCC date floor (1975-01-01).** CW QSOs before 1975-01-01 count as **Mixed**, not CW.
  Grayline does not apply this date filter to the CW class. Low practical impact for modern logs.
- **G7 — CQ DX Marathon is year-scoped.** Different grain `(unit, year)`, work-once-per-year,
  annual submission. Almost certainly out of scope for a lifetime-award dashboard — flag, don't
  build, unless a contest-chase mode is added.
- **G8 — IOTA counted unit.** Island references, not in standard ADIF DXCC; requires the IOTA
  directory. Genuinely new data dependency.
- **G9 — WAC continent count = 6 (no Antarctica).** cty.dat may emit Antarctica ("AN"); exclude
  it from WAC. (Antarctica IS a DXCC entity and a WAZ zone, just not a WAC continent.)

Where a fact could not be confirmed from an official source it is noted inline; the per-band VUCC
microwave (above 23cm) initial thresholds and the full WAS band-endorsement increment ladder were
taken from the ARRL VUCC/WAS rules PDFs as summarized in §7 sources but the highest-microwave
exact initial counts vary by band and should be re-verified against the live VUCC rules PDF before
shipping per-band VUCC threshold logic.

---

## 8. Recommended actions (priority order)

1. **Track confirmation source** (LoTW / paper / eQSL) per QSO; filter per award (fixes G1, enables
   Triple Play, corrects ARRL eQSL over-count).
2. **Fix WAS AK/HI drop** (§6.4) — key WAS on US state postal code, not DXCC 291.
3. **Add `(state, modeclass)`** → WAS-mode + Triple Play.
4. **Surface WAC** `(continent[, band][, modeclass])` as a scope (data already in the oracle).
5. **Add WAZ by mode/band** `(cq_zone, modeclass)` / `(cq_zone, band)`.
6. **Stop surfacing `(entity, band, mode)` / `(entity, band, modeclass)`** as user-facing
   "needed" pills — keep `(entity, band, modeclass)` only as the internal source for the collapsed
   `(entity, modeclass)`.
7. **Add current-DXCC-list + deleted-entity filter** for Honor Roll / Challenge / 5BDXCC.
8. (Optional) WPX scope with eQSL eligibility; IOTA only if island-reference data is sourced.

---

## 9. Sources (official, cited)

ARRL:
- DXCC Rules — http://www.arrl.org/dxcc-rules and https://www.arrl.org/files/file/DXCC/Rules.pdf
- DXCC Award Information / Endorsements — http://www.arrl.org/dxcc-award-information , http://www.arrl.org/dxcc-endorsements
- WAS Rules/Fees — http://www.arrl.org/was ; WAS Rules PDF — https://www.arrl.org/files/file/Awards%20Application%20Forms/wasrules.pdf
- Triple Play WAS — http://www.arrl.org/triple-play and https://lotw.arrl.org/lotwuser/triple-play (LoTW-only)
- VUCC Rules — http://www.arrl.org/vucc and https://www.arrl.org/files/file/Awards%20Application%20Forms/VUCCRULE1a.pdf
- FFMA — http://www.arrl.org/ffma and http://www.arrl.org/files/file/FFMA/FFMA_Announcement.pdf
- WAC (IARU, ARRL-administered) — http://www.arrl.org/wac ; 5BWAC app — https://www.arrl.org/files/file/DXCC/5bWAC%202019.pdf

CQ:
- CQ WAZ Rules — https://www.k0nr.com/wordpress/wp-content/uploads/2024/02/cq_waz_rules_english.pdf ; zone list https://cqww.com/cq_waz_list.htm
- CQ WPX Rules — https://cqwpx.com/rules/ and https://cq-amateur-radio.com/cq_awards/cq_wpx_awards/cq-wpx-award-rules-022017.pdf
- CQ DX Marathon — https://dxmarathon.com/rules/2026/

RSGB / IARU:
- IOTA Programme — https://rsgb.org/main/operating/amateur-radio-awards/iota-programme/ and https://www.iota-world.org/
