# WAJA prefecture resolution

How Grayline determines a Japanese station's prefecture for **WAJA** (JARL's Worked
All Japan — the 47 prefectures of DXCC 339, Japan). There are two independent paths,
using different data on purpose: **award credit** (authoritative, from the logged
QSO) and the **live-spot roster pill** (a best-effort advisory guess).

Code: `worked_state.py` — `resolve_prefecture`, `resolve_prefecture_from_addr2`,
`ja_call_area`, and the `_JA_*` tables. QRZ `addr2` fetch lives in `qrz.py`.

## Prefecture codes

WAJA prefecture numbers are the **ADIF Primary Administrative Subdivision** codes for
Japan — a two-digit string `"01"`–`"47"` (01 = Hokkaido, 10 = Tokyo, 47 = Okinawa).

> ⚠️ These are **not** ISO 3166-2:JP codes (where Tokyo = 13). ADIF and ISO disagree;
> only ADIF codes are used here, because that's what LoTW and logged ADIF speak.

`_JA_PREFECTURES_47` is the full code set; `_JA_PREF_NAME_TO_CODE` maps romaji names →
ADIF codes.

## Path 1 — Award credit (authoritative)

WAJA *scoring* always comes from the logged QSO, never from a guess:

- **`STATE`** — for JA, a two-digit code that *is* the WAJA reference number.
- If `STATE` is absent, the first two digits of **`CNTY`** (the JCC/JCG number) carry
  the same prefecture code.

This populates `worked_prefectures` / `confirmed_prefectures`. Rock-solid; independent
of QRZ.

## Path 2 — Live-spot roster pill (best-effort guess)

A live spot (FT8 decode or cluster spot) usually carries **no** prefecture — just a
callsign. To show an advisory "probably a new prefecture" nudge,
`resolve_prefecture(call, addr2)` combines two signals.

### Ingredient A — QRZ `addr2` name match

`resolve_prefecture_from_addr2(addr2)`:

- Split the QRZ `addr2` free-text on non-letters into tokens.
- Match each token **whole-word**, case-insensitive, against `_JA_PREF_NAME_TO_CODE`
  (all 47 romaji names + variants: Gunma/Gumma, Hyogo/Hyougo, Oosaka/Osaka, …).
- Whole-word is essential: `"Sagamihara"` (a Kanagawa city) must not match `"Saga"`;
  `"Narashino"` must not match `"Nara"`.
- If two *different* prefectures appear in one string (rare — e.g. a QSL-manager
  line), return `None` (ambiguous → decline).

### Ingredient B — Call-area cross-check

`ja_call_area(call)` extracts the area digit — the digit after the prefix letters
(`JA`**`1`**`RL` → `"1"`). `_JA_AREA_PREFECTURES` maps each area → the set of
prefectures in it. This **validates** the `addr2` answer:

- `addr2` code ∈ call-area set → **consistent, accept.**
- `addr2` code ∉ call-area set → **decline** (the QRZ address is probably a QSL /
  mailing address in a different region than where the station is operating).
- call area unknown → trust `addr2`.
- `addr2` gave nothing, but the area has a **single** prefecture (only
  **area 8 = Hokkaido**) → resolve with certainty.

Edge cases handled by `ja_call_area`: portable `/N` overrides (`JA1ABC/6` → operating
in area 6); `7J`–`7N` / `8J`–`8N` prefixes (the leading digit is the prefix, not the
area).

### Guiding rule

**A wrong pill is worse than a missing one.** Anything short of a confident
resolution returns `None` → no pill.

## Worked examples

**Clean resolve — `JA6XYZ` on 6m FT8:**

1. QRZ `addr2 = "Fukuoka-shi, FUKUOKA 812-0011"` → tokens `Fukuoka`, `shi`,
   `FUKUOKA` → `"fukuoka"` → **40 (Fukuoka)**.
2. `JA`**`6`** → area 6 (Kyushu + Okinawa) = {40–47}. 40 ∈ set → **resolve to 40,
   Fukuoka.**

**Decline — `JE6ABC` with a Tokyo mailing address:**

1. QRZ `addr2 = "Minato-ku, TOKYO 105-0011"` → `"tokyo"` → **10 (Tokyo)**.
2. `JE`**`6`** → area 6 = {40–47}. 10 ∉ set → address disagrees with operating
   region → **decline, no pill.**

**Whole-word guard:** `"Sagamihara, KANAGAWA"` → `"Sagamihara"` never matches `"Saga"`;
`"Kanagawa"` → 11 (Kanagawa).

## Key invariant

The QRZ-derived guess feeds **only** the live roster pill. It never contributes to
award credit — WAJA scoring is always from the logged QSO's `STATE` / `CNTY`. The pill
may be wrong occasionally with **zero** consequence to the award totals.
