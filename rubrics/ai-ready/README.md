# AI-Ready Rubrics (28)

These 28 rubric definitions are the canonical AI-Readiness checklist used by FAIRSCAPE for scoring RO-Crates.

**Source:** Originally developed in https://github.com/fairscape/fairscape_grader (the Claude-based reference implementation). They are included here with attribution for use with Grok-native tooling.

## Structure of each rubric

Every file `<id>-<slug>.yaml` contains:

- `id`, `criterion`, `sub_criterion`
- `intent` — what good looks like at a high level
- `what_to_look_for` — concrete signals the grader should examine
- `scoring` — three literal rules for score 0 (Absent), 1 (Partial), 2 (Substantive)
- `output_schema` — the exact JSON shape the LLM must return
- `extractor_inputs` — (informational) what the deterministic Python extractor will produce for this rubric

## The Seven Criteria

| Prefix | Name                          | # Rubrics | Focus |
|--------|-------------------------------|-----------|-------|
| 0      | FAIRness                      | 4         | Classic FAIR + discovery |
| 1      | Provenance                    | 4         | Transparent, traceable, interpretable, actors |
| 2      | Characterization              | 5         | Semantics, stats, standards, bias, quality |
| 3      | Pre-model Explainability      | 3         | Documentation, fitness, verifiability |
| 4      | Ethics                        | 4         | Acquisition, management, dissemination, security |
| 5      | Sustainability                | 4         | Persistence, domain fit, governance, association |
| 6      | Computability                 | 4         | Standardization, accessibility, portability, context |

Total possible score: 56 (2 × 28).

## How scoring works in this Grok port

1. Deterministic extraction (from `fairscape-wizard`) produces one `evidence.json` per rubric.
2. Grok (via `fairscape_grok_grader.grok_scorer`) is given the rubric YAML + that evidence **in complete isolation**.
3. Grok must pick exactly one of the three rules and return a `RubricScore` with rationale + verbatim evidence citations + gaps.
4. Aggregation happens in pure Python (no LLM).

This two-phase design (deterministic facts + isolated LLM judgment) is the key intellectual contribution of the original system and is fully preserved here.

## Reference Implementation

The files `extract.py`, `grade.py`, and `rubric_eval.py` in this directory are copies of the upstream logic (for documentation and to aid future independent maintenance). The live extraction used by the CLI and skills still comes from the installed `fairscape-wizard` package when available.

## Editing Rubrics

If you improve a rubric definition, please contribute the change back upstream to the main `fairscape/fairscape_grader` repository so the entire ecosystem stays in sync.
