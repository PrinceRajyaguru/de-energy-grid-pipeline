# ENTSO-E Dataset Notes — DE-LU Exploration (Phase 1)

Samples pulled: **2026-09-21 (Monday)** and **2026-09-19 (Saturday)**, both
UTC, all 4 datasets (generation, load, price, cross-border flows — now 7
interconnectors: FR/NL/PL/AT/CH/BE/CZ), via `ingestion/fetch_entsoe.py` /
`ingestion/fetch_flows.py` and `ingestion/parse_entsoe.py` /
`ingestion/parse_flows.py`. Both days were re-pulled fresh after every fix
in this document, and everything below is verified against that final data,
not the earlier broken pulls.

## Round 2 — fixes made, re-verified against fresh full re-pulls

Everything in this section was actually implemented and re-tested (both
days re-pulled and re-parsed end to end after every code change) — not just
documented as a plan.

1. **Retry/backoff, all fetch calls.** `entsoe_common.py::get_with_retry`:
   up to 6 attempts total, exponential backoff (2s/4s/8s/16s/32s) on
   non-200 responses and connection errors, `Retry-After` header respected
   on 429 if present. Verified with mocked responses (real API never
   returned a 429 or error in this project) — confirmed it retries
   correctly on 429-with-Retry-After and on persistent HTTP 500, and raises
   loudly (not a silent skip) after all attempts are exhausted. Every retry
   is logged (attempt number, wait time, reason) for visibility during a
   real backfill.
2. **Price fixed: EPEX-only, trimmed to the requested day.** Two issues,
   both fixed in `parse_entsoe.py`:
   - `classification_sequence=2` (EXAA) rows are now dropped from the main
     output and written separately to `day_ahead_prices_exaa_*.json`
     instead of being blended into the average.
   - **New finding during verification**: the price document's own window
     is *wider* than requested — asking for 2026-09-21 returned `Period`s
     spanning `2026-09-20T22:00Z` to `2026-09-22T22:00Z`, touching 3
     calendar UTC days. Without trimming to the requested day, the "daily
     mean" was actually blending 3 different days together. Now trimmed.
   - **Result:** 2026-09-21 EPEX-only mean is **€107.77/MWh** (96 rows,
     clean single day) — this is *not* close to the €165.70 figure quoted
     earlier in this project, and that's expected: the earlier figure was
     computed on data affected by both the window bug and the
     sequence-blending bug, so it wasn't measuring the same thing.
     2026-09-19 (Saturday) came out to **€36.24/MWh** — much lower, which
     is directionally sane (weekend, lower demand).
3. **Generation dual-series (B10) now tagged, not summed.** Every
   generation row for a PsrType that has both an `inBiddingZone_Domain`
   series and an `outBiddingZone_Domain` series now carries a
   `flow_direction` field (`"generation"` or `"consumption"`). B10 (Hydro
   Pumped Storage) is the only PsrType where this actually mattered in our
   samples. Gold-layer total generation must filter to
   `flow_direction="generation"` before summing — documented in Dataset 1
   below.
4. **Silent gap detection added (not fabrication).** `parse_entsoe.py` now
   compares each `TimeSeries`'s actual coverage against the document-level
   requested window (only for generation/load, where 1 TimeSeries = 1
   full-day stream by construction — day-ahead price legitimately has
   multiple `TimeSeries` covering sub-windows by design, so the same check
   there produced false positives and was scoped off). Missing timestamps
   are written to `_gaps_*.json`, never forward-filled or guessed. **B12
   and B17 (Waste) both showed real gaps on BOTH sample days** — 20 and 20
   missing on 2026-09-21, 21 and 8 missing on 2026-09-19 — confirming this
   is a recurring pattern for these two sources, not a one-off fluke.
5. **Flow leading-gap handling fixed and tested.** `parse_flows.py` no
   longer crashes if position 1 is missing. It now looks back to the
   previous calendar day's raw file (if present on disk) and carries
   forward that day's final value; if no previous-day file exists, the
   leading positions are left as `null` (never guessed as 0) and flagged.
   **Still never observed on any real day pulled** (both days, all 7
   neighbors, both directions — position 1 was always explicit), so this
   path is proven only by a hand-built synthetic test
   (`ingestion/test_parse_flows_leading_gap.py`, 2 test cases, both pass) —
   not by real data. Also fixed a related bug found while doing this: flow
   parsing's `total_positions` used to be hardcoded to 92 (the old,
   window-bug value); it's now computed per-file from that file's own
   `Period` start/end, so it would have silently produced wrong output
   after the window fix if left as-is.
