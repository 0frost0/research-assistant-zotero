"""Read-only quote/source audit; exports no paper text or model assertions."""
import argparse
import json
from pathlib import Path
import re
import sqlite3


def audit(root):
    result = json.loads((root / "comparison-private.json").read_text(encoding="utf-8"))
    normalize = lambda text: re.sub(r"\s+", "", text)
    checks, evidence = [], {}
    db = root / "backend/.data/zotero/knowledge.sqlite3"
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        for doc in result["documents"]:
            for entry in doc["evidence"]:
                row = connection.execute("SELECT pages FROM objects WHERE hash=?", (entry["source"][:-4],)).fetchone()
                pages = json.loads(row[0]) if row else []
                page = next((p["text"] for p in pages if p["page"] == entry["page"]), "")
                quote = normalize(entry["quote"])
                valid = bool(quote) and quote in normalize(page) and entry["source"] == doc["source"]
                checks.append({"ref": entry["ref"], "page": entry["page"], "quote_found": valid})
                evidence[entry["ref"]] = entry
    source_checks = []
    for claim in result["comparisons"]:
        refs = claim["evidence_ids"]
        valid = all(ref in evidence for ref in refs)
        distinct = len({evidence[ref]["source"] for ref in refs if ref in evidence})
        source_checks.append({"all_refs_exist": valid, "distinct_sources": distinct,
                              "ai_review_supported": claim["review"]["verdict"] == "supported"})
    report = {"status": result["status"], "model_calls": result["calls"], "evidence": len(checks),
              "unmatched": sum(not c["quote_found"] for c in checks), "checks": checks,
              "accepted_source_checks": source_checks, "human_semantic_accuracy": None}
    (root / "comparison-audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report))
    if report["unmatched"] or any(not c["all_refs_exist"] or c["distinct_sources"] < 2 or not c["ai_review_supported"] for c in source_checks):
        raise SystemExit("Comparison anchor audit failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    audit(parser.parse_args().root)
