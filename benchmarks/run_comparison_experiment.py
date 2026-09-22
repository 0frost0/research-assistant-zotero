"""Controlled concurrency experiments; simulations measure orchestration, not LLM quality."""

import argparse
import asyncio
import hashlib
import json
import math
import os
import statistics
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_assistant.agents.comparison_workflow import ComparisonWorkflow, call_model


class EvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(pattern=r"^D[1-4]E[1-8]$")
    source: str = Field(min_length=1, max_length=240)
    page: int | None = Field(default=None, ge=1)
    quote: str = Field(max_length=1800)


class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=240)
    evidence: list[EvidenceInput] = Field(max_length=8)
    error: None = None


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    question: str = Field(min_length=1, max_length=3000)
    documents: list[DocumentInput] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def coherent_evidence(self):
        sources, refs = set(), set()
        for doc in self.documents:
            if doc.source in sources or sum(len(e.quote) for e in doc.evidence) > 6000:
                raise ValueError("Repeated source or excessive document context")
            sources.add(doc.source)
            for evidence in doc.evidence:
                if evidence.ref in refs or evidence.source != doc.source:
                    raise ValueError("Ambiguous evidence reference or wrong source")
                refs.add(evidence.ref)
        return self


def synthetic_cases():
    documents = [{"source": f"fixture{i}.txt", "error": None, "evidence": [{
        "ref": f"D{i}E1", "source": f"fixture{i}.txt", "page": 1,
        "quote": f"Synthetic study {i} uses its own evaluation setting."}]} for i in range(1, 5)]
    missing = [{**d, "evidence": []} for d in documents[:2]]
    return [Case(id="four_papers", question="Compare evaluation settings.", documents=documents),
            Case(id="missing_evidence", question="Compare unavailable outcomes.", documents=missing)]


async def simulated_call(role, payload):
    await asyncio.sleep(.15 if role == "analyst" else .04)
    if role == "analyst":
        return {"findings": [{"aspect": "setting", "statement": "This study uses its own evaluation setting.",
                              "evidence_ids": [payload["document"]["evidence"][0]["ref"]]}], "gaps": []}
    if role == "synthesizer":
        return {"comparisons": [{"aspect": "setting", "relation": "incomparable",
                                  "statement": "These settings require alignment before ranking outcomes.",
                                  "evidence_ids": ["D1E1", "D2E1"]}]}
    return {"verdicts": [{"claim_index": 0, "verdict": "supported", "reason": "Simulated verdict, not a quality label."}]}


def percentile(values, fraction):
    """Nearest-rank percentile; a tiny sample's p95 is descriptive, not an SLA."""
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def authorize_live(args, cases):
    upper_bound = sum(len(c.documents) + 2 for c in cases) * args.repeats * 2
    if not args.dataset or not args.allow_external or args.max_model_calls < upper_bound:
        raise ValueError(f"Live mode requires --dataset, --allow-external and --max-model-calls >= {upper_bound}")
    return upper_bound


async def experiment(cases, caller, repeats):
    # A warm-up is safe only with a simulated caller, never implicitly spend a live request.
    rows = []
    for repeat in range(repeats):
        # Alternate order to reduce systematic cold/warm ordering bias.
        for concurrency in ((1, 4) if repeat % 2 == 0 else (4, 1)):
            for case in cases:
                try:
                    result = await asyncio.wait_for(ComparisonWorkflow(caller, analyst_concurrency=concurrency).run(
                        case.question, [d.model_dump() for d in case.documents]), timeout=240)
                    trace = result["telemetry"]
                    rows.append({"case_id": case.id, "repeat": repeat + 1, "concurrency": concurrency,
                                 "status": result["status"], "summary": trace["summary"],
                                 "prompt_sha256": trace["prompt_sha256"], "workflow_version": trace["workflow_version"],
                                 "accepted_comparisons": len(result["comparisons"]), "withheld_comparisons": len(result["withheld"]),
                                 "analyses_ok": sum(a["status"] == "ok" for a in result["analyses"]),
                                 "trace": trace})
                except Exception as exc:
                    # Keep failed rows visible, never drop them to improve latency statistics.
                    rows.append({"case_id": case.id, "repeat": repeat + 1, "concurrency": concurrency,
                                 "status": "execution_failed", "error_type": type(exc).__name__})
    groups = []
    for case in cases:
        for concurrency in (1, 4):
            group = [r for r in rows if r["case_id"] == case.id and r["concurrency"] == concurrency]
            completed = [r for r in group if "summary" in r]
            times = [r["summary"]["wall_ms"] for r in completed]
            groups.append({"case_id": case.id, "concurrency": concurrency, "attempts": len(group),
                           "measured_runs": len(completed), "degraded_runs": sum(r["status"] == "degraded" for r in group),
                           "execution_failed_runs": len(group) - len(completed),
                           "p50_ms": statistics.median(times) if times else None,
                           "p95_ms": percentile(times, .95) if times else None,
                           "model_calls": [r["summary"]["model_calls"] for r in completed],
                           "peak_concurrency": [r["summary"]["peak_model_concurrency"] for r in completed]})
    return rows, groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["simulated", "live"], default="simulated")
    parser.add_argument("--dataset", type=Path, help="Explicit JSON evidence snapshot: {cases: [...]} (live only)")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-external", action="store_true")
    parser.add_argument("--max-model-calls", type=int, default=0)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("repeats must be 1..10")
    if args.output.exists():
        parser.error("output already exists; choose a new filename to preserve experiment history")
    try:
        if args.mode == "live":
            raw = json.loads(args.dataset.read_text(encoding="utf-8")) if args.dataset else {}
            cases = [Case.model_validate(c) for c in raw.get("cases", [])]
            if not 1 <= len(cases) <= 20 or len({c.id for c in cases}) != len(cases):
                raise ValueError("dataset must contain 1..20 uniquely named cases")
            bound = authorize_live(args, cases)
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env")
            if not all(os.getenv(k) for k in ("OPENAI_MODEL", "OPENAI_API_KEY", "OPENAI_BASE_URL")):
                raise ValueError("Model configuration is incomplete")
            caller = call_model
        else:
            if args.dataset:
                raise ValueError("simulated mode uses synthetic cases only; do not mislabel real data")
            bound, cases, caller = 0, synthetic_cases(), simulated_call
        rows, groups = asyncio.run(experiment(cases, caller, args.repeats))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    dataset_hash = hashlib.sha256(json.dumps([c.model_dump() for c in cases], sort_keys=True).encode()).hexdigest()
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "mode": args.mode,
              "scope": "generation_on_frozen_evidence_only", "is_scientific_quality_evaluation": False,
              "dataset_sha256": dataset_hash, "max_live_model_calls": bound,
              "configured_model": os.getenv("OPENAI_MODEL") if args.mode == "live" else "simulated-fixed-delay",
              "versions": {p: version(p) for p in ("langgraph", "langchain-core", "langchain-openai")},
              "source_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
                                for p in ("research_assistant/agents/comparison_workflow.py", "research_assistant/core/agent_telemetry.py")},
              "percentile_method": "nearest_rank", "groups": groups, "runs": rows,
              "limitations": ["No retrieval, indexing or PDF parsing is timed.",
                              "Simulated timings do not predict provider latency, cost or scientific accuracy.",
                              "Acceptance by an AI reviewer is not a correctness label.",
                              "Small-sample p95 is descriptive, not a production SLA."]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"mode": args.mode, "groups": groups, "report": str(args.output.resolve())}, ensure_ascii=False, indent=2))
    return int(any(r["status"] == "execution_failed" for r in rows))


if __name__ == "__main__":
    raise SystemExit(main())
