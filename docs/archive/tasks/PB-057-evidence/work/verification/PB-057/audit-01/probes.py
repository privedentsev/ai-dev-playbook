"""Run from repository root: python3 work/verification/PB-057/audit-01/probes.py."""
import copy
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'ai-dev-playbook/scripts'))
import checks_contract as cc

def require(condition, message):
    if not condition:
        raise ValueError(message)

policy = json.loads(Path('tests/fixtures/checks_contract/policy.json').read_text())
for pattern in ('**', '**/*', '**/payments/**'):
    candidate = copy.deepcopy(policy)
    candidate['excludes'] = [{'pattern': pattern, 'reason': 'audit probe', 'origin': 'manual'}]
    cc.validate_policy(candidate, require)
    print('A-01: exclude', pattern, 'ACCEPTED despite intersection with declared inputs')
candidate = copy.deepcopy(policy)
candidate['checks'].append({'id': 'new-command', 'title': 'Audit', 'purpose': 'Audit', 'kind': 'command', 'argv': ['unapproved-tool', '--execute'], 'timeout_seconds': 60})
cc.validate_policy(candidate, require)
print('A-02: new unapproved argv:', cc.classify_policy_change(policy, candidate))
result = json.loads(Path('tests/fixtures/checks_contract/result.json').read_text())
result['tests']['expected'].append(result['tests']['expected'][0])
cc.validate_result(result, require)
print('A-03: duplicate expected test ID ACCEPTED')
candidate = copy.deepcopy(policy)
candidate['test_catalog']['entries'][0]['expected_failure'] = {'allowed': True, 'reason': 'known bug'}
try:
    cc.validate_policy(candidate, require)
except ValueError as exc:
    print('A-04: expected_failure approval cannot be expressed:', exc)
else:
    raise AssertionError('Probe behavior changed')
