"""Run on the remote host; starts only this project's dedicated service."""

import json
import os
import subprocess
import sys
import signal
import time
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parent
if "--restart" in sys.argv:
    pid_path = root / "embedding_service.pid"
    if pid_path.exists():
        service_pid = int(pid_path.read_text())
        proc_path = Path(f"/proc/{service_pid}")
        if proc_path.exists():
            command = (proc_path / "cmdline").read_bytes().split(b"\0")
            expected_script = str(root / "remote_embedding_service.py").encode()
            if expected_script not in command or (proc_path / "cwd").resolve() != root:
                raise RuntimeError("PID does not belong to this project's embedding service")
            os.kill(service_pid, signal.SIGTERM)
            for _ in range(50):
                if not proc_path.exists():
                    break
                time.sleep(0.1)
try:
    with urllib.request.urlopen("http://127.0.0.1:30001/health", timeout=3) as response:
        health = json.load(response)
    if health.get("model_id") != "Qwen/Qwen3-VL-Embedding-2B":
        raise RuntimeError("Port 30001 belongs to another service")
    print("Embedding service already healthy")
except OSError:
    gpu_uuid = os.getenv("RESEARCH_ASSISTANT_EMBEDDING_GPU_UUID",
                         "GPU-18b58394-8027-7379-c44c-5e489720a156")
    free_memory = int(subprocess.check_output(
        ["nvidia-smi", f"--id={gpu_uuid}", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
        text=True).strip())
    if free_memory < 8000:
        raise RuntimeError("The dedicated embedding GPU needs at least 8000 MiB free")
    environment = dict(os.environ, CUDA_VISIBLE_DEVICES=gpu_uuid, HF_HUB_OFFLINE="1",
                       PYTHONPATH=str(root / ".embedding_deps"),
                       TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    with (root / "embedding_service.log").open("ab") as log:
        process = subprocess.Popen([sys.executable, "-u", str(root / "remote_embedding_service.py")],
                                   cwd=root, env=environment, stdout=log, stderr=log,
                                   start_new_session=True, stdin=subprocess.DEVNULL)
    (root / "embedding_service.pid").write_text(str(process.pid), encoding="ascii")
    print(f"Embedding service starting, PID {process.pid}")
