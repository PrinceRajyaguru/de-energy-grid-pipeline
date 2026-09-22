"""
Pulls raw XML for the two datasets previously documented as "future
additions, not yet pulled" in docs/entsoe_dataset_notes.md:

- Day-ahead wind/solar generation forecast (A69, processType A01)
- Installed generation capacity per type (A68, processType A33 — year-ahead,
  the standard process type for this document; capacity doesn't have a
  meaningful "actual" or "day-ahead" value the way generation/load/price do)

Also pulls A71 (aggregated generation forecast, all sources combined, not
just wind/solar) alongside A69, since both were verified live and give a
fuller picture at negligible extra cost.

Fetch only — this is raw XML to disk, same as fetch_entsoe.py and
fetch_flows.py. No parsing/flattening here; per project convention, the raw
structure should be inspected by hand before any parser is written.

Usage:
    python fetch_forecast_capacity.py                # yesterday (UTC)
    python fetch_forecast_capacity.py 2026-09-18      # a specific date (UTC)

Note: A68 (installed capacity) is a yearly figure, not a daily one — the
periodStart/periodEnd window still needs to span the full target year for
ENTSO-E to return data (a single day's window may return an empty or
minimal document). This is called out here rather than silently pulling a
window that might not make sense for this document type; verify the
response before building anything on top of it.
"""

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from entsoe_common import get_with_retry

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw"

load_dotenv(REPO_ROOT / ".env")

API_BASE = "https://web-api.tp.entsoe.eu/api"
DOMAIN_DE_LU = "10Y1001A1001A82H"


def day_window(target_date: date) -> tuple[str, str]:
    start = f"{target_date.strftime('%Y%m%d')}0000"
    end_date = target_date + timedelta(days=1)
    end = f"{end_date.strftime('%Y%m%d')}0000"
    return start, end


def year_window(target_date: date) -> tuple[str, str]:
    start = f"{target_date.year}01010000"
    end = f"{target_date.year + 1}01010000"
    return start, end


DATASETS = {
    "wind_solar_forecast": {
        "params": {"documentType": "A69", "processType": "A01", "in_Domain": DOMAIN_DE_LU},
        "window": "day",
    },
    "generation_forecast": {
        "params": {"documentType": "A71", "processType": "A01", "in_Domain": DOMAIN_DE_LU},
        "window": "day",
    },
    "installed_capacity": {
        "params": {"documentType": "A68", "processType": "A33", "in_Domain": DOMAIN_DE_LU},
        "window": "year",
    },
}


def fetch(name: str, extra_params: dict, period_start: str, period_end: str, target_date: date) -> Path:
    token = os.environ.get("ENTSOE_API_TOKEN")
    if not token:
        raise RuntimeError("ENTSOE_API_TOKEN not set in .env")

    params = {
        "securityToken": token,
        "periodStart": period_start,
        "periodEnd": period_end,
        **extra_params,
    }

    resp = get_with_retry(API_BASE, params)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DIR / f"{name}_{target_date.strftime('%Y%m%d')}.xml"
    out_path.write_bytes(resp.content)
    is_error = b"Acknowledgement_MarketDocument" in resp.content
    flag = " [ACKNOWLEDGEMENT/ERROR DOC — check params/window]" if is_error else ""
    print(f"[{name}] saved {len(resp.content):,} bytes -> {out_path.relative_to(REPO_ROOT)}{flag}")
    return out_path


def main():
    if len(sys.argv) > 1:
        target_date = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    else:
        target_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    print(f"Fetching DE-LU forecast/capacity data for {target_date.isoformat()} (UTC)")
    for name, spec in DATASETS.items():
        period_start, period_end = day_window(target_date) if spec["window"] == "day" else year_window(target_date)
        fetch(name, spec["params"], period_start, period_end, target_date)


if __name__ == "__main__":
    main()
