"""Archive retained development bundles and explicitly selected public reports.

Run after archive_github_releases.py prepare, before committing and publishing.
No model calls, training, private datasets, or model weights are included.
"""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def aggregate_only(value):
    if isinstance(value, dict):
        return {k: aggregate_only(v) for k, v in value.items()
                if k not in {"invalid_examples", "question", "quote", "context", "paragraphs", "raw_output", "answer", "passage"}}
    if isinstance(value, list):
        return [aggregate_only(v) for v in value]
    return value


def prepare(output):
    folder = output / "0.3.11"
    inventory = []
    bundles = sorted((ROOT / "output").glob("*.tar.gz"))
    for path in bundles:
        members = {}
        with tarfile.open(path) as archive:
            for member in archive:
                if not member.isfile():
                    continue
                if (member.name.startswith("/") or ".." in Path(member.name).parts
                        or Path(member.name).suffix.lower() in {".pdf", ".db", ".sqlite", ".sqlite3", ".pem", ".key", ".safetensors"}
                        or (Path(member.name).name.startswith(".env") and Path(member.name).name != ".env.reading.example")):
                    raise ValueError("Unexpected private/unsafe bundle member: " + member.name)
                raw = archive.extractfile(member).read()
                members[member.name] = sha(raw)
        inventory.append({"file": path.name, "bytes": path.stat().st_size,
                          "sha256": sha(path.read_bytes()), "members": members,
                          "boundary": "Original retained development bundle, including historical build caches; not a verified backend match for any XPI version. Filename final/verified is historical naming, not current acceptance."})
    public = ROOT / "docs" / "releases" / "artifacts"
    write_json(public / "retained-bundles.json", inventory)
    with zipfile.ZipFile(folder / "retained-development-bundles.zip", "w", zipfile.ZIP_STORED) as z:
        z.write(public / "retained-bundles.json", "inventory.json")
        for path in bundles:
            z.write(path, path.name)

    sources = sorted((ROOT / "output/public-sufficiency-replay-20261118").glob("*.json"))
    sources += sorted((ROOT / "output/public-sufficiency-v1-20261115").glob("pilot-*/pilot-score*.json"))
    # The smoke report contains only generated fixture data; publish its summary.
    memory = json.loads((ROOT / "output/demo-memory-20261118/report.json").read_text(encoding="utf-8"))
    write_json(public / "personal-memory-smoke.json", {
        "boundary": "Synthetic retrieval-only workflow check; not real-world QA accuracy",
        **{k: memory[k] for k in ("dataset_id", "dataset_sha256", "split", "backend", "summary")},
        "cases": len(memory["cases"]),
    })
    receipts = []
    for source in sources:
        data = json.loads(source.read_text(encoding="utf-8"))
        # These audited score files contain aggregate counts/hashes, never input passages.
        name = source.parent.name + "--" + source.name
        write_json(public / name, aggregate_only(data))
        receipts.append({"source": source.relative_to(ROOT).as_posix(), "sha256": sha(source.read_bytes()),
                         "published": name, "published_sha256": sha((public / name).read_bytes()),
                         "transformation": "Remove invalid examples and raw text fields; retain aggregate scores and fingerprints"})
    write_json(public / "report-provenance.json", receipts)
    with zipfile.ZipFile(folder / "public-experiment-reports.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(public.glob("*.json")):
            z.write(path, path.name)
        for name in ["充分性公开资源调研.md", "QASPER微调记录.md", "SetR重跑记录.md", "IC6提升调研.md", "RINSE第三阶段.md"]:
            z.write(ROOT / "docs" / name, name)

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    record = next(r for r in manifest if r["version"] == "0.3.11")
    for name in ("retained-development-bundles.zip", "public-experiment-reports.zip"):
        record["assets"][name] = sha((folder / name).read_bytes())
    write_json(output / "manifest.json", manifest)
    (folder / "SHA256SUMS.txt").write_text("".join(f"{value}  {key}\n" for key, value in record["assets"].items()), encoding="utf-8")
    notes = folder / "release-notes.md"
    body = notes.read_text(encoding="utf-8")
    marker = "\n## 补充归档\n"
    body = body.split(marker)[0] + marker + f"\n- retained-development-bundles.zip：{len(bundles)} 个原始中间工作流／后端包及逐文件哈希，保留历史字节与缓存；文件名不作为版本对应或验收证明。\n- public-experiment-reports.zip：公开充分性汇总／指纹、失败实验记录与合成记忆 smoke 汇总；不包含私人文献、数据库或模型权重。\n- 每个包的历史功能依据和边界见源码 docs/releases；所有新增 release 均为 prerelease。\n"
    notes.write_text(body, encoding="utf-8")
    print(json.dumps({"retained_bundles": len(bundles), "aggregate_reports": len(sources), "assets": record["assets"]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    prepare(parser.parse_args().output)
