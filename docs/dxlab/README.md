# DXLab reference docs

Local copies of DXLab Suite PDFs, kept here for offline reference while
designing Grayline's award-tracking model. **These are vendor documents,
not our work** — gitignored so we don't commit them.

## Files

- `DXKeeper.pdf` — DXKeeper user manual (current 18.5.4 as of 2026-05-03).
  Page 126 has the per-award requirements table. Award progress UI pattern
  is on the "Tracking DXCC and TopList Award Progress" sections.
- `SpotCollector.pdf` — SpotCollector user manual (current 10.2.1 as of
  2026-05-03). Spot-database-filter and award-color-highlight logic is in
  the SpotDatabase and Configuration sections.

## Why we have them

DXLab's award tracking model is the most sophisticated in the amateur
radio software space. Their hierarchical scope tracking (Mixed | Phone |
CW | Digital × per-band, all parallel and simultaneously visible) is the
pattern we're adapting for Grayline's per-band award scopes. The
spot-side filtering (highlight color = which award caught the spot,
needed-only filter independent of award definitions) is the architectural
separation we're matching: objectives are central config, spot view
consumes them.

We are NOT lifting DXLab's UI (Fred: *"the UI sucks"*). We are NOT
shipping copies of these PDFs (vendor IP). Reference only.

## Why local instead of just bookmarking

DXLab is a one-author project (AA6YQ / Dave Bernstein). Same risk pattern
as VE3NEA / Steve Haynal: author keeps it close, eventually disengages,
docs become hard to access. Having local copies before that day is just
prudent. The wiki at https://www.dxlabsuite.com/dxlabwiki/ and the live
help at https://www.dxlabsuite.com/dxkeeper/Help/ are the canonical
online sources.

## Re-downloading

```
curl -fsSL -o docs/dxlab/DXKeeper.pdf http://www.ambersoft.com/DXLab/DXKeeper/DXKeeper.pdf
curl -fsSL -o docs/dxlab/SpotCollector.pdf http://www.ambersoft.com/DXLab/SpotCollector/SpotCollector.pdf
```