6. **Remaining interconnectors added.** Austria, Switzerland, Belgium, and
   Czechia domain codes looked up and verified live the same way as
   before (real `A65` data document returned, not an
   `Acknowledgement_MarketDocument` error) — not copied from a reference
   list without checking:

   | Zone | EIC domain code |
   |---|---|
   | Austria (AT) | `10YAT-APG------L` |
   | Switzerland (CH) | `10YCH-SWISSGRIDZ` |
   | Belgium (BE) | `10YBE----------2` |
   | Czechia (CZ) | `10YCZ-CEPS-----N` |

   All 7 interconnectors (FR/NL/PL/AT/CH/BE/CZ) now pulled, both
   directions, both sample days. Point-compression holds across all of
   them (ranges from 6% to 100% explicit points depending on
   neighbor/direction/day) — same behavior, nothing that behaves
   differently enough to need special-case code.
7. **Full PsrType table.** Already added in the previous round
   (`entsoe_common.py::PSR_TYPE_LABELS`, B01–B25) — carried forward, no
   change needed this round.

### Still open — genuinely needs production-scale testing, not fixable here

- **No `<Reason>`/`A99`/`A91` explicit unavailability marker has ever been
  observed**, across both sample days, all 4 datasets, all 7
  interconnectors. The parsing path for it remains unbuilt. This must be
  verified during the real historical backfill in Phase 1 — it cannot be
  manufactured from 2 days of live data.
- **Point-compression forward-fill has only been verified across 2 days.**
  The logic is sound and tested against real compressed data on both days,
  but 2 days is not enough to be confident no other compression pattern
  exists (e.g. multi-day-long flat runs, or compression interacting with a
  real gap in the same series). Re-verify once scheduled pulls accumulate
  more days.
- **Flow leading-gap fallback is untested against real data** (see point 5
  above) — synthetic-only. The very first scheduled pull that happens to
  hit this case in production should be checked by hand.

## Corrections since the first pass — read this before anything below

1. **Window bug, fixed.** `periodStart`/`periodEnd` used to request
   `...0000` to `...2300` — 23 hours, one short of a full day. Every
   dataset pulled before this fix was missing its last 4 points (92/96).
   Fixed in both fetch scripts to request `...0000` through the *next*
   day's `...0000`. Confirmed: all re-pulled files now show 96 points
   (`PT15M`, 24h × 4) where a full day of data actually exists.
2. **Point-compression is not flow-specific.** Originally flagged only in
   cross-border flow data. Re-tested against the corrected generation/price
   pulls: **5 of 17 generation `TimeSeries` and 2–4 of 4 price
   `TimeSeries` were also compressed** (missing `Point`s for
   unchanged values), on both sample days. `parse_entsoe.py` now
   forward-fills, same approach as `parse_flows.py`. See "Point
   compression" section below — this replaces the earlier "flow data only"
   framing.
3. **`classification_sequence` meaning resolved** (see Dataset 3 section) —
   it is not a revision/republish marker as originally guessed; it
   distinguishes the primary EPEX auction (sequence 1) from a separate EXAA
   auction (sequence 2).
