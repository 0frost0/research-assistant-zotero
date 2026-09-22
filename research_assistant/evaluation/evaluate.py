"""本地回归评测；默认不调用外部模型。"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from research_assistant.retrieval.knowledge_base import DEFAULT_THRESHOLDS, LiteratureIndex
from research_assistant.core.models import ResearchContext
from research_assistant.agents.research_agent import build_agent, run_agent


from research_assistant.core.paths import PROJECT_ROOT
sys.stdout.reconfigure(encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="科研助手本地评测")
    parser.add_argument("--cases", type=Path, default=PROJECT_ROOT / "benchmarks/datasets/eval_cases.json")
    parser.add_argument("--library", type=Path, default=PROJECT_ROOT / "library")
    parser.add_argument(
        "--backend",
        choices=["tfidf", "bge", "hybrid", "qdrant", "multimodal"],
        default="tfidf",
    )
    parser.add_argument("--output", type=Path, help="可选的 JSON 评测报告路径")
    parser.add_argument("--with-agent", action="store_true", help="同时调用 Agent 检查引用")
    parser.add_argument(
        "--allow-external",
        action="store_true",
        help="明确允许评测片段发送给外部模型；仅与 --with-agent 一起使用",
    )
    parser.add_argument("--trace", action="store_true", help="Agent 评测时保留 LangSmith Trace")
    return parser.parse_args()


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def citation_is_grounded(index: LiteratureIndex, citation: Any) -> bool:
    quote = normalize(citation.quote)
    if not quote:
        return False
    return any(
        chunk.metadata["source"] == citation.source
        and (citation.page is None or chunk.metadata.get("page") == citation.page)
        and quote in normalize(chunk.page_content)
        for chunk in index.chunks
    )


def evaluate_retrieval(index: LiteratureIndex, case: dict[str, Any]) -> dict[str, Any]:
    results = index.search_results(
        case["question"],
        k=int(case.get("k", 5)),
        min_score=float(case.get("min_score", DEFAULT_THRESHOLDS[index.backend])),
        sources=case.get("sources"),
        file_types=case.get("file_types"),
    )
    found = bool(results)
    sources = {result.source for result in results}
    expected_sources = set(case.get("expected_sources", []))
    combined_quotes = normalize(" ".join(result.quote for result in results))
    required_terms = case.get("required_terms", [])

    found_ok = found == bool(case.get("should_find", True))
    source_ok = not expected_sources or bool(sources & expected_sources)
    terms_ok = all(normalize(term) in combined_quotes for term in required_terms)
    passed = found_ok and source_ok and terms_ok
    return {
        "id": case["id"],
        "passed": passed,
        "retrieval": {
            "found_ok": found_ok,
            "source_ok": source_ok,
            "terms_ok": terms_ok,
            "sources": sorted(sources),
            "top_score": results[0].score if results else None,
        },
    }


def load_environment() -> None:
    for env_path in (
        PROJECT_ROOT / ".env",
        PROJECT_ROOT.parent / ".env",
        PROJECT_ROOT.parent / "langchain" / ".env",
    ):
        if env_path.exists():
            load_dotenv(env_path)
            return


def evaluate_agent(
    index: LiteratureIndex,
    case: dict[str, Any],
) -> dict[str, Any]:
    agent = build_agent(
        index,
        sources=case.get("sources"),
        file_types=case.get("file_types"),
        min_score=float(case.get("min_score", DEFAULT_THRESHOLDS[index.backend])),
    )
    response = run_agent(
        agent,
        case["question"],
        ResearchContext(
            research_topic="科研助手回归评测",
            weekly_hours=1,
            mode="answer",
        ),
    )
    grounded = all(citation_is_grounded(index, item) for item in response.citations)
    citation_sources = {item.source for item in response.citations}
    expected_sources = set(case.get("expected_sources", []))
    expected_source_used = not expected_sources or bool(citation_sources & expected_sources)
    if not case.get("should_find", True):
        expected_source_used = not response.citations
    return {
        "status": response.status,
        "grounded": grounded,
        "expected_source_used": expected_source_used,
        "citation_count": len(response.citations),
        "passed": response.status == "ok" and grounded and expected_source_used,
    }


def main() -> int:
    args = parse_args()
    if args.with_agent and not args.allow_external:
        raise SystemExit("使用 --with-agent 时必须同时明确传入 --allow-external")

    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    index = LiteratureIndex(args.library, backend=args.backend)
    report = {
        "backend": args.backend,
        "case_count": len(cases),
        "results": [],
    }

    if args.with_agent:
        load_environment()
        if not args.trace:
            os.environ["LANGSMITH_TRACING"] = "false"

    for case in cases:
        result = evaluate_retrieval(index, case)
        if args.with_agent:
            result["agent"] = evaluate_agent(index, case)
            result["passed"] = result["passed"] and result["agent"]["passed"]
        report["results"].append(result)
        label = "PASS" if result["passed"] else "FAIL"
        print(f"[{label}] {case['id']}")

    passed = sum(item["passed"] for item in report["results"])
    report["passed"] = passed
    report["failed"] = len(cases) - passed
    print(f"\n通过 {passed}/{len(cases)}")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"报告：{args.output.resolve()}")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
