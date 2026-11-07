import copy, json, sys, tempfile, shutil
from pathlib import Path
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'ai-dev-playbook/scripts'))
import playbook, documents, verification
F = ROOT / 'tests/fixtures/task_workflow_v2/valid'
meta = json.loads((F / 'task_metadata_v2.json').read_text())
report = json.loads((F / 'report_v2.json').read_text())
tw = json.loads((F / 'project.json').read_text())['task_workflow']
def task_case(name, value):
    errors=[]
    documents._validate_task_meta(value, name, errors, registry={'unit-tests','visual-check'})
    print(name, json.dumps(errors, ensure_ascii=False))
m=copy.deepcopy(meta); del m['verification_plan']; task_case('missing_plan',m)
m=copy.deepcopy(meta); del m['verification_plan']['checks'][0]['inputs']; task_case('missing_inputs',m)
m=copy.deepcopy(meta); m['verification_plan']['checks'][0]['inputs']=[{'type':'file'}]; task_case('file_without_path',m)
m=copy.deepcopy(meta); m['verification_plan']['checks'][0]['inputs']=[{'type':'environment','names':123}]; task_case('invalid_environment_names',m)
r=copy.deepcopy(report)
for field in ['at','started_at','finished_at']: r.pop(field,None)
r['results']=[{'check_id':'x','definition_hash':'hash','kind':'command','status':'passed','exit_code':0}]
print('report_without_provenance', bool(verification.validate_report(r,playbook.require)))
t=copy.deepcopy(tw); t['registry'][0]['default_inputs']=[{'type':'file','unexpected':True}]
print('registry_unknown_input_field', bool(verification.validate_task_workflow(ROOT,t,playbook.safe_path,playbook.require)))
# Run the production catalog path, not just the low-level validator.
with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp)/'project'
    shutil.copytree(ROOT,root,ignore=lambda directory,names: [n for n in names if n in {'.git','__pycache__'} or (Path(directory)==ROOT and n=='work')])
    (root/'audit-link').symlink_to('/tmp',target_is_directory=True)
    data, paths=playbook.config(root)
    card=root/'docs/tasks/PB-031.md'
    original=card.read_text()
    for name, target in [('symlink_scope','audit-link'),('unknown_registry_without_opt_in','tests')]:
        m=copy.deepcopy(meta); m['verification_plan']['scope']=[target]
        m['verification_plan']['checks'][0]['inputs']=[target]
        if name.startswith('unknown'): m['verification_plan']['checks'][0]['registry_id']='DOES-NOT-EXIST'
        start=original.index('<!-- playbook-meta:start -->'); end=original.index('<!-- playbook-meta:end -->')+len('<!-- playbook-meta:end -->')
        card.write_text(original[:start]+'<!-- playbook-meta:start -->\n```json\n'+json.dumps(m)+'\n```\n<!-- playbook-meta:end -->'+original[end:])
        records=playbook.read_tasks(root,paths)
        catalog=documents.load_catalog(root,data,paths,records,playbook.safe_path,playbook.require)
        print(name, 'catalog_accepted=True')
# Inspect original evidence freshness before adding audit/card edits.
r=verification.load_report(ROOT/'docs/tasks/PB-031-evidence/verify-run/report.json',playbook.require)
fresh,diff=verification.inputs_fresh(ROOT,r,{'docs/tasks/PB-031.md'},playbook.safe_path,playbook.require)
print('executor_report', 'inputs_unchanged=',r.get('inputs_unchanged'),'fresh=',fresh,'diff=',json.dumps(diff,ensure_ascii=False))
