import copy,json,sys,tempfile,shutil,subprocess
from pathlib import Path
ROOT=Path.cwd(); sys.path.insert(0,str(ROOT/'ai-dev-playbook/scripts'))
import playbook,documents,verification,lifecycle
F=ROOT/'tests/fixtures/task_workflow_v2/valid'
meta=json.loads((F/'task_metadata_v2.json').read_text()); tw=json.loads((F/'project.json').read_text())['task_workflow']; report=json.loads((F/'report_v2.json').read_text())
reg={x['id']:x for x in tw['registry']}
r=verification.load_report(ROOT/'work/verification/PB-031/a01cb14755a444749f0f372c636b995e/report.json',playbook.require)
fresh,diff=verification.inputs_fresh(ROOT,r,{'docs/tasks/PB-031.md'},playbook.safe_path,playbook.require)
print('HANDOFF_REPORT',r['status'],'unchanged=',r.get('inputs_unchanged'),'fresh=',fresh,'diff=',json.dumps(diff,ensure_ascii=False))
original=documents.parse_meta((ROOT/'docs/tasks/PB-031.md').read_text(),'card',[])
print('CARD_PROJECTION_MATCH',original['handoff']['card_projection']==lifecycle.card_projection((ROOT/'docs/tasks/PB-031.md').read_text()))
def task(label,m):
    errors=[]; documents._validate_task_meta(m,label,errors,registry=reg,task_workflow_enabled=True,root=ROOT,safe_path=playbook.safe_path)
    print(label,'ACCEPT' if not errors else 'REJECT',json.dumps(errors,ensure_ascii=False))
for label,mut in [('missing_plan',lambda m:m.pop('verification_plan')),('null_plan',lambda m:m.update(verification_plan=None)),('missing_inputs',lambda m:m['verification_plan']['checks'][0].pop('inputs')),('null_inputs',lambda m:m['verification_plan']['checks'][0].update(inputs=None)),('file_without_path',lambda m:m['verification_plan']['checks'][0].update(inputs=[{'type':'file'}])),('environment_invalid',lambda m:m['verification_plan']['checks'][0].update(inputs=[{'type':'environment','names':123}]))]:
    m=copy.deepcopy(meta); mut(m); task(label,m)
for label,mut in [('no_at',lambda r:r.pop('at')),('no_actor',lambda r:r['results'][0].pop('actor')),('no_command_snapshots',lambda r:[r['results'][0].pop(k,None) for k in ['snapshot_before','snapshot_after']]),('invalid_snapshots_and_actor',lambda r:r['results'][0].update(input_snapshot={'files':123},snapshot_before='bad',snapshot_after=False,actor={'name':42})),('no_manual_scenario',lambda r:r['results'][1].pop('scenario_hash')),('invalid_candidate',lambda r:r['results'][1].update(candidate_snapshot={'files':123}))]:
    r=copy.deepcopy(report);mut(r)
    try:
        with tempfile.TemporaryDirectory() as tmp_report:
            report_path=Path(tmp_report)/'report.json';report_path.write_text(json.dumps(r));verification.load_report(report_path,playbook.require)
        print(label,'ACCEPT')
    except (ValueError,TypeError) as e: print(label,'REJECT',str(e))
with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp)/'project'
    shutil.copytree(ROOT,root,ignore=lambda d,ns:[n for n in ns if n in {'.git','__pycache__'}])
    (root/'audit-external').symlink_to('/tmp',target_is_directory=True)
    (root/'audit-internal').symlink_to(root/'tests',target_is_directory=True)
    (root/'audit-parent').mkdir();(root/'audit-parent/link').symlink_to(root/'tests',target_is_directory=True)
    card=root/'docs/tasks/PB-031.md'; text=card.read_text();start=text.index('<!-- playbook-meta:start -->');end=text.index('<!-- playbook-meta:end -->')+len('<!-- playbook-meta:end -->')
    conf=root/'ai-dev-playbook/project.json'; baseline=json.loads(conf.read_text())
    for name,path,enabled,unknown in [('external','audit-external',True,False),('internal','audit-internal',True,False),('parent','audit-parent/link/new.py',True,False),('absent','tests/absent/new.py',True,False),('disabled','tests',False,False),('unknown','tests',True,True),('null_plan_cli','tests',True,False),('null_inputs_cli','tests',True,False)]:
        data=copy.deepcopy(baseline)
        if enabled:data['task_workflow']=tw
        m=copy.deepcopy(meta);m['verification_plan']['scope']=[path];m['verification_plan']['checks'][0]['inputs']=[path]
        if unknown:m['verification_plan']['checks'][0]['registry_id']='DOES-NOT-EXIST'
        if name=='null_plan_cli':m['verification_plan']=None
        if name=='null_inputs_cli':m['verification_plan']['checks'][0]['inputs']=None
        conf.write_text(json.dumps(data));card.write_text(text[:start]+'<!-- playbook-meta:start -->\n```json\n'+json.dumps(m)+'\n```\n<!-- playbook-meta:end -->'+text[end:])
        run=subprocess.run([sys.executable,str(ROOT/'ai-dev-playbook/scripts/playbook.py'),'--root',str(root),'check','--ready'],text=True,capture_output=True)
        print('CLI',name,'exit=',run.returncode,(run.stdout+run.stderr).strip()[:700]); assert run.returncode == (0 if name=='absent' else 1), (name,run.stdout,run.stderr)

# Positive controls for the production loader and independent field mutations.
with tempfile.TemporaryDirectory() as tmp_report:
    path=Path(tmp_report)/'report.json'
    path.write_text(json.dumps(report)); verification.load_report(path,playbook.require)
    print('VALID_REPORT_LOADER ACCEPT')
    for label,idx,field,value in [
        ('actor_name_integer',0,'actor',{'name':42}),
        ('actor_name_blank',0,'actor',{'name':' '}),
        ('input_files_integer',0,'input_snapshot',{'files':123}),
        ('input_empty_object',0,'input_snapshot',{}),
        ('before_string',0,'snapshot_before','bad'),
        ('after_boolean',0,'snapshot_after',False),
        ('candidate_files_integer',1,'candidate_snapshot',{'files':123}),
        ('candidate_unknown_field',1,'candidate_snapshot',{'files':{},'extra':1})
    ]:
        damaged=copy.deepcopy(report);damaged['results'][idx][field]=value
        path.write_text(json.dumps(damaged))
        try:verification.load_report(path,playbook.require)
        except ValueError as error:print('LOADER',label,'REJECT',str(error))
        else:raise AssertionError('Damaged report accepted: '+label)
print('LOADER_MUTATIONS_PASS')
