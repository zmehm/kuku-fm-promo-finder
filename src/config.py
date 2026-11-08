"""
config.py — Centralized configuration for the Promo Finder pipeline.

Everything environment-specific (paths, API key, model names, concurrency) can
be overridden with environment variables or a .env file, so the same code runs
locally, on Streamlit Cloud (put keys in st.secrets and copy them into
os.environ), and in a live interview demo.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Paths ────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SCRIPTS_DIR = (
    PROJECT_ROOT.parent
    / "I Married a Broken Soldier. He was a Duke-script"
    / "I Married a Broken Soldier. He was a Duke"
)
SCRIPTS_DIR = Path(os.getenv("SCRIPTS_DIR", str(_DEFAULT_SCRIPTS_DIR)))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(PROJECT_ROOT / "outputs")))
PARSED_DIR = OUTPUT_DIR / "parsed"
MOMENTS_DIR = OUTPUT_DIR / "moments"
PROMOS_DIR = OUTPUT_DIR / "promos"
RANKED_DIR = OUTPUT_DIR / "ranked"
QUALITY_DIR = OUTPUT_DIR / "quality_report"
CACHE_DIR = OUTPUT_DIR / "llm_cache"          # raw LLM responses (resume + audit trail)
HUMAN_EVAL_DIR = OUTPUT_DIR / "human_eval"


def ensure_output_dirs() -> None:
    """Ensure all required output and cache directories exist."""
    for d in (PARSED_DIR, MOMENTS_DIR, PROMOS_DIR, RANKED_DIR, QUALITY_DIR, CACHE_DIR, HUMAN_EVAL_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ── Gemini ───────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
# Scoring/critique can use a different model than generation to reduce
# "the generator grades its own homework" bias. Defaults to the same model.
GEMINI_JUDGE_MODEL = os.getenv("GEMINI_JUDGE_MODEL", GEMINI_MODEL)
GEMINI_TEMPERATURE = 0.3
GEMINI_JUDGE_TEMPERATURE = 0.0
GEMINI_MAX_OUTPUT_TOKENS = 16384
GEMINI_MAX_RETRIES = 10
GEMINI_RETRY_DELAY_SEC = 3.0
GEMINI_CONCURRENCY = int(os.getenv("GEMINI_CONCURRENCY", "1"))

# ── Promo constraints ────────────────────────────────────────────────────
PROMO_MIN_DURATION_SEC = 20.0
PROMO_MAX_DURATION_SEC = 60.0
PROMO_OPTIMAL_MIN_SEC = 25.0
PROMO_OPTIMAL_MAX_SEC = 45.0

HOOK_WINDOW_SEC = 3.0
CLIFFHANGER_WINDOW_SEC = 5.0
MAX_MOMENT_SEC = 30.0            # a single "dramatic moment" beat may not exceed this

# ── Scoring ──────────────────────────────────────────────────────────────
# Weights are product priors (stop-scroll > install intent > cold comprehension),
# NOT fitted to real ad performance data. Treat them as a hypothesis to A/B test.
SCORING_WEIGHTS = {
    "hook_strength": 0.30,
    "cliffhanger_quality": 0.25,
    "standalone_coherence": 0.20,
    "emotional_impact": 0.15,
    "duration_fitness": 0.10,
}
assert abs(sum(SCORING_WEIGHTS.values()) - 1.0) < 1e-6, "Scoring weights must sum to 1.0"

# ── Selection ────────────────────────────────────────────────────────────
MAX_PROMOS_PER_EPISODE = 2
MAX_CHARACTERS_IN_PROMO = 4
PER_EPISODE_CANDIDATES = 3       # promos requested per episode in Pass 2
CROSS_EPISODE_CANDIDATES = 5     # cross-episode flash-forwards requested in Pass 2
TOP_N_FINAL = 15                 # size of the final ranked shortlist
CRITIQUE_TOP_K = 30              # only the top-K scored candidates get the LLM critique
OVERLAP_DROP_RATIO = 0.5         # drop a candidate overlapping a better one by more than this
BASELINE_SEED = 7

HIGH_INTENSITY_EMOTIONS = {
    "tier_1": ["screaming", "hysterical", "furious", "rage", "shrieking", "devastated", "sobbing", "wailing", "explosive", "murderous", "lethal", "violent", "desperate", "agonized"],
    "tier_2": ["shocked", "horrified", "outraged", "betrayed", "heartbroken", "defiant", "fierce", "venomous", "cruel", "mocking", "gloating", "taunting", "scornful", "contemptuous"],
    "tier_3": ["trembling", "choking", "pleading", "begging", "cold", "commanding", "stern", "dismissive", "incredulous", "bitter"],
}

# Matched as whole words / phrases (see quality_checker.validate_ad_policy).
AD_POLICY_HARD = ["hang", "hanging", "hanged", "noose", "gallows", "suicide", "kill myself", "self-harm", "torture"]
AD_POLICY_SOFT = ["poison", "poisoning", "arsenic", "blood", "bleeding", "bleed", "murder", "murdered", "witch", "witchcraft", "torment"]
AD_POLICY_FLAGGED_WORDS = AD_POLICY_HARD + AD_POLICY_SOFT

CREATIVE_ANGLES = {
    "betrayal_revenge": {"keywords": ["betray", "revenge", "lied", "cheated", "steal", "stole", "traitor", "scheme"], "target_audience": "Drama/soap opera viewers"},
    "protective_romance": {"keywords": ["protect", "love", "marry", "husband", "wife", "embrace", "tender", "gentle"], "target_audience": "Romance readers (#BookTok)"},
    "hidden_identity": {"keywords": ["duke", "duchess", "secret", "disguise", "true identity", "heir", "noble", "royal"], "target_audience": "Fantasy/period drama fans"},
    "survival_thriller": {"keywords": ["trial", "dungeon", "chains", "escape", "death", "execution", "prison", "survive"], "target_audience": "Thriller/suspense viewers"},
}

# Update these to the current published price of the model you actually use.
COST_PER_1M_INPUT_TOKENS = 0.10
COST_PER_1M_OUTPUT_TOKENS = 0.40
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
