"""
Command-line interface for fairscape-grok-grader.

Usage examples:

    # Extract evidence (requires fairscape-wizard installed)
    fairscape-grok extract-evidence ./ro-crate-metadata.json ./grading/

    # Score using Grok (standalone, after evidence exists)
    fairscape-grok score ./grading/ --model grok-3

    # Full one-shot (extract + score)
    fairscape-grok grade ./ro-crate-metadata.json ./output-grading/ --xai-key $XAI_API_KEY
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

import yaml
from rich.console import Console
from rich.table import Table

from .grok_scorer import (
    RubricScore,
    XAI_API_BASE,
    score_rubric_from_files,
    score_rubric_with_grok,
)

console = Console()


def cmd_score(args: argparse.Namespace) -> int:
    """Score all (or filtered) rubrics that have evidence/ already extracted."""
    grading_dir = Path(args.grading_dir).resolve()
    if not grading_dir.exists():
        console.print(f"[red]Grading directory not found:[/red] {grading_dir}")
        return 1

    api_key = args.xai_key or os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY")
    if not api_key:
        console.print("[red]No xAI API key. Set XAI_API_KEY or pass --xai-key.[/red]")
        return 1

    model = args.model or "grok-3"

    rubric_dirs = sorted([d for d in grading_dir.iterdir() if d.is_dir() and "-" in d.name])

    if args.criterion:
        rubric_dirs = [d for d in rubric_dirs if d.name.startswith(f"{args.criterion}.")]

    console.print(f"[bold]Scoring {len(rubric_dirs)} rubrics with[/bold] [cyan]{model}[/cyan] via {XAI_API_BASE}")

    results = []
    for d in rubric_dirs:
        rid, _, slug = d.name.partition("-")
        rubric_path = d / "rubric.yaml"
        evidence_path = d / "evidence.json"
        if not rubric_path.exists() or not evidence_path.exists():
            console.print(f"[yellow]Skipping[/yellow] {d.name} (missing rubric or evidence)")
            continue

        score = score_rubric_from_files(
            rubric_path, evidence_path, output_path=d / "score.json", model=model, api_key=api_key
        )
        results.append({"id": rid, "slug": slug, **score.model_dump()})
        console.print(f"  [{rid}] {slug} → [bold]{score.score}[/bold]")

    # Write aggregate
    aggregate = _build_aggregate(results, model)
    (grading_dir / "aggregated_score.json").write_text(json.dumps(aggregate, indent=2) + "\n")

    _print_aggregate_summary(aggregate)
    return 0


def cmd_grade(args: argparse.Namespace) -> int:
    """One-shot: extract evidence (if possible) then score everything."""
    crate_path = Path(args.crate).resolve()
    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Try to use fairscape-wizard for extraction if available
    try:
        from fairscape_wizard.rubric_eval import cmd_extract_evidence  # type: ignore

        console.print("[green]Using fairscape-wizard for deterministic evidence extraction[/green]")
        cmd_extract_evidence(crate_path, out_dir)
    except ImportError:
        console.print(
            "[yellow]fairscape-wizard not installed.[/yellow] "
            "Evidence extraction skipped. Run with the full extra or use the Claude/Grok hybrid flow."
        )
        console.print("You can still manually place rubric.yaml + evidence.json under the output dir.")
        # Create minimal structure so the user can proceed manually if they want
        (out_dir / "NOTE.txt").write_text(
            "Run `python -m fairscape_wizard.rubric_eval extract-evidence <crate> <this-dir>` "
            "or install fairscape-grok-grader[full] to get automatic extraction.\n"
        )

    # Now score
    sys.argv = ["", "score", str(out_dir), "--model", args.model or "grok-3"]
    if args.xai_key:
        sys.argv.extend(["--xai-key", args.xai_key])
    return cmd_score(argparse.Namespace(grading_dir=out_dir, model=args.model, xai_key=args.xai_key, criterion=None))


def _build_aggregate(per_rubric: list[dict], model: str) -> dict:
    from collections import defaultdict

    CRITERION_NAMES = {
        "0": "FAIRness",
        "1": "Provenance",
        "2": "Characterization",
        "3": "Pre-model Explainability",
        "4": "Ethics",
        "5": "Sustainability",
        "6": "Computability",
    }

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

    pct = round(100 * total / max_total, 1) if max_total else 0.0
    return {
        "model": model,
        "total_score": total,
        "max_score": max_total,
        "percentage": pct,
        "counts": counts,
        "criteria": criteria,
    }


def _print_aggregate_summary(agg: dict) -> None:
    console.print()
    console.rule("[bold]AI-Ready Score Summary[/bold]")
    console.print(f"Total: [bold green]{agg['total_score']}/{agg['max_score']}[/bold green]  = [bold]{agg['percentage']}%[/bold]")
    console.print(f"Substantive: {agg['counts']['substantive']}  |  Partial: {agg['counts']['partial']}  |  Absent: {agg['counts']['absent']}  |  Error: {agg['counts']['error']}")

    table = Table(title="By Criterion")
    table.add_column("Criterion", style="cyan")
    table.add_column("Score", justify="right")
    table.add_column("Max", justify="right")
    table.add_column("%", justify="right")

    for c in agg["criteria"]:
        pct = round(100 * c["score"] / c["max"], 1) if c["max"] else 0
        table.add_row(c["name"], str(c["score"]), str(c["max"]), f"{pct}%")

    console.print(table)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fairscape-grok",
        description="Grok-powered AI-Ready rubric grader for FAIRSCAPE RO-Crates",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    # score subcommand
    p_score = sub.add_parser("score", help="Score rubrics from a pre-existing evidence directory")
    p_score.add_argument("grading_dir", help="Directory containing <id>-<slug>/rubric.yaml + evidence.json")
    p_score.add_argument("--model", default="grok-3", help="Grok model name (default: grok-3)")
    p_score.add_argument("--xai-key", help="xAI API key (falls back to XAI_API_KEY / GROK_API_KEY env)")
    p_score.add_argument("--criterion", help="Only score one criterion (0-6)")

    # grade subcommand (extract + score)
    p_grade = sub.add_parser("grade", help="Extract evidence + score in one shot (requires fairscape-wizard)")
    p_grade.add_argument("crate", help="Path to ro-crate-metadata.json")
    p_grade.add_argument("output_dir", help="Where to write grading/ output")
    p_grade.add_argument("--model", default="grok-3")
    p_grade.add_argument("--xai-key")

    # extract-evidence passthrough (if fairscape-wizard present)
    p_ext = sub.add_parser("extract-evidence", help="Passthrough to fairscape_wizard.rubric_eval extract-evidence")
    p_ext.add_argument("crate_path")
    p_ext.add_argument("out_dir")

    args = ap.parse_args(argv)

    if args.cmd == "score":
        return cmd_score(args)
    if args.cmd == "grade":
        return cmd_grade(args)
    if args.cmd == "extract-evidence":
        try:
            from fairscape_wizard.rubric_eval import cmd_extract_evidence

            return cmd_extract_evidence(Path(args.crate_path), Path(args.out_dir))
        except ImportError:
            console.print("[red]fairscape-wizard is not installed. Install with: pip install fairscape-grok-grader[full][/red]")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
