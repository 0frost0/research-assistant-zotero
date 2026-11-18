"""Build PRIVATE anchor-proxy annotation material, not a formal training set.

Only required anchors have known coverage. Other same-paper passages remain
unjudged (covers=null), never automatically negative. No cross-split papers
are used even as distractors. Output is exclusive-create, like stage 1.
"""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_assistant.retrieval.selector_dataset import (SCHEMA, digest,
    make_example, paper_split, write_dataset)


def build(source):
    if source.get("split") != "development":
        raise ValueError("source must explicitly be development data")
    positive = [c for c in source.get("cases", []) if not c.get("no_answer")]
    by_paper = defaultdict(list)
    for case in positive:
        by_paper[case["paper"]].append(case)
    groups, examples = [], []
    for case in positive:
        dimensions = [{"id": f"anchor-{i}", "description": "Specified stage-1 anchor; NOT a semantic dimension",
                       "category": case.get("category", "unknown")}
                      for i in range(len(case["expected"]))]
        required = []
        for evidence in case["expected"]:
            covers = [f"anchor-{i}" for i, gold in enumerate(case["expected"])
                      if evidence["source_id"] == gold["source_id"] and evidence["page"] == gold["page"]
                      and "".join(gold["quote"].split()) in "".join(evidence["quote"].split())]
            required.append({**evidence, "id": digest(evidence)[:20], "paper": case["paper"], "covers": covers})
        required = list({c["id"]: c for c in required}.values())
        unknown = []
        for other in by_paper[case["paper"]]:
            for evidence in other["expected"]:
                cid = digest(evidence)[:20]
                if cid not in {c["id"] for c in required + unknown}:
                    unknown.append({**evidence, "id": cid, "paper": case["paper"], "covers": None})
        group = {"id": "G" + case["id"], "paper": case["paper"], "split": paper_split(case["paper"]),
                 "question": case["query"], "dimensions": dimensions,
                 "candidate_order": "anchor_annotation_material_not_retrieval_order",
                 "candidates": required + unknown}
        groups.append(group)
        # Only positive anchor-proxy examples; unknown passages never become negatives.
        for candidate in required:
            examples.append(make_example(group, [], candidate))
        for first in required:
            for candidate in required:
                if candidate["id"] != first["id"]:
                    examples.append(make_example(group, [first], candidate))
    papers = sorted(by_paper)
    return {"schema": SCHEMA, "id": "evidence-selector-anchor-proxy-v2",
            "provenance": {"source_dataset": source.get("id"), "independent_test": False,
                           "source_content_sha256": digest(source),
                           "label_status": "development_anchor_proxy", "license": "private_local_no_redistribution",
                           "warning": "Not semantic coverage gold. Formal LoRA training is blocked."},
            "paper_splits": {s: [p for p in papers if paper_split(p) == s] for s in ("train", "dev", "test")},
            "groups": groups, "examples": examples}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.source.read_bytes()
    dataset = build(json.loads(raw))
    dataset["provenance"]["source_file_sha256"] = hashlib.sha256(raw).hexdigest()
    sha = write_dataset(args.output, dataset)
    print(json.dumps({"dataset_id": dataset["id"], "sha256": sha, "groups": len(dataset["groups"]),
                      "examples": len(dataset["examples"]), "paper_splits": dataset["paper_splits"],
                      "formal_training_allowed": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
