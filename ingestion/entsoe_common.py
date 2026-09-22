"""
Shared helpers for the ENTSO-E fetch scripts: a retrying HTTP GET, and the
full PsrType code reference table.
"""

import time

import requests

# Full ENTSO-E PsrType code list (B01-B25), not just the 16 codes that
# happened to appear in our one-day DE-LU sample. Cross-checked against the
# entsoe-py open-source client's mappings.py, since ENTSO-E's own docs page
# wasn't directly fetchable in this session. B21-B24 are grid assets, not
# generation sources, but included for completeness — they exist in the
# code list and could appear in other document types.
PSR_TYPE_LABELS = {
    "B01": "Biomass",
    "B02": "Fossil Brown coal/Lignite",
    "B03": "Fossil Coal-derived gas",
    "B04": "Fossil Gas",
    "B05": "Fossil Hard coal",
    "B06": "Fossil Oil",
    "B07": "Fossil Oil shale",
    "B08": "Fossil Peat",
    "B09": "Geothermal",
    "B10": "Hydro Pumped Storage",
    "B11": "Hydro Run-of-river and pondage",
    "B12": "Hydro Water Reservoir",
    "B13": "Marine",
    "B14": "Nuclear",
    "B15": "Other renewable",
    "B16": "Solar",
    "B17": "Waste",
    "B18": "Wind Offshore",
    "B19": "Wind Onshore",
    "B20": "Other",
    "B21": "AC Link",
    "B22": "DC Link",
    "B23": "Substation",
    "B24": "Transformer",
    "B25": "Energy storage",
}

# Exponential backoff schedule in seconds, applied after each failed
# attempt (index 0 = wait before retry #1, etc). Used for both generic
# non-200/connection-error retries and as the 429 fallback when no
# Retry-After header is present.
BACKOFF_SCHEDULE = [2, 4, 8, 16, 32]
MAX_ATTEMPTS = len(BACKOFF_SCHEDULE) + 1  # 1 initial attempt + 5 retries


def get_with_retry(url: str, params: dict, timeout: int = 45) -> requests.Response:
    """GET with exponential backoff on non-200 responses and connection
    errors, up to MAX_ATTEMPTS total attempts.

    429 is handled specially: if the response carries a Retry-After header
    (seconds or HTTP-date — ENTSO-E hasn't been observed sending one, so
    only the numeric-seconds form is parsed; falls through to the standard
    backoff schedule otherwise), that wait is used instead of the schedule.

    After MAX_ATTEMPTS attempts, raises loudly (requests.HTTPError or the
    underlying connection exception) rather than returning a bad response
    or silently skipping — a persistent failure should stop a backfill run,
    not corrupt it with missing data.
    """
    last_exc = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        is_last_attempt = attempt == MAX_ATTEMPTS
        try:
            resp = requests.get(url, params=params, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if is_last_attempt:
                print(f"  [fail] connection error after {attempt} attempts: {exc}")
                raise
            wait = BACKOFF_SCHEDULE[attempt - 1]
            print(f"  [retry {attempt}/{MAX_ATTEMPTS}] connection error ({exc}), waiting {wait}s")
            time.sleep(wait)
            continue

        if resp.status_code == 200:
            return resp

        if is_last_attempt:
            print(f"  [fail] HTTP {resp.status_code} after {attempt} attempts, giving up")
            resp.raise_for_status()

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after is not None and retry_after.strip().isdigit():
                wait = int(retry_after)
                reason = f"429 rate limit, Retry-After={wait}s"
            else:
                wait = BACKOFF_SCHEDULE[attempt - 1]
                reason = f"429 rate limit, no usable Retry-After header, using backoff schedule"
        else:
            wait = BACKOFF_SCHEDULE[attempt - 1]
            reason = f"HTTP {resp.status_code}"

        print(f"  [retry {attempt}/{MAX_ATTEMPTS}] {reason}, waiting {wait}s")
        time.sleep(wait)

    raise last_exc  # pragma: no cover - unreachable, satisfies linters
