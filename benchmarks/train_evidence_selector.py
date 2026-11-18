"""Offline LoRA selector training; no production service changes or generation.

--smoke is CPU plumbing only. --synthetic-smoke uses the pinned Qwen weights
and a real adapter but cannot establish scientific retrieval effectiveness.
"""
import argparse
import gc
import hashlib
import importlib.metadata
import json
from pathlib import Path
import random
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_assistant.retrieval.selector_dataset import MODEL, REVISION, format_prompt, load_dataset


def training_rows(dataset, *, synthetic_smoke=False):
    status = dataset["provenance"]["label_status"]
    if status == "development_anchor_proxy":
        raise ValueError("stage-1 anchor proxies are development-only; formal training refused")
    if (status == "synthetic") != synthetic_smoke:
        raise ValueError("synthetic data requires --synthetic-smoke; real training needs reviewed labels")
    rows = [r for r in dataset["examples"] if r["split"] == "train"]
    if {r["label"] for r in rows} != {0, 1}:
        raise ValueError("train requires both positive and negative incremental examples")
    return rows


def bounded_encode(tokenizer, prompts, max_length):
    encoded = tokenizer(prompts, add_special_tokens=False, padding=False, truncation=False)
    if any(len(ids) > max_length for ids in encoded["input_ids"]):
        raise ValueError("token budget exceeded; no conditions or negations were truncated")
    return tokenizer.pad(encoded, padding=True, return_tensors="pt")


def parameter_hash(model):
    sha = hashlib.sha256()
    for name, p in model.named_parameters():
        if p.requires_grad:
            sha.update(name.encode())
            sha.update(p.detach().float().cpu().contiguous().numpy().tobytes())
    return sha.hexdigest()


def cpu_smoke(args, dataset):
    import torch
    from torch import nn
    if dataset["provenance"]["label_status"] != "synthetic":
        raise ValueError("CPU smoke only accepts invented synthetic data")
    rows = training_rows(dataset, synthetic_smoke=True)
    torch.manual_seed(args.seed)
    model = nn.Sequential(nn.Linear(3, 8), nn.Tanh(), nn.Linear(8, 1))
    before = parameter_hash(model)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate)
    for row in rows[:args.max_steps]:
        features = torch.tensor([[len(format_prompt(row)) / 2000, len(row["selected"]) / 6,
                                  len(row["candidate"]["quote"]) / 2000]])
        loss = nn.functional.binary_cross_entropy_with_logits(model(features), torch.tensor([[float(row["label"])]]))
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
    after = parameter_hash(model)
    if before == after:
        raise RuntimeError("parameters did not change")
    torch.save(model.state_dict(), args.output / "cpu-smoke.pt")
    reloaded = nn.Sequential(nn.Linear(3, 8), nn.Tanh(), nn.Linear(8, 1))
    reloaded.load_state_dict(torch.load(args.output / "cpu-smoke.pt", weights_only=True))
    return {"mode": "cpu_plumbing_not_lora", "initial_parameter_sha256": before,
            "final_parameter_sha256": after, "reload_verified": parameter_hash(reloaded) == after}


