"""Bounded offline experiment runner. Launch only in the isolated runtime copy.

Existing Qwen3.5-27B teacher is sharded over GPUs 0/1; Qwen reranking uses GPU 2;
student selection uses GPU 0/1. No production edits.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import argparse


ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def run(script, args, *, gpu, log, python=None):
    env = {**os.environ, "CUDA_VISIBLE_DEVICES": str(gpu), "TOKENIZERS_PARALLELISM": "false",
           "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
           "HF_HOME": str(ROOT / "cache/huggingface"), "TRITON_CACHE_DIR": str(ROOT / "cache/triton")}
    with (ROOT / log).open("x", encoding="utf-8") as f:
        subprocess.run([str(python or PY), str(ROOT / "benchmarks" / script), *map(str, args)], cwd=ROOT,
                       env=env, stdout=f, stderr=subprocess.STDOUT, check=True)


def generate(model, manifest, output, *, gpu, shard, shards, dataset, mode, adapter=None, external=False,
             batch_size=1, max_new_tokens=None, python=None):
    args = ["--dataset", dataset, "--output", output, "--model", model, "--model-manifest", manifest,
            "--mode", mode, "--shard", shard, "--shards", shards, "--batch-size", batch_size,
            "--max-new-tokens", max_new_tokens if max_new_tokens is not None else (1536 if mode == "iri" else 256)]
    if adapter:
        args += ["--adapter", adapter]
    if mode == "iri":
        args += ["--max-gpu-memory-gib", 40]
    if external:
        args += ["--split", "external_test_only"]
    run("run_setr_generation.py", args, gpu=gpu, log=output + ".log", python=python)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="output", help="fresh experiment output directory")
    parser.add_argument("--teacher-model", default="models/Qwen3.5-27B")
    parser.add_argument("--teacher-manifest", default="Qwen3.5-27B-manifest.json")
    parser.add_argument("--teacher-python", type=Path, required=True, help="isolated Qwen3.5-compatible interpreter; student stays in selector env")
    args = parser.parse_args()
    if ROOT.name not in {"setr-20261110", "setr-qwen32b-20261110"}:
        raise ValueError("must run in dedicated SetR runtime, never production root")
    out = ROOT / args.output_dir
    out.mkdir(exist_ok=True)
    marker = out / "experiment-start.json"
    if marker.exists():
        raise FileExistsError(f"experiment output already exists: {out}")
    plan = {"method": "SetR-style adaptation; not original 8B/GPT-4o reproduction",
            "teacher": {"model": args.teacher_model, "manifest": args.teacher_manifest,
                        "python": str(args.teacher_python), "mode": "iri", "max_new_tokens": 4096,
                        "do_sample": False, "enable_thinking": False, "gpus": [0, 1]},
            "student": "Qwen3-0.6B selection-only, LoRA q/v r8 alpha16, one epoch lr1e-4 accumulation8",
            "source": "512 train/128 dev from official parsed Top20, query/passage-component isolation",
            "test": "all MultiHopRAG, BM25 Top20 frozen before training",
            "arms": ["bm25_top6", "qwen_top6", "qwen_generation_base", "setr_style_lora"],
            "checkpoint": "fixed final checkpoint; no tuning on external test",
            "failure": "no truncation, no repair, no fallback; failed output has zero coverage, not null success",
            "primary": "macro IC@1/3/5/6 on 2255 answerable, 301 null separately",
            "files": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in [*sorted((ROOT / "benchmarks").glob("*.py")), *sorted((ROOT / "input").glob("*.json"))]}}
    with marker.open("x", encoding="utf-8") as f:
        json.dump(plan, f, indent=2)
    teacher = args.teacher_model
    student = "models/Qwen3-0.6B"
    teacher_manifest = args.teacher_manifest
    student_manifest = "Qwen3-0.6B-manifest.json"
    source = "input/source-train-dev-v1.json"
    external = "input/multihoprag-bm25-v1.json"
    model_external = "input/multihoprag-model-inputs-v1.json"
    prefix = args.output_dir.rstrip("/")
    with ThreadPoolExecutor(max_workers=3) as pool:
        reranker = pool.submit(run, "evaluate_setr.py", ["--dataset", external, "--output", f"{prefix}/qwen-baseline-v1.json",
                               "--base-model", "reranker", "--model-manifest", "reranker-manifest.json"],
                               gpu=2, log=f"{prefix}/qwen-baseline.log")
        # The BF16 teacher needs both cards. A single deterministic process is
        # safer than two processes competing for the same sharded weights.
        generate(teacher, teacher_manifest, f"{prefix}/teacher.jsonl",
                 gpu="0,1", shard=0, shards=1, dataset=source, mode="iri",
                 batch_size=1, max_new_tokens=4096, python=args.teacher_python)
        run("audit_setr_teacher.py", ["--dataset", source, "--teacher", f"{prefix}/teacher.jsonl",
                                     "--output", f"{prefix}/teacher-structural-audit.json",
                                     "--min-valid-rate", "0.95"], gpu=0, log=f"{prefix}/teacher-audit.log")
        print("Teacher generation and structural gate complete; training fixed student", flush=True)
        run("train_setr_adapter.py", ["--dataset", source, "--teacher", f"{prefix}/teacher.jsonl",
                                    "--base-model", student,
                                    "--model-manifest", student_manifest, "--external-test", model_external,
                                    "--output", f"{prefix}/student-adapter"], gpu=0, log=f"{prefix}/train.log")
        print("Training complete; starting frozen external evaluation", flush=True)
        for arm, adapter in [("qwen_generation_base", None), ("setr_style_lora", "output/student-adapter")]:
            jobs = [pool.submit(generate, student, student_manifest, f"{prefix}/{arm}-{s}.jsonl",
                                gpu=s, shard=s, shards=2, dataset=model_external, mode="selection",
                                adapter=f"{prefix}/student-adapter" if adapter else None,
                                external=True, batch_size=1) for s in range(2)]
            for future in jobs:
                future.result()
        reranker.result()
    eval_args = ["--dataset", external, "--output", f"{prefix}/generation-results-v1.json"]
    for arm in ("qwen_generation_base", "setr_style_lora"):
        for s in range(2):
            eval_args += ["--prediction-arm", f"{arm}={prefix}/{arm}-{s}.jsonl"]
    run("evaluate_setr.py", eval_args, gpu=0, log=f"{prefix}/generation-evaluation.log")
    print(f"Experiment finished: {prefix}/qwen-baseline-v1.json and {prefix}/generation-results-v1.json", flush=True)


if __name__ == "__main__":
    main()
