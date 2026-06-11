# Grayline — Theory of Operation

This document explains *why* Grayline works the way it does. The README covers
setup; this covers the design so you can trust (and tune) what you're seeing.

---

## 1. What Grayline is — and isn't

Grayline is a **spot consumer and display layer**, not a logger and not a spot
*producer*. It:

1. **Consumes** validated spots from a DX cluster (telnet),
2. **Enriches** each spot — DXCC entity / continent / zone (via `cty.dat`),
   spotter distance, grid, and your own worked/needed status,
3. **Displays** them as an award-aware call roster, and serves an award
   scoreboard.

It does **not** keep your log. Your logger of record (WSJT-X, N1MM, your
station logbook, QRZ) stays authoritative; Grayline reads from it and never
writes back to it. This is a deliberate boundary: keeping the log correct is the
logger's job.

**Where spots come from.** Grayline itself only speaks DX-cluster telnet. It has
**no** direct RBN, PSKReporter, or DXSummit ingestion. If you want those merged
together you put an aggregator *upstream* (the project pairs with **GoCluster**,
which pulls RBN over telnet + PSKReporter over MQTT + peers, de-dupes, and
serves one clean stream). Point Grayline at any standard cluster and it works;
point it at an aggregator and it works better. **PSKReporter in particular is
only available through an upstream aggregator — Grayline cannot pull it
directly.**

**Why it runs off your operating PC.** High-rate spot/UDP traffic at the
workstation competes with audio (DPC latency → dropouts on a software-defined
radio). Grayline is built to run on a separate always-on box (a container, a
Pi, a NAS) and render to a browser, so the compute and the firehose never touch
the machine that's running your radio. You operate from any device — shack PC,
laptop on the couch, phone over Tailscale — all pointed at the same instance.

---

## 2. The core idea: distance is measured from the *spotter*, not the DX

This is the single most important design decision, and the one that separates
Grayline from a conventional spotting tool.

A normal cluster shows you what **the whole planet** is hearing. On a localized
band that's mostly noise: a station working JA from the west coast tells a
midwest operator nothing about *their* next ten minutes.

Grayline instead asks: **who, geographically near me, is hearing this signal
right now?** For every spot it computes the distance from the **spotter's** QTH
to **your** QTH — not the DX's. The logic:

> If a station a few hundred miles away just decoded this DX, we're very likely
> sharing the same ionospheric path. Their decode is a near-real-time forecast
> of *my* propagation.

A co-located spotter is effectively a **reference receiver** for your own
station. When someone nearby is hearing things you aren't, that's not "they have
propagation you don't" — at a few hundred miles the path is essentially shared —
it's a signal that the opening is *there for you to work*, and the limiter is
your antenna or local noise, not the ionosphere. That's exactly the
"is it propagation, or is it me?" question Grayline is built to answer.

### Tiered radius — tight on VHF, wide on HF

"Local" is band-dependent, because propagation geography is:

| Bands | "Local" radius | Why |
|---|---|---|
| HF (160 m – 10 m) | **≤ 300 mi** | HF skip is broad; a spotter a few hundred miles off shares your opening |
| 6 m and up (VHF/UHF) | **≤ 150 mi** | Sporadic-E and VHF openings are a localized speckle — a closer spotter is the meaningful one |

These are the defaults (`radiusForBand()` in the UI; the telnet re-broadcast
mirrors them server-side). You can override the radius per band in the settings
panel; overrides persist per device. The 6 m case is the one that drove the
feature: a flat 300 mi radius is simply wrong for a band whose openings are
~150 mi wide.

---

## 3. Why you want QRZ lookups (and what happens without them)

To compute spotter→you distance, Grayline needs the **spotter's grid square**. A
cluster spot only carries the spotter's *callsign*, so Grayline resolves
callsign → grid via the **QRZ XML API**, caching every result on disk
(`qrz_cache.json`) so each callsign is looked up once and reused forever (the
cache is also shared/fed by sibling tools).

The lookup is a background worker: unknown spotters are queued, resolved without
blocking spot ingest, and written back to the cache. Over a session the cache
warms and nearly every spotter resolves instantly.

QRZ lookups serve **two** purposes:

1. **Spotter grid → distance filtering** (the local-spotter feature above).
2. **DX-call grid backfill** — for grid-based awards (VUCC, FFMA) a spot needs
   the DX station's grid. FT8 decodes carry it in the message ("CQ N5FWB EL29"),
   but cluster spots and mid-QSO decodes don't; the QRZ cache fills the gap.

**This is why QRZ credentials are recommended.** Without them:

- Spotter grids never resolve → **you cannot use the local-spotter distance
  filter at all** (the killer feature is dark),
- DX grids fall back to whatever the decode carried.

You can still run Grayline with no QRZ account — you'll just be filtering by
band and mode only, on the full firehose, with no distance awareness.

### The spotter gate: strict vs. lenient (`require_spotter_grid`)

There's a direct consequence of relying on spotter grids: **what do you do with
a spot whose spotter you can't place?** Two policies:

- **Lenient (`require_spotter_grid: false`, the default).** Keep the spot, with
  unknown distance. A fresh install — no QRZ creds yet, cold cache, maybe no
  home grid set — still shows a populated roster from the first second. You
  filter by band/mode; distance is simply blank until the spotter resolves.

