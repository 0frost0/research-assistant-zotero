"""Project-local GPU services. No system units, shared caches, or global installs."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).absolute().parents[1]
PORTS = {"qwen": 30001, "mineru": 30000, "reranker": 30002}


def local_path(relative):
    path = ROOT / relative
    if not path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError(f"Write path escapes workspace: {relative}")
    return path


def environment(gpu):
    env = dict(os.environ)
    locations = {
        "XDG_CACHE_HOME": "runtime/cache", "HF_HOME": "runtime/cache/huggingface",
        "TORCH_HOME": "runtime/cache/torch", "TRITON_CACHE_DIR": "runtime/cache/triton",
        "TORCHINDUCTOR_CACHE_DIR": "runtime/cache/inductor", "CUDA_CACHE_PATH": "runtime/cache/cuda",
        "VLLM_CACHE_ROOT": "runtime/cache/vllm", "VLLM_CONFIG_ROOT": "runtime/config/vllm",
        "VLLM_ASSETS_CACHE": "runtime/cache/vllm-assets", "MPLCONFIGDIR": "runtime/cache/matplotlib",
        "NUMBA_CACHE_DIR": "runtime/cache/numba", "TMPDIR": "runtime/tmp",
        "PIP_CACHE_DIR": "runtime/cache/pip",
    }
    for key, relative in locations.items():
        path = local_path(relative)
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    env.update(CUDA_VISIBLE_DEVICES=str(gpu), PYTHONDONTWRITEBYTECODE="1", HF_HUB_OFFLINE="1",
               TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false",
               MINERU_MODEL_SOURCE="local", MINERU_TOOLS_CONFIG_JSON=str(local_path("runtime/mineru.json")),
               QWEN_MODEL_PATH=str(ROOT / "models/Qwen3-VL-Embedding-2B"), NO_PROXY="localhost,127.0.0.1")
    return env


def command(name):
    python = str(ROOT / ".gpu_env/bin/python")
    if name == "qwen":
        return [python, "-B", "-u", str(ROOT / "deployment/embedding/remote_embedding_service.py")]
    if name == "reranker":
        return [python, "-B", "-u", str(ROOT / "deployment/embedding/remote_reranker_service.py")]
    return [python, "-B", str(ROOT / ".gpu_env/bin/mineru-vllm-server"),
            "--model", str(ROOT / "models/MinerU2.5-Pro-2605-1.2B"),
            "--host", "127.0.0.1", "--port", "30000", "--served-model-name", "mineru",
            "--gpu-memory-utilization", "0.20", "--max-model-len", "8192",
            "--max-num-seqs", "2", "--enforce-eager"]


def process_identity(pid):
    proc = Path(f"/proc/{pid}")
    try:
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        return {"cwd": str((proc / "cwd").resolve(strict=True)), "start_ticks": fields[19],
                "command": [v.decode() for v in (proc / "cmdline").read_bytes().split(b"\0") if v]}
    except FileNotFoundError:
        return None


def owned_process(name):
    pidfile = local_path(f"runtime/pids/{name}.json")
    if not pidfile.exists():
        return None
    saved = json.loads(pidfile.read_text())
    current = process_identity(saved["pid"])
    if current is None:
        return None
    if (current != saved["identity"] or current["cwd"] != str(ROOT.resolve()) or
            current["command"] != command(name)):
        raise RuntimeError(f"PID ownership mismatch for {name}; no signal sent")
    return saved["pid"]


def status(name):
    pid = owned_process(name)
    port = PORTS[name]
    endpoint = "/v1/models" if name == "mineru" else "/health"
    health = None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}{endpoint}", timeout=3) as response:
            health = json.load(response)
    except (OSError, ValueError):
        pass
    expected = (health and health.get("model_id") == "Qwen/Qwen3-VL-Embedding-2B") if name == "qwen" else (
        health and any(v.get("id") == "mineru" for v in health.get("data", [])))
    if name == "reranker":
        expected = health and health.get("model") == "Qwen/Qwen3-Reranker-0.6B"
    return {"service": name, "pid": pid, "healthy": bool(pid and expected), "port": port}


def start(name, gpu):
    if owned_process(name):
        return status(name)
    port = PORTS[name]
    # Refuse any existing listener, even if its health resembles our model.
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"Port {port} already belongs to another process")
    env = environment(gpu)
    local_path("runtime/pids").mkdir(parents=True, exist_ok=True)
    local_path("runtime/logs").mkdir(parents=True, exist_ok=True)
    model = ROOT / "models" / {"qwen": "Qwen3-VL-Embedding-2B", "mineru": "MinerU2.5-Pro-2605-1.2B",
                               "reranker": "Qwen3-Reranker-0.6B"}[name]
    if not (model / "config.json").is_file() or not list(model.glob("*.safetensors")):
        raise RuntimeError("Model files not ready")
    free = int(subprocess.check_output(["nvidia-smi", f"--id={gpu}", "--query-gpu=memory.free",
                                        "--format=csv,noheader,nounits"], text=True).strip())
    if free < {"qwen": 6000, "mineru": 11000, "reranker": 3000}[name]:
        raise RuntimeError("Insufficient free GPU memory; existing GPU jobs were left untouched")
    with local_path(f"runtime/logs/{name}.log").open("ab") as log:
        process = subprocess.Popen(command(name), cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True)
    identity = process_identity(process.pid)
    local_path(f"runtime/pids/{name}.json").write_text(json.dumps({"pid": process.pid, "identity": identity}), encoding="utf-8")
    return {"service": name, "pid": process.pid, "starting": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "status", "stop"])
    parser.add_argument("service", choices=["qwen", "mineru", "reranker", "all"])
    parser.add_argument("--gpu", default="3")
    args = parser.parse_args()
    if sys.platform != "linux" or not (ROOT / ".research-assistant-workspace").is_file():
        raise RuntimeError("Run inside the dedicated Linux workspace")
    if ROOT.is_symlink() or ROOT.stat().st_uid != os.getuid():
        raise RuntimeError("Workspace must belong to the current user")
    for name in ["qwen", "mineru", "reranker"] if args.service == "all" else [args.service]:
        if args.action == "start":
            result = start(name, args.gpu)
        elif args.action == "stop":
            pid = owned_process(name)
            if pid:
                os.killpg(pid, signal.SIGTERM)
            result = {"service": name, "stop_requested": bool(pid)}
        else:
            result = status(name)
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
