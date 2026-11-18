import copy
import json
import sys
import tempfile
from pathlib import Path
root = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(root / 'ai-dev-playbook/scripts'))
import verification
import verification_policy as p

def require(ok, message):
    if not ok:
        raise ValueError(message)

with tempfile.TemporaryDirectory() as tmp:
    project = Path(tmp).resolve()
    (project / 'src.py').write_text('old version')
    old = p.snapshot(project, ['src.py'])
    (project / 'src.py').write_text('new version')
    current = p.snapshot(project, ['src.py'])
    assert old != current
    common = dict(check_id='check-a', definition_hash='definition', status='passed',
                  actor={'name': 'auditor'}, environment={'python': '3'},
                  input_snapshot=current, started_at='2026-10-29T06:00:00+03:00',
                  finished_at='2026-10-29T06:01:00+03:00')
    cases = [dict(common, kind='command', exit_code=0, stdout_log='stdout.log',
                  stderr_log='stderr.log', snapshot_before=old, snapshot_after=old),
             dict(common, kind='manual', scenario_hash='scenario',
                  observation='Reviewed the old version', candidate_snapshot=old)]
    for result in cases:
        report = dict(schema_version=2, run_id='audit-probe', task_id='TASK-101',
                      route='small', plan_revision=1, plan_hash='plan', policy_hash='policy',
                      at='2026-10-29T06:01:00+03:00', status='passed', results=[result],
                      coverage=dict(total_checks=1, passed=['check-a'], failed=[],
                                    not_applicable=[], not_run=[], satisfied=True))
        verification.validate_report(report, require)
        reusable = p.reusable(dict(result, policy_hash=report['policy_hash']),
                              definition_hash='definition', input_snapshot=current,
                              environment={'python': '3'}, policy_hash='policy')
        print(json.dumps(dict(kind=result['kind'], schema_valid=True,
                              actual_snapshot_matches_current=False,
                              reusable=reusable, expected_reusable=False)))
        assert reusable is True, 'Defect no longer reproduced'
    # Controls: equal actual/current snapshots pass; changed explicit input rejects.
    result = dict(cases[0], snapshot_before=current, snapshot_after=current, policy_hash='policy')
    args = dict(definition_hash='definition', input_snapshot=current,
                environment={'python': '3'}, policy_hash='policy')
    assert p.reusable(result, **args)
    assert not p.reusable(dict(result, input_snapshot=old), **args)
    print('Controls: matching snapshots PASS; stale input_snapshot rejected PASS')
