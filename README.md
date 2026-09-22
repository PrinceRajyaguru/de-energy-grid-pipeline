# German Energy Grid Stress Pipeline

## Problem Statement

Germany's renewable-heavy grid creates volatile supply-demand gaps. This pipeline
tracks generation vs. load by source and region to flag oversupply
(negative-price risk) and shortfall (import-dependency) periods — genuine
grid-operator/trader decision support, not a toy dashboard.

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
  actual generation per production type, total load, and day-ahead prices for
  the German bidding zone (DE-LU).
- **Access**: RESTful API, registered account + security token required.
- **License / usage terms**: _(fill in — see ENTSO-E Transparency Platform Terms
  of Use before publishing derived data)._

## Tech Stack

- **Ingestion**: Python, Azure Data Factory (Copy activity, parameterized/scheduled)
- **Storage**: Azure Data Lake Storage Gen2 (hierarchical namespace)
- **Transformation**: Azure Databricks (PySpark), Delta Lake
- **Reporting**: Power BI (DAX measures)
- **Orchestration**: Azure Data Factory pipelines

## How to Run

_(fill in once Phase 1–2 are built: env setup, ADF pipeline trigger, Databricks
notebook execution order, Power BI refresh)_

## Layer Explanations

_(fill in with final schema details per layer once implemented)_

## Data Quality Checks

_(document how missing/malformed ENTSO-E records are handled — e.g. missing
generation-type entries, XML parsing failures, timestamp gaps)_

## Dashboard

_(screenshot + link once Phase 3 is complete)_

## Cost Notes

_(what was actually spent — target: $0, using Azure free tier + Databricks
Free Edition + free ENTSO-E API access)_

## Lessons Learned

_(fill in at the end)_
