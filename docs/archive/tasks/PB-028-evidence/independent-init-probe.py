"""Independent PB-028 init conflict probes; only temporary projects are modified."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

def snapshot(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob('*'))

for kind in ('file', 'symlink'):
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        root = base / 'project'
        conflict = root / 'docs/archive/specs'
        conflict.parent.mkdir(parents=True)
        outside = base / 'outside'
        outside.mkdir()
        if kind == 'file':
            conflict.write_text('foreign archive path\n')
        else:
            conflict.symlink_to(outside, target_is_directory=True)
        before = snapshot(root)
        command = [sys.executable, str(REPO / 'ai-dev-playbook/scripts/playbook.py'),
                   'init', str(root), '--apply']
        result = subprocess.run(command, capture_output=True, text=True)
        after = snapshot(root)
        added = sorted(set(after) - set(before))
        print(json.dumps({'case': kind, 'returncode': result.returncode,
                          'stderr': result.stderr.strip(), 'added_count': len(added),
                          'added_sample': added[:12],
                          'kit_remains': (root / 'ai-dev-playbook/manifest.json').exists(),
                          'outside_unchanged': not list(outside.iterdir())}, ensure_ascii=False))
        assert result.returncode != 0
        assert not added, f'Added files left on disk for {kind}: {added}'
