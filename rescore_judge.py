"""
rescore_judge.py — Re-score final shortlist and baselines with a stronger judge model.

Rules satisfied:
  * Reads GEMINI_JUDGE_MODEL_2 from environment (fails with a clear error if unset).
  * Loads top 15 promos from outputs/ranked/ranked_promos.json and all baselines from
    outputs/quality_report/baselines_scored.json, plus parsed episodes from outputs/parsed.
  * Reuses GeminiClient.score_promo() directly: exact same blind scoring prompt
    (SCORING_SYSTEM_PROMPT) and code-computed composite (compute_composite).
  * Uses the existing LLM cache where cache keys incorporate the model name to avoid collision.
  * Saves results to outputs/judge2/judge2_scores.json.
  * Prints:
      1. Mean composite per source under Judge 2.
      2. Spearman rank correlation between original and Judge 2 composites (reusing human_eval.spearman).
      3. Whether the pipeline shortlist mean remains above each baseline mean.
  * Leaves existing ranking and baseline files unmodified.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

load_dotenv()

# 1. Read GEMINI_JUDGE_MODEL_2 from env; fallback: clear error message
judge_model_2 = os.getenv("GEMINI_JUDGE_MODEL_2", "").strip()
if not judge_model_2:
    border = "=" * 72
    msg = (
        f"\n{border}\n"
        "ERROR: Environment variable GEMINI_JUDGE_MODEL_2 is not set.\n"
        f"{border}\n"
        "Please specify a stronger Gemini judge model before running.\n\n"
        "Examples:\n"
        "  PowerShell:  $env:GEMINI_JUDGE_MODEL_2 = 'gemini-flash-latest'\n"
        "               python rescore_judge.py\n\n"
        "  Bash:        export GEMINI_JUDGE_MODEL_2='gemini-flash-latest'\n"
        "               python rescore_judge.py\n\n"
        "  Or add GEMINI_JUDGE_MODEL_2=gemini-flash-latest to your .env file.\n"
        f"{border}\n"
    )
    sys.stderr.write(msg)
    sys.exit(1)

from src import config

# Set the active judge model to the second model so score_promo() uses it
config.GEMINI_JUDGE_MODEL = judge_model_2

from human_eval import spearman
from src.llm_client import GeminiClient
from src.parser import parse_all_episodes
from src.schemas import ParsedEpisode, PromoCandidate

logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger("rescore_judge")


def load_episodes() -> dict[int, ParsedEpisode]:
    """Load parsed episodes from outputs/parsed, or parse raw scripts as fallback."""
    episodes: dict[int, ParsedEpisode] = {}
    if config.PARSED_DIR.exists():
        for p in sorted(config.PARSED_DIR.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                ep = ParsedEpisode(**data)
                episodes[ep.episode_number] = ep
            except Exception as exc:
                logger.warning("Failed parsing %s: %s", p, exc)
    if not episodes:
        logger.info("PARSED_DIR empty or missing; parsing from raw scripts at %s...", config.SCRIPTS_DIR)
        for ep in parse_all_episodes(config.SCRIPTS_DIR):
            episodes[ep.episode_number] = ep
    return episodes


def load_candidates() -> list[dict]:
    """Load top 15 shortlist promos and all baseline candidates."""
    candidates: list[dict] = []

    # 1. Top 15 ranked shortlist promos
    ranked_path = config.RANKED_DIR / "ranked_promos.json"
    if not ranked_path.exists():
        raise FileNotFoundError(f"Missing {ranked_path}. Please run run_batch.py first.")
    ranked_data = json.loads(ranked_path.read_text(encoding="utf-8"))
    for item in ranked_data[:15]:
        promo = PromoCandidate.model_validate(item["promo"])
        orig_comp = float(item["scores"]["composite"])
        candidates.append({
            "promo_id": promo.promo_id,
            "source": "pipeline_shortlist",
            "promo": promo,
            "original_composite": orig_comp,
        })

    # 2. All baselines from baselines_scored.json
    baselines_path = config.QUALITY_DIR / "baselines_scored.json"
    if not baselines_path.exists():
        raise FileNotFoundError(f"Missing {baselines_path}. Please run run_batch.py first.")
    baselines_data = json.loads(baselines_path.read_text(encoding="utf-8"))
    for item in baselines_data:
        promo = PromoCandidate.model_validate(item["promo"])
        orig_comp = float(item.get("composite", item.get("scores", {}).get("composite", 0.0)))
        candidates.append({
            "promo_id": promo.promo_id,
            "source": item.get("source", "baseline"),
            "promo": promo,
            "original_composite": orig_comp,
        })

    return candidates


def main() -> None:
    logger.info("Initializing Judge 2 Rescoring with model: %s", judge_model_2)

    client = GeminiClient()
    if not client.available:
        logger.warning("Gemini API client not initialized. Only cached responses will be used.")

    episodes = load_episodes()
    logger.info("Loaded %d parsed episodes.", len(episodes))

    candidates = load_candidates()
    n_pipeline = sum(1 for c in candidates if c["source"] == "pipeline_shortlist")
    n_baselines = len(candidates) - n_pipeline
    logger.info("Loaded %d items to score (%d pipeline shortlist, %d baselines).",
                len(candidates), n_pipeline, n_baselines)

    scored_records: list[dict] = []

    for idx, item in enumerate(candidates, 1):
        promo = item["promo"]
        logger.info("[%d/%d] Scoring %s (%s)...", idx, len(candidates), promo.promo_id, item["source"])
        prev_hits = client.cache_hits
        scores, reasoning = client.score_promo(promo, episodes)

        record = {
            "promo_id": item["promo_id"],
            "source": item["source"],
            "judge2_model": judge_model_2,
            "original_composite": item["original_composite"],
            "judge2_composite": scores.composite,
            "judge2_scores": scores.model_dump(),
            "judge2_reasoning": reasoning,
        }
        scored_records.append(record)

        # Pace live uncached API calls to stay within free-tier 15 RPM limit smoothly
        if client.cache_hits == prev_hits and idx < len(candidates):
            time.sleep(2.0)

    # Save to outputs/judge2/judge2_scores.json
    judge2_dir = config.OUTPUT_DIR / "judge2"
    judge2_dir.mkdir(parents=True, exist_ok=True)
    out_file = judge2_dir / "judge2_scores.json"
    out_file.write_text(json.dumps(scored_records, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Saved %d scored records to %s", len(scored_records), out_file)

    # Compute source statistics
    source_j2: dict[str, list[float]] = defaultdict(list)
    source_orig: dict[str, list[float]] = defaultdict(list)
    all_orig: list[float] = []
    all_j2: list[float] = []

    for r in scored_records:
        source_j2[r["source"]].append(r["judge2_composite"])
        source_orig[r["source"]].append(r["original_composite"])
        all_orig.append(r["original_composite"])
        all_j2.append(r["judge2_composite"])

    # Spearman rank correlation across all evaluated items
    rho = spearman(all_orig, all_j2)

    # Output formatted summary
    border = "=" * 74
    line = "-" * 74
    print(f"\n{border}")
    print(f"JUDGE 2 RESCORING SUMMARY (Model: {judge_model_2})")
    print(border)
    print(f"Total evaluated items: {len(scored_records)} (Cache hits: {client.cache_hits})")
    print(f"Spearman rank correlation (Original Judge vs Judge 2): rho = {rho if rho is not None else 'N/A'}")
    print(line)
    print(f"{'Source':<25} | {'N':<4} | {'Orig Mean':<10} | {'Judge 2 Mean':<12} | {'Delta':<8}")
    print(line)

    for src in sorted(source_j2.keys(), key=lambda s: -sum(source_j2[s]) / len(source_j2[s])):
        j2_mean = sum(source_j2[src]) / len(source_j2[src])
        orig_mean = sum(source_orig[src]) / len(source_orig[src])
        delta = j2_mean - orig_mean
        print(f"{src:<25} | {len(source_j2[src]):<4} | {orig_mean:<10.2f} | {j2_mean:<12.2f} | {delta:+<8.2f}")

    print(line)
    print("PIPELINE SHORTLIST VS BASELINES UNDER JUDGE 2:")

    pipe_j2_mean = sum(source_j2.get("pipeline_shortlist", [0])) / max(len(source_j2.get("pipeline_shortlist", [])), 1)

    all_beaten = True
    for base_src in sorted(k for k in source_j2.keys() if k != "pipeline_shortlist"):
        base_mean = sum(source_j2[base_src]) / len(source_j2[base_src])
        diff = pipe_j2_mean - base_mean
        win = diff > 0
        if not win:
            all_beaten = False
        status = f"BEATEN (+{diff:.2f})" if win else f"TRAILS ({diff:.2f})"
        print(f"  - vs {base_src:<22}: Shortlist {pipe_j2_mean:.2f} vs Baseline {base_mean:.2f} -> {status}")

    verdict = (
        "YES — Pipeline shortlist outperforms all baselines under Judge 2"
        if all_beaten
        else "NO — Pipeline shortlist does not beat all baselines under Judge 2"
    )
    print(f"\nPipeline shortlist mean > each baseline mean: {verdict}")
    print(f"{border}\n")


if __name__ == "__main__":
    main()