4. **A real, unmarked missing-period case was found** (see "New finding —
   silent partial-day series" below) — the first observed case of ENTSO-E
   data being incomplete with *no* `<Reason>`/error marker at all, which is
   worse than the "might happen" risk noted in the first pass.

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

## Point compression — Points are omitted when the value doesn't change

Confirmed across generation, price, and flow data (not just flow, as first
thought): ENTSO-E only emits a `Point` when its value differs from the
previous one. A source/series that holds steady for hours can show as few
as 1 explicit `Point` for a whole day (e.g. Geothermal generation, which was
a single constant value all day in this sample).

`parse_entsoe.py` and `parse_flows.py` both forward-fill: every skipped
position gets the value of the last explicit point at or before it, so the
flattened JSON has one row per interval, not one row per explicit XML
`Point`. A leading gap (position 1 itself missing, nothing to forward-fill
from) is handled by skipping that row rather than guessing a value — this
case was **not observed** in either sample day, but the code doesn't assume
it can't happen.

## Flattened row shape

All 4 datasets flatten to the same base shape (one row per 15-min interval,
after forward-fill):

```json
{
  "timestamp_utc": "2026-09-21T00:00:00Z",
  "region": "DE-LU",
  "value": 3797.24789,
  "unit": "MAW",
  "resolution": "PT15M",
  "psr_type": "B01",              // generation only
  "psr_type_label": "Biomass",    // generation only, from the full B01-B25 lookup
  "business_type": "A01",         // present on all, meaning varies by dataset
  "currency": "EUR"               // price only
}
```

Cross-border flow rows have a different shape — see Dataset 4 below.

## Dataset 1 — Actual generation per production type (A75 / A16)

- Root element: `GL_MarketDocument`
- **17 `TimeSeries` blocks**, one per `PsrType` (production source) present
  that day for DE-LU. Resolution `PT15M`, 96 points/day where present (post
  window-fix) — but see "New finding" below: not every `TimeSeries`'s
  `Period` necessarily spans the full day.
- `quantity_Measure_Unit.name` = `MAW` (megawatts).
- **Occasionally 2 `TimeSeries` for the same `PsrType`** — observed for B10
  (Hydro Pumped Storage): one with `inBiddingZone_Domain` set (generation
  mode) and one with `outBiddingZone_Domain` set (pumping/consumption
  mode), both `businessType=A01`.

### Dual-series handling (B10 / pumped storage) — Gold-layer rule

**Fixed in `parse_entsoe.py`:** every row now carries a `flow_direction`
field, `"generation"` or `"consumption"`, derived from which domain field
(`inBiddingZone_Domain` vs `outBiddingZone_Domain`) was populated on that
row's `TimeSeries`. Rows are kept separate, never summed together during
ingestion.

**Rule for the Gold-layer balance calculation:** total generation must sum
only `flow_direction="generation"` rows. `flow_direction="consumption"`
rows (pumped storage actively pumping) should be excluded from "total
generation" and, if modeled at all, added to load/demand instead — pumping
is DE-LU consuming power, not producing it. Summing both directions
together for B10 would net generation against pumping and understate both
numbers.

### Full PsrType reference table

`ingestion/entsoe_common.py::PSR_TYPE_LABELS` now holds the complete
official code list (B01–B25), not just the codes seen in one sample —
cross-checked against the open-source `entsoe-py` client's mappings (source
found via search, not invented) since ENTSO-E's own docs page wasn't
directly fetchable this session:

| Code | Label | Code | Label |
|---|---|---|---|
| B01 | Biomass | B14 | Nuclear |
| B02 | Fossil Brown coal/Lignite | B15 | Other renewable |
| B03 | Fossil Coal-derived gas | B16 | Solar |
| B04 | Fossil Gas | B17 | Waste |
| B05 | Fossil Hard coal | B18 | Wind Offshore |
| B06 | Fossil Oil | B19 | Wind Onshore |
| B07 | Fossil Oil shale | B20 | Other |
| B08 | Fossil Peat | B21 | AC Link |
| B09 | Geothermal | B22 | DC Link |
| B10 | Hydro Pumped Storage | B23 | Substation |
| B11 | Hydro Run-of-river and pondage | B24 | Transformer |
| B12 | Hydro Water Reservoir | B25 | Energy storage |
| B13 | Marine | | |

B21–B24 are grid assets, not generation sources — included for completeness
since they're part of the official code list, but shouldn't appear in
generation data in practice.

### Codes observed in our samples

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

Same 16 codes appeared on both sample days (Monday and Saturday) — still not
claiming this is exhaustive for other days/seasons/regions, but consistent
across two different day types.

### Silent partial-day series (B12 / B17) — confirmed recurring, now detected

`B12` (Hydro Water Reservoir)'s `Period` block had `<start>2026-09-21T05:00Z`
instead of the document's overall `2026-09-21T00:00Z` on the first sample
day — i.e. this source reported **zero data for the first 5 hours of the
day**, with no `<Reason>` code, no error marker, nothing to indicate it.

**Confirmed recurring, not a one-off**, after re-pulling and re-checking on
2026-09-19: both `B12` and `B17` (Waste) showed real gaps on **both** sample
days:

