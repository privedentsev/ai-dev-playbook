import sys, json
sys.path[:0] = ['/Users/privedentsev1/Dev/ai-dev-playbook/tests', '/Users/privedentsev1/Dev/ai-dev-playbook/ai-dev-playbook/scripts']
from test_verification_v2 import VerificationV2Test

def run(mode):
    t = VerificationV2Test()
    t.setUp()
    try:
        if mode == 'manual':
            t.add_registry_check({'id':'visual','kind':'manual','purpose':'visual','scenario':'Inspect current file','applicability':{'kind':'always'}})
            checks=[{'id':'unit-tests','kind':'command','purpose':'unit','registry_id':'unit-tests','inputs':['target.txt'],'applicability':{'kind':'always'}}, {'id':'visual','kind':'manual','purpose':'visual','registry_id':'visual','inputs':['target.txt'],'applicability':{'kind':'always'}}]
            (t.root/'target.txt').write_text('original')
            t.create_v2_task(checks=checks, scope=['target.txt'])
            print('candidate run',t.cli('verify','DEV-1'))
            (t.root/'target.txt').write_text('CHANGED after presentation')
            (t.root/'statement.json').write_text(json.dumps({'check_id':'visual','status':'passed','observation':'Inspected old candidate','environment':{'browser':'Chrome'},'engineer_confirmation':{'who':'Engineer'}}))
            result=t.cli('verify','DEV-1','--record-manual','statement.json')
            print('MISSING CANDIDATE AFTER EDIT',result)
            assert result[0]==0, result
        else:
            (t.root/'target.txt').write_text('original')
            t.add_registry_check({'id':'mutator','kind':'command','purpose':'mutate earlier input','argv':[sys.executable,'-c',"from pathlib import Path; Path('target.txt').write_text('changed')"],'timeout_seconds':30,'applicability':{'kind':'always'}})
            checks=[{'id':'unit-tests','kind':'command','purpose':'check target','registry_id':'unit-tests','inputs':['target.txt'],'applicability':{'kind':'always'}}, {'id':'mutator','kind':'command','purpose':'mutate','registry_id':'mutator','inputs':['independent.txt'],'applicability':{'kind':'always'}}]
            (t.root/'independent.txt').write_text('independent')
            t.create_v2_task(checks=checks,scope=['target.txt','independent.txt'])
            result=t.cli('verify','DEV-1')
            print('LATER COMMAND MUTATES EARLIER INPUT',result)
            assert result[0]==0, result
            assert (t.root/'target.txt').read_text()=='changed'
    finally:
        t.doCleanups()

run('manual')
run('cross-check')
