"""
Pulls one day of cross-border physical flow data (A11) between DE-LU and its
neighbors, both directions, for the same day used in fetch_entsoe.py, so
everything lines up on the same timestamps.

Domain codes were verified against the live ENTSO-E API (each returned a
real data document, not an Acknowledgement_MarketDocument error), not just
copied from a reference list.

This is an exploration script — raw XML only, no flattening yet.
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
DOMAIN_FR = "10YFR-RTE------C"
DOMAIN_NL = "10YNL----------L"
DOMAIN_PL = "10YPL-AREA-----S"
# Verified but not pulled this round: Denmark DK1 (10YDK-1--------W),
# Denmark DK2 (10Y1001A1001A796) — split zone, deferred to keep this batch
# to 3 interconnectors.

NEIGHBORS = {
    "FR": DOMAIN_FR,
    "NL": DOMAIN_NL,
    "PL": DOMAIN_PL,
}

# Same day/window as fetch_entsoe.py, computed the same way (UTC).
_now_utc = datetime.now(timezone.utc)
_yesterday_utc = (_now_utc - timedelta(days=1)).date()
PERIOD_START = f"{_yesterday_utc.strftime('%Y%m%d')}0000"
PERIOD_END = f"{_yesterday_utc.strftime('%Y%m%d')}2300"


def fetch(name: str, in_domain: str, out_domain: str) -> Path:
    token = os.environ.get("ENTSOE_API_TOKEN")
    if not token:
        raise RuntimeError("ENTSOE_API_TOKEN not set in .env")

    params = {
        "securityToken": token,
        "documentType": "A11",
        "in_Domain": in_domain,
        "out_Domain": out_domain,
        "periodStart": PERIOD_START,
        "periodEnd": PERIOD_END,
    }

    resp = requests.get(API_BASE, params=params, timeout=45)
    resp.raise_for_status()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DIR / f"flow_{name}_{_yesterday_utc.strftime('%Y%m%d')}.xml"
    out_path.write_bytes(resp.content)
    is_error = b"Acknowledgement_MarketDocument" in resp.content
    flag = " [ACKNOWLEDGEMENT/ERROR DOC]" if is_error else ""
    print(f"[{name}] saved {len(resp.content):,} bytes -> {out_path.relative_to(REPO_ROOT)}{flag}")
    return out_path


def main():
    print(f"Fetching DE-LU cross-border flows for {_yesterday_utc.isoformat()} (UTC window {PERIOD_START}-{PERIOD_END})")
    for label, domain in NEIGHBORS.items():
        # DE -> neighbor (export)
        fetch(f"DE_to_{label}", in_domain=domain, out_domain=DOMAIN_DE_LU)
        # neighbor -> DE (import)
        fetch(f"{label}_to_DE", in_domain=DOMAIN_DE_LU, out_domain=domain)


if __name__ == "__main__":
    main()
