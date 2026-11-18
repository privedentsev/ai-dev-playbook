"""Independent PB-080 probe: child import, natural cache, preserved timestamps."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(root / 'tests'), str(root / 'ai-dev-playbook/scripts')]
import checks_unittest
from test_checks_run import run_policy

with tempfile.TemporaryDirectory(prefix='pb080-independent-') as tmp:
    project = Path(tmp).resolve()
    (project / 'tests').mkdir()
    (project / 'lib').mkdir()
    source = project / 'lib/value.py'
    source.write_text('value = 1\n')
    observed = project / 'seen.json'
    (project / 'tests/test_payments.py').write_text(
        'import unittest,subprocess,sys,json\nfrom pathlib import Path\n'
        'class T(unittest.TestCase):\n def test_child(self):\n'
        '  value=subprocess.check_output([sys.executable,"-c",'
        '"from value import value; print(value)"],text=True).strip()\n'
        f'  Path({str(observed)!r}).write_text(json.dumps({{"value":value,'
        '"prefix":sys.pycache_prefix,"dont_write":sys.dont_write_bytecode}))\n'
        '  self.assertEqual(value,"1")\n')
    env = dict(os.environ, PYTHONPATH=str(project / 'lib'))
    env.pop('PYTHONDONTWRITEBYTECODE', None)
    env.pop('PYTHONPYCACHEPREFIX', None)
    subprocess.run([sys.executable, '-c', 'import value'], cwd=project,
                   env=env, check=True)
    caches = {p: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in project.rglob('*.pyc')}
    assert caches
    before = source.stat()
    source.write_text('value = 2\n')
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert source.stat().st_size == before.st_size
    run = project / 'run'
    run.mkdir()
    result = checks_unittest.collect(project, run_policy()['checks'][0], run,
                                    timeout=20, env=env)
    seen = json.loads(observed.read_text())
    assert result['report']['actual'][0]['outcome'] == 'failed', result
    assert seen['value'] == '2' and seen['dont_write']
    assert not Path(seen['prefix']).exists()
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == h
               for p, h in caches.items())
    print(json.dumps({'observed': seen, 'actual': result['report']['actual'],
                      'cache_preserved': True, 'private_prefix_removed': True}, indent=2))
