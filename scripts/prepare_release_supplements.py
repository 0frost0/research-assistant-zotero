"""Package explicitly selected public experiment receipts.

Run after archive_github_releases.py prepare, before committing and publishing.
No model calls, training, private datasets, or model weights are included.
"""
import argparse
import hashlib
import json
from pathlib import Path
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
    public = ROOT / "docs" / "releases" / "artifacts"

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
    name = "public-experiment-receipts.zip"
    with zipfile.ZipFile(folder / name, "w", zipfile.ZIP_DEFLATED) as z:
        names = ["personal-memory-smoke.json", "report-provenance.json", *(r["published"] for r in receipts)]
        for receipt in sorted(names):
            z.write(public / receipt, receipt)

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    record = next(r for r in manifest if r["version"] == "0.3.11")
    record["assets"][name] = sha((folder / name).read_bytes())
    write_json(output / "manifest.json", manifest)
    (folder / "SHA256SUMS.txt").write_text("".join(f"{value}  {key}\n" for key, value in record["assets"].items()), encoding="utf-8")
    notes = folder / "release-notes.md"
    body = notes.read_text(encoding="utf-8")
    marker = "\n## 补充归档\n"
    body = body.split(marker)[0] + marker + "\n- public-experiment-receipts.zip：公开充分性汇总、指纹与合成记忆检索结果。\n"
    notes.write_text(body, encoding="utf-8")
    print(json.dumps({"aggregate_reports": len(sources), "assets": record["assets"]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    prepare(parser.parse_args().output)
