# ENTSO-E Dataset Notes — DE-LU Exploration (Phase 1)

Sample pulled: **2026-09-21 (UTC)**, one day, all three datasets, via
`ingestion/fetch_entsoe.py` and `ingestion/parse_entsoe.py`.

## Timezone handling — read this first

All ENTSO-E timestamps (`periodStart`/`periodEnd` request params, and every
`<start>`/`<end>` in the response) are **UTC**, not CET/CEST. There is no UTC
offset in the XML — a value like `2026-09-21T00:00Z` is already UTC.

This matters for hourly aggregation later: naively joining these timestamps
against a "local day" (Europe/Berlin) boundary will be off by 1 or 2 hours
depending on DST. The Bronze→Silver layer should either:
- keep everything in UTC end-to-end and only convert to CET/CEST at the
  presentation layer (Power BI), or
- explicitly convert with DST-aware logic (`zoneinfo("Europe/Berlin")`), never
  a fixed offset.

`fetch_entsoe.py` computes "yesterday" using `datetime.now(timezone.utc)`
specifically to avoid local-clock drift.

## Point → timestamp reconstruction

The XML does not give a timestamp per data point — it gives a `Period` with a
`start` and a `resolution` (e.g. `PT15M`), and each `Point` has a 1-based
`position`. The actual timestamp is:

```
timestamp = period.start + (position - 1) * resolution
```

This is implemented in `parse_entsoe.py::summarize_and_flatten`.

## Flattened row shape

All three datasets flatten to the same base shape (one row per point):

```json
{
  "timestamp_utc": "2026-09-21T00:00:00Z",
  "region": "DE-LU",
  "value": 3797.24789,
  "unit": "MAW",
  "resolution": "PT15M",
  "psr_type": "B01",        // generation only
  "business_type": "A01",   // present on all, meaning varies by dataset
  "currency": "EUR"         // price only
}
```

## Dataset 1 — Actual generation per production type (A75 / A16)

- Root element: `GL_MarketDocument`
- **17 `TimeSeries` blocks**, one per `PsrType` (production source) present
  that day for DE-LU. Each has exactly one `Period`, resolution `PT15M`.
- 92 points per series in this sample (see "23 vs 24 hours" note below).
- `quantity_Measure_Unit.name` = `MAW` (megawatts).

### PsrType codes observed in this sample

| Code | Meaning |
|------|---------|
| B01 | Biomass |
| B02 | Fossil Brown coal/Lignite |
| B03 | Fossil Coal-derived gas |
| B04 | Fossil Gas |
| B05 | Fossil Hard coal |
| B06 | Fossil Oil |
| B09 | Geothermal |
| B10 | Hydro Pumped Storage |
| B11 | Hydro Run-of-river and poundage |
| B12 | Hydro Water Reservoir |
| B15 | Other renewable |
| B16 | Solar |
| B17 | Waste |
| B18 | Wind Offshore |
| B19 | Wind Onshore |
| B20 | Other |

(Full ENTSO-E PsrType code list has more entries — e.g. B07 Fossil Hard coal
variants, B08 Fossil Oil shale, B13/B14 marine/nuclear — but only the 16 above
actually appeared in this one-day DE-LU sample. Don't assume this list is
exhaustive for other days/regions.)

## Dataset 2 — Actual total load (A65 / A16)

- Root element: `GL_MarketDocument` (same schema family as generation).
- **1 `TimeSeries`**, single `Period`, resolution `PT15M`, unit `MAW`.
- `businessType` = `A04` (consumption).
- Much simpler than generation — no PsrType breakdown, just one aggregate
  load curve for the whole bidding zone.

## Dataset 3 — Day-ahead prices (A44)

- Root element: `Publication_MarketDocument` (**different schema** from the
  other two — different XML namespace, different field names:
  `price.amount` instead of `quantity`, `period.timeInterval` instead of
  `time_Period.timeInterval`).
- Unit: `price_Measure_Unit.name` = `MWH`, `currency_Unit.name` = `EUR`.
- Resolution: `PT15M` in this sample (ENTSO-E migrated day-ahead prices from
  hourly to 15-minute resolution in 2025 — don't assume `PT60M` if pulling
  older historical data).

### ⚠️ Unresolved irregularity — multiple overlapping TimeSeries per day

Unlike generation/load, the price response contained **4 `TimeSeries`
blocks** for a request spanning one nominal day, in two pairs covering
overlapping/adjacent windows, distinguished by a field called
`classificationSequence_AttributeInstanceComponent.position` (values `1` and
`2`):

