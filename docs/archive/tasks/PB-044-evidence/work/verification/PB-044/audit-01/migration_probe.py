"""Read-only independent baseline/current migration comparison."""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
root = Path('/Users/privedentsev1/Dev/ai-dev-playbook')
baseline_path = root / 'docs/tasks/PB-044-evidence/baseline.json'
baseline = json.loads(baseline_path.read_text())
files = []
for item in baseline['files']:
    dst = root / item['to']
    digest = 'sha256:' + hashlib.sha256(dst.read_bytes()).hexdigest() if dst.is_file() else None
    files.append({'from': item['from'], 'to': item['to'], 'baseline': item['sha256'],
                  'current': digest, 'size_matches': dst.is_file() and dst.stat().st_size == item['bytes'],
                  'source_absent': not (root / item['from']).exists(), 'matches': digest == item['sha256']})
cards = []
for item in baseline['cards']:
    text = (root / item['path']).read_text()
    fields = [line for line in text.splitlines() if line.startswith(('Status:', 'Closed:'))]
    cards.append({'path': item['path'], 'baseline': item['fields'], 'current': fields,
                  'matches': fields == item['fields'],
                  'source_directory_absent': not (root / 'docs/tasks' / (Path(item['path']).stem + '-evidence')).exists()})
result = {'baseline_path': str(baseline_path.relative_to(root)),
          'baseline_sha256': 'sha256:' + hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
          'files_count': len(files), 'cards_count': len(cards), 'files': files, 'cards': cards,
          'passed': len(files) == 119 and len(cards) == 6 and all(x['matches'] and x['size_matches'] and x['source_absent'] for x in files) and all(x['matches'] and x['source_directory_absent'] for x in cards)}
print(json.dumps(result, ensure_ascii=False, indent=2))
sys.exit(0 if result['passed'] else 1)
