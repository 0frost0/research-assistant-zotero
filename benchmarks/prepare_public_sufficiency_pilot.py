"""Freeze a reserve-only paired pilot without creating or editing labels."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.run_public_sufficiency import SOURCE_SHA, VERSION, digest, file_sha256, load, validate_input, write_new


def select_reserve(source, pairs, skip=0):
    if type(pairs) is not int or pairs < 1 or type(skip) is not int or skip < 0:
        raise ValueError("Invalid reserve range")
    manifest = load(source / "manifest.json")
    if manifest["version"] != VERSION or manifest["source_sha256"] != SOURCE_SHA:
        raise ValueError("Unexpected frozen source")
    for split in ("reserve", "development", "heldout"):
        for kind in ("inputs", "gold"):
            name = f"{split}.{kind}.json"
            if file_sha256(source / name) != manifest["files"][name]:
                raise ValueError("Changed source file: " + name)
    inputs = load(source / "reserve.inputs.json")
    gold = load(source / "reserve.gold.json")
    if manifest["version"] != VERSION or len(inputs) != len(gold):
        raise ValueError("Unexpected frozen reserve")
    by_group = defaultdict(list)
    lookup = {item["id"]: item for item in inputs}
    if len(lookup) != len(inputs) or {g["id"] for g in gold} != set(lookup):
        raise ValueError("Reserve identity mismatch")
    for item in inputs:
        validate_input(item)
        if item["id"] != digest({k: v for k, v in item.items() if k != "id"}):
            raise ValueError("Changed input identity")
    for row in gold:
        if type(row["sufficient"]) is not bool:
            raise ValueError("Reserve label is not boolean")
        by_group[row["group"]].append(row)
    if any(len(rows) != 2 or {row["sufficient"] for row in rows} != {True, False}
           or len({lookup[row["id"]]["question"] for row in rows}) != 1
           for rows in by_group.values()):
        raise ValueError("Reserve is not paired")
    ordered_groups = sorted(by_group, key=lambda group: digest([VERSION, "reserve", group]))
    selected_groups = ordered_groups[skip:skip + pairs]
    if len(selected_groups) != pairs:
        raise ValueError("Not enough reserve pairs")
    selected_ids = {row["id"] for group in selected_groups for row in by_group[group]}
    selected_inputs = sorted((lookup[ident] for ident in selected_ids), key=lambda item: item["id"])
    selected_gold = [row for row in gold if row["id"] in selected_ids]
    questions = {item["question"] for item in selected_inputs}
    for split in ("development", "heldout"):
        if (set(selected_groups) & {r["group"] for r in load(source / f"{split}.gold.json")} or
                questions & {r["question"] for r in load(source / f"{split}.inputs.json")}):
            raise ValueError("Pilot overlaps development/heldout")
    return selected_inputs, selected_gold


def prepare(source, output, pairs, skip=0):
    selected_inputs, selected_gold = select_reserve(source, pairs, skip)
    output.mkdir(parents=True, exist_ok=False)
    write_new(output / "inputs.json", selected_inputs)
    write_new(output / "gold.json", selected_gold)
    write_new(output / "manifest.json", {
        "version": "musique-public-sufficiency-reserve-pilot-v2",
        "source_manifest_sha256": file_sha256(source / "manifest.json"),
        "source_inputs_sha256": file_sha256(source / "reserve.inputs.json"),
        "source_gold_sha256": file_sha256(source / "reserve.gold.json"),
        "selection": "sorted sha256([version, reserve, question_group])[skip:skip+pairs]",
        "skip": skip,
        "pairs": pairs,
        "inputs_sha256": file_sha256(output / "inputs.json"),
        "gold_sha256": file_sha256(output / "gold.json"),
        "labels_reused": True,
        "training": False,
    })
    print(json.dumps({"pairs": pairs, "inputs": len(selected_inputs),
                      "output": str(output)}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=32)
    parser.add_argument("--skip", type=int, default=0)
    args = parser.parse_args()
    if args.pairs < 1 or args.skip < 0:
        parser.error("--pairs must be positive and --skip non-negative")
    prepare(args.source, args.output, args.pairs, args.skip)
