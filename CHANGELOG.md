# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-05-27

### Added
- Automatic loading of `XAI_API_KEY` (and `GROK_API_KEY`) from `.env` files using `python-dotenv`
- `.env.example` template file for easy setup
- Improved error messages and CLI help text documenting all supported API key configuration methods
- Updated Quick Start section in README with `.env` instructions

### Changed
- `python-dotenv` is now a core (required) dependency
- Bumped version to `0.2.0` to reflect the new configuration feature
- Updated skill documentation references from v0.1 to v0.2

## [0.1.0] - 2026-05-26

### Added
- Initial public release of `fairscape-grok-grader`
- Grok-native port of the AI-Ready RO-Crate rubric grader from `fairscape/fairscape_grader`
- Full Python package (`fairscape_grok_grader`) with:
  - `grok_scorer.py` — Core logic for scoring rubrics using xAI Grok models (OpenAI-compatible API)
  - CLI (`fairscape-grok`) with `score`, `grade`, and `extract-evidence` commands
- All 28 AI-Ready rubric YAML definitions (with attribution to the upstream project)
- Grok skills for the TUI / CLI environment:
  - `fairscape-grok-ai-ready-grader` (main entrypoint skill)
  - `grok-rubric-scorer` (isolated subagent skill for reproducible per-rubric scoring)
- `install-skills.sh` helper to install skills into `~/.grok/skills/`
- GitHub Actions CI workflow (linting with Ruff + testing with pytest across Python 3.10–3.12)
- Comprehensive README with architecture explanation (Claude → Grok mapping), usage examples, and isolation guarantees
- Unit tests for core models and aggregation logic
- Proper packaging (`pyproject.toml`, MIT license, `.gitignore`)

### Notes
- This release provides functional parity with the agentic scoring core of the original Claude-based grader, adapted for Grok models and the Grok TUI skill/agent system.
- Emphasis on reproducibility through isolated subagent scoring (one fresh context per rubric).
- Output artifacts (`grading/*/score.json` + `aggregated_score.json`) are drop-in compatible with downstream tooling from the FAIRSCAPE ecosystem.

[0.2.0]: https://github.com/sadnanalmanir/fairscape-grok-grader/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/sadnanalmanir/fairscape-grok-grader/releases/tag/v0.1.0
