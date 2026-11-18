"""Run from repository root; independent PB-057 re-audit probes."""
import copy
import fnmatch
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'ai-dev-playbook/scripts'))
import checks_contract as cc

def req(ok,message):
    if not ok: raise ValueError(message)

def fixture(name):
    return json.loads((Path('tests/fixtures/checks_contract')/name).read_text())

def rejects(fn,document):
    try: fn(document,req)
    except ValueError as error: return str(error)
    raise AssertionError('Expected rejection')

policy=fixture('policy.json')
for pattern in ('**','**/*','**/payments/**','*'):
    candidate=copy.deepcopy(policy)
    candidate['excludes']=[dict(pattern=pattern,reason='audit',origin='manual')]
    print('A-01 original:',pattern,'REJECTED:',rejects(cc.validate_policy,candidate))
for kind in ('test','command'):
    candidate=copy.deepcopy(policy)
    check=dict(id='new-check',title='audit',purpose='audit',kind=kind,argv=['unapproved-tool','--execute'],timeout_seconds=60)
    if kind=='test': check['adapter']='python-unittest'
    candidate['checks'].append(check)
    cc.validate_policy(candidate,req)
    verdict=cc.classify_policy_change(policy,candidate)['verdict']
    assert verdict=='weakening'
    print('A-02:',kind,'novel argv =>',verdict)
candidate=copy.deepcopy(policy)
candidate['checks'].append(dict(id='new-check',title='audit',purpose='audit',kind='command',argv=policy['checks'][1]['argv'],timeout_seconds=60))
assert cc.classify_policy_change(policy,candidate)['verdict']=='extension'
print('A-02: previously approved argv => extension')
result=fixture('result.json'); result['tests']['expected'].append(result['tests']['expected'][0])
print('A-03: duplicate expected ID REJECTED:',rejects(cc.validate_result,result))
candidate=copy.deepcopy(policy)
candidate['test_catalog']['entries'][0]['expected_failure']=dict(allowed=True,reason='audit')
cc.validate_policy(candidate,req)
assert cc.classify_policy_change(policy,candidate)['verdict']=='weakening'
print('A-04: explicit expected_failure accepted; existing test new allowance => weakening')
for pattern in ('**/test_payments.py','tests/test_payments.p?'):
    candidate=copy.deepcopy(policy)
    candidate['excludes']=[dict(pattern=pattern,reason='audit',origin='manual')]
    explicit_input=candidate['checks'][0]['inputs'][1]
    assert explicit_input=='tests/test_payments.py'
    assert fnmatch.fnmatchcase(explicit_input,pattern)
    cc.validate_policy(candidate,req)
    print('A-05: ACCEPTED exclusion',pattern,'matches explicit input',explicit_input)
for rule in ('skip','expected_failure'):
    candidate=copy.deepcopy(policy)
    candidate['test_catalog']['entries'].append(dict(id='tests.New.test_case',description='new test',**{rule:dict(allowed=True,reason='audit')}))
    cc.validate_policy(candidate,req)
    verdict=cc.classify_policy_change(policy,candidate)['verdict']
    assert verdict=='extension'
    print('A-06: new test with',rule,'allowed=True =>',verdict)
