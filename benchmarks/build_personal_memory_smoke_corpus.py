"""Build a tiny isolated Chinese personal-memory corpus and its eval set."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_reading_memory import pdf
from research_assistant.knowledge.store import KnowledgeStore, digest


def build(root):
    root.mkdir(parents=True, exist_ok=False)
    store = KnowledgeStore(root)
    base = store.create({"connection": "memory-smoke", "library": "user:0", "collection": "A", "name": "中文记忆 smoke"})
    paper = root / "synthetic.pdf"
    pdf(paper)
    content = paper.read_bytes()
    sha = digest(content)
    store.put_object(sha, content)
    store.sync(base["id"], {"name": base["name"], "include_children": False, "complete": True,
                             "expected_revision": 0, "entities": [{"key": "PDF00001", "kind": "document",
                             "title": "合成论文", "hash": sha}]})
    first = store.save_note(base["id"], {"type": "understanding", "title": "方法理解",
        "body": "我理解：这个方法适合小样本验证，但外推到长期随访前还需要额外证据。",
        "confirmed": True, "anchors": [{"source": sha + ".pdf", "page": 1,
        "quote": "Scientific evidence from original paper.", "kind": "original",
        "url": "zotero://open-pdf/library/items/PDF00001?page=1"}]})
    current = store.save_note(base["id"], {"expected_revision": first["revision"], "type": "understanding",
        "title": "方法理解", "body": "我修正理解：这个方法适合小样本验证，但长期外推仍需额外证据。",
        "confirmed": True}, first["id"])
    archived = store.save_note(base["id"], {"type": "question", "title": "已解决疑问",
        "body": "旧问题：作者是否报告了长期随访？", "confirmed": True})
    store.save_note(base["id"], {"expected_revision": archived["revision"], "title": archived["title"],
        "body": archived["body"], "type": archived["type"], "confirmed": archived["confirmed"], "archived": True}, archived["id"])
    dataset = {"id": "personal-memory-smoke-v1", "split": "test", "cases": [
        {"id": "M1", "query": "我之前如何理解这个方法的适用范围？", "kb_ids": [base["id"]],
         "expected": [{"entity_id": current["id"], "revision": current["revision"], "quote": "修正理解"}], "no_answer": False},
        {"id": "M2", "query": "小样本验证和长期外推有什么关系？", "kb_ids": [base["id"]],
         "expected": [{"entity_id": current["id"], "revision": current["revision"], "quote": "长期外推"}], "no_answer": False},
        {"id": "M3", "query": "作者报告了长期随访吗？", "kb_ids": [base["id"]],
         "expected": [], "no_answer": True},
        {"id": "M4", "query": "量子纠缠实验的误差是多少？", "kb_ids": [base["id"]],
         "expected": [], "no_answer": True},
    ]}
    path = root / "dataset.json"
    path.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"root": str(root), "dataset": str(path), "kb_id": base["id"], "note_revision": current["revision"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    build(parser.parse_args().root)
