"""
quality_checker.py — Deterministic validation, snapping and baselines.

Layer 1 (rule-based, this file):
- Directional timestamp validation (starts on element starts, ends on element ends).
- Duration 20-60s with minimum sub-segment length guard (>= 3.0s).
- Hook presence inside 3.0s window.
- Speaker count validation.
- Whole-word regex ad-policy screening.
- Quoted claims anti-hallucination check (FailureType.HALLUCINATED_CLAIM).
- Deduplication and overlap checks.
- Baseline generation (Random, Last-30s, Rule-heuristic).

Layer 2 (LLM critique) lives in llm_client.py. Judgment-style failure modes
such as "resolved cliffhanger" and "context dependency" are LLM-detected.
"""
from __future__ import annotations

import logging
import random
import re
import statistics

from src.clips import EPS, clip_blob, clip_elements, element_intensity, sorted_elements
from src.config import (
    AD_POLICY_FLAGGED_WORDS,
    AD_POLICY_HARD,
    AD_POLICY_SOFT,
    CLIFFHANGER_WINDOW_SEC,
    HOOK_WINDOW_SEC,
    MAX_CHARACTERS_IN_PROMO,
    PROMO_MAX_DURATION_SEC,
    PROMO_MIN_DURATION_SEC,
    PROMO_OPTIMAL_MAX_SEC,
    PROMO_OPTIMAL_MIN_SEC,
)
from src.schemas import (
    ElementType,
    FailureType,
    ParsedEpisode,
    PromoCandidate,
    PromoSegment,
    PromoType,
    QualityFlag,
    QualityReport,
    RankedPromo,
    Severity,
)

logger = logging.getLogger(__name__)

# Minimum duration for any single segment in a multi-segment promo (avoids jarring micro-cuts)
MIN_SEGMENT_DURATION_SEC = 3.0

_AD_HARD_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(re.escape(w) for w in sorted(AD_POLICY_HARD, key=len, reverse=True)) + r")(?![A-Za-z])",
    re.IGNORECASE,
)
_AD_SOFT_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(re.escape(w) for w in sorted(AD_POLICY_SOFT, key=len, reverse=True)) + r")(?![A-Za-z])",
    re.IGNORECASE,
)
_AD_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(re.escape(w) for w in sorted(AD_POLICY_FLAGGED_WORDS, key=len, reverse=True)) + r")(?![A-Za-z])",
    re.IGNORECASE,
)


def _to_cs(seconds: float) -> int:
    """Quantize seconds to integer centiseconds (0.01s) to avoid float rounding traps."""
    return int(round(seconds * 100))


# ── Deterministic scoring component ──────────────────────────────────────
def compute_duration_fitness(duration_sec: float) -> int:
    """Duration fitness (1-10) computed by code, never by the LLM."""
    d = duration_sec
    if PROMO_OPTIMAL_MIN_SEC <= d <= PROMO_OPTIMAL_MAX_SEC:
        return 10
    if PROMO_MIN_DURATION_SEC <= d < PROMO_OPTIMAL_MIN_SEC:
        return round(6 + 4 * (d - PROMO_MIN_DURATION_SEC) / (PROMO_OPTIMAL_MIN_SEC - PROMO_MIN_DURATION_SEC))
    if PROMO_OPTIMAL_MAX_SEC < d <= PROMO_MAX_DURATION_SEC:
        return round(10 - 4 * (d - PROMO_OPTIMAL_MAX_SEC) / (PROMO_MAX_DURATION_SEC - PROMO_OPTIMAL_MAX_SEC))
    return 1


def total_duration(promo: PromoCandidate) -> float:
    return sum(s.duration_sec() for s in promo.segments)


# ── Timestamps ───────────────────────────────────────────────────────────
def get_all_timestamps(episode: ParsedEpisode) -> set[str]:
    timestamps: set[str] = set()
    for scene in episode.scenes:
        timestamps.add(scene.start_time)
        timestamps.add(scene.end_time)
        for elem in scene.elements:
            timestamps.add(elem.start_time)
            timestamps.add(elem.end_time)
    return timestamps


