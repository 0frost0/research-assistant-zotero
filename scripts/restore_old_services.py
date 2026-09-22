"""Old server only. Start existing MinerU weights offline; no model transfer."""
import os
import socket
import subprocess
from pathlib import Path

ROOT=Path('/home/dingone/dst/research_assistant')
MODEL=Path('/home/dingone/.cache/modelscope/models/OpenDataLab--MinerU2.5-Pro-2605-1.2B/snapshots/master')
if not (MODEL/'config.json').is_file():raise SystemExit('Existing MinerU weights missing; no download allowed')
with socket.socket() as check:
    if check.connect_ex(('127.0.0.1',30000))==0:raise SystemExit('Port 30000 occupied; do not replace existing process')
env=dict(os.environ,CUDA_VISIBLE_DEVICES='1',CUDA_DEVICE_ORDER='PCI_BUS_ID',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
with (ROOT/'logs/mineru-restore-20260914.log').open('ab') as log:
    p=subprocess.Popen([str(ROOT/'.venv/bin/mineru-vllm-server'),'--model',str(MODEL),'--host','127.0.0.1','--port','30000','--gpu-memory-utilization','0.5','--max-model-len','8192'],cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
(ROOT/'run/mineru-restore-20260914.pid').write_text(str(p.pid))
print('Existing MinerU started:',p.pid)
