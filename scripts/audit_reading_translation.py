"""Local-only PDF acceptance metrics; generation is not semantic approval.

Use the existing application Python. Prints metadata, not paper passages or keys.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pypdf import PdfReader
from research_assistant.translation.service import validate_output, VERSIONS


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(original, translated):
    before, after = PdfReader(original), PdfReader(translated)
    pages = []
    for i, (a, b) in enumerate(zip(before.pages, after.pages)):
        source, target = a.extract_text() or "", b.extract_text() or ""
        numbers = lambda text: Counter(re.findall(r"(?<![\d.])\d+(?:\.\d+)?", text))
        src, dst = numbers(source), numbers(target)
        pages.append({
            "page": i + 1,
            "source_characters": len(source),
            "translated_characters": len(target),
            "selectable_chinese_characters": len(re.findall(r"[\u4e00-\u9fff]", target)),
            "missing_numeric_tokens": sorted(set(src) - set(dst)),
            "added_numeric_tokens": sorted(set(dst) - set(src)),
            "numeric_occurrence_differences": {n: {"original": src[n], "translation": dst[n]} for n in sorted(set(src) | set(dst)) if src[n] != dst[n]},
            "replacement_characters": target.count("\ufffd"),
        })
    try:
        validation = validate_output(original, translated)
    except ValueError as exc:
        validation = {"automated_checks": "failed", "error": str(exc)}
    return {
        "original_sha256": fingerprint(original),
        "translation_sha256": fingerprint(translated),
        "original_page_count": len(before.pages),
        "translation_page_count": len(after.pages),
        "engine_versions": VERSIONS,
        "validation": validation,
        "pages": pages,
        "review_status": "requires visual and semantic review; numbers alone do not prove fidelity",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("original", type=Path)
    parser.add_argument("translation", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.original, args.translation)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
