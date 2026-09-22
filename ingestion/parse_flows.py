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
import sys
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw"
PROCESSED_DIR = REPO_ROOT / "data" / "processed"

STEP_MAP = {"PT15M": timedelta(minutes=15), "PT60M": timedelta(hours=1)}


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


def last_explicit_value(xml_path: Path):
    """The value at the highest explicit position in a flow file — i.e. the
    forward-filled value the day ends on. Used as the carry-in for the next
    day's leading-gap handling. Returns None if the file has no points."""
    tree = ET.parse(xml_path)
    ts = local(tree.getroot(), "TimeSeries")
    if ts is None:
        return None
    period = local(ts, "Period")
    points = [c for c in period if strip_ns(c.tag) == "Point"]
    if not points:
        return None
    explicit = {int(local_text(p, "position")): float(local_text(p, "quantity")) for p in points}
    return explicit[max(explicit)]


def parse_flow_file(xml_path: Path, total_positions: int, carry_in_value: float | None = None):
    """Returns explicit points plus a forward-filled `values` list of length
    total_positions (index 0 = position 1). total_positions is computed from
    the file's own period start/end/resolution, not assumed, since the
    window-fix changed it from 92 to 96 and a second sample day could differ
    again.

    Leading-gap handling (position 1 has no explicit Point): if
    carry_in_value is given (the previous day's final value for this same
    neighbor/direction), leading positions are filled with it instead of
    left unknown. If not given, or if the caller passed None because no
    previous day's file was available, leading positions are left as None
    — never guessed as 0, which would be indistinguishable from a real
    zero-flow reading.
    """
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
    last_value = carry_in_value
    missing_leading = None
    leading_filled_from_previous_day = carry_in_value is not None and 1 not in explicit
    for pos in range(1, total_positions + 1):
        if pos in explicit:
            last_value = explicit[pos]
        elif last_value is None and missing_leading is None:
            # Position 1 (or an unbroken run from it) has no explicit point,
            # and no carry-in value was available either (no previous day's
            # file, or this is the first day ever pulled). Left as None —
            # not silently defaulted to 0 — so it's visible downstream
            # rather than mistaken for a real zero-flow reading.
            missing_leading = pos
        filled.append(last_value)

    return {
        "in_domain": in_domain,
        "out_domain": out_domain,
        "unit": unit,
        "period_start": period_start,
        "resolution": resolution,
        "explicit_point_count": len(points),
        "total_positions": total_positions,
        "missing_leading_position": missing_leading,
        "leading_filled_from_previous_day": leading_filled_from_previous_day,
        "values": filled,
    }


def timestamps_for(period_start: str, resolution: str, total_positions: int):
    step = STEP_MAP[resolution]
    start = datetime.strptime(period_start, "%Y-%m-%dT%H:%MZ")
    return [start + i * step for i in range(total_positions)]


NEIGHBORS = ["FR", "NL", "PL", "AT", "CH", "BE", "CZ", "DK1", "DK2"]


