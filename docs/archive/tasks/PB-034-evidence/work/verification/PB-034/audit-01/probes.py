import sys, unittest, copy
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
from test_task_workflow_lifecycle import TaskWorkflowLifecycleTest
from test_playbook import playbook

class AuditProbe(TaskWorkflowLifecycleTest):
    def test_audit_plan_drift_blocks_accept(self):
        self.start_verify_handoff()
        meta = self.meta()
        meta['verification_plan']['rationale'] += ' revised'
        self.card.write_text(playbook.lifecycle._splice_meta(self.card.read_text(), meta))
        self.assertEqual(self.engineer_accept(ok=False)[0], 1)
    def test_audit_policy_drift_blocks_accept(self):
        self.start_verify_handoff()
        self.settings['task_workflow']['registry'][0]['purpose'] += ' revised'
        self.save_project()
        self.assertEqual(self.engineer_accept(ok=False)[0], 1)
    def test_audit_changed_source_blocks_close_after_accept(self):
        self.start_verify_handoff()
        self.engineer_accept()
        (self.root / 'src/value.txt').write_text('after acceptance')
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 1)
    def test_audit_small_route_accepts_independent_auditor(self):
        self.trans('start')
        self.trans('route', '--to', 'small')
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        result = self.accept('--result', 'accepted', '--role', 'auditor')
        self.assertEqual(result[0], 0, result)
        result = self.cli('close', 'DEV-1', '--dry-run')
        self.assertEqual(result[0], 0, result)

suite = unittest.TestSuite(AuditProbe(n) for n in unittest.defaultTestLoader.getTestCaseNames(AuditProbe) if n.startswith('test_audit_') and n != 'test_audit_package_and_accept_repeat_do_not_repeat_expensive_check')
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
