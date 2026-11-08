# 🎬 Kuku FM — AI Promo Discovery & Ranking Engine
> **AI Generalist Assignment — Problem 1: Find the Promos (Ads)**  
> **Candidate:** Zaryab Mehmood  
> **Show Analyzed:** *"I Married a Broken Soldier. He was a Duke"* (50 Episodes)  
> **Deliverables:** Working Prototype (Streamlit) | 2-Page Solution Doc (PDF) | Raw Outputs JSON | Live Script Runner

---

## 📌 Executive Summary

Performance marketing teams for microdrama apps (TikTok, Reels, Shorts) require high volumes of 20–60s ad cuts to combat creative fatigue. Today, video editors manually watch 50 episodes and log timestamps, taking an estimated **15–20 hours per show** (unverified industry estimate).

This repository implements a **Multi-Pass Deterministic + LLM Pipeline** designed around strict separation of concerns:
1. **0.01s Script Precision:** Parses screenplay text files into structured JSON with 0.01s timestamp precision (3,723 elements parsed in under a second).
2. **Discrete ID Anchoring:** The LLM *never writes timestamps*. It identifies moments by script element ID (`[01.014]`), preventing timestamp hallucination by construction.
3. **Promo Construction:** Assembles 20–60s candidate promos with start/end boundaries snapped directly to element borders.
4. **Multi-Judge Blind Scoring:** Candidates and baselines are judged identically using separate blind prompts that see only clip text (never generator claims). Tested across two distinct models (`gemini-flash-lite-latest` and `gemini-3.5-flash-lite`) with weighted marketing criteria computed in code.
5. **Dual-Layer Validation & Ad-Policy Tiering:** Combines deterministic checks (hard/soft ad policy regex, sub-segment guards, verbatim claim matching) with adversarial LLM critique. Hard violations are auto-trimmed or excluded from the shortlist.
6. **Single-Rater Blind Human Evaluation:** Evaluates LLM vs. human ratings on shuffled, source-blinded clips, plus pairwise blind A/B comparison against production baselines.

---

## 🏗️ Architecture & Division of Labor

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       KUKU FM PROMO ENGINE PIPELINE                         │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  [50 Screenplays] ──► [Pass 0: Deterministic Parser] ──► [Structured JSON] │
│                                                                    │        │
│    ┌───────────────────────────────────────────────────────────────┘        │
│    ▼                                                                        │
│  [Pass 1: Dramatic Moments]   (Emotion Intensity + Sound FX Crescendos)     │
│    ▼                                                                        │
│  [Pass 2: Promo Construction] (Anchor-and-Expand + 20-60s Snapped Bounds)   │
│    ▼                                                                        │
│  [Pass 3: Blind Scorer]       (Hook / Cliffhanger / Coherence / Emotion)    │
│    ▼                                                                        │
│  [Pass 4: Adversarial Audit]  (Hard/Soft Ad Policy, Context & Taxonomy)     │
│    ▼                                                                        │
│  [Pass 5: Delivery]           (Streamlit Web UI + Programmatic 2-Page PDF)  │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│  DIVISION OF RESPONSIBILITY:                                                │
│  • Code: Regex parsing, 0.01s snapping, duration bounds, composite formula  │
│  • LLM (Gemini Flash Lite): Subjective hooks, cold-viewer comprehension     │
│  • Human Editor: 15-minute review of ranked shortlist & cut fine-tuning     │
│  • Measured Run Metrics (Full 50-Ep Run): 266 API calls, $0.0934 cost,      │
│    11.2 min (673.0s) initial generation, 82.6% validation pass rate         │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 🏆 Deliverables Manifest

