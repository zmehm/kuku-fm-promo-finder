"""
parser.py — Deterministic screenplay parser for Kuku FM microdrama scripts.

Converts raw .txt screenplay files into structured ``ParsedEpisode`` objects.
All extraction is regex-based and deterministic — no LLM calls are made here.

Supported element types
-----------------------
- **DIALOGUE**: ``**CHARACTER NAME (Emotion):**`` with timestamps and quoted text
- **ACTION**: ``**ACTION:**`` with timestamps and description
- **CAMERA**: ``**CAMERA:**`` with timestamps and description
- **SOUND FX**: ``**SOUND FX:**`` with timestamps and description
- **MUSIC**: ``**MUSIC:**`` with timestamps and description

Public API
----------
- ``parse_timestamp``   — "MM:SS.ms" → seconds
- ``parse_episode``     — single file  → ``ParsedEpisode``
- ``parse_all_episodes``— directory    → list of ``ParsedEpisode``
- ``save_parsed_episodes`` — serialise episodes to JSON files

Usage::

    python -m src.parser
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from src.config import LOG_FORMAT, LOG_LEVEL, PARSED_DIR, SCRIPTS_DIR
from src.schemas import ElementType, ParsedEpisode, Scene, ScriptElement

# ──────────────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────────────
logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# REGEX PATTERNS (compiled once at import time)
# ──────────────────────────────────────────────────────────────────────

# Episode metadata ---------------------------------------------------
_RE_EPISODE_NUM = re.compile(r"episode-(\d{3})-", re.IGNORECASE)
_RE_EPISODE_HEADER_NUM = re.compile(r"^EPISODE NUMBER:\s*(?:Episode\s*)?(\d+)", re.IGNORECASE | re.MULTILINE)
_RE_LOGLINE = re.compile(r"^EPISODE LOGLINE:\s*(.+)$", re.MULTILINE)
_RE_CONTINUITY = re.compile(r"^CONTINUITY NOTES:\s*(.+)$", re.MULTILINE)

# Scene boundaries ---------------------------------------------------
_RE_SCENE_HEADER = re.compile(
    r"^SCENE\s+(\d+)\s*$",
    re.MULTILINE,
)
_RE_SCENE_HEADING = re.compile(r"^Scene heading:\s*(.+)$", re.MULTILINE)
_RE_SCENE_START = re.compile(r"^Scene start:\s*(\d{2}:\d{2}[.,]\d{1,3})\s*$", re.MULTILINE)
_RE_SCENE_END = re.compile(r"^Scene end:\s*(\d{2}:\d{2}[.,]\d{1,3})\s*$", re.MULTILINE)

# Timestamp line shared by all elements ------------------------------
_RE_TIMESTAMP_LINE = re.compile(
    r"^\s*Start:\s*\((\d{2}:\d{2}[.,]\d{1,3})\)\s*\|\s*End:\s*\((\d{2}:\d{2}[.,]\d{1,3})\)\s*$",
    re.MULTILINE,
)

# Element header patterns --------------------------------------------
# Non-dialogue: **ACTION:** / **CAMERA:** / **SOUND FX:** / **MUSIC:**
_RE_NON_DIALOGUE_HEADER = re.compile(
    r"^\*\*(?P<type>ACTION|CAMERA|SOUND FX|MUSIC):\*\*\s*$",
    re.MULTILINE,
)

# Dialogue header pattern — handles:
#   **CHARACTER NAME (Emotion):**
#   **CHARACTER NAME (O.S.) (Emotion):**
#   **CHARACTER NAME (V.O.) (Emotion):**
#   **CHARACTER NAME (O.S. / Continues):**
#   **CHARACTER NAME (Emotion, Emotion2):**
#   **CHARACTER NAME (Choking on Tears):**
#   **CHARACTER NAME (Reverent toward Clarissa):**
# The pattern is deliberately broad for the emotion group to handle
# multi-word emotions, annotations like "O.S. / Continues", etc.
_RE_DIALOGUE_HEADER = re.compile(
    r"^\*\*"
    r"(?P<character>[A-Z][A-Z .'\-]+?)"      # Character name (UPPERCASE, may contain ., ', -, space)
    r"(?:\s*\((?P<annotation>O\.S\.|V\.O\.|Off-screen|Voiceover|Voice-over)"  # Optional annotation
    r"(?:\s*/\s*[A-Za-z]+)?\))??"             # Optional /Continues etc. after annotation — non-greedy
    r"\s*\((?P<emotion>[^)]+)\)"              # Emotion(s) in parens
    r":\*\*\s*$",
    re.MULTILINE,
)

# Also handle the combined pattern: **CHARACTER (O.S.) (Emotion):**
# where O.S./V.O./Off-screen is a separate parenthetical before the emotion.
# The above regex already handles this with the optional annotation group.

# Fallback: header that has annotation but potentially no separate emotion group
# e.g., **LORD ASHCROFT (O.S. / Continues):**
_RE_DIALOGUE_HEADER_ANNOTATION_ONLY = re.compile(
    r"^\*\*"
    r"(?P<character>[A-Z][A-Z .'\-]+?)"
    r"\s*\((?P<annotation>O\.S\.|V\.O\.|Off-screen|Voiceover|Voice-over)"
    r"(?:\s*/\s*(?P<extra>[A-Za-z]+))?\)"
    r":\*\*\s*$",
    re.MULTILINE,
)


# ──────────────────────────────────────────────────────────────────────
# TIMESTAMP CONVERSION & NORMALISATION
# ──────────────────────────────────────────────────────────────────────

def normalize_timestamp(ts: str) -> str:
    """Normalise timestamp to MM:SS.cc format (^\\d{2}:\\d{2}\\.\\d{2}$).

    Accepts '.' or ',' as decimal separator and 1 to 3 fractional digits.
    Pads with trailing zero (e.g. '00:04.8' -> '00:04.80') or rounds to 0.01s precision.
    """
    ts = ts.strip()
    match = re.fullmatch(r"(\d{2}):(\d{2})[.,](\d{1,3})", ts)
    if not match:
        raise ValueError(f"Invalid timestamp format: {ts!r}. Expected 'MM:SS.ms'.")
    minutes = int(match[1])
    seconds = int(match[2])
    frac_str = match[3]

    if len(frac_str) == 1:
        centiseconds = int(frac_str) * 10
    elif len(frac_str) == 2:
        centiseconds = int(frac_str)
    else:  # 3 fractional digits
        centiseconds = int(round(int(frac_str) / 10.0))
        if centiseconds >= 100:
            seconds += 1
            centiseconds -= 100
            if seconds >= 60:
                minutes += 1
                seconds -= 60
    return f"{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def parse_timestamp(ts: str) -> float:
    """Convert a timestamp string to total seconds.

    Accepts '.' or ',' as decimal separator and 1 to 3 fractional digits.
    Converts and normalises to 0.01s precision.

    Parameters
    ----------
    ts : str
        Timestamp in ``"MM:SS.ms"`` format, e.g. ``"02:15.00"``, ``"00:04.8"``, ``"00:04,80"``.

    Returns
    -------
    float
        Total seconds.  ``"02:15.00"`` → ``135.0``.

    Raises
    ------
    ValueError
        If *ts* does not match the expected format.
    """
    norm = normalize_timestamp(ts)
    match = re.fullmatch(r"(\d{2}):(\d{2})\.(\d{2})", norm)
    if not match:
        raise ValueError(f"Invalid timestamp format: {ts!r}. Expected 'MM:SS.ms'.")
    minutes, seconds, centiseconds = int(match[1]), int(match[2]), int(match[3])
    return minutes * 60.0 + seconds + centiseconds / 100.0


# ──────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ──────────────────────────────────────────────────────────────────────

_ELEMENT_TYPE_MAP: dict[str, ElementType] = {
    "ACTION": ElementType.ACTION,
    "CAMERA": ElementType.CAMERA,
    "SOUND FX": ElementType.SOUND_FX,
    "MUSIC": ElementType.MUSIC,
}


def _extract_episode_number(filepath: Path, text: str = "") -> int:
    """Extract the episode number from a filename like ``episode-001-Episode 1671703.txt``.

    Falls back to parsing the ``EPISODE NUMBER:`` line inside the file
    if the filename pattern does not match.
    """
    m = _RE_EPISODE_NUM.search(filepath.name)
    if m:
        return int(m.group(1))
    if not text and filepath.exists():
        text = filepath.read_text(encoding="utf-8", errors="replace")
    m2 = _RE_EPISODE_HEADER_NUM.search(text)
    if m2:
        return int(m2.group(1))
    m3 = re.search(r"(\d+)", filepath.stem)
    if m3:
        return int(m3.group(1))
    return 1


def _extract_metadata(text: str) -> tuple[str, str]:
    """Return ``(logline, continuity_notes)`` from the episode header."""
    logline_m = _RE_LOGLINE.search(text)
    continuity_m = _RE_CONTINUITY.search(text)
    logline = logline_m.group(1).strip() if logline_m else ""
    continuity = continuity_m.group(1).strip() if continuity_m else ""
    return logline, continuity


def _parse_element_block(lines: list[str], line_idx: int) -> tuple[Optional[ScriptElement], int]:
    """Parse a single element block starting at *line_idx*.

    An element block looks like::

        **TYPE:**  (or **CHARACTER (Emotion):**)
        Start: (MM:SS.ms) | End: (MM:SS.ms)
        Content line(s)

    Returns
    -------
    (ScriptElement | None, next_line_index)
    """
    if line_idx >= len(lines):
        return None, line_idx

    header_line = lines[line_idx].strip()
    if not header_line.startswith("**"):
        return None, line_idx + 1

    # ── Try non-dialogue match first (ACTION, CAMERA, SOUND FX, MUSIC)
    nd_match = _RE_NON_DIALOGUE_HEADER.match(header_line)
    if nd_match:
        elem_type = _ELEMENT_TYPE_MAP[nd_match.group("type")]
        character: Optional[str] = None
        emotion: Optional[str] = None
    else:
        # ── Try dialogue match
        d_match = _RE_DIALOGUE_HEADER.match(header_line)
        if d_match:
            character = d_match.group("character").strip()
            annotation = d_match.group("annotation")
            emotion = d_match.group("emotion").strip()
            if annotation:
                character = f"{character} ({annotation})"
            elem_type = ElementType.DIALOGUE
        else:
            # ── Try annotation-only dialogue (no separate emotion)
            da_match = _RE_DIALOGUE_HEADER_ANNOTATION_ONLY.match(header_line)
            if da_match:
                character = da_match.group("character").strip()
                annotation = da_match.group("annotation")
                extra = da_match.group("extra")
                character = f"{character} ({annotation})"
                emotion = extra if extra else None
                elem_type = ElementType.DIALOGUE
            else:
                logger.warning("Unrecognised header at line %d: %s", line_idx + 1, header_line)
                return None, line_idx + 1

    # ── Next line must be the timestamp line
    ts_idx = line_idx + 1
    if ts_idx >= len(lines):
        logger.warning("Missing timestamp after header at line %d", line_idx + 1)
        return None, ts_idx

    ts_match = _RE_TIMESTAMP_LINE.match(lines[ts_idx])
    if not ts_match:
        logger.warning(
            "Expected timestamp at line %d but got: %s",
            ts_idx + 1,
            lines[ts_idx].strip(),
        )
        return None, ts_idx

    start_time = normalize_timestamp(ts_match.group(1))
    end_time = normalize_timestamp(ts_match.group(2))

    # ── Collect content lines until the next element header or blank run
    content_lines: list[str] = []
    ci = ts_idx + 1
    while ci < len(lines):
        cl = lines[ci].strip()
        # Stop if we hit the next element header
        if cl.startswith("**"):
            break
        # Stop if we hit a scene boundary or script-end marker
        if cl.startswith("SCENE ") or cl == "--- SCRIPT END ---":
            break
        if cl:
            content_lines.append(cl)
        ci += 1

    content_text = " ".join(content_lines)
    # Strip surrounding quotes from dialogue content
    if elem_type == ElementType.DIALOGUE and content_text:
        content_text = content_text.strip('"').strip('\u201c\u201d')

    if not content_text:
        # Some camera/action elements may lack a separate content line
        # if description is very short — still create the element
        logger.debug("Empty content for element at line %d", line_idx + 1)
        content_text = "(no content)"

    try:
        start_sec = parse_timestamp(start_time)
        end_sec = parse_timestamp(end_time)
    except ValueError as exc:
        logger.error("Bad timestamp at line %d: %s", line_idx + 1, exc)
        return None, ci

    element = ScriptElement(
        element_type=elem_type,
        start_time=start_time,
        end_time=end_time,
        character=character,
        emotion=emotion,
        content=content_text,
        start_seconds=start_sec,
        end_seconds=end_sec,
    )

    return element, ci


def _split_into_scene_blocks(text: str) -> list[tuple[int, str]]:
    """Split the script body into per-scene text blocks.

    Returns a list of ``(scene_number, scene_text)`` tuples.
    """
    # Find all SCENE headers with their positions
    scene_starts: list[tuple[int, int]] = []  # (char_pos, scene_num)
    for m in _RE_SCENE_HEADER.finditer(text):
        scene_starts.append((m.start(), int(m.group(1))))

    if not scene_starts:
        return []

    blocks: list[tuple[int, str]] = []
    for i, (pos, scene_num) in enumerate(scene_starts):
        if i + 1 < len(scene_starts):
            block_text = text[pos : scene_starts[i + 1][0]]
        else:
            block_text = text[pos:]
        blocks.append((scene_num, block_text))

    return blocks


def _parse_scene(scene_num: int, scene_text: str) -> Scene:
    """Parse a single scene text block into a ``Scene`` model."""
    heading_m = _RE_SCENE_HEADING.search(scene_text)
    start_m = _RE_SCENE_START.search(scene_text)
    end_m = _RE_SCENE_END.search(scene_text)

    heading = heading_m.group(1).strip() if heading_m else f"SCENE {scene_num:02d}"
    start_time = normalize_timestamp(start_m.group(1)) if start_m else "00:00.00"
    end_time = normalize_timestamp(end_m.group(1)) if end_m else "00:00.00"

    start_sec = parse_timestamp(start_time)
    end_sec = parse_timestamp(end_time)

    # Parse elements line-by-line
    lines = scene_text.splitlines()
    elements: list[ScriptElement] = []

    # Find the "Actions and dialogue:" marker to start parsing elements
    start_idx = 0
    for idx, line in enumerate(lines):
        if line.strip().lower().startswith("actions and dialogue"):
            start_idx = idx + 1
            break

    i = start_idx
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("**"):
            elem, next_i = _parse_element_block(lines, i)
            if elem is not None:
                elements.append(elem)
            i = next_i
        else:
            i += 1

    return Scene(
        scene_number=scene_num,
        heading=heading,
        start_time=start_time,
        end_time=end_time,
        start_seconds=start_sec,
        end_seconds=end_sec,
        elements=elements,
    )


# ──────────────────────────────────────────────────────────────────────
# PUBLIC API
# ──────────────────────────────────────────────────────────────────────

def parse_episode(filepath: Path) -> ParsedEpisode:
    """Parse a single screenplay ``.txt`` file into a :class:`ParsedEpisode`.

    Parameters
    ----------
    filepath : Path
        Absolute or relative path to the episode ``.txt`` file.

    Returns
    -------
    ParsedEpisode
        Fully populated episode with scenes, elements, and computed stats.

    Raises
    ------
    FileNotFoundError
        If *filepath* does not exist.
    ValueError
        If the episode number cannot be extracted.
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"Episode file not found: {filepath}")

    logger.info("Parsing episode: %s", filepath.name)
    text = filepath.read_text(encoding="utf-8")
    episode_num = _extract_episode_number(filepath, text)
    episode_id = filepath.stem  # e.g. "episode-001-Episode 1671703"
    logline, continuity_notes = _extract_metadata(text)

    # Split into scenes and parse each
    scene_blocks = _split_into_scene_blocks(text)
    scenes: list[Scene] = []
    for scene_num, scene_text in scene_blocks:
        scene = _parse_scene(scene_num, scene_text)
        scenes.append(scene)
        logger.debug(
            "  Scene %02d: %d elements, %.1f–%.1fs",
            scene.scene_number,
            len(scene.elements),
            scene.start_seconds,
            scene.end_seconds,
        )

    # Compute aggregate stats
    all_elements = [e for s in scenes for e in s.elements]
    if not all_elements:
        raise ValueError(
            f"No script elements parsed from {filepath.name}: check the timestamp/format"
        )
    dialogue_elements = [e for e in all_elements if e.element_type == ElementType.DIALOGUE]

    # Total duration from the last scene's end time, falling back to largest end_seconds
    total_duration = max((s.end_seconds for s in scenes), default=0.0)
    if total_duration <= 0.0 and all_elements:
        total_duration = max((e.end_seconds for e in all_elements), default=0.0)

    episode = ParsedEpisode(
        episode_number=episode_num,
        episode_id=episode_id,
        logline=logline,
        continuity_notes=continuity_notes,
        scenes=scenes,
        total_duration_sec=total_duration,
        total_elements=len(all_elements),
        total_dialogue_lines=len(dialogue_elements),
    )

    logger.info(
        "  ✓ Episode %03d: %d scenes, %d elements (%d dialogue), %.1fs total",
        episode.episode_number,
        len(episode.scenes),
        episode.total_elements,
        episode.total_dialogue_lines,
        episode.total_duration_sec,
    )

    return episode


