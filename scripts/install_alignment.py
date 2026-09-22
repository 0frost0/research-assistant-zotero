"""Download a pinned public CPU model. Never sends document contents."""
from pathlib import Path
import hashlib
import json
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'cstr/awesome-align-onnx-int8'
REVISION = 'e803a2f501474044099b539160aaca461976ecd0'


def main():
    dest = ROOT / '.alignment_models' / REVISION
    dest.mkdir(parents=True, exist_ok=True)
    manifest = {'model': MODEL, 'revision': REVISION, 'files': {}}
    for name in ('model.onnx', 'tokenizer.json', 'README.md', 'config.json'):
        path = dest / name
        if not path.exists():
            request = urllib.request.Request(f'https://huggingface.co/{MODEL}/resolve/{REVISION}/{name}', headers={'User-Agent': 'research-assistant-installer'})
            temp = path.with_suffix(path.suffix + '.part')
            with urllib.request.urlopen(request, timeout=60) as response, temp.open('wb') as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            temp.replace(path)
        manifest['files'][name] = hashlib.sha256(path.read_bytes()).hexdigest()
        print(name, path.stat().st_size, flush=True)
    (dest / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    from prepare_alignment_model import prepare
    prepare(dest)


if __name__ == '__main__':
    main()
