"""Zotero engine entry point. Run only when ready to start local services."""
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    # Never inherit the deployment host's bind address for the local desktop app.
    token_path = ROOT / ".data/zotero/connection-token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if not token_path.exists():
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(32))
    # Desktop launch uses the same local token file read by Zotero, regardless
    # of a legacy remote deployment token in .env.
    os.environ["RESEARCH_ZOTERO_TOKEN"] = token_path.read_text().strip()
    print(f"Local backend: http://127.0.0.1:8765; token file: {token_path}", flush=True)
    import uvicorn
    from research_assistant.web.web_app import app
    uvicorn.run(app, host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
