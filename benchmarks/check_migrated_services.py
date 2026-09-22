"""Real local GPU smoke checks; no paid generation and no library writes."""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from research_assistant.retrieval.knowledge_base import LiteratureIndex
from research_assistant.ingestion.mineru_extractor import MinerUExtractor


def main():
    report={'paid_api_calls':0}
    index=LiteratureIndex(ROOT/'library',backend='multimodal')
    if index.unified_error:raise RuntimeError(index.unified_error)
    hits=index.search_results('心力衰竭的定义是什么',k=3)
    assert hits,'Chinese query returned no results'
    report['text_query']={'hits':len(hits),'sources':sorted({h.source for h in hits})}
    with sqlite3.connect(ROOT/'.data/evidence.sqlite3') as con:
        image_path=Path(con.execute("SELECT image_path FROM visual_assets WHERE image_path IS NOT NULL LIMIT 1").fetchone()[0])
    assert image_path.is_file()
    visual=index.unified_index.search('',query_image=image_path,k=3,min_score=0)
    assert visual,'Image query returned no results'
    report['image_query']={'hits':len(visual),'asset_exists':True}
    with tempfile.TemporaryDirectory(prefix='research-mineru-smoke-') as directory:
        folder=Path(directory);path=folder/'synthetic.pdf'
        writer=PdfWriter();page=writer.add_blank_page(width=600,height=800)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        content=DecodedStreamObject();content.set_data(b'BT /F1 20 Tf 60 700 Td (Migration smoke test) Tj 0 -40 Td /F1 12 Tf (Patients receive treatment. Accuracy is 85 percent.) Tj ET')
        page[NameObject('/Contents')]=writer._add_object(content)
        writer.write(path)
        runtime=ROOT/('.benchmark_envs/mineru/Scripts/python.exe' if os.name=='nt' else '.gpu_env/bin/python')
        parser=MinerUExtractor(cache_dir=folder/'cache',python_executable=runtime,timeout_seconds=240)
        documents,meta=parser.parse(path)
        text=' '.join(d.page_content for d in documents).lower()
        assert 'patients' in text and '85' in text,'Synthetic content missing'
        report['mineru']={'parsed_blocks':len(documents),'expected_text_present':True,'python':str(parser.python_executable)}
    output=ROOT/'output/migration';output.mkdir(parents=True,exist_ok=True)
    (output/'services.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