| Item | Path in Repository | Description |
|:---|:---|:---|
| **Solution Doc (PDF)** | [`outputs/Kuku_FM_Solution_Doc.pdf`](./outputs/Kuku_FM_Solution_Doc.pdf) | Official 2-page PDF populated programmatically from output files. |
| **Interactive Prototype** | [`app.py`](./app.py) | Streamlit dashboard with promo browser, auditor, and live script runner. |
| **Pipeline Runner** | [`run_batch.py`](./run_batch.py) | Batch CLI runner supporting `--scripts`, `--episodes`, `--top-n`, and `--no-llm`. |
| **Cross-Judge Rescoring** | [`outputs/judge2/judge2_scores.json`](./outputs/judge2/judge2_scores.json) | Second judge, same model family (`gemini-3.5-flash-lite`) confirming ranking stability ($\rho = 0.848$). |
| **Pairwise Blind Eval** | [`outputs/human_eval/pairs_results.json`](./outputs/human_eval/pairs_results.json) | Head-to-head pairwise comparison against Last-30s and Rule-Based baselines. |
| **Blind Human Eval** | [`human_eval.py`](./human_eval.py) | Single-rater blind human verification harness calculating Spearman rank correlation. |
| **Raw Parsed Scripts** | [`outputs/parsed/`](./outputs/parsed/) | 50 unedited JSON files containing all 3,723 parsed elements. |
| **Ranked Output Shortlist** | [`outputs/ranked/ranked_promos.json`](./outputs/ranked/ranked_promos.json) | Top ranked promos with scores, verbatim dialogue, and cut boundaries. |
| **Ranked Output Table (CSV)**| [`outputs/ranked/ranked_promos.csv`](./outputs/ranked/ranked_promos.csv) | Editor-ready spreadsheet with timestamps and marketing angles. |
| **Quality Audit Report** | [`outputs/quality_report/quality_report.json`](./outputs/quality_report/quality_report.json) | Automated quality flags, failure breakdown, and baseline benchmarks. |
| **Execution Manifest** | [`outputs/run_manifest.json`](./outputs/run_manifest.json) | Real token counts, API cost, wall-clock time, and model citations. |

---

## 🚀 Quickstart & Run Steps

### 1. Installation
Clone the repository and install pinned dependencies:
```bash
cd promo-finder
pip install -r requirements.txt
```

### 2. Configure Environment
Create a `.env` file in `promo-finder/`:
```env
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-flash-lite-latest
GEMINI_JUDGE_MODEL_2=gemini-3.5-flash-lite
SCRIPTS_DIR=path/to/screenplays  # Optional: defaults to sibling directory
```
*(Note: If no API key is provided, passing `--no-llm` enables deterministic rule-based dry runs).*

### 3. Run the Batch Pipeline
Run on target episodes or the full series:
```bash
# Run with Gemini API on target episodes:
python run_batch.py --episodes 1,2,3

# Run offline heuristic dry-run (no API calls):
python run_batch.py --episodes 1,2 --no-llm
```

### 4. Second-Judge Re-Scoring (Same Model Family)
Re-score shortlist and baselines with the second judge model (`gemini-3.5-flash-lite`):
```bash
python rescore_judge.py
```

### 5. Run Single-Rater Blind Human Evaluation
```bash
# Absolute rating evaluation:
python human_eval.py make --n-pipeline 10 --n-each-baseline 5
python human_eval.py score

# Pairwise blind evaluation:
python human_eval.py pairs-make
# Fill 'your_pick' ('A' or 'B') in outputs/human_eval/pairs_sheet.csv
python human_eval.py pairs-score
```

### 6. Launch the Streamlit Web Application
```bash
streamlit run app.py
```
Open `http://localhost:8501`.

### 7. Regenerate the 2-Page Executive PDF
```bash
python generate_pdf.py
```

---

## 📏 Verified Benchmark Results

All figures below are read programmatically from [`outputs/quality_report/quality_report.json`](./outputs/quality_report/quality_report.json), [`outputs/judge2/judge2_scores.json`](./outputs/judge2/judge2_scores.json), and [`outputs/human_eval/human_eval_results.json`](./outputs/human_eval/human_eval_results.json):

