"""
run_batch.py — Run the full promo pipeline on every episode and write the submission outputs.

Usage:
    python run_batch.py                         # all episodes, real Gemini calls
    python run_batch.py --episodes 1,2,3,41,42  # subset
    python run_batch.py --no-llm                # offline heuristic dry-run (NOT a submission run)
    python run_batch.py --no-cache              # force fresh LLM calls, bypassing disk cache
    python run_batch.py --scripts "path/to/episodes" --top-n 15

Responses are cached in outputs/llm_cache, so a run that dies on an API quota can simply be
re-run: finished calls are served from disk. Output files are plain JSON/CSV written by code
from model outputs; nothing is typed in by hand.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):  # Windows consoles
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src import config
from src.llm_client import GeminiClient, HeuristicClient
from src.parser import parse_all_episodes, save_parsed_episodes
from src.pipeline import run_pipeline, save_result

logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger("run_batch")


def _progress(stage: str, done: int, total: int) -> None:
    if done == total or done % 10 == 0:
        logger.info("%s: %d/%d", stage, done, total)


def main() -> int:
    t_start = time.time()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scripts", type=Path, default=config.SCRIPTS_DIR, help="folder with episode-*.txt files")
    ap.add_argument("--episodes", type=str, default="", help="comma-separated episode numbers (e.g. 1,2,3)")
    ap.add_argument("--top-n", type=int, default=config.TOP_N_FINAL, help="number of top ranked promos to output")
    ap.add_argument("--no-llm", action="store_true", help="offline heuristic dry-run (no API calls)")
    ap.add_argument("--no-cache", action="store_true", help="bypass disk cache and force fresh API calls")
    ap.add_argument("--no-baselines", action="store_true", help="skip baseline benchmark scoring")
    ap.add_argument("--no-critique", action="store_true", help="skip adversarial critique pass")
    args = ap.parse_args()

    config.ensure_output_dirs()

    # Parse and validate target episodes argument
    targets: list[int] | None = None
    if args.episodes.strip():
        try:
            targets = [int(x.strip()) for x in args.episodes.split(",") if x.strip()]
        except ValueError:
            logger.error("Invalid episode argument: %r. Must be comma-separated integers (e.g. --episodes 1,2,3).", args.episodes)
            return 2

    # Parse screenplays
    episodes_list = parse_all_episodes(args.scripts)
    save_parsed_episodes(episodes_list, config.PARSED_DIR)
    episodes = {e.episode_number: e for e in episodes_list}
    n_el = sum(e.total_elements for e in episodes_list)
    n_dl = sum(e.total_dialogue_lines for e in episodes_list)
    logger.info("Parsed %d episodes: %d elements, %d dialogue lines", len(episodes), n_el, n_dl)

    empty = [e.episode_number for e in episodes_list if e.total_elements == 0]
    if empty:
        logger.error("Episodes with ZERO parsed elements (format problem?): %s", empty)
        return 2

    # Client selection
    if args.no_llm:
        client = HeuristicClient()
        logger.warning("HEURISTIC DRY-RUN: outputs are NOT LLM results.")
    else:
        client = GeminiClient(use_cache=not args.no_cache)
        if not client.available:
            logger.error("No GEMINI_API_KEY (or google-genai missing). Set it in .env, or use --no-llm for an offline dry-run.")
            return 2

    # Execute full pipeline
    result = run_pipeline(
        episodes,
        client,
        target_episodes=targets,
        run_baselines=not args.no_baselines,
        critique=not args.no_critique,
        top_n=args.top_n,
        progress=_progress,
    )
    save_result(result)

    m = result.manifest
    total_elapsed = round(time.time() - t_start, 1)

    print("\n" + "=" * 72)
    print(f"DONE | mode={m['mode']} | episodes={m['n_episodes']} | candidates scored={m['candidates_scored']} | shortlist={m['shortlist_size']}")
    print(f"cost=${m['cost']['estimated_cost_usd']:.4f} | api calls={m['cost']['total_api_calls']} (cache hits {m['cache_hits']}) | {total_elapsed}s total")
    if m["fallback_to_heuristic"]["moments"] or m["fallback_to_heuristic"]["construction"] or m["fallback_to_heuristic"].get("scoring"):
        print(f"WARNING heuristic fallback used: {m['fallback_to_heuristic']}")
    if m["scoring_failures"]:
        print(f"WARNING scoring failed for: {m['scoring_failures']}")
    print("-" * 72)
    for r in result.ranked:
        p = r.promo
        segs = " + ".join(f"Ep{s.episode} {s.start_time}-{s.end_time}" for s in p.segments)
        print(f"#{r.rank:<2} {p.promo_id} {r.scores.composite:>5.2f}  {segs}  ({p.total_duration_sec:.1f}s, {p.creative_angle})")
        print(f"      hook: {p.hook[:100]}")
        print(f"      cliff: {p.cliffhanger[:100]}")
    print("-" * 72)
    print("baseline comparison (same blind scorer):", result.report.baseline_comparison)
    print(f"outputs written to: {config.OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
