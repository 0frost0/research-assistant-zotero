"""Real-corpus retrieval diagnostics; never calls an external generative model."""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research_assistant.storage.evidence_store import EvidenceStore
from research_assistant.retrieval.embeddings.remote_embeddings import RemoteQwenEmbeddings

QUERIES = ["论文中的医学流程图", "心脏结构示意图", "胸部X光片",
           "medical flowchart figure", "临床试验结果图表", "心衰是什么"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--instructions", action="store_true")
    args = parser.parse_args()
    started = time.perf_counter()
    if args.instructions:
        import numpy as np
        cached = np.load(ROOT / ".data" / "qwen_sample_vectors.npz")
        matrix, text_count = cached["matrix"], int(cached["text_count"])
        model = RemoteQwenEmbeddings()
        if str(cached["fingerprint"]) != model.profile["fingerprint"]:
            raise RuntimeError("Sample embeddings use a different model")
        for instruction in (
            "Retrieve relevant documents for the query.",
            "Given a search query, retrieve relevant text passages, figures, or tables from scientific papers.",
            "Retrieve an image that matches the given description.",
        ):
            results = []
            for query in QUERIES:
                vector = model._embed([{"text": query, "instruction": instruction}])[0]
                ranks = np.argsort(-(matrix @ np.asarray(vector)))
                results.append({"query": query, "first_image_rank": next(
                    (i + 1 for i, n in enumerate(ranks) if n >= text_count), None)})
            print(json.dumps({"instruction": instruction, "results": results}, ensure_ascii=False), flush=True)
        return
    if args.full:
        from research_assistant.retrieval.knowledge_base import LiteratureIndex
        index = LiteratureIndex(ROOT / "library", backend="multimodal")
        if index.unified_index is None:
            raise RuntimeError(index.unified_error)
        unified = index.unified_index
        report = {"model": unified.model_name, "dimension": unified.dimension,
                  "collection": unified.collection_name,
                  "points": unified.client.count(unified.collection_name, exact=True).count,
                  "sqlite": unified.store.stats(), "bge_loaded": index.embeddings is not None,
                  "build_seconds": round(time.perf_counter() - started, 2), "queries": []}
        for query in QUERIES:
            begin = time.perf_counter()
            hits = unified.search(query, k=20, min_score=0)
            displayed = index.search_results(query, k=5, min_score=0.35)
            report["queries"].append({"query": query,
                "seconds": round(time.perf_counter() - begin, 2),
                "first_image_rank": next((i + 1 for i, h in enumerate(hits) if h.modality == "image"), None),
                "top10": [{"modality": h.modality, "source": h.source, "page": h.page,
                           "score": round(h.score, 4), "id": h.point_id} for h in hits[:10]],
                "displayed_modalities": [h.modality for h in displayed]})
        print(json.dumps(report, ensure_ascii=False), flush=True)
        (ROOT / "benchmarks" / "unified_qwen_results.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        unified.client.close()
    else:
        import numpy as np
        store = EvidenceStore(ROOT / ".data" / "evidence.sqlite3")
        candidates = store.active_image_candidates()
        with sqlite3.connect(ROOT / ".data" / "evidence.sqlite3") as con:
            rows = con.execute("SELECT text FROM evidence WHERE active=1 ORDER BY source, ordinal").fetchall()
        texts = [row[0] for row in rows[::max(1, len(rows) // 132)]][:132]
        model = RemoteQwenEmbeddings()
        print(f"Sample: {len(texts)} text blocks, {len(candidates)} images", flush=True)
        text_vectors = model.embed_texts(texts)
        print("Sample text encoding complete", flush=True)
        image_vectors = model.embed_images([Path(item.image_path) for item in candidates],
                                          captions=[item.caption for item in candidates])
        matrix = np.asarray(text_vectors + image_vectors)
        np.savez(ROOT / ".data" / "qwen_sample_vectors.npz", matrix=matrix,
                 text_count=len(texts), fingerprint=model.profile["fingerprint"])
        results = []
        for query in QUERIES:
            scores = matrix @ np.asarray(model.embed_query(query))
            ranks = np.argsort(-scores)
            results.append({"query": query,
                "first_image_rank": next((i + 1 for i, n in enumerate(ranks) if n >= len(texts)), None),
                "top10_modalities": ["text" if n < len(texts) else "image" for n in ranks[:10]],
                "max_text": round(float(max(scores[:len(texts)])), 4),
                "max_image": round(float(max(scores[len(texts):])), 4)})
        print(json.dumps({"seconds": round(time.perf_counter() - started, 2), "queries": results},
                         ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
