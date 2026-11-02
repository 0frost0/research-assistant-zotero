"""Run on the Qwen host once. Never prints or overwrites the key."""
import os
from pathlib import Path
import secrets

if __name__ == "__main__":
    path = Path(__file__).resolve().parent / "api-key"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        print("Existing api-key retained.")
    else:
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(32))
        print("Created api-key with owner-only permissions. Enter its contents in Zotero; do not commit it.")
