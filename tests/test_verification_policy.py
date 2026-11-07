import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ai-dev-playbook/scripts'))
import verification_policy as p


class PolicyTests(unittest.TestCase):
    def app(self, patterns, scope=('docs',), baseline=None, current=None):
        return p.applicability({'kind': 'when_paths', 'paths': patterns},
                               scope=list(scope), baseline=baseline, current=current)

    def test_baseline_unknown_is_required(self):
        self.assertTrue(self.app(['src/**'])['required'])

    def test_missing_scope_is_required(self):
        self.assertTrue(self.app(['src/**'], scope=(), baseline={}, current={})['required'])

    def test_narrow_unrelated_scope(self):
        self.assertFalse(self.app(['src/**/*.py'], baseline={}, current={})['required'])

    def test_broad_scope_and_wildcard_prefix(self):
        for scope, pattern in [('src', 'src/deep/*.py'), ('src/deep', 'src/**'),
                               ('src', '**/*.py'), ('src/deep', 'src/*/nested/*.py')]:
            with self.subTest(scope=scope, pattern=pattern):
                self.assertTrue(self.app([pattern], [scope], {}, {})['required'])

    def test_added_deleted_changed_and_zero_segment_glob(self):
        for before, after in [({}, {'src/a.py': 'x'}), ({'src/a.py': 'x'}, {}),
                              ({'src/deep/a.py': 'x'}, {'src/deep/a.py': 'y'})]:
            self.assertTrue(self.app(['src/**/*.py'], baseline=before, current=after)['required'])

    def test_star_does_not_cross_slash(self):
        self.assertFalse(self.app(['src/*.py'], baseline={},
                                  current={'src/deep/a.py': 'x'})['required'])

    def test_always_and_explicit_checkpoint(self):
        args = dict(scope=[], baseline=None, current=None)
        self.assertTrue(p.applicability(**args)['required'])
        self.assertFalse(p.applicability('checkpoint', **args)['required'])
        self.assertTrue(p.applicability({'kind': 'checkpoint', 'checkpoint': 'release'},
                                       checkpoint='release', **args)['required'])
        self.assertFalse(p.applicability({'kind': 'checkpoint', 'checkpoint': 'release'},
                                        checkpoint='merge', **args)['required'])

    def test_project_obligation_cannot_be_omitted_or_weakened(self):
        settings = {'registry': [{'id': 'unit', 'applicability': 'always'}]}
        plan = {'scope': ['docs'], 'checks': []}
        self.assertFalse(p.required_checks(settings, plan, baseline={}, current={})['ready'])
        plan['checks'] = [{'id': 'local', 'registry_id': 'unit',
                           'applicability': {'kind': 'when_paths', 'paths': ['src/**']}}]
        resolved = p.required_checks(settings, plan, baseline={}, current={})
        self.assertTrue(resolved['ready'])
        self.assertTrue(resolved['checks']['local']['required'])
        plan['checks'] = []
        resolved = p.required_checks({'registry': []}, plan, baseline={}, current={}, failed_history=['removed'])
        self.assertFalse(resolved['ready'])

    def test_coverage_every_id_and_status(self):
        required = {k: {'required': True} for k in ('a', 'b')}
        for status in ('failed', 'timeout', 'not_run', 'not_applicable'):
            self.assertFalse(p.coverage(required, {'a': {'status': 'passed'},
                                                 'b': {'status': status}})['satisfied'])
        self.assertFalse(p.coverage(required, {'a': {'status': 'passed'}})['satisfied'])
        self.assertTrue(p.coverage(required, {k: {'status': 'passed'} for k in required})['satisfied'])

    def test_failed_exclusion_requires_engineer_history(self):
        decision = dict(check_id='a', at='now', actor='executor',
                        decision='not_applicable', reason='outside scope')
        self.assertFalse(p.exclusion_allowed('a', [decision], {'a'}))
        decision['engineer_approval'] = dict(who='engineer', at='now', source='request', detail='approved')
        self.assertTrue(p.exclusion_allowed('a', [decision], {'a'}))
        self.assertEqual(decision['decision'], 'not_applicable')

    def test_task_projection_preserves_requirements_and_metadata(self):
        text = '# Task\nStatus: draft\nCreated: yesterday\n\n## Goal\nDo it\n'
        meta = dict(schema_version=2, verification_plan={'revision': 1},
                    workflow={'route': 'small', 'history': []}, handoff={'id': 'old'})
        original = p.task_projection(text, meta)
        changed = copy.deepcopy(meta)
        changed['handoff'] = {'id': 'new'}
        changed['workflow']['history'].append('event')
        self.assertEqual(original, p.task_projection(text.replace('draft', 'in_review'), changed))
        for change in [dict(verification_plan={'revision': 2}), dict(extra='unknown'),
                       dict(workflow={'route': 'medium'})]:
            self.assertNotEqual(original, p.task_projection(text, dict(meta, **change)))
        self.assertNotEqual(original, p.task_projection(text.replace('Do it', 'New requirement'), meta))

    def test_independent_policy_definition(self):
        policy = {'schema_version': 2, 'registry': [{'id': 'a', 'argv': ['a']}, {'id': 'b', 'argv': ['b']}]}
        original = p.policy_projection(policy, ['a'])
        policy['registry'][1]['argv'] = ['new']
        self.assertEqual(original, p.policy_projection(policy, ['a']))
        policy['registry'][0]['argv'] = ['changed']
        self.assertNotEqual(original, p.policy_projection(policy, ['a']))
        with self.assertRaises(ValueError):
            p.policy_projection(policy, ['unknown'])

    def test_absent_create_delete_directory_membership(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'src').mkdir()
            inputs = ['src', {'type': 'file', 'path': 'future'}]
            before = p.snapshot(root, inputs)
            (root / 'src/a').write_text('new')
            after = p.snapshot(root, inputs)
            self.assertFalse(p.compare_snapshots(before, after)['fresh'])
            (root / 'src/a').unlink()
            self.assertEqual(before, p.snapshot(root, inputs))
            (root / 'future').write_text('created')
            self.assertFalse(p.compare_snapshots(before, p.snapshot(root, inputs))['fresh'])

    def test_path_rejections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'file').write_text('x')
            (root / 'link').symlink_to(root / 'file')
            (root / 'dir').mkdir()
            (root / 'dir/link').symlink_to(root / 'file')
            for path in ('../escape', '/absolute', './file', 'a//b', 'a/../b',
                         'work/report', 'link', 'file/child', 'dir'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    p.snapshot(root, [path])
            with self.assertRaises(ValueError):
                p.snapshot(root, [{'type': 'directory', 'path': 'file'}])
            with self.assertRaises(ValueError):
                p.snapshot(root, [{'type': 'file', 'path': 'file'}] * 2)

    def test_environment_and_unknown_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with self.assertRaises(ValueError):
                p.snapshot(root, [{'type': 'environment', 'names': ['python']}])
            a = p.snapshot(root, [{'type': 'environment', 'names': ['python']}], environment={'python': '3'})
            b = p.snapshot(root, [{'type': 'environment', 'names': ['python']}], environment={'python': '4'})
            self.assertFalse(p.compare_snapshots(a, b)['fresh'])
        self.assertFalse(p.compare_snapshots({'sha256': 'x'}, {'sha256': 'x'})['fresh'])
        self.assertFalse(p.compare_snapshots({'files': {}}, {'files': {}})['fresh'])

    def assert_reuse_snapshot_binding(self, kind):
        # Real file changes produce schema-valid but internally inconsistent
        # reports, reproducing A-01 without relying on malformed result data.
        import verification
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'src.py').write_text('old version')
            old = p.snapshot(root, ['src.py'])
            (root / 'src.py').write_text('new version')
            current = p.snapshot(root, ['src.py'])
        result = dict(check_id='check-a', kind=kind, definition_hash='definition',
                      status='passed', actor={'name': 'tester'}, environment={'python': '3'},
                      input_snapshot=current, started_at='2026-10-29T06:00:00+03:00',
                      finished_at='2026-10-29T06:01:00+03:00')
        if kind == 'command':
            result.update(exit_code=0, stdout_log='stdout.log', stderr_log='stderr.log',
                          snapshot_before=old, snapshot_after=old)
            snapshot_fields = ('snapshot_before', 'snapshot_after')
        else:
            result.update(scenario_hash='scenario', observation='Reviewed old version',
                          candidate_snapshot=old)
            snapshot_fields = ('candidate_snapshot',)
        report = dict(schema_version=2, run_id='regression-a01', task_id='TASK-101',
                      route='small', plan_revision=1, plan_hash='plan', policy_hash='policy',
                      at='2026-10-29T06:01:00+03:00', status='passed', results=[result],
                      coverage=dict(total_checks=1, passed=['check-a'], failed=[],
                                    not_applicable=[], not_run=[], satisfied=True))

        def require(condition, message):
            if not condition:
                raise ValueError(message)

        verification.validate_report(report, require)
        args = dict(definition_hash='definition', input_snapshot=current,
                    environment={'python': '3'}, policy_hash='policy')
        view = dict(result, policy_hash='policy')
        self.assertFalse(p.reusable(view, **args))
        consistent = dict(view, **{field: current for field in snapshot_fields})
        self.assertTrue(p.reusable(consistent, **args))
        for field in (*snapshot_fields, 'input_snapshot'):
            for unknown in (old, None, {'sha256': 'opaque'}, {'files': {}}):
                with self.subTest(kind=kind, field=field, snapshot=unknown):
                    self.assertFalse(p.reusable(dict(consistent, **{field: unknown}), **args))

    def test_command_reuse_binds_input_before_and_after(self):
        self.assert_reuse_snapshot_binding('command')

    def test_manual_reuse_binds_input_and_presented_candidate(self):
        self.assert_reuse_snapshot_binding('manual')

    def test_reuse_bindings_and_race(self):
        snap = {'files': {'src': 'hash'}}
        result = dict(kind='command', status='passed', definition_hash='def', policy_hash='policy',
                      environment={'python': '3'}, input_snapshot=snap, exit_code=0,
                      snapshot_before=snap, snapshot_after=snap)
        args = dict(definition_hash='def', policy_hash='policy', environment={'python': '3'}, input_snapshot=snap)
        self.assertTrue(p.reusable(result, **args))
        for key, value in [('definition_hash', 'other'), ('policy_hash', 'other'),
                           ('environment', {}), ('status', 'failed'), ('exit_code', 1),
                           ('input_snapshot', {'files': {'src': 'new'}}),
                           ('snapshot_after', {'files': {'new': 'hash'}})]:
            with self.subTest(key=key):
                self.assertFalse(p.reusable(dict(result, **{key: value}), **args))


if __name__ == '__main__':
    unittest.main()
