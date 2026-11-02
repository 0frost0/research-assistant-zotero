"""Invoked by Zotero: start dependencies without a terminal or model request."""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def run(args, timeout=180):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=FLAGS)
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} 执行失败，请检查 Docker 或 SSH 配置（退出码 {result.returncode}）。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ssh-host", default="")
    parser.add_argument("--ssh-executable", default="ssh")
    parser.add_argument("--qwen-unit", default="")
    args = parser.parse_args()
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("未安装 Docker Desktop，请先完成安装。")
    ready = subprocess.run([docker, "info"], capture_output=True, timeout=15, creationflags=FLAGS).returncode == 0
    if not ready and os.name == "nt":
        desktop = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Docker/Docker/Docker Desktop.exe"
        if not desktop.is_file():
            raise RuntimeError("Docker Desktop 路径不存在。")
        subprocess.Popen([str(desktop)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=FLAGS)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            time.sleep(3)
            if subprocess.run([docker, "version", "--format", "{{.Server.Version}}"], capture_output=True, timeout=10, creationflags=FLAGS).returncode == 0:
                ready = True
                break
    if not ready:
        raise RuntimeError("Docker 未就绪，请查看 Docker Desktop 的启动提示。")
    run([docker, "volume", "create", "research-assistant-qdrant-data"])
    run([docker, "compose", "-f", str(ROOT / "deployment/qdrant/compose.qdrant.yaml"), "up", "-d"], 300)
    if args.qwen_unit:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.service", args.qwen_unit):
            raise RuntimeError("Qwen 服务名须为明确的 systemd .service 名称。")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", args.ssh_host):
            raise RuntimeError("请填写 SSH 别名。")
        run([args.ssh_executable, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", args.ssh_host,
             "sudo", "-n", "systemctl", "start", args.qwen_unit], 90)
    print("依赖启动命令完成；MinerU 在精析时按需运行。")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc))
        raise SystemExit(1)
