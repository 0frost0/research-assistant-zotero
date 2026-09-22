"""Restore searchable vectors from preserved evidence using the SAME local model."""
import json
import sqlite3
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research_assistant.core.paths import PROJECT_ROOT
from research_assistant.retrieval.knowledge_base import LiteratureIndex

started=time.monotonic()
with sqlite3.connect(PROJECT_ROOT/'.data/evidence.sqlite3') as con:
 before={r[0] for r in con.execute('SELECT evidence_id FROM evidence WHERE active=1')}
print(json.dumps({'stage':'rebuild_start','existing_active_evidence':len(before)}),flush=True)
index=LiteratureIndex(PROJECT_ROOT/'library',backend='multimodal')
if index.unified_error:raise RuntimeError(index.unified_error)
with sqlite3.connect(PROJECT_ROOT/'.data/evidence.sqlite3') as con:
 after={r[0] for r in con.execute('SELECT evidence_id FROM evidence WHERE active=1')}
if before!=after:raise RuntimeError('Evidence identities changed during rebuild')
print(json.dumps({'stage':'complete','evidence_ids_preserved':len(after),'seconds':round(time.monotonic()-started,2),
 'collection':index.unified_index.collection_name,'api_generation_calls':0}),flush=True)
