"""
spot_check.py — Verifies numbers and claims across PDF, README, and JSON outputs.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
import pypdf

from human_eval import spearman

# 1. Load output files
m_path = Path("outputs/run_manifest.json")
m_init_path = Path("outputs/run_manifest_initial_run.json")
q_path = Path("outputs/quality_report/quality_report.json")
h_path = Path("outputs/human_eval/human_eval_results.json")
j2_path = Path("outputs/judge2/judge2_scores.json")
pr_path = Path("outputs/human_eval/pairs_results.json")

for p in [m_path, m_init_path, q_path, h_path, j2_path, pr_path]:
    if not p.exists():
        raise FileNotFoundError(f"Missing required file: {p}")

m = json.loads(m_path.read_text(encoding="utf-8"))
m_init = json.loads(m_init_path.read_text(encoding="utf-8"))
q = json.loads(q_path.read_text(encoding="utf-8"))
h = json.loads(h_path.read_text(encoding="utf-8"))
j2 = json.loads(j2_path.read_text(encoding="utf-8"))
pr = json.loads(pr_path.read_text(encoding="utf-8"))
readme = Path("README.md").read_text(encoding="utf-8")

pdf_reader = pypdf.PdfReader("outputs/Kuku_FM_Solution_Doc.pdf")
pdf_text = "".join(page.extract_text() for page in pdf_reader.pages)
n_pages = len(pdf_reader.pages)

print(f"\n=======================================================")
print(f" PDF SPOT CHECK & VERIFICATION REPORT")
print(f"=======================================================")
print(f"PDF Page count: {n_pages} (Required: 2)")
assert n_pages == 2, f"PDF page count must be exactly 2, got {n_pages}!"

# Compute Judge 2 stats dynamically from JSON data
j2_by_source = defaultdict(list)
all_orig = []
all_j2 = []
for row in j2:
    j2_by_source[row["source"]].append(row["judge2_composite"])
    all_orig.append(row["original_composite"])
    all_j2.append(row["judge2_composite"])

j2_means = {src: sum(vals) / len(vals) for src, vals in j2_by_source.items()}
j2_model = j2[0]["judge2_model"]
j2_rho = spearman(all_orig, all_j2)

# Build verification checks comparing PDF and README text directly to JSON outputs
checks = [
    ("n_episodes", str(m["n_episodes"])),
    ("api_calls", str(m["cost"]["total_api_calls"])),
    ("cost_usd", f"{m['cost']['estimated_cost_usd']:.4f}"),
    ("pass_rate", f"{q['validation_pass_rate']:.1f}%"),
    ("shortlist_mean_judge1", f"{q['mean_composite_score']:.2f}"),
    ("judge2_model", j2_model),
    ("judge2_shortlist_mean", f"{j2_means['pipeline_shortlist']:.2f}"),
    ("judge2_last30_mean", f"{j2_means['last_30_sec_baseline']:.2f}"),
    ("judge2_random_mean", f"{j2_means['random_baseline']:.2f}"),
    ("judge2_rule_mean", f"{j2_means['rule_based_baseline']:.2f}"),
    ("judge2_spearman_rho", f"{j2_rho:.3f}"),
    ("judge2_beats_baselines", "outperforms all baselines"),
    ("judge2_biased_upward", "biased upward"),
    ("judge2_same_family", "same model family"),
    ("wall_min", f"{m_init['wall_seconds'] / 60.0:.1f} min"),
]

if pr["n_pairs"] == 0:
    checks.append(("pairs_not_completed", "Pairwise comparison not completed in this submission"))
    checks.append(("pairs_caveat", "single rater"))
else:
    checks.append(("pairs_vs_last30", f"{pr['vs_last_30_sec']['pipeline_wins']}/{pr['vs_last_30_sec']['total']}"))
    checks.append(("pairs_vs_rule", f"{pr['vs_rule_based']['pipeline_wins']}/{pr['vs_rule_based']['total']}"))
    checks.append(("pairs_p_value", f"{pr['p_value_two_sided']:.4f}"))
    checks.append(("pairs_caveat", "single rater"))

checks.extend([
    ("ad_hard_trimmed", f"{m['ad_policy']['hard_trimmed']} hard trimmed"),
    ("ad_hard_excluded", f"{m['ad_policy']['hard_excluded']} hard excluded"),
    ("ad_soft_kept", f"{m['ad_policy']['soft_kept_flagged']} soft kept flagged"),
    ("selection_bias_disclosure", "selection-bias disclosure"),
    ("single_rater_ratings_rho", f"{h['spearman_human_vs_llm']:.3f}"),
])

# Conditional fallback check: only if moments is non-empty
if m["fallback_to_heuristic"]["moments"]:
    checks.append(("fallback_moments_PR080", "PR-080"))
    checks.append(("fallback_moments_PR042", "PR-042"))

passed = 0
print("-" * 55)
for name, val in checks:
    in_pdf = val.lower() in pdf_text.lower()
    in_readme = val.lower() in readme.lower()
    status = "OK" if in_pdf and in_readme else f"FAIL (PDF:{in_pdf}, README:{in_readme})"
    print(f"{name:<26} [{val:<20}]: {status}")
    if in_pdf and in_readme:
        passed += 1

print("-" * 55)
print(f"Verification Results: {passed}/{len(checks)} checks passed.")
print("=======================================================\n")

assert passed == len(checks), f"Only {passed}/{len(checks)} checks passed!"