def snap_to_valid_timestamp(episode: ParsedEpisode, target_sec: float) -> tuple[str, float]:
    """Legacy: nearest boundary of ANY kind (scene or element, start or end)."""
    candidates: list[tuple[float, str]] = []
    for sc in episode.scenes:
        candidates += [(sc.start_seconds, sc.start_time), (sc.end_seconds, sc.end_time)]
        for el in sc.elements:
            candidates += [(el.start_seconds, el.start_time), (el.end_seconds, el.end_time)]
    best = min(candidates, key=lambda x: abs(x[0] - target_sec))
    return best[1], best[0]


def snap_start_to_element_start(episode: ParsedEpisode, target_sec: float) -> tuple[str, float]:
    """A clip may only START where an element starts (never mid-line)."""
    best = min(sorted_elements(episode), key=lambda e: abs(e.start_seconds - target_sec))
    return best.start_time, best.start_seconds


def snap_end_to_element_end(episode: ParsedEpisode, target_sec: float) -> tuple[str, float]:
    """A clip may only END where an element ends (never mid-line)."""
    best = min(sorted_elements(episode), key=lambda e: abs(e.end_seconds - target_sec))
    return best.end_time, best.end_seconds


def snap_promo_timestamps(promo: PromoCandidate, episodes: dict[int, ParsedEpisode]) -> PromoCandidate:
    """Snap starts to element starts and ends to element ends.
    Active safety: if inward snapping causes duration to drop below PROMO_MIN_DURATION_SEC,
    expands the final segment outward to the next element end to preserve duration validity.
    """
    for seg in promo.segments:
        ep = episodes.get(seg.episode)
        if ep is None or not sorted_elements(ep):
            continue
        seg.start_time, seg.start_seconds = snap_start_to_element_start(ep, seg.start_seconds)
        seg.end_time, seg.end_seconds = snap_end_to_element_end(ep, seg.end_seconds)

    # Active preservation of duration boundaries
    if promo.segments and total_duration(promo) < PROMO_MIN_DURATION_SEC:
        last_seg = promo.segments[-1]
        ep = episodes.get(last_seg.episode)
        if ep:
            els = sorted_elements(ep)
            needed = PROMO_MIN_DURATION_SEC - total_duration(promo)
            target_end = last_seg.end_seconds + needed
            candidates = [e for e in els if e.end_seconds >= target_end]
            if candidates:
                next_el = min(candidates, key=lambda e: e.end_seconds)
                last_seg.end_time = next_el.end_time
                last_seg.end_seconds = next_el.end_seconds

    promo.total_duration_sec = total_duration(promo)
    return promo


def validate_timestamps(promo: PromoCandidate, episodes: dict[int, ParsedEpisode]) -> list[QualityFlag]:
    """Every segment must start on an element start and end on an element end."""
    flags: list[QualityFlag] = []
    for seg in promo.segments:
        ep = episodes.get(seg.episode)
        if ep is None:
            flags.append(QualityFlag(
                promo_id=promo.promo_id, failure_type=FailureType.TIMESTAMP_IMPRECISION, severity=Severity.HIGH,
                explanation=f"Episode {seg.episode} does not exist in the dataset.",
                suggested_fix="Verify the episode number.",
            ))
            continue
        els = sorted_elements(ep)
        # Centisecond integer quantization prevents float rounding errors
        starts = {_to_cs(e.start_seconds) for e in els}
        ends = {_to_cs(e.end_seconds) for e in els}

        if seg.end_seconds - seg.start_seconds <= 0:
            flags.append(QualityFlag(
                promo_id=promo.promo_id, failure_type=FailureType.TIMESTAMP_IMPRECISION, severity=Severity.HIGH,
                explanation=f"Segment in Ep {seg.episode} has zero or negative length.",
                suggested_fix="Re-select the segment.",
            ))
        if _to_cs(seg.start_seconds) not in starts:
            flags.append(QualityFlag(
                promo_id=promo.promo_id, failure_type=FailureType.TIMESTAMP_IMPRECISION, severity=Severity.MEDIUM,
                explanation=f"Start {seg.start_time} (Ep {seg.episode}) is not the start of any script element.",
                suggested_fix="Snap the start to the nearest element start.",
            ))
        if _to_cs(seg.end_seconds) not in ends:
            flags.append(QualityFlag(
                promo_id=promo.promo_id, failure_type=FailureType.TIMESTAMP_IMPRECISION, severity=Severity.MEDIUM,
                explanation=f"End {seg.end_time} (Ep {seg.episode}) is not the end of any script element (may cut mid-line).",
                suggested_fix="Snap the end to the nearest element end.",
            ))
    return flags


