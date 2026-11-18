"""Four retrieval/selection ablations, real Qwen/reranker, isolated memory Qdrant.

No answer-model calls. Synthetic corpora only; never touches production vectors.
Outputs explicitly distinguish failed/absent reranking from successful inference.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qdrant_client import QdrantClient
from research_assistant.core.service_context import stored_service_context
from benchmarks.evaluate_knowledge_qa import evaluate


async def run(args):
    if json.loads(args.dataset.read_text(encoding="utf-8")).get("split") != "synthetic":
        raise ValueError("This isolated smoke runner only accepts explicit synthetic data")
    client = QdrantClient(":memory:")
    try:
        with stored_service_context(args.profile_root, args.connection), patch(
                "research_assistant.retrieval.unified_index.QdrantClient", return_value=client):
            for algorithm in ("legacy", "hybrid", "hybrid_rerank", "hybrid_set"):
                options = argparse.Namespace(root=args.root, dataset=args.dataset,
                    expected_split="synthetic", allow_synthetic=True, case_ids=None,
                    max_cases=args.max_cases, backend="multimodal", with_model=False,
                    strategies=["adaptive"], sufficiency_gates=["off"], algorithm=algorithm,
                    output=args.output / (algorithm + ".json"))
                await evaluate(options)
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--profile-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--connection", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-cases", type=int, default=12)
    args = parser.parse_args()
    asyncio.run(run(args))
