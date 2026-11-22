"""Build a clean Git revision and byte-verify its source archive.

Run from a clean checkout. Outputs must be outside the checkout, preserving
vcs.modified=false. Does not publish or replace historical release assets.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile


def build(root, output):
    root, output = root.resolve(), output.resolve()
    if output.is_relative_to(root):
        raise ValueError("Release output must be outside the checkout")
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root)
    if git("status", "--porcelain").strip():
        raise ValueError("Clean checkout required")
    if git("ls-files", "-ci", "--exclude-from=.gitignore").strip():
        raise ValueError("Ignored local files are tracked")
    sha = git("rev-parse", "HEAD").decode().strip()
    output.mkdir(parents=True, exist_ok=False)
    platform = subprocess.check_output(["go", "env", "GOOS", "GOARCH"], cwd=root / "backend", text=True).splitlines()
    extension = ".exe" if platform[0] == "windows" else ""
    executable = output / f"researchd-{'-'.join(platform)}-{sha[:7]}-clean{extension}"
    subprocess.run(["go", "build", "-trimpath", "-o", str(executable), "./cmd/researchd"], cwd=root / "backend", check=True)
    info = subprocess.check_output(["go", "version", "-m", str(executable)], text=True)
    if "vcs.revision=" + sha not in info or "vcs.modified=false" not in info:
        raise ValueError("Executable provenance mismatch")
    archive = output / f"research-assistant-go-core-{sha[:7]}-git.zip"
    git("-c", "core.autocrlf=false", "archive", "--format=zip", "--output=" + str(archive), sha)
    files = git("ls-tree", "-rz", "--name-only", sha).decode().rstrip("\0").split("\0")
    with zipfile.ZipFile(archive) as zipped:
        members = [name for name in zipped.namelist() if not name.endswith("/")]
        if set(members) != set(files):
            raise ValueError("Source archive file list differs from Git")
        for name in members:
            if zipped.read(name) != git("show", sha + ":" + name):
                raise ValueError("Archive blob differs: " + name)
    if git("status", "--porcelain").strip() or git("rev-parse", "HEAD").decode().strip() != sha:
        raise ValueError("Checkout changed during build")
    record = {"source_commit": sha, "source_archive_matches_git_blobs": True, "build_info": info,
              "assets": {p.name: {"size": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in (executable, archive)}}
    (output / "provenance.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps(record))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.checkout, args.output)
