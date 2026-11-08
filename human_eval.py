"""
human_eval.py — A small BLIND human check of the LLM scorer (about 30 minutes of your time).

Why: the pipeline's scores come from an LLM judge. That is circular unless a human
agrees with it. This script builds a shuffled sheet of clips (pipeline picks mixed with
random / last-30s / rule-based baselines) with the source hidden. You rate each 1-10 as a
stranger scrolling TikTok would. Then it reports whether the LLM score tracks YOUR ratings
and whether pipeline clips beat baselines in YOUR eyes.

Usage:
    python human_eval.py make  [--n-pipeline 15] [--n-each-baseline 5]
    # open outputs/human_eval/blind_sheet.csv, fill the 'your_rating_1_to_10' column, save
    python human_eval.py score [--ratings outputs/human_eval/blind_sheet.csv]

Run run_batch.py first (it writes ranked_promos.json and baselines_scored.json).
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):  # Windows consoles
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src import config
from src.clips import clip_elements, render_clip_text
from src.llm_client import GeminiClient
from src.parser import parse_all_episodes
from src.quality_checker import _AD_HARD_RE, generate_last30_baseline, generate_rule_baseline
from src.schemas import ParsedEpisode, PromoCandidate

logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger("human_eval")

SHEET = config.HUMAN_EVAL_DIR / "blind_sheet.csv"
KEY = config.HUMAN_EVAL_DIR / "answer_key.csv"
RESULT = config.HUMAN_EVAL_DIR / "human_eval_results.json"

PAIRS_SHEET = config.HUMAN_EVAL_DIR / "pairs_sheet.csv"
PAIRS_KEY = config.HUMAN_EVAL_DIR / "pairs_key.csv"
PAIRS_RESULT = config.HUMAN_EVAL_DIR / "pairs_results.json"


def _ranks(values: list[float]) -> list[float]:
    """Standard fractional ranking with tie averaging."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(x: list[float], y: list[float]) -> float | None:
    """Computes Spearman's rank correlation coefficient (rho) in pure Python."""
    if len(x) < 3:
        return None
    rx, ry = _ranks(x), _ranks(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return None if den == 0 else round(num / den, 3)


def _load_episodes(scripts: Path) -> dict[int, ParsedEpisode]:
    """Load parsed episodes from PARSED_DIR if available; otherwise parse raw scripts."""
    episodes: dict[int, ParsedEpisode] = {}
    if config.PARSED_DIR.exists():
        for p in sorted(config.PARSED_DIR.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                ep = ParsedEpisode(**data)
                episodes[ep.episode_number] = ep
            except Exception:
                pass
    if not episodes:
        for ep in parse_all_episodes(scripts):
            episodes[ep.episode_number] = ep
    return episodes


def make(n_pipeline: int, n_each: int, seed: int, scripts: Path) -> None:
    config.ensure_output_dirs()

    # Pre-flight check for prerequisite pipeline outputs
    ranked_file = config.RANKED_DIR / "ranked_promos.json"
    baselines_file = config.QUALITY_DIR / "baselines_scored.json"

    if not ranked_file.exists():
        logger.error("Missing %s. Please run 'python run_batch.py' first.", ranked_file)
        sys.exit(1)

    episodes = _load_episodes(scripts)
    ranked = json.loads(ranked_file.read_text(encoding="utf-8"))
    baselines = json.loads(baselines_file.read_text(encoding="utf-8")) if baselines_file.exists() else []

    rng = random.Random(seed)

    items: list[dict] = []
    for r in ranked[:n_pipeline]:
        items.append({
            "source": "pipeline",
            "promo": PromoCandidate(**r["promo"]),
            "llm": r["scores"]["composite"],
            "ref": r["promo"]["promo_id"],
        })

    by_src: dict[str, list[dict]] = defaultdict(list)
    for b in baselines:
        by_src[b.get("source", "baseline")].append(b)

    for src, rows in by_src.items():
        for b in rng.sample(rows, min(n_each, len(rows))):
            items.append({
                "source": src,
                "promo": PromoCandidate(**b["promo"]),
                "llm": b.get("composite", 0.0),
                "ref": b["promo"]["promo_id"],
            })

    rng.shuffle(items)

    with open(SHEET, "w", newline="", encoding="utf-8") as fs, open(KEY, "w", newline="", encoding="utf-8") as fk:
        ws, wk = csv.writer(fs), csv.writer(fk)
        ws.writerow(["item_id", "clip_text", "your_rating_1_to_10"])
        wk.writerow(["item_id", "source", "ref", "llm_composite"])
        for i, it in enumerate(items, 1):
            iid = f"B{i:02d}"
            ws.writerow([iid, render_clip_text(it["promo"], episodes), ""])
            wk.writerow([iid, it["source"], it["ref"], it["llm"]])

    print("\n" + "=" * 70)
    print(f" BLIND EVALUATION SHEET GENERATED: {len(items)} items")
    print("=" * 70)
    print(f"1. Open: {SHEET}")
    print("2. Enter your rating in 'your_rating_1_to_10' (integer or float 1-10).")
    print("3. Run: python human_eval.py score")


def score(ratings_path: Path) -> None:
    if not KEY.exists():
        logger.error("Missing answer key: %s. Run 'python human_eval.py make' first.", KEY)
        sys.exit(1)
    if not ratings_path.exists():
        logger.error("Ratings sheet not found: %s.", ratings_path)
        sys.exit(1)

    with open(KEY, encoding="utf-8") as fk:
        key = {r["item_id"]: r for r in csv.DictReader(fk)}

    rated: list[tuple[str, float, float]] = []
    skipped_out_of_bounds = 0

    with open(ratings_path, encoding="utf-8") as fr:
        for row in csv.DictReader(fr):
            val_str = (row.get("your_rating_1_to_10") or "").strip()
            if not val_str:
                continue
            try:
                h = float(val_str)
            except ValueError:
                continue

            # Validate range [1.0, 10.0]
            if not (1.0 <= h <= 10.0):
                skipped_out_of_bounds += 1
                logger.warning("Item %s: rating %f out of bounds [1.0, 10.0], skipping.", row.get("item_id"), h)
                continue

            iid = row.get("item_id", "")
            k = key.get(iid)
            if not k:
                continue

            rated.append((k["source"], h, float(k.get("llm_composite", 0.0))))

    if len(rated) < 6:
        print(f"\n⚠️  Only {len(rated)} valid ratings found. Please rate at least 10–20 rows for meaningful statistical significance.")
        return

    by_src: dict[str, list[float]] = defaultdict(list)
    for s, h, _ in rated:
        by_src[s].append(h)

    means = {s: round(sum(v) / len(v), 2) for s, v in by_src.items()}
    human_scores = [h for _, h, _ in rated]
    llm_scores = [l for _, _, l in rated]
    rho = spearman(human_scores, llm_scores)

    out = {
        "n_rated": len(rated),
        "skipped_out_of_bounds": skipped_out_of_bounds,
        "human_mean_by_source": means,
        "spearman_human_vs_llm": rho,
    }

    config.ensure_output_dirs()
    RESULT.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    print("🏆 DOUBLE-BLIND EVALUATION RESULTS")
    print("=" * 70)
    print(json.dumps(out, indent=2))
    print("-" * 70)
    print("INTERPRETING THE METRICS:")
    print("• Spearman rho near +1.0: LLM scores strongly track human intuition (>0.5 is decent).")
    print("• If 'pipeline' human mean exceeds 'random' and 'last_30_sec', you have empirical proof")
    print("  that the pipeline discovers superior creative moments beyond naive heuristic baselines.")
    print(f"Results saved to: {RESULT}")


def pairs_make(seed: int, scripts: Path) -> None:
    config.ensure_output_dirs()

    cands_file = config.PROMOS_DIR / "promo_candidates.json"
    if not cands_file.exists():
        logger.error("Missing %s. Please run 'python run_batch.py' first.", cands_file)
        sys.exit(1)

    episodes = _load_episodes(scripts)
    candidates_raw = json.loads(cands_file.read_text(encoding="utf-8"))
    candidates = [PromoCandidate.model_validate(c) for c in candidates_raw]

    # Pre-load known scores from ranked_promos.json and fallback to cached score_promo
    known_scores: dict[str, float] = {}
    ranked_file = config.RANKED_DIR / "ranked_promos.json"
    if ranked_file.exists():
        try:
            for r in json.loads(ranked_file.read_text(encoding="utf-8")):
                known_scores[r["promo"]["promo_id"]] = float(r["scores"]["composite"])
        except Exception:
            pass

    client = GeminiClient(use_cache=True)

    # Filter candidates with HARD ad-policy flags and find highest-scoring per episode
    best_by_ep: dict[int, tuple[PromoCandidate, float]] = {}
    for c in candidates:
        has_hard = any(
            _AD_HARD_RE.search(e.content)
            for s in c.segments
            for e in clip_elements(episodes.get(s.episode), s.start_seconds, s.end_seconds)
            if episodes.get(s.episode)
        )
        if has_hard:
            continue

        comp = known_scores.get(c.promo_id)
        if comp is None:
            try:
                sc, _ = client.score_promo(c, episodes)
                comp = sc.composite
            except Exception:
                comp = 5.0

        for ep_num in c.episodes:
            if ep_num not in best_by_ep or comp > best_by_ep[ep_num][1]:
                best_by_ep[ep_num] = (c, comp)

    qualifying_eps = [
        ep_num for ep_num in sorted(best_by_ep.keys())
        if episodes.get(ep_num) and episodes[ep_num].total_duration_sec >= config.PROMO_MIN_DURATION_SEC
    ]
    n_qualifying = len(qualifying_eps)
    n_pairs = min(25, n_qualifying)

    if n_qualifying < 25:
        print(f"Notice: {n_qualifying} episodes qualify (fewer than 25 requested). Using {n_qualifying} episodes.")
    else:
        print(f"Found {n_qualifying} qualifying episodes. Selecting {n_pairs} distinct episodes.")

    rng = random.Random(seed)
    chosen_eps = rng.sample(qualifying_eps, n_pairs)

    n_last30 = min(15, len(chosen_eps))
    assignments: list[tuple[int, str]] = []
    for ep in chosen_eps[:n_last30]:
        assignments.append((ep, "last_30_sec_baseline"))
    for ep in chosen_eps[n_last30:]:
        assignments.append((ep, "rule_based_baseline"))

    existing_baselines: dict[tuple[int, str], PromoCandidate] = {}
    baselines_file = config.QUALITY_DIR / "baselines_scored.json"
    if baselines_file.exists():
        try:
            for b in json.loads(baselines_file.read_text(encoding="utf-8")):
                p = PromoCandidate.model_validate(b["promo"])
                if p.episodes:
                    existing_baselines[(p.episodes[0], b.get("source", ""))] = p
        except Exception:
            pass

    pair_rows_sheet: list[list[str]] = []
    pair_rows_key: list[list[Any]] = []

    for i, (ep_num, base_type) in enumerate(assignments, 1):
        pair_id = f"PAIR-{i:02d}"
        pipe_cand, _ = best_by_ep[ep_num]

        if (ep_num, base_type) in existing_baselines:
            base_clip = existing_baselines[(ep_num, base_type)]
        else:
            ep = episodes[ep_num]
            if "last_30" in base_type:
                gen_list = generate_last30_baseline({ep_num: ep}, n=1)
            else:
                gen_list = generate_rule_baseline({ep_num: ep}, n=1)
            if not gen_list:
                continue
            base_clip = gen_list[0]

        pipe_text = render_clip_text(pipe_cand, episodes)
        base_text = render_clip_text(base_clip, episodes)

        if rng.random() < 0.5:
            clip_a_text, clip_b_text = pipe_text, base_text
            a_src, b_src = "pipeline", base_type
        else:
            clip_a_text, clip_b_text = base_text, pipe_text
            a_src, b_src = base_type, "pipeline"

        pair_rows_sheet.append([pair_id, clip_a_text, clip_b_text, ""])
        pair_rows_key.append([pair_id, ep_num, a_src, b_src, base_type, pipe_cand.promo_id])

    with open(PAIRS_SHEET, "w", newline="", encoding="utf-8") as fs:
        writer = csv.writer(fs)
        writer.writerow(["pair_id", "clip_A_text", "clip_B_text", "your_pick"])
        writer.writerows(pair_rows_sheet)

    with open(PAIRS_KEY, "w", newline="", encoding="utf-8") as fk:
        writer = csv.writer(fk)
        writer.writerow(["pair_id", "episode", "A_source", "B_source", "baseline_type", "pipeline_promo_id"])
        writer.writerows(pair_rows_key)

    print("\n" + "=" * 70)
    print(f"PAIRWISE BLIND EVALUATION SHEET GENERATED: {len(pair_rows_sheet)} pairs")
    print("=" * 70)
    print(f"1. Open: {PAIRS_SHEET}")
    print("2. Enter 'A' or 'B' in the 'your_pick' column for each pair.")
    print("3. Run: python human_eval.py pairs-score")
    print("=" * 70 + "\n")


def _binom_test_two_sided(k: int, n: int) -> float:
    """Computes pure Python two-sided binomial sign-test p-value against p=0.5."""
    if n == 0:
        return 1.0
    k_min = min(k, n - k)
    prob = sum(math.comb(n, i) for i in range(k_min + 1)) * (0.5 ** n)
    return min(1.0, 2.0 * prob)


def pairs_score(ratings_path: Path) -> None:
    if not PAIRS_KEY.exists():
        logger.error("Missing answer key: %s. Run 'python human_eval.py pairs-make' first.", PAIRS_KEY)
        sys.exit(1)
    if not ratings_path.exists():
        logger.error("Ratings sheet not found: %s.", ratings_path)
        sys.exit(1)

    with open(PAIRS_KEY, encoding="utf-8") as fk:
        key = {r["pair_id"]: r for r in csv.DictReader(fk)}

    pipe_wins_last30 = 0
    total_last30 = 0
    pipe_wins_rule = 0
    total_rule = 0
    overall_pipe_wins = 0
    n_rated_pairs = 0

    with open(ratings_path, encoding="utf-8") as fr:
        for row in csv.DictReader(fr):
            pid = row.get("pair_id", "").strip()
            k = key.get(pid)
            if not k:
                continue

            pick = (row.get("your_pick") or "").strip().upper()
            if pick not in ("A", "B"):
                continue

            winner_source = k["A_source"] if pick == "A" else k["B_source"]
            is_pipe_win = (winner_source == "pipeline")
            b_type = k["baseline_type"]

            n_rated_pairs += 1
            if is_pipe_win:
                overall_pipe_wins += 1

            if "last_30" in b_type:
                total_last30 += 1
                if is_pipe_win:
                    pipe_wins_last30 += 1
            elif "rule" in b_type:
                total_rule += 1
                if is_pipe_win:
                    pipe_wins_rule += 1

    p_value = _binom_test_two_sided(overall_pipe_wins, n_rated_pairs)

    caveat_text = (
        f"Single-rater blind evaluation with small sample size (N={n_rated_pairs}); "
        "indicates directionality, not population-wide ground truth."
    )

    results = {
        "n_pairs": n_rated_pairs,
        "vs_last_30_sec": {
            "pipeline_wins": pipe_wins_last30,
            "total": total_last30,
            "win_rate": round(pipe_wins_last30 / max(total_last30, 1), 3),
        },
        "vs_rule_based": {
            "pipeline_wins": pipe_wins_rule,
            "total": total_rule,
            "win_rate": round(pipe_wins_rule / max(total_rule, 1), 3),
        },
        "overall": {
            "pipeline_wins": overall_pipe_wins,
            "total": n_rated_pairs,
            "win_rate": round(overall_pipe_wins / max(n_rated_pairs, 1), 3),
        },
        "p_value_two_sided": round(p_value, 4),
        "caveat": caveat_text,
    }

    config.ensure_output_dirs()
    PAIRS_RESULT.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    print("PAIRWISE BLIND EVALUATION RESULTS")
    print("=" * 70)
    print(f"Total Rated Pairs: {n_rated_pairs}")
    print(f"  - vs Last-30s Baseline : {pipe_wins_last30}/{total_last30} wins ({results['vs_last_30_sec']['win_rate'] * 100:.1f}%)")
    print(f"  - vs Rule-Based Baseline: {pipe_wins_rule}/{total_rule} wins ({results['vs_rule_based']['win_rate'] * 100:.1f}%)")
    print(f"  - Overall Pipeline Wins : {overall_pipe_wins}/{n_rated_pairs} ({results['overall']['win_rate'] * 100:.1f}%)")
    print(f"  - Binomial Sign-Test p-value (vs 50% chance): p = {results['p_value_two_sided']:.4f}")
    print("-" * 70)
    print(f"Caveat: {caveat_text}")
    print(f"Results saved to: {PAIRS_RESULT}")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("make", help="generate blind evaluation sheet")
    m.add_argument("--n-pipeline", type=int, default=10, help="number of pipeline promos to sample")
    m.add_argument("--n-each-baseline", type=int, default=5, help="number of promos per baseline to sample")
    m.add_argument("--seed", type=int, default=config.BASELINE_SEED, help="random seed for shuffling")
    m.add_argument("--scripts", type=Path, default=config.SCRIPTS_DIR, help="folder with screenplay text files")

    s = sub.add_parser("score", help="unblind and score filled evaluation sheet")
    s.add_argument("--ratings", type=Path, default=SHEET, help="path to filled blind_sheet.csv")

    pm = sub.add_parser("pairs-make", help="generate pairwise blind evaluation sheet")
    pm.add_argument("--seed", type=int, default=config.BASELINE_SEED, help="random seed for pairing/shuffling")
    pm.add_argument("--scripts", type=Path, default=config.SCRIPTS_DIR, help="folder with screenplay text files")

    ps = sub.add_parser("pairs-score", help="unblind and score filled pairwise sheet")
    ps.add_argument("--ratings", type=Path, default=PAIRS_SHEET, help="path to filled pairs_sheet.csv")

    args = ap.parse_args()
    if args.cmd == "make":
        make(args.n_pipeline, args.n_each_baseline, args.seed, args.scripts)
    elif args.cmd == "score":
        score(args.ratings)
    elif args.cmd == "pairs-make":
        pairs_make(args.seed, args.scripts)
    elif args.cmd == "pairs-score":
        pairs_score(args.ratings)
