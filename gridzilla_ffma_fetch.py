#!/usr/bin/env python3
"""
gridzilla_ffma_fetch — refresh data/ffma_rarity.json from Gridzilla's live API.

Replaces the hand-maintained snapshot of N7PHY's FFMA Leader Board with the
live, canonical feed. Gridzilla (gridzilla.us) became the de-facto FFMA tracker
in 2026, endorsed on the FFMA groups.io by Ed N7PHY himself (who runs the
Leader Board AND is the source of the rarity tiers Grayline already shipped).

Methodology is IDENTICAL to the old static file: the endpoint defaults to
"leaders" mode (population = users with >= 400 confirmed grids, same 400+ cut
N7PHY's board uses), so `missing / population` is the same "% of FFMA leaders
still needing this grid" metric — just live and regenerated daily.

  GET https://gridzilla.us/api/map/aggregates/ffma_most_needed
    -> { generated_at_utc, population:{population_size,total_users},
         grids: { GRID: { confirmed, worked, missing }, ... } }

Output: data/ffma_rarity.json (same schema the server's _load_ffma_rarity reads):
  { _source, _fetched, _generated_at_utc, _metric, _population, _count, _tiers,
    grids: { GRID: { pct_needed, leaders_needing, tier }, ... } }

Safety: validates the payload (>= 400 grids, sane population) and writes
atomically (tmp + replace). On ANY failure it leaves the existing file
untouched, so the server keeps serving the last-good rarity (degrade-gracefully,
same posture as dxcc_rarity_refresh_loop). No API key, public endpoint.

Usage:
  python3 gridzilla_ffma_fetch.py          # fetch + rewrite data/ffma_rarity.json
  python3 gridzilla_ffma_fetch.py --test   # fetch + validate + print summary, no write

Cron (daily): see crontab — runs as fred, stdlib only, no venv needed.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = _BASE_DIR / "data" / "ffma_rarity.json"
API_URL = "https://gridzilla.us/api/map/aggregates/ffma_most_needed"

# Honest, identifying User-Agent (Cloudflare lets it through — no browser spoof).
USER_AGENT = "Grayline/1.0 (+https://github.com/hotairfred/grayline) FFMA-rarity-sync"

# Tier thresholds — % of 400+ leaders still needing the grid. These reproduce the
# prior static file's split exactly and match the personal_tier overlay in
# grayline_server.py (_augment_ffma_reachability).
RARE_PCT = 30.0
UNCOMMON_PCT = 10.0

# Sanity floors: refuse to overwrite good data with a degenerate response.
MIN_GRIDS = 400          # FFMA is 488 grids; a truncated payload must not win
MIN_POPULATION = 50      # too few leaders -> noisy ratios, keep the cached file


def _tier(pct: float) -> str:
    return "rare" if pct >= RARE_PCT else ("uncommon" if pct >= UNCOMMON_PCT else "common")


def fetch() -> dict:
    """Fetch + validate the Gridzilla aggregate. Raises on anything suspicious."""
    req = urllib.request.Request(API_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read().decode("utf-8"))

    grids = payload.get("grids")
    pop = (payload.get("population") or {}).get("population_size")
    if not isinstance(grids, dict) or len(grids) < MIN_GRIDS:
        raise ValueError(f"unexpected payload: {len(grids) if isinstance(grids, dict) else 'no'} grids")
    if not isinstance(pop, int) or pop < MIN_POPULATION:
        raise ValueError(f"unexpected population_size: {pop!r}")
    return payload


def build(payload: dict) -> dict:
    """Map Gridzilla's per-grid {confirmed,worked,missing} onto Grayline's
    {pct_needed, leaders_needing, tier} schema. pct_needed = missing/pop*100."""
    pop = payload["population"]["population_size"]
    out_grids = {}
    counts = {"rare": 0, "uncommon": 0, "common": 0}
    for grid, rec in payload["grids"].items():
        missing = int(rec.get("missing", 0))
        pct = round(missing / pop * 100, 1)
        tier = _tier(pct)
        counts[tier] += 1
        out_grids[grid.upper()] = {
            "pct_needed": pct,
            "leaders_needing": missing,
            "tier": tier,
        }
    return {
        "_source": "Gridzilla (gridzilla.us) /api/map/aggregates/ffma_most_needed "
                   "— live FFMA Leader Board, leaders mode (>=400 confirmed), Ed N7PHY",
        "_fetched": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "_generated_at_utc": payload.get("generated_at_utc"),
        "_metric": "pct_needed = % of FFMA leaders (400+ confirmed) still needing the grid",
        "_population": pop,
        "_count": len(out_grids),
        "_tiers": counts,
        "grids": out_grids,
    }


def write_atomic(data: dict) -> None:
    tmp = OUTPUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(OUTPUT_PATH)


def main() -> int:
    ap = argparse.ArgumentParser(description="Refresh FFMA rarity from Gridzilla")
    ap.add_argument("--test", action="store_true",
                    help="fetch + validate + print summary, do NOT write the file")
    args = ap.parse_args()

    try:
        payload = fetch()
        data = build(payload)
    except Exception as e:
        print(f"gridzilla_ffma_fetch: FAILED ({type(e).__name__}: {e}) "
              f"— kept existing {OUTPUT_PATH.name}", file=sys.stderr)
        return 1

    t = data["_tiers"]
    summary = (f"{data['_count']} grids  pop={data['_population']}  "
               f"rare={t['rare']} uncommon={t['uncommon']} common={t['common']}  "
               f"(gridzilla generated {data['_generated_at_utc']})")
    if args.test:
        print(f"gridzilla_ffma_fetch: OK (--test, no write) — {summary}")
        return 0

    write_atomic(data)
    print(f"gridzilla_ffma_fetch: wrote {OUTPUT_PATH.name} — {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