| PsrType | 2026-09-21 missing | 2026-09-19 missing |
|---|---|---|
| B12 (Hydro Water Reservoir) | 20 of 96 | 21 of 96 |
| B17 (Waste) | 20 of 96 | 8 of 96 |

**Fixed:** `parse_entsoe.py` now runs gap detection — comparing each
`TimeSeries`'s actual timestamp coverage against the document-level
requested window (not just its own possibly-shorter `Period`) — and writes
any missing timestamps to `data/processed/_gaps_<date>.json` per the rule
in the request: detected and made visible, **not** forward-filled or
interpolated (that's a Silver/Gold modeling decision). This means **the
Silver layer cannot assume every source has 96 rows for every day** — needs
a left join against a full time grid, not an inner join across sources.

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

### `classification_sequence` — resolved

Originally 4 `TimeSeries` per day (2 pairs), split by
`classificationSequence_AttributeInstanceComponent.position` (1 or 2),
looked like it might be an original-vs-corrected republish. **That guess
was wrong.** Checked against a maintained open-source ENTSO-E Home
Assistant integration's issue tracker (ENTSO-E's own guide page returned
HTTP 400 when fetched directly in this session, so this is the best source
found, not a first-party doc):

- **`classification_sequence = 1`** is the **primary day-ahead auction**
  (EPEX Spot / MRC) — this is "the" DE-LU day-ahead price.
- **`classification_sequence = 2`** is a **separate auction run by EXAA**
  (the Austrian energy exchange, which also clears a DE-LU price at its own
  10:15 CE(S)T auction) — a genuinely different market, not a revision of
  sequence 1.

This explains the meaningfully different averages found earlier (€165.70
vs €185.69/MWh) — they're two different markets' clearing prices, not
noise. It also explains the irregular, non-matching point counts between
the two sequences (96/91/92/96) — each auction has its own independent
publishing behavior/compression, unrelated to the other.

**Fixed, not just documented:** `parse_entsoe.py` now filters to
`classification_sequence = 1` (EPEX) in the main output; sequence 2 (EXAA)
rows are written separately to `day_ahead_prices_exaa_<date>.json` instead
of being silently discarded or blended in.

**Second bug found while verifying this fix:** the price document's `Period`
window is *wider* than the requested day — requesting 2026-09-21 returned
`Period`s spanning `2026-09-20T22:00Z` to `2026-09-22T22:00Z`, touching 3
calendar UTC days. Without trimming to the requested day, the "daily mean"
was actually a blend of 3 different days, not just 2 sequences. Also fixed:
`parse_entsoe.py` now trims to rows whose date matches the requested day.

**Verified result** after both fixes: 2026-09-21 EPEX-only mean is
**€107.77/MWh** over a clean 96 rows. This is *not* close to the €165.70
figure quoted earlier in this project — that number was computed on data
affected by the window bug, the sequence-blending bug, and the 3-day
blending bug simultaneously, so it wasn't measuring a comparable thing; no
attempt was made to force the new number to match it. 2026-09-19 (Saturday)
came out to €36.24/MWh, sensibly lower (weekend, lower demand).

## Missing/malformed periods

Still no `<Reason>`/`A99`/`A91` explicit unavailability markers observed in
any file across both sample days, all 4 datasets, all 7 interconnectors —
but real gaps **were** found with no marker at all (B12, B17 — see Dataset 1
above), confirmed recurring on both sample days. Gap-handling logic
**cannot rely on an explicit error/reason code being present** — the only
reliable check is comparing each `TimeSeries`'s actual coverage against the
document-level requested window, which `parse_entsoe.py` now does
(`_gaps_<date>.json`). The `<Reason>`/`A99`/`A91` path itself remains
unbuilt — flagged as a known gap to close during the real historical
backfill, not solvable from 2 days of live sampling.

## Rate limiting

Not encountered in either exploration pass, including the larger round-2
re-pulls (generation/load/price + 7 flow interconnectors × 2 directions ×
2 days ≈ 34 requests total, no 429s). Exponential backoff with proper
`Retry-After` handling on 429 (`entsoe_common.py::get_with_retry`,
6 attempts, 2s/4s/8s/16s/32s schedule) is now in both fetch scripts and
was verified correct against mocked 429/500 responses (see "Round 2"
section above) — cheap insurance, not a response to an observed problem.
Real rate-limit behavior under a real multi-day historical backfill at
scale is still uncharacterized.

