"""Read-only configuration check; --live explicitly permits at most six model calls."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_assistant.core.service_config import chat_base_url
from research_assistant.core.service_context import service_context
from benchmarks.visual_fixture import CAPTION, chart_png


def inspect_config(config):
    llm = config.get("llm", {})
    configured = all(isinstance(llm.get(k), str) and llm[k].strip()
                     for k in ("base_url", "model", "api_key"))
    url = urlsplit(chat_base_url(llm.get("base_url", "")))
    safe = bool(url.hostname and not (url.username or url.password or url.query or url.fragment) and
        (url.scheme == "https" or url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}))
    declared = llm.get("supports_images") is True
    return {"configured": configured, "declared_image_support": declared,
        "endpoint_policy_valid": safe, "protocol": "chat-completions:image_url+production-structured-output",
        "live_eligible": configured and declared and safe,
        "pixel_discrimination": "not_tested", "answer_schema": "not_tested", "review_schema": "not_tested",
        "model_calls": 0, "status": "not_verified" if configured and declared and safe else "blocked"}


def color_of(value):
    value = value.strip().lower().rstrip("。. ")
    return {"blue": "blue", "蓝": "blue", "蓝色": "blue", "orange": "orange",
            "橙": "orange", "橙色": "orange"}.get(value)


async def probe(config, *, live=False, caller=None):
    from research_assistant.knowledge.qa import ModelCaller, MultimodalDraft, Review
    report = inspect_config(config)
    report["live_requested"] = live
    if not live or not report["live_eligible"]:
        return report
    caller = caller or ModelCaller()
    token = service_context.set({"connection": "visual-readiness", "config": config})
    report.update(status="failed", cases=[], unknown_usage_calls=0, input_tokens=0, output_tokens=0)

    async def call(role, payload):
        report["model_calls"] += 1
        report["unknown_usage_calls"] += 1
        value, usage = await asyncio.wait_for(caller(role, payload, 45), 45)
        if usage and all(type(usage.get(k)) is int for k in ("input_tokens", "output_tokens")):
            report["unknown_usage_calls"] -= 1
            for key in ("input_tokens", "output_tokens"):
                report[key] += usage[key]
        return value

    try:
        for blue_taller in (True, False):
            expected = "blue" if blue_taller else "orange"
            pixels = [{"evidence_id": "Vfixture", "data_url": "data:image/png;base64," +
                base64.b64encode(chart_png(blue_taller)).decode("ascii")}]
            payload = {"multimodal": True, "_visual_inputs": pixels,
                "question": "Which colored bar is taller? Set claim text and visual observation to exactly blue or orange (not a sentence).",
                "evidence": [{"evidence_id": "Vfixture", "modality": "image", "page": 1,
                              "quote": "", "visual_caption": CAPTION}]}
            value = await call("answer", payload)
            try:
                draft = MultimodalDraft.model_validate(value)
                report["answer_schema"] = "passed"
            except Exception:
                report["answer_schema"] = "failed"
                raise
            correct = len(draft.claims) == 1 and color_of(draft.claims[0].text) == expected
            citations = draft.claims[0].citations if len(draft.claims) == 1 else []
            correct = correct and len(citations) == 1 and citations[0].evidence_id == "Vfixture" and \
                getattr(citations[0], "citation_type", None) == "visual" and color_of(citations[0].observation) == expected
            case = {"expected": expected, "pixel_correct": bool(correct), "positive_review": False, "negative_review": False}
            report["cases"].append(case)
            for positive in (True, False):
                color = expected if positive else "orange" if expected == "blue" else "blue"
                review_payload = {"question": payload["question"], "multimodal": True, "_visual_inputs": pixels,
                    "claims": [{"text": color, "citations": [{"evidence_id": "Vfixture", "page": 1,
                        "citation_type": "visual", "observation": color}]}]}
                value = await call("review", review_payload)
                try:
                    review = Review.model_validate(value)
                except Exception:
                    report["review_schema"] = "failed"
                    raise
                report["review_schema"] = "passed"
                case["positive_review" if positive else "negative_review"] = bool(len(review.verdicts) == 1 and
                    review.verdicts[0].claim_index == 0 and review.verdicts[0].supported is positive and
                    (review.question_relevant if positive else True))
        report["pixel_discrimination"] = "passed" if all(c["pixel_correct"] for c in report["cases"]) else "failed"
        report["status"] = "synthetic_probe_passed" if all(all(c[k] for k in
            ("pixel_correct", "positive_review", "negative_review")) for c in report["cases"]) else "failed"
    except Exception as exc:
        # Never print provider bodies, endpoint/key, model output or pixels.
        report["error_type"] = type(exc).__name__
    finally:
        service_context.reset(token)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--connection", required=True)
    parser.add_argument("--live", action="store_true", help="Permit up to 6 billable requests, each capped at 45 seconds")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        path = args.root / ".data/zotero/service-config.json"
        configs = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        report = asyncio.run(probe(configs.get(args.connection, {}), live=args.live))
    except Exception as exc:
        report = {"status": "blocked", "error_type": type(exc).__name__, "model_calls": 0}
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output)
    return 0 if report["status"] in {"not_verified", "synthetic_probe_passed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
