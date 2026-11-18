"""Exercise the actual authenticated Zotero API with an explicit case/call budget.

Reports operational and citation-anchor checks, never human semantic accuracy.
Paper excerpts and credentials stay out of the report. Saved QA jobs remain
available in the workbench history. No generation unless --with-model.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_assistant.knowledge.qa import normalized


def assess(case, result):
    claims = result.get("claims", [])
    expected = case.get("expected", [])
    matched = []
    for gold in expected:
        matched.append(any(
            (not gold.get("source_id") or c.get("source_id") == gold["source_id"])
            and (not gold.get("entity_id") or c.get("entity_id") == gold["entity_id"])
            and (gold.get("page") is None or c.get("page") == gold["page"])
            and (gold.get("revision") is None or c.get("revision") == gold["revision"])
            and (not gold.get("quote") or normalized(gold["quote"]) in
                 normalized(c.get("support_quote", c.get("quote", ""))))
            for claim in claims for c in claim.get("citations", [])))
    status = result.get("status")
    completed = status in {"answered", "partial", "no_evidence"}
    return {"status": status, "claims": len(claims), "completed": completed,
            "expected_anchor_hits": sum(matched), "expected_anchors": len(expected),
            "expected_behavior_observed": (
                status == "no_evidence" and not claims if case.get("no_answer") else
                completed and bool(claims) and all(matched)),
            "human_semantic_review": None}


def run(args):
    raw = args.cases.read_bytes()
    dataset = json.loads(raw)
    cases = dataset["cases"][:args.max_cases]
    token = (args.root / ".data/zotero/connection-token").read_text().strip()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {"Authorization": "Bearer " + token, "X-Research-Connection": args.connection,
               "Content-Type": "application/json"}

    def request(route, payload=None, method=None):
        req = urllib.request.Request(args.endpoint.rstrip("/") + "/api/knowledge/" + route,
                                     data=json.dumps(payload).encode() if payload is not None else None,
                                     headers=headers, method=method)
        with opener.open(req, timeout=20) as response:
            return json.load(response)

    # Only retry the read-only readiness check. Never replay a paid submission.
    deadline = time.monotonic() + 20
    while True:
        try:
            request("status")
            break
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)

    report = {"dataset_id": dataset["id"], "dataset_sha256": hashlib.sha256(raw).hexdigest(),
              "provenance": dataset.get("provenance"), "with_model": args.with_model,
              "max_model_calls": args.max_model_calls, "cases": [], "human_semantic_accuracy": None}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    used_calls = 0
    for case in cases:
        scope = case.get("scope", "original")
        ceiling = 5 if scope == "both" else 2 if scope == "notes" else 4
        if args.with_model and used_calls + ceiling > args.max_model_calls:
            report["budget_exhausted"] = True
            break
        payload = {k: case[k] for k in ("query", "kb_ids", "scope") if k in case}
        start = time.monotonic()
        if args.with_model:
            job = request("ask-async", {**payload, "conversation_id": "acceptance-real-library"})
            deadline = time.monotonic() + 210
            while job["state"] in {"queued", "running"}:
                if time.monotonic() >= deadline:
                    request("jobs/" + job["job_id"], {"action": "cancel"}, "POST")
                    raise TimeoutError("Acceptance job cancelled after its deadline")
                time.sleep(1)
                job = request("jobs/" + job["job_id"])
            result = job.get("result") or {"status": "unavailable", "claims": []}
            trace = result.get("trace", {})
            used_calls += trace.get("model_calls", 0)
            row = {"id": case["id"], "scope": scope, "job_id": job["job_id"],
                   "job_state": job["state"], **assess(case, result),
                   "model_calls": trace.get("model_calls", 0),
                   "retrieval_calls": trace.get("retrieval_calls", 0),
                   "known_total_tokens": trace.get("total_tokens", 0),
                   "unknown_usage_calls": trace.get("unknown_usage_calls", 0),
                   "models": sorted({v for v in [trace.get("model"), trace.get("review_model")] if v}),
                   "retrieval_backend": trace.get("retrieval_backend"),
                   "revisions": result.get("revisions", {})}
        else:
            result = request("search", {**payload, "k": 20, "text_only": scope != "notes"})
            row = {"id": case["id"], "scope": scope, "status": result.get("status", "retrieval_only"),
                   "retrieval_backend": result.get("backend"), "hits": len(result.get("results", [])),
                   "model_calls": 0}
        row["elapsed_ms"] = round((time.monotonic() - start) * 1000)
        report["cases"].append(row)
        report["model_calls"] = used_calls
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(row, ensure_ascii=True), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--connection", required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765")
    parser.add_argument("--max-cases", type=int, default=3)
    parser.add_argument("--max-model-calls", type=int, default=12)
    parser.add_argument("--with-model", action="store_true")
    options = parser.parse_args()
    if options.max_cases < 1 or options.max_model_calls < 1:
        parser.error("Case and model budgets must be positive")
    run(options)
