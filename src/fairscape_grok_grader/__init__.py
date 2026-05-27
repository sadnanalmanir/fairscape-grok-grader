"""
fairscape-grok-grader

Grok-native AI-Ready rubric grader for FAIRSCAPE RO-Crates.

Replaces the Claude-based agentic scoring in fairscape/fairscape_grader
with xAI Grok models + Grok skills / subagents for isolated, reproducible scoring.
"""

__version__ = "0.1.0"

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
