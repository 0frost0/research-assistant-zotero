"""Explicit synthetic test; run on the embedding host, no private corpus."""
import base64
import io
import json
import math
import urllib.request
from PIL import Image, ImageDraw

if __name__ == '__main__':
    image = Image.new('RGB', (128, 128), 'white')
    ImageDraw.Draw(image).rectangle((24, 32, 104, 96), fill='red')
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    payload = {'inputs': [{'text': 'A red rectangle on a white background.'},
                          {'text': '白色背景上的红色矩形'},
                          {'image_base64': base64.b64encode(stream.getvalue()).decode(),
                           'text': 'A red rectangle.'}]}
    request = urllib.request.Request('http://127.0.0.1:30001/embed', data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=120) as response:
        result = json.load(response)
    vectors = result['vectors']
    assert len(vectors) == 3
    assert all(len(v) == 2048 and all(math.isfinite(n) for n in v) for v in vectors)
    print(json.dumps({'items': len(vectors), 'dimensions': [len(v) for v in vectors],
                      'norms': [round(math.sqrt(sum(n*n for n in v)), 5) for v in vectors],
                      'protocol': 'legacy /embed', 'synthetic_only': True}))
