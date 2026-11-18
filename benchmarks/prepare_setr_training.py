"""Freeze query-isolated RankZephyr inputs for an explicitly small SetR adaptation.

Old ranker responses never become selection labels. No MultiHopRAG gold used.
One deterministic Top-20 candidate version per query; exact normalized query and
passage overlap between train/dev is audited and connected components stay whole.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.setr_protocol import clean, digest, normalize


def prepare(source, *, train_limit=512, dev_limit=128):
    by_query = defaultdict(list)
    counts = Counter()
    for row in source:
        counts[len(row["passages"])] += 1
        if len(row["passages"]) == 20:
            by_query[normalize(row["query"])].append(row)
    rows = [min(versions, key=lambda r: digest([r["id"], r["passages"]])) for versions in by_query.values()]
    # Union queries with any identical normalized passage before splitting.
    parent = list(range(len(rows)))
    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    seen = {}
    for i, row in enumerate(rows):
        for passage in row["passages"]:
            key = digest(normalize(passage))
            if key in seen:
                parent[root(i)] = root(seen[key])
            else:
                seen[key] = i
    components = defaultdict(list)
    for i in range(len(rows)):
        components[root(i)].append(i)
    groups = []
    # Assignment is content-hash deterministic, before teacher/model execution.
    for indices in sorted(components.values(), key=lambda ids: digest(sorted(normalize(rows[i]["query"]) for i in ids))):
        split = "dev" if len([g for g in groups if g["split"] == "dev"]) < dev_limit else "train"
        limit = dev_limit if split == "dev" else train_limit
        if len([g for g in groups if g["split"] == split]) + len(indices) > limit:
            continue
        for i in indices:
            row = rows[i]
            groups.append({"id": digest(normalize(row["query"])), "source_id": row["id"], "split": split,
                           "question": clean(row["query"]),
                           "candidates": [{"text": clean(p)} for p in row["passages"]]})
        if sum(g["split"] == "train" for g in groups) == train_limit:
            break
    if not groups or not any(g["split"] == "train" for g in groups) or not any(g["split"] == "dev" for g in groups):
        raise ValueError("empty training/development split")
    sets = {s: {digest(normalize(c["text"])) for g in groups if g["split"] == s for c in g["candidates"]} for s in ("train", "dev")}
    if sets["train"] & sets["dev"]:
        raise ValueError("train/dev passage overlap")
    return {"schema": "setr-style-source-v1", "exact_paper_reproduction": False,
            "supervision": "pending fresh teacher generation; inherited ranker responses discarded",
            "split_rule": "normalized query and exact-normalized-passage connected components; content hash order",
            "near_duplicate_audit": "not performed; exact isolation does not exclude semantic/near duplicates",
            "input_rows": sum(counts.values()), "source_candidate_counts": dict(counts),
            "unique_top20_queries": len(rows), "components": len(components),
            "split_counts": dict(Counter(g["split"] for g in groups)), "groups": groups}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--train-limit", type=int, default=512)
    p.add_argument("--dev-limit", type=int, default=128)
    a = p.parse_args()
    if a.output.exists():
        raise FileExistsError(a.output)
    with a.source.open(encoding="utf-8") as f:
        data = prepare((json.loads(line) for line in f), train_limit=a.train_limit, dev_limit=a.dev_limit)
    data["source_sha256"] = hashlib.sha256(a.source.read_bytes()).hexdigest()
    data["source_url"] = "https://media.githubusercontent.com/media/LGAI-Research/SetR/main/train_data/rank_zephyr_training_data-parsed.jsonl"
    data["license_note"] = "SetR code: License.md BSD-3-Clause; underlying RankZephyr/MS MARCO content rights separate; no redistribution of passages or teacher labels"
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open("x", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(json.dumps({k: v for k, v in data.items() if k != "groups"}, indent=2))


if __name__ == "__main__":
    main()
