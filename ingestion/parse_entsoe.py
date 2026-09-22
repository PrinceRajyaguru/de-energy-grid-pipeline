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
import sys
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from entsoe_common import PSR_TYPE_LABELS

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


def document_window(root) -> tuple[datetime, datetime] | None:
    """The document-level requested window (as opposed to any single
    TimeSeries's own, possibly-shorter, Period). GL_MarketDocument uses
    time_Period.timeInterval; Publication_MarketDocument (prices, flows)
    uses period.timeInterval. Used to detect gaps: a TimeSeries whose own
    Period doesn't cover this full window is genuinely missing data, not
    just compressed."""
    interval = local(root, "time_Period.timeInterval")
    if interval is None:
        interval = local(root, "period.timeInterval")
    if interval is None:
        return None
    return parse_timestamp(local_text(interval, "start")), parse_timestamp(local_text(interval, "end"))


def summarize_and_flatten(xml_path: Path, region: str, check_full_window_gaps: bool = True):
    """check_full_window_gaps: whether it's meaningful to compare each
    TimeSeries's own Period against the document's overall window to detect
    silent gaps (see B12 case). True for generation/load, where one
    TimeSeries = one full-day source stream by construction, so a shorter
    Period IS a gap. False for day-ahead prices, where multiple TimeSeries
    legitimately partition the document window by day AND by
    classification_sequence — a single TimeSeries covering less than the
    full window there is normal structure, not a gap, and comparing it
    directly produces false positives."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    doc_type = strip_ns(root.tag)
    top_level_children = sorted({strip_ns(c.tag) for c in root})

    time_series_list = [c for c in root if strip_ns(c.tag) == "TimeSeries"]
    doc_window = document_window(root) if check_full_window_gaps else None

    summary = {
        "file": xml_path.name,
        "root_element": doc_type,
        "top_level_elements": top_level_children,
        "time_series_count": len(time_series_list),
        "time_series_summaries": [],
    }

    rows = []
    gap_report = []  # populated below: one entry per TimeSeries with missing timestamps vs. the doc-level window

    for ts in time_series_list:
        psr_elem = local(ts, "MktPSRType")
        psr_type = local_text(psr_elem, "psrType") if psr_elem is not None else None
        business_type = local_text(ts, "businessType")
        class_seq = local_text(ts, "classificationSequence_AttributeInstanceComponent.position")
        unit = local_text(ts, "quantity_Measure_Unit.name") or local_text(ts, "price_Measure_Unit.name")
        currency = local_text(ts, "currency_Unit.name")

        # Some PsrTypes (observed: B10 Hydro Pumped Storage) publish TWO
        # separate TimeSeries for the same period — one with
        # inBiddingZone_Domain set (this source generating power) and one
        # with outBiddingZone_Domain set (this source consuming/pumping
        # power). Summing them as one "generation" number would net out
        # pumping against generation and understate both. Tagged explicitly
        # so Gold-layer aggregation can sum only flow_direction="generation"
        # into total generation, and treat "consumption" as additional load.
        has_in_domain = local(ts, "inBiddingZone_Domain.mRID") is not None
        has_out_domain = local(ts, "outBiddingZone_Domain.mRID") is not None
        if has_in_domain and not has_out_domain:
            flow_direction = "generation"
        elif has_out_domain and not has_in_domain:
            flow_direction = "consumption"
        else:
            flow_direction = None  # load doc uses outBiddingZone only; single-direction PsrTypes don't need this field

        periods = [c for c in ts if strip_ns(c.tag) == "Period"]
        ts_point_count = 0
        ts_timestamps = []  # every timestamp actually produced for this TimeSeries, across all its Periods

        for period in periods:
            time_interval = local(period, "timeInterval")
            period_start = parse_timestamp(local_text(time_interval, "start"))
            resolution_str = local_text(period, "resolution")
            time_interval_end = parse_timestamp(local_text(time_interval, "end"))

            if resolution_str == "P1Y":
                # Calendar years vary in length (leap years), so this can't
                # be a fixed timedelta the way PT15M/P1D can. Observed on
                # installed_capacity (A68): exactly 1 Point per TimeSeries,
                # representing the whole year — so "resolution" here really
                # means "one point spans the whole requested period," and
                # step is just that period's actual duration.
                step = time_interval_end - period_start
            else:
                step = RESOLUTION_MAP.get(resolution_str)
                if step is None:
                    raise ValueError(f"Unknown resolution '{resolution_str}' in {xml_path.name}")

            expected_point_count = int((time_interval_end - period_start) / step)

            points = [c for c in period if strip_ns(c.tag) == "Point"]
            ts_point_count += len(points)

            # ENTSO-E omits a Point when its value is unchanged from the
            # previous one — confirmed here too (not just flow data): on
            # 2026-09-21, 5/17 generation TimeSeries and 2/4 price
            # TimeSeries were compressed this way. Forward-fill to one row
            # per position, same approach as parse_flows.py, so rows aren't
            # silently missing for timestamps where a value just happened
            # to hold steady.
            explicit = {}
            for point in points:
                position = int(local_text(point, "position"))
                quantity_text = local_text(point, "quantity")
                price_text = local_text(point, "price.amount")
                value = float(quantity_text) if quantity_text is not None else float(price_text)
                explicit[position] = value

            positions_seen = set(explicit)
            is_compressed = len(positions_seen) < expected_point_count

            last_value = None
            for position in range(1, expected_point_count + 1):
                if position in explicit:
                    last_value = explicit[position]
                elif last_value is None:
                    # Leading gap: position 1 (or a run from it) has no
                    # explicit value yet, nothing to forward-fill from.
                    # Left as None rather than guessed at.
                    continue

                timestamp = period_start + (position - 1) * step

                row = {
                    "timestamp_utc": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "region": region,
                    "value": last_value,
                    "unit": unit,
                    "resolution": resolution_str,
                }
                if psr_type:
                    row["psr_type"] = psr_type
                    row["psr_type_label"] = PSR_TYPE_LABELS.get(psr_type, "Unknown")
                    if flow_direction:
                        row["flow_direction"] = flow_direction
                if business_type:
                    row["business_type"] = business_type
                if currency:
                    row["currency"] = currency
                if class_seq is not None:
                    # Unresolved series-selection question — see
                    # docs/entsoe_dataset_notes.md: classification_sequence=1
                    # is EPEX (primary), =2 is EXAA's separate auction. Both
                    # kept here; downstream consumers should filter to
                    # sequence=1 unless they specifically want EXAA.
                    row["classification_sequence"] = class_seq

                rows.append(row)
                ts_timestamps.append(timestamp)

        # Gap detection (not fabrication): compare what this TimeSeries
        # actually covers against the document-level requested window, at
        # the document's own resolution. A TimeSeries whose Period(s) don't
        # span the full window — like B12 starting at 05:00Z instead of
        # 00:00Z on 2026-09-21, with no <Reason> marker at all — shows up
        # here as missing timestamps. This is intentionally NOT
        # forward-filled or interpolated; that's a Silver/Gold modeling
        # decision, not ingestion's job.
        ts_resolution = local_text(periods[0], "resolution") if periods else None
        if doc_window and periods and ts_resolution in RESOLUTION_MAP:
            doc_start, doc_end = doc_window
            doc_step = RESOLUTION_MAP[ts_resolution]
            full_grid = set()
            t = doc_start
            while t < doc_end:
                full_grid.add(t)
                t += doc_step
            missing = sorted(full_grid - set(ts_timestamps))
            if missing:
                gap_report.append({
                    "psr_type": psr_type,
                    "flow_direction": flow_direction,
                    "business_type": business_type,
                    "expected_count": len(full_grid),
                    "actual_count": len(ts_timestamps),
                    "missing_count": len(missing),
                    "missing_timestamps": [m.strftime("%Y-%m-%dT%H:%M:%SZ") for m in missing],
                })

        summary["time_series_summaries"].append({
            "psr_type": psr_type,
            "flow_direction": flow_direction,
            "business_type": business_type,
            "classification_sequence": class_seq,
            "unit": unit,
            "currency": currency,
            "period_count": len(periods),
            "point_count": ts_point_count,
            "expected_point_count": expected_point_count if periods else None,
            "is_compressed": is_compressed if periods else None,
            "resolutions": sorted({local_text(p, "resolution") for p in periods}),
        })

    return summary, rows, gap_report


DATASETS = {
    "generation_per_type": "generation",
    "actual_total_load": "load",
    "day_ahead_prices": "price",
}


def main():
    date_filter = sys.argv[1].replace("-", "") if len(sys.argv) > 1 else None
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    all_summaries = {}
    all_gaps = {}
    suffix = f"_{date_filter}" if date_filter else ""

    for stem, region in DATASETS.items():
        pattern = f"{stem}_{date_filter}.xml" if date_filter else f"{stem}_*.xml"
        xml_files = sorted(RAW_DIR.glob(pattern))
        if not xml_files:
            print(f"[skip] no raw file found for {stem}" + (f" on {date_filter}" if date_filter else ""))
            continue
        xml_path = xml_files[-1]

        summary, rows, gap_report = summarize_and_flatten(
            xml_path, region="DE-LU", check_full_window_gaps=(stem != "day_ahead_prices")
        )
        all_summaries[stem] = summary
        if gap_report:
            all_gaps[stem] = gap_report

        print(f"\n=== {stem} ({xml_path.name}) ===")
        print(f"  root element: {summary['root_element']}")
        print(f"  top-level elements: {summary['top_level_elements']}")
        print(f"  TimeSeries count: {summary['time_series_count']}")
        for ts_sum in summary["time_series_summaries"][:5]:
            print(f"    - {ts_sum}")
        if len(summary["time_series_summaries"]) > 5:
            print(f"    ... and {len(summary['time_series_summaries']) - 5} more")

        compressed = [t for t in summary["time_series_summaries"] if t["is_compressed"]]
        if compressed:
            print(f"  ⚠ {len(compressed)} TimeSeries in this file had FEWER points than expected "
                  f"(same compression behavior seen in flow data) — forward-filled during flattening.")

        if gap_report:
            total_missing = sum(g["missing_count"] for g in gap_report)
            print(f"  ⚠ GAP DETECTED: {len(gap_report)} TimeSeries missing {total_missing} timestamp(s) "
                  f"vs. the full-day grid (not forward-filled — see _gaps{suffix}.json)")

        # Price fixes:
        # 1. Keep only classification_sequence=1 (EPEX, the primary DE-LU
        #    day-ahead auction). classification_sequence=2 is EXAA's
        #    separate auction — a genuinely different market, not a
        #    revision — and blending it into the same average is
        #    meaningless. Written to its own file instead of silently
        #    discarded, for transparency.
        # 2. Trim to the requested day. Discovered while verifying this fix:
        #    the price document's own window is WIDER than requested (e.g.
        #    requesting 2026-09-21 returned TimeSeries spanning
        #    2026-09-20T22:00Z to 2026-09-22T22:00Z, touching 3 calendar
        #    UTC days) — a real ENTSO-E behavior for this document type, not
        #    a bug in our request. Without trimming, the "daily mean" is
        #    actually a blend of neighboring days.
        if stem == "day_ahead_prices":
            target_date_str = xml_path.stem.rsplit("_", 1)[-1]  # e.g. "20260921"
            target_iso_date = f"{target_date_str[:4]}-{target_date_str[4:6]}-{target_date_str[6:8]}"

            exaa_rows = [r for r in rows if r.get("classification_sequence") == "2"]
            epex_rows = [r for r in rows if r.get("classification_sequence") != "2"]
            out_of_window_count = sum(1 for r in epex_rows if not r["timestamp_utc"].startswith(target_iso_date))
            rows = [r for r in epex_rows if r["timestamp_utc"].startswith(target_iso_date)]

            if exaa_rows:
                exaa_path = PROCESSED_DIR / f"day_ahead_prices_exaa{suffix}.json"
                exaa_path.write_text(json.dumps(exaa_rows, indent=2))
                print(f"  filtered out {len(exaa_rows)} EXAA (classification_sequence=2) rows "
                      f"-> {exaa_path.relative_to(REPO_ROOT)}")
            if out_of_window_count:
                print(f"  trimmed {out_of_window_count} EPEX rows outside {target_iso_date} "
                      f"(document window was wider than the requested day)")

            values = [r["value"] for r in rows if r["value"] is not None]
            if values:
                print(f"  EPEX-only, {target_iso_date} mean: €{sum(values) / len(values):.2f}/MWh "
                      f"over {len(values)} rows")

        out_path = PROCESSED_DIR / f"{stem}{suffix}.json"
        out_path.write_text(json.dumps(rows, indent=2))
        print(f"  wrote {len(rows)} flat rows -> {out_path.relative_to(REPO_ROOT)}")

    summary_path = PROCESSED_DIR / f"_structure_summary{suffix}.json"
    summary_path.write_text(json.dumps(all_summaries, indent=2))
    print(f"\nStructure summary written -> {summary_path.relative_to(REPO_ROOT)}")

    if all_gaps:
        gaps_path = PROCESSED_DIR / f"_gaps{suffix}.json"
        gaps_path.write_text(json.dumps(all_gaps, indent=2))
        print(f"Gap report written -> {gaps_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
