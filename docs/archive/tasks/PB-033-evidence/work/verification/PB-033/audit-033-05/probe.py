import importlib.util, pathlib, os, json
repo=pathlib.Path('/Users/privedentsev1/Dev/ai-dev-playbook')
spec=importlib.util.spec_from_file_location('probe',repo/'docs/tasks/AUDIT-007-evidence/probe.py'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
out=pathlib.Path('/tmp/pb033-reaudit-1106'); out.mkdir(exist_ok=True)
p=m.Project(out,'policy')
try:
 p.cli('verify','DEV-1',expect=0); p.cli('verify','DEV-1',expect=0); assert p.counts()=={'a':1,'b':1}
 p.settings['task_workflow']['registry'][0]['argv'][-1]+='; print("revision")'; p.save_settings(); p.cli('verify','DEV-1',expect=0); assert p.counts()=={'a':2,'b':1}
 p.handoff(); p.accept(); p.cli('close','DEV-1','--dry-run',expect=0)
finally: p.finish()
p=m.Project(out,'environment',('command',))
try:
 p.settings['task_workflow']['registry'][0]['argv'][-1]+='; import os; raise SystemExit(0 if os.getenv("AUDIT007_MODE")=="pass" else 9)'; p.save_settings()
 os.environ['AUDIT007_MODE']='pass'; p.cli('verify','DEV-1',expect=0); p.handoff(); p.accept()
 os.environ['AUDIT007_MODE']='fail'; p.accept(expect=1); p.cli('close','DEV-1','--dry-run',expect=1); p.handoff(expect=1); p.cli('verify','DEV-1',expect=1); assert p.latest()['results'][0]['exit_code']==9; assert p.counts()=={'a':2}
finally: os.environ.pop('AUDIT007_MODE',None); p.finish()
p=m.Project(out,'terminal',('manual',))
try:
 p.settings['task_workflow']['registry'][0]['required_environment']=['terminal']; p.save_settings(); p.cli('verify','DEV-1',expect=1); candidate=p.latest()['results'][0]['candidate_snapshot']; p.manual(candidate,expect=0); p.cli('verify','DEV-1',expect=0); p.handoff(); p.accept(); p.cli('close','DEV-1','--dry-run',expect=0)
finally:p.finish()
print('PASS: independent policy counts, environment accept/close/handoff rejection and exit9, terminal manual lifecycle')
