"""
compare_parsed_counts.py — Compares all 50 parsed screenplay counts against outputs/parsed/*.json reference files.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src import config
from src.parser import parse_episode

def main():
    txt_files = sorted(config.SCRIPTS_DIR.glob("episode-*.txt"))
    assert len(txt_files) == 50, f"Expected 50 episodes, found {len(txt_files)}"

    print("=" * 76)
    print(" PARSER VERIFICATION: 50 EPISODES VS OUTPUTS/PARSED REFERENCE JSON")
    print("=" * 76)
    print(f"{'Episode':<10} | {'Status':<6} | {'New Elements':<14} | {'Ref Elements':<14} | {'New Dur':<8} | {'Ref Dur':<8}")
    print("-" * 76)

    mismatches = []
    dur_changes = []

    for fp in txt_files:
        ep = parse_episode(fp)
        ref_file = config.PARSED_DIR / f"episode-{ep.episode_number:03d}.json"
        assert ref_file.exists(), f"Reference file missing: {ref_file}"
        ref = json.loads(ref_file.read_text(encoding="utf-8"))

        match_elem = (ep.total_elements == ref["total_elements"])
        match_dlg = (ep.total_dialogue_lines == ref["total_dialogue_lines"])
        status = "OK" if (match_elem and match_dlg) else "DIFF"

        if status != "OK":
            mismatches.append({
                "episode": ep.episode_number,
                "elem_new": ep.total_elements,
                "elem_ref": ref["total_elements"],
                "dlg_new": ep.total_dialogue_lines,
                "dlg_ref": ref["total_dialogue_lines"],
            })

        if abs(ep.total_duration_sec - ref["total_duration_sec"]) > 0.01:
            dur_changes.append({
                "episode": ep.episode_number,
                "dur_new": ep.total_duration_sec,
                "dur_ref": ref["total_duration_sec"],
            })

        print(f"Ep {ep.episode_number:<7} | {status:<6} | {ep.total_elements:<14} | {ref['total_elements']:<14} | {ep.total_duration_sec:<8.1f} | {ref['total_duration_sec']:<8.1f}")

    print("=" * 76)
    print(f"Element Count Mismatches: {len(mismatches)} / {len(txt_files)}")
    if mismatches:
        for m in mismatches:
            print(f"  DIFF: Ep {m['episode']} elements: {m['elem_new']} vs {m['elem_ref']}, dialogue: {m['dlg_new']} vs {m['dlg_ref']}")
    else:
        print("✓ All 50 episodes have IDENTICAL element counts and dialogue line counts to outputs/parsed/*.json.")

    print(f"\nDuration Adjustments: {len(dur_changes)}")
    for d in dur_changes:
        print(f"  - Ep {d['episode']:03d}: Recovered total_duration_sec = {d['dur_new']:.1f}s (was {d['dur_ref']:.1f}s in un-recovered reference)")
    print("=" * 76 + "\n")

if __name__ == "__main__":
    main()
