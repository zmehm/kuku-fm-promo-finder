"""
pipeline.py — The full promo-finding pipeline as ONE reusable function.

Used by run_batch.py (all episodes -> submission outputs) and by the Streamlit
"Live Script Runner" (one uploaded script -> promos). Same code path for both.

Passes:
  0  parse (done by the caller; deterministic)
  1  moments        (LLM, heuristic fallback per episode)
  2  construction   (LLM single-episode + cross-episode; IDs -> exact timestamps)
  3  blind scoring  (LLM judge with heuristic fallback on quota error, composite in code)
  4  critique       (LLM adversarial) + rule-based validators
  5  selection      (diversity on primary episode, overlap, hard-fail filter) -> ranked shortlist
"""
from __future__ import annotations

import csv
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src import config
from src.llm_client import PROMPT_VERSION, GeminiClient, HeuristicClient
from src.quality_checker import (
    auto_trim_ad_policy_hard,
    auto_trim_promo,
    build_quality_report,
    generate_last30_baseline,
    generate_random_baseline,
    generate_rule_baseline,
    promo_overlap_ratio,
    run_all_validations,
    total_duration,
)
from src.schemas import (
    EpisodeMoments,
    FailureType,
    ParsedEpisode,
    PromoCandidate,
    PromoScores,
    QualityFlag,
    QualityReport,
    RankedPromo,
    Severity,
)

logger = logging.getLogger(__name__)
ProgressFn = Callable[[str, int, int], None]

# A critique flag of these types at HIGH severity removes a promo from the shortlist.
HARD_FAIL_TYPES = {
    FailureType.WEAK_HOOK,
    FailureType.RESOLVED_CLIFFHANGER,
    FailureType.TIMESTAMP_IMPRECISION,
    FailureType.HALLUCINATED_CLAIM,
    FailureType.AD_POLICY_RISK,
}


@dataclass
class PipelineResult:
    episodes: dict[int, ParsedEpisode]
    moments: list[EpisodeMoments]
    candidates: list[PromoCandidate]
    ranked: list[RankedPromo]
    flags: list[QualityFlag]
    report: QualityReport
    manifest: dict[str, Any]
    baselines: list[dict[str, Any]] = field(default_factory=list)


def _pmap(fn: Callable, items: list, workers: int, stage: str, progress: ProgressFn | None) -> list[tuple[bool, Any]]:
    """Run fn over items (threaded). Returns [(ok, result_or_exception)] in input order."""
    results: list[tuple[bool, Any]] = [(False, None)] * len(items)
    if not items:
        return results
    if workers <= 1:
        for i, it in enumerate(items):
            try:
                results[i] = (True, fn(it))
            except Exception as exc:  # noqa: BLE001
                results[i] = (False, exc)
            if progress:
                progress(stage, i + 1, len(items))
        return results
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, it): i for i, it in enumerate(items)}
        for n, fut in enumerate(as_completed(futs), 1):
            i = futs[fut]
            try:
                results[i] = (True, fut.result())
            except Exception as exc:  # noqa: BLE001
                results[i] = (False, exc)
            if progress:
                progress(stage, n, len(items))
    return results


def _score_one(client, promo: PromoCandidate, episodes: dict[int, ParsedEpisode]) -> tuple[PromoScores, str]:
    scores, reasoning = client.score_promo(promo, episodes)
    scores.compute_composite(config.SCORING_WEIGHTS)
    return scores, reasoning


def _flag_summary(flags: list[QualityFlag]) -> str:
    notable = [f for f in flags if f.severity in (Severity.HIGH, Severity.MEDIUM)]
    if not notable:
        return ""
    return "REVIEW: " + "; ".join(f"{f.failure_type.value} ({f.severity.value})" for f in notable[:4])


