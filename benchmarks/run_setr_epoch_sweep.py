"""Predeclared 3/5-epoch extension; MultiHopRAG reuse is exploratory only.

Train from the same frozen base (not an extra 3/5 epochs on the 1-epoch
adapter). Choose among 1/3/5 solely on source teacher-dev loss BEFORE external
generation. Report all fixed endpoints, never pick a winner from test scores.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys


def sha(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def choose_by_source_dev(manifests):
    for epoch, manifest in manifests.items():
        if not manifest.get("fresh_reload_verified") or manifest["epochs"] != epoch:
            raise ValueError("unverified or mismatched checkpoint")
        if not math.isfinite(manifest["final_teacher_dev_loss"]):
            raise ValueError("nonfinite source dev loss")
    return min(manifests, key=lambda e: (manifests[e]["final_teacher_dev_loss"], e))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir", type=Path, required=True)
    a = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    if root.name != "setr-qwen32b-20261110":
        raise ValueError("run only in dedicated experiment runtime")
    out = a.output_dir.resolve()
    if root / "output" not in out.parents:
        raise ValueError("output must be a new experiment subdirectory")
    out.mkdir(parents=True, exist_ok=False)
    source = root / "input/source-train-dev-v1.json"
    teacher = root / "output/teacher-qwen35-27b-full-v1.jsonl"
    inputs = root / "input/multihoprag-model-inputs-v1.json"
    base = root / "models/Qwen3-0.6B"
    base_manifest = root / "Qwen3-0.6B-manifest.json"
    old_adapter = root / "output/student-adapter-qwen35-v1"
    paths = [source, teacher, inputs, base_manifest, old_adapter / "manifest.json",
             root / "output/generation-results-v1.json",
             root / "output/qwen-generation-base-v1.jsonl", root / "output/setr-style-lora-v1.jsonl",
             *sorted((root / "benchmarks").glob("*setr*.py"))]
    frozen = {str(path): sha(path) for path in paths}
    old = json.loads((old_adapter / "manifest.json").read_bytes())
    if sha(source) != old["source_dataset_sha256"] or sha(base_manifest) != old["base_manifest_sha256"]:
        raise ValueError("source/base differs from epoch-1 baseline")
    if sha(teacher) not in old["teacher_sha256"].values():
        raise ValueError("teacher differs from epoch-1 baseline")
    if not json.loads((root / "output/teacher-qwen35-27b-full-v1-audit.json").read_bytes())["passed"]:
        raise ValueError("teacher structural gate failed")
    plan = {"kind": "exploratory epoch extension after viewing MultiHopRAG v1",
            "epochs": [1, 3, 5], "seed": 20261110, "lr": 1e-4, "accumulation": 8,
            "training": "3 and 5 epochs each from frozen base, not continued from adapter",
            "checkpoint_rule": "lowest final source teacher-dev loss; tie chooses fewer epochs; before external predictions",
            "external_rule": "report all fixed endpoints, no external-based checkpoint selection",
            "generation": {"max_new_tokens": 1536, "batch_size": 1, "mode": "selection", "do_sample": False},
            "production_writes": False, "frozen_sha256": frozen}
    (out / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    env = {**os.environ, "TOKENIZERS_PARALLELISM": "false", "HF_HUB_OFFLINE": "1",
           "TRANSFORMERS_OFFLINE": "1", "PYTHONUNBUFFERED": "1",
           "HF_HOME": str(root / "cache/huggingface"), "TRITON_CACHE_DIR": str(root / "cache/triton")}

    def run(script, args, gpu, log):
        with log.open("x", encoding="utf-8") as f:
            subprocess.run([sys.executable, str(root / "benchmarks" / script), *map(str, args)],
                           cwd=root, env={**env, "CUDA_VISIBLE_DEVICES": str(gpu)},
                           stdout=f, stderr=subprocess.STDOUT, check=True)

    manifests = {1: old}
    for epoch in (3, 5):
        run("train_setr_adapter.py", ["--dataset", source, "--teacher", teacher,
            "--base-model", base, "--model-manifest", base_manifest,
            "--external-test", inputs, "--output", out / f"adapter-epoch{epoch}",
            "--epochs", epoch, "--lr", "0.0001", "--accumulation", 8, "--max-length", 10240],
            0, out / f"train-epoch{epoch}.log")
        manifests[epoch] = json.loads((out / f"adapter-epoch{epoch}/manifest.json").read_bytes())
        print(json.dumps({"trained_epochs": epoch, "dev_loss": manifests[epoch]["final_teacher_dev_loss"]}), flush=True)
    selected = choose_by_source_dev(manifests)
    selection = {"selected_epochs": selected, "rule": plan["checkpoint_rule"],
                 "teacher_dev_losses": {e: m["final_teacher_dev_loss"] for e, m in manifests.items()},
                 "not_independent_test": True}
    (out / "source-dev-selection.json").write_text(json.dumps(selection, indent=2), encoding="utf-8")
    print(json.dumps(selection), flush=True)
    for path, expected in frozen.items():
        if sha(path) != expected:
            raise ValueError(f"frozen input changed: {path}")

    def generate(epoch, gpu):
        run("run_setr_generation.py", ["--dataset", inputs, "--output", out / f"epoch{epoch}.jsonl",
            "--model", base, "--model-manifest", base_manifest,
            "--adapter", out / f"adapter-epoch{epoch}", "--mode", "selection",
            "--split", "external_test_only", "--shard", 0, "--shards", 1,
            "--batch-size", 1, "--max-new-tokens", 1536], gpu, out / f"generate-epoch{epoch}.log")

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(generate, e, g) for e, g in [(3, 0), (5, 1)]]
        for job in jobs:
            job.result()
    args = ["--dataset", root / "input/multihoprag-bm25-v1.json", "--output", out / "results.json",
            "--prediction-arm", f"qwen_generation_base={root / 'output/qwen-generation-base-v1.jsonl'}",
            "--prediction-arm", f"epoch1={root / 'output/setr-style-lora-v1.jsonl'}"]
    for e in (3, 5):
        args += ["--prediction-arm", f"epoch{e}={out / f'epoch{e}.jsonl'}"]
    run("evaluate_setr.py", args, 0, out / "evaluate.log")
    print(json.dumps({"completed": True, "results": str(out / "results.json"),
                      "selected_by_source_dev": selected, "exploratory_only": True}), flush=True)


if __name__ == "__main__":
    main()
