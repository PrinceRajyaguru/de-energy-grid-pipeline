"""
Pulls one day of ENTSO-E Transparency Platform data for the DE-LU bidding
zone and saves the raw XML responses to data/raw/.

Usage:
    python fetch_entsoe.py                # yesterday (UTC)
    python fetch_entsoe.py 2026-09-18      # a specific date (UTC)

This is an exploration script (Phase 1) — no parsing/flattening logic here.
Run this first, inspect the raw XML by hand, THEN build the parser.
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


def window_for(target_date: date) -> tuple[str, str]:
    """Full UTC day window: target 00:00 through the NEXT day's 00:00.
    Bug fix: this used to end at target's 23:00, one hour short of a full
    day, which silently dropped the last 4 points (92 instead of 96 per
    15-min TimeSeries) from every dataset pulled so far."""
    start = f"{target_date.strftime('%Y%m%d')}0000"
    end_date = target_date + timedelta(days=1)
    end = f"{end_date.strftime('%Y%m%d')}0000"
    return start, end


def datasets_for(domain: str) -> dict:
    return {
        "generation_per_type": {
            "documentType": "A75",
            "processType": "A16",
            "in_Domain": domain,
        },
        "actual_total_load": {
            "documentType": "A65",
            "processType": "A16",
            "outBiddingZone_Domain": domain,
        },
        "day_ahead_prices": {
            "documentType": "A44",
            "in_Domain": domain,
            "out_Domain": domain,
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
    print(f"[{name}] saved {len(resp.content):,} bytes -> {out_path.relative_to(REPO_ROOT)}")
    return out_path


def main():
    if len(sys.argv) > 1:
        target_date = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    else:
        # "Yesterday" computed in UTC, not local time — do not swap this for
        # datetime.now() without tzinfo, or the window will drift by your
        # UTC offset.
        target_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    period_start, period_end = window_for(target_date)
    print(f"Fetching DE-LU data for {target_date.isoformat()} (UTC window {period_start}-{period_end})")
    for name, params in datasets_for(DOMAIN_DE_LU).items():
        fetch(name, params, period_start, period_end, target_date)


if __name__ == "__main__":
    main()
