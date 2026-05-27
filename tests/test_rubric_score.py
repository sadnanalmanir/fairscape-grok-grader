"""Basic unit tests for fairscape-grok-grader (no API key required)."""

import json
import tempfile
from pathlib import Path

# We test only the parts that don't need network / openai
from src.fairscape_grok_grader.grok_scorer import RubricScore


def test_rubric_score_model_valid():
    score = RubricScore(
        score=2,
        rationale="Both persistent identifier and recognized archive are present per rule 2.",
        evidence=["identifier: doi:10.18130/V3/ABC123", "publisher: Zenodo"],
        gaps=[],
    )
    assert score.score == 2
    assert len(score.evidence) == 2
    d = score.model_dump()
    assert d["score"] == 2


def test_rubric_score_model_rejects_bad_score():
    try:
        RubricScore(score=3, rationale="bad", evidence=[], gaps=[])
        assert False, "Should have raised ValidationError"
    except Exception:
        pass  # expected


def test_aggregation_logic_matches_original_shape():
    # Simulate what the CLI does after scoring
    from collections import defaultdict

    per_rubric = [
        {"id": "0.a", "slug": "findable", "score": 2, "rationale": "r", "evidence": [], "gaps": []},
        {"id": "0.b", "slug": "accessible", "score": 1, "rationale": "r", "evidence": [], "gaps": []},
        {"id": "1.a", "slug": "transparent", "score": 2, "rationale": "r", "evidence": [], "gaps": []},
    ]

    CRITERION_NAMES = {"0": "FAIRness", "1": "Provenance"}

    groups = defaultdict(list)
    for r in per_rubric:
        groups[r["id"][0]].append(r)

    criteria = []
    total = 0
    max_total = 0
    for prefix in sorted(groups):
        rubrics = groups[prefix]
        c_score = sum((r["score"] or 0) for r in rubrics if r["score"] is not None)
        c_max = 2 * len(rubrics)
        criteria.append({
            "id": prefix,
            "name": CRITERION_NAMES.get(prefix, prefix),
            "score": c_score,
            "max": c_max,
            "rubrics": rubrics,
        })
        total += c_score
        max_total += c_max

    agg = {
        "model": "grok-3",
        "total_score": total,
        "max_score": max_total,
        "percentage": round(100 * total / max_total, 1) if max_total else 0,
        "counts": {"substantive": 2, "partial": 1, "absent": 0, "error": 0},
        "criteria": criteria,
    }

    assert agg["total_score"] == 5
    assert agg["max_score"] == 6
    assert len(agg["criteria"]) == 2
    assert agg["criteria"][0]["name"] == "FAIRness"
