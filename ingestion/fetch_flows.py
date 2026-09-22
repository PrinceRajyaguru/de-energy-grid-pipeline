"""
Pulls one day of cross-border physical flow data (A11) between DE-LU and its
neighbors, both directions, for the same day used in fetch_entsoe.py, so
everything lines up on the same timestamps.

Usage:
    python fetch_flows.py                # yesterday (UTC)
    python fetch_flows.py 2026-09-18      # a specific date (UTC)

Domain codes were verified against the live ENTSO-E API (each returned a
real data document, not an Acknowledgement_MarketDocument error), not just
copied from a reference list.

This is an exploration script — raw XML only, no flattening yet.
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
DOMAIN_FR = "10YFR-RTE------C"
DOMAIN_NL = "10YNL----------L"
DOMAIN_PL = "10YPL-AREA-----S"
DOMAIN_AT = "10YAT-APG------L"
DOMAIN_CH = "10YCH-SWISSGRIDZ"
DOMAIN_BE = "10YBE----------2"
DOMAIN_CZ = "10YCZ-CEPS-----N"
DOMAIN_DK1 = "10YDK-1--------W"
DOMAIN_DK2 = "10Y1001A1001A796"

NEIGHBORS = {
    "FR": DOMAIN_FR,
    "NL": DOMAIN_NL,
    "PL": DOMAIN_PL,
    "AT": DOMAIN_AT,
    "CH": DOMAIN_CH,
    "BE": DOMAIN_BE,
    "CZ": DOMAIN_CZ,
    "DK1": DOMAIN_DK1,
    "DK2": DOMAIN_DK2,
}


def window_for(target_date: date) -> tuple[str, str]:
    """Full UTC day window: target 00:00 through the NEXT day's 00:00.
    Same bug fix as fetch_entsoe.py — this used to end at 23:00, one hour
    short, silently dropping the last 4 points per TimeSeries."""
    start = f"{target_date.strftime('%Y%m%d')}0000"
    end_date = target_date + timedelta(days=1)
    end = f"{end_date.strftime('%Y%m%d')}0000"
    return start, end


def fetch(name: str, in_domain: str, out_domain: str, period_start: str, period_end: str, target_date: date) -> Path:
    token = os.environ.get("ENTSOE_API_TOKEN")
    if not token:
        raise RuntimeError("ENTSOE_API_TOKEN not set in .env")

    params = {
        "securityToken": token,
        "documentType": "A11",
        "in_Domain": in_domain,
        "out_Domain": out_domain,
        "periodStart": period_start,
        "periodEnd": period_end,
    }

    resp = get_with_retry(API_BASE, params)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DIR / f"flow_{name}_{target_date.strftime('%Y%m%d')}.xml"
    out_path.write_bytes(resp.content)
    is_error = b"Acknowledgement_MarketDocument" in resp.content
    flag = " [ACKNOWLEDGEMENT/ERROR DOC]" if is_error else ""
    print(f"[{name}] saved {len(resp.content):,} bytes -> {out_path.relative_to(REPO_ROOT)}{flag}")
    return out_path


def main():
    if len(sys.argv) > 1:
        target_date = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    else:
        target_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    period_start, period_end = window_for(target_date)
    print(f"Fetching DE-LU cross-border flows for {target_date.isoformat()} (UTC window {period_start}-{period_end})")
    for label, domain in NEIGHBORS.items():
        # DE -> neighbor (export)
        fetch(f"DE_to_{label}", in_domain=domain, out_domain=DOMAIN_DE_LU,
              period_start=period_start, period_end=period_end, target_date=target_date)
        # neighbor -> DE (import)
        fetch(f"{label}_to_DE", in_domain=DOMAIN_DE_LU, out_domain=domain,
              period_start=period_start, period_end=period_end, target_date=target_date)


if __name__ == "__main__":
    main()