| # | classification_sequence | period start (UTC) | period end (UTC) | point count |
|---|---|---|---|---|
| 0 | 2 | 2026-09-20T22:00Z | 2026-09-21T22:00Z | 96 |
| 1 | 1 | 2026-09-20T22:00Z | 2026-09-21T22:00Z | 91 |
| 2 | 2 | 2026-09-21T22:00Z | 2026-09-22T22:00Z | 92 |
| 3 | 1 | 2026-09-21T22:00Z | 2026-09-22T22:00Z | 96 |

Two things stand out:
1. Two series per day-window instead of one, split by `classification_sequence`.
2. Point counts are irregular (96/91/92/96) instead of a clean 96-per-day
   (24h × 4 × 15min). Not yet understood why — possibly an artifact of
   ENTSO-E's 2025 hourly→15-min resolution migration (some days may still
   partially publish in a legacy shape), possibly something else.

**Decision for this phase:** not deduplicating or selecting between these.
`parse_entsoe.py` preserves `classification_sequence` as a field on every
price row so nothing is silently discarded or double-counted downstream.
Before building the Silver layer for prices, this needs real investigation —
pull a few more days (including a day known to be mid-migration) and check
the ENTSO-E API documentation / forum for what `classificationSequence`
means. Do not build price aggregation logic on top of this data until that's
resolved, or Bronze→Silver row counts for prices could double.

## "23 vs 24 hours" — request window boundary

`fetch_entsoe.py` requests `periodStart=...0000` to `periodEnd=...2300`
(23:00, not 24:00/next-day-00:00), so generation and load each came back with
**92 points** (23h × 4 × 15min) instead of a full day's 96. This is a
request-construction artifact, not an API quirk — the fetch script's window
should be widened to the next day's `0000` to get a clean 96-point day. Left
as-is for this exploration pass since the goal was inspecting shape, not
correctness of window bounds. Fix before this feeds anything downstream.

## Missing/malformed periods

