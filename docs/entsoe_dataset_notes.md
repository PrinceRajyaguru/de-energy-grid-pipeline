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

## Files

- `ingestion/fetch_entsoe.py` — pulls raw XML → `data/raw/` (gitignored)
- `ingestion/parse_entsoe.py` — flattens XML → `data/processed/*.json`
  (gitignored) + prints structural summary
- `data/processed/_structure_summary.json` — machine-readable version of the
  summary printed above, per dataset
