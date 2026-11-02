"""Export human-approved retrieval cases as a clean, reproducible dataset."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research_assistant.evaluation.retrieval_evaluation import EvaluationStore


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export only human-approved cases; AI review is never promoted to gold."
    )
    parser.add_argument("--root", type=Path, required=True, help="Project root containing .data/")
    parser.add_argument("--run-id", required=True, help="Evaluation run to export")
    parser.add_argument("--output", type=Path, required=True, help="Destination JSON file")
    args = parser.parse_args()
    dataset = EvaluationStore(args.root / ".data/evaluations.sqlite3").export_approved_dataset(args.run_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "cases": len(dataset["cases"]),
                      "source_run_id": dataset["source_run_id"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
