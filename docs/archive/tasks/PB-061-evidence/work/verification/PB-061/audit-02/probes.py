import sys, json, unittest, os, tempfile
from pathlib import Path
sys.path[:0] = [str(Path.cwd()/'tests'),str(Path.cwd()/'ai-dev-playbook/scripts')]
from test_checks_trust import init_home,trust_policy,verifier_run
from test_checks_run import execute
import checks_trust,checks_verifier

c=unittest.TestCase()
try:
    policy=trust_policy(); policy['checks']=[policy['checks'][0]]
    proj,home=init_home(c,policy)
    proj.write('tests/test_payments.py', '''import sys,json,os
report={'protocol':1,'expected':['test_payments.TestPay.test_ok'],
 'actual':[{'id':'test_payments.TestPay.test_ok','outcome':'passed'}],'exit_code':0}
sys.stdout.write('CHECKS-REPORT-V1 '+json.dumps(report)+'\\n')
sys.stdout.flush()
sys.stdin.buffer.read(1)
os._exit(0)
''')
    r=verifier_run(proj,home)
    g=checks_verifier.gate(proj.root,home,emit=lambda _:None)
    print('ACK_FORGERY',json.dumps({'run_ok':r['outcome']['ok'],
        'verify_ok':checks_verifier.verify(home,r['receipt'])['ok'],
        'gate_ok':g['ok'],'defined_tests':0,'executed_tests':0}))

    home.chmod(0o555)
    try:
        p=checks_trust.probe(proj.root,home)
        key=next((home/'keys').glob('*.key'))
        with key.open('a'): pass
        print('CHMOD_PROBE',json.dumps({'capability':p['capability'],
            'separate_identity':p['separate_identity'],'key_writable':True}))
    finally:home.chmod(0o755)
    store=checks_trust.load_home(home)
    print('DIRECTORY_WRITABILITY',json.dumps(checks_trust._home_writability(home,store)))

    cp=trust_policy(); cp['checks']=[{'id':'env','title':'env','purpose':'audit',
        'kind':'command','argv':[sys.executable,'-c',
         'import os,json; print(json.dumps(dict(os.environ),sort_keys=True))'],
        'timeout_seconds':10,'applicability':'always','required_environment':['api']}]
    cp['environment']['external']=[{'id':'api','freshness':{'kind':'always_run'}}]
    proj2,home2=init_home(c,cp)
    r2=verifier_run(proj2,home2)
    actual=json.loads(next(home2.glob('arena/*/work/checks/runs/*/env.stdout.log')).read_text())
    print('PROTECTED_ENV',json.dumps({'ok':r2['outcome']['ok'],
        'HOME_in_arena':str(home2/'arena') in actual.get('HOME',''),
        'TMPDIR_in_arena':str(home2/'arena') in actual.get('TMPDIR',''),
        'PATH_present':bool(actual.get('PATH'))}))
    marker='PB061_AUDIT02_ENV';os.environ[marker]='inherited'
    try:
        ordinary,log=execute(proj2,cp,scope='project')
        actual_local=json.loads(next(proj2.root.glob('work/checks/runs/*/env.stdout.log')).read_text())
        print('ORDINARY_ENV',json.dumps({'ok':ordinary['ok'],'inherited':actual_local.get(marker)}))
    finally:os.environ.pop(marker,None)

    lp=trust_policy();lp['checks']=[lp['checks'][-1]]
    proj3,home3=init_home(c,lp)
    source=verifier_run(proj3,home3)['receipt']
    agg1=checks_verifier.aggregate(home3,proj3.root,receipt_ids=[source['receipt_id']],emit=lambda _:None)['receipt']
    agg2=checks_verifier.aggregate(home3,proj3.root,receipt_ids=[agg1['receipt_id']],emit=lambda _:None)['receipt']
    checks_trust.revoke(checks_trust.load_home(home3),receipt_id=source['receipt_id'],actor='audit')
    for receipt in (source,agg1):
        (proj3.root/checks_trust.PROJECT_RECEIPTS_DIR/f"{receipt['receipt_id']}.json").unlink()
    print('TRANSITIVE_REVOCATION',json.dumps({'source_ok':checks_verifier.verify(home3,source)['ok'],
        'aggregate1_ok':checks_verifier.verify(home3,agg1)['ok'],
        'aggregate2_ok':checks_verifier.verify(home3,agg2)['ok'],
        'gate_ok':checks_verifier.gate(proj3.root,home3,emit=lambda _:None)['ok']}))
finally:c.doCleanups()
