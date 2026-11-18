import sys, json, unittest, os
from pathlib import Path
sys.path[:0] = [str(Path.cwd()/'tests'), str(Path.cwd()/'ai-dev-playbook/scripts')]
from test_checks_trust import init_home, trust_policy, verifier_run
import checks_trust, checks_verifier

case = unittest.TestCase()
try:
    policy = trust_policy()
    policy['checks'] = [{'id':'env', 'title':'env', 'purpose':'audit', 'kind':'command',
        'argv':[sys.executable,'-c',"import os,json; print(json.dumps(dict(os.environ),sort_keys=True))"],
        'timeout_seconds':10, 'applicability':'always'}]
    proj, home = init_home(case, policy)
    result = verifier_run(proj, home)
    stdout = next(home.glob('arena/*/work/checks/runs/*/env.stdout.log'))
    actual = json.loads(stdout.read_text())
    print('ENV', json.dumps({'passed':result['outcome']['ok'], 'HOME':actual.get('HOME'),
        'TMPDIR':actual.get('TMPDIR'), 'PATH':actual.get('PATH'), 'keys':sorted(actual)}))

    # Directory is read-only, but all sensitive files remain owned and writable by agent.
    home.chmod(0o555)
    try:
        probe = checks_trust.probe(proj.root, home)
        key = next((home/'keys').glob('*.key'))
        with key.open('a') as out:
            out.write('')
        gate = checks_verifier.gate(proj.root, home, emit=lambda s:None)
        print('PROBE', json.dumps({'capability':probe['capability'],
            'agent_can_write':probe['agent_can_write'], 'key_writable':True,
            'protected_confirmed':gate['protected_confirmed']}))
    finally:
        home.chmod(0o755)

    policy2 = trust_policy()
    policy2['checks'] = [policy2['checks'][-1]]  # lint
    proj2, home2 = init_home(case, policy2)
    original = verifier_run(proj2, home2)['receipt']
    agg = checks_verifier.aggregate(home2, proj2.root,
        receipt_ids=[original['receipt_id']], emit=lambda s:None)['receipt']
    checks_trust.revoke(checks_trust.load_home(home2),
        receipt_id=original['receipt_id'], actor='audit')
    (proj2.root/checks_trust.PROJECT_RECEIPTS_DIR/f"{original['receipt_id']}.json").unlink()
    gate2 = checks_verifier.gate(proj2.root,home2,emit=lambda s:None)
    print('REVOCATION', json.dumps({'original_ok':checks_verifier.verify(home2,original)['ok'],
        'aggregate_ok':checks_verifier.verify(home2,agg)['ok'], 'gate_ok':gate2['ok']}))

    # Add always_run requirement to one unittest check, which has a valid adapter.
    policy3 = trust_policy()
    policy3['checks'] = [policy3['checks'][0]]
    policy3['checks'][0]['required_environment'] = ['api']
    policy3['environment']['external'] = [{'id':'api','freshness':{'kind':'always_run'}}]
    proj3, home3 = init_home(case, policy3)
    try:
        result3 = verifier_run(proj3, home3)
        print('EXTERNAL',json.dumps(result3['outcome']['summary']))
    except Exception as error:
        print('EXTERNAL_EXCEPTION',type(error).__name__,str(error))

    policy4 = trust_policy()
    policy4['checks'] = [policy4['checks'][0]]
    proj4, home4 = init_home(case, policy4)
    # Candidate module forges the adapter report during discovery; no test runs.
    proj4.write('tests/test_payments.py', '''import sys, json, os
from pathlib import Path
target = Path(sys.argv[sys.argv.index('--out')+1])
target.write_text(json.dumps({'protocol':1,
 'expected':['test_payments.TestPay.test_ok'],
 'actual':[{'id':'test_payments.TestPay.test_ok','outcome':'passed'}],
 'exit_code':0}))
os._exit(0)
''')
    result4 = verifier_run(proj4, home4)
    gate4 = checks_verifier.gate(proj4.root,home4,emit=lambda s:None)
    print('FORGED_ADAPTER', json.dumps({'run_ok':result4['outcome']['ok'],
        'signature_ok':checks_verifier.verify(home4,result4['receipt'])['ok'],
        'gate_ok':gate4['ok'], 'tests_defined':False, 'test_events_fabricated':True}))
finally:
    case.doCleanups()