def train(args, dataset):
    import torch
    from peft import LoraConfig, TaskType, get_peft_model, PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    rows = training_rows(dataset, synthetic_smoke=args.synthetic_smoke)
    # Local weights only, pinned via deployment manifest; never resolve mutable main.
    manifest = json.loads(args.model_manifest.read_text(encoding="utf-8"))
    path = args.base_model.resolve()
    if (manifest.get("repo") != MODEL or manifest.get("revision") != REVISION or
            Path(manifest.get("path", "")).resolve() != path):
        raise ValueError("base model manifest identity/path mismatch")
    if not torch.cuda.is_available():
        raise ValueError("Qwen training requires CUDA; use --smoke for CPU plumbing")
    tokenizer = AutoTokenizer.from_pretrained(path, padding_side="left", local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    token_ids = [tokenizer.convert_tokens_to_ids(t) for t in ("no", "yes")]
    if (len(set(token_ids)) != 2 or any(type(t) is not int or t == tokenizer.unk_token_id for t in token_ids)):
        raise ValueError("invalid no/yes token IDs")
    # Exclude whole over-budget examples, never silently truncate qualifiers.
    skipped = {"train": 0, "dev": 0}
    def eligible(items, split):
        result = []
        for row in items:
            ids = tokenizer(format_prompt(row), add_special_tokens=False, truncation=False).input_ids
            if len(ids) > args.max_length:
                skipped[split] += 1
            else:
                result.append(row)
        return result
    rows = eligible(rows, "train")
    dev_rows = eligible([r for r in dataset["examples"] if r["split"] == "dev"], "dev")
    random.Random(args.seed).shuffle(dev_rows)
    dev_rows = dev_rows[:args.validation_examples]
    if {r["label"] for r in rows} != {0, 1} or not dev_rows:
        raise ValueError("insufficient eligible training/development examples")
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    rng = random.Random(args.seed)
    base = AutoModelForCausalLM.from_pretrained(path, torch_dtype=torch.bfloat16,
                                               local_files_only=True, attn_implementation="sdpa").cuda()
    base.config.use_cache = False
    model = get_peft_model(base, LoraConfig(task_type=TaskType.CAUSAL_LM, r=args.lora_rank,
        lora_alpha=args.lora_alpha, lora_dropout=args.lora_dropout, target_modules=["q_proj", "v_proj"]))
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    def validate():
        model.eval()
        losses, tp, tn, positives, negatives = [], 0, 0, 0, 0
        with torch.inference_mode():
            for row in dev_rows:
                inputs = bounded_encode(tokenizer, [format_prompt(row)], args.max_length).to("cuda")
                logits = model(**inputs, logits_to_keep=1).logits[:, -1, token_ids].float()
                target = torch.tensor([row["label"]], device="cuda")
                losses.append(float(torch.nn.functional.cross_entropy(logits, target).cpu()))
                pred = int(logits.argmax(-1).item())
                positives += row["label"] == 1
                negatives += row["label"] == 0
                tp += pred == row["label"] == 1
                tn += pred == row["label"] == 0
        return {"loss": sum(losses) / len(losses), "positive_recall": tp / positives if positives else None,
                "negative_recall": tn / negatives if negatives else None,
                "balanced_accuracy": ((tp / positives + tn / negatives) / 2) if positives and negatives else None,
                "examples": len(dev_rows)}
    before_dev = validate()
    print(json.dumps({"event": "pretrain_dev", "metrics": before_dev, "eligible_train": len(rows),
                      "skipped_over_budget": skipped}), flush=True)
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    before = parameter_hash(model)
    optimizer = torch.optim.AdamW((p for p in model.parameters() if p.requires_grad), lr=args.learning_rate)
    steps, losses, start = 0, [], time.monotonic()
    torch.cuda.reset_peak_memory_stats()
    for _epoch in range(args.epochs):
        rng.shuffle(rows)
        for index in range(0, len(rows), args.batch_size):
            if steps >= args.max_steps or time.monotonic() - start >= args.max_seconds:
                break
            batch = rows[index:index + args.batch_size]
            encoded = bounded_encode(tokenizer, [format_prompt(r) for r in batch], args.max_length).to("cuda")
            model.train()
            logits = model(**encoded, logits_to_keep=1).logits[:, -1, token_ids].float()
            labels = torch.tensor([r["label"] for r in batch], device="cuda")
            loss = torch.nn.functional.cross_entropy(logits, labels)
            if not torch.isfinite(loss):
                raise ValueError("non-finite loss")
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_((p for p in model.parameters() if p.requires_grad), 1.0)
            optimizer.step()
            steps += 1
            losses.append(float(loss.detach().cpu()))
            if steps % 50 == 0:
                print(json.dumps({"step": steps, "recent_mean_loss": sum(losses[-50:]) / len(losses[-50:]),
                                  "seconds": round(time.monotonic() - start, 1)}), flush=True)
        if steps >= args.max_steps or time.monotonic() - start >= args.max_seconds:
            break
    after = parameter_hash(model)
    if not steps or before == after:
        raise ValueError("no verified adapter parameter update")
    after_dev = validate()
    model.eval()
    probe = bounded_encode(tokenizer, [format_prompt(rows[0])], args.max_length).to("cuda")
    with torch.inference_mode():
        expected = model(**probe, logits_to_keep=1).logits[:, -1, token_ids].float().cpu()
    model.save_pretrained(args.output)
    tokenizer.save_pretrained(args.output)
    # Fresh base, not unload/re-wrap: PEFT leaves configuration attributes on
    # unloaded models. A fresh load also rules out accidental in-memory reuse.
    del model, optimizer, base, loss, logits, encoded
    gc.collect()
    torch.cuda.empty_cache()
    base = AutoModelForCausalLM.from_pretrained(path, torch_dtype=torch.bfloat16,
                                               local_files_only=True, attn_implementation="sdpa").cuda()
    reloaded = PeftModel.from_pretrained(base, args.output).eval()
    with torch.inference_mode():
        actual = reloaded(**probe, logits_to_keep=1).logits[:, -1, token_ids].float().cpu()
    reload_verified = torch.allclose(expected, actual, atol=1e-4, rtol=1e-4)
    if not reload_verified:
        raise ValueError("saved adapter predictions failed reload verification")
    return {"mode": "qwen_lora_synthetic_smoke" if args.synthetic_smoke else "qwen_lora",
            "base_model": MODEL, "revision": REVISION, "trainable_parameters": trainable,
            "initial_parameter_sha256": before, "final_parameter_sha256": after,
            "reload_verified": reload_verified, "reload_from_fresh_base": True, "steps": steps,
            "first_loss": losses[0], "last_loss": losses[-1],
            "pretrain_dev": before_dev, "posttrain_dev": after_dev,
            "eligible_train_examples": len(rows), "skipped_over_budget": skipped,
            "training_complete_epoch": steps * args.batch_size >= len(rows),
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
            "gpu": torch.cuda.get_device_name(), "no_token_truncation": True,
            "versions": {p: importlib.metadata.version(p) for p in ("torch", "transformers", "peft", "accelerate")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-model", type=Path)
    parser.add_argument("--model-manifest", type=Path)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--max-length", type=int, default=4096)
    parser.add_argument("--max-steps", type=int, default=100)
    parser.add_argument("--max-seconds", type=int, default=900)
    parser.add_argument("--validation-examples", type=int, default=256)
    parser.add_argument("--lora-rank", type=int, default=8)
    parser.add_argument("--lora-alpha", type=int, default=16)
    parser.add_argument("--lora-dropout", type=float, default=.05)
    parser.add_argument("--seed", type=int, default=20261110)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--smoke", action="store_true")
    group.add_argument("--synthetic-smoke", action="store_true")
    args = parser.parse_args()
    if any(getattr(args, k) <= 0 for k in ("epochs", "batch_size", "max_length", "max_steps", "max_seconds", "validation_examples", "lora_rank", "lora_alpha")):
        parser.error("training budgets must be positive")
    if not 0 < args.learning_rate < 1 or not 0 <= args.lora_dropout < 1:
        parser.error("invalid learning rate or dropout")
    if not args.smoke and (args.base_model is None or args.model_manifest is None):
        parser.error("LoRA requires explicit local --base-model and --model-manifest")
    dataset = load_dataset(args.dataset)
    if not args.smoke:
        training_rows(dataset, synthetic_smoke=args.synthetic_smoke)
    args.output.mkdir(parents=True, exist_ok=False)
    result = (cpu_smoke if args.smoke else train)(args, dataset)
    result.update(dataset_id=dataset["id"], dataset_sha256=hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
                  seed=args.seed, split="train", effectiveness_claim=False,
                  production_writes=False, settings={k: v for k, v in vars(args).items()
                      if isinstance(v, (int, float, bool))})
    (args.output / "manifest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
