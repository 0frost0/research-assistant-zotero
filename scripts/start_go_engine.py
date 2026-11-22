"""Opt-in local Zotero engine. The launcher hosts inference and owns one Go API."""
import argparse
import asyncio
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WEB_STATIC = ROOT / "research_assistant/web/static"
sys.path.insert(0, str(ROOT))


async def serve(root, executable, port):
    import uvicorn
    from dotenv import load_dotenv

    if not executable.is_file():
        raise FileNotFoundError(f"Build the Go development backend first: {executable}")
    load_dotenv(ROOT / ".env")
    from research_assistant.web.inference_worker import create_worker
    token_file = root / ".data/zotero/connection-token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(32))
    if not token_file.read_text().strip():
        raise ValueError("Empty local connection token")
    private_token = secrets.token_urlsafe(32)
    reranker = os.environ.setdefault("RESEARCH_RERANKER_URL", "http://127.0.0.1:30002/rerank")
    worker = uvicorn.Server(uvicorn.Config(create_worker(root, private_token), log_level="warning"))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        worker_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        serving = asyncio.create_task(worker.serve(sockets=[sock]))
        api = None
        try:
            while not worker.started and not serving.done():
                await asyncio.sleep(0.05)
            if not worker.started:
                await serving
                raise RuntimeError("Private worker did not start")
            api = await asyncio.create_subprocess_exec(
                str(executable), "-root", str(root), "-listen", f"127.0.0.1:{port}",
                "-worker", worker_url, "-managed-stdin", "-reranker", reranker,
                "-web-static", str(WEB_STATIC),
                stdin=asyncio.subprocess.PIPE, cwd=ROOT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            api.stdin.write((private_token + "\n").encode("ascii"))
            await api.stdin.drain()
            exit_status = asyncio.create_task(api.wait())
            await asyncio.wait([serving, exit_status], return_when=asyncio.FIRST_COMPLETED)
            if exit_status.done() and exit_status.result():
                raise RuntimeError(f"Go backend exited with status {exit_status.result()}")
        finally:
            if api is not None:
                api.stdin.close()
                try:
                    await asyncio.wait_for(api.wait(), timeout=10)
                except asyncio.TimeoutError:
                    api.kill()
                    await api.wait()
            worker.should_exit = True
            await serving


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Data root (Zotero engine workdir by default)")
    default_binary = "researchd.exe" if os.name == "nt" else "researchd"
    parser.add_argument("--researchd", type=Path, default=ROOT / "output/zotero/dev" / default_binary)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    asyncio.run(serve(args.root.resolve(), args.researchd.resolve(), args.port))
