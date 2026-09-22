"""Isolated ONNX runtime: real text semantics -> source token offsets -> PDF boxes.

No network, no generative model. Coordinates only locate text in its OWN PDF.
"""
import hashlib
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer
import pymupdf as fitz

PROFILE = 'bilingual-sentence-v3-portable'
MAX_TOKENS = 240
MAX_SENTENCES = 1600


def compact(text):
    return re.sub(r'\s+', '', text)


def segment_box(segment):
    boxes = [c['box'] for c in segment['chars'] if c['box'] and c['c'].strip()]
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)] if boxes else None


def continues_same_sentence(previous, current):
    if previous['page'] != current['page'] or re.search(r'[。！？!?]|\.[\s"\'”’）)\]]*$', previous['text']):
        return False
    a, b = segment_box(previous), segment_box(current)
    if not a or not b:
        return False
    ah, bh = a[3]-a[1], b[3]-b[1]
    line_overlap = max(0, min(a[3], b[3])-max(a[1], b[1])) / max(1, min(ah, bh))
    if line_overlap > .5:
        return -2 <= b[0]-a[2] < max(24, ah*2)
    vertical_gap = a[1]-b[3]
    x_overlap = max(0, min(a[2], b[2])-max(a[0], b[0])) / max(1, min(a[2]-a[0], b[2]-b[0]))
    return -2 <= vertical_gap <= max(ah, bh)*.65 and x_overlap > .25


def sentences(path, page_numbers=None):
    """Keep character boxes in PDF user space, not translated layout coordinates."""
    result = []
    with fitz.open(path) as doc:
        for index in (page_numbers if page_numbers is not None else range(len(doc))):
            page = doc[index]
            inverse = ~page.transformation_matrix
            for block in page.get_text('rawdict', sort=True)['blocks']:
                if block['type'] != 0:
                    continue
                chars = []
                for line in block['lines']:
                    if abs(line['dir'][0] - 1) > .01 or abs(line['dir'][1]) > .01:
                        continue  # Vertical text is not silently treated as horizontal.
                    for span in line['spans']:
                        for c in span['chars']:
                            box = fitz.Rect(c['bbox']) * inverse
                            chars.append({'c': c['c'], 'box': list(box)})
                    chars.append({'c': ' ', 'box': None})
                text = ''.join(c['c'] for c in chars)
                # Preserve decimal numbers. Sentence boundaries never determine a pair.
                spans = list(re.finditer(r'.+?(?:[。！？!?]+|\.(?=\s+[A-Z]|\s*$)|$)', text))
                for match in spans:
                    start, end = match.span()
                    segment = chars[start:end]
                    if sum(c['c'].isalnum() for c in segment) < 2:
                        continue
                    result.append({'page': index + 1, 'text': text[start:end], 'chars': segment})
    merged = []
    for segment in result:
        if merged and continues_same_sentence(merged[-1], segment):
            merged[-1]['text'] += segment['text']
            merged[-1]['chars'] += segment['chars']
        else:
            merged.append(segment)
    return merged


def intersects(box, rects):
    if box is None:
        return False
    x0, y0, x1, y1 = box
    a = max(0, x1-x0)*max(0, y1-y0)
    return a > 0 and any(max(0,min(x1,r[2])-max(x0,r[0]))*max(0,min(y1,r[3])-max(y0,r[1]))/a > .45 for r in rects)


def selected_segments(segments, anchor):
    selected=[]
    for segment in segments:
        positions=[i for i,c in enumerate(segment['chars']) if c['c'].strip() and intersects(c['box'],anchor['rects'])]
        if positions:
            selected.append((segment,positions))
    actual=''.join(s['chars'][i]['c'] for s,indices in selected for i in indices)
    # Do not align a different repeated occurrence or invisible OCR/text artifact.
    if compact(actual) != compact(anchor['excerpt']):
        return []
    return selected


