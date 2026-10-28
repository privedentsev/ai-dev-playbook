import json,subprocess,sys,shutil,hashlib
from pathlib import Path
ROOT=Path.cwd(); OUT=ROOT/'work/audit-006-final/rehearsal-final-02'; OUT.mkdir(parents=True,exist_ok=True); dst=OUT/'project'
if dst.exists(): raise RuntimeError('Existing rehearsal must be preserved')
ignore=shutil.ignore_patterns('__pycache__','*.pyc','.DS_Store')
for name in ['ai-dev-playbook','docs','tests']:
 shutil.copytree(ROOT/name,dst/name,ignore=ignore)
for name in ['AGENTS.md','README.md','CHANGELOG.md']:
 shutil.copy2(ROOT/name,dst/name)
# Isolated lifecycle rehearsal: verify executes actual structural check.
config=dst/'ai-dev-playbook/project.json';data=json.loads(config.read_text());data['verification']['checks']=[{'id':'self-check','argv':[sys.executable,'ai-dev-playbook/scripts/playbook.py','check','--ready'],'timeout_seconds':60}];config.write_text(json.dumps(data,ensure_ascii=False,indent=2))
steps=[]
def cli(*args, expected=0):
 p=subprocess.run([sys.executable,'ai-dev-playbook/scripts/playbook.py',*args],cwd=dst,capture_output=True,text=True)
 steps.append({'argv':args,'rc':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
 (OUT/'steps.json').write_text(json.dumps(steps,ensure_ascii=False,indent=2))
 assert p.returncode==expected,(args,p.stdout,p.stderr)
 return p

def hashes(base):return {str(p.relative_to(base)):hashlib.sha256(p.read_bytes()).hexdigest() for p in base.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
cli('index','--all');cli('check','--ready');cli('verify','AUDIT-006');cli('lifecycle','handoff','AUDIT-006','--summary','Synthetic joint archival rehearsal; structural runner only','--actor','Test-Harness','--model','automated-cli-probe')
request={'schema_version':1,'actor':{'name':'Test-Harness','role':'engineer','model':'automated-cli-probe'},'basis':'Simulation only; not engineer acceptance in source project','scope':{'tasks':['AUDIT-006'],'documents':['plan:workflow-and-document-lifecycle']},'actions':[{'action':'archive_document','document':'plan:workflow-and-document-lifecycle','reason':'Rehearsal of block completion','successor':None},{'action':'close_task','task':'AUDIT-006','outcome':'done'}]}
req=dst/'work/request.json';req.write_text(json.dumps(request))
before=hashes(dst/'docs');cli('lifecycle','plan','work/request.json',expected=1);assert before==hashes(dst/'docs')
cli('lifecycle','accept','AUDIT-006','--result','accepted','--role','engineer','--actor','Test-Harness','--model','automated-cli-probe','--note','Simulation only; no real engineer acceptance')
cli('lifecycle','plan','work/request.json','--save')
planfile=next((dst/'work/lifecycle/plans').glob('*.json'));plan=json.loads(planfile.read_text())
(OUT/'combined-plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2))
code_before=hashes(dst/'ai-dev-playbook');tests_before=hashes(dst/'tests');evidence_before=hashes(dst/'docs/tasks/AUDIT-006-evidence');history_before=hashes(dst/'docs/plans/workflow-and-document-lifecycle-evidence')
cli('lifecycle','apply',str(planfile.relative_to(dst)));cli('check','--ready')
assert not (dst/'docs/tasks/AUDIT-006.md').exists()
assert (dst/'docs/archive/tasks/AUDIT-006.md').is_file()
assert (dst/'docs/archive/plans/workflow-and-document-lifecycle.md').is_file()
for folder in ['specs','designs']:
 assert (dst/'docs'/folder/'workflow-and-document-lifecycle.md').is_file()
assert code_before==hashes(dst/'ai-dev-playbook');assert tests_before==hashes(dst/'tests')
after_e=hashes(dst/'docs/archive/tasks/AUDIT-006-evidence')
assert all(after_e.get(k)==v for k,v in evidence_before.items())
after_history=hashes(dst/'docs/archive/plans/workflow-and-document-lifecycle-evidence')
assert all(after_history.get(k)==v for k,v in history_before.items())
cli('lifecycle','apply',str(planfile.relative_to(dst)))
result={'result':'PASS','synthetic_acceptance':True,'verification_scope':'rehearsal: actual check --ready; source full suite separately','audit_evidence_preserved':len(evidence_before),'plan_history_preserved':len(history_before),'code_tests_unchanged':True,'active_spec_design_preserved':True,'refusal_without_acceptance_unchanged':True,'steps':len(steps)}
(OUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result))
