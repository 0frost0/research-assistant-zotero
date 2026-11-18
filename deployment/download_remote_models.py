"""Download pinned weights only into this project; reuse explicit local paths."""
import argparse
import json
import os
from pathlib import Path


MODELS = {
    "qwen": ("Qwen/Qwen3-VL-Embedding-2B", "9f2f7e710d6d81056aa5c0a4f04764fec6bb7bda"),
    "mineru": ("OpenDataLab/MinerU2.5-Pro-2605-1.2B", "08aaea840498d49ce16247b6263196cf02814885"),
    "reranker": ("Qwen/Qwen3-Reranker-0.6B", "e61197ed45024b0ed8a2d74b80b4d909f1255473"),
}


def own_path(root, relative):
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"Refusing to write outside project: {relative}")
    return path


def prepare(root, selected, reuse=None):
    root = root.resolve()
    if not (root / ".research-assistant-workspace").is_file():
        raise ValueError("Missing dedicated workspace marker")
    repo, revision = MODELS[selected]
    target = root / "models" / repo.split("/")[-1]
    if reuse:
        source = Path(reuse).expanduser().resolve(strict=True)
        if not source.is_dir() or not (source / "config.json").is_file():
            raise ValueError("Reuse source must be an existing model directory")
        own_path(root, "models").mkdir(exist_ok=True)
        if target.is_symlink() and target.resolve() == source:
            return {"path": str(target), "source": str(source), "mode": "reused_readonly"}
        if target.exists() or target.is_symlink():
            raise FileExistsError("Refusing to replace an existing model path")
        target.symlink_to(source, target_is_directory=True)
        return {"path": str(target), "source": str(source), "mode": "reused_readonly"}
    # Never download through a shared-model symlink.
    own_path(root, target.relative_to(root))
    cache = own_path(root, "runtime/cache/huggingface")
    os.environ.update(HF_HOME=str(cache), HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
                      HF_HUB_DISABLE_XET="1", HF_HUB_DISABLE_TELEMETRY="1")
    from huggingface_hub import snapshot_download
    snapshot_download(repo, revision=revision, local_dir=target, cache_dir=cache,
                      max_workers=4, ignore_patterns=["*.md", "*.png", "*.jpg", ".gitattributes"])
    return {"path": str(target), "repo": repo, "revision": revision, "mode": "downloaded"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("model", choices=MODELS)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--reuse", type=Path)
    args = parser.parse_args()
    result = prepare(args.root, args.model, args.reuse)
    report = own_path(args.root.resolve(), f"runtime/{args.model}-model.json")
    report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)
