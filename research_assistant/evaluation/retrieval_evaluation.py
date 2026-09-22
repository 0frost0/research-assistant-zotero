"""Reproducible evidence-group retrieval evaluation and explicit human judgments."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

KS = (1, 3, 5, 10)
SCHEMA = 1


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def evidence_catalog(root: Path):
    path = root / ".data/evidence.sqlite3"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        texts = con.execute("SELECT e.* FROM evidence e JOIN documents d ON e.document_id=d.document_id "
                            "AND e.version=d.active_version WHERE e.active=1").fetchall()
        images = con.execute("SELECT v.* FROM visual_assets v JOIN documents d ON v.document_id=d.document_id "
                             "AND v.document_version=d.active_version WHERE v.active=1").fetchall()
    result = {}
    for row in texts:
        item = dict(row)
        result[item["evidence_id"]] = {**item, "point_id": item["evidence_id"], "modality": "text"}
    for row in images:
        item = dict(row)
        result[item["visual_id"]] = {**item, "point_id": item["visual_id"], "modality": "image", "text": item["caption"]}
    return result


def corpus_snapshot(root: Path, catalog=None):
    catalog = evidence_catalog(root) if catalog is None else catalog
    library = root / "library"
    sources = {str(p.relative_to(library)): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted(library.rglob("*")) if p.is_file() and p.suffix.lower() in {".pdf", ".md", ".txt"}}
    identities = sorted((key, value["version"]) for key, value in catalog.items())
    return {"source_sha256": sources, "evidence_sha256": digest(identities), "points": len(identities)}


def validate_dataset(dataset, root: Path):
    if dataset.get("schema") != SCHEMA or not dataset.get("cases"):
        raise ValueError("Unsupported or empty evaluation dataset")
    catalog = evidence_catalog(root)
    if dataset.get("corpus") != corpus_snapshot(root, catalog):
        raise ValueError("Evaluation corpus is stale. Re-annotate a new dataset version; do not reuse old approvals.")
    seen = set()
    for case in dataset["cases"]:
        cid = case["id"]
        if cid in seen or case["query_type"] not in {"text", "image", "mixed"}:
            raise ValueError(f"Invalid or duplicate case: {cid}")
        seen.add(cid)
        actual_input = "mixed" if case.get("query_image_id") and case.get("question", "").strip() else (
            "image" if case.get("query_image_id") else "text")
        if case.get("input_modality") != actual_input:
            raise ValueError(f"Query input modality mismatch: {cid}")
        if case.get("query_image_id"):
            asset = catalog.get(case["query_image_id"])
            if not asset or asset["modality"] != "image":
                raise ValueError(f"Missing query image: {cid}")
            image = Path(asset["image_path"]).resolve()
            if not image.is_relative_to((root / ".cache/mineru").resolve()) or not image.is_file():
                raise ValueError(f"Unsafe or missing query image: {cid}")
            if hashlib.sha256(image.read_bytes()).hexdigest() != asset["image_hash"]:
                raise ValueError(f"Changed query image: {cid}")
        if not case.get("expected") and not case.get("unanswerable"):
            raise ValueError(f"Positive case has no expected evidence: {cid}")
        used = set()
        for group in case.get("expected", []):
            ids = group.get("any_of", [])
            if not ids or set(ids) & used:
                raise ValueError(f"Empty or overlapping evidence groups: {cid}")
            used.update(ids)
            for point_id in ids:
                item = catalog.get(point_id)
                if (not item or item["source"] != group["source"] or item["page"] != group["page"]
                        or item["modality"] != group["modality"]):
                    raise ValueError(f"Unresolved expected evidence: {cid}/{point_id}")
    return catalog


def score_ranking(case, ranking, ks=KS):
    """Equivalent overlapping chunks form ONE relevant group, not many easy positives."""
    groups = [set(g["any_of"]) for g in case.get("expected", [])]
    if not groups:
        return {"negative": True, "returned": len(ranking)}
    pages = {(g["source"], g["page"]) for g in case["expected"]}
    output = {}
    for k in ks:
        ids = {h["point_id"] for h in ranking[:k]}
        found_pages = {(h["source"], h["page"]) for h in ranking[:k]}
        output[f"recall@{k}"] = sum(bool(g & ids) for g in groups) / len(groups)
        output[f"page_recall@{k}"] = len(pages & found_pages) / len(pages)
    first = next((i for i, hit in enumerate(ranking[:max(ks)], 1)
                  if any(hit["point_id"] in group for group in groups)), None)
    output[f"mrr@{max(ks)}"] = 1 / first if first else 0.0
    return output


def display_ranking(ranking, threshold, limit=10):
    """Frozen legacy-v1 baseline, not the current production policy."""
    counts, result = Counter(), []
    for hit in ranking:
        if hit["score"] < threshold or counts[hit["source"]] >= 3:
            continue
        counts[hit["source"]] += 1
        result.append(hit)
        if len(result) == limit:
            break
    return result


def score_context_ranking(case, ranking, ks=KS):
    """Credit grouped anchors only when their identical source context is delivered."""
    if not case.get("expected"):
        return {}
    groups = [set(g["any_of"]) for g in case["expected"]]
    output = {}
    for k in ks:
        ids = {pid for h in ranking[:k] for pid in h.get("context_evidence_ids", [h["point_id"]])}
        output[f"context_recall@{k}"] = sum(bool(g & ids) for g in groups) / len(groups)
    return output


def compare_policies(rows):
    baseline = [{**c, "metrics": {"raw": c["metrics"]["raw"], "displayed": c["baseline"]["metrics"]}}
                for c in rows]
    changes = []
    for case in rows:
        if case.get("unanswerable") or case.get("split") == "self_match_probe":
            continue
        old, new = case["baseline"]["metrics"], case["metrics"]["displayed"]
        changes.append({"case_id": case["id"], "anchor_recall_delta": new["recall@10"] - old["recall@10"],
                        "context_recall_delta": new["context_recall@10"] - old["recall@10"]})
    regressions = [c["case_id"] for c in changes if c["context_recall_delta"] < -1e-9]
    return {"baseline_policy": "legacy-v1-cap3", "same_candidates": True,
            "baseline_summary": summarize(baseline), "case_changes": changes,
            "regressions": regressions,
            "status": "needs_review" if regressions else "no_context_recall_regression",
            "criterion": "Delivered-context Recall@10 must not fall below legacy anchor Recall@10 per development case; not a quality acceptance test."}


def summarize(rows, approved=()):
    output = {}
    for scope in ("draft_development", "human_approved_development", "self_match_probe"):
        selected = [r for r in rows if not r.get("unanswerable") and
                    ((r.get("split") == "self_match_probe") if scope == "self_match_probe" else
                     (r.get("split") != "self_match_probe" and
                      (scope == "draft_development" or r["id"] in approved)))]
        output[scope] = {}
        for group in ("all", "text", "image", "mixed"):
            subset = [r for r in selected if group == "all" or r["query_type"] == group]
            metrics = {"count": len(subset)}
            for mode in ("raw", "displayed"):
                metrics[mode] = {key: mean(r["metrics"][mode][key] for r in subset)
                                 for key in subset[0]["metrics"][mode]} if subset else None
            output[scope][group] = metrics
    negatives = [r for r in rows if r.get("unanswerable")]
    output["negative_controls"] = {"count": len(negatives), "returned_evidence_cases":
                                   sum(bool(r["rankings"]["displayed"]) for r in negatives)}
    return output


def human_summary(rows, judgments, mode="displayed", k=10):
    output = {}
    lookup = {(j["case_id"], j["point_id"]): j["grade"] for j in judgments}
    for group in ("all", "text", "image", "mixed"):
        selected = [r for r in rows if r.get("split") != "self_match_probe" and not r.get("unanswerable")
                    and (group == "all" or r["query_type"] == group)]
        slots = [(r["id"], h["point_id"]) for r in selected for h in r["rankings"][mode][:k]]
        grades = [lookup[key] for key in slots if key in lookup]
        output[group] = {"returned": len(slots), "judged": len(grades),
                         "coverage": len(grades) / len(slots) if slots else None,
                         "mean_grade": mean(grades) if grades else None,
                         "relevant_fraction_among_judged": mean(g >= 2 for g in grades) if grades else None}
    return output


def review_snapshot_hash(run):
    # Exclude mutable human judgments and derived summaries, bind all retrieval inputs/outputs.
    return digest({key: value for key, value in run.items() if key not in {
        "judgments", "approvals", "summary", "human_relevance", "human_approved_relevance",
        "ai_review", "ai_relevance"}})


def ai_summary(rows, judgments):
    result = {mode: human_summary(rows, judgments, mode) for mode in ("raw", "displayed")}
    lookup = {(j["case_id"], j["point_id"]): j["grade"] for j in judgments}
    result["coverage"] = {}
    for scope in ("all", "development", "self_match_probe", "negative_controls"):
        selected = [c for c in rows if scope == "all" or (
            c.get("unanswerable") if scope == "negative_controls" else
            c.get("split") == "self_match_probe" if scope == "self_match_probe" else
            not c.get("unanswerable") and c.get("split") != "self_match_probe")]
        pairs = {(c["id"], h["point_id"]) for c in selected
                 for ranking in c["rankings"].values() for h in ranking}
        result["coverage"][scope] = {"cases": len(selected), "pairs": len(pairs),
            "judged": len(pairs & lookup.keys()),
            "grade_counts": {str(g): sum(lookup.get(pair) == g for pair in pairs) for g in range(4)}}
    return result


def validate_ai_review(run, package):
    if (package.get("schema") != 1 or package.get("annotation_origin") != "ai_assisted"
            or package.get("run_id") != run["run_id"]
            or package.get("dataset_hash") != run["dataset_hash"]
            or package.get("snapshot_hash") != review_snapshot_hash(run)):
        raise ValueError("AI review origin or immutable snapshot mismatch")
    for key in ("reviewer", "method"):
        if not isinstance(package.get(key), str) or not package[key].strip():
            raise ValueError(f"AI review requires {key}")
    for key in ("limitations", "source_checks"):
        if not isinstance(package.get(key), list) or not package[key]:
            raise ValueError(f"AI review requires {key}")
    if not isinstance(package.get("rubric"), dict) or not package["rubric"]:
        raise ValueError("AI review requires a rubric")
    expected = {(c["id"], h["point_id"]) for c in run["cases"]
                for ranking in c["rankings"].values() for h in ranking}
    seen = set()
    for judgment in package.get("judgments", []):
        pair = (judgment.get("case_id"), judgment.get("point_id"))
        if pair not in expected or pair in seen:
            raise ValueError("Unknown or duplicate AI evidence judgment")
        if type(judgment.get("grade")) is not int or judgment["grade"] not in range(4):
            raise ValueError("AI grade must be integer 0..3")
        if not isinstance(judgment.get("note"), str) or not judgment["note"].strip():
            raise ValueError("AI judgment requires a reason")
        if judgment.get("confidence") not in {"low", "medium", "high"}:
            raise ValueError("Invalid AI confidence")
        seen.add(pair)
    if seen != expected:
        raise ValueError("AI review must cover every returned case/evidence pair")
    cases, reviewed = {c["id"] for c in run["cases"]}, set()
    for review in package.get("case_reviews", []):
        if review.get("case_id") not in cases or review["case_id"] in reviewed:
            raise ValueError("Unknown or duplicate AI case review")
        if (review.get("status") not in {"consistent", "needs_revision", "needs_review", "negative_control", "self_match_only"}
                or type(review.get("needs_human_review")) is not bool
                or review.get("confidence") not in {"low", "medium", "high"}
                or not isinstance(review.get("note"), str) or not review["note"].strip()):
            raise ValueError("Invalid AI case review")
        reviewed.add(review["case_id"])
    if reviewed != cases:
        raise ValueError("AI review must cover every case")


def run_evaluation(root: Path, index, dataset: dict, progress=lambda done, total: None):
    catalog = validate_dataset(dataset, root)
    unified = index.unified_index
    if unified is None:
        raise ValueError("Unified model is unavailable; fallback retrieval must not be scored as Qwen")
    expected_ids = {pid for c in dataset["cases"] for g in c.get("expected", []) for pid in g["any_of"]}
    if not unified._contains_points(sorted(expected_ids)):
        raise ValueError("Expected evidence is missing from Qdrant; synchronize before evaluation")
    from research_assistant.retrieval.knowledge_base import DEFAULT_THRESHOLDS
    from research_assistant.retrieval.retrieval_policy import CANDIDATE_K, policy_settings, select_evidence
    threshold = DEFAULT_THRESHOLDS["multimodal"]
    rows, start = [], time.monotonic()
    for position, case in enumerate(dataset["cases"], 1):
        text = index._expanded_query(case.get("question", ""))
        image = Path(catalog[case["query_image_id"]]["image_path"]) if case.get("query_image_id") else None
        hits = unified.search(text, k=CANDIDATE_K, min_score=-1.0, query_image=image)
        ranked = [asdict(hit) for hit in hits]
        baseline = display_ranking(ranked, threshold)
        displayed = select_evidence(ranked, threshold, context=index.evidence_context)
        # Save actual evidence text to the local review artifact, never to external LLM logs.
        row = {**case, "expanded_query": text, "rankings": {"raw": ranked[:10], "displayed": displayed},
               "baseline": {"ranking": baseline, "metrics": score_ranking(case, baseline)},
               "metrics": {"raw": score_ranking(case, ranked), "displayed": {
                   **score_ranking(case, displayed), **score_context_ranking(case, displayed)}}}
        rows.append(row)
        progress(position, len(dataset["cases"]))
    if corpus_snapshot(root) != dataset["corpus"]:
        raise ValueError("Corpus changed during evaluation; run discarded")
    return {"schema": SCHEMA, "run_id": str(uuid.uuid4()), "created_at": now(),
            "dataset_id": dataset["id"], "dataset_hash": digest(dataset), "corpus": dataset["corpus"],
            "model": unified.model_name, "dimension": unified.dimension, "collection": unified.collection_name,
            "encoder_profile": getattr(unified.embeddings, "profile", {}),
            "settings": {"ks": KS, "threshold": threshold, "raw_threshold": -1.0,
                         "candidate_limit": 200, "per_source_cap": None, "retrieval_policy": policy_settings(),
                         "query_input": "text or image+text, single forward pass"},
            "duration_seconds": round(time.monotonic() - start, 2), "cases": rows,
            "summary": summarize(rows), "suitability": "not_established",
            "policy_comparison": compare_policies(rows),
            "limitations": ["Assistant-authored development gold is not human-verified.",
                            "Image means image evidence sought; input_modality records actual query inputs.",
                            "Self-image probes are not independent quality tests and are reported separately.",
                            "This measures retrieval, not answer correctness or numerical table extraction accuracy.",
                            "Context recall credits grouped retrieved IDs only when their exact source context is actually delivered; anchor Recall/MRR are separate.",
                            "AI relevance judgments from earlier runs are not copied to new evidence contexts.",
                            "Small development set: require human review and a held-out set before acceptance."]}


class EvaluationStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as con, con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS judgments (
                    run_id TEXT, case_id TEXT, point_id TEXT, grade INTEGER CHECK(grade BETWEEN 0 AND 3),
                    reviewer TEXT NOT NULL, note TEXT, updated_at TEXT, PRIMARY KEY(run_id,case_id,point_id));
                CREATE TABLE IF NOT EXISTS approvals (
                    dataset_hash TEXT, case_id TEXT, reviewer TEXT NOT NULL, note TEXT, updated_at TEXT,
                    PRIMARY KEY(dataset_hash,case_id));
                CREATE TABLE IF NOT EXISTS ai_reviews (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id), payload TEXT NOT NULL, created_at TEXT NOT NULL);
            """)

    def save(self, run):
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("INSERT INTO runs VALUES(?,?,?)", (run["run_id"], run["created_at"], json.dumps(run, ensure_ascii=False)))

    def list_runs(self):
        with closing(sqlite3.connect(self.path)) as con:
            return [{"run_id": r[0], "created_at": r[1]} for r in con.execute("SELECT run_id,created_at FROM runs ORDER BY created_at DESC LIMIT 50")]

    def get(self, run_id):
        with closing(sqlite3.connect(self.path)) as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT payload FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("Unknown evaluation run")
            run = json.loads(row[0])
            judgments = [dict(r) for r in con.execute("SELECT * FROM judgments WHERE run_id=?", (run_id,))]
            approvals = [dict(r) for r in con.execute("SELECT * FROM approvals WHERE dataset_hash=?", (run["dataset_hash"],))]
            ai_row = con.execute("SELECT payload,created_at FROM ai_reviews WHERE run_id=?", (run_id,)).fetchone()
        run["judgments"], run["approvals"] = judgments, approvals
        run["summary"] = summarize(run["cases"], {a["case_id"] for a in approvals})
        run["human_relevance"] = {mode: human_summary(run["cases"], judgments, mode) for mode in ("raw", "displayed")}
        approved_ids = {a["case_id"] for a in approvals}
        run["human_approved_relevance"] = {mode: human_summary(
            [c for c in run["cases"] if c["id"] in approved_ids], judgments, mode) for mode in ("raw", "displayed")}
        run["ai_review"] = {**json.loads(ai_row[0]), "imported_at": ai_row[1]} if ai_row else None
        run["ai_relevance"] = ai_summary(run["cases"], run["ai_review"]["judgments"]) if ai_row else None
        return run

    def import_ai_review(self, package):
        """Atomic, immutable AI-only import; never writes judgments or gold approvals."""
        payload = json.dumps(package, ensure_ascii=False, sort_keys=True, allow_nan=False)
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload FROM runs WHERE run_id=?", (package.get("run_id"),)).fetchone()
            if not row:
                raise ValueError("Unknown evaluation run")
            validate_ai_review(json.loads(row[0]), package)
            existing = con.execute("SELECT payload FROM ai_reviews WHERE run_id=?", (package["run_id"],)).fetchone()
            if existing:
                if json.loads(existing[0]) != package:
                    raise ValueError("AI review is immutable; refusing to overwrite existing review")
                return
            con.execute("INSERT INTO ai_reviews VALUES(?,?,?)", (package["run_id"], payload, now()))

    def judge(self, run_id, data):
        run = self.get(run_id)
        reviewer = str(data.get("reviewer", "")).strip()
        if not reviewer or len(reviewer) > 80 or len(str(data.get("note", ""))) > 1000:
            raise ValueError("Reviewer required (max 80 characters), note max 1000")
        case = next((c for c in run["cases"] if c["id"] == data.get("case_id")), None)
        if not case:
            raise ValueError("Unknown case")
        with closing(sqlite3.connect(self.path)) as con, con:
            if data.get("action") in {"approve_gold", "revoke_gold"}:
                if data["action"] == "revoke_gold":
                    con.execute("DELETE FROM approvals WHERE dataset_hash=? AND case_id=?", (run["dataset_hash"], case["id"]))
                else:
                    con.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?)",
                                (run["dataset_hash"], case["id"], reviewer, data.get("note", ""), now()))
            else:
                ids = {h["point_id"] for ranking in case["rankings"].values() for h in ranking}
                grade = data.get("grade")
                if data.get("point_id") not in ids or type(grade) is not int or grade not in range(4):
                    raise ValueError("Grade must be 0..3 for a returned evidence item")
                con.execute("INSERT OR REPLACE INTO judgments VALUES(?,?,?,?,?,?,?)",
                            (run_id, case["id"], data["point_id"], grade, reviewer, data.get("note", ""), now()))
        return self.get(run_id)
