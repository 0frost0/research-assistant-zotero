"""Read-only citation audit for native real-library runs; export no excerpts."""
import argparse
import json
from pathlib import Path
import re
import sqlite3


def audit(root):
    report = json.loads((root / "result.json").read_text(encoding="utf-8"))
    home = root / "backend/.data/zotero"
    normalize = lambda value: re.sub(r"\s+", "", value)
    results = []
    with sqlite3.connect((home / "qa_jobs.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as jobs, sqlite3.connect((home / "knowledge.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as knowledge:
        for case in report["cases"]:
            answer = json.loads(jobs.execute("SELECT result FROM qa_jobs WHERE id=?", (case["job_id"],)).fetchone()[0])
            checks = []
            for claim in answer.get("claims", []):
                for citation in claim["citations"]:
                    if citation["kind"] == "original":
                        row = knowledge.execute("SELECT pages FROM objects WHERE hash=?", (citation["source_id"][:-4],)).fetchone()
                        text = next((p["text"] for p in json.loads(row[0]) if p["page"] == citation["page"]), "") if row else ""
                    else:
                        table = "note_revisions" if citation["kind"] == "user_note" else "entity_revisions"
                        row = knowledge.execute(f"SELECT payload FROM {table} WHERE id=? AND revision=?", (citation["entity_id"], citation["revision"])).fetchone()
                        value = json.loads(row[0]) if row else {}
                        text = value.get("body", "") + "\n" + value.get("comment", "")
                    quote = normalize(citation.get("support_quote", ""))
                    checks.append({"page": citation.get("page"), "revision": citation["revision"],
                                   "quote_found": bool(quote) and quote in normalize(text),
                                   "zotero_link": citation.get("url", "").startswith("zotero://")})
            results.append({"id": case["id"], "status": answer["status"], "checks": checks,
                            "gaps_count": len(answer.get("gaps", [])),
                            "model_output_failure": answer.get("trace", {}).get("model_output_failure"),
                            "human_semantic_review": None})
    (root / "anchor-audit.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run": root.name, "cases": len(results), "citations": sum(len(r["checks"]) for r in results),
                      "unmatched": sum(not c["quote_found"] for r in results for c in r["checks"])}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    audit(parser.parse_args().root)
