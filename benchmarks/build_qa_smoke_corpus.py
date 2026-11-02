"""Small synthetic bilingual retrieval smoke test, NOT scientific benchmark gold."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from research_assistant.knowledge.store import KnowledgeStore, digest


def build(root):
    root.mkdir(parents=True, exist_ok=False)
    store = KnowledgeStore(root)
    base = store.create({"connection": "qa-smoke", "library": "user:0", "collection": "A", "name": "Synthetic QA"})
    passages = [
        "In a synthetic randomized trial, 200 adults with heart failure received treatment A. The primary endpoint was all-cause mortality at 12 months. Treatment A did not reduce mortality in this small cohort. Patients with severe renal impairment were excluded.",
        "In a synthetic study of 500 adults with heart failure, treatment B reduced hospital admissions over 24 months. This observational study did not assess mortality. Its nonrandomized design limits causal conclusions.",
    ]
    entities, hashes = [], []
    for i, passage in enumerate(passages):
        writer = PdfWriter()
        page = writer.add_blank_page(width=600, height=800)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        words, lines, current = passage.split(), [], ""
        for word in words:
            if len(current + word) > 75:
                lines.append(current.strip())
                current = ""
            current += word + " "
        lines.append(current.strip())
        stream.set_data(('BT /F1 11 Tf 14 TL 45 730 Td ' + ' '.join(f'({line}) Tj T*' for line in lines) + ' ET').encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
        path = root / f'synthetic-{i}.pdf'
        writer.write(path)
        content = path.read_bytes()
        sha = digest(content)
        hashes.append(sha)
        store.put_object(sha, content)
        entities.append({"key": f"PDF0000{i}", "kind": "document", "title": f"Synthetic study {i}", "hash": sha})
    store.sync(base['id'], {"name": base['name'], "include_children": False, "complete": True, "expected_revision": 0, "entities": entities})
    questions = [
        ("A研究纳入多少名心衰患者？", 0, "200 adults with heart failure"),
        ("A治疗能降低死亡率吗？", 0, "Treatment A did not reduce mortality"),
        ("什么患者被排除在研究之外？", 0, "severe renal impairment were excluded"),
        ("观察性研究的随访时间是多久？", 1, "over 24 months"),
        ("B研究能证明因果关系吗？", 1, "nonrandomized design limits causal conclusions"),
        ("我记得有篇文章降低了住院率，是哪篇？", 1, "reduced hospital admissions"),
        ("这两篇论文是否比较了脑肿瘤手术效果？", None, ""),
        ("A治疗的药物剂量是多少？", None, ""),
    ]
    cases = [{"id": f"S{i + 1:02}", "query": q, "kb_ids": [base['id']], "no_answer": doc is None,
              "expected": [] if doc is None else [{"source_id": hashes[doc] + '.pdf', "page": 1, "quote": quote}]}
             for i, (q, doc, quote) in enumerate(questions)]
    (root / 'dataset.json').write_text(json.dumps({"id": "synthetic-qa-smoke-v1", "split": "synthetic", "cases": cases}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(root / 'dataset.json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=Path)
    build(parser.parse_args().root)
