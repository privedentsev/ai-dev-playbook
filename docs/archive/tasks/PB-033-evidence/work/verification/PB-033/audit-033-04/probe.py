import sys, json, copy
from pathlib import Path
sys.path[:0] = ['/Users/privedentsev1/Dev/ai-dev-playbook/tests','/Users/privedentsev1/Dev/ai-dev-playbook/ai-dev-playbook/scripts']
from test_verification_v2 import VerificationV2Test

def setup_manual(t):
    t.add_registry_check({'id':'visual','kind':'manual','purpose':'visual','scenario':'Inspect button color','applicability':{'kind':'always'}})
    checks=[{'id':'unit-tests','kind':'command','purpose':'unit','registry_id':'unit-tests','inputs':['target.txt'],'applicability':{'kind':'always'}}, {'id':'visual','kind':'manual','purpose':'visual','registry_id':'visual','inputs':['target.txt'],'applicability':{'kind':'always'}}]
    (t.root/'target.txt').write_text('original')
    t.create_v2_task(checks=checks,scope=['target.txt'])
    code,out,err=t.cli('verify','DEV-1')
    assert code==1, (out,err)
    rep=json.loads(next((t.root/'work/verification/DEV-1').glob('*/report.json')).read_text())
    cand=next(r for r in rep['results'] if r['check_id']=='visual')['candidate_snapshot']
    return {'check_id':'visual','status':'passed','observation':'Inspected original candidate','environment':{'browser':'Chrome'},'engineer_confirmation':{'who':'Engineer'},'candidate_snapshot':cand}

def manual(mode):
    t=VerificationV2Test(); t.setUp()
    try:
        statement=setup_manual(t)
        if mode=='missing':
            (t.root/'target.txt').write_text('changed')
            del statement['candidate_snapshot']
        elif mode=='stale':
            (t.root/'target.txt').write_text('changed')
        elif mode=='scenario':
            next(r for r in t.settings['task_workflow']['registry'] if r['id']=='visual')['scenario']='Inspect keyboard navigation and focus instead'
            t.save_project()
        (t.root/'statement.json').write_text(json.dumps(statement))
        result=t.cli('verify','DEV-1','--record-manual','statement.json')
        print(mode, result, flush=True)
        if mode=='scenario':
            print('verify after scenario change',t.cli('verify','DEV-1'),flush=True)
        if mode in {'missing','stale'}:
            assert result[0]==1, result
        if mode=='valid':
            assert result[0]==0, result
    finally: t.doCleanups()

def presentation_change(mode):
    t=VerificationV2Test(); t.setUp()
    try:
        statement=setup_manual(t)
        directory=t.root/'work/verification/DEV-1'
        old=copy.deepcopy(statement['candidate_snapshot'])
        if mode=='scenario':
            t.settings['task_workflow']['registry'][-1]['scenario']='Inspect keyboard navigation'
        elif mode=='definition':
            t.settings['task_workflow']['registry'][-1]['purpose']='Updated purpose'
        elif mode=='policy':
            t.settings['task_workflow']['checkpoints']['other']=['unit-tests']
        t.save_project()
        def submit(candidate):
            statement['candidate_snapshot']=candidate
            (t.root/'statement.json').write_text(json.dumps(statement))
            return t.cli('verify','DEV-1','--record-manual','statement.json')
        result=submit(old)
        print(mode+' old before presentation',result,flush=True)
        assert result[0]==1,result
        result=t.cli('verify','DEV-1')
        assert result[0]==1,result
        result=submit(old)
        print(mode+' old after presentation',result,flush=True)
        assert result[0]==1,result
        candidates=[r['candidate_snapshot'] for p in directory.glob('*/report.json') for r in json.loads(p.read_text())['results'] if r['check_id']=='visual' and r['candidate_snapshot']!=old]
        result=submit(candidates[-1])
        print(mode+' new candidate',result,flush=True)
        assert result[0]==0,result
        result=t.cli('verify','DEV-1')
        print(mode+' new reuse',result,flush=True)
        assert result[0]==0 and 'REUSE: visual' in result[1],result
    finally: t.doCleanups()

def invalid_history(mode):
    t=VerificationV2Test(); t.setUp()
    try:
        statement=setup_manual(t)
        for p in (t.root/'work/verification/DEV-1').glob('*/report.json'):
            report=json.loads(p.read_text())
            if mode=='foreign-task': report['task_id']='OTHER-1'
            elif mode=='invalid-report': report.pop('coverage')
            elif mode=='legacy-candidate':
                for r in report['results']:
                    if r['check_id']=='visual': r['candidate_snapshot'].pop('sha256',None)
                statement['candidate_snapshot'].pop('sha256',None)
            p.write_text(json.dumps(report))
        (t.root/'statement.json').write_text(json.dumps(statement))
        result=t.cli('verify','DEV-1','--record-manual','statement.json')
        print(mode,result,flush=True)
        assert result[0]==1,result
    finally: t.doCleanups()


def late(reused):
    t=VerificationV2Test(); t.setUp()
    try:
        (t.root/'target.txt').write_text('original')
        (t.root/'independent.txt').write_text('v1')
        (t.root/'mutator.py').write_text('pass\n' if reused else "from pathlib import Path\nPath('target.txt').write_text('changed')\n")
        t.add_registry_check({'id':'mutator','kind':'command','purpose':'mutate','argv':[sys.executable,'mutator.py'],'timeout_seconds':30,'applicability':{'kind':'always'}})
        checks=[{'id':'unit-tests','kind':'command','purpose':'unit','registry_id':'unit-tests','inputs':['target.txt'],'applicability':{'kind':'always'}}, {'id':'mutator','kind':'command','purpose':'mutate','registry_id':'mutator','inputs':['independent.txt','mutator.py'],'applicability':{'kind':'always'}}]
        t.create_v2_task(checks=checks,scope=['target.txt','independent.txt','mutator.py'])
        if reused:
            result=t.cli('verify','DEV-1'); assert result[0]==0,result
            (t.root/'mutator.py').write_text("from pathlib import Path\nPath('target.txt').write_text('changed')\n")
        result=t.cli('verify','DEV-1')
        print('late reused='+str(reused),result,flush=True)
        assert result[0]==1 and 'stale:' in result[1],result
        if reused: assert 'REUSE: unit-tests' in result[1],result
    finally: t.doCleanups()


for mode in ['missing','stale','valid']: manual(mode)
for mode in ['scenario','definition','policy']: presentation_change(mode)
for mode in ['foreign-task','invalid-report','legacy-candidate']: invalid_history(mode)
late(False)
late(True)
