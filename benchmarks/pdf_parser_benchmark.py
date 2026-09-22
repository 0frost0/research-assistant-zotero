"""Utilities for comparing PDF parser outputs on the same page range."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import pypdf


def prepare_excerpt(source: Path, output: Path, start: int, end: int) -> None:
    """Create a PDF excerpt using one-based inclusive page numbers."""
    reader = pypdf.PdfReader(source)
    if start < 1 or end < start or end > len(reader.pages):
        raise ValueError(f"Invalid page range {start}-{end}; PDF has {len(reader.pages)} pages")

    writer = pypdf.PdfWriter()
    for page_index in range(start - 1, end):
        writer.add_page(reader.pages[page_index])
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as file:
        writer.write(file)


def text_metrics(text: str) -> dict[str, int]:
    """Return format-oriented metrics that are comparable across parsers."""
    lines = text.splitlines()
    return {
        "characters": len(text),
        "words": len(re.findall(r"[A-Za-z]+(?:[-'][A-Za-z]+)*", text)),
        "headings": sum(bool(re.match(r"^#{1,6}\s+", line)) for line in lines),
        "markdown_table_rows": sum(
            line.strip().startswith("|") and line.strip().endswith("|") for line in lines
        ),
        "image_references": len(re.findall(r"!\[[^]]*]\([^)]+\)", text)),
        "math_delimiters": text.count("$$") + len(re.findall(r"(?<!\$)\$(?!\$)", text)),
        "replacement_characters": text.count("\ufffd"),
    }


def run_current(source: Path, output_dir: Path, project_dir: Path) -> None:
    """Run the research assistant's current pypdf/Tesseract extractor."""
    sys.path.insert(0, str(project_dir.resolve()))
    from research_assistant.ingestion.pdf_extractor import PDFExtractor

    started = time.perf_counter()
    documents, report = PDFExtractor(enable_ocr=True).extract(source)
    elapsed = time.perf_counter() - started

    text = "\n\n".join(
        f"<!-- page {doc.metadata['page']} -->\n\n{doc.page_content}"
        for doc in documents
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "content.md").write_text(text, encoding="utf-8")
    (output_dir / "extraction_report.json").write_text(
        json.dumps(report.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    metrics = {
        "backend": "current_pypdf_tesseract",
        "elapsed_seconds": round(elapsed, 3),
        "pages": report.total_pages,
        "native_pages": report.native_pages,
        "ocr_pages": report.ocr_pages,
        **text_metrics(text),
    }
    (output_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


def summarize_output(backend: str, output_dir: Path, elapsed: float | None) -> None:
    """Summarize Markdown and image artifacts produced by an external parser."""
    markdown_files = sorted(output_dir.rglob("*.md"))
    text = "\n\n".join(path.read_text(encoding="utf-8") for path in markdown_files)
    metrics = {
        "backend": backend,
        "elapsed_seconds": round(elapsed, 3) if elapsed is not None else None,
        "markdown_files": len(markdown_files),
        "json_files": len(list(output_dir.rglob("*.json"))),
        "extracted_images": len(
            [
                path
                for path in output_dir.rglob("*")
                if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            ]
        ),
        **text_metrics(text),
    }
    (output_dir / "benchmark_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("source", type=Path)
    prepare.add_argument("output", type=Path)
    prepare.add_argument("--start", type=int, default=1)
    prepare.add_argument("--end", type=int, default=10)

    current = subparsers.add_parser("current")
    current.add_argument("source", type=Path)
    current.add_argument("output_dir", type=Path)
    current.add_argument("--project-dir", type=Path, required=True)

    summarize = subparsers.add_parser("summarize")
    summarize.add_argument("backend")
    summarize.add_argument("output_dir", type=Path)
    summarize.add_argument("--elapsed", type=float)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "prepare":
        prepare_excerpt(args.source, args.output, args.start, args.end)
    elif args.command == "current":
        run_current(args.source, args.output_dir, args.project_dir)
    else:
        summarize_output(args.backend, args.output_dir, args.elapsed)


if __name__ == "__main__":
    main()
