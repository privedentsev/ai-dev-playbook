"""Normal timestamp bytecode cache versus equal-size/equal-mtime source edit."""
import sys,os,json,unittest,hashlib
from pathlib import Path
ROOT=Path(sys.argv[1]).resolve();sys.path[:0]=[str(ROOT/'tests'),str(ROOT/'ai-dev-playbook/scripts')]
from test_checks_run import run_policy,run_project
import checks_runner,checks_gate
case=unittest.TestCase()
try:
 proj=run_project(case);counter=proj.root.parent/'observed.json'
 body=f'''import unittest,json
from pathlib import Path
from src.payments.pay import value
class TestPay(unittest.TestCase):
    def test_ok(self):
        p=Path({str(counter)!r})
        old=json.loads(p.read_text()) if p.exists() else []
        p.write_text(json.dumps(old+[value]))
        self.assertEqual(value, 1)
'''
 proj.write('tests/test_payments.py',body);source=proj.write('src/payments/pay.py','value = 1\n');before=source.stat();old_hash=hashlib.sha256(source.read_bytes()).hexdigest()
 policy=run_policy();policy['checks']=[policy['checks'][0]]
 env=dict(os.environ);env.pop('PYTHONDONTWRITEBYTECODE',None);env.pop('PYTHONPYCACHEPREFIX',None)
 content=(proj.root/'docs/tasks/DEV-1.md').read_text()
 def run(mode):return checks_runner.execute(proj.root,policy,task_id='DEV-1',scope='project',mode=mode,checkpoint=None,task_content=content,task_meta={},env=env,emit=lambda s:None)
 first=run('force');caches=list((source.parent/'__pycache__').glob('pay*.pyc'))
 source.write_text('value = 2\n');os.utime(source,ns=(before.st_atime_ns,before.st_mtime_ns));changed=source.stat();new_hash=hashlib.sha256(source.read_bytes()).hexdigest()
 second=run('stale');after_second=checks_gate.evaluate(proj.root,policy,task_id='DEV-1',scope='project',task_content=content,task_meta={})
 observed_before_control=json.loads(counter.read_text())
 for cache in caches:cache.unlink()
 third=run('force');after_control=checks_gate.evaluate(proj.root,policy,task_id='DEV-1',scope='project',task_content=content,task_meta={})
 def brief(r):return {'ok':r['ok'],'summary':r['summary'],'results':r['results']}
 print(json.dumps({'fixture':'Unmodified adapter and real subprocesses, no mock or pyc editing. First real run generates the normal cache. Source then changes at equal size/mtime; control removes only this temporary-project cache. Normal Python cache-writing env for all three executions.','cache_files':[str(p.relative_to(proj.root)) for p in caches],'same_size':before.st_size==changed.st_size,'same_mtime_ns':before.st_mtime_ns==changed.st_mtime_ns,'old_sha256':old_hash,'new_sha256':new_hash,'source_after_edit':source.read_text(),'first':brief(first),'second':brief(second),'gate_after_second':after_second,'observed_before_control':observed_before_control,'control':brief(third),'gate_after_control':after_control,'observed_all':json.loads(counter.read_text())},ensure_ascii=False,indent=2))
finally:case.doCleanups()
