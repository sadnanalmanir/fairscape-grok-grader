"""
grade.py — full RO-Crate AI-Ready scoring pipeline.

Loads an RO-Crate, runs all 28 RubricExtractor classes from
fairscape-agent/rubrics/ai-ready/scorer/extract.py, then for each rubric
asks an LLM (via pydantic-ai) to apply the rubric's 0/1/2 scoring rules
to the extracted evidence. Writes a per-rubric folder containing
rubric.yaml + evidence.json + score.json, plus a top-level
aggregated_score.json grouped by criterion.

Run:

    python fairscape-agent/rubrics/ai-ready/grade.py \\
        <ro-crate-metadata.json> <output-dir> \\
        --model anthropic:claude-opus-4-7 \\
        --api-key <key>

--model is a pydantic-ai model string. Supported provider prefixes:
anthropic, openai, google-gla, google, groq. The --api-key value is
written to the matching env var (ANTHROPIC_API_KEY, OPENAI_API_KEY,
GOOGLE_API_KEY, GROQ_API_KEY) before the Agent is instantiated.

The `uvarc` prefix routes to the UVA Research Computing GenAI
OpenAI-compatible endpoint (bypasses pydantic-ai; uses urllib +
RubricScore validation directly). Example:

    --model "uvarc:Kimi K2.5" --api-key $UVARC_GenAI_API

Calls are made sequentially — 28 round-trips per run. Failed rubrics
get score: null and an error string in score.json; in the aggregate
they contribute 0 to the subscore but their max still counts toward
the criterion total.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_ai import Agent

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
MODELS_DIR = REPO_ROOT.parent / "fairscape_models"
RUBRIC_SRC_DIR = HERE

for p in (HERE, MODELS_DIR):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

from extract import ALL_EXTRACTORS, ExtractContext, ReleaseBundle, root_summary  # noqa: E402


CRITERION_NAMES = {
    "0": "FAIRness",
    "1": "Provenance",
    "2": "Characterization",
    "3": "Pre-model Explainability",
    "4": "Ethics",
    "5": "Sustainability",
    "6": "Computability",
}

PROVIDER_ENV_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google-gla": "GOOGLE_API_KEY",
    "google": "GOOGLE_API_KEY",
    "groq": "GROQ_API_KEY",
}

UVARC_PROVIDER = "uvarc"
UVARC_BASE_URL = "https://open-webui.rc.virginia.edu/api/chat/completions"

BASE_SYSTEM_PROMPT = (
    "You are an RO-Crate AI-Readiness rubric grader. You score one rubric at a "
    "time using only the evidence payload provided. Follow the rubric's "
    "scoring rules literally — choose 0 (Absent), 1 (Partial), or 2 "
    "(Substantive) based solely on the rule that matches the evidence. Quote "
    "@id refs or short text fragments from the evidence to justify the score, "
    "and list specific gaps that would raise the score (empty if score is 2)."
)

PROMPT_TEMPLATE = """\
You are grading a single rubric for an RO-Crate AI-Readiness assessment.
Return a JSON object matching the rubric's output_schema — your reply will be
validated against the RubricScore model.

================ RUBRIC ================
ID:            {rubric_id}
Criterion:     {criterion}
Sub-criterion: {sub_criterion}

INTENT:
{intent}

WHAT TO LOOK FOR:
{what_to_look_for}

SCORING RULES (apply LITERALLY — pick the single rule that matches the evidence):
  0 — {label_0}: {rule_0}
  1 — {label_1}: {rule_1}
  2 — {label_2}: {rule_2}

OUTPUT SCHEMA (your response must conform):
{output_schema_json}

================ EVIDENCE ================
The evidence below was deterministically extracted from the crate. Treat it as
the complete factual basis for your decision — do not invent or assume fields
that are not present.

{evidence_json}

================ INSTRUCTIONS ================
1. Decide which scoring rule (0, 1, or 2) matches the evidence above.
2. Write a 1-3 sentence rationale that cites the specific rule that applied
   and points at the evidence fields that decided it.
3. Populate `evidence` with direct @id refs or short string fragments from the
   payload above (no fabrication — only strings actually present).
4. Populate `gaps` with what is missing that would raise the score; leave it
   empty if score == 2.
