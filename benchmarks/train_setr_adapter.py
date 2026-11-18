"""Fixed-budget selection-only LoRA distilled from whole-pool teacher output.

Not original SetR IRI training: no inherited RankZephyr response, no external
test gold, no test-based checkpoint selection. One final checkpoint only.
"""
import argparse
from collections import Counter
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import random
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.setr_protocol import messages, normalize
from benchmarks.evaluate_setr import read_predictions


def selection_target(indices):
    return "### Final Selection: " + (" ".join(f"[{i + 1}]" for i in indices) or "[]")


def training_examples(groups, predictions):
    result = []
    for group in groups:
        pred = predictions[group["id"]]
        if pred["status"] == "ok":
            result.append({"id": group["id"], "split": group["split"],
                           "messages": messages(group["question"], group["candidates"], "selection"),
                           "target": selection_target(pred["selected_indices"])})
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--teacher", type=Path, action="append", required=True)
    p.add_argument("--base-model", type=Path, required=True)
    p.add_argument("--model-manifest", type=Path, required=True)
    p.add_argument("--external-test", type=Path, required=True, help="query-only overlap guard; gold never read")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--max-length", type=int, default=10240)
    p.add_argument("--epochs", type=int, default=1)
    p.add_argument("--accumulation", type=int, default=8)
    p.add_argument("--lr", type=float, default=0.0001)
    a = p.parse_args()
    if a.epochs < 1 or a.accumulation < 1 or a.max_length < 1 or a.lr <= 0:
        p.error("epochs, accumulation, max-length and lr must be positive")
    if a.output.exists():
        raise FileExistsError(a.output)
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import LoraConfig, get_peft_model, PeftModel
    random.seed(20261110)
    torch.manual_seed(20261110)
    data = json.loads(a.dataset.read_bytes())
    if data.get("schema") != "setr-style-source-v1" or data.get("role") == "external_test_only":
        raise ValueError("training requires separately frozen source")
    external = {normalize(g["question"]) for g in json.loads(a.external_test.read_bytes())["groups"]}
    if any(normalize(g["question"]) in external for g in data["groups"]):
        raise ValueError("source/external question overlap")
    predictions = read_predictions(a.teacher, data["groups"])
    examples = training_examples(data["groups"], predictions)
    model_manifest = json.loads(a.model_manifest.read_bytes())
    for name, expected in model_manifest["files"].items():
        if hashlib.sha256((a.base_model / name).read_bytes()).hexdigest() != expected:
            raise ValueError("base fingerprint mismatch")
    tok = AutoTokenizer.from_pretrained(a.base_model, local_files_only=True)
    encoded, skipped = [], Counter()
    for row in examples:
        prompt = tok.apply_chat_template(row["messages"], tokenize=False, add_generation_prompt=True, enable_thinking=False)
        prefix = tok.encode(prompt, add_special_tokens=False)
        target = tok.encode(row["target"] + tok.eos_token, add_special_tokens=False)
        if len(prefix) + len(target) > a.max_length:
            skipped[row["split"]] += 1
            continue
        encoded.append({**row, "prefix": prefix, "target_ids": target})
    train = [r for r in encoded if r["split"] == "train"]
    dev = [r for r in encoded if r["split"] == "dev"]
    if not train or not dev:
        raise ValueError("no eligible train/dev examples")
    model = AutoModelForCausalLM.from_pretrained(a.base_model, local_files_only=True, torch_dtype=torch.bfloat16,
                                                attn_implementation="sdpa").cuda()
    model.config.use_cache = False
    model = get_peft_model(model, LoraConfig(r=8, lora_alpha=16, lora_dropout=0.05,
                                            target_modules=["q_proj", "v_proj"], task_type="CAUSAL_LM"))
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    optimizer = torch.optim.AdamW((p for p in model.parameters() if p.requires_grad), lr=a.lr)
    def loss_for(row):
        ids = torch.tensor([row["prefix"] + row["target_ids"]], device="cuda")
        n = len(row["target_ids"])
        # Only response-token vocabulary logits; prefix loss is never included.
        logits = model(input_ids=ids, attention_mask=torch.ones_like(ids), logits_to_keep=n + 1).logits[:, :-1].float()
        target = torch.tensor(row["target_ids"], device="cuda")
        return torch.nn.functional.cross_entropy(logits.reshape(-1, logits.shape[-1]), target)
    def dev_loss():
        model.eval()
        with torch.inference_mode():
            values = [float(loss_for(row)) for row in dev]
        return sum(values) / len(values)
    t = time.monotonic()
    initial_dev = dev_loss()
    epoch_history = []
    step, losses = 0, []
    model.train()
    optimizer.zero_grad()
    for epoch in range(a.epochs):
        model.train()
        epoch_loss_start = len(losses)
        random.shuffle(train)
        for i, row in enumerate(train):
            loss = loss_for(row)
            if not torch.isfinite(loss):
                raise ValueError("nonfinite loss")
            # Correctly normalize the final smaller accumulation group.
            denom = min(a.accumulation, len(train) - (i // a.accumulation) * a.accumulation)
            (loss / denom).backward()
            losses.append(float(loss.detach()))
            if (i + 1) % a.accumulation == 0 or i + 1 == len(train):
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                optimizer.zero_grad()
                step += 1
                print(json.dumps({"epoch": epoch, "optimizer_step": step, "examples": i + 1,
                                  "loss": sum(losses[-denom:]) / denom}), flush=True)
        current_dev = dev_loss()
        if not math.isfinite(current_dev):
            raise ValueError("nonfinite dev loss")
        epoch_history.append({"epoch": epoch + 1, "optimizer_steps": step,
                              "train_loss": sum(losses[epoch_loss_start:]) / len(train),
                              "teacher_dev_loss": current_dev})
        print(json.dumps({"stage": "epoch_complete", **epoch_history[-1]}), flush=True)
    final_dev = epoch_history[-1]["teacher_dev_loss"]
    probe = encoded[0]
    # The reloaded adapter is evaluated with dropout disabled.  Match that
    # mode before capturing the reference logits; otherwise a stochastic LoRA
    # dropout mask can make a correct fresh reload fail verification.
    model.eval()
    inputs = torch.tensor([probe["prefix"]], device="cuda")
    with torch.inference_mode():
        expected = model(input_ids=inputs, logits_to_keep=1).logits[:, -1].float().cpu()
    a.output.mkdir(parents=True)
    model.save_pretrained(a.output)
    tok.save_pretrained(a.output)
    del optimizer, model
    import gc
    gc.collect()
    torch.cuda.empty_cache()
    fresh = AutoModelForCausalLM.from_pretrained(a.base_model, local_files_only=True, torch_dtype=torch.bfloat16,
                                                 attn_implementation="sdpa").cuda()
    # Training disables KV caching for gradient checkpointing.  Compare the
    # fresh adapter under the same execution mode; cache=True can change BF16
    # SDPA logits enough to fail a checkpoint identity check without indicating
    # a broken adapter.
    fresh.config.use_cache = False
    fresh = PeftModel.from_pretrained(fresh, a.output).eval()
    with torch.inference_mode():
        actual = fresh(input_ids=inputs, logits_to_keep=1).logits[:, -1].float().cpu()
    if not torch.allclose(expected, actual, atol=0.02, rtol=0.01):
        raise ValueError("fresh base reload verification failed")
    manifest = {"method": "SetR-style selection-only small-model distillation, not original IRI reproduction",
                "seed": 20261110, "epochs": a.epochs, "lr": a.lr, "accumulation": a.accumulation,
                "max_length": a.max_length, "optimizer_steps": step,
                "train_examples": len(train), "dev_examples": len(dev), "overflow_skipped": dict(skipped),
                "teacher_status": dict(Counter(r["status"] for r in predictions.values())),
                 "initial_teacher_dev_loss": initial_dev, "final_teacher_dev_loss": final_dev,
                 "epoch_history": epoch_history,
                "checkpoint_rule": "one final checkpoint, no external-test/checkpoint tuning",
                "fresh_reload_verified": True, "external_question_overlap": 0,
                "seconds": time.monotonic() - t, "peak_cuda_bytes": torch.cuda.max_memory_allocated(),
                "source_dataset_sha256": hashlib.sha256(a.dataset.read_bytes()).hexdigest(),
                "base_manifest_sha256": hashlib.sha256(a.model_manifest.read_bytes()).hexdigest(),
                "teacher_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in a.teacher},
                "trainer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in a.output.iterdir() if p.is_file()},
                "packages": {n: importlib.metadata.version(n) for n in ("torch", "transformers", "peft", "ftfy")}}
    (a.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == "__main__":
    main()
