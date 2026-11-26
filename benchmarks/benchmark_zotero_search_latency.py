"""Measure bounded Go/Zotero retrieval latency without calling a generation model.

The target must be an isolated run directory prepared by
``prepare_real_zotero_run.py``.  The script records only timings, statuses,
candidate counts and public backend labels; it never writes a token, quote or
source identifier to the result file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_QUERIES = {
    "heart_failure_original": "什么是心衰",
    "heart_failure_cn": "什么是心力衰竭？",
    "heart_failure_en": "What is heart failure?",
    "ehr": "什么是EHR？",
    "absent": "火星基地建造预算是多少？",
}


def local_opener(endpoint):
    url = urlsplit(endpoint)
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username or url.password or url.query or url.fragment or url.path):
        raise ValueError("Expected a loopback HTTP origin")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def validate_result(body, payload, revisions, sources):
    if body.get("query") != payload["query"]:
        raise ValueError("query_roundtrip_mismatch")
    if body.get("revisions") != revisions:
        raise ValueError("revision_mismatch")
    hits = body.get("results")
    if not isinstance(hits, list) or len(hits) > payload["k"]:
        raise ValueError("invalid_results")
    for hit in hits:
        if (hit.get("kind") != "original" or hit.get("modality") != "text"
                or (hit.get("source_id"), hit.get("entity_id"), hit.get("revision")) not in sources):
            raise ValueError("source_scope_mismatch")


def request_search(endpoint, headers, payload, *, opener, revisions, sources,
                   timeout=90, poll_seconds=0.5, deadline_seconds=300):
    started = time.perf_counter()
    responses = 0
    while time.perf_counter() - started < deadline_seconds:
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        responses += 1
        try:
            remaining = deadline_seconds - (time.perf_counter() - started)
            with opener.open(request, timeout=min(timeout, max(.01, remaining))) as response:
                code = response.status
                body = json.loads(response.read())
            if code == 200:
                validate_result(body, payload, revisions, sources)
            elif code != 202 or body.get("status") != "indexing" or body.get("results") != []:
                raise ValueError("invalid_indexing_response")
        except Exception as exc:  # keep the failure boundary in the receipt
            return {
                "status": "request_error",
                "error_type": type(exc).__name__,
                "http_status": exc.code if isinstance(exc, urllib.error.HTTPError) else None,
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "polls": responses,
            }
        if code != 202 or body.get("status") != "indexing":
            return {
                "status": "completed" if code == 200 else f"http_{code}",
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "polls": responses,
                "candidate_count": len(body.get("results", [])),
                "backend": body.get("backend"),
                "hybrid_succeeded": body.get("backend") == "multimodal-hybrid",
                "warning": bool(body.get("warning")),
                "index_diagnostics": body.get("index_diagnostics"),
                "query_scope_revision_audit": "passed",
            }
        time.sleep(poll_seconds)
    return {
        "status": "indexing_timeout",
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "polls": responses,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8790")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--k", type=int, default=20)
    parser.add_argument("--all-bases", action="store_true", help="Use every active base in the isolated copy")
    parser.add_argument("--output-name", default="search-latency-verified.json")
    parser.add_argument("--deadline-seconds", type=int, default=300)
    args = parser.parse_args()
    if not 1 <= args.deadline_seconds <= 1800:
        parser.error("Deadline must be 1..1800 seconds")
    if args.repeats < 1 or args.repeats > 20 or args.k < 1 or args.k > 20:
        parser.error("--repeats must be 1..20 and --k must be 1..20")

    target = args.target.resolve()
    project = Path(__file__).resolve().parents[1]
    if not target.is_relative_to(project / "output"):
        parser.error("Target must be an isolated directory under project output/")
    if Path(args.output_name).name != args.output_name:
        parser.error("Output name must be a filename")
    output = target / args.output_name
    if output.exists():
        parser.error("Refusing to overwrite a prior measurement")
    opener = local_opener(args.endpoint)
    config = json.loads((target / "run-config.json").read_text(encoding="utf-8"))
    token = (target / "backend/.data/zotero/connection-token").read_text(encoding="utf-8").strip()
    headers = {
        "Authorization": "Bearer " + token,
        "Origin": args.endpoint,
        "X-Research-Connection": config["connection"],
        "Content-Type": "application/json",
    }
    def get(action):
        request = urllib.request.Request(args.endpoint + "/api/knowledge/" + action, headers=headers)
        with opener.open(request, timeout=30) as response:
            return json.loads(response.read())
    bases = [b for b in get("bases")["bases"] if not b["archived"]]
    ids = [b["id"] for b in bases] if args.all_bases else [config["base"]]
    revisions, sources, documents = {}, set(), set()
    for ident in ids:
        snapshot = get("bases/" + ident)
        revisions.update({b["id"]: b["revision"] for b in snapshot["bases"]})
        for entity in snapshot["entities"]:
            value = entity["data"]
            if value["kind"] == "document":
                source = value["hash"] + ".pdf"
                documents.add(source)
                sources.add((source, entity["id"], entity["revision"]))
    endpoint = args.endpoint + "/api/knowledge/search"
    receipt = {"scope": "original", "mode": "hybrid", "bases": len(ids),
               "unique_documents": len(documents), "generation_calls": 0,
               "reranking_measured": False, "cold_start_claim": "caller must restart isolated engine",
               "query_sha256": {k: hashlib.sha256(v.encode("utf-8")).hexdigest() for k, v in DEFAULT_QUERIES.items()}}
    receipt.update(started_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   git_revision=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip(),
                   k=args.k, repeats=args.repeats, deadline_seconds=args.deadline_seconds)
    rows = []
    # Reserve a unique receipt before sending any query; persist each completed attempt.
    with output.open("x", encoding="utf-8") as stream:
        json.dump({**receipt, "phase": "running", "rows": rows}, stream)
    for label, query in DEFAULT_QUERIES.items():
        for repeat in range(args.repeats):
            result = request_search(
                endpoint,
                headers,
                {
                    "kb_ids": ids,
                    "query": query,
                    "scope": "original",
                    "k": args.k,
                    "text_only": True,
                    "retrieval_mode": "hybrid",
                },
                opener=opener, revisions=revisions, sources=sources, deadline_seconds=args.deadline_seconds,
            )
            rows.append({"query_label": label, "repeat": repeat, **result})
            output.write_text(json.dumps({**receipt, "phase": "running", "rows": rows}, indent=2), encoding="utf-8")
            print(json.dumps(rows[-1]), flush=True)
            if result["status"] != "completed":
                break
        if rows[-1]["status"] != "completed":
            break

    complete = len(rows) == len(DEFAULT_QUERIES) * args.repeats and all(r["status"] == "completed" for r in rows)
    success = complete and all(r.get("hybrid_succeeded") for r in rows)
    phase = "completed" if success else "degraded" if complete else "failed"
    output.write_text(json.dumps({**receipt, "phase": phase, "rows": rows}, indent=2), encoding="utf-8")
    for label in DEFAULT_QUERIES:
        values = [r["elapsed_ms"] for r in rows if r["query_label"] == label and r["status"] == "completed"]
        print(json.dumps({
            "query_label": label,
            "completed": len(values),
            "median_ms": statistics.median(values) if values else None,
            "min_ms": min(values) if values else None,
            "max_ms": max(values) if values else None,
        }, ensure_ascii=False))
    print(output)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