## Dataset 4 — Cross-border physical flows (A11)

Pulled to make "shortfall (import dependency)" a measured fact instead of an
inferred guess from load exceeding domestic generation. None of the first 3
datasets measure imports/exports directly.

Both sample days (2026-09-21, 2026-09-19, UTC), for **7 of DE-LU's
interconnectors** — France, Netherlands, Poland, Austria, Switzerland,
Belgium, Czechia — both directions each (28 requests total). Root element
`Publication_MarketDocument` (same family as day-ahead prices), `type=A11`.

### Domain codes — verified, not guessed

Looked these up via search, but a third-party GitHub list was the only
concrete source found for most of them (ENTSO-E's own EIC pages didn't
surface a direct downloadable Area List in this session). Rather than trust
that unverified, **every candidate code was tested against the live API**
(an actual `A65` load request) — a wrong code returns an
`Acknowledgement_MarketDocument` error, not a data document. All 9 below
returned real data:

| Zone | EIC domain code | Verified how |
|---|---|---|
| DE-LU | `10Y1001A1001A82H` | already in use since dataset 1–3 |
| France (FR) | `10YFR-RTE------C` | live API test — OK |
| Netherlands (NL) | `10YNL----------L` | live API test — OK |
| Poland (PL) | `10YPL-AREA-----S` | live API test — OK |
| Austria (AT) | `10YAT-APG------L` | live API test — OK |
| Switzerland (CH) | `10YCH-SWISSGRIDZ` | live API test — OK |
| Belgium (BE) | `10YBE----------2` | live API test — OK |
| Czechia (CZ) | `10YCZ-CEPS-----N` | live API test — OK |
| Denmark DK1 | `10YDK-1--------W` | live API test — OK; flow data now fetched, real data |
| Denmark DK2 | `10Y1001A1001A796` | live API test — OK for load queries, but flow (A11) returns a genuine "no data" `<Reason>` — see "Denmark DK2" section below |

Flow data for DK1/DK2 has now been fetched (9 interconnector zones total).
DK1 returned real data; DK2 correctly returns an error — Germany has no
direct physical interconnector with DK2, only DK1. See the dedicated
section below for the actual `<Reason>` code, the first one observed in
this entire project.

### How direction works — no `flowDirection` field, must query per pair

`A11` is queried as one `in_Domain`/`out_Domain` pair per request — there is
**no parameter that returns "net" flow directly**. To get DE→neighbor you set
`in_Domain=neighbor, out_Domain=DE-LU`; for neighbor→DE you swap them. This
was pulled explicitly both ways per neighbor (`fetch_flows.py`).

Confirmed empirically across all files, all 7 neighbors, both directions,
both days: `businessType` is always `A66` regardless of direction, and
quantities are **never negative** — each direction's file is a plain
non-negative magnitude, not a signed net value. **Net import must be
computed ourselves**: `net_import = (neighbor→DE) - (DE→neighbor)`, done
per-timestamp in `parse_flows.py`.

### Point compression — stress-tested across 2 days, 7 interconnectors

(See the general "Point compression" section near the top — this was
originally found here first, then confirmed to also affect generation and
price data.)

Compression observed on **both** sample days, **all 7 interconnectors**,
varying a lot by direction/neighbor — from 6% to 100% explicit points.
Nothing about any of the 4 newly-added interconnectors (AT/CH/BE/CZ)
behaves differently enough to need special-case code; the same
forward-fill logic handles all of them:

| Neighbor | Mon 2026-09-21 export | Mon import | Sat 2026-09-19 export | Sat import |
|---|---|---|---|---|
| FR | 30/96 (31%) | 96/96 (100%) | 40/96 (42%) | 93/96 (97%) |
| NL | 93/96 (97%) | 6/96 (6%) | 52/96 (54%) | 56/96 (58%) |
| PL | 96/96 (100%) | 16/96 (17%) | 96/96 (100%) | 31/96 (32%) |
| AT | 96/96 (100%) | 96/96 (100%) | 96/96 (100%) | 96/96 (100%) |
| CH | 95/96 (99%) | 96/96 (100%) | 96/96 (100%) | 68/96 (71%) |
| BE | 96/96 (100%) | 55/96 (57%) | 96/96 (100%) | 53/96 (55%) |
| CZ | 96/96 (100%) | 92/96 (96%) | 96/96 (100%) | 47/96 (49%) |

