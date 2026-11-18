"""Synthetic, isolated browser acceptance server. Never loads the production app."""
import argparse
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles
from research_assistant.web.knowledge_api import KnowledgeAPI
from research_assistant.core.paths import STATIC_DIR
from research_assistant.knowledge.store import digest
from research_assistant.core.service_config import ServiceConfigStore
from tests.test_reading_memory import pdf

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8771)
parser.add_argument("--qa-scripted", action="store_true", help="Deterministic model double, not a quality evaluation")
parser.add_argument("--qa-live", action="store_true", help="Explicitly call the configured real model on isolated fixture data")
parser.add_argument("--model-connection", default="")
parser.add_argument("--visual-fixture", action="store_true", help="Synthetic pixels + local fake embeddings; scripted only")
parser.add_argument("--visual-live", action="store_true", help="Only visual answer/review use real model; hard limit 2 calls")
parser.add_argument("--root", type=Path)
parser.add_argument("--backend", choices=["tfidf", "multimodal"], default="tfidf")
args = parser.parse_args()
if args.qa_live and (args.qa_scripted or not args.model_connection):
    parser.error("--qa-live requires --model-connection and cannot use --qa-scripted")
if args.visual_fixture and (not args.qa_scripted or args.backend != "tfidf"):
    parser.error("--visual-fixture requires --qa-scripted --backend tfidf (no paid calls)")
if args.visual_live and (not args.visual_fixture or not args.model_connection or args.qa_live):
    parser.error("--visual-live requires --visual-fixture --qa-scripted --model-connection")
temporary = None if args.root else tempfile.TemporaryDirectory(prefix="research-knowledge-fixture-")
root = args.root.resolve() if args.root else Path(temporary.name)
root.mkdir(parents=True, exist_ok=True)
api = KnowledgeAPI(root, token="synthetic-acceptance-token", backend=args.backend)
if args.visual_fixture:
    from benchmarks.visual_fixture import visual_service, seed_visual_report, taller_color
    api._service = visual_service(api.store)
    api._service_configs._write({"fixture": {"llm": {"base_url": "https://fixture.invalid/v1",
        "model": "synthetic-pixel-double", "api_key": "not-a-real-key", "supports_images": True}}})
if args.visual_live:
    from benchmarks.visual_fixture import BoundedVisualCaller
    from benchmarks.check_visual_readiness import inspect_config
    from research_assistant.knowledge.qa import ModelCaller
    config = ServiceConfigStore(Path(__file__).resolve().parents[1]).get(args.model_connection)
    if not inspect_config(config)["live_eligible"]:
        raise ValueError("Live visual model must be configured and explicitly image-enabled")
    # No embedding/MinerU credentials or live retrieval services in this fixture.
    api._service_configs._write({"fixture": {"llm": config["llm"]}})
    visual_caller = BoundedVisualCaller(ModelCaller(), root / "visual-live-usage.json")
if args.qa_live:
    config = ServiceConfigStore(Path(__file__).resolve().parents[1]).get(args.model_connection)
    if not all(config.get("llm", {}).get(k) for k in ("base_url", "model", "api_key")):
        raise ValueError("Live acceptance model is not configured")
    api._service_configs._write({"fixture": config})
