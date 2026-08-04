# Grayline Award Dashboard — Design Spec

Generalize the FFMA tab into a single **band × award × mode** drill-down.
FFMA-on-6m stops being a special page and becomes *one cell* of a generic grid.
Status: SPEC (no code moved yet). Data is ~all pre-computed in `worked_state`.

---

## 1. Award-config table (the heart)

One declarative table drives everything. Adding an award = adding a row.

```python
# family → picks the renderer.  bands → band-aware gating.  modes → CW/Phone/Digital filter applies?
# target → the goal.  source → the worked_state set to read.  universe → the full list to diff against.
AWARDS = {
  "dxcc":   {"name":"DXCC",      "family":"entity", "bands":"all",   "modes":True,  "target":100, "source":"confirmed_dxcc_band",     "universe":"DXCC_ENTITIES"},
  "was":    {"name":"WAS",       "family":"state",  "bands":"all",   "modes":True,  "target":50,  "source":"confirmed_state_band",    "universe":"US_STATES"},
  "vucc":   {"name":"VUCC",      "family":"grid",   "bands":"vhf+",  "modes":False, "target":100, "source":"confirmed_grid_band",     "universe":"grids_seen"},
  "ffma":   {"name":"FFMA",      "family":"grid",   "bands":["6m"],  "modes":False, "target":488, "source":"confirmed_grid_band",     "universe":"FFMA_488", "overlay":"ffma_rarity"},
  # phase 2:
  "wac":    {"name":"WAC",       "family":"zone",   "bands":"all",   "modes":False, "target":6,   "source":"confirmed_continent_band","universe":"CONTINENTS"},
  "waz":    {"name":"CQ WAZ",    "family":"zone",   "bands":"all",   "modes":False, "target":40,  "source":"confirmed_cq_zone_band",  "universe":"CQ_ZONES"},
}
```

- `bands`: `"all"` | `"vhf+"` | explicit list (`["6m"]`). Drives the Award dropdown per selected band.
- `modes`: grids are mixed-mode → filter grays out; entity/state awards honor CW/Phone/Digital.
- `overlay`: per-award extra layer (FFMA rarity/scream) so VUCC doesn't inherit FFMA-only chrome.

## 2. Families → renderers (3 for v1)

| Family | Unit | Renderer | Awards |
|---|---|---|---|
| **grid** | Maidenhead | the **existing FFMA map/matrix**, parameterized by (band, universe, overlay) | FFMA, VUCC |
| **entity** | DXCC # | country grid/list, colored confirmed/worked/needed, band(+mode) scoped | DXCC, 10BDXCC, mode-DXCC |
| **state** | US state | 50-cell/US map, band(+mode) scoped | WAS, Triple Play |
| *(zone — ph2)* | continent/zone | small list | WAC, WAZ, WAJA |

## 3. The endpoint (one handler, all awards)

```
GET /api/award?band=6m&award=ffma&mode=all
GET /api/award?band=20m&award=dxcc&mode=cw
GET /api/award?band=40m&award=was&mode=digital
```

Response:
```json
{ "award":"dxcc", "band":"20m", "mode":"cw", "family":"entity", "target":100,
  "counts": {"confirmed":93, "worked":4, "needed":3},
  "items": [ {"id":"291","label":"United States","status":"confirmed"},
             {"id":"6","label":"Alaska","status":"needed"} ] }
```

Handler logic (generic): `cfg = AWARDS[award]` → resolve `universe` list → for each item classify
`confirmed / worked / needed` against `worked_state[cfg.source]` scoped to `band` (and `mode` if `cfg.modes`)
→ attach `overlay` data if present. **One handler covers every award** because the config says which set to read.

## 4. Data sources — already computed in `worked_state`

| Award axis | worked_state set | status |
|---|---|---|
| DXCC per band | `confirmed_dxcc_band`, `slot_calls` (worked) | ✅ exists |
| mode-DXCC | `confirmed_country_band_mode`, `confirmed_dxcc_modeclass` | ✅ exists |
| WAS per band | `confirmed_state_band`, `confirmed_state_modeclass` | ✅ exists |
| VUCC / FFMA grids | `confirmed_grid_band` + `grid_band_status()` | ✅ exists (FFMA tab uses it) |
| WAC | `confirmed_continent_band` | ✅ exists |
| CQ zones | `confirmed_cq_zones` (band variant to add) | ~exists |
| WAJA prefectures | `confirmed_prefecture_band` | ✅ exists |

⇒ This is an **expose-existing-data** job, not a **compute-new-data** job. Universes needed: `US_STATES` (50, static),
`DXCC_ENTITIES` (from cty.dat/dxcc names), `FFMA_488` (have it: `data/ffma_grids.json`).

## 5. UI

```
[ Band: 6m ▾ ]   [ Award: FFMA ▾ ]      Mode: (•) All  ( ) CW  ( ) Digital  ( ) Phone
──────────────────────────────────────────────────────────────────────────────────
   < renderer for the selected family — grid map / entity grid / US-state map >
──────────────────────────────────────────────────────────────────────────────────
   384 confirmed · 11 worked-pending · 90 needed        79% → 488
```

- **Band dropdown gates the Award dropdown** (band-aware; FFMA/VUCC only appear on VHF+).
- **Mode toggle** grays out for grid awards (mixed-mode); active for entity/state.
- Renderer swaps by `family`. Counts bar is universal (confirmed/worked/needed/target).

## 6. FFMA refactor-in (zero loss)

- Current `/api/ffma_map` + `_FFMA_MAP_PAGE` → the **grid renderer** with the `ffma` config (universe=FFMA_488,
  field-matrix layout, rarity overlay).
- `/ffma_map` stays as an **alias** → `/award?band=6m&award=ffma` (nav link + bookmarks unbroken).
- Rarity/scream overlay is FFMA-specific → lives on the `overlay:"ffma_rarity"` hook, so VUCC/other grid awards
  don't inherit it.
- Net: **no FFMA functionality lost** — it becomes the first citizen of the generic tab.

## 7. v1 scope

- Families: **grid, entity, state.**
- Awards: **FFMA, VUCC, DXCC, WAS** (+ 10BDXCC as a DXCC all-band rollup view).
- Mode filter on entity/state; band-aware gating; FFMA refactored in.
- **Defer to phase 2:** WAC, CQ WAZ, WAJA (zone renderer), Triple Play, Challenge rollup — each is ~a config row.

## 8. Free bonus: the 10BDXCC / grail cockpit

10BDXCC = "DXCC, target 100, across all ten bands." The entity family with a band=**all** rollup renders a
**10-band progress strip** for free — 8 green bands, 6m + 160m climbing. That's the grail cockpit from the
season roadmaps, rendered from the same machinery. (See `reference_2026_6m_season_baseline` roadmap.)

## 9. Build order (so nothing breaks mid-flight)

1. `AWARDS` table + universes + `/api/award` handler (generic; returns items+counts). Test via curl, no UI.
2. **Entity renderer** (new) — DXCC/10BDXCC/WAS-adjacent; the biggest new value.
3. **State renderer** (new) — WAS.
4. **Grid renderer** = refactor the FFMA page to read the config; alias `/ffma_map`. **Do this LAST** so the
   working FFMA tab is untouched until the generic frame is proven.
5. New `/awards` tab + nav link; band-aware dropdowns; mode toggle; counts bar.

Guardrails: ARRL-sanctioned awards only by default ([[feedback_arrl_default_personal_extension]]); band-aware
scope ([[feedback_per_band_award_scope]]); lead with the live-grail combos ([[feedback_evidence_based_scope]]).
