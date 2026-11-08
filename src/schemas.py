"""
schemas.py — Pydantic v2 models for strict type safety and validation.

Every data structure flowing through the pipeline has a schema here.
These enforce data contracts between phases and prevent hallucinated
or malformed LLM outputs from propagating downstream.
"""

from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field, field_validator, model_validator
from enum import Enum


# ──────────────────────────────────────────────────────────────────────
# ENUMS
# ──────────────────────────────────────────────────────────────────────

class ElementType(str, Enum):
    """Types of elements found in a screenplay."""
    DIALOGUE = "DIALOGUE"
    ACTION = "ACTION"
    CAMERA = "CAMERA"
    SOUND_FX = "SOUND_FX"
    MUSIC = "MUSIC"
    SCENE_HEADING = "SCENE_HEADING"


class MomentType(str, Enum):
    """Types of dramatic moments identifiable in a script."""
    HOOK_VISUAL = "hook_visual"           # Physical action: slap, explosion, reveal
    HOOK_DIALOGUE = "hook_dialogue"       # Shocking spoken line
    CLIFFHANGER = "cliffhanger"           # Unresolved tension at a cut point
    CONFRONTATION = "confrontation"       # Face-to-face conflict
    REVEAL = "reveal"                     # Identity or secret exposed
    BETRAYAL = "betrayal"                 # Trust broken
    EMOTIONAL_PEAK = "emotional_peak"     # Maximum emotional intensity


class FailureType(str, Enum):
    """Categories of quality failures in promo picks."""
    CONTEXT_DEPENDENCY = "context_dependency"
    WEAK_HOOK = "weak_hook"
    RESOLVED_CLIFFHANGER = "resolved_cliffhanger"
    GENRE_MISMATCH = "genre_mismatch"
    TIMESTAMP_IMPRECISION = "timestamp_imprecision"
    REDUNDANCY = "redundancy"
    PRONOUN_AMBIGUITY = "pronoun_ambiguity"
    AD_POLICY_RISK = "ad_policy_risk"
    HALLUCINATED_CLAIM = "hallucinated_claim"


