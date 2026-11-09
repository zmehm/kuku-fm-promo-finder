# Stress audit

Offline stress tests and isolated packaged-app executions. No fresh API calls; unavailable API was simulated. No human quality or ad-conversion validation.

Passed: 47. Failed: 0.

- **PASS regression_suite**: exit=0; see regression_suite.log
- **PASS parser_valid_original**: accepted 8 elements; nonpositive intervals=0; expected complete parse
- **PASS parser_valid_crlf**: accepted 8 elements; nonpositive intervals=0; expected complete parse
- **PASS parser_valid_bom**: accepted 8 elements; nonpositive intervals=0; expected complete parse
- **PASS parser_valid_comma**: accepted 8 elements; nonpositive intervals=0; expected complete parse
- **PASS parser_valid_one_decimal**: accepted 8 elements; nonpositive intervals=0; expected complete parse
- **PASS parser_valid_three_decimals**: accepted 8 elements; nonpositive intervals=0; expected complete parse
- **PASS parser_valid_aliases**: accepted 8 elements; nonpositive intervals=0; expected complete parse
- **PASS parser_empty**: ValueError: No script elements parsed from empty.txt: check the timestamp/format
- **PASS parser_prose**: ValueError: No script elements parsed from prose.txt: check the timestamp/format
- **PASS parser_header_only**: ValueError: No script elements parsed from header_only.txt: check the timestamp/format
- **PASS parser_unsupported_header**: ValueError: Unsupported timed element header at line 8: **Captain [Mira]:**. Adapt the screenplay format before extraction.
- **PASS parser_inverted_element**: ValueError: Nonpositive element interval at line 8: 00:08.00-00:06.00
- **PASS parser_zero_length_element**: ValueError: Nonpositive element interval at line 8: 00:00.00-00:00.00
- **PASS parser_seconds_out_of_range**: ValueError: Invalid timestamp '00:99.00': seconds must be between 00 and 59.
- **PASS parser_malformed_timed_block**: ValueError: Missing or malformed timestamp after element header at line 8: **CAPTAIN (Mira) (Shocked):**
- **PASS parser_missing_timed_block**: ValueError: Missing or malformed timestamp after element header at line 8: **CAPTAIN (Mira) (Shocked):**
- **PASS parser_duplicate_episode**: ValueError: Multiple episode headers in one file; split concatenated or duplicated episodes first.
- **PASS invalid_timestamp_00:60.00**: Invalid timestamp '00:60.00': seconds must be between 00 and 59.
- **PASS invalid_timestamp_00:99.00**: Invalid timestamp '00:99.00': seconds must be between 00 and 59.
- **PASS invalid_timestamp_99:99.99**: Invalid timestamp '99:99.99': seconds must be between 00 and 59.
- **PASS timestamp_fuzz_1000**: 1000/1000 valid centisecond strings preserved
- **PASS catalog_timestamp_integrity**: 104 candidate/ranked entries checked; candidate issues=1, ranked issues=0
- **PASS ranked_dialogue_boundary_crossings**: 0/15 shortlisted promos cross dialogue boundaries
- **PASS validator_string_numeric_consistency**: A numeric 0s start labelled 00:12.00 must be flagged
- **PASS validator_repeated_internal_segment**: Same 0-24s segment repeated twice should be detected, not treated as a novel 48s promo
- **PASS validator_unknown_episode**: Unknown episode 999 flagged
- **PASS long_dialogue_judge_visibility**: A critical reveal after 400 characters is visible
- **PASS explicit_closure_review_hint**: Explicit ending closure is flagged for editor review, not certified semantically resolved
- **PASS new_threat_not_false_closure**: A new threat after apparent closure is not flagged as resolved
- **PASS offline_catalog_workers_1**: 50 episodes; 114 candidates; 15 shortlisted; 0.197s
- **PASS offline_catalog_workers_4**: 50 episodes; 114 candidates; 15 shortlisted; 0.212s
- **PASS offline_concurrency_stability**: 1 vs 4 workers; timings={'1': 0.197, '4': 0.212}
- **PASS offline_repeat_stability_20**: 20 repeated offline runs compared
- **PASS unavailable_api_fallback**: Shortlisted 1; fallbacks={'moments': [901], 'construction': [901], 'scoring': ['PR-001']}; critique failures=['PR-001']
- **PASS rule_adversarial_detection**: Caught 4/4 known bad cases; details in legacy_checks.log
- **PASS blind_review_content_integrity**: Exact pipeline source text verified in 3 handouts; pair counts=[10, 10, 10]
- **PASS packaged_private_hardening_parity**: Archived hardening code and tests exactly match the verified workspace
- **PASS packaged_private_regressions**: exit=0; see packaged_private_regressions.log
- **PASS packaged_private_dashboard**: exit=0; see packaged_private_dashboard.log
- **PASS packaged_private_offline**: exit=0; see packaged_private_offline.log
- **PASS packaged_public_hardening_parity**: Archived hardening code and tests exactly match the verified workspace
- **PASS packaged_public_regressions**: exit=0; see packaged_public_regressions.log
- **PASS packaged_public_dashboard**: exit=0; see packaged_public_dashboard.log
- **PASS packaged_public_offline**: exit=0; see packaged_public_offline.log
- **PASS combined_zip_integrity**: 45 outer entries checked
- **PASS submission_evidence_unchanged**: SHA-256 comparison of manifests, ranked output, reviewer ratings/results and combined ZIP