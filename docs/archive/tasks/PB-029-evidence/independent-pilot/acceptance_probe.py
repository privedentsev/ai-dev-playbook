"""Independent acceptance probe; run with pilot project as cwd."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from src.validator import parse_age
cases = {'': None, ' ': None, '\t\n': None, '\u00a0': None,
         '0': 0, '25': 25, ' 42 ': 42, '\t17\n': 17}
for value, expected in cases.items():
    actual = parse_age(value)
    assert actual == expected, (repr(value), actual, expected)
    print(repr(value), '=>', repr(actual))
try:
    parse_age('abc')
except ValueError:
    print('non-numeric input: existing ValueError behavior preserved')
else:
    raise AssertionError('Unexpected change of non-numeric input behavior')
print('PASS: 9 independent cases')
