"""Independent checks: 0.0.2 fixture executability and init absent destination."""
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'tests'))
from test_pb028 import build_authentic_002_artifact

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    artifact = build_authentic_002_artifact(base / 'artifact')
    cli = artifact / 'ai-dev-playbook/scripts/playbook.py'
    result = subprocess.run([sys.executable, str(cli), '--help'], capture_output=True, text=True)
    print(json.dumps({'case': 'declared_authentic_002_cli', 'returncode': result.returncode,
                      'stderr': result.stderr.strip(),
                      'cli_sha256': hashlib.sha256(cli.read_bytes()).hexdigest(),
                      'same_cli_as_current': cli.read_bytes() == (REPO / 'ai-dev-playbook/scripts/playbook.py').read_bytes()}, ensure_ascii=False))
    target = base / 'new-project'
    result = subprocess.run([sys.executable, str(REPO / 'ai-dev-playbook/scripts/playbook.py'),
                             'init', str(target), '--apply'], capture_output=True, text=True)
    print(json.dumps({'case': 'init_absent_destination', 'returncode': result.returncode,
                      'stderr': result.stderr.strip(), 'target_exists': target.exists()}, ensure_ascii=False))
