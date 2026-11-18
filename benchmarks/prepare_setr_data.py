"""Freeze original MultiHopRAG questions, corpus-derived BM25 pool; no gold injection.

Chunking is whitespace-word 256/64, NOT an assertion of exact SetR retrieval.
External test questions are never used for distillation or model selection.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.setr_protocol import clean, digest, evidence_sets, normalize, PROTOCOL
from research_assistant.retrieval.hybrid_retrieval import BM25Index


def corpus_chunks(corpus, *, size=256, overlap=64):
    if not 0 <= overlap < size:
        raise ValueError("invalid chunk overlap")
    result = []
    for doc in corpus:
        body = clean(doc["body"])
        words = list(re.finditer(r"\S+", body))
        source = digest(doc["url"])
        metadata = "\n".join(f"{k}: {clean(str(doc.get(k) or ''))}" for k in ("title", "source", "published_at"))
        for start in range(0, len(words), size - overlap):
            end = min(start + size, len(words))
            text = body[words[start].start():words[end - 1].end()]
            result.append({"id": digest([source, start, end]), "source_id": source, "text": metadata + "\n\n" + text,
                           "word_start": start, "word_end": end})
            if end == len(words):
                break
    return result


def build(questions, corpus):
    chunks = corpus_chunks(corpus)
    index = BM25Index([{"point_id": c["id"], "text": c["text"], "source": c["source_id"], "file_type": ".txt"} for c in chunks])
    lookup = {c["id"]: c for c in chunks}
    groups = []
    for row in questions:
        hits = index.search(row["query"], k=20)
        candidates = [lookup[h["point_id"]] for h in hits]
        if len(candidates) != 20:
            raise ValueError("pool is not Top-20")
        groups.append({"id": digest(row["query"]), "question": row["query"], "question_type": row["question_type"],
                       "candidates": candidates, "gold": row["evidence_list"]})
    if len({g["id"] for g in groups}) != len(groups):
        raise ValueError("duplicate test questions")
    corpus_text = "\n".join(normalize(d["body"]) for d in corpus)
    all_facts = {normalize(e["fact"]) for q in questions for e in q["evidence_list"]}
    unmatched = sorted(digest(f) for f in all_facts if f not in corpus_text)
    return {"protocol": PROTOCOL, "dataset": "MultiHopRAG", "role": "external_test_only",
            "retriever": "project BM25, k1=1.2,b=0.75, no gold injection",
            "chunking": {"unit": "whitespace_words", "size": 256, "overlap": 64, "metadata": ["title", "source", "published_at"]},
            "exact_paper_reproduction": False, "questions": len(groups), "chunks": len(chunks), "documents": len(corpus),
            "question_types": dict(Counter(g["question_type"] for g in groups)),
            "matching": "ftfy then remove whitespace; literal case-sensitive gold-span containment, distinct union",
            "unmatched_gold_fact_hashes_in_corpus": unmatched, "groups": groups}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--raw-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    q = json.loads((a.raw_dir / "MultiHopRAG.json").read_bytes())
    c = json.loads((a.raw_dir / "corpus.json").read_bytes())
    data = build(q, c)
    import hashlib
    data["raw_sha256"] = {name: hashlib.sha256((a.raw_dir / name).read_bytes()).hexdigest() for name in ("MultiHopRAG.json", "corpus.json", "README.md")}
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open("x", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(json.dumps({k: v for k, v in data.items() if k != "groups"}, indent=2))
