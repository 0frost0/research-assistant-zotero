"""Generate CC0 synthetic scientific PDFs and optionally run a REAL translation.

Run with the isolated translation Python (PyMuPDF). No private paper contents.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def generate(destination):
    destination.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for number in range(2):
        page = doc.new_page(width=595, height=842)
        page.insert_text(
            (45, 48),
            "Evidence-guided Scientific Reading"
            if number == 0
            else "Experiments and Limitations",
            fontsize=19,
        )
        text = (
            "We evaluate retrieval over scientific papers. The baseline uses lexical matching. "
            "The proposed method preserves evidence from the original document. "
            "Personal notes are not equivalent to experimental results. "
            "The sample contains 200 documents and 50 questions. "
            "Accuracy is 0.82 and recall is 0.91 in this synthetic experiment [1]. "
            "These numbers are illustrative, not a real research finding. "
        )
        for left in (45, 310):
            remaining = page.insert_textbox(
                fitz.Rect(left, 85, left + 235, 390), (text + "\n\n") * 2, fontsize=10
            )
            assert remaining >= 0, (
                "Fixture text overflow; do not validate an empty column"
            )
        page.insert_text((45, 425), "E = m c^2     x = 0.25     n = 200", fontsize=13)
        page.insert_text(
            (45, 465),
            "Table 1. Synthetic comparison (no real measurements)",
            fontsize=11,
        )
        for row, values in enumerate(
            (
                "Method      Recall     Time (s)",
                "Baseline     0.70          2.5",
                "Proposed    0.91          3.0",
            )
        ):
            y = 490 + row * 24
            page.draw_rect(fitz.Rect(45, y - 15, 550, y + 8))
            page.insert_text((55, y), values, fontsize=11)
        page.draw_rect(fitz.Rect(60, 600, 255, 730), color=(0, 0, 0.8))
        page.draw_line((70, 710), (240, 620), color=(0, 0, 0.8))
        page.insert_text((310, 625), "Figure 1. Evidence coverage", fontsize=11)
        page.insert_textbox(
            fitz.Rect(310, 645, 545, 770),
            "Limitations: the sample is synthetic. It does not establish effectiveness on clinical papers. Manual review is required for formulas, units and translated tables.",
            fontsize=10,
        )
        page.insert_text(
            (45, 805),
            "[1] Example reference. Synthetic fixture released under CC0.",
            fontsize=9,
        )
    doc.save(destination / "scientific-en.pdf")
    doc.close()
    # UI fixture is deliberately NOT represented as a genuine engine translation.
    zh = fitz.open()
    for _ in range(2):
        page = zh.new_page(width=595, height=842)
        page.insert_font(fontname="china-s")
        page.insert_text(
            (45, 60),
            "科研阅读测试：人工构造的中文界面样本",
            fontname="china-s",
            fontsize=16,
        )
        page.insert_textbox(
            fitz.Rect(45, 90, 285, 500),
            "个人笔记不是论文结论。我们需要核对原始证据。\n模型方法依赖检索质量，我的疑问是小样本是否可靠。\n"
            * 6,
            fontname="china-s",
            fontsize=12,
        )
        page.insert_textbox(
            fitz.Rect(310, 90, 550, 500),
            "这是右侧分栏。跨栏选区应该拆开保存。\n数值公式需要逐项人工核对，不能只看文件生成。\n"
            * 6,
            fontname="china-s",
            fontsize=12,
        )
    zh.save(destination / "synthetic-zh-ui-only.pdf")
    zh.close()
    print(json.dumps({"fixtures": str(destination), "synthetic": True}))


async def translate(destination):
    from dotenv import load_dotenv

    for p in (ROOT / ".env", ROOT.parent / ".env", ROOT.parent / "langchain/.env"):
        if p.exists():
            load_dotenv(p)
            break
    key = os.getenv("TRANSLATION_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = os.getenv("TRANSLATION_MODEL") or os.getenv("OPENAI_MODEL")
    base = os.getenv("TRANSLATION_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    if not all((key, model, base)):
        print("Configuration missing; no real translation attempted.")
        return
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ROOT / "deployment/translation/worker.py"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    request = {
        "input": str(destination / "scientific-en.pdf"),
        "output": str(destination / "real"),
        "versions": {"pdf2zh-next": "2.9.0", "BabelDOC": "0.6.2", "PyMuPDF": "1.25.2"},
        "key": key,
        "model": model,
        "base_url": base,
    }
    # Direct acceptance scripts must use the same durable request gate as the UI.
    from research_assistant.translation.ledger import TranslationLedger
    import hashlib
    ledger = TranslationLedger(destination / "request-budget.sqlite3")
    job_id = hashlib.sha256((model + base + str(destination.resolve())).encode()).hexdigest()
    ledger.account(job_id, 10000, 10)
    ledger.recover()
    attempt = len(ledger.summary(job_id)["attempts"]) + 1
    ledger.start(job_id, attempt)
    request["ledger"] = {"database": ledger.path, "job_id": job_id, "attempt": attempt,
                         "endpoint_hash": hashlib.sha256(base.encode()).hexdigest()}
    proc.stdin.write((json.dumps(request) + "\n").encode())
    await proc.stdin.drain()
    proc.stdin.close()
    while line := await proc.stdout.readline():
        print(line.decode().strip(), flush=True)
    print("worker_exit", await proc.wait())
    ledger.recover(job_id, "finished")
    print(json.dumps({"budget": ledger.summary(job_id)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--translate", action="store_true")
    parser.add_argument(
        "--paper",
        type=Path,
        help="Translate ONLY the first two pages as a labelled real-paper acceptance excerpt",
    )
    parser.add_argument("--output", type=Path, help="New isolated output directory; preserve previous runs")
    args = parser.parse_args()
    output = args.output or ROOT / "output/pdf/reading-sample"
    if args.paper:
        output = args.output or ROOT / "output/pdf/reading-paper-excerpt"
        output.mkdir(parents=True, exist_ok=True)
        with fitz.open(args.paper) as source:
            excerpt = fitz.open()
            excerpt.insert_pdf(source, from_page=0, to_page=min(1, len(source) - 1))
            excerpt.save(output / "scientific-en.pdf")
            excerpt.close()
        print("Acceptance excerpt: first two pages only; NOT a full-paper translation.")
    else:
        generate(output)
    if args.translate:
        asyncio.run(translate(output))
