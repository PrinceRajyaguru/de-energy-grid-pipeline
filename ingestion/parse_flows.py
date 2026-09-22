"""
Parses raw ENTSO-E cross-border flow XML (A11, saved by fetch_flows.py) into
flat JSON rows, and computes net import/export per neighbor.

Flow XML omits a <Point> when its value is unchanged from the previous point
(observed empirically — see docs/entsoe_dataset_notes.md). This is different
from generation/load, which had every position present in the sample pulled
so far. This parser forward-fills: every skipped position gets the value of
the last explicit point at or before it, so the output has one row per
15-minute interval per neighbor/direction, same as the other datasets.

Also computes a `net_import_mw` field per neighbor/timestamp:
    net_import = (neighbor -> DE flow) - (DE -> neighbor flow)
ENTSO-E does NOT provide this directly — flows are always non-negative,
directional, and queried as separate in_Domain/out_Domain pairs.
"""

import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw"
PROCESSED_DIR = REPO_ROOT / "data" / "processed"

TOTAL_POSITIONS = 92  # matches the 23:00-bounded window used by fetch scripts


def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def local(elem, name):
    for child in elem:
        if strip_ns(child.tag) == name:
            return child
    return None


def local_text(elem, name):
    e = local(elem, name)
    return e.text if e is not None else None


def parse_flow_file(xml_path: Path):
    """Returns (direction_label, forward_filled_values) where
    forward_filled_values is a list of length TOTAL_POSITIONS, index 0 = position 1."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    ts = local(root, "TimeSeries")
    if ts is None:
        return None

    in_domain = local_text(ts, "in_Domain.mRID")
    out_domain = local_text(ts, "out_Domain.mRID")
    unit = local_text(ts, "quantity_Measure_Unit.name")

    period = local(ts, "Period")
    period_start = local_text(local(period, "timeInterval"), "start")
    resolution = local_text(period, "resolution")

    points = [c for c in period if strip_ns(c.tag) == "Point"]
    explicit = {}
    for p in points:
        pos = int(local_text(p, "position"))
        explicit[pos] = float(local_text(p, "quantity"))

    if not explicit:
        raise ValueError(f"No points found in {xml_path.name}")

    filled = []
    last_value = None
    for pos in range(1, TOTAL_POSITIONS + 1):
        if pos in explicit:
            last_value = explicit[pos]
        filled.append(last_value)

    if any(v is None for v in filled):
        raise ValueError(
            f"{xml_path.name}: position 1 was not explicit, can't forward-fill from a gap at the start"
        )

    return {
        "in_domain": in_domain,
        "out_domain": out_domain,
        "unit": unit,
        "period_start": period_start,
        "resolution": resolution,
        "explicit_point_count": len(points),
        "values": filled,
    }


def timestamps_for(period_start: str, resolution: str):
    from datetime import datetime, timedelta
    step_map = {"PT15M": timedelta(minutes=15), "PT60M": timedelta(hours=1)}
    step = step_map[resolution]
    start = datetime.strptime(period_start, "%Y-%m-%dT%H:%MZ")
    return [start + i * step for i in range(TOTAL_POSITIONS)]


NEIGHBORS = ["FR", "NL", "PL"]


def main():
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    all_rows = []
    structure_notes = {}

    for neighbor in NEIGHBORS:
        de_to_n_files = sorted(RAW_DIR.glob(f"flow_DE_to_{neighbor}_*.xml"))
        n_to_de_files = sorted(RAW_DIR.glob(f"flow_{neighbor}_to_DE_*.xml"))
        if not de_to_n_files or not n_to_de_files:
            print(f"[skip] missing raw files for {neighbor}")
            continue

        export = parse_flow_file(de_to_n_files[-1])   # DE -> neighbor
        import_ = parse_flow_file(n_to_de_files[-1])   # neighbor -> DE

        structure_notes[neighbor] = {
            "export_explicit_points": export["explicit_point_count"],
            "import_explicit_points": import_["explicit_point_count"],
            "compression_ratio_export": round(export["explicit_point_count"] / TOTAL_POSITIONS, 2),
            "compression_ratio_import": round(import_["explicit_point_count"] / TOTAL_POSITIONS, 2),
        }

        timestamps = timestamps_for(export["period_start"], export["resolution"])

        for i, ts in enumerate(timestamps):
            export_mw = export["values"][i]
            import_mw = import_["values"][i]
            all_rows.append({
                "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "region": "DE-LU",
                "neighbor": neighbor,
                "export_mw": export_mw,     # DE -> neighbor
                "import_mw": import_mw,     # neighbor -> DE
                "net_import_mw": import_mw - export_mw,
                "unit": export["unit"],
                "resolution": export["resolution"],
            })

    out_path = PROCESSED_DIR / "cross_border_flows.json"
    out_path.write_text(json.dumps(all_rows, indent=2))
    print(f"Wrote {len(all_rows)} flat rows -> {out_path.relative_to(REPO_ROOT)}")

    print("\nCompression / forward-fill summary (explicit points out of 92):")
    for neighbor, notes in structure_notes.items():
        print(f"  {neighbor}: export {notes['export_explicit_points']}/92 "
              f"({notes['compression_ratio_export']:.0%}), "
              f"import {notes['import_explicit_points']}/92 "
              f"({notes['compression_ratio_import']:.0%})")

    summary_path = PROCESSED_DIR / "_flows_structure_summary.json"
    summary_path.write_text(json.dumps(structure_notes, indent=2))
    print(f"\nStructure summary written -> {summary_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