def validate_duration(promo: PromoCandidate) -> list[QualityFlag]:
    flags: list[QualityFlag] = []
    dur = total_duration(promo)

    # 1. Total duration bounds
    if dur < PROMO_MIN_DURATION_SEC:
        flags.append(QualityFlag(
            promo_id=promo.promo_id, failure_type=FailureType.TIMESTAMP_IMPRECISION, severity=Severity.HIGH,
            explanation=f"Total duration {dur:.1f}s is below minimum {PROMO_MIN_DURATION_SEC}s.",
            suggested_fix="Extend the end to the next element boundary or add context.",
        ))
    elif dur > PROMO_MAX_DURATION_SEC:
        flags.append(QualityFlag(
            promo_id=promo.promo_id, failure_type=FailureType.TIMESTAMP_IMPRECISION, severity=Severity.HIGH,
            explanation=f"Total duration {dur:.1f}s exceeds maximum {PROMO_MAX_DURATION_SEC}s.",
            suggested_fix="Trim the setup or tighten the segments.",
        ))

    # 2. Individual sub-segment minimum guard for multi-segment cuts
    if len(promo.segments) > 1:
        for seg in promo.segments:
            seg_dur = seg.duration_sec()
            if seg_dur < MIN_SEGMENT_DURATION_SEC:
                flags.append(QualityFlag(
                    promo_id=promo.promo_id, failure_type=FailureType.TIMESTAMP_IMPRECISION, severity=Severity.HIGH,
                    explanation=f"Segment in Ep {seg.episode} is only {seg_dur:.1f}s (< {MIN_SEGMENT_DURATION_SEC}s minimum). Visual micro-cuts under 3s confuse viewers.",
                    suggested_fix="Lengthen this sub-segment or merge it with adjacent scene context.",
                ))
    return flags


def validate_hook_presence(promo: PromoCandidate, episodes: dict[int, ParsedEpisode]) -> list[QualityFlag]:
    """Dialogue or action must START inside the first HOOK_WINDOW_SEC of the clip."""
    if not promo.segments:
        return []
    first = promo.segments[0]
    ep = episodes.get(first.episode)
    if ep is None:
        return []
    hook_end = first.start_seconds + HOOK_WINDOW_SEC
    hook_elements = [
        e for e in clip_elements(ep, first.start_seconds, first.end_seconds)
        if e.start_seconds <= hook_end + EPS
        and (
            e.element_type == ElementType.DIALOGUE
            or (e.element_type == ElementType.ACTION and element_intensity(e) >= 6)
        )
    ]
    if hook_elements:
        return []
    return [QualityFlag(
        promo_id=promo.promo_id, failure_type=FailureType.WEAK_HOOK, severity=Severity.HIGH,
        explanation=f"No dialogue or action starts within the first {HOOK_WINDOW_SEC}s of the clip (only camera directions).",
        suggested_fix="Start the clip on a dialogue line or an impactful action.",
    )]


def validate_character_count(promo: PromoCandidate, episodes: dict[int, ParsedEpisode]) -> list[QualityFlag]:
    speaking: set[str] = set()
    for seg in promo.segments:
        ep = episodes.get(seg.episode)
        if ep is None:
            continue
        for e in clip_elements(ep, seg.start_seconds, seg.end_seconds):
            if e.element_type == ElementType.DIALOGUE and e.character:
                speaking.add(e.character)
    if len(speaking) > MAX_CHARACTERS_IN_PROMO:
        return [QualityFlag(
            promo_id=promo.promo_id, failure_type=FailureType.PRONOUN_AMBIGUITY, severity=Severity.MEDIUM,
            explanation=f"{len(speaking)} characters speak in this clip ({', '.join(sorted(speaking))}); more than {MAX_CHARACTERS_IN_PROMO} is hard for a cold viewer to follow.",
            suggested_fix="Trim to 2-3 core characters.",
        )]
    return []


