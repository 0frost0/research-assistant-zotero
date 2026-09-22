"""Build draft gold from preselected source blocks, NEVER from retrieval rankings."""

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research_assistant.ingestion.mineru_extractor import MinerUExtractor, _content_list_text
from research_assistant.evaluation.retrieval_evaluation import corpus_snapshot, evidence_catalog, validate_dataset

D = "Dementia-Agents.pdf"
H = "Guideline for the Management of Heart Failure.pdf"


def build():
    catalog = evidence_catalog(ROOT)
    extractor = MinerUExtractor()
    blocks, paths = {}, {}
    for source in (D, H):
        paths[source] = extractor._find_content_list(extractor.cache_path(ROOT / "library" / source))
        blocks[source] = json.loads(paths[source].read_text(encoding="utf-8"))

    def target(source, ordinal, modality):
        block = blocks[source][ordinal]
        page = block["page_idx"] + 1
        anchor = " ".join(_content_list_text(block).split())[:100]
        if modality == "image":
            path = (paths[source].parent / block["img_path"]).resolve()
            matches = [pid for pid, item in catalog.items() if item["modality"] == modality and
                       item["source"] == source and item["page"] == page and Path(item["image_path"]).resolve() == path]
        else:
            matches = [pid for pid, item in catalog.items() if item["modality"] == modality and
                       item["source"] == source and item["page"] == page and anchor in " ".join(item["text"].split())]
        if not matches:
            raise ValueError(f"Unresolved preselected block: {source}/{ordinal}")
        return {"source": source, "page": page, "block_index": ordinal, "modality": modality,
                "bbox": block.get("bbox"), "anchor": anchor, "any_of": matches}

    # Explicit source targets selected before running the evaluated retriever.
    specs = [
        ("T01", "text", "心力衰竭的临床定义是什么？", [(H, 260, "text")]),
        ("T02", "text", "NYHA 功能分级依据什么，它是否会随时间变化？", [(H, 270, "text")]),
        ("T03", "text", "HFimpEF 患者射血分数改善后，是否仍需继续 HFrEF 治疗？", [(H, 296, "text")]),
        ("T04", "text", "How many classes does Dementia-Agents use for staging, and which probability activation is used?", [(D, 34, "text")]),
        ("T05", "text", "How is phenotype prediction formulated in Dementia-Agents: how many labels and which activation?", [(D, 37, "text")]),
        ("T06", "text", "Dementia-Agents 的 Data Agent 如何处理缺失值并路由临床数据？", [(D, 19, "text")]),
        ("I01", "image", "找出 Dementia-Agents 从临床数据路由到专家再到诊断聚合的整体流程图。", [(D, 8, "image")]),
        ("I02", "image", "Find the figure showing the encoder and classifier architecture of each expert in Dementia-Agents.", [(D, 23, "image")]),
        ("I03", "image", "找出 Dementia-Agents 训练、验证、测试集表型标签支持数及长尾分布的图表。", [(D, 49, "image")]),
        ("I04", "image", "找出心衰 ACC/AHA 从 A 到 D 四个阶段的示意图。", [(H, 277, "image")]),
        ("I05", "image", "Find the heart failure diagnostic algorithm figure with LVEF-based classification.", [(H, 313, "image")]),
        ("I06", "image", "请定位 HFrEF Stage C 和 D 推荐治疗的流程图。", [(H, 716, "image")]),
        ("M01", "mixed", "结合整体流程图和方法文字，说明 Dementia-Agents 的三个主要步骤。", [(D, 8, "image"), (D, 17, "text")]),
        ("M02", "mixed", "结合专家架构图和 Task Formulation 文字，说明编码器与分类器如何连接。", [(D, 23, "image"), (D, 29, "text")]),
        ("M03", "mixed", "结合表型标签分布图表及文字，解释 Dementia-Agents 表型数据划分如何保留标签比例。", [(D, 49, "image"), (D, 51, "text")]),
        ("M04", "mixed", "结合 ACC/AHA 心衰阶段示意图与章节说明，解释分期为何强调疾病进展。", [(H, 277, "image"), (H, 262, "text")]),
        ("M05", "mixed", "结合诊断流程图与正文说明，HFmrEF 和 HFpEF 的诊断需要什么客观证据？", [(H, 313, "image"), (H, 303, "text")]),
        ("M06", "mixed", "结合 Dementia-Agents 消融表格与对应文字，解释移除一个专家会怎样影响性能。", [(D, 61, "image"), (D, 63, "text")]),
    ]
    cases = [{"id": cid, "query_type": kind, "input_modality": "text", "question": question,
              "split": "development", "gold_status": "draft", "annotation_origin": "assistant_seed",
              "expected": [target(*entry) for entry in entries]}
             for cid, kind, question, entries in specs]
    # Identical-corpus-image inputs only test transport/joint embedding, not generalization.
    for number, cid in enumerate(("I01", "I05", "M02", "M05"), 1):
        case = copy.deepcopy(next(c for c in cases if c["id"] == cid))
        case.update(id=f"P{number:02d}", split="self_match_probe",
                    input_modality="image" if number < 3 else "mixed",
                    query_image_id=case["expected"][0]["any_of"][0])
        if number < 3:
            case["question"] = ""
        cases.append(case)
    for cid, question in [("N01", "本资料库是否记录了 2030 年某指定医院心衰再入院率的实际观测结果？"),
                          ("N02", "给出本资料库中火星表面土壤矿物测量实验的图表和原始数据。")]:
        cases.append({"id": cid, "query_type": "text", "input_modality": "text", "question": question,
                      "split": "development", "gold_status": "draft", "annotation_origin": "assistant_seed",
                      "unanswerable": True, "expected": []})
    dataset = {"schema": 1, "id": "research-multimodal-dev-v1", "purpose": "development_not_acceptance",
               "page_numbering": "PDF physical pages, one-based; NOT printed page labels",
               "query_type_definition": "target evidence: text, image, or both; see input_modality for actual input",
               "gold_policy": "Assistant seeds require explicit human approval. Expand incomplete relevance sets during review.",
               "corpus": corpus_snapshot(ROOT, catalog), "cases": cases}
    validate_dataset(dataset, ROOT)
    return dataset


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "benchmarks/multimodal_eval_v1.json")
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Refusing to overwrite existing gold. Choose a NEW --output dataset version.")
    args.output.write_text(json.dumps(build(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)
