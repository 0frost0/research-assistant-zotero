"""Compare recalculated evidence identity with migrated SQLite, no model calls."""
from pathlib import Path
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research_assistant.core.paths import PROJECT_ROOT,version_metadata
from research_assistant.retrieval.knowledge_base import LiteratureIndex
from research_assistant.retrieval.qdrant_index import INDEX_SCHEMA_VERSION

index=LiteratureIndex(PROJECT_ROOT/'library',backend='tfidf')
grouped=defaultdict(list)
for chunk in index.chunks:grouped[chunk.metadata['source']].append(chunk)
rows=[]
with sqlite3.connect(PROJECT_ROOT/'.data/evidence.sqlite3') as con:
 for source,chunks in grouped.items():
  h=hashlib.sha256(INDEX_SCHEMA_VERSION.encode())
  for chunk in chunks:
   h.update(chunk.page_content.encode())
   h.update(json.dumps(version_metadata(chunk.metadata),ensure_ascii=False,sort_keys=True,default=str).encode())
  stored=con.execute('SELECT active_version FROM documents WHERE source=?',(source,)).fetchone()
  rows.append({'source':source,'chunks':len(chunks),'version_matches':bool(stored and stored[0]==h.hexdigest())})
print(json.dumps(rows,ensure_ascii=True))
if not all(r['version_matches'] for r in rows):raise SystemExit(1)