def validate_ad_policy(promo: PromoCandidate, episodes: dict[int, ParsedEpisode]) -> list[QualityFlag]:
    """Whole-word keyword screen ('change' no longer matches 'hang'). Flags for review —
    Hard hits -> HIGH severity. Soft hits -> LOW severity."""
    hard_found: set[str] = set()
    soft_found: set[str] = set()
    for seg in promo.segments:
        ep = episodes.get(seg.episode)
        if ep is None:
            continue
        for e in clip_elements(ep, seg.start_seconds, seg.end_seconds):
            hard_found.update(m.group(1).lower() for m in _AD_HARD_RE.finditer(e.content))
            soft_found.update(m.group(1).lower() for m in _AD_SOFT_RE.finditer(e.content))
    flags: list[QualityFlag] = []
    if hard_found:
        flags.append(QualityFlag(
            promo_id=promo.promo_id, failure_type=FailureType.AD_POLICY_RISK, severity=Severity.HIGH,
            explanation=f"Contains HARD ad-policy prohibited words: {', '.join(sorted(hard_found))} (tier: hard).",
            suggested_fix="Must trim or exclude from paid ad placements.",
        ))
    if soft_found:
        flags.append(QualityFlag(
            promo_id=promo.promo_id, failure_type=FailureType.AD_POLICY_RISK, severity=Severity.LOW,
            explanation=f"Contains SOFT ad-policy sensitive words: {', '.join(sorted(soft_found))} (tier: soft).",
            suggested_fix="Editor review: ad-policy (soft); confirm if acceptable for organic/paid placement.",
        ))
    return flags


_QUOTE_RES = [
    re.compile(r"“([^”]{8,})”"),
    re.compile(r'"([^"]{8,})"'),
    re.compile(r"(?<![A-Za-z])'([^']{8,}?)'(?![A-Za-z])"),
]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", s.lower())).strip()


def validate_quoted_claims(promo: PromoCandidate, episodes: dict[int, ParsedEpisode]) -> list[QualityFlag]:
    """Any line the hook/cliffhanger text puts in quotes must literally occur inside the clip."""
    blob = _norm(clip_blob(promo, episodes))
    if not blob:
        return []
    flags: list[QualityFlag] = []
    for label, text in (("hook", promo.hook), ("cliffhanger", promo.cliffhanger)):
        for rx in _QUOTE_RES:
            for m in rx.finditer(text):
                q = _norm(m.group(1).replace("...", " ").replace("…", " "))
                if len(q) >= 8 and q not in blob:
                    flags.append(QualityFlag(
                        promo_id=promo.promo_id, failure_type=FailureType.HALLUCINATED_CLAIM, severity=Severity.MEDIUM,
                        explanation=f"The {label} text quotes \"{m.group(1)[:80]}\" but that line is not inside the clip's time range (claim/clip mismatch).",
                        suggested_fix="Fix the description or move the cut so the quoted line is inside the clip.",
                    ))
    return flags


def promo_overlap_ratio(p1: PromoCandidate, p2: PromoCandidate) -> float:
    """Fraction of p2's duration that is already covered by p1."""
    total2 = total_duration(p2)
    if total2 <= 0:
        return 0.0
    covered = 0.0
    for s2 in p2.segments:
        for s1 in p1.segments:
            if s1.episode == s2.episode:
                covered += max(0.0, min(s1.end_seconds, s2.end_seconds) - max(s1.start_seconds, s2.start_seconds))
    return min(1.0, covered / total2)


def validate_redundancy(promos: list[PromoCandidate]) -> list[QualityFlag]:
    flags: list[QualityFlag] = []
    for i, p1 in enumerate(promos):
        for p2 in promos[i + 1:]:
            if promo_overlap_ratio(p1, p2) > 0.5:
                flags.append(QualityFlag(
                    promo_id=p2.promo_id, failure_type=FailureType.REDUNDANCY, severity=Severity.LOW,
                    explanation=f"Overlaps >50% with {p1.promo_id}; posting both would feel repetitive.",
                    suggested_fix=f"Keep {p1.promo_id} and drop this one.",
                ))
    return flags


