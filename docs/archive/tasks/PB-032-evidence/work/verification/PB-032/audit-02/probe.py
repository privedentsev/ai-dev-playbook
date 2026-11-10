import copy
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path.cwd() / 'ai-dev-playbook/scripts'))
import verification as v
import verification_policy as p
count = 0

def check(name, condition):
    global count
    assert condition, name
    count += 1
    print('PASS', name)

def require(ok, msg):
    if not ok:
        raise ValueError(msg)

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp).resolve()
    (root / 'a').write_text('old')
    (root / 'b').write_text('independent')
    old = p.snapshot(root, ['a'])
    independent = p.snapshot(root, ['b'])
    (root / 'a').write_text('new')
    current = p.snapshot(root, ['a'])
    for kind, fields in [('command', ['snapshot_before', 'snapshot_after']),
                         ('manual', ['candidate_snapshot'])]:
        base = dict(check_id='a', definition_hash='def', kind=kind, status='passed',
                    actor='auditor', environment={}, input_snapshot=current,
                    started_at='2026-10-29T06:00:00+03:00',
                    finished_at='2026-10-29T06:01:00+03:00')
        if kind == 'command':
            base.update(exit_code=0, stdout_log='stdout.log', stderr_log='stderr.log')
        else:
            base.update(scenario_hash='scenario', observation='Reviewed candidate')
        base.update({field: current for field in fields})
        args = dict(definition_hash='def', input_snapshot=current, environment={}, policy_hash='policy')
        def reuse(result):
            return p.reusable(dict(result, policy_hash='policy'), **args)
        check(kind + ': matching snapshots', reuse(base))
        mismatch = dict(base, **{field: old for field in fields})
        report = dict(schema_version=2, run_id='probe', task_id='TASK-101',
                      route='small', plan_revision=1, plan_hash='plan', policy_hash='policy',
                      at='2026-10-29T06:01:00+03:00', status='passed', results=[mismatch],
                      coverage=dict(total_checks=1, passed=['a'], failed=[],
                                    not_applicable=[], not_run=[], satisfied=True))
        v.validate_report(report, require)
        check(kind + ': A-01 schema-valid mismatch rejected', not reuse(mismatch))
        for field in ['input_snapshot'] + fields:
            for label, snap in [('old', old), ('absent', None), ('opaque', {'sha256':'unknown'}),
                                ('empty', {'files':{}}), ('other read-set', independent)]:
                check(kind + ': ' + field + '/' + label,
                      not reuse(dict(base, **{field:snap})))
            missing = copy.deepcopy(base)
            del missing[field]
            check(kind + ': missing ' + field, not reuse(missing))
    independent_result = dict(kind='command', status='passed', definition_hash='b',
                              policy_hash='policy', environment={}, input_snapshot=independent,
                              snapshot_before=independent, snapshot_after=independent, exit_code=0)
    check('independent b remains reusable after a changes', p.reusable(independent_result,
          definition_hash='b', input_snapshot=p.snapshot(root, ['b']), environment={}, policy_hash='policy'))
    required = {ident:{'required':True} for ident in ['a','b']}
    for status in ['failed','timeout','not_run']:
        check('coverage rejects b=' + status, not p.coverage(required,
              {'a':{'status':'passed'}, 'b':{'status':status}})['satisfied'])
    plan = dict(scope=['docs'], checks=[])
    settings = dict(registry=[dict(id='regression', applicability='checkpoint')],
                    checkpoints={'release':['regression']})
    check('local close does not require release regression', p.required_checks(settings, plan,
          baseline={}, current={})['ready'])
    check('release requires omitted regression', not p.required_checks(settings, plan,
          baseline={}, current={}, checkpoint='release')['ready'])
print('TOTAL', count, 'PASS')