class Severity(str, Enum):
    """Severity levels for quality flags."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class PromoType(str, Enum):
    """Whether a promo uses one continuous stretch or multiple segments."""
    SINGLE_EPISODE = "single_episode"
    MULTI_SEGMENT = "multi_segment"
    CROSS_EPISODE = "cross_episode"


# ──────────────────────────────────────────────────────────────────────
# PHASE 0: PARSED SCRIPT ELEMENTS
# ──────────────────────────────────────────────────────────────────────

class ScriptElement(BaseModel):
    """A single parsed element from a screenplay (dialogue, action, camera, sound)."""
    element_type: ElementType
    start_time: str = Field(..., pattern=r"^\d{2}:\d{2}\.\d{2}$", description="Timestamp in MM:SS.ms format")
    end_time: str = Field(..., pattern=r"^\d{2}:\d{2}\.\d{2}$")
    character: Optional[str] = Field(None, description="Speaker name for dialogue elements")
    emotion: Optional[str] = Field(None, description="Emotion label from the script, e.g., 'Heartbroken'")
    content: str = Field(..., min_length=1, description="The dialogue text, action description, or camera direction")
    start_seconds: float = Field(0.0, description="Start time converted to seconds for computation")
    end_seconds: float = Field(0.0, description="End time converted to seconds for computation")

    @field_validator("start_seconds", "end_seconds", mode="before")
    @classmethod
    def compute_seconds(cls, v):
        return v  # Computed by parser, not from user input

    def duration_sec(self) -> float:
        return self.end_seconds - self.start_seconds


class Scene(BaseModel):
    """A single scene within an episode."""
    scene_number: int
    heading: str = Field(..., description="e.g., 'INT. CATHEDRAL – DAY'")
    start_time: str
    end_time: str
    start_seconds: float = 0.0
    end_seconds: float = 0.0
    elements: list[ScriptElement] = Field(default_factory=list)

    def duration_sec(self) -> float:
        return self.end_seconds - self.start_seconds


class ParsedEpisode(BaseModel):
    """Complete structured representation of a parsed episode screenplay."""
    episode_number: int = Field(..., ge=1, le=999)
    episode_id: str = Field(..., description="Original filename identifier")
    logline: str = Field("", description="One-sentence episode summary")
    continuity_notes: str = Field("", description="Continuity context from the script")
    scenes: list[Scene] = Field(default_factory=list)
    total_duration_sec: float = 0.0
    total_elements: int = 0
    total_dialogue_lines: int = 0

    def get_all_elements(self) -> list[ScriptElement]:
        """Flatten all elements across all scenes."""
        elements = []
        for scene in self.scenes:
            elements.extend(scene.elements)
        return elements

    def get_dialogue_elements(self) -> list[ScriptElement]:
        """Get only dialogue elements."""
        return [e for e in self.get_all_elements() if e.element_type == ElementType.DIALOGUE]

    def get_unique_characters(self) -> set[str]:
        """Get set of all speaking characters."""
        return {e.character for e in self.get_dialogue_elements() if e.character}


# ──────────────────────────────────────────────────────────────────────
# PHASE 1: DRAMATIC MOMENTS
# ──────────────────────────────────────────────────────────────────────

class DramaticMoment(BaseModel):
    """A tagged dramatic moment identified within an episode."""
    episode_number: int
    moment_type: MomentType
    intensity_score: int = Field(..., ge=1, le=10, description="1-10 intensity rating")
    start_time: str = Field(..., description="Exact timestamp from the script")
    end_time: str = Field(..., description="Exact timestamp from the script")
    start_seconds: float = 0.0
    end_seconds: float = 0.0
    description: str = Field(..., min_length=10, description="What happens in this moment")
    key_dialogue: Optional[str] = Field(None, description="The most impactful line, if dialogue-based")
    characters_involved: list[str] = Field(default_factory=list)
    emotion_labels: list[str] = Field(default_factory=list, description="Emotion labels from the script")


class EpisodeMoments(BaseModel):
    """All dramatic moments found in a single episode."""
    episode_number: int
    logline: str = ""
    moments: list[DramaticMoment] = Field(default_factory=list)
    total_duration_sec: float = 0.0


# ──────────────────────────────────────────────────────────────────────
# PHASE 2: PROMO CANDIDATES
# ──────────────────────────────────────────────────────────────────────

class PromoSegment(BaseModel):
    """A single continuous segment within a promo."""
    episode: int = Field(..., ge=1)
    start_time: str = Field(..., description="Start timestamp MM:SS.ms")
    end_time: str = Field(..., description="End timestamp MM:SS.ms")
    start_seconds: float = 0.0
    end_seconds: float = 0.0

    def duration_sec(self) -> float:
        return self.end_seconds - self.start_seconds


class PromoCandidate(BaseModel):
    """A complete promo candidate with segments, hook, cliffhanger, and reasoning."""
    promo_id: str = Field(..., description="Unique identifier, e.g., 'PR-001'")
    promo_type: PromoType
    episodes: list[int] = Field(..., min_length=1, description="Episode numbers involved")
    segments: list[PromoSegment] = Field(..., min_length=1, description="Ordered list of segments")
    total_duration_sec: float = Field(0.0, description="Sum of all segment durations")
    hook: str = Field(..., min_length=10, description="What the scroll-stopping hook is in the first 3 seconds")
    cliffhanger: str = Field(..., min_length=10, description="What the unresolved ending is")
    why_it_works: str = Field(..., min_length=20, description="One-line explanation of why this promo would attract viewers")
    characters_in_clip: list[str] = Field(default_factory=list)
    creative_angle: Optional[str] = Field(None, description="Audience angle: betrayal_revenge, protective_romance, etc.")

    @model_validator(mode="after")
    def compute_total_duration(self):
        if self.segments:
            self.total_duration_sec = sum(s.duration_sec() for s in self.segments)
        return self


# ──────────────────────────────────────────────────────────────────────
# PHASE 3: SCORED & RANKED PROMOS
# ──────────────────────────────────────────────────────────────────────

class PromoScores(BaseModel):
    """Individual and composite scores for a promo."""
    hook_strength: int = Field(..., ge=1, le=10)
    cliffhanger_quality: int = Field(..., ge=1, le=10)
    standalone_coherence: int = Field(..., ge=1, le=10)
    emotional_impact: int = Field(..., ge=1, le=10)
    duration_fitness: int = Field(..., ge=1, le=10)
    composite: float = Field(0.0, description="Weighted composite score, computed by code not LLM")

    def compute_composite(self, weights: dict[str, float]) -> float:
        """Deterministic composite calculation — never let the LLM compute this."""
        self.composite = round(
            weights["hook_strength"] * self.hook_strength
            + weights["cliffhanger_quality"] * self.cliffhanger_quality
            + weights["standalone_coherence"] * self.standalone_coherence
            + weights["emotional_impact"] * self.emotional_impact
            + weights["duration_fitness"] * self.duration_fitness,
            2,
        )
        return self.composite


class RankedPromo(BaseModel):
    """A fully scored, ranked promo ready for output."""
    rank: int = Field(..., ge=1)
    promo: PromoCandidate
    scores: PromoScores
    score_reasoning: str = Field("", description="Chain-of-thought scoring rationale from LLM")
    posting_rationale: str = Field("", description="Why this should be posted at this rank position")


# ──────────────────────────────────────────────────────────────────────
# PHASE 4: QUALITY FLAGS & REPORT
# ──────────────────────────────────────────────────────────────────────

class QualityFlag(BaseModel):
    """A specific quality issue found in a promo."""
    promo_id: str
    failure_type: FailureType
    severity: Severity
    explanation: str = Field(..., min_length=10)
    suggested_fix: str = Field("", description="How to improve this promo pick")


class QualityReport(BaseModel):
    """Complete quality analysis report for the pipeline run."""
    total_candidates_generated: int = 0
    total_passed_validation: int = 0
    validation_pass_rate: float = 0.0
    total_flagged: int = 0
    flags: list[QualityFlag] = Field(default_factory=list)
    failure_breakdown: dict[str, int] = Field(default_factory=dict, description="Count by failure type")
    mean_composite_score: float = 0.0
    score_std_dev: float = 0.0
    episode_coverage: int = Field(0, description="How many unique episodes are represented in top promos")
    baseline_comparison: dict[str, float] = Field(
        default_factory=dict,
        description="Average composite scores: our_pipeline vs random vs last_30_sec vs rule_only",
    )
    honest_limitations: list[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────
# COST TRACKING
# ──────────────────────────────────────────────────────────────────────

class CostTracker(BaseModel):
    """Tracks API usage and estimated cost."""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_api_calls: int = 0
    total_retries: int = 0
    estimated_cost_usd: float = 0.0

    def add_usage(self, input_tokens: int, output_tokens: int, cost_in: float, cost_out: float):
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_api_calls += 1
        self.estimated_cost_usd += (input_tokens / 1_000_000 * cost_in) + (output_tokens / 1_000_000 * cost_out)
        self.estimated_cost_usd = round(self.estimated_cost_usd, 6)
