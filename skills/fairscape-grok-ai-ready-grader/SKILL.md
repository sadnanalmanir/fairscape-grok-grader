---
name: fairscape-grok-ai-ready-grader
description: Score a FAIRSCAPE RO-Crate against the 28 AI-Ready rubrics using Grok models and isolated subagents. Deterministic evidence extraction (via fairscape-wizard) + agentic scoring with strong isolation guarantees for reproducibility. This is the Grok-native replacement for the Claude-based agentic-rescore + parts of the remote wizard.
---

# fairscape-grok-ai-ready-grader

**Grok-native AI-Ready rubric scorer for FAIRSCAPE RO-Crates.**

This skill replaces the Claude Code `agentic-rescore` workflow. It produces identical artifacts (`grading/<id>-<slug>/score.json` + `aggregated_score.json`) so existing downstream tools and the original `fairscape_grader` ecosystem continue to work.

## Core Principles (same as the Claude version)

1. **Isolation for reproducibility** — Each rubric is scored by a fresh subagent that sees *only* its own `rubric.yaml` + `evidence.json`. No conversation history, no paper, no prior decisions leak into the score.
2. **Deterministic evidence first** — Python extractors (from `fairscape-wizard`) do the factual heavy lifting. Grok only interprets the pre-extracted evidence against the written rules.
3. **0/1/2 literal scoring** — Never average. Follow the exact rule text in each rubric YAML.
4. **Actionable gaps** — Every score < 2 must list concrete things that would raise it.

## Prerequisites

- Python ≥ 3.10
- `fairscape-wizard` installed (provides the 28 extractors): `pip install fairscape-wizard`
- xAI API key in `XAI_API_KEY` (or `GROK_API_KEY`)
- A `ro-crate-metadata.json` (can be from `fairscape-cli import`, the remote wizard, or any valid crate)

Quick check:
```bash
python -c "from fairscape_wizard.rubric_eval import cmd_extract_evidence; print('OK')"
echo $XAI_API_KEY | head -c 8
```

## Usage

From inside a directory containing (or next to) your RO-Crate:

```
/fairscape-grok-ai-ready-grader
```

The skill will:

1. Ask for (or auto-detect) the path to `ro-crate-metadata.json`.
2. Run deterministic evidence extraction into `./grading/`.
3. Offer "all 28" or "one criterion (0-6)".
4. Launch **parallel isolated subagents** (one per rubric) using `spawn_subagent`.
5. Each subagent reads only its two files, calls Grok with strict isolation, and writes `score.json`.
6. Aggregate and show beautiful summary + top gaps.
7. Save state to `.fairscape-grok-grading-state.json` (resume-friendly).

## How the parallel subagent scoring works (implementation detail)

When you choose to score, this skill will output a block like:

> Dispatching 28 isolated sub-scorers...

Then it will make multiple `spawn_subagent` calls (in one logical step when possible) with prompts that contain **only** the three absolute paths:

```
RUBRIC_YAML: /abs/path/to/grading/0.a-findable/rubric.yaml
EVIDENCE_JSON: /abs/path/to/grading/0.a-findable/evidence.json
OUTPUT:        /abs/path/to/grading/0.a-findable/score.json
```

Each subagent runs the equivalent of:

```python
from fairscape_grok_grader.grok_scorer import score_rubric_from_files
score_rubric_from_files(rubric, evidence, output, model="grok-3")
```

and replies with a one-line status: `0.a → 2`

This is the direct Grok equivalent of the Claude `Agent(subagent_type=general-purpose)` fan-out pattern used in the original `agentic-rescore` skill.

## Resume / Partial Runs

- If `./grading/` already contains some `score.json` files, the skill will only dispatch the missing ones.
- You can delete specific `score.json` files and re-invoke to force a re-score of just those rubrics (great for testing prompt/model changes).
- State file (`.fairscape-grok-grading-state.json`) records completed rubrics and the last aggregate.

## Output Layout (identical to original)

```
your-crate/
└── grading/
    ├── summary.json
    ├── aggregated_score.json
    ├── 0.a-findable/
    │   ├── rubric.yaml
    │   ├── evidence.json
    │   └── score.json          # written by Grok subagent
    ├── 1.b-traceable/
    └── ...
```

`aggregated_score.json` has the same shape as the original `fairscape_grader`, grouped by criterion (0–6).

## Using the standalone Python CLI (outside the Grok TUI)

```bash
# After evidence is extracted
pip install -e .
fairscape-grok score ./grading/ --model grok-3

# Or the one-shot (if fairscape-wizard is present)
fairscape-grok grade /path/to/ro-crate-metadata.json ./my-grading/ --model grok-3
```

The Python path also supports any Grok model name your xAI key has access to (`grok-3`, `grok-3-mini`, future reasoning models, etc.).

## Model Recommendations

- `grok-3` — current strong default for careful rubric following
- `grok-3-mini` (or whatever the fast variant is) — for cheaper bulk re-scores during development

Set temperature=0.0 (the library does this).

## Differences from / Improvements over the Claude Version

- Uses Grok's tool use + JSON mode for structured `RubricScore` output.
- Subagent isolation is enforced by the Grok runtime's `spawn_subagent` mechanism (even stronger guarantees than conversation-based subagents in some hosts).
- The Python `fairscape_grok_grader.grok_scorer` module can be used completely standalone or from other agents.
- Easier to run the pure scoring loop from CI or notebooks (just an API key + the evidence dir).

## Out of Scope (v0.2)

- Full 6-phase remote-source wizard (import → schema → enrich → provenance → grade → improve). The grading core is the priority.
- Re-implementing all 28 complex extractors (we depend on `fairscape-wizard` for now; this is the correct factoring).

## Contributing / Roadmap

See the project README. The long-term goal is a complete Grok-native companion to the FAIRSCAPE tooling that can be invoked entirely from the Grok TUI without ever touching Claude Code.

---

**Invoke this skill with `/fairscape-grok-ai-ready-grader`**

After it finishes you will have a complete, auditable, reproducible AI-Ready assessment powered by Grok.