def run_pipeline(
    episodes: dict[int, ParsedEpisode],
    client: GeminiClient | HeuristicClient | None = None,
    target_episodes: list[int] | None = None,
    run_baselines: bool = True,
    critique: bool = True,
    top_n: int = config.TOP_N_FINAL,
    workers: int | None = None,
    progress: ProgressFn | None = None,
) -> PipelineResult:
    t0 = time.time()
    client = client or GeminiClient()
    workers = workers or config.GEMINI_CONCURRENCY
    heuristic = HeuristicClient()
    is_llm = isinstance(client, GeminiClient)
    targets = sorted(n for n in (target_episodes or episodes.keys()) if n in episodes)
    sub = {n: episodes[n] for n in targets}
    manifest: dict[str, Any] = {
        "mode": "llm" if is_llm else "heuristic",
        "model": config.GEMINI_MODEL if is_llm else None,
        "judge_model": config.GEMINI_JUDGE_MODEL if is_llm else None,
        "prompt_version": PROMPT_VERSION if is_llm else None,
        "episodes_processed": targets,
        "n_episodes": len(targets),
        "fallback_to_heuristic": {"moments": [], "construction": [], "scoring": []},
        "scoring_failures": [],
    }

    # ── Pass 1: moments ──────────────────────────────────────────────
    def moments_for(n: int) -> tuple[EpisodeMoments, str]:
        try:
            em = client.identify_moments(sub[n])
            if em.moments:
                return em, manifest["mode"]
            raise RuntimeError("no valid moments returned")
        except Exception as exc:  # noqa: BLE001
            if not is_llm:
                raise
            logger.warning("Ep %d: LLM moments failed (%s) — heuristic fallback", n, exc)
            return heuristic.identify_moments(sub[n]), "heuristic"

    res = _pmap(moments_for, targets, workers, "Pass 1: dramatic moments", progress)
    moments_list: list[EpisodeMoments] = []
    for n, (ok, val) in zip(targets, res):
        if not ok:
            logger.error("Ep %d: moments failed: %s", n, val)
            continue
        em, how = val
        moments_list.append(em)
        if how == "heuristic" and is_llm:
            manifest["fallback_to_heuristic"]["moments"].append(n)
    moments_by_ep = {em.episode_number: em for em in moments_list}

    # ── Pass 2: construction ─────────────────────────────────────────
    def construct_for(n: int) -> tuple[list[PromoCandidate], str]:
        try:
            return client.construct_episode_promos(sub[n], moments_by_ep.get(n), config.PER_EPISODE_CANDIDATES), manifest["mode"]
        except Exception as exc:  # noqa: BLE001
            if not is_llm:
                raise
            logger.warning("Ep %d: LLM construction failed (%s) — heuristic fallback", n, exc)
            return heuristic.construct_episode_promos(sub[n], moments_by_ep.get(n), config.PER_EPISODE_CANDIDATES), "heuristic"

    candidates: list[PromoCandidate] = []
    res = _pmap(construct_for, targets, workers, "Pass 2: building promos", progress)
    for n, (ok, val) in zip(targets, res):
        if not ok:
            logger.error("Ep %d: construction failed: %s", n, val)
            continue
        promos, how = val
        candidates.extend(promos)
        if how == "heuristic" and is_llm:
            manifest["fallback_to_heuristic"]["construction"].append(n)

    if len(targets) >= 2 and moments_list:
        try:
            candidates.extend(client.construct_cross_episode_promos(moments_list, sub, config.CROSS_EPISODE_CANDIDATES))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cross-episode construction failed (%s) — heuristic fallback", exc)
            manifest["cross_episode_error"] = str(exc)[:200]
            try:
                candidates.extend(heuristic.construct_cross_episode_promos(moments_list, sub, config.CROSS_EPISODE_CANDIDATES))
                manifest["fallback_to_heuristic"]["construction"].append("cross_episode")
            except Exception as h_exc:
                logger.error("Heuristic cross-episode failed: %s", h_exc)

    # drop exact duplicates, then assign stable IDs
    seen_sig: set[tuple] = set()
    unique: list[PromoCandidate] = []
    for c in candidates:
        sig = tuple((s.episode, round(s.start_seconds, 2), round(s.end_seconds, 2)) for s in c.segments)
        if sig not in seen_sig:
            seen_sig.add(sig)
            unique.append(c)
    candidates = [c.model_copy(update={"promo_id": f"PR-{i:03d}"}) for i, c in enumerate(unique, 1)]
    manifest["candidates_generated"] = len(candidates)
    if not candidates:
        raise RuntimeError("No promo candidates were generated — check the API key/quota and the script format.")

    # ── Pass 3: blind scoring with resilient fallback ────────────────
    res = _pmap(lambda p: _score_one(client, p, sub), candidates, workers, "Pass 3: scoring", progress)
    scored: list[tuple[PromoCandidate, PromoScores, str]] = []
    for p, (ok, val) in zip(candidates, res):
        if ok:
            scored.append((p, val[0], val[1]))
        else:
            # Resilient fallback: score using HeuristicClient rather than discarding candidate
            if is_llm:
                try:
                    h_score, h_reason = _score_one(heuristic, p, sub)
                    scored.append((p, h_score, f"[Heuristic Fallback] {h_reason}"))
                    manifest["fallback_to_heuristic"]["scoring"].append(p.promo_id)
                    continue
                except Exception as h_exc:
                    logger.warning("Heuristic scoring also failed for %s: %s", p.promo_id, h_exc)
            manifest["scoring_failures"].append(p.promo_id)
            logger.warning("%s: scoring failed (%s) — excluded from ranking", p.promo_id, val)

    scored.sort(key=lambda t: (-t[1].composite, -t[1].hook_strength, -t[1].cliffhanger_quality, t[0].promo_id))

    # ── Pass 4: critique (top-K) + rule-based validation (all) ───────
    flags = run_all_validations([p for p, _, _ in scored], sub)
    if critique and is_llm:
        top = [p for p, _, _ in scored[: config.CRITIQUE_TOP_K]]
        res = _pmap(lambda p: client.critique_promo(p, sub), top, workers, "Pass 4: adversarial critique", progress)
        manifest["critique_failures"] = []
        for p, (ok, val) in zip(top, res):
            if ok:
                flags.extend(val)
            else:
                manifest["critique_failures"].append(p.promo_id)
    flags_by_id: dict[str, list[QualityFlag]] = {}
    for f in flags:
        flags_by_id.setdefault(f.promo_id, []).append(f)

    # ── Pass 5: selection (diversity anchored to primary episode) ────
    selected: list[tuple[PromoCandidate, PromoScores, str]] = []
    per_ep: dict[int, int] = {}
    dropped = {"hard_fail": [], "duration": [], "overlap": [], "episode_quota": [], "ad_policy_hard": []}
    ad_policy_counts = {"hard_trimmed": 0, "hard_excluded": 0, "soft_kept_flagged": 0}

    # Pre-selection ad-policy handling for all scored candidates
    prepared_scored: list[tuple[PromoCandidate, PromoScores, str]] = []
    for p, sc, r in scored:
        fl = flags_by_id.get(p.promo_id, [])
        has_hard = any(f.failure_type == FailureType.AD_POLICY_RISK and f.severity == Severity.HIGH for f in fl)
        if has_hard:
            trimmed_p, fixes = auto_trim_ad_policy_hard(p, sub)
            if fixes:
                p = trimmed_p
                new_fl = run_all_validations([p], sub)
                fl = [f for f in fl if f.failure_type != FailureType.AD_POLICY_RISK] + new_fl
                flags_by_id[p.promo_id] = fl
                ad_policy_counts["hard_trimmed"] += 1
                manifest.setdefault("auto_trimmed", []).append({"promo_id": p.promo_id, "fixes": fixes})
                prepared_scored.append((p, sc, r))
            else:
                ad_policy_counts["hard_excluded"] += 1
                dropped["ad_policy_hard"].append(p.promo_id)
                # Excluded from shortlist consideration; candidate remains in promo_candidates.json
                continue
        else:
            prepared_scored.append((p, sc, r))

        if any(f.failure_type == FailureType.AD_POLICY_RISK and f.severity == Severity.LOW for f in fl):
            ad_policy_counts["soft_kept_flagged"] += 1

    for p, sc, r in prepared_scored:
        fl = flags_by_id.get(p.promo_id, [])
        primary_ep = p.episodes[0]

        # Attempt auto-trim for flagged hooks or cliffhangers before hard failure
        if any(f.severity == Severity.HIGH and f.failure_type in (FailureType.WEAK_HOOK, FailureType.RESOLVED_CLIFFHANGER) for f in fl):
            trimmed_p, fixes = auto_trim_promo(p, sub, fl)
            if fixes:
                p = trimmed_p
                # Re-validate trimmed promo
                new_fl = run_all_validations([p], sub)
                fl = [f for f in fl if f.failure_type not in (FailureType.WEAK_HOOK, FailureType.RESOLVED_CLIFFHANGER)] + new_fl
                flags_by_id[p.promo_id] = fl
                manifest.setdefault("auto_trimmed", []).append({"promo_id": p.promo_id, "fixes": fixes})

        if not (config.PROMO_MIN_DURATION_SEC <= total_duration(p) <= config.PROMO_MAX_DURATION_SEC):
            dropped["duration"].append(p.promo_id)
        elif any(f.severity == Severity.HIGH and f.failure_type in HARD_FAIL_TYPES for f in fl):
            dropped["hard_fail"].append(p.promo_id)
        elif any(promo_overlap_ratio(q, p) > config.OVERLAP_DROP_RATIO for q, _, _ in selected):
            dropped["overlap"].append(p.promo_id)
        elif per_ep.get(primary_ep, 0) >= config.MAX_PROMOS_PER_EPISODE:
            dropped["episode_quota"].append(p.promo_id)
        else:
            selected.append((p, sc, r))
            per_ep[primary_ep] = per_ep.get(primary_ep, 0) + 1
        if len(selected) >= top_n:
            break
    manifest["dropped_in_selection"] = dropped
    manifest["ad_policy"] = ad_policy_counts

    ranked: list[RankedPromo] = []
    for i, (p, sc, r) in enumerate(selected, 1):
        p_flags = flags_by_id.get(p.promo_id, [])
        note = _flag_summary(p_flags)
        has_soft_ad = any(f.failure_type == FailureType.AD_POLICY_RISK and f.severity == Severity.LOW for f in p_flags)
        role = "Primary creative" if i <= 3 else "Rotation creative (swap in when primaries fatigue)"
        ad_note = " editor review: ad-policy (soft)." if has_soft_ad else ""
        rationale = f"{role} — angle: {p.creative_angle}. Editor to confirm cut points (±1-2s).{ad_note}" + (f" {note}" if note else "")
        ranked.append(RankedPromo(
            rank=i, promo=p, scores=sc, score_reasoning=r,
            posting_rationale=rationale.strip(),
        ))

    # ── Baselines, scored by the SAME blind scorer ───────────────────
    baselines_out: list[dict[str, Any]] = []
    comparison: dict[str, float] = {}
    if run_baselines:
        n_base = min(15, len(sub))
        sources = {
            "random_baseline": generate_random_baseline(sub, n=n_base, seed=config.BASELINE_SEED),
            "last_30_sec_baseline": generate_last30_baseline(sub, n=n_base),
            "rule_based_baseline": generate_rule_baseline(sub, n=n_base),
        }
        for name, clips in sources.items():
            res = _pmap(lambda p: _score_one(client, p, sub), clips, workers, f"Baseline: {name}", progress)
            vals = []
            for p, (ok, val) in zip(clips, res):
                if ok:
                    vals.append(val[0].composite)
                    baselines_out.append({"source": name, "promo": p.model_dump(), "scores": val[0].model_dump(), "composite": val[0].composite})
            if vals:
                comparison[name] = round(sum(vals) / len(vals), 2)
        single = [(p, sc) for p, sc, _ in scored if len(set(p.episodes)) == 1]
        best_per_ep: dict[int, float] = {}
        for p, sc in single:
            best_per_ep[p.episodes[0]] = max(best_per_ep.get(p.episodes[0], 0.0), sc.composite)
        if best_per_ep:
            comparison["pipeline_best_per_episode"] = round(sum(best_per_ep.values()) / len(best_per_ep), 2)
        comparison["pipeline_all_candidates"] = round(sum(sc.composite for _, sc, _ in scored) / len(scored), 2)
        if ranked:
            comparison["pipeline_final_shortlist"] = round(sum(r.scores.composite for r in ranked) / len(ranked), 2)

    report = build_quality_report([p for p, _, _ in scored], ranked, flags, sub, baseline_comparison=comparison)
    if is_llm:
        report.honest_limitations.append(
            "The baseline comparison uses the same LLM family as the generator (blind scoring, temperature 0); "
            "it shows relative ordering under that judge, not ground truth. See the blind human-rating check."
        )
    else:
        report.honest_limitations.append("HEURISTIC MODE: no LLM was used; scores are rule-based approximations.")
    fallbacks = manifest["fallback_to_heuristic"]["moments"] + manifest["fallback_to_heuristic"]["construction"]
    if fallbacks:
        report.honest_limitations.append(
            f"Heuristic fallback was used for episodes {sorted(set(str(x) for x in fallbacks))} after API failures."
        )

    manifest.update({
        "candidates_scored": len(scored),
        "shortlist_size": len(ranked),
        "client_stats": dict(getattr(client, "stats", {})),
        "cost": client.cost_tracker.model_dump(),
        "cache_hits": getattr(client, "cache_hits", 0),
        "wall_seconds": round(time.time() - t0, 1),
        "ad_policy": ad_policy_counts,
    })
    return PipelineResult(sub, moments_list, [p for p, _, _ in scored], ranked, flags, report, manifest, baselines_out)


