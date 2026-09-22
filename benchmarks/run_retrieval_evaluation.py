"""Run the versioned local benchmark without any answer-generation API calls."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from research_assistant.retrieval.knowledge_base import LiteratureIndex
from research_assistant.evaluation.retrieval_evaluation import EvaluationStore, run_evaluation


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=ROOT / "benchmarks/multimodal_eval_v1.json")
    parser.add_argument("--fail-on-regression", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    index = LiteratureIndex(ROOT / "library", backend="multimodal")
    run = run_evaluation(ROOT, index, dataset, lambda done, total: print(f"{done}/{total}", flush=True))
    store = EvaluationStore(ROOT / ".data/evaluations.sqlite3")
    store.save(run)
    output = ROOT / "benchmarks" / f"retrieval_run_{run['run_id']}.json"
    output.write_text(json.dumps(store.get(run["run_id"]), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run_id": run["run_id"], "summary": run["summary"],
                      "policy_comparison": run["policy_comparison"]}, ensure_ascii=False, indent=2))
    if args.fail_on_regression and run["policy_comparison"]["regressions"]:
        sys.exit(2)
