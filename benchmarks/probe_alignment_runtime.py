"""Compare ONNX CPU optimization modes without caching model outputs."""
import json
from pathlib import Path
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

root=Path(__file__).resolve().parents[1]/'.alignment_models/e803a2f501474044099b539160aaca461976ecd0'
tokenizer=Tokenizer.from_file(str(root/'tokenizer.json'))
for label,level in [('disabled',ort.GraphOptimizationLevel.ORT_DISABLE_ALL),('basic',ort.GraphOptimizationLevel.ORT_ENABLE_BASIC),('all',ort.GraphOptimizationLevel.ORT_ENABLE_ALL)]:
 options=ort.SessionOptions();options.graph_optimization_level=level;options.intra_op_num_threads=2
 session=ort.InferenceSession(str(root/'model.onnx'),sess_options=options,providers=['CPUExecutionProvider'])
 vectors=[]
 for text in ['The model retrieves evidence from scientific papers.','模型从科研论文中检索证据。']:
  encoded=tokenizer.encode(text);ids=np.array([encoded.ids],dtype=np.int64)
  inputs={'input_ids':ids,'attention_mask':np.ones_like(ids),'token_type_ids':np.zeros_like(ids)}
  raw=session.run(None,{i.name:inputs[i.name] for i in session.get_inputs()})[0][0]
  v=raw[[i for i,(a,b) in enumerate(encoded.offsets) if b>a and any(c.isalnum() for c in text[a:b])]].astype(np.float32)
  v/=np.maximum(np.linalg.norm(v,axis=1,keepdims=True),1e-8);vectors.append(v)
 sim=vectors[0]@vectors[1].T
 print(json.dumps({'mode':label,'score':float((sim.max(0).mean()+sim.max(1).mean())/2),'min':float(sim.min()),'max':float(sim.max())}),flush=True)
