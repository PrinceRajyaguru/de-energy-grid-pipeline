"""
Synthetic test for the flow-parsing leading-gap path (position 1 missing).

This case has never occurred in any real day pulled so far (see
docs/entsoe_dataset_notes.md), so it can't be tested against real data —
this hand-builds a minimal XML file matching ENTSO-E's A11 shape, with
position 1 deliberately absent, to prove parse_flow_file handles it both
with and without a previous-day carry-in value.

Run directly: `python test_parse_flows_leading_gap.py`
"""

import sys
import tempfile
from pathlib import Path

from parse_flows import parse_flow_file

# A11-shaped document, 8 positions (not 96, to keep this readable), missing
# positions 1-2 entirely (first explicit point is position 3).
SYNTHETIC_XML = """<?xml version="1.0" encoding="utf-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:0">
  <mRID>test</mRID>
  <type>A11</type>
  <TimeSeries>
    <mRID>1</mRID>
    <businessType>A66</businessType>
    <in_Domain.mRID codingScheme="A01">10YFR-RTE------C</in_Domain.mRID>
    <out_Domain.mRID codingScheme="A01">10Y1001A1001A82H</out_Domain.mRID>
    <quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name>
    <curveType>A03</curveType>
    <Period>
      <timeInterval>
        <start>2026-09-22T00:00Z</start>
        <end>2026-09-22T02:00Z</end>
      </timeInterval>
      <resolution>PT15M</resolution>
      <Point><position>3</position><quantity>100.0</quantity></Point>
      <Point><position>5</position><quantity>150.0</quantity></Point>
      <Point><position>8</position><quantity>0</quantity></Point>
    </Period>
  </TimeSeries>
</Publication_MarketDocument>
"""


def write_synthetic_file() -> Path:
    tmp_dir = Path(tempfile.mkdtemp())
    path = tmp_dir / "flow_TEST_to_DE_99999999.xml"
    path.write_text(SYNTHETIC_XML)
    return path


def test_leading_gap_without_carry_in():
    path = write_synthetic_file()
    result = parse_flow_file(path, total_positions=8, carry_in_value=None)

    assert result["missing_leading_position"] == 1, result["missing_leading_position"]
    assert result["leading_filled_from_previous_day"] is False
    # positions 1-2: no value available yet -> None, not guessed as 0
    assert result["values"][0] is None, "position 1 should be None, not fabricated"
    assert result["values"][1] is None, "position 2 should be None, not fabricated"
    # position 3 onward: explicit/forward-filled as normal
    assert result["values"][2] == 100.0
    assert result["values"][3] == 100.0  # position 4, forward-filled from 3
    assert result["values"][4] == 150.0  # position 5, explicit
    assert result["values"][7] == 0.0    # position 8, explicit
    print("PASS: test_leading_gap_without_carry_in")


def test_leading_gap_with_carry_in():
    path = write_synthetic_file()
    result = parse_flow_file(path, total_positions=8, carry_in_value=42.5)

    assert result["missing_leading_position"] is None, "should not flag a gap once carry-in fills it"
    assert result["leading_filled_from_previous_day"] is True
    # positions 1-2: filled from the previous day's carry-in value
    assert result["values"][0] == 42.5, "position 1 should use carry-in value"
    assert result["values"][1] == 42.5, "position 2 should use carry-in value"
    # position 3 onward: unaffected, same as without carry-in
    assert result["values"][2] == 100.0
    assert result["values"][4] == 150.0
    print("PASS: test_leading_gap_with_carry_in")


if __name__ == "__main__":
    test_leading_gap_without_carry_in()
    test_leading_gap_with_carry_in()
    print("\nAll synthetic leading-gap tests passed.")
