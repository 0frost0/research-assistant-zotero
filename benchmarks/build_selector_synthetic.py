"""Fully invented multi-dimension fixtures; never evidence of real quality."""
import argparse
from itertools import combinations
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_assistant.retrieval.selector_dataset import SCHEMA, make_example, write_dataset


def build():
    groups, examples = [], []
    for split, paper, n in [("train", "fictional-alfa", 24), ("dev", "fictional-beta", 37),
                            ("test", "fictional-gamma", 53)]:
        group = {"id": paper, "paper": paper, "split": split,
                 "question": f"For invented study {paper}, describe the method, eligibility, result and limitation.",
                 "dimensions": [{"id": d, "description": desc, "category": d} for d, desc in [
                     ("method", "Which invented intervention was used?"),
                     ("condition", "Who was eligible for this experiment?"),
                     ("result", "What number was observed and on which endpoint?"),
                     ("limitation", "What outcome was NOT measured?")]]}
        texts = [("method", f"Study {paper} used fictional patch {paper}-X to record pulse.", ["method"]),
                 ("duplicate", f"In study {paper}, pulse was recorded using fictional patch {paper}-X.", ["method"]),
                 ("condition", f"In {paper}, only adults aged at least {n} years were eligible; minors were excluded.", ["condition"]),
                 ("result", f"In {paper}, {n} participants completed monitoring; this is a completion count, not a treatment benefit.", ["result"]),
                 ("limitation", f"Study {paper} did not measure survival and cannot establish a survival benefit.", ["limitation"]),
                 ("related", f"Study {paper} mentions monitoring in its historical introduction but gives no design details there.", []),
                 ("wrong-condition", f"An earlier pilot discussed by {paper} used a different device for children, not this experiment.", [])]
        group["candidates"] = [{"id": f"{paper}-{cid}", "paper": paper, "source_id": paper,
                                "page": i + 1, "quote": text, "covers": covers}
                               for i, (cid, text, covers) in enumerate(texts)]
        groups.append(group)
        for size in range(3):
            for subset in combinations(group["candidates"][:5], size):
                for candidate in group["candidates"]:
                    if candidate not in subset:
                        examples.append(make_example(group, list(subset), candidate))
    return {"schema": SCHEMA, "id": "selector-synthetic-v1",
            "provenance": {"label_status": "synthetic", "independent_test": False,
                           "license": "project_authored_synthetic", "effectiveness_claim": False},
            "paper_splits": {g["split"]: [g["paper"]] for g in groups},
            "groups": groups, "examples": examples}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = build()
    print({"sha256": write_dataset(args.output, data), "examples": len(data["examples"])})