**Leading-gap handling, fixed and tested.** Previously: if position 1 was
missing, values were just left `null` with no fallback. **Fixed:**
`parse_flows.py` now looks back to the previous calendar day's raw file for
the same neighbor/direction (if present on disk) and carries forward that
day's final value; only falls back to `null` if no previous-day file
exists. **Still not observed on real data** — across both days × 7
neighbors × 2 directions (28 series), position 1 was explicit every single
time — so this fix is proven only by a hand-built synthetic test
(`ingestion/test_parse_flows_leading_gap.py`), which covers both the
carry-in and no-carry-in cases and passes. Flagged as genuinely untestable
from the data available so far, not swept under the rug.

`total_positions` is computed per-file from that file's own `Period`
start/end (`(end - start) / resolution`), not hardcoded — this was a bug in
the original version (hardcoded to 92, matching the old window-bug value;
would have silently produced wrong/truncated output after the window fix if
left as-is).

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

## Denmark DK2 — first real `<Reason>` marker observed

Adding Denmark to the flow interconnectors (`fetch_flows.py`) surfaced
something genuinely new: **DK1 pulled real data cleanly** (74–84 explicit
points, same compression pattern as every other neighbor), but **DK2
returned an `Acknowledgement_MarketDocument` error on both directions**:

```
<Reason>
  <code>999</code>
  <text>No matching data found for Data item NET_CROSS_BORDER_PHYSICAL_FLOWS_R3
  [12.1.G] (10Y1001A1001A796, 10Y1001A1001A82H) and interval ...</text>
</Reason>
```

This is the **first real `<Reason>` code seen in this entire project** —
every previous "missing/malformed periods" section said this had never
been observed. Makes physical sense: DK2 (East Denmark) has no direct
transmission link to Germany (it connects via Sweden instead); only DK1
(West Denmark) is directly interconnected. This isn't a bug or a data
quality problem — it's the API correctly saying "this pair has no data
item because no such interconnector exists." `fetch_flows.py` already
handled this gracefully at fetch time (flags it in the log, doesn't crash,
still writes the file).

**Parser now handles it too.** `parse_flows.py::is_acknowledgement_doc`
detects an `Acknowledgement_MarketDocument` root (as opposed to a normal
flow document) before attempting to parse it as one, and skips that
neighbor with the real `<Reason>` text surfaced in the log — rather than
crashing on a missing `TimeSeries` element. DK1 parses normally and joined
`cross_border_flows_<date>.json` alongside the other 7 neighbors (768 rows
= 8 neighbors × 96, once DK1 data exists for a given day); DK2 is present
in `NEIGHBORS` but always resolves to a documented no-data skip.

## Dataset 5 — Forecast & installed capacity (A69 / A71 / A68)

Fetched with `ingestion/fetch_forecast_capacity.py`, parsed with
`ingestion/parse_forecast_capacity.py`. `documentType`/`processType`
combinations were verified live before fetching, same approach as the
domain codes.

**Structurally identical to generation/load** — same `GL_MarketDocument` /
`TimeSeries` / `Period` / `Point` schema, so `parse_forecast_capacity.py`
reuses `parse_entsoe.py`'s `summarize_and_flatten()` directly instead of
duplicating forward-fill / gap-detection / PsrType-labeling logic.

- **Day-ahead wind/solar forecast** (`A69`, `processType=A01`). 3
  `TimeSeries`: Solar (B16), Wind Offshore (B18), Wind Onshore (B19).
  `businessType` differs by source — B16 uses `A94`, B18/B19 use `A93`
  (not yet investigated further; carried through as a field, not acted
  on). Solar's series was compressed (55/96 explicit points — sensible,
  solar forecast is 0 for a long overnight stretch) and forward-filled
  the same way as every other compressed series in this project.
- **Generation forecast, all sources** (`A71`, `processType=A01`). Single
  aggregate `TimeSeries`, no PsrType breakdown — same shape as the load
  dataset. Clean 96/96 points.
