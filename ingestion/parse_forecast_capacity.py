"""
Parses raw XML for the day-ahead wind/solar forecast (A69), generation
forecast (A71), and installed generation capacity (A68) datasets saved by
fetch_forecast_capacity.py.

All 3 share the same GL_MarketDocument / TimeSeries / Period / Point schema
already handled by parse_entsoe.py for generation/load — including
installed_capacity, which turned out to use resolution=P1Y (already in
RESOLUTION_MAP) with exactly 1 Point per source per year, rather than a
different shape. Reuses summarize_and_flatten() directly instead of
duplicating the forward-fill / gap-detection / PsrType-labeling logic.
"""

import json
import sys
from pathlib import Path

from parse_entsoe import PROCESSED_DIR, RAW_DIR, REPO_ROOT, summarize_and_flatten

DATASET_STEMS = ["wind_solar_forecast", "generation_forecast", "installed_capacity"]


def main():
    date_filter = sys.argv[1].replace("-", "") if len(sys.argv) > 1 else None
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    all_summaries = {}
    all_gaps = {}
    suffix = f"_{date_filter}" if date_filter else ""

    for stem in DATASET_STEMS:
        pattern = f"{stem}_{date_filter}.xml" if date_filter else f"{stem}_*.xml"
        xml_files = sorted(RAW_DIR.glob(pattern))
        if not xml_files:
            print(f"[skip] no raw file found for {stem}" + (f" on {date_filter}" if date_filter else ""))
            continue
        xml_path = xml_files[-1]

        summary, rows, gap_report = summarize_and_flatten(xml_path, region="DE-LU")
        all_summaries[stem] = summary
        if gap_report:
            all_gaps[stem] = gap_report

        print(f"\n=== {stem} ({xml_path.name}) ===")
        print(f"  root element: {summary['root_element']}")
        print(f"  TimeSeries count: {summary['time_series_count']}")
        for ts_sum in summary["time_series_summaries"][:5]:
            print(f"    - {ts_sum}")
        if len(summary["time_series_summaries"]) > 5:
            print(f"    ... and {len(summary['time_series_summaries']) - 5} more")

        compressed = [t for t in summary["time_series_summaries"] if t["is_compressed"]]
        if compressed:
            print(f"  ⚠ {len(compressed)} TimeSeries had FEWER points than expected — forward-filled.")

        if gap_report:
            total_missing = sum(g["missing_count"] for g in gap_report)
            print(f"  ⚠ GAP DETECTED: {len(gap_report)} TimeSeries missing {total_missing} timestamp(s) "
                  f"vs. the full requested window (not forward-filled — see _forecast_capacity_gaps{suffix}.json)")

        out_path = PROCESSED_DIR / f"{stem}{suffix}.json"
        out_path.write_text(json.dumps(rows, indent=2))
        print(f"  wrote {len(rows)} flat rows -> {out_path.relative_to(REPO_ROOT)}")

    summary_path = PROCESSED_DIR / f"_forecast_capacity_structure_summary{suffix}.json"
    summary_path.write_text(json.dumps(all_summaries, indent=2))
    print(f"\nStructure summary written -> {summary_path.relative_to(REPO_ROOT)}")

    if all_gaps:
        gaps_path = PROCESSED_DIR / f"_forecast_capacity_gaps{suffix}.json"
        gaps_path.write_text(json.dumps(all_gaps, indent=2))
        print(f"Gap report written -> {gaps_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
