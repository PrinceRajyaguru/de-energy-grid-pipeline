"""
Parses raw ENTSO-E XML (saved by fetch_entsoe.py) into flat JSON rows and
prints a structural summary of each document.

Timestamps: ENTSO-E gives a Period start/end (UTC, ISO8601 with trailing 'Z')
plus a resolution (e.g. PT15M) and a list of Points with a 1-based
`position`. The actual UTC timestamp of a point is:
    period_start + (position - 1) * resolution
All timestamps written to the flattened JSON are UTC.
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw"
PROCESSED_DIR = REPO_ROOT / "data" / "processed"

RESOLUTION_MAP = {
    "PT15M": timedelta(minutes=15),
    "PT30M": timedelta(minutes=30),
    "PT60M": timedelta(hours=1),
    "P1D": timedelta(days=1),
}


def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def local(elem, path):
    """Find a child by local name, ignoring the document's XML namespace."""
    for child in elem:
        if strip_ns(child.tag) == path:
            return child
    return None


def local_text(elem, path):
    e = local(elem, path)
    return e.text if e is not None else None


def parse_timestamp(s: str) -> datetime:
    # ENTSO-E gives e.g. "2026-09-21T00:00Z" — UTC, no offset math needed.
    return datetime.strptime(s, "%Y-%m-%dT%H:%MZ")


def summarize_and_flatten(xml_path: Path, region: str):
    tree = ET.parse(xml_path)
    root = tree.getroot()
    doc_type = strip_ns(root.tag)
    top_level_children = sorted({strip_ns(c.tag) for c in root})

    time_series_list = [c for c in root if strip_ns(c.tag) == "TimeSeries"]

    summary = {
        "file": xml_path.name,
        "root_element": doc_type,
        "top_level_elements": top_level_children,
        "time_series_count": len(time_series_list),
        "time_series_summaries": [],
    }

    rows = []

    for ts in time_series_list:
        psr_elem = local(ts, "MktPSRType")
        psr_type = local_text(psr_elem, "psrType") if psr_elem is not None else None
        business_type = local_text(ts, "businessType")
        class_seq = local_text(ts, "classificationSequence_AttributeInstanceComponent.position")
        unit = local_text(ts, "quantity_Measure_Unit.name") or local_text(ts, "price_Measure_Unit.name")
        currency = local_text(ts, "currency_Unit.name")

        periods = [c for c in ts if strip_ns(c.tag) == "Period"]
        ts_point_count = 0

        for period in periods:
            time_interval = local(period, "timeInterval")
            period_start = parse_timestamp(local_text(time_interval, "start"))
            resolution_str = local_text(period, "resolution")
            step = RESOLUTION_MAP.get(resolution_str)
            if step is None:
                raise ValueError(f"Unknown resolution '{resolution_str}' in {xml_path.name}")

            points = [c for c in period if strip_ns(c.tag) == "Point"]
            ts_point_count += len(points)

            for point in points:
                position = int(local_text(point, "position"))
                quantity_text = local_text(point, "quantity")
                price_text = local_text(point, "price.amount")
                value = float(quantity_text) if quantity_text is not None else float(price_text)

                timestamp = period_start + (position - 1) * step

                row = {
                    "timestamp_utc": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "region": region,
                    "value": value,
                    "unit": unit,
                    "resolution": resolution_str,
                }
                if psr_type:
                    row["psr_type"] = psr_type
                if business_type:
                    row["business_type"] = business_type
                if currency:
                    row["currency"] = currency
                if class_seq is not None:
                    # Unresolved: day-ahead price docs can carry multiple
                    # TimeSeries per day distinguished by this field, with
                    # overlapping/irregular point counts. Not deduplicating
                    # here — see docs/entsoe_dataset_notes.md.
                    row["classification_sequence"] = class_seq

                rows.append(row)

        summary["time_series_summaries"].append({
            "psr_type": psr_type,
            "business_type": business_type,
            "classification_sequence": class_seq,
            "unit": unit,
            "currency": currency,
            "period_count": len(periods),
            "point_count": ts_point_count,
            "resolutions": sorted({local_text(p, "resolution") for p in periods}),
        })

    return summary, rows


DATASETS = {
    "generation_per_type": "generation",
    "actual_total_load": "load",
    "day_ahead_prices": "price",
}


def main():
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    all_summaries = {}

    for stem, region in DATASETS.items():
        xml_files = sorted(RAW_DIR.glob(f"{stem}_*.xml"))
        if not xml_files:
            print(f"[skip] no raw file found for {stem}")
            continue
        xml_path = xml_files[-1]

        summary, rows = summarize_and_flatten(xml_path, region="DE-LU")
        all_summaries[stem] = summary

        print(f"\n=== {stem} ({xml_path.name}) ===")
        print(f"  root element: {summary['root_element']}")
        print(f"  top-level elements: {summary['top_level_elements']}")
        print(f"  TimeSeries count: {summary['time_series_count']}")
        for ts_sum in summary["time_series_summaries"][:5]:
            print(f"    - {ts_sum}")
        if len(summary["time_series_summaries"]) > 5:
            print(f"    ... and {len(summary['time_series_summaries']) - 5} more")

        out_path = PROCESSED_DIR / f"{stem}.json"
        out_path.write_text(json.dumps(rows, indent=2))
        print(f"  wrote {len(rows)} flat rows -> {out_path.relative_to(REPO_ROOT)}")

    summary_path = PROCESSED_DIR / "_structure_summary.json"
    summary_path.write_text(json.dumps(all_summaries, indent=2))
    print(f"\nStructure summary written -> {summary_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
