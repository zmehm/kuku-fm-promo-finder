"""
test_parser.py — Portable test for deterministic screenplay parser.
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src import config
from src.parser import normalize_timestamp, parse_episode, parse_timestamp


def test_parse_episode_elements():
    txt_files = sorted(config.SCRIPTS_DIR.glob("episode-*.txt"))
    if not txt_files:
        return
    ep = parse_episode(txt_files[0])
    assert ep.episode_number >= 1
    assert ep.total_elements > 0
    assert ep.total_dialogue_lines > 0
    assert ep.total_duration_sec > 0.0
    dialogues = ep.get_dialogue_elements()
    assert len(dialogues) == ep.total_dialogue_lines
    for d in dialogues[:5]:
        assert d.start_seconds < d.end_seconds


def test_timestamp_robustness():
    schema_pattern = re.compile(r"^\d{2}:\d{2}\.\d{2}$")

    # 1. Trailing zero dropped (1 decimal digit)
    assert normalize_timestamp("00:04.8") == "00:04.80"
    assert schema_pattern.match(normalize_timestamp("00:04.8"))
    assert parse_timestamp("00:04.8") == 4.80

    # 2. Comma decimal separator
    assert normalize_timestamp("00:04,80") == "00:04.80"
    assert schema_pattern.match(normalize_timestamp("00:04,80"))
    assert parse_timestamp("00:04,80") == 4.80

    # 3. 3 decimal digits
    assert normalize_timestamp("00:04.123") == "00:04.12"
    assert schema_pattern.match(normalize_timestamp("00:04.123"))
    assert parse_timestamp("00:04.123") == 4.12

    assert normalize_timestamp("00:04.128") == "00:04.13"
    assert schema_pattern.match(normalize_timestamp("00:04.128"))
    assert parse_timestamp("00:04.128") == 4.13

    # Standard format preserved
    assert normalize_timestamp("02:15.00") == "02:15.00"
    assert parse_timestamp("02:15.00") == 135.0


def test_zero_elements_raises_value_error():
    with tempfile.TemporaryDirectory() as td:
        empty_f = Path(td) / "episode-099-empty.txt"
        empty_f.write_text("", encoding="utf-8")
        try:
            parse_episode(empty_f)
            assert False, "Expected ValueError on empty file!"
        except ValueError as exc:
            msg = str(exc)
            assert f"No script elements parsed from {empty_f.name}: check the timestamp/format" in msg

        header_f = Path(td) / "episode-099-header.txt"
        header_f.write_text("EPISODE LOGLINE: Test\nCONTINUITY NOTES: Test\n", encoding="utf-8")
        try:
            parse_episode(header_f)
            assert False, "Expected ValueError on header-only file!"
        except ValueError as exc:
            msg = str(exc)
            assert f"No script elements parsed from {header_f.name}: check the timestamp/format" in msg


def test_episode_6_duration_recovery():
    txt_files = sorted(config.SCRIPTS_DIR.glob("episode-*.txt"))
    ep6_files = [f for f in txt_files if "006" in f.name]
    if ep6_files:
        ep6 = parse_episode(ep6_files[0])
        assert ep6.total_duration_sec == 115.0, f"Expected 115.0s, got {ep6.total_duration_sec}s"
        assert ep6.total_elements == 97


if __name__ == "__main__":
    test_parse_episode_elements()
    test_timestamp_robustness()
    test_zero_elements_raises_value_error()
    test_episode_6_duration_recovery()
    print("[PASS] All parser robustness tests completed successfully.")