class Encoder:
    def __init__(self, model_dir, cache_path):
        model_dir=Path(model_dir)
        manifest=json.loads((model_dir/'manifest.json').read_text(encoding='utf-8'))
        for name in ('model.onnx','tokenizer.json'):
            if hashlib.sha256((model_dir/name).read_bytes()).hexdigest()!=manifest['files'][name]:
                raise ValueError('model hash mismatch')
        portable=json.loads((model_dir/'portable.json').read_text(encoding='utf-8'))
        portable_hash=hashlib.sha256((model_dir/'model.portable.onnx').read_bytes()).hexdigest()
        if portable['source_sha256']!=manifest['files']['model.onnx'] or portable_hash!=portable['sha256']:
            raise ValueError('derived model hash mismatch')
        self.fingerprint=portable_hash
        options=ort.SessionOptions()
        options.intra_op_num_threads=2
        options.inter_op_num_threads=1
        self.session=ort.InferenceSession(str(model_dir/'model.portable.onnx'),sess_options=options,providers=['CPUExecutionProvider'])
        self.tokenizer=Tokenizer.from_file(str(model_dir/'tokenizer.json'))
        self.tokenizer.no_truncation()
        self.cache=sqlite3.connect(cache_path,timeout=2)
        self.cache.execute('CREATE TABLE IF NOT EXISTS embeddings(key TEXT PRIMARY KEY, offsets TEXT, vectors BLOB, rows INTEGER, cols INTEGER)')

    def encode(self,text):
        key=hashlib.sha256((PROFILE+self.fingerprint+text).encode()).hexdigest()
        row=self.cache.execute('SELECT offsets,vectors,rows,cols FROM embeddings WHERE key=?',(key,)).fetchone()
        if row:
            try:
                return json.loads(row[0]),np.frombuffer(row[1],dtype=np.float16).reshape(row[2],row[3]).astype(np.float32)
            except (ValueError,TypeError):
                pass
        encoding=self.tokenizer.encode(text)
        if len(encoding.ids)>MAX_TOKENS:
            return [],None  # Never truncate and claim complete coverage.
        ids=np.array([encoding.ids],dtype=np.int64)
        inputs={'input_ids':ids,'attention_mask':np.ones_like(ids),'token_type_ids':np.zeros_like(ids)}
        out=self.session.run(None,{i.name:inputs[i.name] for i in self.session.get_inputs()})[0][0]
        indices=[i for i,(a,b) in enumerate(encoding.offsets) if b>a and any(c.isalnum() for c in text[a:b])]
        offsets=[encoding.offsets[i] for i in indices]
        vectors=out[indices].astype(np.float32)
        vectors/=np.maximum(np.linalg.norm(vectors,axis=1,keepdims=True),1e-8)
        if not indices:
            return [],None
        try:
            self.cache.execute('INSERT OR REPLACE INTO embeddings VALUES(?,?,?,?,?)',(key,json.dumps(offsets),vectors.astype(np.float16).tobytes(),*vectors.shape))
            self.cache.commit()
        except sqlite3.Error:
            pass
        return offsets,vectors


def semantic_score(a,b):
    sim=a@b.T
    # Bidirectional token coverage (BERTScore-style); no page or geometry term.
    return float((sim.max(axis=1).mean()+sim.max(axis=0).mean())/2)


def anchor_for(segment,ranges,original):
    chars=segment['chars']
    rects=[]
    for start,end in ranges:
        for c in chars[start:end]:
            b=c['box']
            if not b or not c['c'].strip():continue
            # Merge only adjacent source characters on the same line.
            if rects and abs(rects[-1][1]-b[1])<1 and abs(rects[-1][3]-b[3])<1 and 0<=b[0]-rects[-1][2]<4:
                rects[-1][2]=b[2]
            else:rects.append(b.copy())
    meta=original['metadata']['pages'][segment['page']-1]
    return {'artifact_id':original['id'],'artifact_hash':original['hash'],'page':segment['page'],
            'coordinate_system':'pdf_user_space','view_box':meta['view_box'],
            'rects':rects,'excerpt':' … '.join(segment['text'][a:b] for a,b in ranges),
            'mapping_status':'machine_aligned_unconfirmed','mapping_method':PROFILE}


def run(payload):
    started=time.monotonic()
    selected=selected_segments(sentences(payload['translation_path'],[payload['anchor']['page']-1]),payload['anchor'])
    if len(selected)!=1:
        return {'status':'unsupported','message':'无法可靠还原一个完整译文句；请在单句内重新选择。'}
    encoder=Encoder(payload['model_dir'],payload['cache_path'])
    originals=sentences(payload['original_path'])
    if len(originals)>MAX_SENTENCES:
        return {'status':'unsupported','message':'文档超过本地对齐首版的 1600 句上限，请使用手动关联。'}
    pool=[]
    for segment in originals:
        offsets,v=encoder.encode(segment['text'])
        if v is not None:pool.append((segment,offsets,v))
    if not pool:return {'status':'unmatched','message':'原件没有可用于对齐的文字句子。'}
    anchors=[];metrics=[]
    for target,_positions in selected:
        offsets,tv=encoder.encode(target['text'])
        if tv is None:
            return {'status':'unsupported','message':'当前句子超过 240 子词上限，未截断后继续猜测。'}
        ranked=sorted([(semantic_score(tv,sv),i) for i,(_,_,sv) in enumerate(pool)],reverse=True)
        score,index=ranked[0]
        margin=score-ranked[1][0] if len(ranked)>1 else 1.
        if score<.58 or margin<.012:
            return {'status':'unmatched','message':'对应原句不够明确或存在重复候选，未高亮推测位置。'}
        src,_so,_sv=pool[index]
        anchors.append(anchor_for(src,[(0,len(src['text']))],payload['original']))
        metrics.append({'sentence_similarity':round(score,4),'candidate_margin':round(margin,4)})
    return {'status':'aligned','alignment_unit':'sentence','anchors':anchors,'metrics':metrics,
            'elapsed_ms':round((time.monotonic()-started)*1000),
            'message':'本地模型完整句对齐（紫色），未经人工确认；高亮来自原文句自身文字坐标。'}


if __name__=='__main__':
    payload=json.loads(sys.stdin.read())
    try:
        print(json.dumps(run(payload),ensure_ascii=False,allow_nan=False))
    except Exception:
        # No extracted document text or full payloads in diagnostics.
        print('Local alignment worker failed',file=sys.stderr)
        sys.exit(1)