def run_all_validations(promos: list[PromoCandidate], episodes: dict[int, ParsedEpisode]) -> list[QualityFlag]:
    flags: list[QualityFlag] = []
    for promo in promos:
        flags += validate_timestamps(promo, episodes)
        flags += validate_duration(promo)
        flags += validate_hook_presence(promo, episodes)
        flags += validate_character_count(promo, episodes)
        flags += validate_ad_policy(promo, episodes)
        flags += validate_quoted_claims(promo, episodes)
    flags += validate_redundancy(promos)
    logger.info("Rule-based checks: %d flags across %d promos", len(flags), len(promos))
    return flags


def auto_trim_promo(
    promo: PromoCandidate,
    episodes: dict[int, ParsedEpisode],
    flags: list[QualityFlag],
) -> tuple[PromoCandidate, list[str]]:
    """Auto-trim boundaries to cure flagged hooks (delayed dialogue) or resolved cliffhangers.

    Returns (possibly trimmed promo, list of fixes applied).
    """
    fixes: list[str] = []
    trimmed = promo.model_copy(deep=True)
    if not trimmed.segments:
        return trimmed, fixes

    flag_types = {f.failure_type for f in flags if f.severity in (Severity.HIGH, Severity.MEDIUM)}

    # 1. Auto-trim WEAK_HOOK: start of first segment trimmed to first impactful dialogue/action
    if FailureType.WEAK_HOOK in flag_types:
        first_seg = trimmed.segments[0]
        ep = episodes.get(first_seg.episode)
        if ep:
            elems = clip_elements(ep, first_seg.start_seconds, first_seg.end_seconds)
            impact_elems = [e for e in elems if e.element_type in (ElementType.DIALOGUE, ElementType.ACTION)]
            if impact_elems:
                first_impact = impact_elems[0]
                new_start = first_impact.start_seconds
                if new_start > first_seg.start_seconds:
                    diff = new_start - first_seg.start_seconds
                    if total_duration(trimmed) - diff >= PROMO_MIN_DURATION_SEC:
                        first_seg.start_seconds = round(new_start, 2)
                        first_seg.start_time = first_impact.start_time
                        fixes.append(f"Auto-trimmed {diff:.1f}s setup to ensure immediate hook at {first_impact.start_time}")

    # 2. Auto-trim RESOLVED_CLIFFHANGER: end of last segment trimmed before post-peak resolution
    if FailureType.RESOLVED_CLIFFHANGER in flag_types:
        last_seg = trimmed.segments[-1]
        ep = episodes.get(last_seg.episode)
        if ep:
            elems = clip_elements(ep, last_seg.start_seconds, last_seg.end_seconds)
            dialogues = [e for e in elems if e.element_type == ElementType.DIALOGUE]
            if len(dialogues) >= 2:
                peak_dialogue = dialogues[-2]
                new_end = peak_dialogue.end_seconds
                diff = last_seg.end_seconds - new_end
                if diff > 0 and total_duration(trimmed) - diff >= PROMO_MIN_DURATION_SEC:
                    last_seg.end_seconds = round(new_end, 2)
                    last_seg.end_time = peak_dialogue.end_time
                    fixes.append(f"Auto-trimmed {diff:.1f}s resolution fallout to preserve suspense at {peak_dialogue.end_time}")

    return trimmed, fixes


