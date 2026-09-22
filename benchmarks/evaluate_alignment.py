"""Small explicit development set; real local model, no API calls."""
import json
import sys
import tempfile
import platform
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research_assistant.translation.alignment_worker import Encoder,semantic_score,aligned_offsets

CASES=[
 ('The model retrieves evidence from scientific papers.','模型从科研论文中检索证据。','证据','evidence'),
 ('The model retrieves evidence from scientific papers.','模型从科研论文中检索证据。','模型','model'),
 ('The model retrieves evidence from scientific papers.','模型从科研论文中检索证据。','检索','retrieves'),
 ('Heart failure requires long-term treatment.','心力衰竭需要长期治疗。','治疗','treatment'),
 ('The experiment uses a small dataset.','实验使用一个小型数据集。','数据集','dataset'),
 ('The accuracy increased from 70 to 85 percent.','准确率从70提高到85个百分点。','85','85'),
 ('This method does not require training.','这种方法不需要训练。','训练','training'),
 ('Patients were excluded from the study.','患者被排除在研究之外。','患者','Patients'),
 ('Memory is stored in a local database.','记忆存储在本地数据库中。','记忆','Memory'),
 ('We compare two different methods.','我们比较两种不同的方法。','比较','compare'),
]


def main():
 root=Path(__file__).resolve().parents[1]
 e=Encoder(root/'.alignment_models/e803a2f501474044099b539160aaca461976ecd0',Path(tempfile.gettempdir())/'research_alignment_eval.sqlite3')
 rows=[]
 for en,zh,query,expected in CASES:
  so,sv=e.encode(en);to,tv=e.encode(zh)
  start=zh.index(query)
  match=aligned_offsets({'text':zh},range(start,start+len(query)),{'text':en},to,tv,so,sv)
  actual=' '.join(en[a:b] for a,b in match[0]) if match else None
  rows.append({'query':query,'expected':expected,'actual':actual,'exact':actual==expected,'sentence_score':round(semantic_score(tv,sv),4)})
 report={'set':'development, not held-out','cases':rows,'exact':sum(r['exact'] for r in rows),'total':len(rows),'api_calls':0,
         'platform':platform.platform(),'python':platform.python_version(),
         'graph':json.loads((root/'.alignment_models/e803a2f501474044099b539160aaca461976ecd0/portable.json').read_text())}
 output=root/'output/alignment-validation';output.mkdir(parents=True,exist_ok=True)
 (output/'development-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