def is_acknowledgement_doc(xml_path: Path) -> str | None:
    """DK2 (East Denmark) has no direct physical interconnector with
    Germany, so A11 queries for that pair return a real
    Acknowledgement_MarketDocument error (code 999, 'No matching data
    found') instead of a TimeSeries — the first genuine <Reason> marker
    seen anywhere in this project. Returns the reason text if this file is
    such a document, else None."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    if strip_ns(root.tag) != "Acknowledgement_MarketDocument":
        return None
    reason = local(root, "Reason")
    return local_text(reason, "text") if reason is not None else "Acknowledgement document with no <Reason> text"


def find_previous_day_file(current_path: Path) -> Path | None:
    """Given .../flow_DE_to_FR_20260921.xml, look for the same
    neighbor/direction file one day earlier. Returns None if not present on
    disk (e.g. first day ever pulled) — that's a normal, expected case, not
    an error."""
    stem = current_path.stem  # e.g. flow_DE_to_FR_20260921
    prefix, date_str = stem.rsplit("_", 1)
    try:
        current_date = datetime.strptime(date_str, "%Y%m%d").date()
    except ValueError:
        return None
    previous_date = current_date - timedelta(days=1)
    candidate = current_path.parent / f"{prefix}_{previous_date.strftime('%Y%m%d')}.xml"
    return candidate if candidate.exists() else None


def main():
    date_filter = sys.argv[1].replace("-", "") if len(sys.argv) > 1 else None
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    all_rows = []
    structure_notes = {}

    for neighbor in NEIGHBORS:
        pattern_suffix = f"{date_filter}.xml" if date_filter else "*.xml"
        de_to_n_files = sorted(RAW_DIR.glob(f"flow_DE_to_{neighbor}_{pattern_suffix}"))
        n_to_de_files = sorted(RAW_DIR.glob(f"flow_{neighbor}_to_DE_{pattern_suffix}"))
        if not de_to_n_files or not n_to_de_files:
            print(f"[skip] missing raw files for {neighbor}" + (f" on {date_filter}" if date_filter else ""))
            continue

        de_to_n_path, n_to_de_path = de_to_n_files[-1], n_to_de_files[-1]

        export_reason = is_acknowledgement_doc(de_to_n_path)
        import_reason = is_acknowledgement_doc(n_to_de_path)
        if export_reason or import_reason:
            print(f"[skip] {neighbor}: no data (Acknowledgement doc) — "
                  f"{export_reason or import_reason}")
            structure_notes[neighbor] = {"no_data": True, "reason": export_reason or import_reason}
            continue

        # total_positions derived from the period's own start/end, not a
        # hardcoded constant.
        tree = ET.parse(de_to_n_path)
        period = local(local(tree.getroot(), "TimeSeries"), "Period")
        ti = local(period, "timeInterval")
        p_start = datetime.strptime(local_text(ti, "start"), "%Y-%m-%dT%H:%MZ")
        p_end = datetime.strptime(local_text(ti, "end"), "%Y-%m-%dT%H:%MZ")
        resolution = local_text(period, "resolution")
        total_positions = int((p_end - p_start) / STEP_MAP[resolution])

        # Leading-gap fallback: if this day's export/import series starts
        # mid-gap (position 1 not explicit), look back to the previous
        # calendar day's final value for the same neighbor/direction rather
        # than leaving it unknown, when that previous day's raw file exists
        # locally.
        export_prev_file = find_previous_day_file(de_to_n_path)
        import_prev_file = find_previous_day_file(n_to_de_path)
        export_carry_in = last_explicit_value(export_prev_file) if export_prev_file else None
        import_carry_in = last_explicit_value(import_prev_file) if import_prev_file else None

        export = parse_flow_file(de_to_n_path, total_positions, export_carry_in)   # DE -> neighbor
        import_ = parse_flow_file(n_to_de_path, total_positions, import_carry_in)  # neighbor -> DE

        structure_notes[neighbor] = {
            "total_positions": total_positions,
            "export_explicit_points": export["explicit_point_count"],
            "import_explicit_points": import_["explicit_point_count"],
            "compression_ratio_export": round(export["explicit_point_count"] / total_positions, 2),
            "compression_ratio_import": round(import_["explicit_point_count"] / total_positions, 2),
            "export_missing_leading_position": export["missing_leading_position"],
            "import_missing_leading_position": import_["missing_leading_position"],
            "export_leading_filled_from_previous_day": export["leading_filled_from_previous_day"],
            "import_leading_filled_from_previous_day": import_["leading_filled_from_previous_day"],
        }

        timestamps = timestamps_for(export["period_start"], export["resolution"], total_positions)

        for i, ts in enumerate(timestamps):
            export_mw = export["values"][i]
            import_mw = import_["values"][i]
            net = (import_mw - export_mw) if (export_mw is not None and import_mw is not None) else None
            all_rows.append({
                "timestamp_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "region": "DE-LU",
                "neighbor": neighbor,
                "export_mw": export_mw,     # DE -> neighbor
                "import_mw": import_mw,     # neighbor -> DE
                "net_import_mw": net,
                "unit": export["unit"],
                "resolution": export["resolution"],
            })

    suffix = f"_{date_filter}" if date_filter else ""
    out_path = PROCESSED_DIR / f"cross_border_flows{suffix}.json"
    out_path.write_text(json.dumps(all_rows, indent=2))
    print(f"Wrote {len(all_rows)} flat rows -> {out_path.relative_to(REPO_ROOT)}")

    print("\nCompression / forward-fill summary:")
    for neighbor, notes in structure_notes.items():
        if notes.get("no_data"):
            print(f"  {neighbor}: no data (no direct interconnector) — {notes['reason']}")
            continue
        total = notes["total_positions"]
        print(f"  {neighbor}: export {notes['export_explicit_points']}/{total} "
              f"({notes['compression_ratio_export']:.0%}), "
              f"import {notes['import_explicit_points']}/{total} "
              f"({notes['compression_ratio_import']:.0%})"
              + (" | export leading gap: filled from previous day's final value"
                 if notes["export_leading_filled_from_previous_day"]
                 else f" | export leading gap at position {notes['export_missing_leading_position']} "
                      f"(UNKNOWN, no previous day file available)"
                 if notes["export_missing_leading_position"] else "")
              + (" | import leading gap: filled from previous day's final value"
                 if notes["import_leading_filled_from_previous_day"]
                 else f" | import leading gap at position {notes['import_missing_leading_position']} "
                      f"(UNKNOWN, no previous day file available)"
                 if notes["import_missing_leading_position"] else ""))

    summary_path = PROCESSED_DIR / f"_flows_structure_summary{suffix}.json"
    summary_path.write_text(json.dumps(structure_notes, indent=2))
    print(f"\nStructure summary written -> {summary_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
