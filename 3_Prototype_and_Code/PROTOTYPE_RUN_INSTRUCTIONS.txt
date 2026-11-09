# Kuku FM - Find the Promos

Zeeshan Mehmood | Problem 1

The tool parses all 50 supplied episodes, proposes continuous or ordered multi-segment 20-60-second cuts, ranks them, and records where its choices are weak. It identifies cuts only; it does not add editing, music, or overlays.

## Run

Python 3.10+. Extract source_code.zip, then:

    pip install -r requirements.txt
    streamlit run app.py

Saved recommendations open without an API key. For new extraction, use Live Script Runner with explicit Offline or Gemini mode. Gemini requires GEMINI_API_KEY in .env and available quota; GEMINI_MODEL and GEMINI_JUDGE_MODEL can override models. Public source starts without proprietary saved results and includes synthetic input examples.

Input is UTF-8 .txt using the supplied timestamped scene/ACTION/dialogue format. Different screenplay layouts, plain prose, PDFs and subtitles need adapters. Unsupported timed headers fail explicitly. For a batch: python run_batch.py --scripts "path/to/episodes" --no-llm --no-baselines. Set OUTPUT_DIR to a new folder to preserve submission artifacts.

## Required deliverables

- outputs/revised/ranked/ranked_promos.json and .csv: 15 ranked cuts, every segment in order, source episode/timestamps, hook, cliffhanger, why it works, and posting rationale.
- outputs/revised/RANKED_PROMOS.md: readable posting order with all segments and quality caveats.
- outputs/revised/QUALITY_REVIEW.md: source review of all 15 picks and concrete bad-pick examples.
- outputs/Kuku_FM_Solution_Doc.pdf: two-page explanation of method, results, and limitations.

Human evaluation is optional and is not a requirement or completion gate for this assignment. Earlier review scaffolding and historical evidence are retained for traceability, not presented as completed evaluation or as required next steps.

## What was verified

Saved catalog evidence covers 50 episodes / 3,727 elements, 89 candidates and 15 shortlisted cuts across 14 episodes. All saved cuts satisfy duration and timestamp checks, with no dialogue crossing boundaries. Source descriptions were corrected without changing boundaries or historical model scores. PR-027's hook is the order/refusal before 3s, not the later shove; PR-046 starts with a weak call to Rupert, not the later command. Softer reveal-based endings are explicitly identified.

Scores are historical model judgments, not audience or conversion measurements. The catalog replay uses cached responses; the attempted fresh full-catalog run stopped at quota. No replacement live-run success is claimed. Authored quality cases and offline regressions measure bounded behaviors, not general creative quality. External Shakespeare input ran uncached but used estimated timing and retained an unsupported modifier that passed the model auditor.

## Checks

    python -m pytest -q -p no:cacheprovider test_parser.py test_revision.py test_review_tools.py test_hardening.py test_human_review.py test_quality_checks.py
    python verify_submission.py
    python generate_pdf.py

Optional review-helper tests preserve existing behavior; collecting human ratings is not necessary to run the tool or complete the assignment.

Private archives include Kuku-derived material and belong in the designated assignment channel. source_code_public.zip excludes proprietary outputs and credentials. API prices and editor-time savings are assumptions, not verified billing or measured savings.
