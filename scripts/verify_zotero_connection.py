"""Read configured plugin connection without ever printing its bearer token."""
import argparse
import json
from pathlib import Path
import re
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument('--prefs', type=Path, required=True)
args = parser.parse_args()
prefs = args.prefs.read_text(encoding='utf-8')
values = {}
for field in ('endpoint', 'token'):
    match = re.search(r'user_pref\("extensions\.researchAssistant\.' + field + r'",\s*("(?:[^"\\]|\\.)*")\);', prefs)
    values[field] = json.loads(match[1]) if match else ''
if not values['endpoint'] or not values['token']:
    raise SystemExit('Plugin endpoint/token is not configured')
request = urllib.request.Request(values['endpoint'].rstrip('/') + '/api/knowledge/status',
                                 headers={'Authorization': 'Bearer ' + values['token']})
with urllib.request.urlopen(request, timeout=10) as response:
    status = json.load(response)
print(json.dumps({'http': response.status, 'endpoint': values['endpoint'], **status}, ensure_ascii=False))