"""


class RubricScore(BaseModel):
    score: int = Field(..., ge=0, le=2)
    rationale: str
    evidence: list[str] = []
    gaps: list[str] = []


def _setup_api_key(model: str, api_key: str) -> None:
    if ":" not in model:
        raise SystemExit(
            f"--model must be 'provider:name' (got {model!r}); "
            f"e.g. anthropic:claude-opus-4-7"
        )
    prefix = model.split(":", 1)[0]
    if prefix == UVARC_PROVIDER:
        return
    env_var = PROVIDER_ENV_MAP.get(prefix)
    if env_var is None:
        supported = sorted([*PROVIDER_ENV_MAP, UVARC_PROVIDER])
        raise SystemExit(
            f"unknown provider prefix {prefix!r}; supported: {supported}"
        )
    os.environ[env_var] = api_key


class UVARCClient:
    """pydantic-ai Agent stand-in for the UVA RC GenAI OpenAI-compatible endpoint.

    Exposes a `run_sync(prompt)` method that returns an object with `.output`
    populated by a RubricScore instance, so grade.py's scoring loop can treat
    it identically to a real pydantic-ai Agent.
    """

    def __init__(self, model_name: str, api_key: str, system_prompt: str):
        self.model_name = model_name
        self.api_key = api_key
        self.system_prompt = system_prompt

    def run_sync(self, prompt: str):
        body = json.dumps(
            {
                "model": self.model_name,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {
                        "role": "user",
                        "content": (
                            prompt
                            + "\n\nReturn ONLY a single JSON object matching the "
                            "output schema above. No markdown fences, no prose "
                            "before or after."
                        ),
                    },
                ],
                "temperature": 0,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            UVARC_BASE_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
            if content.endswith("```"):
                content = content[: -3]
            content = content.strip()
            if content.startswith("json"):
                content = content[4:].lstrip()
        data = json.loads(content)
        return SimpleNamespace(output=RubricScore(**data))


def _load_rubric_yaml(rubric_id: str, rubric_slug: str) -> dict:
    path = RUBRIC_SRC_DIR / f"{rubric_id}-{rubric_slug}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"rubric YAML missing: {path}")
    with path.open() as f:
        return yaml.safe_load(f)


def _build_prompt(rubric_yaml: dict, evidence_payload: dict) -> str:
    scoring = rubric_yaml["scoring"]
    what_to_look_for = "\n".join(
        f"- {line.strip()}" for line in rubric_yaml.get("what_to_look_for", [])
    )
    return PROMPT_TEMPLATE.format(
        rubric_id=rubric_yaml["id"],
        criterion=rubric_yaml.get("criterion", ""),
        sub_criterion=rubric_yaml.get("sub_criterion", ""),
        intent=str(rubric_yaml.get("intent", "")).strip(),
        what_to_look_for=what_to_look_for,
        label_0=scoring["0"]["label"],
        rule_0=str(scoring["0"]["rule"]).strip(),
        label_1=scoring["1"]["label"],
        rule_1=str(scoring["1"]["rule"]).strip(),
        label_2=scoring["2"]["label"],
        rule_2=str(scoring["2"]["rule"]).strip(),
        output_schema_json=json.dumps(rubric_yaml["output_schema"], indent=2),
        evidence_json=json.dumps(evidence_payload, indent=2, default=str),
    )


def _score_one(
    agent: Agent, prompt: str
) -> tuple[Optional[RubricScore], Optional[str]]:
    try:
        result = agent.run_sync(prompt)
        return result.output, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _aggregate(per_rubric: list[dict], model: str, target: Path) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in per_rubric:
        groups[r["id"][0]].append(r)

    criteria = []
    total = 0
    max_total = 0
    counts = {"substantive": 0, "partial": 0, "absent": 0, "error": 0}

    for prefix in sorted(groups):
        rubrics = groups[prefix]
        c_score = sum((r["score"] or 0) for r in rubrics if r["score"] is not None)
        c_max = 2 * len(rubrics)
        for r in rubrics:
            s = r["score"]
            if s == 2:
                counts["substantive"] += 1
            elif s == 1:
                counts["partial"] += 1
            elif s == 0:
                counts["absent"] += 1
            else:
                counts["error"] += 1
        criteria.append(
            {
                "id": prefix,
                "name": CRITERION_NAMES.get(prefix, f"Unknown ({prefix})"),
                "score": c_score,
                "max": c_max,
                "rubrics": rubrics,
            }
        )
        total += c_score
        max_total += c_max

    percentage = round(100 * total / max_total, 1) if max_total else 0.0
    return {
        "model": model,
        "target": str(target),
        "total_score": total,
        "max_score": max_total,
        "percentage": percentage,
        "counts": counts,
        "criteria": criteria,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Score an RO-Crate against the 28 AI-Ready rubrics using an LLM.",
    )
    ap.add_argument("crate_path", type=Path, help="path to ro-crate-metadata.json")
    ap.add_argument("output_dir", type=Path, help="output directory (created if missing)")
    ap.add_argument(
        "--model",
        required=True,
        help="pydantic-ai model string, e.g. anthropic:claude-opus-4-7",
    )
    ap.add_argument(
        "--api-key",
        required=True,
        help="API key for the provider; set as the matching env var for this run",
    )
    ap.add_argument(
        "--system-prompt-extra",
        default="",
        help="optional extra text appended to the base system prompt",
    )
    args = ap.parse_args()

    if not args.crate_path.exists():
        raise SystemExit(f"crate not found: {args.crate_path}")

    _setup_api_key(args.model, args.api_key)

    print(f"[grade] loading {args.crate_path}")
    bundle = ReleaseBundle.load(args.crate_path)
    print(
        f"[grade] loaded {len(bundle.entities)} entities "
        f"({len(bundle.sub_crates)} sub-crates)"
    )

    ctx = ExtractContext(bundle)
    print(
        f"[grade] dataset={ctx.dataset_count}  software={ctx.software_count}  "
        f"computation={ctx.computation_count}  experiment={ctx.experiment_count}  "
        f"schema={ctx.schema_count}"
    )

    rubrics_dir = args.output_dir / "rubrics"
    rubrics_dir.mkdir(parents=True, exist_ok=True)

    system_prompt = BASE_SYSTEM_PROMPT
    if args.system_prompt_extra:
        system_prompt = f"{system_prompt}\n\n{args.system_prompt_extra}"

    print(f"[grade] using model {args.model}")
    prefix, _, model_name = args.model.partition(":")
    if prefix == UVARC_PROVIDER:
        agent = UVARCClient(model_name, args.api_key, system_prompt)
    else:
        agent = Agent(
            args.model,
            output_type=RubricScore,
            system_prompt=system_prompt,
            model_settings={"temperature": 0},
        )

    per_rubric: list[dict] = []

    for cls in ALL_EXTRACTORS:
        slug_dir = rubrics_dir / f"{cls.rubric_id}-{cls.rubric_slug}"
        slug_dir.mkdir(parents=True, exist_ok=True)

        src_yaml = RUBRIC_SRC_DIR / f"{cls.rubric_id}-{cls.rubric_slug}.yaml"
        if not src_yaml.exists():
            raise FileNotFoundError(f"rubric YAML missing: {src_yaml}")
        shutil.copy(src_yaml, slug_dir / "rubric.yaml")

        evidence_payload = cls().extract(ctx)
        (slug_dir / "evidence.json").write_text(
            json.dumps(evidence_payload, indent=2, sort_keys=True, default=str) + "\n"
        )

        rubric_yaml = _load_rubric_yaml(cls.rubric_id, cls.rubric_slug)
        prompt = _build_prompt(rubric_yaml, evidence_payload)
        score, err = _score_one(agent, prompt)

        if score is not None:
            score_dict = score.model_dump()
        else:
            score_dict = {
                "score": None,
                "rationale": None,
                "evidence": [],
                "gaps": [],
                "error": err,
            }
        (slug_dir / "score.json").write_text(json.dumps(score_dict, indent=2) + "\n")

        per_rubric.append(
            {**score_dict, "id": cls.rubric_id, "slug": cls.rubric_slug}
        )
        print(f"  [{cls.rubric_id}] {cls.rubric_slug}  -> score={score_dict.get('score')}")

    summary = {
        "target": str(args.crate_path),
        "root_summary": root_summary(bundle),
        "stats": ctx.stats,
        "rubric_ids": [c.rubric_id for c in ALL_EXTRACTORS],
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n"
    )

    aggregate = _aggregate(per_rubric, args.model, args.crate_path)
    (args.output_dir / "aggregated_score.json").write_text(
        json.dumps(aggregate, indent=2, default=str) + "\n"
    )

    print(
        f"[grade] total {aggregate['total_score']}/{aggregate['max_score']} "
        f"= {aggregate['percentage']}%  "
        f"(substantive={aggregate['counts']['substantive']}, "
        f"partial={aggregate['counts']['partial']}, "
        f"absent={aggregate['counts']['absent']}, "
        f"error={aggregate['counts']['error']})"
    )
    print(f"[grade] wrote {args.output_dir / 'aggregated_score.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