None observed in this sample — no `<Reason>` codes, no `A99`/`A91`
unavailability markers in any of the three files. This is a single clean day;
gap-handling logic (e.g. ENTSO-E's `<Reason><code>` block for missing data)
has **not** been tested against a real example yet and should not be assumed
solved.

## Rate limiting

Not encountered in this exploration (3 sequential GET requests, no 429s).
Not yet characterized — no retry/backoff logic exists yet in
`fetch_entsoe.py`. Needed before any multi-day historical backfill.

## Dataset 4 — Cross-border physical flows (A11)

Pulled to make "shortfall (import dependency)" a measured fact instead of an
inferred guess from load exceeding domestic generation. None of the first 3
datasets measure imports/exports directly.

Same day as the other datasets (2026-09-21, UTC), for DE-LU's 3 biggest
interconnectors: **France, Netherlands, Poland**, both directions each (6
requests total). Root element `Publication_MarketDocument` (same family as
day-ahead prices), `type=A11`.

### Domain codes — verified, not guessed

Looked these up via search, but a third-party GitHub list was the only
concrete source found (ENTSO-E's own EIC pages didn't surface a direct
downloadable Area List in this session). Rather than trust that unverified,
**every candidate code was tested against the live API** (an actual `A65`
load request) — a wrong code returns an `Acknowledgement_MarketDocument`
error, not a data document. All 5 below returned real data:

| Zone | EIC domain code | Verified how |
|---|---|---|
| DE-LU | `10Y1001A1001A82H` | already in use since dataset 1–3 |
| France (FR) | `10YFR-RTE------C` | live API test — OK |
| Netherlands (NL) | `10YNL----------L` | live API test — OK |
| Denmark DK1 | `10YDK-1--------W` | live API test — OK (code only, not pulled) |
| Denmark DK2 | `10Y1001A1001A796` | live API test — OK (code only, not pulled) |
| Poland (PL) | `10YPL-AREA-----S` | live API test — OK |

Denmark's codes are confirmed and recorded here for a future pull; flow data
itself was not fetched for DK1/DK2 this round (kept to 3 interconnectors to
stay manageable).

### How direction works — no `flowDirection` field, must query per pair

`A11` is queried as one `in_Domain`/`out_Domain` pair per request — there is
**no parameter that returns "net" flow directly**. To get DE→neighbor you set
`in_Domain=neighbor, out_Domain=DE-LU`; for neighbor→DE you swap them. This
was pulled explicitly both ways per neighbor (`fetch_flows.py`).

Confirmed empirically across all 6 files: `businessType` is always `A66`
regardless of direction, and quantities are **never negative** — each
direction's file is a plain non-negative magnitude, not a signed net value.
**Net import must be computed ourselves**: `net_import = (neighbor→DE) -
(DE→neighbor)`, done per-timestamp in `parse_flows.py`.

### ⚠️ Structural surprise — Points are compressed, not one-per-interval

This is different from generation/load/price, and important enough that
flattening logic was paused and shown before being written (per the "stop
and check" rule).

Generation and load data had exactly 92 `Point` elements per `TimeSeries`
(one per 15-min interval in the request window) — every position present.
Flow data does **not**: a `Point` is only emitted when its value *changes*
from the previous one. Example from `flow_NL_to_DE_20260921.xml` (full file,
only 6 `Point`s for a whole day):

```
position=1  quantity=0          <- flow is 0 from position 1 through 28
position=29 quantity=273.70667  <- changes here
position=30 quantity=539.61667
position=31 quantity=731.68999
position=32 quantity=535.82667
position=33 quantity=0          <- drops to 0, stays 0 through position 92
```

Observed compression varied a lot by direction/neighbor in this sample:

| Neighbor | export (DE→n) explicit points | import (n→DE) explicit points |
|---|---|---|
| FR | 30/92 (33%) | 92/92 (100%) |
| NL | 89/92 (97%) | 6/92 (7%) |
| PL | 92/92 (100%) | 12/92 (13%) |

**Decision:** `parse_flows.py` forward-fills every skipped position with the
last explicit value, producing one row per 15-min interval (verified by
spot-checking output against the raw XML — matches exactly). This assumes
"missing position = value unchanged," which fits every case inspected here,
but has only been confirmed on one day's sample. If a future pull ever shows
position 1 missing (no value to forward-fill from), `parse_flows.py` raises
rather than guessing a default.

**Open question, not yet resolved:** whether this same compression applies
to generation/load/price data on days where a value happens to stay exactly
constant across intervals (we just never saw it in the one sample pulled,
because those values fluctuate constantly). If Bronze/Silver parsing for
those 3 datasets is ever built assuming "always 92 explicit points," that
assumption should be re-tested, not carried over from this one day.

### Resolution — matches the other datasets

`PT15M`, same as generation/load/day-ahead prices. No resolution mismatch to
worry about for Silver-layer alignment — once forward-filled, all 4 datasets
share the same 15-minute grid.

### Flattened row shape

```json
{
  "timestamp_utc": "2026-09-21T07:00:00Z",
  "region": "DE-LU",
  "neighbor": "NL",
  "export_mw": 0.0,
  "import_mw": 273.70667,
  "net_import_mw": 273.70667,
  "unit": "MAW",
  "resolution": "PT15M"
}
```

### Missing/malformed periods

None of the usual `<Reason>`/`A99`/`A91` markers seen — same as the other 3
datasets. The Point-compression behavior above is a distinct, separate thing
from missing-data markers (it's a valid, complete series, just sparsely
encoded).

## Future additions (documented, not yet pulled)

- **Day-ahead generation forecast** (`documentType=A69`/`A71`), especially
  wind/solar. Would let the pipeline flag "surprise" stress — actual
  generation diverging from what was forecast — rather than only comparing
  raw generation against a static threshold. Not pulled yet; needs the same
  structural-exploration pass as the 4 datasets above before assuming its
  shape.
- **Installed generation capacity per type** (`documentType=A68`). Would let
  the pipeline compute *% of available capacity running* per source instead
  of raw MW, which is a more meaningful stress signal (e.g. "gas at 90% of
  installed capacity" says more than "gas at 4,000 MW" on its own). Not
  pulled yet.

## Files

- `ingestion/fetch_entsoe.py` — pulls generation/load/price raw XML →
  `data/raw/` (gitignored)
- `ingestion/parse_entsoe.py` — flattens generation/load/price XML →
  `data/processed/*.json` (gitignored) + prints structural summary
- `ingestion/fetch_flows.py` — pulls cross-border flow raw XML (A11) →
  `data/raw/` (gitignored)
- `ingestion/parse_flows.py` — forward-fills and flattens flow XML,
  computes net import → `data/processed/cross_border_flows.json`
  (gitignored)
- `data/processed/_structure_summary.json`,
  `data/processed/_flows_structure_summary.json` — machine-readable
  structural summaries
