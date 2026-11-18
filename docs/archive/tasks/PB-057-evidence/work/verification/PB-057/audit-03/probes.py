"""Independent PB-057 re-audit. Run from repository root."""
import copy
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'ai-dev-playbook/scripts'))
import checks_contract as cc

def req(ok,msg):
    if not ok: raise ValueError(msg)

def fixture(name):
    return json.loads((Path('tests/fixtures/checks_contract')/name).read_text())

def rejects(fn,document):
    try: fn(document,req)
    except ValueError as e: return str(e)
    raise AssertionError('Expected rejection')

policy=fixture('policy.json')
for typed in (False,True):
    for pattern in ('**/test_payments.py','tests/test_payments.p?','tests/test_payments.py','*/test_payments.py','**/test_payments.[p]y'):
        p=copy.deepcopy(policy)
        if typed: p['checks'][0]['inputs'][1]={'type':'file','path':'tests/test_payments.py'}
        p['excludes']=[dict(pattern=pattern,reason='audit',origin='manual')]
        assert 'конфликтует' in rejects(cc.validate_policy,p)
        print('A-05 PASS rejection:',pattern,'typed_input=',typed)
for pattern in ('tests/*.log','src/payments/*.py','**/fixtures.bin'):
    p=copy.deepcopy(policy);p['excludes']=[dict(pattern=pattern,reason='audit',origin='manual')]
    cc.validate_policy(p,req); print('A-05 PASS permitted selective exclusion:',pattern)
for rule in ('skip','expected_failure'):
    for allowed in (True,False,None):
        p=copy.deepcopy(policy);entry=dict(id='tests.New.test_case',description='audit')
        if allowed is not None:entry[rule]=dict(allowed=allowed,reason='audit')
        p['test_catalog']['entries'].append(entry)
        cc.validate_policy(p,req)
        expected='weakening' if allowed else 'extension'
        actual=cc.classify_policy_change(policy,p)['verdict']
        assert actual==expected,(rule,allowed,actual)
        print('A-06 PASS new entry:',rule,allowed,actual)
p=copy.deepcopy(policy);p['test_catalog']['entries'].append(dict(id='tests.New.test_case',description='audit',skip=dict(allowed=True,reason='audit'),expected_failure=dict(allowed=True,reason='audit')))
p['checks'].append(dict(id='new-manual',title='audit',purpose='audit',kind='manual',scenario='audit'))
cc.validate_policy(p,req);assert cc.classify_policy_change(policy,p)['verdict']=='weakening'
print('A-06 PASS mixed extension and two allowances => weakening')
for pattern in ('**','**/*','**/payments/**'):
    p=copy.deepcopy(policy);p['excludes']=[dict(pattern=pattern,reason='audit',origin='manual')]
    assert 'конфликтует' in rejects(cc.validate_policy,p)
print('Regression A-01 PASS')
p=copy.deepcopy(policy);p['checks'].append(dict(id='new-command',title='audit',purpose='audit',kind='command',argv=['unapproved-tool'],timeout_seconds=60))
cc.validate_policy(p,req);assert cc.classify_policy_change(policy,p)['verdict']=='weakening'
print('Regression A-02 PASS')
r=fixture('result.json');r['tests']['expected'].append(r['tests']['expected'][0]);assert 'повторный' in rejects(cc.validate_result,r)
print('Regression A-03 PASS')
p=copy.deepcopy(policy);p['test_catalog']['entries'][0]['expected_failure']=dict(allowed=True,reason='audit');cc.validate_policy(p,req);assert cc.classify_policy_change(policy,p)['verdict']=='weakening'
print('Regression A-04 PASS')
print('Independent probes: ALL PASSED')
