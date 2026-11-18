import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'ai-dev-playbook/scripts'))
import checks_setup

def ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from ids(item)
        else:
            yield item.id()

cases = {
    'factory': {
        'base.py': 'import unittest\nBase = type("Base", (unittest.TestCase,), {"__module__": __name__, "test_ok": lambda self: None})\n',
        'test_a.py': 'from base import Base\n',
    },
    'mro': {
        'test_a.py': 'import unittest\nclass Base(unittest.TestCase):\n def test_ok(self): pass\nclass Left(Base):\n test_ok = None\nclass Right(Base): pass\nclass Child(Left, Right): pass\n',
    },
}
for label, files in cases.items():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        tests = root / 'tests'
        tests.mkdir()
        for name, body in files.items():
            (tests / name).write_text(body)
        result = checks_setup.survey(root)
        print(json.dumps({'case': label, 'predicted': [e['id'] for e in result['catalog_entries']], 'actual': list(ids(unittest.TestLoader().discover(str(tests)))), 'unknowns': result['unknowns']}, ensure_ascii=False))
    for name in ('test_a', 'base'):
        sys.modules.pop(name, None)
