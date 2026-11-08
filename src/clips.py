"""
clips.py — Shared helpers used by the parser, LLM client, quality checker and pipeline.

Key idea: the LLM never writes timestamps. Every script element gets a stable ID
(e.g. "01.017" = episode 1, 17th element by start time). The LLM picks IDs, and
CODE converts IDs into exact, verbatim timestamps. This removes timestamp
hallucination by construction.
"""
from __future__ import annotations

from src import config
from src.schemas import ElementType, ParsedEpisode, PromoCandidate, PromoSegment, ScriptElement

EPS = 0.011  # float tolerance for 0.01s script timestamps


# ── Timestamps ───────────────────────────────────────────────────────────
def fmt_ts(seconds: float) -> str:
    """Seconds -> 'MM:SS.cc' (the script's own timestamp format)."""
    total_cs = int(round(max(0.0, seconds) * 100))
    minutes, rem = divmod(total_cs, 6000)
    return f"{minutes:02d}:{rem / 100:05.2f}"


# ── Element indexing ─────────────────────────────────────────────────────
def sorted_elements(ep: ParsedEpisode) -> list[ScriptElement]:
    """All elements ordered by start time (stable w.r.t. script order)."""
    els = ep.get_all_elements()
    order = sorted(range(len(els)), key=lambda i: (els[i].start_seconds, i))
    return [els[i] for i in order]


def element_ids(ep: ParsedEpisode) -> list[tuple[str, ScriptElement]]:
    """[(id, element)] where id = '<episode>.<1-based position by start time>'."""
    return [(f"{ep.episode_number:02d}.{i:03d}", e) for i, e in enumerate(sorted_elements(ep), 1)]


def get_element_by_id(ep: ParsedEpisode, target_id: str) -> ScriptElement | None:
    """Find a script element by its stable ID (e.g. '01.017')."""
    for eid, e in element_ids(ep):
        if eid == target_id:
            return e
    return None


def id_to_segment(ep: ParsedEpisode, start_id: str, end_id: str) -> PromoSegment | None:
    """Convert a start and end ID into an exact, verbatim PromoSegment."""
    start_elem = get_element_by_id(ep, start_id)
    end_elem = get_element_by_id(ep, end_id)
    if not start_elem or not end_elem:
        return None
    return PromoSegment(
        episode=ep.episode_number,
        start_time=start_elem.start_time,
        end_time=end_elem.end_time,
        start_seconds=start_elem.start_seconds,
        end_seconds=end_elem.end_seconds,
    )


def clip_elements(ep: ParsedEpisode, start: float, end: float) -> list[ScriptElement]:
    """Elements fully inside [start, end]."""
    return [
        e for e in sorted_elements(ep)
        if e.start_seconds >= start - EPS and e.end_seconds <= end + EPS
    ]


def speakers_in_clip(ep: ParsedEpisode, start: float, end: float) -> list[str]:
    seen: list[str] = []
    for e in clip_elements(ep, start, end):
        if e.element_type == ElementType.DIALOGUE and e.character and e.character not in seen:
            seen.append(e.character)
    return seen


# ── Text rendering ───────────────────────────────────────────────────────
def _label(e: ScriptElement) -> str:
    return e.element_type.value.replace("_", " ")


def element_text(e: ScriptElement, max_chars: int | None = None) -> str:
    if e.element_type == ElementType.DIALOGUE:
        emo = f" ({e.emotion})" if e.emotion else ""
        text = f'{e.character}{emo}: "{e.content}"'
    else:
        text = f"{_label(e)}: {e.content}"
    if max_chars and len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def render_episode_for_llm(ep: ParsedEpisode, max_chars: int = 220) -> str:
    """Episode as ID-tagged lines. The LLM must reference lines by ID only."""
    scene_of: dict[int, str] = {}
    for sc in ep.scenes:
        for e in sc.elements:
            scene_of[id(e)] = sc.heading
    lines = [
        f"=== EPISODE {ep.episode_number} ===",
        f"Logline: {ep.logline}",
        f"Duration: {ep.total_duration_sec:.1f}s",
        "",
    ]
    last_scene = None
    for eid, e in element_ids(ep):
        heading = scene_of.get(id(e))
        if heading and heading != last_scene:
            lines.append(f"--- {heading} ---")
            last_scene = heading
        lines.append(f"[{eid}] {e.start_time}-{e.end_time} | {element_text(e, max_chars)}")
    return "\n".join(lines)


def render_clip_text(promo: PromoCandidate, episodes: dict[int, ParsedEpisode], max_chars: int = 220) -> str:
    """What a viewer would see/hear, with times relative to the start of the clip."""
    total = sum(s.duration_sec() for s in promo.segments)
    lines = [f"CLIP LENGTH: {total:.1f}s"]
    offset = 0.0
    for i, seg in enumerate(promo.segments):
        ep = episodes.get(seg.episode)
        if ep is None:
            continue
        if i > 0:
            lines.append("--- HARD CUT TO A DIFFERENT MOMENT OF THE SHOW ---")
        for e in clip_elements(ep, seg.start_seconds, seg.end_seconds):
            t = offset + max(0.0, e.start_seconds - seg.start_seconds)
            lines.append(f"[+{t:05.1f}s] {element_text(e, max_chars)}")
        offset += seg.duration_sec()
    return "\n".join(lines)


def clip_blob(promo: PromoCandidate, episodes: dict[int, ParsedEpisode]) -> str:
    """All clip text (dialogue + actions) concatenated, for substring checks."""
    parts: list[str] = []
    for seg in promo.segments:
        ep = episodes.get(seg.episode)
        if ep is None:
            continue
        parts.extend(e.content for e in clip_elements(ep, seg.start_seconds, seg.end_seconds))
    return " ".join(parts)


# ── Heuristic intensity (used by the offline fallback + rule baseline) ────
_ACTION_WORDS = (
    "slap", "kneel", "blood", "sword", "seize", "strike", "clench", "inferno", "chains",
    "explosion", "fire", "dagger", "scream", "collapse", "noose", "brand", "shatter",
)
_DIALOGUE_WORDS = ("betray", "kill", "swore", "marry", "poison", "slap", "chains", "explosion", "hang", "traitor", "treason")


def element_intensity(e: ScriptElement) -> int:
    """0-10 rule-based dramatic intensity of one element."""
    score = 0
    if e.element_type == ElementType.DIALOGUE:
        if e.emotion:
            low = e.emotion.lower()
            for tier, val in (("tier_1", 9), ("tier_2", 8), ("tier_3", 6)):
                if any(w in low for w in config.HIGH_INTENSITY_EMOTIONS[tier]):
                    score = max(score, val)
        low_c = e.content.lower()
        if any(w in low_c for w in _DIALOGUE_WORDS):
            score = max(score, 7)
        if "!" in e.content:
            score = max(score, 4)
    elif e.element_type == ElementType.ACTION:
        low_c = e.content.lower()
        score = 8 if any(w in low_c for w in _ACTION_WORDS) else 3
    elif e.element_type == ElementType.SOUND_FX:
        score = 7
    return score
