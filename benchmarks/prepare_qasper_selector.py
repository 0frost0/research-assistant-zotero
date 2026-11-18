"""Download official QASPER v0.3 and derive auditable evidence-set supervision.

CC BY 4.0 dataset, Dasigi et al. (NAACL 2021), S2ORC paper text. No generated
questions/answers or external API labels. Each annotated supporting paragraph
is an evidence requirement proxy, NOT an independently annotated semantic facet.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import sys
import tarfile
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_assistant.retrieval.hybrid_retrieval import BM25Index
from research_assistant.retrieval.selector_dataset import SCHEMA, digest, make_example, write_dataset

URL = "https://qasper-dataset.s3.us-west-2.amazonaws.com/qasper-train-dev-v0.3.tgz"
CARD = "https://huggingface.co/datasets/allenai/qasper/raw/main/README.md"


def norm(text):
    return " ".join(text.split())


def download(folder):
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / "qasper-train-dev-v0.3.tgz"
    if not archive.exists():
        temporary = archive.with_suffix(".partial")
        with urllib.request.urlopen(URL, timeout=120) as response, temporary.open("xb") as output:
            while block := response.read(1024 * 1024):
                output.write(block)
        temporary.rename(archive)
    with tarfile.open(archive, "r:gz") as tar:
        for name in ("qasper-train-v0.3.json", "qasper-dev-v0.3.json"):
            path = folder / name
            if not path.exists():
                member = next(m for m in tar.getmembers() if Path(m.name).name == name and m.isfile())
                if member.size > 200_000_000:
                    raise ValueError("unexpected archive member size")
                with path.open("xb") as stream:
                    stream.write(tar.extractfile(member).read())
    card = folder / "dataset-card.md"
    if not card.exists():
        with urllib.request.urlopen(CARD, timeout=90) as response:
            card.write_bytes(response.read())
    manifest = {"url": URL, "license": "CC-BY-4.0", "citation": "Dasigi et al., NAACL 2021, arXiv:2105.03011",
                "card_url": CARD, "sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (archive, folder / "qasper-train-v0.3.json", folder / "qasper-dev-v0.3.json", card)}}
    (folder / "download-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def partition(paper, official):
    if official == "dev":
        return "test"  # Official dev is held out from training/model selection.
    return "dev" if int(digest(paper)[:8], 16) % 10 == 0 else "train"


def build(raw_splits, *, seed=20261110):
    stats, groups, examples, splits = Counter(), [], [], {s: [] for s in ("train", "dev", "test")}
    owners, cross_texts = {}, {}
    # Remove cross-split identical paragraphs before pooling (generic boilerplate
    # too). Near duplicates are not automatically detected and remain a caveat.
    for official, papers in raw_splits.items():
        for paper, body in papers.items():
            split = partition(paper, official)
            if paper in owners:
                raise ValueError("paper duplicated across official splits")
            owners[paper] = split
            splits[split].append(paper)
            for sec in body["full_text"]:
                for text in sec["paragraphs"]:
                    cross_texts.setdefault(digest(norm(text)), set()).add(split)
    rng = random.Random(seed)
    for official, papers in raw_splits.items():
        for paper, body in sorted(papers.items()):
            split = owners[paper]
            paragraphs, seen = [], set()
            for section in body["full_text"]:
                for text in section["paragraphs"]:
                    key = norm(text)
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    if len(cross_texts[digest(key)]) > 1:
                        stats["cross_split_duplicate_paragraphs_removed"] += 1
                        continue
                    # Keep whole paragraphs. Long paragraphs are counted, never clipped.
                    if len(text) > 2000:
                        stats["overlong_paragraphs_removed"] += 1
                        continue
                    paragraphs.append({"id": paper + ":" + digest(key)[:16], "paper": paper,
                                       "source_id": "qasper:" + paper, "page": len(paragraphs) + 1,
                                       "locator_type": "paragraph_ordinal_not_pdf_page", "quote": text,
                                       "section": section.get("section_name", "")})
            if not paragraphs:
                continue
            index = BM25Index([{"point_id": p["id"], "text": p["quote"],
                                "source": p["source_id"], "file_type": ".txt"} for p in paragraphs])
            lookup = {norm(p["quote"]): p for p in paragraphs}
            for qa in body["qas"]:
                stats["questions_seen"] += 1
                annotations = []
                unsupported = False
                for annotation in qa["answers"]:
                    answer = annotation["answer"]
                    if answer.get("unanswerable"):
                        continue
                    evidence = list(dict.fromkeys(norm(t) for t in answer.get("evidence", []) if t.strip()))
                    # Do not silently drop a table/figure or unmatched required paragraph.
                    if not evidence or any(t.startswith("FLOAT SELECTED") or t not in lookup for t in evidence):
                        unsupported = True
                        continue
                    annotations.append((annotation.get("annotation_id"), evidence))
                if not annotations or unsupported:
                    stats["questions_skipped_unmatched_float_or_no_evidence"] += 1
                    continue
                # Union is a conservative support-set supervision proxy. Retain each
                # annotator's alternative set separately for best-annotation metrics.
                gold = list(dict.fromkeys(t for _, ev in annotations for t in ev))
                if not 2 <= len(gold) <= 6:
                    stats["questions_skipped_not_2_to_6_evidence_paragraphs"] += 1
                    continue
                gold_ids = [lookup[t]["id"] for t in gold]
                dimensions = [{"id": "e" + str(i), "description": "QASPER human support paragraph " + str(i),
                               "category": "annotated_evidence_group"} for i in range(len(gold))]
                covers = {pid: [dimensions[i]["id"]] for i, pid in enumerate(gold_ids)}
                rank = index.search(qa["question"], k=20)
                ranked_ids = [r["point_id"] for r in rank]
                # If lexical scores are all zero, preserve paper order as lexical fallback.
                ranked_ids += [p["id"] for p in paragraphs if p["id"] not in ranked_ids]
                pool_ids = ranked_ids[:20]
                if split == "train":
                    # Ensure missing positives participate in training; evaluation is
                    # NEVER gold-injected, and candidate recall is reported separately.
                    pool_ids = gold_ids + [p for p in pool_ids if p not in gold_ids]
                    pool_ids = pool_ids[:20]
                    rng.shuffle(pool_ids)
                by_id = {p["id"]: p for p in paragraphs}
                candidates = [{**by_id[pid], "covers": covers.get(pid, [])} for pid in pool_ids]
                if split == "train":
                    # Natural overlapping windows: a support paragraph plus its
                    # adjacent paragraph. This introduces the SAME support under
                    # a different candidate ID without fabricating a paraphrase.
                    windows = []
                    for i, para in enumerate(paragraphs[:-1]):
                        pair = paragraphs[i:i + 2]
                        support = sorted({d for p in pair for d in covers.get(p["id"], [])})
                        text = "\n\n".join(p["quote"] for p in pair)
                        if support and len(text) <= 2000:
                            windows.append({**para, "id": paper + ":window:" + digest(text)[:16],
                                            "quote": text, "covers": support,
                                            "label_derivation": "whole_paragraph_containment"})
                        if len(windows) >= 2:
                            break
                    # Keep all gold paragraphs, at least one weak negative, <=20.
                    candidates = candidates[:20 - len(windows)] + windows
                # Need actual non-evidence candidates for a useful selection task.
                if len(candidates) <= 6 or not any(not c["covers"] for c in candidates):
                    stats["questions_skipped_small_pool"] += 1
                    continue
                group = {"id": "Q" + qa["question_id"], "paper": paper, "split": split,
                         "question": qa["question"], "dimensions": dimensions, "candidates": candidates,
                         "annotation_ids": [a for a, _ in annotations],
                         "alternative_evidence_sets": [[lookup[t]["id"] for t in ev] for _, ev in annotations],
                         "candidate_order": "training_gold_augmented" if split == "train" else "bm25_top20_no_gold_injection"}
                groups.append(group)
                stats[split + "_groups"] += 1
                positives = [c for c in candidates if c["covers"]]
                negatives = [c for c in candidates if not c["covers"]]
                # State-dependent pairs include overlapping windows with already
                # covered evidence (negative) and new support (positive).
                states = [[]] + [[p] for p in positives[:3]]
                if len(positives) > 2:
                    states.append(positives[:min(3, len(positives) - 1)])
                seen_examples = set()
                for selected in states:
                    remaining = [p for p in positives if p not in selected]
                    for c in remaining + negatives[:max(1, len(remaining))]:
                        row = make_example(group, selected, c)
                        if row["id"] not in seen_examples:
                            examples.append(row)
                            seen_examples.add(row["id"])
                stats[split + "_multi_evidence_groups"] += 1
    return {"schema": SCHEMA, "id": "qasper-selector-v1",
            "provenance": {"label_status": "public_evidence_derived", "independent_test": False,
                           "source": "QASPER v0.3", "license": "CC-BY-4.0",
                           "citation": "Dasigi et al., NAACL 2021, arXiv:2105.03011",
                           "supervision": "human paragraph support annotations; unselected paragraphs are weak negatives, not exhaustive relevance gold",
                           "coverage_proxy": "support paragraphs, NOT semantic facets; alternative annotations retained",
                           "test_protocol": "official dev reserved as test; official train hash10 held out for internal dev"},
            "paper_splits": {s: sorted(p) for s, p in splits.items()},
            "construction_stats": dict(stats), "groups": groups, "examples": examples}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = download(args.raw_dir)
    raw = {s: json.loads((args.raw_dir / f"qasper-{s}-v0.3.json").read_bytes()) for s in ("train", "dev")}
    data = build(raw)
    data["provenance"]["download_manifest"] = manifest
    print(json.dumps({"sha256": write_dataset(args.output, data), "stats": data["construction_stats"],
                      "examples": len(data["examples"])}, indent=2))


if __name__ == "__main__":
    main()
