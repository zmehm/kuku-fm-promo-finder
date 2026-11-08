"""
llm_client.py — Gemini client (retry, disk cache, cost tracking) + offline HeuristicClient.

Design rules:
  * The LLM NEVER writes timestamps. It selects script-line IDs ("12.034"); code
    turns IDs into exact verbatim timestamps. Moments for cross-episode stitching
    are referenced by moment ID ("M41-03").
  * The LLM does judgement only: moment detection, hook/cliffhanger/coherence/emotion
    scoring, adversarial critique. Weighted math and duration fitness are done in code.
  * Scoring is BLIND: the scorer sees only the clip text, never the generator's
    claims, so pipeline clips and baseline clips are judged identically.
  * Every response is cached on disk (resume after quota errors + audit trail).
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from src import config
from src.clips import (
    clip_elements,
    element_ids,
    element_intensity,
    element_text,
    render_clip_text,
    render_episode_for_llm,
    sorted_elements,
    speakers_in_clip,
)
from src.quality_checker import compute_duration_fitness, total_duration
from src.schemas import (
    CostTracker,
    DramaticMoment,
    ElementType,
    EpisodeMoments,
    FailureType,
    MomentType,
    ParsedEpisode,
    PromoCandidate,
    PromoScores,
    PromoSegment,
    PromoType,
    QualityFlag,
    Severity,
)

try:  # the SDK is optional so the offline HeuristicClient works without it
    from google import genai
    from google.genai import types
except ImportError:  # pragma: no cover
    genai = None
    types = None

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# PROMPTS (exact prompts used; versioned so the manifest can cite them)
# ──────────────────────────────────────────────────────────────────────
PROMPT_VERSION = "v2-id-anchored"

MOMENT_SYSTEM_PROMPT = """\
You are a senior performance-marketing editor at a microdrama streaming app (like ReelShort or DramaBox). You find the moments in a screenplay that make strangers scrolling Instagram Reels / TikTok / YouTube Shorts stop and install the app.

Every script line has an ID in square brackets, e.g. [07.012]. You must refer to lines ONLY by these IDs. NEVER write or invent timestamps.

Find 6-12 dramatic MOMENTS in the episode. A moment is a short beat: 1-8 consecutive lines, at most 25 seconds. For each moment return:
- moment_type: one of hook_visual, hook_dialogue, cliffhanger, confrontation, reveal, betrayal, emotional_peak
- intensity: integer 1-10 (10 = guaranteed scroll-stopper). Be harsh: only a few moments in an episode deserve 9-10.
- start_id, end_id: the first and last line of the beat (end_id at or after start_id). Prefer ending on a DIALOGUE, ACTION or SOUND FX line, not a CAMERA line.
- description: one sentence saying what happens, naming roles (the bride, the duke, the evil sister), not just pronouns.
- key_dialogue: the single most striking line, copied VERBATIM from the script, or "" if none.

Definitions:
- HOOK: something in the first 3 seconds that works with ZERO prior context: a slap, a betrayal reveal, a shocking line, a violent action.
- CLIFFHANGER: the beat ends BEFORE the outcome. The viewer must need to know what happens next. Never include the resolution.
- Use ACTION and SOUND FX lines too: many of the best hooks are visual (a slap, someone kneeling in mud, an explosion, a string crescendo).
Respond with JSON only."""

EPISODE_PROMO_SYSTEM_PROMPT = """\
You are a senior performance-marketing editor assembling 20-60 second promo clips from ONE microdrama episode for Instagram Reels / TikTok / YouTube Shorts.

Every script line has an ID in square brackets, e.g. [07.012]. A promo is one or more SEGMENTS; each segment is (start_id, end_id) and covers every line from start_id to end_id inclusive. NEVER write or invent timestamps — only IDs.

Rules for every promo:
1. HOOK: the first 3 seconds must contain a scroll-stopper (shocking line, physical action, reveal). No slow setup, no exposition.
2. CLIFFHANGER: the final line must leave the tension UNRESOLVED. Cut BEFORE the outcome. Never include the resolution.
3. STANDALONE: a stranger who has never seen the show must understand who is who and what the conflict is within about 10 seconds. Prefer clips where roles are made clear by the dialogue itself.
4. DURATION: total length of all segments between 20 and 60 seconds; aim for 25-45. Use the line timestamps shown to add up lengths.
5. Segments may be one continuous stretch, or 2-3 stitched moments of the same episode if they flow logically.
6. Different promos must use different parts of the episode (no big overlaps) and, where possible, different creative angles.
7. creative_angle must be exactly one of: betrayal_revenge, protective_romance, hidden_identity, survival_thriller.
8. In hook and cliffhanger, describe what actually happens in the first 3 seconds / last 5 seconds of YOUR clip. If you quote dialogue, copy it VERBATIM from a line inside your clip.
Respond with JSON only."""

CROSS_EPISODE_SYSTEM_PROMPT = """\
You are a senior performance-marketing editor building CROSS-EPISODE "flash-forward" promos for a microdrama app.

