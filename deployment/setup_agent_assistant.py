"""Configure the dedicated single-GPU host after files and environments exist."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


ROOT = Path("/srv/research-assistant/app")
QDRANT_SHA256 = "7951936099f2bdc05776f875f5b683fe49beeb8c8ad71e4ff7ae042e984efc83"


def require(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"Required deployment asset is missing: {path}")


def unit(name: str, command: str, environment: dict[str, str]) -> str:
    lines = [
        "[Unit]",
        f"Description=Research assistant {name}",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={ROOT}",
        f"ExecStart={command}",
        "Restart=on-failure",
        "RestartSec=10",
        "TimeoutStopSec=30",
        "UMask=0077",
    ]
    lines.extend(f'Environment="{key}={value}"' for key, value in environment.items())
    lines.extend(["", "[Install]", "WantedBy=default.target", ""])
    return "\n".join(lines)


def main() -> None:
    if os.name == "nt" or Path.home() != Path("/home/ubuntu"):
        raise SystemExit("This installer is restricted to the agent-assistant ubuntu account")
    require(ROOT / ".venv/bin/python")
    require(ROOT / ".gpu_env/bin/python")
    require(ROOT / ".gpu_env/bin/mineru-vllm-server")
    require(ROOT / "deployment/embedding/models/Qwen3-VL-Embedding-2B/model.safetensors")
    mineru = ROOT / "models/OpenDataLab--MinerU2.5-Pro-2605-1.2B/snapshots/master"
    require(mineru / "model.safetensors")
    qdrant = ROOT / "runtime/qdrant/qdrant"
    require(qdrant)
    if hashlib.sha256(qdrant.read_bytes()).hexdigest() != QDRANT_SHA256:
        raise SystemExit("Qdrant binary hash differs from the verified project binary")

    common = {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    specs = {
        "web": unit(
            "web",
            f"{ROOT}/.venv/bin/python -B {ROOT}/web_app.py",
            {
                **common,
                "RESEARCH_ASSISTANT_MINERU_PYTHON": str(ROOT / ".gpu_env/bin/python"),
                "RESEARCH_ASSISTANT_QDRANT_URL": "http://127.0.0.1:6333",
            },
        ),
        "qdrant": unit(
            "qdrant",
            f"{qdrant} --disable-telemetry",
            {
                "QDRANT__SERVICE__HOST": "127.0.0.1",
                "QDRANT__STORAGE__STORAGE_PATH": str(ROOT / "runtime/qdrant/storage"),
            },
        ),
        "embedding": unit(
            "embedding",
            f"{ROOT}/.gpu_env/bin/python -u {ROOT}/deployment/embedding/remote_embedding_service.py",
            {**common, "CUDA_VISIBLE_DEVICES": "0"},
        ),
        "mineru": unit(
            "mineru",
            (
                f"{ROOT}/.gpu_env/bin/mineru-vllm-server --model {mineru} "
                "--host 127.0.0.1 --port 30000 --gpu-memory-utilization 0.42 "
                "--max-model-len 8192"
            ),
            {
                **common,
                "CUDA_VISIBLE_DEVICES": "0",
                "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
                # MinerU otherwise auto-probes Hugging Face/ModelScope before
                # failing over, and some CLI paths can start a weight download.
                "MINERU_MODEL_SOURCE": "local",
            },
        ),
    }
    folder = Path.home() / ".config/systemd/user"
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in specs.items():
        path = folder / f"research-assistant-{name}.service"
        if path.exists() and str(ROOT) not in path.read_text(encoding="utf-8"):
            raise SystemExit(f"Refusing to replace unrelated unit: {path}")
        path.write_text(content, encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(
        ["systemctl", "--user", "enable", *(f"research-assistant-{name}" for name in specs)],
        check=True,
    )
    print("Configured four loopback-only services for one RTX 5090")


if __name__ == "__main__":
    main()
