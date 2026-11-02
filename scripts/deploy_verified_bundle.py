"""Install a code-only release on the authorized Linux host; data stays untouched.

Run with system Python as ubuntu: deploy_verified_bundle.py bundle.tar.gz.
Backs up overwritten files and installs project-specific systemd units.
"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess as sp
import sys
import tarfile
import time

APP = Path('/home/ubuntu/research-assistant/app')
DATA = Path('/mnt/research-data/research-assistant')
stamp = time.strftime('%Y%m%d-%H%M%S')
release = DATA / 'releases' / stamp


def run(*args):
    return sp.check_output(args, text=True).strip()


def main():
    assert APP.is_dir() and DATA.is_dir()
    assert run('findmnt', '-n', '-o', 'TARGET', '--target', str(DATA)) == '/mnt/research-data'
    release.mkdir(parents=True, exist_ok=False)
    staging = release / 'source'
    staging.mkdir()
    allowed = {'research_assistant', 'tests', 'benchmarks', 'deployment', 'scripts', '.github', 'README.md', 'AGENTS.md', 'docs', 'updates.json', '问题排查笔记.md', '.env.reading.example'}
    with tarfile.open(sys.argv[1]) as archive:
        for member in archive.getmembers():
            path = Path(member.name)
            assert not path.is_absolute() and '..' not in path.parts
            assert path.parts[0] in allowed and not member.issym() and not member.islnk()
        archive.extractall(staging, filter='data')
    files = [p for p in staging.rglob('*') if p.is_file()]
    backup = release / 'before'
    manifest = {}
    for source in files:
        relative = source.relative_to(staging)
        target = APP / relative
        assert target.resolve().is_relative_to(APP), str(target)
        if target.exists():
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
        manifest[str(relative)] = hashlib.sha256(source.read_bytes()).hexdigest()
    # Stop accepting requests while replacing Python sources, never alter documents.
    sp.run(['sudo', '-n', 'systemctl', 'stop', 'research-assistant-web'], stderr=sp.DEVNULL)
    sp.run(['tmux', 'send-keys', '-t', 'research-web', 'C-c'], stderr=sp.DEVNULL)
    time.sleep(3)
    sp.run(['tmux', 'kill-session', '-t', 'research-web'], stderr=sp.DEVNULL)
    for source in files:
        target = APP / source.relative_to(staging)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        assert hashlib.sha256(target.read_bytes()).hexdigest() == manifest[str(source.relative_to(staging))]
    (release / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    env_path = APP / '.env'
    if not env_path.exists():
        with env_path.open('x') as stream:
            stream.write('# Fill these values; this file is private and excluded from Git.\nOPENAI_API_KEY=\nOPENAI_BASE_URL=\nOPENAI_MODEL=\nRESEARCH_ASSISTANT_MODEL_TIMEOUT=20\nRESEARCH_TRANSLATION_ENABLED=false\nRESEARCH_ASSISTANT_ENABLE_LANGSMITH=false\n')
        env_path.chmod(0o600)
    # Optional explicit role configuration, never change the user's main model.
    if len(sys.argv) > 2:
        review_model = sys.argv[2]
        assert review_model == 'GLM-4.7', 'Unvalidated review model'
        content = env_path.read_text()
        if not any(line.startswith('RESEARCH_QA_REVIEW_MODEL=') for line in content.splitlines()):
            with env_path.open('a') as stream:
                stream.write('\nRESEARCH_QA_REVIEW_MODEL=' + review_model + '\n')
            env_path.chmod(0o600)
    units = {
        'qdrant': (f'{APP}/runtime/qdrant/qdrant --disable-telemetry', [
            'QDRANT__SERVICE__HOST=127.0.0.1', f'QDRANT__STORAGE__STORAGE_PATH={APP}/runtime/qdrant/storage']),
        'embedding': (f'{APP}/.gpu_env/bin/python -u deployment/embedding/remote_embedding_service.py', []),
        'mineru': (f'{APP}/.gpu_env/bin/mineru-vllm-server --model {APP}/models/OpenDataLab--MinerU2.5-Pro-2605-1.2B/snapshots/master --host 127.0.0.1 --port 30000 --gpu-memory-utilization 0.30 --max-model-len 8192', [
            f'CPATH={APP}/runtime/python-dev/usr/include/python3.12:{APP}/runtime/python-dev/usr/include:{APP}/runtime/python-dev/usr/include/x86_64-linux-gnu']),
        'web': (f'{APP}/.venv/bin/python -B web_app.py', [
            'RESEARCH_ASSISTANT_QDRANT_URL=http://127.0.0.1:6333',
            'RESEARCH_ASSISTANT_EMBEDDING_URL=http://127.0.0.1:30001',
            'RESEARCH_ASSISTANT_MINERU_URL=http://127.0.0.1:30000',
            f'RESEARCH_ASSISTANT_MINERU_PYTHON={APP}/.gpu_env/bin/python',
            'NO_PROXY=localhost,127.0.0.1']),
    }
    for name, (command, env) in units.items():
        unit_name = f'research-assistant-{name}.service'
        target = Path('/etc/systemd/system') / unit_name
        if target.exists():
            shutil.copy2(target, release / (unit_name + '.before'))
        text = '[Unit]\nDescription=Research Assistant ' + name + '\nAfter=network-online.target\nRequiresMountsFor=/mnt/research-data\nConditionPathIsMountPoint=/mnt/research-data\nStartLimitIntervalSec=300\nStartLimitBurst=3\n\n[Service]\nType=simple\nUser=ubuntu\nGroup=ubuntu\nWorkingDirectory=' + str(APP) + '\n'
        for value in ['HF_HUB_OFFLINE=1', 'TRANSFORMERS_OFFLINE=1', 'TOKENIZERS_PARALLELISM=false', 'CUDA_VISIBLE_DEVICES=0', *env]:
            text += 'Environment=' + value + '\n'
        if name == 'web':
            text += f'EnvironmentFile=-{APP}/.env\n'
        text += f'ExecStart={command}\nRestart=on-failure\nRestartSec=10\nTimeoutStopSec=30\nKillMode=control-group\nUMask=0077\nStandardOutput=append:{APP}/logs/{name}.log\nStandardError=append:{APP}/logs/{name}.log\n\n[Install]\nWantedBy=multi-user.target\n'
        source = release / unit_name
        source.write_text(text)
        run('sudo', '-n', 'install', '-m', '644', str(source), str(target))
    for name in ['mineru', 'embedding', 'qdrant']:
        sp.run(['tmux', 'send-keys', '-t', 'research-' + name, 'C-c'], stderr=sp.DEVNULL)
    time.sleep(5)
    for name in ['mineru', 'embedding', 'qdrant']:
        sp.run(['tmux', 'kill-session', '-t', 'research-' + name], stderr=sp.DEVNULL)
    run('sudo', '-n', 'systemctl', 'daemon-reload')
    # A deployment intentionally stops and starts each unit.  Clear the
    # previous failed/start-limit counter first, otherwise several quick
    # verified releases can make systemd reject the next healthy start with
    # ``start-limit-hit`` even though the process itself exits cleanly.
    for name in units:
        sp.run(['sudo', '-n', 'systemctl', 'reset-failed',
                'research-assistant-' + name], check=False)
    for name in units:
        run('sudo', '-n', 'systemctl', 'enable', '--now', 'research-assistant-' + name)
    print(json.dumps({'release': str(release), 'verified_files': len(files)}))


if __name__ == '__main__':
    main()
