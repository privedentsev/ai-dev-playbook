import sys,os,json,unittest,tempfile,errno
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path.cwd()/'tests'),str(Path.cwd()/'ai-dev-playbook/scripts')]
from test_checks_trust import trust_policy,command_policy,init_home,verifier_run
from test_checks_run import execute
import checks_trust as t,checks_verifier as v,checks_results
c=unittest.TestCase()
try:
    p,h=init_home(c,trust_policy())
    p.write('tests/test_payments.py', '''import sys,json,os
from pathlib import Path
Path('EXECUTED').write_text('yes')
print('CHECKS-REPORT-V1 '+json.dumps({'protocol':1,
 'expected':['test_payments.TestPay.test_ok'],
 'actual':[{'id':'test_payments.TestPay.test_ok','outcome':'passed'}],'exit_code':0}),flush=True)
sys.stdin.buffer.read(1)
os._exit(0)
''')
    try:
        verifier_run(p,h)
        refused=False
    except ValueError as error:
        refused='self-reported' in str(error)
    print('ACK_BLOCK',json.dumps({'refused':refused,
        'receipts':len(list((h/t.RECEIPTS_DIR).glob('*.json'))),
        'gate_ok':v.gate(p.root,h,emit=lambda _:None)['ok'],
        'arenas':len(list((h/t.ARENA_DIR).iterdir()))}))

    p2,h2=init_home(c,command_policy([sys.executable,'-c','pass']))
    r=verifier_run(p2,h2)['receipt']
    agg=v.aggregate(h2,p2.root,receipt_ids=[r['receipt_id']],emit=lambda _:None)['receipt']
    print('COMMAND_SMOKE',json.dumps({'verify_ok':v.verify(h2,r)['ok'],
        'aggregate_ok':v.verify(h2,agg)['ok'],'gate_ok':v.gate(p2.root,h2,emit=lambda _:None)['ok']}))
    store=t.load_home(h2)
    old=v._load_result(store,r['subject']['results'][0]['result_digest'])
    old['kind']='test';old.pop('command',None)
    old['tests']={'expected':['fake.test'],'actual':[{'id':'fake.test','outcome':'passed'}]}
    old['result_id']='historical-probe'
    v._index_results(store,'legacy-probe',[old])
    legacy=v._issue_receipt(store,{'kind':'run','results':v._result_subject([old])},
        candidate_digest=r['candidate_digest'],now=None)
    verdict=v.verify(h2,legacy)
    try:
        v.aggregate(h2,p2.root,receipt_ids=[legacy['receipt_id']],emit=lambda _:None)
        aggregate_refused=False
    except ValueError:aggregate_refused=True
    print('LEGACY_TEST_RECEIPT',json.dumps({'verify_ok':verdict['ok'],
        'self_reported_reason':any('self-reported' in s for s in verdict['reasons']),
        'aggregate_refused':aggregate_refused}))

    with tempfile.TemporaryDirectory() as td:
        d=Path(td);neighbor=d/'.probe-neighbor';neighbor.write_text('preserve')
        writable=t._writable(d,directory=True)
        d.chmod(0o555)
        try:denied=t._writable(d,directory=True)
        finally:d.chmod(0o755)
        with patch.object(os,'open',side_effect=OSError(errno.EIO,'audit')):
            unknown=t._writable(d,directory=True)
        print('MARKER',json.dumps({'writable':writable,'denied':denied,
            'unknown':unknown,'preserved_neighbor':neighbor.read_text()=='preserve',
            'no_own_residue':sorted(x.name for x in d.iterdir())==['.probe-neighbor']}))
    report=t.probe(p2.root,h2)
    print('PROBE',json.dumps({'capability':report['capability'],
        'writable_points':report['writable_points'],'agent_can_write':report['agent_can_write']}))
finally:c.doCleanups()