def auto_trim_ad_policy_hard(
    promo: PromoCandidate, episodes: dict[int, ParsedEpisode]
) -> tuple[PromoCandidate, list[str]]:
    """Try trimming hard ad-policy elements if within the first or last ~3 seconds of a segment.
    Keep the trim only if total duration stays within 20-60s and no segment is under 3s.
    Returns (possibly trimmed promo, list of fixes applied).
    """
    fixes: list[str] = []
    trimmed = promo.model_copy(deep=True)
    if not trimmed.segments:
        return trimmed, fixes

    for seg in trimmed.segments:
        ep = episodes.get(seg.episode)
        if not ep:
            continue
        elems = clip_elements(ep, seg.start_seconds, seg.end_seconds)
        hard_elems = [e for e in elems if _AD_HARD_RE.search(e.content)]
        if not hard_elems:
            continue

        first_h = hard_elems[0]
        if first_h.start_seconds - seg.start_seconds <= 3.0 + 1e-4:
            new_s = first_h.end_seconds
            rem = [e for e in elems if e.start_seconds >= new_s - 1e-4]
            if rem:
                nxt = rem[0]
                diff = nxt.start_seconds - seg.start_seconds
                seg.start_seconds = round(nxt.start_seconds, 2)
                seg.start_time = nxt.start_time
                fixes.append(f"Auto-trimmed {diff:.1f}s from start of Ep {seg.episode} to remove hard ad-policy word '{first_h.content[:30]}'")

        elems_after = clip_elements(ep, seg.start_seconds, seg.end_seconds)
        rem_h = [e for e in elems_after if _AD_HARD_RE.search(e.content)]
        if rem_h:
            last_h = rem_h[-1]
            if seg.end_seconds - last_h.end_seconds <= 3.0 + 1e-4:
                new_e = last_h.start_seconds
                prv = [e for e in elems_after if e.end_seconds <= new_e + 1e-4]
                if prv:
                    p_el = prv[-1]
                    diff = seg.end_seconds - p_el.end_seconds
                    seg.end_seconds = round(p_el.end_seconds, 2)
                    seg.end_time = p_el.end_time
                    fixes.append(f"Auto-trimmed {diff:.1f}s from end of Ep {seg.episode} to remove hard ad-policy word '{last_h.content[:30]}'")

    final_dur = total_duration(trimmed)
    has_short = any(s.duration_sec() < MIN_SEGMENT_DURATION_SEC for s in trimmed.segments)
    still_hard = any(
        _AD_HARD_RE.search(e.content)
        for s in trimmed.segments
        for e in clip_elements(episodes.get(s.episode), s.start_seconds, s.end_seconds)
        if episodes.get(s.episode)
    )

    if (
        fixes
        and PROMO_MIN_DURATION_SEC <= final_dur <= PROMO_MAX_DURATION_SEC
        and not has_short
        and not still_hard
    ):
        trimmed.total_duration_sec = final_dur
        return trimmed, fixes
    return promo, []


# ── Baselines (all snapped to element boundaries so they are valid clips) ─
def _baseline(promo_id: str, ep: ParsedEpisode, start: float, end: float, tag: str) -> PromoCandidate | None:
    els = sorted_elements(ep)
    if not els or end - start <= 0:
        return None
    s_el = min(els, key=lambda e: abs(e.start_seconds - start))
    e_el = min(els, key=lambda e: abs(e.end_seconds - end))
    if e_el.end_seconds <= s_el.start_seconds:
        return None
    return PromoCandidate(
        promo_id=promo_id,
        promo_type=PromoType.SINGLE_EPISODE,
        episodes=[ep.episode_number],
        segments=[PromoSegment(
            episode=ep.episode_number, start_time=s_el.start_time, end_time=e_el.end_time,
            start_seconds=s_el.start_seconds, end_seconds=e_el.end_seconds,
        )],
        hook=f"({tag} baseline — no hook selection)",
        cliffhanger=f"({tag} baseline — no cliffhanger selection)",
        why_it_works=f"({tag} baseline for comparison only)",
    )


def generate_random_baseline(episodes: dict[int, ParsedEpisode], n: int = 15, seed: int = 7) -> list[PromoCandidate]:
    """One truly random 20-60s clip per episode (seeded, so the run is reproducible)."""
    out: list[PromoCandidate] = []
    for ep in sorted(episodes.values(), key=lambda e: e.episode_number)[:n]:
        if ep.total_duration_sec < PROMO_MIN_DURATION_SEC:
            continue
        rng = random.Random(seed * 1000 + ep.episode_number)
        max_dur = min(PROMO_MAX_DURATION_SEC, ep.total_duration_sec)
        target_dur = rng.uniform(PROMO_MIN_DURATION_SEC, max_dur)
        start = rng.uniform(0, max(0.0, ep.total_duration_sec - target_dur))
        p = _baseline(f"BASELINE-RAND-{ep.episode_number:03d}", ep, start, start + target_dur, "Random")
        if p:
            out.append(p)
    return out