- **Strict (`require_spotter_grid: true`).** Drop any spot whose spotter has no
  resolvable grid. This is the "only verifiable spotters" policy: it filters out
  junk and mis-located spotters at ingest (a station claiming to spot EU on 2 m
  from the far side of the planet), and it's what you want **once you have QRZ
  creds and a warm cache**. With strict mode on *before* those are in place, you
  see nothing — which is correct (everything is unverifiable) but unhelpful for
  a first run.

The recommended path: start lenient, add QRZ credentials, then flip to strict
once the cache is warm and you want the cleaner feed.

---

## 4. Worked / needed — how Grayline knows your status

Grayline merges three log sources into one in-memory worked/confirmed model,
refreshed periodically:

1. **QRZ Logbook** (`qrz_logbook.json`) — your long-tail archive, pulled via the
   QRZ Logbook API. Lags by minutes-to-hours.
2. **LoTW** (`lotw_qsl.adi`) — confirmations pulled incrementally straight from
   ARRL LoTW, so a confirmation lands here without waiting for it to surface in
   QRZ.
3. **Local ADIF** (`qso_logged.adi`) — written in real time on every WSJT-X /
   N1MM "QSO Logged" UDP event, so a QSO you just made flips to *worked*
   immediately, before any sync round-trip.

Records are de-duplicated across sources; confirmation evidence is OR'd so the
surviving record carries the strongest proof.

### Confirmation **source** matters (not just "confirmed")

Grayline tracks *how* each QSO was confirmed — **LoTW**, **paper card**, or
**eQSL** — because award programs disagree:

| Confirmation | LoTW | Paper card | eQSL |
|---|---|---|---|
| **ARRL** (DXCC, WAS, VUCC, …) | ✅ | ✅ | ❌ |
| **ARRL Triple Play** | ✅ only | ❌ | ❌ |
| **CQ** (WAZ, WPX) | ✅ | ✅ | ✅ |

So a card-confirmed contact counts toward DXCC and WAS but **not** Triple Play;
an eQSL-only contact counts toward CQ awards but **not** ARRL. Collapsing these
into one "confirmed" flag over-counts ARRL awards for eQSL users and would let
card contacts wrongly satisfy Triple Play. Grayline keeps them separate so each
award counts by its own rules.

A useful side effect: because Grayline holds the QSO-level record, it knows
*which* contact earned a paper-card credit — something LoTW's award-credit view
discards. That's why Grayline's award totals can legitimately read a hair above
your LoTW online totals: it counts your valid paper cards, which LoTW's online
tally (upload-only) doesn't show.

---

## 5. Per-band award scopes

What counts as "needed" depends on the band, because the awards do:

| Bands | Chases |
|---|---|
| HF (160 m – 6 m) | DXCC entity × band (ARRL Challenge / per-band DXCC / 5BDXCC) |
| 6 m | **Both** — DXCC×band *and* grids (FFMA + 6 m DXCC are both live awards) |
| 2 m and up | Grids (VUCC) |

A spot highlights against the award(s) that actually matter on its band, so you
don't get nagged about a DXCC entity on a band where you only care about grids,
or miss a needed grid because an HF-DX filter hid it. Mode-specific DXCCs
(CW / Phone / Digital) are tracked **entity-level (any band)**, which is the
real ARRL grain — not band×mode, which is no award at all.

Award rows are individually toggleable (Scores setup). ARRL awards are on by
default; CQ (WAZ) and JARL (WAJA) are opt-in.

---

## 6. Spot precedence and de-duplication

The same QSO can arrive from several sources within the spot TTL. Grayline keeps
the highest-fidelity copy by source precedence:

```
WSJTX-LOCAL  (your own running WSJT-X)   highest
SPARKGAP/-LOCAL (your own skimmer)
GOCLUSTER    (local validated aggregator)
everything else (RBN/PSKR/DXSummit via the aggregator)   lowest
```

Your own receive path is trusted over third-party propagation reports.

### Grid persistence

A station's grid only rides in its FT8 **CQ** (or a grid-reply); mid-QSO
messages (signal reports, `RR73`, `73`) carry none. Because every decode of a
call shares one spot, a later grid-less decode must not erase the grid an
earlier CQ gave us. Grayline holds an **effective grid** that never regresses to
blank: this decode's grid wins (so a *rover* updates correctly), else the last
grid we decoded for that call, else the QRZ-cached grid. A live decode always
beats a cached value.

---

## 7. Putting it together — the intended workflow

1. A nearby spotter (or your own decoder) hears a station.
2. Grayline places the spotter relative to you, and the DX relative to your
   awards.
3. The roster surfaces, near the top, **stations that are (a) needed for an
   award you're chasing on that band and (b) being heard by receivers close
   enough to share your propagation.**
4. You click to tune (WSJT-X for digital, Flex slice for CW/SSB) and work it.

The whole point: most tools tell you what the band is doing *somewhere*. Grayline
tells you what *you* can probably work *right now* — and which of those actually
moves an award needle. The local-spotter distance model is what makes "probably
work right now" real, and QRZ lookups are what make the distance model possible.
