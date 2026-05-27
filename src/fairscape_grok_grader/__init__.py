"""
fairscape-grok-grader

Grok-native AI-Ready rubric grader for FAIRSCAPE RO-Crates.

Replaces the Claude-based agentic scoring in fairscape/fairscape_grader
with xAI Grok models + Grok skills / subagents for isolated, reproducible scoring.
"""

__version__ = "0.2.0"

# Automatically load variables from .env file in the current directory
# (or parent directories). This allows users to keep XAI_API_KEY in a .env
# file instead of (or in addition to) environment variables.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    # python-dotenv is optional at runtime for non-scoring use cases
    pass

from .grok_scorer import (
    RubricScore,
    score_rubric_with_grok,
    score_rubrics_batch,
    XAI_API_BASE,
)

__all__ = [
    "RubricScore",
    "score_rubric_with_grok",
    "score_rubrics_batch",
    "XAI_API_BASE",
]
