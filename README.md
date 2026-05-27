# fairscape-grok-grader

**Grok-native AI-Ready rubric grader for FAIRSCAPE RO-Crates.**

This project is the direct Grok / xAI equivalent of the Claude-based grading system in [fairscape/fairscape_grader](https://github.com/fairscape/fairscape_grader).

It lets you score any RO-Crate against the 28-rubric **AI-Ready** checklist (FAIRness, Provenance, Characterization, Pre-model Explainability, Ethics, Sustainability, Computability) using Grok models, with the same strong isolation and reproducibility guarantees as the original.

## Why this exists

The original `fairscape_grader` does something very powerful:

- Deterministic Python extractors pull structured evidence from the crate for each rubric.
- An LLM then scores each rubric **in complete isolation** (one fresh agent per rubric, seeing only that rubric's YAML definition + its evidence).
- The result is auditable, reproducible, and produces concrete "gaps" that tell you exactly how to improve the crate.

This project brings the same workflow to the Grok ecosystem (Grok TUI skills + xAI API) so FAIRSCAPE users who prefer (or are required to use) Grok can get identical value.

## Architecture (Claude → Grok mapping)

| Component                    | Original (Claude)                  | Grok Equivalent                              |
|-----------------------------|------------------------------------|----------------------------------------------|
| Skill definitions           | `.claude/skills/*.md`              | `skills/*.md` (this repo) + `~/.grok/skills/` |
| Parallel isolated scoring   | `spawn_subagent` / sub-agents      | `spawn_subagent` (same pattern, stronger runtime isolation) |
| LLM for scoring             | Anthropic Claude (via pydantic-ai) | xAI Grok via OpenAI-compatible API (`api.x.ai`) |
| Evidence extraction         | `fairscape_wizard.rubric_eval`     | Same (we depend on it)                       |
| Output artifacts            | `grading/*/score.json` + aggregate | **Identical** (drop-in compatible)           |

The isolation contract is preserved exactly: a score for rubric `2.d` depends *only* on `2.d`'s rubric rules + the evidence the Python extractor produced for it.

## Quick Start (inside Grok TUI)

1. Install the Python support package (gives you the CLI + scorer library):

   ```bash
   cd fairscape-grok-grader
   pip install -e ".[full]"
   ```

2. Make sure you have an xAI API key:

   ```bash
   export XAI_API_KEY=...
   ```

3. In any directory with (or next to) an RO-Crate, invoke:

   ```
   /fairscape-grok-ai-ready-grader
   ```

4. Point it at your `ro-crate-metadata.json`. It will:
   - Extract evidence for all 28 rubrics (deterministic)
   - Launch parallel isolated Grok sub-scorers
   - Show you the full breakdown + top actionable gaps

## Quick Start (standalone / CI / notebooks)

```bash
pip install -e ".[full]"

# One-shot (extract + score)
fairscape-grok grade ./path/to/ro-crate-metadata.json ./grading-output/ --model grok-3

# Or step by step
python -m fairscape_wizard.rubric_eval extract-evidence ./ro-crate-metadata.json ./grading/
fairscape-grok score ./grading/ --model grok-3
```

## Project Layout

```
fairscape-grok-grader/
├── pyproject.toml
├── README.md
├── rubrics/
│   └── ai-ready/               # All 28 rubric YAML definitions (sourced from fairscape/fairscape_grader)
│       ├── 0.a-findable.yaml
│       └── ...
├── src/fairscape_grok_grader/
│   ├── __init__.py
│   ├── grok_scorer.py          # Core: calls xAI Grok with structured RubricScore output
│   └── cli.py                  # fairscape-grok CLI
└── skills/
    └── fairscape-grok-ai-ready-grader/
        ├── SKILL.md            # Main entrypoint skill (what you invoke with /...)
        └── grok-rubric-scorer/
            └── SKILL.md        # The isolated single-rubric sub-scorer
```

## The 28 Rubrics (AI-Ready)

The rubrics live in `rubrics/ai-ready/`. They are copied from the upstream `fairscape_grader` project with clear attribution.

Criteria groups:
- **0** FAIRness (Findable, Accessible, Interoperable, Reusable)
- **1** Provenance (Transparent, Traceable, Interpretable, Key Actors)
- **2** Characterization (Semantics, Statistics, Standards, Bias, Data Quality)
- **3** Pre-model Explainability
- **4** Ethics (Acquired, Managed, Disseminated, Secure)
- **5** Sustainability (Persistent, Domain-appropriate, Well-governed, Associated)
- **6** Computability (Standardized, Computationally-accessible, Portable, Contextualized)

Each rubric is a small, self-contained YAML with `intent`, `what_to_look_for`, three literal scoring rules, and an `output_schema`.

## Development

```bash
# Install dev deps
pip install -e ".[dev]"

# Run the CLI directly during development
python -m fairscape_grok_grader.cli grade ...

# The skills are plain markdown. Edit them in place; Grok will pick up changes when you invoke them.
```

## Relationship to Other FAIRSCAPE Projects

- `fairscape-models` — the canonical Pydantic schemas
- `fairscape-cli` — import, schema inference, augment, build datasheet, etc.
- `fairscape-wizard` — the Python evidence extractors + the original Claude rocrate wizard
- `fairscape-grader` (this one's parent) — the original Claude implementation this project ports

This project is intentionally a **thin, focused companion** that adds first-class Grok support without duplicating the hard work of evidence extraction or the rubric definitions themselves.

## License

MIT (same as the parent tooling).

The rubric YAML content is derived from work in the FAIRSCAPE organization.

## Acknowledgments

- The FAIRSCAPE team (especially the original design of the 28-rubric AI-Ready framework and the isolated agentic scoring pattern).
- xAI for building Grok and the excellent agent/subagent tooling that made this port natural.

---

**Status:** v0.1 — Core Grok scorer + CLI + main Grok skills working. Full remote-source wizard flow planned for a future release.

Contributions, issues, and usage reports from the FAIRSCAPE community are very welcome.