def parse_all_episodes(scripts_dir: Path) -> list[ParsedEpisode]:
    """Parse all ``.txt`` screenplay files in *scripts_dir*, sorted by episode number.

    Parameters
    ----------
    scripts_dir : Path
        Directory containing episode ``.txt`` files.

    Returns
    -------
    list[ParsedEpisode]
        Parsed episodes sorted by episode number.
    """
    scripts_dir = Path(scripts_dir)
    if not scripts_dir.is_dir():
        raise NotADirectoryError(f"Scripts directory not found: {scripts_dir}")

    txt_files = sorted(scripts_dir.glob("episode-*.txt"))
    if not txt_files:
        logger.warning("No episode-*.txt files found in %s", scripts_dir)
        return []

    logger.info("Found %d episode files in %s", len(txt_files), scripts_dir)

    episodes: list[ParsedEpisode] = []
    for fp in txt_files:
        try:
            ep = parse_episode(fp)
            episodes.append(ep)
        except Exception as exc:
            logger.error("Failed to parse %s: %s", fp.name, exc)

    episodes.sort(key=lambda e: e.episode_number)
    logger.info("Successfully parsed %d / %d episodes", len(episodes), len(txt_files))
    return episodes


def save_parsed_episodes(episodes: list[ParsedEpisode], output_dir: Path) -> None:
    """Serialise each :class:`ParsedEpisode` to a JSON file.

    Files are written as ``episode-NNN.json`` into *output_dir*.

    Parameters
    ----------
    episodes : list[ParsedEpisode]
        Episodes to save.
    output_dir : Path
        Target directory (created if it does not exist).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    for ep in episodes:
        out_path = output_dir / f"episode-{ep.episode_number:03d}.json"
        out_path.write_text(
            ep.model_dump_json(indent=2),
            encoding="utf-8",
        )
        logger.info("Saved parsed episode → %s", out_path)


# ──────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    # Parse Episode 1 and print a summary
    ep1_path = SCRIPTS_DIR / "episode-001-Episode 1671703.txt"
    if not ep1_path.exists():
        logger.error("Episode 1 not found at %s", ep1_path)
        sys.exit(1)

    ep = parse_episode(ep1_path)

    unique_chars = ep.get_unique_characters()
    dialogue_elements = ep.get_dialogue_elements()
    all_elements = ep.get_all_elements()

    print("\n" + "=" * 64)
    print("  PROMO FINDER — PARSER SUMMARY")
    print("=" * 64)
    print(f"  Episode:            {ep.episode_number}")
    print(f"  Episode ID:         {ep.episode_id}")
    print(f"  Logline:            {ep.logline[:80]}...")
    print(f"  Scenes:             {len(ep.scenes)}")
    print(f"  Total Elements:     {ep.total_elements}")
    print(f"  Dialogue Lines:     {ep.total_dialogue_lines}")
    print(f"  Unique Characters:  {len(unique_chars)} — {', '.join(sorted(unique_chars))}")
    print(f"  Total Duration:     {ep.total_duration_sec:.1f}s")
    print("-" * 64)

    # Per-scene breakdown
    for scene in ep.scenes:
        scene_dialogue = [e for e in scene.elements if e.element_type == ElementType.DIALOGUE]
        print(
            f"  Scene {scene.scene_number:02d}: "
            f"{scene.heading:<35s} "
            f"{len(scene.elements):3d} elements  "
            f"{len(scene_dialogue):3d} dialogue  "
            f"{scene.duration_sec():.1f}s"
        )

    # Element type breakdown
    print("-" * 64)
    from collections import Counter

    type_counts = Counter(e.element_type.value for e in all_elements)
    for etype, count in sorted(type_counts.items()):
        print(f"  {etype:<12s}: {count}")

    # Sample dialogue
    print("-" * 64)
    print("  SAMPLE DIALOGUE (first 5 lines):")
    for d in dialogue_elements[:5]:
        print(
            f"    [{d.start_time}–{d.end_time}] "
            f"{d.character} ({d.emotion}): \"{d.content}\""
        )
    print("=" * 64 + "\n")
