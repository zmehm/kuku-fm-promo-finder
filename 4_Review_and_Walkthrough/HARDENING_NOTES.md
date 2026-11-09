# Stress-test fixes

The original failing report is retained at outputs/stress_audit_20261109_013533/REPORT.md. The first repaired-workspace audit passes 43/43 checks at outputs/stress_audit_20261109_014153/REPORT.md. The final audit passes 47/47 checks at outputs/stress_audit_20261109_014612/REPORT.md, including exact code parity, all 42 regression tests, dashboard startup, and offline extraction inside both rebuilt archives.

Implemented changes:
- Reject invalid clock seconds, nonpositive element intervals, duplicated episode content, and repeated scene identifiers.
- Raise clear errors for missing/malformed timestamps. Catalog parsing now rejects partial catalogs and duplicate episode IDs rather than silently dropping inputs.
- Require agreement between timestamp labels and numeric seconds, finite values, valid source anchors, episode bounds, and non-overlapping internal segments. Violations are high severity.
- Preserve full dialogue/action text in model renderers. Oversized model input raises an explicit error at LLM_MAX_INPUT_CHARS (default 200,000); it is not silently truncated. Any heuristic fallback remains disclosed.
- Flag stale quoted ending descriptions separately from semantic cliffhanger resolution.
- Add a narrowly scoped explicit-closure review hint with negation/question/new-threat counterexamples. This is not a general semantic judge and does not prove suspense is resolved.

Benchmark correction:
The previous resolved-cliffhanger test merely put an intense line eight seconds before a cut. Its example actually continues toward another reveal, so the test did not establish resolution. It is now correctly a stale ending-claim test. Genuine closure and counterexamples are tested separately. This correction is disclosed; a higher detection count is not evidence of general semantic accuracy.

Verification:
42 regression tests pass. The strict parser still reads all 50 supplied episodes and 3,727 elements. Existing saved creative scores, baseline comparisons, and synthetic AI outputs are historical observations; they have not been rejudged under the full-text prompts. Human ratings remain pending. No fresh API calls are required for the hardening audit.

Run:
python -m pytest -q -p no:cacheprovider test_parser.py test_revision.py test_review_tools.py test_hardening.py
python stress_audit.py

Stress reports are versioned separately. Original failures are not deleted or overwritten. Current 15 shortlist clips retain valid timing; one rejected/raw candidate has a short-segment validation issue and is not in the shortlist.