- **Installed generation capacity per type** (`A68`, `processType=A33`,
  the year-ahead process type — capacity doesn't have a meaningful
  "actual" or "day-ahead" value). **New resolution: `P1Y`** — not the
  usual `PT15M`. Exactly 1 `Point` per `TimeSeries` (20 `TimeSeries`, one
  per PsrType — the fullest PsrType coverage seen in this project, 20 of
  25 possible codes, including B07/B08/B13 which never appeared in
  generation data and are legitimately 0 for Germany). Uses a full
  calendar-year `periodStart`/`periodEnd` window (Jan 1–Jan 1), not a
  single day.

  **Fix required and made:** `timedelta` can't represent `P1Y` as a fixed
  duration (years vary in length — leap years). `parse_entsoe.py`'s
  `RESOLUTION_MAP` only ever needed fixed-length steps
  (`PT15M`/`PT30M`/`PT60M`/`P1D`) before this. Fixed by special-casing
  `P1Y`: the step is computed as that `TimeSeries`'s own actual period
  duration (`end - start`) rather than a table lookup — correct because
  there's always exactly 1 point per year, so "resolution" here really
  means "the whole period is one point," not a repeating interval.
  Doc-level gap detection is intentionally **not** applied to `P1Y` data
  (it only fires for resolutions in `RESOLUTION_MAP`, which `P1Y` isn't) —
  annual data needs a different notion of "gap" than a 15-min grid, not
  yet designed.

Not yet done: no cross-day comparison (only 2026-09-21 pulled), no
investigation of the A93/A94 businessType distinction, no attempt to join
this against generation/load for the "surprise stress" or "% of capacity
running" use cases mentioned when these were first proposed — that's
Silver/Gold work, out of scope for now.

## Files

- `ingestion/entsoe_common.py` — shared retry/backoff HTTP helper
  (`get_with_retry`, with proper 429/`Retry-After` handling) and the full
  `PSR_TYPE_LABELS` (B01–B25) lookup
- `ingestion/fetch_entsoe.py` — pulls generation/load/price raw XML →
  `data/raw/` (gitignored). Takes an optional `YYYY-MM-DD` arg to pull a
  specific date instead of "yesterday"
- `ingestion/parse_entsoe.py` — flattens generation/load/price XML: forward
  fills compressed Points, tags dual-direction generation rows
  (`flow_direction`), detects (not fabricates) silent gaps, filters price
  to EPEX-only and trims to the requested day, handles the `P1Y` resolution
  → `data/processed/*.json` (gitignored), `_gaps_<date>.json`,
  `day_ahead_prices_exaa_<date>.json` + prints structural summary. Same
  optional date arg. `summarize_and_flatten()` is imported and reused by
  `parse_forecast_capacity.py`
- `ingestion/fetch_flows.py` — pulls cross-border flow raw XML (A11) for 9
  interconnector zones (FR/NL/PL/AT/CH/BE/CZ/DK1/DK2 — DK2 correctly
  returns a "no data" error doc, no direct DE-LU link, see "Denmark DK2"
  section above) → `data/raw/` (gitignored). Same optional date arg
- `ingestion/parse_flows.py` — forward-fills and flattens flow XML,
  computes net import, falls back to the previous day's final value on a
  leading gap, detects and skips Acknowledgement (no-data) documents like
  DK2's → `data/processed/cross_border_flows_<date>.json` (gitignored).
  Same optional date arg. Covers all 9 interconnector zones now
  (FR/NL/PL/AT/CH/BE/CZ/DK1/DK2)
- `ingestion/fetch_forecast_capacity.py` — pulls raw XML for day-ahead
  wind/solar forecast (A69), generation forecast (A71), and installed
  capacity (A68, year-window) → `data/raw/` (gitignored). Same optional
  date arg
- `ingestion/parse_forecast_capacity.py` — flattens all 3 forecast/capacity
  datasets, reusing `parse_entsoe.py`'s `summarize_and_flatten()` →
  `data/processed/{wind_solar_forecast,generation_forecast,installed_capacity}_<date>.json`
  (gitignored), `_forecast_capacity_structure_summary_<date>.json`,
  `_forecast_capacity_gaps_<date>.json`. Same optional date arg
- `ingestion/test_parse_flows_leading_gap.py` — synthetic test for the
  leading-gap path (position 1 missing), since this has never occurred in
  real data pulled so far. Run directly: `python
  test_parse_flows_leading_gap.py`
- `data/processed/_structure_summary_<date>.json`,
  `data/processed/_flows_structure_summary_<date>.json`,
  `data/processed/_gaps_<date>.json` — machine-readable structural/gap
  summaries
