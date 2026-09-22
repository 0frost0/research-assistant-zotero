"""Read-only local model + existing PDF check, no paid service."""
import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research_assistant.translation.alignment_worker import sentences,run

root=Path(__file__).resolve().parents[1]
with sqlite3.connect((root/'.data/reading.sqlite3').as_uri()+'?mode=ro',uri=True) as con:
 con.row_factory=sqlite3.Row
 translated=dict(con.execute("SELECT * FROM artifacts WHERE kind='translation' ORDER BY created_at DESC LIMIT 1").fetchone())
 original=dict(con.execute('SELECT * FROM artifacts WHERE id=?',(translated['parent_id'],)).fetchone())
for a in (translated,original):a['metadata']=json.loads(a['metadata'])
path=lambda a:root/'.data/reading_artifacts'/(a['hash']+'.pdf')
parts=sentences(path(translated),[0])
query=sys.argv[1] if len(sys.argv)>1 else '痴呆'
segment=next((s for s in parts if query in s['text']),None)
if segment is None:
 print(json.dumps({'query':query,'status':'query_not_on_page','api_calls':0},ensure_ascii=False));sys.exit(0)
start=segment['text'].index(query)
anchor={'artifact_id':translated['id'],'artifact_hash':translated['hash'],'page':1,
 'coordinate_system':'pdf_user_space','view_box':translated['metadata']['pages'][0]['view_box'],
 'rects':[c['box'] for c in segment['chars'][start:start+len(query)] if c['box']], 'excerpt':query}
cache=root/'output/alignment-validation'
cache.mkdir(parents=True,exist_ok=True)
payload={'original_path':str(path(original)),'translation_path':str(path(translated)),
 'model_dir':str(root/'.alignment_models/e803a2f501474044099b539160aaca461976ecd0'),
 'cache_path':str(cache/'embeddings.sqlite3'),'anchor':anchor,'original':original}
result=run(payload)
print(json.dumps({'query':query,'status':result['status'],'message':result['message'],
 'matches':[{'page':a['page'],'text':a['excerpt'],'rectangles':len(a['rects'])} for a in result.get('anchors',[])],
 'metrics':result.get('metrics'),'elapsed_ms':result.get('elapsed_ms')},ensure_ascii=False))
