# German Energy Grid Stress Pipeline

## Problem Statement

Germany's renewable-heavy grid creates volatile supply-demand gaps. This pipeline
tracks generation vs. load by source and region to flag oversupply
(negative-price risk) and shortfall (import-dependency) periods — genuine
grid-operator/trader decision support, not a toy dashboard.

## Project Status

| Stage | Status |
|---|---|
| **Ingestion** — pull raw data from the ENTSO-E API, validate its structure, and convert it into clean, flat records | ✅ Complete |
| **Transformation** — Bronze/Silver/Gold layers in Azure Databricks | Not started |
| **Reporting** — Power BI dashboard | Not started |

The ingestion layer has been tested against multiple independent days of
live data, including a full end-to-end run against a day never touched by
any prior code, and is documented in detail in
[`docs/entsoe_dataset_notes.md`](docs/entsoe_dataset_notes.md) — covering
every field, every quirk of the source API, and every data-quality issue
found and how it was resolved (see [Data Quality Checks](#data-quality-checks)
below for a summary).

## Architecture

```
ENTSO-E API
    │
    ▼
Azure Data Factory (scheduled pull)
    │
    ▼
ADLS Gen2 — Bronze (raw XML/JSON)
    │
    ▼
Azure Databricks / PySpark — Silver (cleaned, normalized by source + region)
    │
    ▼
Gold (hourly supply/demand/balance aggregates, Delta tables)
    │
    ▼
Power BI Dashboard
```

- **Bronze**: raw ENTSO-E API responses (XML), landed as-is, partitioned by pull date.
- **Silver**: parsed and normalized — generation by source (wind, solar, nuclear,
  fossil, etc.), load, and day-ahead prices, keyed by timestamp and region.
  Missing/malformed records are handled explicitly (see [Data Quality Checks](#data-quality-checks)).
- **Gold**: hourly aggregated supply-vs-demand balance, with a derived stress
  flag — oversupply when renewable generation exceeds load by a defined
  threshold, shortfall when load exceeds domestic generation.

## Data Source & License

- **Source**: [ENTSO-E Transparency Platform](https://transparency.entsoe.eu) —
  5 datasets pulled for the German bidding zone (DE-LU):
  - Actual generation per production type (`A75`)
  - Actual total load (`A65`)
  - Day-ahead prices (`A44`)
  - Cross-border physical flows (`A11`) — 9 interconnector zones: France,
    Netherlands, Poland, Austria, Switzerland, Belgium, Czechia, and both
    Danish zones (DK1/DK2 — DK2 has no direct physical link to Germany and
    correctly returns a "no data" response)
  - Day-ahead wind/solar forecast (`A69`), generation forecast (`A71`), and
    installed generation capacity per type (`A68`)
- **Access**: RESTful API, registered account + security token required (free).
  To get one:
  1. Register at [transparency.entsoe.eu](https://transparency.entsoe.eu) —
     click **Sign in** → **Register**, and set a password (14+ characters,
     including a special character).
  2. Email **transparency@entsoe.eu** with the subject line
     `RESTful API access` and your registered email address in the body.
     ENTSO-E's helpdesk typically responds within ~3 working days.
  3. Once access is granted, log in and go to **My Account Settings** →
     generate your security token there.
  4. Set it locally as `ENTSOE_API_TOKEN=<your token>` — never commit it (see
     `.gitignore`).
- **License / usage terms**: Data on the Transparency Platform is published
  under EU Regulation 543/2013, and ENTSO-E lists most of it as open for reuse
  (including commercial use) under **CC BY 4.0**, requiring attribution to
  ENTSO-E as the source. Not every dataset on the platform is necessarily
  covered the same way, so before publishing anything derived from this data,
  verify current terms directly on the
  [Transparency Platform's legal terms page](https://transparencyplatform.zendesk.com/hc/en-us/articles/40921911218961-Legal-Terms-and-Conditions)
  rather than relying on this summary.

## Tech Stack

- **Ingestion**: Python, Azure Data Factory (Copy activity, parameterized/scheduled)
- **Storage**: Azure Data Lake Storage Gen2 (hierarchical namespace)
- **Transformation**: Azure Databricks (PySpark), Delta Lake
- **Reporting**: Power BI (DAX measures)
- **Orchestration**: Azure Data Factory pipelines

## How to Run

The commands below run the ingestion layer end to end: pulling raw data from
the ENTSO-E API and converting it into clean, flat JSON records.

```bash
cd ingestion
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# .env (gitignored) needs: ENTSOE_API_TOKEN=<your token>

# Fetch raw XML for a given UTC date (defaults to yesterday if omitted)
python fetch_entsoe.py 2026-09-21              # generation, load, day-ahead prices
python fetch_flows.py 2026-09-21               # cross-border flows, 9 interconnectors
python fetch_forecast_capacity.py 2026-09-21   # forecast + installed capacity

# Parse raw XML into flat JSON rows (same date arg)
python parse_entsoe.py 2026-09-21
python parse_flows.py 2026-09-21
python parse_forecast_capacity.py 2026-09-21

# Run the test suite (includes a synthetic case for an edge condition
# not yet observed in live data)
python test_parse_flows_leading_gap.py
```

Raw XML lands in `data/raw/`, flattened JSON in `data/processed/` — both
gitignored; nothing here is committed to the repo. See
[`docs/entsoe_dataset_notes.md`](docs/entsoe_dataset_notes.md) for the full
field-by-field breakdown of every dataset's shape.

## Layer Explanations

_(fill in with final schema details per layer once Databricks Silver/Gold are
implemented)_

## Data Quality Checks

The source API has several undocumented quirks that would silently corrupt
downstream analysis if left unhandled. Full detail in
[`docs/entsoe_dataset_notes.md`](docs/entsoe_dataset_notes.md); summary of
what the ingestion layer handles:

- **Point compression**: ENTSO-E omits a data point whenever its value is
  unchanged from the previous one (confirmed in generation, price, and flow
  data — not documented anywhere by ENTSO-E, found empirically). Forward-filled
  during parsing so every 15-minute interval gets a row.
- **Silent gaps** (no explicit error marker at all): detected by comparing
  each series' actual coverage against the full requested window — flagged
  in `_gaps_<date>.json`, never fabricated or forward-filled across a real
  gap. Confirmed recurring for two generation sources (Hydro Water Reservoir,
  Waste) across multiple sample days.
- **Real `<Reason>` error documents**: handled where they occur (e.g. DK2 has
  no direct interconnector with Germany and returns a proper "no data" error,
  not silently a crash).
- **Day-ahead price duplication**: prices publish under two different
  `classification_sequence` values — sequence 1 is the primary EPEX auction,
  sequence 2 is a separate EXAA auction. Filtered to sequence 1 only; EXAA
  rows kept in a separate file rather than discarded or blended in.
- **Pumped storage double-counting**: generation sources that both produce
  and consume (pumped hydro) are tagged with a `flow_direction` field so
  Silver-layer aggregation doesn't net generation against pumping.
- **Retry/backoff**: exponential backoff on non-200 responses, with proper
  `Retry-After` handling on HTTP 429, on every API call.

## Dashboard

_(screenshot + link once Phase 3 is complete)_

## Cost Notes

_(what was actually spent — target: $0, using Azure free tier + Databricks
Free Edition + free ENTSO-E API access)_

## Lessons Learned

_(fill in at the end)_
