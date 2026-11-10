import sys, json, subprocess, hashlib
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
from test_close_lifecycle import CloseLifecycleCase, TASK_META
from test_task_cards import BODY, record
c=CloseLifecycleCase(); c.setUp()
def cli(*args, ok=0):
    p=subprocess.run([sys.executable,str(c.root/'ai-dev-playbook/scripts/playbook.py'),'--root',str(c.root),*args],capture_output=True,text=True)
    assert p.returncode==ok,(args,p.stdout,p.stderr)
    return p.stdout
def snapshot():
    return {str(p.relative_to(c.root)):(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns) for p in c.root.rglob('*') if p.is_file()}
try:
    c.managed_task(status='in_review',meta={**TASK_META,'card_format_version':2},body=BODY+record('EXEC')+record('EXEC',2))
    path=c.tasks_dir()/'DEV-1.md'
    cfg=c.root/'ai-dev-playbook/project.json'; conf=json.loads(cfg.read_text())
    conf['verification']={'inputs':['docs','source.txt','ai-dev-playbook/project.json'],'checks':[{'id':'once','argv':[sys.executable,'-c',"from pathlib import Path; p=Path('work/count'); p.parent.mkdir(exist_ok=True); p.write_text(p.read_text()+'x' if p.exists() else 'x')"],'timeout_seconds':30}]}
    cfg.write_text(json.dumps(conf)); (c.root/'source.txt').write_text('v1')
    cli('index'); cli('verify','DEV-1')
    path.write_text(path.read_text()+record('CHECK')+record('CHECK',2)+record('HANDOFF',**{'Выполнение':'EXEC-02, EXEC-01','Проверки':'CHECK-02, CHECK-01'}))
    cli('lifecycle','handoff','DEV-1','--summary','independent probe','--actor','Devin','--model','SWE-2 High')
    before=snapshot(); view=json.loads(cli('card','show','DEV-1','--handoff','--json'))
    assert snapshot()==before
    assert [e['id'] for e in view['entries']]==['EXEC-01','EXEC-02','CHECK-01','CHECK-02','HANDOFF-01']
    assert view['context']['handoff_state']=='current'
    for e in view['entries']:
        assert e==json.loads(cli('card','show','DEV-1','--entry',e['id'],'--json'))
    assert snapshot()==before
    assert (c.root/'work/count').read_text()=='x'
    cli('close','DEV-1','--dry-run',ok=1)
    (c.root/'source.txt').write_text('v2')
    assert json.loads(cli('card','show','DEV-1','--handoff','--json'))['context']['handoff_state']=='not_current'
    cli('lifecycle','handoff','DEV-1','--summary','stale','--actor','Devin','--model','SWE-2 High')
    assert json.loads(cli('card','show','DEV-1','--handoff','--json'))['context']['handoff_state']=='not_current'
    path.write_text(path.read_text()+record('AUDIT',actor='Auditor'))
    cli('lifecycle','accept','DEV-1','--result','accepted','--actor','Auditor','--model','SWE-2 High')
    stale=json.loads(cli('card','show','DEV-1','--handoff','--json'))
    assert stale['context']['acceptance_state']=='not_current'
    assert stale['context']['handoff_state']=='not_current'
    cli('close','DEV-1','--dry-run',ok=1)
    print('PASS: subprocess CLI; multi-EXEC/CHECK chronological selection; exact entry equality; hash+mtime read-only; one verify; no premature close; stale source rejection')
finally:
    c.tearDown()
