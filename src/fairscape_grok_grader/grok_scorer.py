"""
Grok-native rubric scorer for AI-Ready RO-Crate assessment.

Uses the xAI Grok API (OpenAI-compatible at https://api.x.ai/v1) to score
individual rubrics against deterministically extracted evidence.

The key design principle (inherited from the Claude version) is **isolation**:
each rubric is scored in a fresh context with *only* its rubric.yaml + evidence.json.
This makes scores reproducible and auditable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

# openai is an optional runtime dependency (only needed when actually calling the API)
try:
    from openai import OpenAI  # type: ignore
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore

XAI_API_BASE = "https://api.x.ai/v1"


class RubricScore(BaseModel):
    """Structured output for a single rubric score. Must match rubric output_schema."""

    score: int = Field(..., ge=0, le=2, description="0=Absent, 1=Partial, 2=Substantive")
    rationale: str = Field(..., description="1-3 sentences citing the exact scoring rule that applied and the evidence that decided it")
    evidence: list[str] = Field(default_factory=list, description="Direct @id references or verbatim short strings from the EVIDENCE_JSON that justify the score")
    gaps: list[str] = Field(default_factory=list, description="Specific missing things that would raise the score to the next level; empty when score==2")


SYSTEM_PROMPT = """You are an expert, impartial RO-Crate AI-Readiness rubric grader using Grok.

You score **one rubric at a time** using ONLY the evidence payload provided in the user message.
Follow the rubric's scoring rules **literally** — never average, never hedge, never invent facts.

Key rules:
- Choose exactly one of 0 (Absent), 1 (Partial), or 2 (Substantive) based on which rule's conditions are met by the evidence.
- Quote @id values or short literal fragments from the evidence to justify your choice.
- If score < 2, the `gaps` list must contain concrete, actionable missing items.
- Never cite the rubric YAML itself inside the `evidence` array — `evidence` must come from the provided evidence payload only.
- Be neutral and audit-friendly in the rationale.
"""


def _get_xai_client(api_key: Optional[str] = None):
    if OpenAI is None:
        raise RuntimeError(
            "The 'openai' package is required for scoring. "
            "Install with: pip install 'fairscape-grok-grader[full]' or pip install openai"
        )
    key = api_key or os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY")
    if not key:
        raise RuntimeError(
            "No xAI API key found. Set XAI_API_KEY (or GROK_API_KEY) environment variable, "
            "or pass api_key=... explicitly."
        )
    return OpenAI(api_key=key, base_url=XAI_API_BASE)


def score_rubric_with_grok(
    rubric_yaml: dict,
    evidence_payload: dict,
    model: str = "grok-3",
    api_key: Optional[str] = None,
    temperature: float = 0.0,
) -> RubricScore:
    """
    Score a single rubric using Grok via the xAI API.

    The prompt is deliberately minimal and isolated — only the rubric definition
    and the evidence dict are sent. This matches the reproducibility contract
    used in the original Claude implementation.
    """
    client = _get_xai_client(api_key)

    # Build the exact same style of prompt as the original grade.py / agentic-rescore
    scoring = rubric_yaml["scoring"]
    what = "\n".join(f"- {line.strip()}" for line in rubric_yaml.get("what_to_look_for", []))

    user_prompt = f"""You are grading **one** AI-Ready rubric for an RO-Crate.

Return ONLY a single JSON object matching this exact schema (no markdown, no extra text):

{json.dumps(rubric_yaml.get("output_schema", {}), indent=2)}

================ RUBRIC ================
ID:            {rubric_yaml.get("id")}
Criterion:     {rubric_yaml.get("criterion")}
Sub-criterion: {rubric_yaml.get("sub_criterion")}

INTENT:
{rubric_yaml.get("intent", "").strip()}

WHAT TO LOOK FOR:
{what}

SCORING RULES (apply LITERALLY — pick the single rule whose conditions match the evidence):
  0 — {scoring["0"]["label"]}: {scoring["0"]["rule"].strip()}
  1 — {scoring["1"]["label"]}: {scoring["1"]["rule"].strip()}
  2 — {scoring["2"]["label"]}: {scoring["2"]["rule"].strip()}

================ EVIDENCE (complete factual basis — do not invent fields) ================
{json.dumps(evidence_payload, indent=2, default=str)}

Follow the instructions in the system prompt exactly. Output only the JSON object.
"""

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content.strip()
        data = json.loads(content)
        return RubricScore(**data)
    except Exception as e:
        # Return a graceful failure object so batch scoring can continue
        return RubricScore(
            score=0,
            rationale=f"ERROR during Grok scoring: {type(e).__name__}: {e}",
            evidence=[],
            gaps=["Scoring failed due to API or parsing error — re-run this rubric"],
        )


def score_rubrics_batch(
    rubric_items: list[tuple[dict, dict, str, str]],  # (rubric_yaml, evidence, rubric_id, slug)
    model: str = "grok-3",
    api_key: Optional[str] = None,
    max_workers: int = 4,
) -> list[dict]:
    """
    Score multiple rubrics. Currently sequential (simple + reliable).

    For true parallelism in the Grok TUI environment, prefer launching multiple
    `spawn_subagent` calls from a skill (each subagent calls this function for one rubric).
    That gives stronger isolation than threads.

    Returns list of dicts ready for aggregation: {id, slug, score, rationale, evidence, gaps}
    """
    results = []
    client = _get_xai_client(api_key)  # warm the client once

    for rubric_yaml, evidence, rid, slug in rubric_items:
        score_obj = score_rubric_with_grok(
            rubric_yaml=rubric_yaml,
            evidence_payload=evidence,
            model=model,
            api_key=api_key,
        )
        results.append({
            "id": rid,
            "slug": slug,
            "score": score_obj.score,
            "rationale": score_obj.rationale,
            "evidence": score_obj.evidence,
            "gaps": score_obj.gaps,
        })
        print(f"  [{rid}] {slug}  -> score={score_obj.score}")
    return results


# Convenience helper for the common "read files from disk" case used by skills
def score_rubric_from_files(
    rubric_path: Path | str,
    evidence_path: Path | str,
    output_path: Optional[Path | str] = None,
    model: str = "grok-3",
    api_key: Optional[str] = None,
) -> RubricScore:
    """Read rubric.yaml + evidence.json from disk, score, optionally write score.json."""
    import yaml

    rubric_yaml = yaml.safe_load(Path(rubric_path).read_text())
    evidence = json.loads(Path(evidence_path).read_text())

    score = score_rubric_with_grok(rubric_yaml, evidence, model=model, api_key=api_key)

    if output_path:
        Path(output_path).write_text(json.dumps(score.model_dump(), indent=2) + "\n")
    return score