# ── Saving ────────────────────────────────────────────────────────────────
def _dump(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def save_result(result: PipelineResult) -> None:
    """Write the submission artifacts (file names kept compatible with existing app.py)."""
    config.ensure_output_dirs()
    for em in result.moments:
        _dump(config.MOMENTS_DIR / f"moments_ep{em.episode_number:03d}.json", em.model_dump())
    _dump(config.PROMOS_DIR / "promo_candidates.json", [c.model_dump() for c in result.candidates])
    _dump(config.RANKED_DIR / "ranked_promos.json", [r.model_dump() for r in result.ranked])
    _dump(config.QUALITY_DIR / "quality_report.json", result.report.model_dump())
    _dump(config.OUTPUT_DIR / "run_manifest.json", result.manifest)
    _dump(config.QUALITY_DIR / "baselines_scored.json", result.baselines)
    with open(config.RANKED_DIR / "ranked_promos.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "promo_id", "composite", "hook", "cliffhanger", "coherence", "emotion", "duration_fit",
                    "episodes", "segments", "duration_s", "angle", "hook_text", "cliffhanger_text", "posting_rationale"])
        for r in result.ranked:
            p, s = r.promo, r.scores
            segs = " + ".join(f"Ep{g.episode} {g.start_time}-{g.end_time}" for g in p.segments)
            w.writerow([r.rank, p.promo_id, s.composite, s.hook_strength, s.cliffhanger_quality, s.standalone_coherence,
                        s.emotional_impact, s.duration_fitness, "/".join(map(str, p.episodes)), segs,
                        round(total_duration(p), 1), p.creative_angle, p.hook, p.cliffhanger, r.posting_rationale])