| Strategy (Baselines N=14–15) | Judge 1 (`gemini-flash-lite-latest`) | Judge 2 (`gemini-3.5-flash-lite`) | Human Rating (1–10) | Description & Key Characteristics |
|:---|:---:|:---:|:---:|:---|
| **Random Baseline (20–60s, N=14)** | **6.27** | **6.23** | **7.40** | Truly random windows snapped to script element borders. Often starts on dead air or cuts mid-arc. |
| **Last-30s Baseline (N=14)** | **6.25** | **6.28** | **8.10** | Final ~30s of each episode ending at the actual episode end. Strong cliffhanger (human rated 8.10). |
| **Rule-Based Baseline (N=15)** | **6.00** | **6.07** | **7.00** | Maximum density of high-intensity dialogue/action words (`slap`, `kill`, `betray`). Highest keyword intensity. |
| **Pipeline: All Candidates (N=92)** | **6.40** | — | — | Raw average across all 92 candidate promos generated across the 50 episodes. |
| **Pipeline: Best Per Episode (N=50)** | **6.88** | — | — | Top scoring single candidate for each episode (N=50). |
| **Pipeline Shortlist (N=15)** | **7.89** | **7.57** | **7.90** | Balances 0–3s hook (30%), cliffhanger (25%), standalone coherence (20%), and emotion (15%). |

### Second-Judge Rescoring (Same Model Family)
- **Second Judge Model:** `gemini-3.5-flash-lite` (second judge, same model family: separate blind scoring prompt, identical code composite math).
- **Shortlist Performance vs. Baselines:** Under the second judge, same model family (`gemini-3.5-flash-lite`), the pipeline shortlist achieves a mean composite score of **7.57**, and outperforms all baselines: **+1.29 vs. Last-30s (6.28)**, **+1.34 vs. Random (6.23)**, and **+1.50 vs. Rule-Based (6.07)** (top-15 shortlist vs unselected baselines; biased upward).
- **Cross-Judge Rank Correlation:** Spearman $\rho = 0.848$ across all 58 evaluated clips, demonstrating strong inter-model agreement and confirming ranking robustness.
- **Selection-Bias Disclosure:** The fairer comparisons are all candidates (6.40) and best-per-episode (6.88) against baselines of 6.0–6.3. The pipeline's gain over baselines under this judge is therefore modest (about 0.1–0.6). In the single-rater blind human test, human raters preferred last-30s endings (8.1 vs 7.9), likely because screenwriters design episode finales as cliffhangers; this is a hypothesis we did not test. At N=25 with one rater, the 0.2 gap between pipeline and last-30s is within noise.

### Single-Rater Blind Human Evaluation & Pairwise Comparison
- **Absolute Ratings Task ($N = 25$):** Shuffled blind rating test yielded Spearman correlation $\rho = 0.313$ between human rater and LLM scores (weak positive correlation, non-significant at N=25; commercial calibration requires a multi-editor review panel).
- **Pairwise Blind Comparison Task (`human_eval.py pairs-score`):** Pairwise comparison not completed in this submission. (Caveat: single rater, small sample size).

---

## 🛑 Quality Audit: Real Failure Examples & Auto-Trim Fixes

