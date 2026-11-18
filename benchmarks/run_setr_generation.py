"""Offline whole-pool SetR-style generation; JSONL is gold-free and resumable.

Use on source train/dev for teacher labels or on frozen external-test inputs.
Never truncates, pads a selected set, repairs malformed output, or reads gold.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.setr_protocol import digest, messages, parse_selection


def input_fingerprint(group, mode):
    return digest(messages(group["question"], group["candidates"], mode))


def file_sha256(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def file_identity(path):
    p = Path(path)
    s = p.stat()
    return {"path": str(p.resolve()), "size": s.st_size, "mtime_ns": s.st_mtime_ns,
            "ctime_ns": s.st_ctime_ns, "inode": s.st_ino, "device": s.st_dev}


def verify_model_files(root, manifest_path, receipt_path=None):
    """Full SHA on first use; optional local stat receipt avoids repeated full I/O.

    Stat receipts assume a trusted, non-adversarial local/shared filesystem. They
    detect ordinary replacements/edits, not malicious metadata-preserving edits.
    Small configuration/template files are always SHA-verified.
    """
    manifest = json.loads(manifest_path.read_bytes())
    manifest_hash = file_sha256(manifest_path)
    receipt = None
    if receipt_path and receipt_path.exists():
        receipt = json.loads(receipt_path.read_bytes())
        if receipt.get("manifest_sha256") != manifest_hash:
            receipt = None
    identities = {}
    for name, expected in manifest["files"].items():
        if Path(name).name != name:
            raise ValueError("model manifest must contain local filenames")
        path = root / name
        before = file_identity(path)
        cached = (receipt is not None and path.suffix == ".safetensors" and
                  receipt.get("files", {}).get(name) == before)
        if not cached and file_sha256(path) != expected:
            raise ValueError(f"base model fingerprint mismatch: {name}")
        if before != file_identity(path):
            raise ValueError(f"model changed during verification: {name}")
        identities[name] = before
        print(json.dumps({"verified": name, "method": "local_stat_receipt" if cached else "sha256"}), flush=True)
    if receipt_path:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        temp = receipt_path.with_name(receipt_path.name + f".{os.getpid()}.tmp")
        with temp.open("x", encoding="utf-8") as f:
            json.dump({"manifest_sha256": manifest_hash, "files": identities,
                       "trust_boundary": "local stat reuse is not a new cryptographic verification"}, f, indent=2)
        os.replace(temp, receipt_path)
    return manifest


def model_loader_name(model_type):
    # Qwen3.5 checkpoints include a vision encoder even for text-only use.
    return "AutoModelForImageTextToText" if model_type in {"qwen3_5", "qwen3_5_moe"} else "AutoModelForCausalLM"


def select_groups(data, split, shard, shards, max_samples=None):
    if data.get("role") == "external_test_only":
        if split != "external_test_only":
            raise ValueError("external test must be explicitly requested")
        if max_samples is not None:
            raise ValueError("smoke limits are source-only; do not subsample external tests")
    groups = [g for g in data["groups"] if split in (None, "external_test_only") or g.get("split") == split]
    groups = groups[shard::shards]
    return groups if max_samples is None else groups[:max_samples]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--model", type=Path, required=True)
    p.add_argument("--model-manifest", type=Path, required=True)
    p.add_argument("--adapter", type=Path)
    p.add_argument("--mode", choices=["iri", "selection", "rank"], default="iri")
    p.add_argument("--split", choices=["train", "dev", "external_test_only"])
    p.add_argument("--max-input-tokens", type=int, default=10240)
    p.add_argument("--max-new-tokens", type=int, default=2048)
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--batch-size", type=int, default=1)
    p.add_argument("--device-map", default="auto", help="Transformers device map; auto shards large teacher models")
    p.add_argument("--max-gpu-memory-gib", type=int, help="per visible GPU weight dispatch budget; leaves generation headroom")
    p.add_argument("--max-samples", type=int, help="source-only deterministic smoke limit; never external test")
    p.add_argument("--verification-receipt", type=Path, help="optional project-local stat receipt; first use fully hashes, later unchanged weight files reuse verification")
    a = p.parse_args()
    if not 0 <= a.shard < a.shards or a.batch_size < 1 or (a.max_samples is not None and a.max_samples < 1):
        p.error("invalid sharding/batch")
    import torch
    import transformers
    from transformers import AutoConfig, AutoTokenizer
    if a.verification_receipt and a.model.resolve() in a.verification_receipt.resolve().parents:
        raise ValueError("verification receipt must be outside shared model directory")
    verify_model_files(a.model, a.model_manifest, a.verification_receipt)
    if a.adapter:
        adapter_manifest = json.loads((a.adapter / "manifest.json").read_bytes())
        if adapter_manifest["base_manifest_sha256"] != hashlib.sha256(a.model_manifest.read_bytes()).hexdigest():
            raise ValueError("adapter/base mismatch")
        for name, expected in adapter_manifest["files"].items():
            if hashlib.sha256((a.adapter / name).read_bytes()).hexdigest() != expected:
                raise ValueError("adapter file fingerprint mismatch")
    data = json.loads(a.dataset.read_bytes())
    groups = select_groups(data, a.split, a.shard, a.shards, a.max_samples)
    model_config = AutoConfig.from_pretrained(a.model, local_files_only=True)
    loader_name = model_loader_name(model_config.model_type)
    config = {"dataset_sha256": hashlib.sha256(a.dataset.read_bytes()).hexdigest(),
              "model_manifest_sha256": hashlib.sha256(a.model_manifest.read_bytes()).hexdigest(),
              "adapter_manifest_sha256": hashlib.sha256((a.adapter / "manifest.json").read_bytes()).hexdigest() if a.adapter else None,
              "mode": a.mode, "split": a.split, "max_input_tokens": a.max_input_tokens,
              "max_new_tokens": a.max_new_tokens, "do_sample": False, "batch_size": a.batch_size,
              "shard": a.shard, "shards": a.shards,
               "device_map": a.device_map, "max_gpu_memory_gib": a.max_gpu_memory_gib,
               "max_samples": a.max_samples, "model_loader": loader_name, "enable_thinking": False,
               "verification_policy": "sha256_then_trusted_local_stat_receipt" if a.verification_receipt else "full_sha256",
              "worker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "protocol_sha256": hashlib.sha256(Path(__file__).with_name("setr_protocol.py").read_bytes()).hexdigest(),
              "packages": {n: importlib.metadata.version(n) for n in ("torch", "transformers", "ftfy")}}
    run_id = digest(config)
    completed = set()
    if a.output.exists():
        with a.output.open(encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                if row["run_id"] != run_id or row["id"] in completed:
                    raise ValueError("resume fingerprint or duplicate mismatch")
                completed.add(row["id"])
    a.output.parent.mkdir(parents=True, exist_ok=True)
    meta = a.output.with_suffix(".manifest.json")
    if not meta.exists():
        meta.write_text(json.dumps(config, indent=2), encoding="utf-8")
    tok = AutoTokenizer.from_pretrained(a.model, local_files_only=True, padding_side="left")
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    dispatch = {}
    if a.max_gpu_memory_gib:
        dispatch["max_memory"] = {i: f"{a.max_gpu_memory_gib}GiB" for i in range(torch.cuda.device_count())}
    print(json.dumps({"stage": "loading_model", "model": str(a.model), "loader": loader_name}), flush=True)
    load_started = time.monotonic()
    model = getattr(transformers, loader_name).from_pretrained(
        a.model, local_files_only=True, torch_dtype=torch.bfloat16,
        attn_implementation="sdpa", device_map=a.device_map, **dispatch).eval()
    if any(str(device) in {"cpu", "disk"} for device in getattr(model, "hf_device_map", {}).values()):
        raise ValueError("GPU budget insufficient; CPU/disk offload is not allowed for this experiment")
    print(json.dumps({"model_loaded": True, "load_seconds": time.monotonic() - load_started,
                      "device_map": getattr(model, "hf_device_map", {})}), flush=True)
    if a.adapter:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, a.adapter).eval()
    groups = [g for g in groups if g["id"] not in completed]
    with a.output.open("a", encoding="utf-8") as f:
        for start in range(0, len(groups), a.batch_size):
            batch = groups[start:start + a.batch_size]
            records, encoded = [], []
            for group in batch:
                prompt = tok.apply_chat_template(messages(group["question"], group["candidates"], a.mode),
                                                 tokenize=False, add_generation_prompt=True, enable_thinking=False)
                ids = tok.encode(prompt, add_special_tokens=False)
                rec = {"id": group["id"], "run_id": run_id, "input_sha256": input_fingerprint(group, a.mode),
                       "mode": a.mode, "input_tokens": len(ids), "selected_indices": [], "output": ""}
                records.append(rec)
                if len(ids) > a.max_input_tokens:
                    rec["status"] = "input_overflow"
                else:
                    encoded.append((len(records) - 1, ids))
            t = time.monotonic()
            if encoded:
                input_device = model.get_input_embeddings().weight.device
                inputs = tok.pad({"input_ids": [ids for _, ids in encoded]}, padding=True, return_tensors="pt").to(input_device)
                with torch.inference_mode():
                    output = model.generate(**inputs, max_new_tokens=a.max_new_tokens, do_sample=False,
                                            pad_token_id=tok.pad_token_id, use_cache=True, logits_to_keep=1)
                for (index, _), generated in zip(encoded, output):
                    tail = generated[inputs.input_ids.shape[1]:].tolist()
                    rec = records[index]
                    rec["output"] = tok.decode(tail, skip_special_tokens=True)
                    rec["output_tokens"] = len(tail)
                    if not any(token in tail for token in ([model.generation_config.eos_token_id] if isinstance(model.generation_config.eos_token_id, int) else model.generation_config.eos_token_id or [tok.eos_token_id])):
                        rec["status"] = "output_limit"
                    else:
                        try:
                            rec["selected_indices"] = parse_selection(rec["output"], len(batch[index]["candidates"]), ranking=a.mode == "rank")
                            rec["status"] = "ok"
                        except ValueError as exc:
                            rec["status"] = "invalid_output"
                            rec["error"] = str(exc)
            for rec in records:
                rec["batch_seconds"] = time.monotonic() - t
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
            print(json.dumps({"completed": start + len(batch), "total": len(groups),
                              "status": [r["status"] for r in records]}), flush=True)


if __name__ == "__main__":
    main()