You are given a catalogue of dramatic moments from many episodes. Each has a moment ID like M41-03 (episode 41, moment 3), a time range, a length in seconds, an intensity, and a description. Build promos by STITCHING 2-4 moments (use the moment IDs ONLY; never write timestamps).

Classic structure: (1) open IN MEDIA RES on a shocking, high-stakes moment from a LATER episode, (2) cut back to an earlier moment that explains the injustice or the stakes, (3) end on an UNRESOLVED cliffhanger moment. The total length of the stitched moments must be 20-60 seconds (aim 30-45); add up the lengths shown.

Rules: a cold viewer must understand who is who; do not end on a resolution; do not reuse the same moment in two promos; creative_angle must be exactly one of betrayal_revenge, protective_romance, hidden_identity, survival_thriller. In hook and cliffhanger, describe what happens at the start and end of the stitched clip; copy any quoted dialogue VERBATIM from the moment descriptions or key dialogue given.
Respond with JSON only."""

SCORING_SYSTEM_PROMPT = """\
You are a strict evaluator of short promotional video clips for a microdrama app, judging ONLY from the clip text you are given. You know nothing about the show; you are a stranger scrolling TikTok who has never heard of it.

The clip is shown as time-stamped lines (seconds from the start of the clip). Score 4 criteria, each an integer 1-10:
1. hook_strength: would a stranger STOP scrolling within the first 3 seconds? 10 = slap / shocking reveal / explosion; 5 = interesting but slow; 1 = talking heads, setup, only camera directions.
2. cliffhanger_quality: does the ending leave tension UNRESOLVED so the viewer must find out what happens next? 10 = cut at the peak, outcome unknown; 5 = tense but partly answered; 1 = the conflict is already resolved or the clip just fizzles out.
3. standalone_coherence: can a stranger understand who the people are and what the conflict is, with no outside knowledge? 10 = roles and stakes are crystal clear; 1 = needs many episodes of context, or unexplained pronouns ("he told them") everywhere.
4. emotional_impact: does it trigger a strong reaction (outrage, sympathy, shock)? 10 = you physically react; 1 = nothing.

Write your reasoning FIRST (3-5 sentences, citing specific lines), THEN the four scores. Be harsh and calibrated: most clips should land between 4 and 7; give 9-10 only to clips that are genuinely exceptional. Do not reward length or pretty descriptions; reward what a viewer would see and hear.
Respond with JSON only."""

CRITIQUE_SYSTEM_PROMPT = """\
You are a ruthless social-media content critic and ad-policy reviewer. Your job is to find FLAWS in a promo clip. You see the clip text (time-stamped lines) AND the editor's CLAIMS about its hook and cliffhanger. Check the claims against the clip.

Failure types you may report (use these exact strings):
- context_dependency: a cold viewer who has never seen the show would be confused (unintroduced characters, plot that needs earlier episodes).
- weak_hook: the first 3 seconds are not actually compelling, or the claimed hook is not in the first 3 seconds.
- resolved_cliffhanger: the clip includes the outcome/resolution, or the claimed unresolved moment is already answered inside the clip.
- pronoun_ambiguity: "he/she/they" are used without the clip establishing who they are.
- ad_policy_risk: content Meta/TikTok ad review would flag (suicide, hanging, self-harm, graphic violence, torture, threats of death).
- hallucinated_claim: claimed dialogue or actions in the hook/cliffhanger pitch do not actually occur in the clip.