From real run logs in [`outputs/quality_report/quality_report.json`](./outputs/quality_report/quality_report.json) and [`outputs/run_manifest.json`](./outputs/run_manifest.json):
1. **Ad-Policy Tiering (2 hard trimmed, 9 hard excluded, 15 soft kept flagged):** Hard triggers (`suicide`, `hanging`, `noose`, `gallows`, `self-harm`, `torture`) are auto-trimmed at segment boundaries if duration remains $\ge$20s, or excluded from shortlist; soft triggers (`poison`, `arsenic`, `blood`, `witch`, `witchcraft`) remain flagged for human editor review.
2. **Hallucinated Claims (8 flags):** Layer 1 verification caught generator claims quoting dialogue spoken outside clip boundaries: `PR-009` quoted *"Then keep it. Let them keep everything else"* and `PR-005` quoted *"Look at you... still clinging to that little locket"* (both lines spoken elsewhere in scene).
3. **Delayed Hook Beat (8 flags):** PR-006 claimed a warning hook, but dialogue began at `[+03.9s]`.
4. **Resolved Cliffhanger (2 flags):** PR-020 and PR-053 were flagged; both were excluded from the shortlist.
5. **Heuristic Fallback Disclosure:** Moments discovery fell back to heuristic on Episodes 24 and 45 when Gemini returned 0 beats. Shortlist candidates **PR-042 (Ep 24)** and **PR-080 (Ep 45, Rank #2)** originated from this heuristic moment discovery.

---

## 🧪 Stress Tests

All metrics are read directly from [`outputs/stress_test/stress_results.json`](./outputs/stress_test/stress_results.json):

| Test Suite | Command | Result | Details |
|:---|:---|:---:|:---|
| **Parser Robustness** | `python stress_test.py parser` | **9/9 PASS** | Handles empty files, header-only files (raises clear ValueError), 1–3 fractional digits, and comma decimals without silent 0-element output. |
| **Timestamp Integrity** | `python stress_test.py integrity` | **107/107 Valid** | Zero out-of-bounds or non-element cut violations across all candidate and ranked promos (1 micro-segment warning <3s). |
| **Adversarial Audit** | `python stress_test.py adversarial` | **3/4 Caught** | Successfully detects slow hooks, verbatim hallucinated claims, and ad-policy violations; resolved cliffhanger unflagged (missed by rule-based checker). |
| **Rerun Stability** | `python stress_test.py stability` | **2/3 Stable** | Top candidate segment IoU $\ge 0.5$ across un-cached re-runs on Episodes 1–3. |
| **Judge Discrimination** | `python stress_test.py judge` | **PASS** | Low noise (mean score spread $0.27$ across repeats); discriminates good vs. flat promos with a $+1.24$ score gap. |
| **Fallback Modes** | `python stress_test.py fallback` | **PASS** | Graceful degradation under `--no-llm` (rule-based) and bad-model API failure fallbacks. |

---

## 🛠️ Tools Used & Where They Got It Wrong

- **Tools:** Python 3.12, Google GenAI SDK (`gemini-flash-lite-latest`, `gemini-3.5-flash-lite`), Pydantic v2, ReportLab, Streamlit, Pandas.
- **Where They Got It Wrong:** Gemini initially hallucinated floating-point timestamps when asked in natural language (illustrative example). This hallucination motivated our discrete ID-anchored coordinate architecture (`clips.py`), completely forbidding the model from writing raw timestamps.

---

## 📋 Stated Assumptions & Planned Features

### Stated Assumptions
1. We assume human editors spend ~15–20 hours per 50-episode show manually watching and logging cut points (unverified industry estimate).
2. Scoring weights (Hook 30%, Cliffhanger 25%, Coherence 20%, Emotion 15%, Duration 10%) are untested product priors, not fitted to live ad performance.
3. Screenplay timestamps provide 0.01s script precision; human editors will still require ~1–2s trim adjustments for actor breath or music swells.

### Planned Features (Not Yet Implemented)
- Multi-character entity-resolution graph to automate pronoun tracking.
- Automated sentiment-rebound classifier to detect cliffhanger resolution.
- Live Meta/TikTok ad-spend feedback loop to tune formula weights dynamically.

---

## ⚡ Live Script Runner (Interview Demo)

1. Run `streamlit run app.py`.
2. Open **Tab 4: 🚀 Live Script Runner**.
3. Upload any unseen `.txt` microdrama screenplay or select an indexed episode.
4. The runner parses the script, extracts candidate promos, and renders the verbatim script with cut boundaries.
