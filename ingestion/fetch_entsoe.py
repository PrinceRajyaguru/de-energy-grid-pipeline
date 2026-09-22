"""
Pulls one day of ENTSO-E Transparency Platform data for the DE-LU bidding zone
and saves the raw XML responses to data/raw/.

This is an exploration script (Phase 1) — no parsing/flattening logic yet.
Run this first, inspect the raw XML by hand, THEN build the parser.
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw"

load_dotenv(REPO_ROOT / ".env")

API_BASE = "https://web-api.tp.entsoe.eu/api"
DOMAIN_DE_LU = "10Y1001A1001A82H"

# ENTSO-E period boundaries are UTC, format yyyyMMddHHmm.
# "Yesterday" is computed in UTC, not local time — do not swap this for
# datetime.now() without tzinfo, or the window will drift by your UTC offset.
_now_utc = datetime.now(timezone.utc)
_yesterday_utc = (_now_utc - timedelta(days=1)).date()
PERIOD_START = f"{_yesterday_utc.strftime('%Y%m%d')}0000"
PERIOD_END = f"{_yesterday_utc.strftime('%Y%m%d')}2300"

DATASETS = {
    "generation_per_type": {
        "documentType": "A75",
        "processType": "A16",
        "in_Domain": DOMAIN_DE_LU,
    },
    "actual_total_load": {
        "documentType": "A65",
        "processType": "A16",
        "outBiddingZone_Domain": DOMAIN_DE_LU,
    },
    "day_ahead_prices": {
        "documentType": "A44",
        "in_Domain": DOMAIN_DE_LU,
        "out_Domain": DOMAIN_DE_LU,
    },
}


def fetch(name: str, extra_params: dict) -> Path:
    token = os.environ.get("ENTSOE_API_TOKEN")
    if not token:
        raise RuntimeError("ENTSOE_API_TOKEN not set in .env")

    params = {
        "securityToken": token,
        "periodStart": PERIOD_START,
        "periodEnd": PERIOD_END,
        **extra_params,
    }

    resp = requests.get(API_BASE, params=params, timeout=30)
    resp.raise_for_status()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DIR / f"{name}_{_yesterday_utc.strftime('%Y%m%d')}.xml"
    out_path.write_bytes(resp.content)
    print(f"[{name}] saved {len(resp.content):,} bytes -> {out_path.relative_to(REPO_ROOT)}")
    return out_path


def main():
    print(f"Fetching DE-LU data for {_yesterday_utc.isoformat()} (UTC window {PERIOD_START}-{PERIOD_END})")
    for name, params in DATASETS.items():
        fetch(name, params)


if __name__ == "__main__":
    main()