if args.qa_scripted:
    from research_assistant.knowledge.qa import CombinedQA, EvidenceQA, MemoryQA

    async def scripted(role, payload, timeout):
        if role == "sufficiency":
            # Deterministic contract fixture only, never a scientific judgment.
            # Live visual acceptance remains strictly answer+review (2 calls).
            if args.visual_live:
                raise ValueError("native_live_sufficiency_not_enabled")
            candidates = payload["evidence"]
            sufficient = bool(candidates) and payload["question"] != "fixture missing condition"
            return {"sufficient": sufficient,
                    "evidence_ids": [e["evidence_id"] for e in candidates] if sufficient else [],
                    "gaps": [] if sufficient else ["synthetic condition missing"],
                    "followup_query": "synthetic missing condition" if not sufficient else ""}, {}
        if payload.get("multimodal"):
            if args.visual_live:
                return await visual_caller(role, payload, timeout)
            pixels = payload.get("_visual_inputs", [])
            color = taller_color(pixels[0]["data_url"]) if pixels else None
            if role == "answer":
                return {"claims": [{"text": color, "citations": [{"evidence_id": pixels[0]["evidence_id"],
                    "citation_type": "visual", "observation": color}]}] if color else []}, {}
            return {"question_relevant": bool(color), "verdicts": [
                {"claim_index": i, "supported": claim["text"] == color and all(
                    c.get("observation") == color for c in claim["citations"]), "reason": "synthetic pixel check"}
                for i, claim in enumerate(payload["claims"])]}, {}
        if role == "review":
            return {"question_relevant": True,
                    "verdicts": [{"claim_index": i, "supported": True, "reason": "fixture"}
                                 for i in range(len(payload["claims"]))]}, {"input_tokens": 10, "output_tokens": 10}
        candidates = payload["evidence"]
        return {**({"question_relevant": True} if payload.get("memory_mode") else {}),
                "claims": [{"text": "合成资料：检索证据需要核验。", "citations": [
            {"evidence_id": candidates[0]["evidence_id"], "quote": candidates[0]["quote"]}]}] if candidates else []}, {"input_tokens": 10, "output_tokens": 10}

    class FixtureQA:
        """Allow the native fixture to exercise a failed durable job safely."""

        def __init__(self, delegate):
            self.delegate = delegate
            self.lock = delegate.lock

        async def ask(self, data):
            if data.get("query") == "fixture failure":
                raise RuntimeError("fixture failure")
            return await self.delegate.ask(data)

    api._qa = FixtureQA(EvidenceQA(api.service, caller=scripted, configured=lambda: True))
    api._memory_qa = MemoryQA(api.service, caller=scripted, configured=lambda: True)
    # The production ``both`` path deliberately uses separate answer objects;
    # give both halves the same deterministic fixture caller without enabling
    # any real model or network request.
    api._combined_qa = CombinedQA(
        api.service,
        evidence=EvidenceQA(api.service, caller=scripted, configured=lambda: True),
        memory=MemoryQA(api.service, caller=scripted, configured=lambda: True),
    )
elif args.qa_live:
    from research_assistant.knowledge.qa import EvidenceQA

    class LiveFixtureQA:
        def __init__(self, delegate):
            self.delegate, self.lock = delegate, delegate.lock

        async def ask(self, data):
            if data.get("query") == "fixture failure":
                raise RuntimeError("fixture failure")
            return await self.delegate.ask(data)

    api._qa = LiveFixtureQA(EvidenceQA(api.service))
source = root / "synthetic.pdf"
if args.visual_fixture:
    from benchmarks.visual_fixture import write_visual_pdf
    write_visual_pdf(source)
else:
    pdf(source)
content = source.read_bytes()
sha = digest(content)
api.store.put_object(sha, content)
if args.visual_fixture:
    seed_visual_report(api.store, sha)
for key, name, entities in [
    ("A", "检索方法", [{"key": "PDF00001", "kind": "document", "title": "Scientific Evidence", "hash": sha},
                    {"key": "NOTE0001", "kind": "note", "title": "阅读记录", "body": "检索结果必须回到原文核验。Scientific evidence requires validation."}]),
    ("B", "长期研究计划与跨学科证据评估", [{"key": "NOTE0002", "kind": "note", "title": "独立笔记", "body": "独立库只讨论时间安排与实验记录。"}]),
]:
    base = api.store.create({"connection": "fixture", "library": "user:0", "collection": key, "name": name})
    api.store.sync(base["id"], {"name": name, "include_children": True, "complete": True,
                               "entities": entities, "expected_revision": 0})
app = Starlette(routes=api.routes() + [Mount("/static", StaticFiles(directory=STATIC_DIR))])
if __name__ == "__main__":
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        if temporary:
            temporary.cleanup()
