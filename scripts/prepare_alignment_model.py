"""Avoid AVX2 U8/S8 saturation using float accumulation of EXISTING quantized weights.

No training/download. Original model is retained; this is a derived ONNX graph.
"""
import hashlib
import json
from pathlib import Path
import onnx
from onnx import helper,TensorProto


def prepare(folder):
    folder=Path(folder)
    source=folder/'model.onnx';target=folder/'model.portable.onnx'
    source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    record=folder/'portable.json'
    if record.exists() and target.exists():
        data=json.loads(record.read_text())
        if data.get('source_sha256')==source_hash and data.get('sha256')==hashlib.sha256(target.read_bytes()).hexdigest():return data
    model=onnx.load(source)
    nodes=[];outputs=set();count=0
    for node in model.graph.node:
        if node.op_type!='MatMulInteger':nodes.append(node);continue
        count+=1;args=[]
        for i in range(2):
            cast=node.name+f'_fp32_{i}'
            nodes.append(helper.make_node('Cast',[node.input[i]],[cast],to=TensorProto.FLOAT))
            if len(node.input)>i+2 and node.input[i+2]:
                zero=cast+'_zero';center=cast+'_center'
                nodes.append(helper.make_node('Cast',[node.input[i+2]],[zero],to=TensorProto.FLOAT))
                nodes.append(helper.make_node('Sub',[cast,zero],[center]));cast=center
            args.append(cast)
        nodes.append(helper.make_node('MatMul',args,list(node.output),name=node.name+'_portable'))
        outputs.update(node.output)
    del model.graph.node[:];model.graph.node.extend(nodes)
    for value in model.graph.value_info:
        if value.name in outputs:value.type.tensor_type.elem_type=TensorProto.FLOAT
    onnx.checker.check_model(model)
    pending=target.with_suffix('.pending.onnx');onnx.save(model,pending);pending.replace(target)
    data={'source_sha256':source_hash,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
          'transformation':'int8-values-fp32-matmul-v1','replaced_matmuls':count,'downloaded_weights':False}
    record.write_text(json.dumps(data,indent=2),encoding='utf-8')
    return data


if __name__=='__main__':
    print(json.dumps(prepare(Path(__file__).resolve().parents[1]/'.alignment_models/e803a2f501474044099b539160aaca461976ecd0')))
