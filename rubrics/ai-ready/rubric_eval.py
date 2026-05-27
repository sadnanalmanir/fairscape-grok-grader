"""Evidence dump and score aggregation for agentic RO-Crate rubric scoring.

This is a thin wrapper around ``fairscape-agent/rubrics/ai-ready/extract.py``
that splits the grading flow into two halves:

* ``extract-evidence`` — load a crate, run all 28 ``RubricExtractor`` classes,
  write ``<out_dir>/<rubric_id>-<slug>/{rubric.yaml,evidence.json}``. This is
  the deterministic half — no LLM involved.

* ``aggregate`` — scan ``<out_dir>/*/score.json`` (written by the wizard one
  rubric at a time, in-conversation) and emit ``<out_dir>/aggregated_score.json``
  with totals grouped by criterion (id[0]). Match the shape that
  ``grade.py:_aggregate`` produces so downstream tooling can consume either.

The agentic scoring itself lives in the ``agentic-rescore`` SKILL — Claude reads
``rubric.yaml`` + ``evidence.json`` and writes ``score.json`` per rubric. We never
import or call ``grade.py``.

CLI:
    python -m fairscape_wizard.rubric_eval extract-evidence <crate.json> <out_dir>
    python -m fairscape_wizard.rubric_eval aggregate <out_dir>
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve()
AGENT_ROOT = HERE.parents[2]                      # .../fairscape-agent
RUBRIC_SRC_DIR = AGENT_ROOT / "rubrics" / "ai-ready"

if str(RUBRIC_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(RUBRIC_SRC_DIR))

# fairscape_models lives as a sibling repo when not pip-installed.
_MODELS_DIR = AGENT_ROOT.parent / "fairscape_models"
if _MODELS_DIR.exists() and str(_MODELS_DIR) not in sys.path:
    sys.path.insert(0, str(_MODELS_DIR))

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


def cmd_extract_evidence(crate_path: Path, out_dir: Path) -> int:
    if not crate_path.exists():
        raise SystemExit(f"crate not found: {crate_path}")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[rubric_eval] loading {crate_path}", file=sys.stderr)
    bundle = ReleaseBundle.load(crate_path)
    print(
        f"[rubric_eval] loaded {len(bundle.entities)} entities "
        f"({len(bundle.sub_crates)} sub-crates)",
        file=sys.stderr,
    )

    ctx = ExtractContext(bundle)

    summary = {
        "target": str(crate_path),
        "root_summary": root_summary(bundle),
        "stats": ctx.stats,
        "rubric_ids": [c.rubric_id for c in ALL_EXTRACTORS],
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n"
    )

    for cls in ALL_EXTRACTORS:
        slug_dir = out_dir / f"{cls.rubric_id}-{cls.rubric_slug}"
        slug_dir.mkdir(parents=True, exist_ok=True)

        src_yaml = RUBRIC_SRC_DIR / f"{cls.rubric_id}-{cls.rubric_slug}.yaml"
        if not src_yaml.exists():
            raise SystemExit(f"rubric YAML missing: {src_yaml}")
        shutil.copy(src_yaml, slug_dir / "rubric.yaml")

        evidence = cls().extract(ctx)
        (slug_dir / "evidence.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n"
        )
        print(f"  [{cls.rubric_id}] {cls.rubric_slug}", file=sys.stderr)

    print(json.dumps({"out_dir": str(out_dir), "rubrics": len(ALL_EXTRACTORS)}))
    return 0


def cmd_aggregate(out_dir: Path, model: str = "agentic:claude-code") -> int:
    if not out_dir.exists():
        raise SystemExit(f"output dir not found: {out_dir}")

    per_rubric: list[dict] = []
    for slug_dir in sorted(out_dir.iterdir()):
        if not slug_dir.is_dir():
            continue
        score_path = slug_dir / "score.json"
        if not score_path.exists():
            continue
        rubric_yaml = slug_dir / "rubric.yaml"
        rubric_id, _, slug = slug_dir.name.partition("-")
        score = json.loads(score_path.read_text())
        per_rubric.append({
            "id": rubric_id,
            "slug": slug,
            "score": score.get("score"),
            "rationale": score.get("rationale"),
            "evidence": score.get("evidence", []),
            "gaps": score.get("gaps", []),
            "error": score.get("error"),
            "rubric_yaml_path": str(rubric_yaml) if rubric_yaml.exists() else None,
        })

    if not per_rubric:
        raise SystemExit(f"no score.json files found under {out_dir}")

    aggregate = _aggregate(per_rubric, model)
    aggregate_path = out_dir / "aggregated_score.json"
    aggregate_path.write_text(json.dumps(aggregate, indent=2, default=str) + "\n")

    counts = aggregate["counts"]
    print(
        f"[rubric_eval] {aggregate['total_score']}/{aggregate['max_score']} "
        f"= {aggregate['percentage']}%  (substantive={counts['substantive']}, "
        f"partial={counts['partial']}, absent={counts['absent']}, "
        f"error={counts['error']})",
        file=sys.stderr,
    )
    print(json.dumps({
        "aggregated_score_path": str(aggregate_path),
        "total_score": aggregate["total_score"],
        "max_score": aggregate["max_score"],
        "percentage": aggregate["percentage"],
        "rubrics_scored": len(per_rubric),
    }))
    return 0


def _aggregate(per_rubric: list[dict], model: str) -> dict:
    """Mirror of ``grade.py:_aggregate`` — group by ``id[0]``, sum scores,
    compute percentage, count outcomes. Kept in sync with grade.py.
    """
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
        criteria.append({
            "id": prefix,
            "name": CRITERION_NAMES.get(prefix, f"Unknown ({prefix})"),
            "score": c_score,
            "max": c_max,
            "rubrics": rubrics,
        })
        total += c_score
        max_total += c_max

    percentage = round(100 * total / max_total, 1) if max_total else 0.0
    return {
        "model": model,
        "total_score": total,
        "max_score": max_total,
        "percentage": percentage,
        "counts": counts,
        "criteria": criteria,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Evidence dump + score aggregation for agentic RO-Crate grading.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    ee = sub.add_parser("extract-evidence", help="Run extractors and dump evidence.json + rubric.yaml per rubric.")
    ee.add_argument("crate_path", type=Path)
    ee.add_argument("out_dir", type=Path)

    ag = sub.add_parser("aggregate", help="Aggregate per-rubric score.json files into aggregated_score.json.")
    ag.add_argument("out_dir", type=Path)
    ag.add_argument("--model", default="agentic:claude-code")

    args = ap.parse_args(argv)

    if args.cmd == "extract-evidence":
        return cmd_extract_evidence(args.crate_path, args.out_dir)
    if args.cmd == "aggregate":
        return cmd_aggregate(args.out_dir, args.model)
    return 2


if __name__ == "__main__":
    sys.exit(main())