Severity: high (would likely sink the ad), medium (should be fixed by an editor), low (minor).
Only report REAL problems you can point to in the clip; if the clip is good, return an empty flags list. Keep each explanation to 1-2 sentences and cite the line.
Respond with JSON only."""


# ──────────────────────────────────────────────────────────────────────
# LLM output schemas (deliberately simple types so Gemini accepts them)
# ──────────────────────────────────────────────────────────────────────
class LLMMomentOut(BaseModel):
    moment_type: str
    intensity: int
    start_id: str
    end_id: str
    description: str
    key_dialogue: str


class LLMMomentsOut(BaseModel):
    moments: list[LLMMomentOut]


class LLMSegOut(BaseModel):
    start_id: str
    end_id: str


class LLMPromoOut(BaseModel):
    segments: list[LLMSegOut]
    hook: str
    cliffhanger: str
    why_it_works: str
    creative_angle: str


class LLMPromosOut(BaseModel):
    promos: list[LLMPromoOut]


class LLMXSegOut(BaseModel):
    moment_id: str


class LLMXPromoOut(BaseModel):
    segments: list[LLMXSegOut]
    hook: str
    cliffhanger: str
    why_it_works: str
    creative_angle: str


class LLMXPromosOut(BaseModel):
    promos: list[LLMXPromoOut]


class LLMScoreOut(BaseModel):
    reasoning: str
    hook_strength: int
    cliffhanger_quality: int
    standalone_coherence: int
    emotional_impact: int


class LLMFlagOut(BaseModel):
    failure_type: str
    severity: str
    explanation: str
    suggested_fix: str


class LLMCritiqueOut(BaseModel):
    verdict: str
    flags: list[LLMFlagOut]


# ──────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────
def _clamp(v: Any, lo: int = 1, hi: int = 10) -> int:
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return lo


def _pad(text: str, min_len: int, fallback: str) -> str:
    text = (text or "").strip()
    if len(text) >= min_len:
        return text
    return (text + " " + fallback).strip() if text else fallback


def _moment_type(raw: str) -> MomentType:
    raw = (raw or "").strip().lower().replace(" ", "_")
    try:
        return MomentType(raw)
    except ValueError:
        return MomentType.EMOTIONAL_PEAK


def _angle(raw: str, *texts: str) -> str:
    raw = (raw or "").strip().lower()
    if raw in config.CREATIVE_ANGLES:
        return raw
    blob = " ".join(texts).lower()
    best, best_hits = "betrayal_revenge", 0
    for name, meta in config.CREATIVE_ANGLES.items():
        hits = sum(1 for k in meta["keywords"] if k in blob)
        if hits > best_hits:
            best, best_hits = name, hits
    return best


def moment_ref(ep_num: int, index_1based: int) -> str:
    return f"M{ep_num:02d}-{index_1based:02d}"


def _moment_table(moments_list: list[EpisodeMoments]) -> dict[str, DramaticMoment]:
    table: dict[str, DramaticMoment] = {}
    for em in moments_list:
        for i, m in enumerate(em.moments, 1):
            table[moment_ref(em.episode_number, i)] = m
    return table


def _build_promo(
    segments: list[PromoSegment],
    episodes: dict[int, ParsedEpisode],
    hook: str,
    cliffhanger: str,
    why: str,
    angle: str,
) -> PromoCandidate:
    ep_list: list[int] = []
    for s in segments:
        if s.episode not in ep_list:
            ep_list.append(s.episode)
    if len(ep_list) > 1:
        ptype = PromoType.CROSS_EPISODE
    elif len(segments) > 1:
        ptype = PromoType.MULTI_SEGMENT
    else:
        ptype = PromoType.SINGLE_EPISODE
    chars: list[str] = []
    for s in segments:
        ep = episodes.get(s.episode)
        if ep:
            for c in speakers_in_clip(ep, s.start_seconds, s.end_seconds):
                if c not in chars:
                    chars.append(c)
    return PromoCandidate(
        promo_id="TMP",
        promo_type=ptype,
        episodes=ep_list,
        segments=segments,
        hook=_pad(hook, 10, "Opening moment of the clip."),
        cliffhanger=_pad(cliffhanger, 10, "Final moment of the clip."),
        why_it_works=_pad(why, 20, "Strong hook with an unresolved ending for cold viewers."),
        characters_in_clip=chars,
        creative_angle=angle,
    )


# ──────────────────────────────────────────────────────────────────────
# GEMINI CLIENT
# ──────────────────────────────────────────────────────────────────────
class GeminiClient:
    """Resilient Gemini wrapper: retry + backoff with jitter, on-disk response cache, token/cost tracking."""

    name = "gemini"

    def __init__(self, api_key: str | None = None, use_cache: bool = True) -> None:
        key = api_key or config.GEMINI_API_KEY
        self._client = None
        if key and genai is not None:
            self._client = genai.Client(api_key=key)
        elif not key:
            logger.warning("No Gemini API key configured — only cached responses can be served.")
        else:
            logger.warning("google-genai SDK not installed — only cached responses can be served.")
        self.use_cache = use_cache
        self.cost_tracker = CostTracker()
        self.cache_hits = 0
        self.stats: Counter[str] = Counter()
        self._lock = threading.Lock()
        logger.info("GeminiClient ready (model=%s, judge=%s, live=%s)", config.GEMINI_MODEL, config.GEMINI_JUDGE_MODEL, self.available)

    @property
    def available(self) -> bool:
        return self._client is not None

    # ── plumbing ─────────────────────────────────────────────────────
    def _bump(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.stats[key] += n

    def _track(self, in_t: int, out_t: int, cached: bool = False) -> None:
        with self._lock:
            self.cost_tracker.add_usage(in_t, out_t, config.COST_PER_1M_INPUT_TOKENS, config.COST_PER_1M_OUTPUT_TOKENS)
            if cached:
                self.cache_hits += 1

    @staticmethod
    def _usage(response: Any) -> tuple[int, int]:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return 0, 0
        return (getattr(usage, "prompt_token_count", 0) or 0, getattr(usage, "candidates_token_count", 0) or 0)

    @staticmethod
    def _parse_json(text: str) -> Any:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            nl = cleaned.find("\n")
            cleaned = cleaned[nl + 1:] if nl != -1 else cleaned[3:]
            if cleaned.rstrip().endswith("```"):
                cleaned = cleaned.rstrip()[:-3]
            cleaned = cleaned.strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            a, b = cleaned.find("{"), cleaned.rfind("}")
            if a != -1 and b > a:
                try:
                    return json.loads(cleaned[a:b + 1])
                except json.JSONDecodeError:
                    pass
            raise ValueError(f"Failed to parse LLM JSON response: {text[:300]!r}")

    @staticmethod
    def _cache_key(model: str, system: str, prompt: str, schema: type[BaseModel] | None, temperature: float) -> str:
        blob = json.dumps([model, system, prompt, schema.__name__ if schema else "", temperature], ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return config.CACHE_DIR / f"{key}.json"

    def _cache_read(self, key: str) -> dict | None:
        if not self.use_cache:
            return None
        p = self._cache_path(key)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        return None

    def _cache_write(self, key: str, payload: dict) -> None:
        if not self.use_cache:
            return
        try:
            config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path(key).with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._cache_path(key))
        except OSError as exc:  # cache is best-effort
            logger.debug("cache write failed: %s", exc)

    def generate(
        self,
        prompt: str,
        system_instruction: str = "",
        response_schema: type[BaseModel] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> Any:
        """One Gemini call with disk cache, retry and cost tracking. Returns parsed JSON when a schema is given."""
        model = model or getattr(self, "model", config.GEMINI_MODEL)
        temperature = config.GEMINI_TEMPERATURE if temperature is None else temperature
        key = self._cache_key(model, system_instruction, prompt, response_schema, temperature)

        cached = self._cache_read(key)
        if cached is not None:
            self._track(cached.get("in", 0), cached.get("out", 0), cached=True)
            return self._parse_json(cached["text"]) if response_schema is not None else cached["text"]

        if self._client is None:
            raise RuntimeError("Gemini unavailable (no API key / SDK) and no cached response for this prompt.")

        kwargs: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_output_tokens or config.GEMINI_MAX_OUTPUT_TOKENS,
        }
        if system_instruction:
            kwargs["system_instruction"] = system_instruction
        if response_schema is not None:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = response_schema
        gen_config = types.GenerateContentConfig(**kwargs)

        last_exc: Exception | None = None
        for attempt in range(1, config.GEMINI_MAX_RETRIES + 1):
            try:
                response = self._client.models.generate_content(model=model, contents=prompt, config=gen_config)
                in_t, out_t = self._usage(response)
                text = response.text or ""
                if not text.strip():
                    raise RuntimeError("Empty response from Gemini")
                parsed = self._parse_json(text) if response_schema is not None else text
                self._track(in_t, out_t)
                self._cache_write(key, {"text": text, "in": in_t, "out": out_t, "model": model, "prompt_version": PROMPT_VERSION})
                return parsed
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                msg = str(exc)
                fatal = any(t in msg for t in ("API_KEY_INVALID", "PERMISSION_DENIED", "INVALID_ARGUMENT", "NOT_FOUND"))
                logger.warning("Gemini error (attempt %d/%d): %s: %s", attempt, config.GEMINI_MAX_RETRIES, type(exc).__name__, msg[:200])
                if fatal or attempt == config.GEMINI_MAX_RETRIES:
                    break
                with self._lock:
                    self.cost_tracker.total_retries += 1
                rate = "429" in msg or "RESOURCE_EXHAUSTED" in msg.upper() or "quota" in msg.lower()
                busy = "503" in msg or "UNAVAILABLE" in msg.upper()
                base_delay = config.GEMINI_RETRY_DELAY_SEC * (2 ** (attempt - 1))
                # Add jitter to prevent thundering herd on concurrent threads
                jitter = random.uniform(0.75, 1.25)
                delay = (max(base_delay, 15.0 * attempt) if (rate or busy) else base_delay) * jitter
                time.sleep(delay)
        raise RuntimeError(f"Gemini call failed: {last_exc}") from last_exc

    @staticmethod
    def _as(model_cls: type[BaseModel], raw: Any, list_key: str) -> Any:
        if isinstance(raw, list):
            raw = {list_key: raw}
        return model_cls.model_validate(raw)

    # ── Pass 1: moments ──────────────────────────────────────────────
    def identify_moments(self, episode: ParsedEpisode) -> EpisodeMoments:
        prompt = (
            render_episode_for_llm(episode)
            + '\n\nIdentify the dramatic moments. Respond with JSON: {"moments": [{"moment_type": "...", "intensity": 8, '
            '"start_id": "01.014", "end_id": "01.018", "description": "...", "key_dialogue": "..."}]}'
        )
        raw = self.generate(prompt, MOMENT_SYSTEM_PROMPT, LLMMomentsOut)
        out = self._as(LLMMomentsOut, raw, "moments")

        ids = element_ids(episode)
        emap = dict(ids)
        pos = {eid: n for n, (eid, _) in enumerate(ids)}
        moments: list[DramaticMoment] = []
        for m in out.moments:
            sid = m.start_id.strip("[] ")
            eid = m.end_id.strip("[] ")
            a, b = emap.get(sid), emap.get(eid)
            if a is None or b is None or pos.get(sid, 0) > pos.get(eid, 0):
                self._bump("moments_rejected_bad_ids")
                continue
            start, end = a.start_seconds, max(a.end_seconds, b.end_seconds)
            if end - start > config.MAX_MOMENT_SEC or end <= start:
                self._bump("moments_rejected_too_long")
                continue
            els = clip_elements(episode, start, end)
            desc = _pad(m.description, 10, f"Dramatic beat in episode {episode.episode_number}.")
            moments.append(DramaticMoment(
                episode_number=episode.episode_number,
                moment_type=_moment_type(m.moment_type),
                intensity_score=_clamp(m.intensity),
                start_time=a.start_time,
                end_time=(b.end_time if b.end_seconds >= a.end_seconds else a.end_time),
                start_seconds=start,
                end_seconds=end,
                description=desc,
                key_dialogue=m.key_dialogue.strip() or None,
                characters_involved=list(dict.fromkeys(e.character for e in els if e.character)),
                emotion_labels=list(dict.fromkeys(e.emotion for e in els if e.emotion)),
            ))
        moments.sort(key=lambda x: x.start_seconds)
        return EpisodeMoments(
            episode_number=episode.episode_number,
            logline=episode.logline,
            moments=moments,
            total_duration_sec=episode.total_duration_sec,
        )

    # ── Pass 2a: single-episode promos ───────────────────────────────
    def construct_episode_promos(
        self,
        episode: ParsedEpisode,
        moments: EpisodeMoments | None = None,
        n: int = config.PER_EPISODE_CANDIDATES,
    ) -> list[PromoCandidate]:
        hint = ""
        if moments and moments.moments:
            lines = [
                f"- {m.moment_type.value} | intensity {m.intensity_score} | {m.start_time}-{m.end_time} | {m.description}"
                for m in sorted(moments.moments, key=lambda x: -x.intensity_score)[:10]
            ]
            hint = "\n\nCANDIDATE MOMENTS found earlier (use as anchors, not as limits):\n" + "\n".join(lines)
        prompt = (
            render_episode_for_llm(episode) + hint
            + f"\n\nCreate up to {n} promo clips from THIS episode. Respond with JSON: "
            '{"promos": [{"segments": [{"start_id": "01.014", "end_id": "01.030"}], "hook": "...", "cliffhanger": "...", '
            '"why_it_works": "...", "creative_angle": "betrayal_revenge"}]}'
        )
        raw = self.generate(prompt, EPISODE_PROMO_SYSTEM_PROMPT, LLMPromosOut)
        out = self._as(LLMPromosOut, raw, "promos")

        ids = element_ids(episode)
        emap = dict(ids)
        pos = {eid: i for i, (eid, _) in enumerate(ids)}
        promos: list[PromoCandidate] = []
        for p in out.promos:
            segs: list[PromoSegment] = []
            ok = bool(p.segments)
            for s in p.segments:
                sid, eid = s.start_id.strip("[] "), s.end_id.strip("[] ")
                a, b = emap.get(sid), emap.get(eid)
                if a is None or b is None or pos.get(sid, 0) > pos.get(eid, 0) or b.end_seconds <= a.start_seconds:
                    ok = False
                    break
                segs.append(PromoSegment(
                    episode=episode.episode_number, start_time=a.start_time, end_time=b.end_time,
                    start_seconds=a.start_seconds, end_seconds=b.end_seconds,
                ))
            if not ok:
                self._bump("promos_rejected_bad_ids")
                continue
            dur = sum(s.duration_sec() for s in segs)
            if not (config.PROMO_MIN_DURATION_SEC <= dur <= config.PROMO_MAX_DURATION_SEC):
                self._bump("promos_rejected_duration")
                continue
            try:
                promos.append(_build_promo(segs, {episode.episode_number: episode}, p.hook, p.cliffhanger, p.why_it_works,
                                           _angle(p.creative_angle, p.hook, p.cliffhanger, p.why_it_works)))
            except ValidationError as exc:
                logger.warning("Skipping invalid promo: %s", exc)
                self._bump("promos_rejected_invalid")
        return promos

    # ── Pass 2b: cross-episode flash-forwards ────────────────────────
    def construct_cross_episode_promos(
        self,
        moments_list: list[EpisodeMoments],
        episodes: dict[int, ParsedEpisode],
        n: int = config.CROSS_EPISODE_CANDIDATES,
        per_episode: int = 5,
    ) -> list[PromoCandidate]:
        table = _moment_table(moments_list)
        lines: list[str] = []
        for em in moments_list:
            indexed = [(i, m) for i, m in enumerate(em.moments, 1)]
            top = sorted(indexed, key=lambda t: -t[1].intensity_score)[:per_episode]
            lines.append(f"=== EPISODE {em.episode_number} — {em.logline[:160]}")
            for i, m in sorted(top, key=lambda t: t[1].start_seconds):
                kd = f' | key line: "{m.key_dialogue}"' if m.key_dialogue else ""
                lines.append(
                    f"{moment_ref(em.episode_number, i)} | {m.moment_type.value} | int {m.intensity_score} | "
                    f"{m.end_seconds - m.start_seconds:.1f}s | {m.description}{kd}"
                )
        prompt = (
            "\n".join(lines)
            + f"\n\nBuild up to {n} cross-episode promos. Respond with JSON: "
            '{"promos": [{"segments": [{"moment_id": "M41-03"}, {"moment_id": "M01-05"}], "hook": "...", "cliffhanger": "...", '
            '"why_it_works": "...", "creative_angle": "hidden_identity"}]}'
        )
        raw = self.generate(prompt, CROSS_EPISODE_SYSTEM_PROMPT, LLMXPromosOut)
        out = self._as(LLMXPromosOut, raw, "promos")

        promos: list[PromoCandidate] = []
        for p in out.promos:
            mids = [s.moment_id.strip("[] ") for s in p.segments]
            if not (2 <= len(mids) <= 4) or len(set(mids)) != len(mids) or any(m not in table for m in mids):
                self._bump("cross_rejected_bad_ids")
                continue
            segs = [
                PromoSegment(
                    episode=table[m].episode_number, start_time=table[m].start_time, end_time=table[m].end_time,
                    start_seconds=table[m].start_seconds, end_seconds=table[m].end_seconds,
                )
                for m in mids
            ]
            dur = sum(s.duration_sec() for s in segs)

            # Elastic rescue: if within 4s of minimum boundary, expand final segment to next script element
            if dur < config.PROMO_MIN_DURATION_SEC and (config.PROMO_MIN_DURATION_SEC - dur) <= 4.0:
                last_seg = segs[-1]
                ep = episodes.get(last_seg.episode)
                if ep:
                    els = sorted_elements(ep)
                    needed = config.PROMO_MIN_DURATION_SEC - dur
                    candidates = [e for e in els if e.end_seconds >= last_seg.end_seconds + needed]
                    if candidates:
                        next_el = min(candidates, key=lambda e: e.end_seconds)
                        last_seg.end_time = next_el.end_time
                        last_seg.end_seconds = next_el.end_seconds
                        dur = sum(s.duration_sec() for s in segs)

            if not (config.PROMO_MIN_DURATION_SEC <= dur <= config.PROMO_MAX_DURATION_SEC):
                self._bump("cross_rejected_duration")
                continue
            try:
                promos.append(_build_promo(segs, episodes, p.hook, p.cliffhanger, p.why_it_works,
                                           _angle(p.creative_angle, p.hook, p.cliffhanger, p.why_it_works)))
            except ValidationError as exc:
                logger.warning("Skipping invalid cross-episode promo: %s", exc)
                self._bump("cross_rejected_invalid")
        return promos

    # ── Pass 3: blind scoring ────────────────────────────────────────
    def score_promo(self, promo: PromoCandidate, episodes: dict[int, ParsedEpisode]) -> tuple[PromoScores, str]:
        clip = render_clip_text(promo, episodes)
        prompt = (
            "Evaluate this clip.\n\n" + clip
            + '\n\nRespond with JSON: {"reasoning": "...", "hook_strength": 5, "cliffhanger_quality": 5, '
            '"standalone_coherence": 5, "emotional_impact": 5}'
        )
        raw = self.generate(prompt, SCORING_SYSTEM_PROMPT, LLMScoreOut,
                            model=config.GEMINI_JUDGE_MODEL, temperature=config.GEMINI_JUDGE_TEMPERATURE)
        s = LLMScoreOut.model_validate(raw)
        scores = PromoScores(
            hook_strength=_clamp(s.hook_strength),
            cliffhanger_quality=_clamp(s.cliffhanger_quality),
            standalone_coherence=_clamp(s.standalone_coherence),
            emotional_impact=_clamp(s.emotional_impact),
            duration_fitness=compute_duration_fitness(total_duration(promo)),
        )
        scores.compute_composite(config.SCORING_WEIGHTS)
        return scores, s.reasoning.strip()

    # ── Pass 4: adversarial critique ─────────────────────────────────
    def critique_promo(self, promo: PromoCandidate, episodes: dict[int, ParsedEpisode]) -> list[QualityFlag]:
        prompt = (
            "CLAIMS BY THE EDITOR\n"
            f"Hook: {promo.hook}\nCliffhanger: {promo.cliffhanger}\n\n"
            "THE CLIP\n" + render_clip_text(promo, episodes)
            + '\n\nRespond with JSON: {"verdict": "one sentence", "flags": [{"failure_type": "weak_hook", "severity": "high", '
            '"explanation": "...", "suggested_fix": "..."}]}'
        )
        raw = self.generate(prompt, CRITIQUE_SYSTEM_PROMPT, LLMCritiqueOut,
                            model=config.GEMINI_JUDGE_MODEL, temperature=config.GEMINI_JUDGE_TEMPERATURE)
        out = LLMCritiqueOut.model_validate(raw)
        allowed = {
            FailureType.CONTEXT_DEPENDENCY,
            FailureType.WEAK_HOOK,
            FailureType.RESOLVED_CLIFFHANGER,
            FailureType.PRONOUN_AMBIGUITY,
            FailureType.AD_POLICY_RISK,
            FailureType.HALLUCINATED_CLAIM,
        }
        flags: list[QualityFlag] = []
        for f in out.flags:
            try:
                ftype = FailureType(f.failure_type.strip().lower())
                sev = Severity(f.severity.strip().lower())
            except ValueError:
                continue
            if ftype not in allowed:
                continue
            try:
                flags.append(QualityFlag(
                    promo_id=promo.promo_id, failure_type=ftype, severity=sev,
                    explanation=_pad(f.explanation, 10, "Flagged by the critique pass."),
                    suggested_fix=f.suggested_fix.strip(),
                ))
            except ValidationError:
                continue
        return flags


# ──────────────────────────────────────────────────────────────────────
# OFFLINE HEURISTIC CLIENT (same interface; used for --no-llm runs, tests, and as a
# per-episode fallback when the API fails). Its outputs are labelled "heuristic" in
# the run manifest and must never be presented as LLM results.
# ──────────────────────────────────────────────────────────────────────
class HeuristicClient:
    name = "heuristic"
    available = True

    def __init__(self) -> None:
        self.cost_tracker = CostTracker()
        self.cache_hits = 0
        self.stats: Counter[str] = Counter()

    def identify_moments(self, episode: ParsedEpisode) -> EpisodeMoments:
        moments: list[DramaticMoment] = []
        for e in sorted_elements(episode):
            inten = element_intensity(e)
            if inten < 6 or e.end_seconds <= e.start_seconds:
                continue
            if e.element_type == ElementType.SOUND_FX:
                mtype = MomentType.CLIFFHANGER
            elif e.element_type == ElementType.ACTION:
                mtype = MomentType.HOOK_VISUAL
            else:
                mtype = MomentType.HOOK_DIALOGUE
            who = f"{e.character}: " if e.character else "Action: "
            moments.append(DramaticMoment(
                episode_number=episode.episode_number, moment_type=mtype, intensity_score=_clamp(inten),
                start_time=e.start_time, end_time=e.end_time, start_seconds=e.start_seconds, end_seconds=e.end_seconds,
                description=_pad((who + e.content)[:140], 10, "[Dramatic beat]"),
                key_dialogue=e.content if e.element_type == ElementType.DIALOGUE else None,
                characters_involved=[e.character] if e.character else [],
                emotion_labels=[e.emotion] if e.emotion else [],
            ))
        return EpisodeMoments(episode_number=episode.episode_number, logline=episode.logline,
                              moments=moments, total_duration_sec=episode.total_duration_sec)

    def construct_episode_promos(self, episode: ParsedEpisode, moments: EpisodeMoments | None = None,
                                 n: int = config.PER_EPISODE_CANDIDATES) -> list[PromoCandidate]:
        els = sorted_elements(episode)
        moments = moments or self.identify_moments(episode)
        used: list[tuple[float, float]] = []
        promos: list[PromoCandidate] = []
        for m in sorted(moments.moments, key=lambda x: (-x.intensity_score, x.start_seconds)):
            if len(promos) >= n:
                break
            start = m.start_seconds
            ends = [e for e in els if 25.0 <= e.end_seconds - start <= 45.0] or \
                   [e for e in els if config.PROMO_MIN_DURATION_SEC <= e.end_seconds - start <= config.PROMO_MAX_DURATION_SEC]
            if not ends:
                continue
            end_el = max(ends, key=lambda e: (element_intensity(e), -abs(e.end_seconds - start - 35.0)))
            end = end_el.end_seconds
            if any(not (end <= u0 or start >= u1) for u0, u1 in used):
                continue
            used.append((start, end))
            inside = [e for e in clip_elements(episode, start, end) if e.element_type in (ElementType.DIALOGUE, ElementType.ACTION)]
            if not inside:
                continue
            seg = PromoSegment(episode=episode.episode_number, start_time=m.start_time, end_time=end_el.end_time,
                               start_seconds=start, end_seconds=end)
            hook_el, last_el = inside[0], inside[-1]
            try:
                promos.append(_build_promo(
                    [seg], {episode.episode_number: episode},
                    f"Opens on: {element_text(hook_el, 120)}", f"Ends on: {element_text(last_el, 120)}",
                    "Heuristic pick: high-intensity opening beat and a 25-45s window ending on an intense line.",
                    _angle("", hook_el.content, last_el.content, episode.logline),
                ))
            except ValidationError:
                continue
        return promos

    def construct_cross_episode_promos(
        self,
        moments_list: list[EpisodeMoments],
        episodes: dict[int, ParsedEpisode],
        n: int = config.CROSS_EPISODE_CANDIDATES,
        per_episode: int = 5,
    ) -> list[PromoCandidate]:
        """Offline deterministic heuristic for cross-episode flash-forwards.
        Stitches a late-episode high-intensity hook with an early-episode betrayal context."""
        table = _moment_table(moments_list)
        if not table:
            return []

        # Find late-episode climax moments (ep >= 30 or highest available)
        late_moments = [
            (ref, m) for ref, m in table.items()
            if m.episode_number >= 30 and m.intensity_score >= 8
        ]
        # Find early-episode setup/betrayal moments (ep <= 10)
        early_moments = [
            (ref, m) for ref, m in table.items()
            if m.episode_number <= 10 and m.intensity_score >= 7
        ]

        if not late_moments or not early_moments:
            sorted_m = sorted(table.items(), key=lambda x: -x[1].intensity_score)
            if len(sorted_m) < 2:
                return []
            late_moments = [sorted_m[0]]
            early_moments = [sorted_m[1]]

        promos: list[PromoCandidate] = []
        for l_ref, l_m in late_moments[:n]:
            for e_ref, e_m in early_moments:
                if len(promos) >= n:
                    break
                segs = [
                    PromoSegment(
                        episode=l_m.episode_number,
                        start_time=l_m.start_time,
                        end_time=l_m.end_time,
                        start_seconds=l_m.start_seconds,
                        end_seconds=l_m.end_seconds,
                    ),
                    PromoSegment(
                        episode=e_m.episode_number,
                        start_time=e_m.start_time,
                        end_time=e_m.end_time,
                        start_seconds=e_m.start_seconds,
                        end_seconds=e_m.end_seconds,
                    ),
                ]
                dur = sum(s.duration_sec() for s in segs)
                # Pad to minimum duration if needed
                if dur < config.PROMO_MIN_DURATION_SEC:
                    ep = episodes.get(e_m.episode_number)
                    if ep:
                        els = sorted_elements(ep)
                        needed = config.PROMO_MIN_DURATION_SEC - dur
                        candidates = [e for e in els if e.end_seconds >= segs[-1].end_seconds + needed]
                        if candidates:
                            next_el = min(candidates, key=lambda e: e.end_seconds)
                            segs[-1].end_time = next_el.end_time
                            segs[-1].end_seconds = next_el.end_seconds
                            dur = sum(s.duration_sec() for s in segs)

                if config.PROMO_MIN_DURATION_SEC <= dur <= config.PROMO_MAX_DURATION_SEC:
                    try:
                        p = _build_promo(
                            segs,
                            episodes,
                            f"Flash-forward opens on Ep {l_m.episode_number}: {l_m.description[:80]}",
                            f"Flashback cut to Ep {e_m.episode_number}: {e_m.description[:80]}",
                            "Heuristic cross-episode hook: in media res climax stitched with origin betrayal.",
                            "hidden_identity" if l_m.episode_number >= 40 else "betrayal_revenge",
                        )
                        promos.append(p)
                    except ValidationError:
                        continue
        return promos

    def score_promo(self, promo: PromoCandidate, episodes: dict[int, ParsedEpisode]) -> tuple[PromoScores, str]:
        first, last = promo.segments[0], promo.segments[-1]
        ep_f, ep_l = episodes[first.episode], episodes[last.episode]
        hook_els = [e for e in clip_elements(ep_f, first.start_seconds, first.end_seconds)
                    if e.start_seconds <= first.start_seconds + config.HOOK_WINDOW_SEC + 0.011]
        tail_els = [e for e in clip_elements(ep_l, last.start_seconds, last.end_seconds)
                    if e.end_seconds >= last.end_seconds - config.CLIFFHANGER_WINDOW_SEC - 0.011]
        hook = max((element_intensity(e) for e in hook_els), default=1)
        tail = max((element_intensity(e) for e in tail_els), default=1)
        speakers = {c for s in promo.segments for c in speakers_in_clip(episodes[s.episode], s.start_seconds, s.end_seconds)}
        all_int = sorted((element_intensity(e) for s in promo.segments
                          for e in clip_elements(episodes[s.episode], s.start_seconds, s.end_seconds)), reverse=True)
        emo = sum(all_int[:3]) / max(1, len(all_int[:3])) if all_int else 1
        coherence = 8 - 1.5 * max(0, len(speakers) - 2) - 1.0 * (len(promo.segments) - 1)
        scores = PromoScores(
            hook_strength=_clamp(hook * 0.9), cliffhanger_quality=_clamp(tail * 0.9 + (1 if tail_els and tail_els[-1].element_type == ElementType.SOUND_FX else 0)),
            standalone_coherence=_clamp(coherence), emotional_impact=_clamp(emo),
            duration_fitness=compute_duration_fitness(total_duration(promo)),
        )
        scores.compute_composite(config.SCORING_WEIGHTS)
        return scores, "Heuristic rule-based score (emotion labels + keywords); no LLM involved."

    def critique_promo(self, promo: PromoCandidate, episodes: dict[int, ParsedEpisode]) -> list[QualityFlag]:
        return []
