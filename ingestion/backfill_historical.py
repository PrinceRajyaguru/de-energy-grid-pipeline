"""
One-off historical backfill: pulls raw ENTSO-E XML for DE-LU and uploads it
straight to the Azure "bronze" container, using the same
{datasetType}/{datasetType}_{YYYY-MM-DD}.xml layout the daily ADF pipeline
(pl_ENTSOE_Ingestion) writes. The Databricks notebooks then process the
backfilled days like any other Bronze data.

This is a MANUAL utility, not part of the scheduled pipeline. It is not
wired into the ADF trigger or the Databricks Job. Run it once to seed
history; the daily automation handles new days after that.

Datasets (the same 21 as ADF's datasetConfigs): generation, load, price,
16 cross-border flows (8 neighbours x import/export), forecast_wind_solar,
forecast_generation. Annual installed capacity is not pulled.

Date range: START_DATE through yesterday (UTC), inclusive. Yesterday is
computed at run time.

Usage (from the repo root or ingestion/):
    pip install -r ingestion/requirements.txt
    python ingestion/backfill_historical.py

Environment (read from the repo-root .env, which is gitignored):
    ENTSOE_API_TOKEN     ENTSO-E Transparency Platform API token
    ADLS_STORAGE_KEY     access key for the stdeenergygriddev storage account

Behaviour:
- Calls run one at a time with a short pause between them, on top of the
  retry/backoff in entsoe_common.get_with_retry.
- A failure never aborts the run; it is logged and the run moves on.
- Outcomes are logged distinctly:
    OK           fetched and uploaded
    NO_DATA      ENTSO-E answered but has no data for that day/dataset
                 (a real gap, or not yet published). Nothing is uploaded.
    FETCH_FAILED transient/network/HTTP problem after all retries
    UPLOAD_FAILED fetched fine but the blob upload failed
- Re-running overwrites existing blobs, so it is safe to repeat.
"""

import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv

from entsoe_common import get_with_retry
from fetch_entsoe import API_BASE, DOMAIN_DE_LU, REPO_ROOT, datasets_for, window_for
from fetch_flows import NEIGHBORS as ALL_NEIGHBORS
from fetch_forecast_capacity import DATASETS as FORECAST_CAPACITY_DATASETS

load_dotenv(REPO_ROOT / ".env")

START_DATE = date(2026, 8, 15)
STORAGE_ACCOUNT = "stdeenergygriddev"
CONTAINER = "bronze"
DELAY_SECONDS = 0.75

# ADF pulls 8 neighbours; fetch_flows.py also knows DK2, which ADF does not use.
ADF_NEIGHBORS = ["FR", "NL", "PL", "AT", "CH", "BE", "CZ", "DK1"]


def build_datasets() -> dict:
    """datasetType -> ENTSO-E query params, mirroring ADF's datasetConfigs."""
    base = datasets_for(DOMAIN_DE_LU)
    datasets = {
        "generation": base["generation_per_type"],
        "load": base["actual_total_load"],
        "price": base["day_ahead_prices"],
    }
    for label in ADF_NEIGHBORS:
        neighbor = ALL_NEIGHBORS[label]
        # Same in/out convention as ADF: "import" is in=DE-LU, out=neighbour.
        datasets[f"flow_{label}_import"] = {
            "documentType": "A11", "in_Domain": DOMAIN_DE_LU, "out_Domain": neighbor}
        datasets[f"flow_{label}_export"] = {
            "documentType": "A11", "in_Domain": neighbor, "out_Domain": DOMAIN_DE_LU}
    datasets["forecast_wind_solar"] = FORECAST_CAPACITY_DATASETS["wind_solar_forecast"]["params"]
    datasets["forecast_generation"] = FORECAST_CAPACITY_DATASETS["generation_forecast"]["params"]
    return datasets


def no_data_reason(body: bytes):
    """Return ENTSO-E's reason text if the body is an Acknowledgement saying
    there is no matching data, else None."""
    if b"Acknowledgement_MarketDocument" not in body:
        return None
    text = body.decode("utf-8", errors="replace")
    if "No matching data" in text or re.search(r"<code>999</code>", text):
        m = re.search(r"<text>(.*?)</text>", text, re.S)
        return m.group(1).strip() if m else "No matching data found"
    return None


def fetch_one(token: str, params: dict, target_date: date) -> bytes:
    period_start, period_end = window_for(target_date)
    query = {"securityToken": token, "periodStart": period_start, "periodEnd": period_end, **params}
    return get_with_retry(API_BASE, query).content


def main() -> int:
    token = os.environ.get("ENTSOE_API_TOKEN")
    storage_key = os.environ.get("ADLS_STORAGE_KEY")
    if not token:
        sys.exit("ENTSOE_API_TOKEN not set in .env")
    if not storage_key:
        sys.exit("ADLS_STORAGE_KEY not set in .env")

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    days = [START_DATE + timedelta(days=i) for i in range((yesterday - START_DATE).days + 1)]
    if not days:
        sys.exit(f"Nothing to do: START_DATE {START_DATE} is after yesterday ({yesterday})")

    datasets = build_datasets()
    total = len(days) * len(datasets)
    print(f"Backfill {days[0]} -> {days[-1]}: {len(days)} days x {len(datasets)} datasets = {total} calls")

    conn_str = (f"DefaultEndpointsProtocol=https;AccountName={STORAGE_ACCOUNT};"
                f"AccountKey={storage_key};EndpointSuffix=core.windows.net")
    container = BlobServiceClient.from_connection_string(conn_str).get_container_client(CONTAINER)

    results = {"OK": [], "NO_DATA": [], "FETCH_FAILED": [], "UPLOAD_FAILED": []}
    done = 0
    for day in days:
        for dataset_type, params in datasets.items():
            done += 1
            label = f"{day} {dataset_type}"
            blob_name = f"{dataset_type}/{dataset_type}_{day.isoformat()}.xml"
            status, detail = "OK", ""
            try:
                body = fetch_one(token, params, day)
                reason = no_data_reason(body)
                if reason:
                    status, detail = "NO_DATA", reason
                else:
                    try:
                        container.upload_blob(blob_name, body, overwrite=True)
                        detail = f"{len(body):,} bytes -> {CONTAINER}/{blob_name}"
                    except Exception as exc:
                        status, detail = "UPLOAD_FAILED", str(exc)
            except requests.exceptions.HTTPError as exc:
                # ENTSO-E can signal "no data" with an HTTP error body as well.
                reason = no_data_reason(exc.response.content) if exc.response is not None else None
                if reason:
                    status, detail = "NO_DATA", reason
                else:
                    status, detail = "FETCH_FAILED", str(exc)
            except Exception as exc:
                status, detail = "FETCH_FAILED", str(exc)

            results[status].append(label)
            print(f"[{done}/{total}] {status:<13} {label}  {detail}")
            time.sleep(DELAY_SECONDS)

    print("\nSummary")
    for status, items in results.items():
        print(f"  {status:<13} {len(items)}")
    for status in ("NO_DATA", "FETCH_FAILED", "UPLOAD_FAILED"):
        if results[status]:
            print(f"\n{status}:")
            for item in results[status]:
                print(f"  {item}")

    return 1 if results["FETCH_FAILED"] or results["UPLOAD_FAILED"] else 0


if __name__ == "__main__":
    sys.exit(main())