def generate_last30_baseline(episodes: dict[int, ParsedEpisode], n: int = 15) -> list[PromoCandidate]:
    """The final ~30s of every episode ending at the actual episode end."""
    out: list[PromoCandidate] = []
    for ep in sorted(episodes.values(), key=lambda e: e.episode_number)[:n]:
        if ep.total_duration_sec < PROMO_MIN_DURATION_SEC:
            continue
        end = max((e.end_seconds for e in sorted_elements(ep)), default=ep.total_duration_sec)
        p = _baseline(f"BASELINE-LAST30-{ep.episode_number:03d}", ep, max(0.0, end - 30.0), end, "Last-30s")
        if p:
            out.append(p)
    return out


def generate_rule_baseline(episodes: dict[int, ParsedEpisode], n: int = 15) -> list[PromoCandidate]:
    """Pure keyword/emotion heuristic: 20-60s window with highest cumulative element_intensity."""
    out: list[PromoCandidate] = []
    for ep in sorted(episodes.values(), key=lambda e: e.episode_number)[:n]:
        els = sorted_elements(ep)
        if len(els) < 2:
            continue
        best: tuple[float, float, float] | None = None  # (intensity_sum, start, end)
        n = len(els)
        for i, s_el in enumerate(els):
            s_time = s_el.start_seconds
            current_sum = 0
            for j in range(i, n):
                e_el = els[j]
                dur = e_el.end_seconds - s_time
                current_sum += element_intensity(e_el)
                if dur < PROMO_MIN_DURATION_SEC:
                    continue
                if dur > PROMO_MAX_DURATION_SEC:
                    break
                if best is None or current_sum > best[0]:
                    best = (current_sum, s_time, e_el.end_seconds)

        if best:
            p = _baseline(f"BASELINE-RULE-{ep.episode_number:03d}", ep, best[1], best[2], "Rule-based")
            if p:
                out.append(p)
    return out


# ── Report ───────────────────────────────────────────────────────────────
def build_quality_report(
    all_candidates: list[PromoCandidate],
    ranked_promos: list[RankedPromo],
    flags: list[QualityFlag],
    episodes: dict[int, ParsedEpisode],
    baseline_comparison: dict[str, float] | None = None,
) -> QualityReport:
    flagged_ids = {f.promo_id for f in flags if f.severity == Severity.HIGH}
    passed = [p for p in all_candidates if p.promo_id not in flagged_ids]
    pass_rate = len(passed) / max(len(all_candidates), 1) * 100

    scores = [rp.scores.composite for rp in ranked_promos]
    mean_score = statistics.mean(scores) if scores else 0.0
    std_dev = statistics.stdev(scores) if len(scores) > 1 else 0.0

    covered: set[int] = set()
    for rp in ranked_promos:
        covered.update(rp.promo.episodes)

    breakdown: dict[str, int] = {}
    for f in flags:
        breakdown[f.failure_type.value] = breakdown.get(f.failure_type.value, 0) + 1

    limitations = [
        "Context dependency: later episodes reference earlier plot. The scorer is told to judge as a cold viewer, "
        "but an LLM can still over-credit context; a human skim of the shortlist is required.",
        "Text != video: we work from screenplays, not footage. A 'silent stare' that is powerful on screen reads as nothing in text.",
        "Cliffhanger over-inclusion: every episode ends on a designed cliffhanger, so the pipeline may over-weight episode endings.",
        "Emotion labels come from the script's own annotations; a mislabelled line is a missed beat.",
        "Timestamp precision: script timestamps mark line starts/ends, not video frames; an editor will fine-tune cuts by about 1-2s.",
        "Scores are LLM-judged and the weights are product priors, not fitted to real ad performance. "
        "Validate with the blind human-rating sheet (human_eval.py) and, ultimately, with live A/B tests.",
    ]
    return QualityReport(
        total_candidates_generated=len(all_candidates),
        total_passed_validation=len(passed),
        validation_pass_rate=round(pass_rate, 1),
        total_flagged=len(flags),
        flags=flags,
        failure_breakdown=breakdown,
        mean_composite_score=round(mean_score, 2),
        score_std_dev=round(std_dev, 2),
        episode_coverage=len(covered),
        baseline_comparison=baseline_comparison or {},
        honest_limitations=limitations,
    )
