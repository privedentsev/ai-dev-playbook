"""PB-035: quick template and real CLI iterations on ordinary cards."""
import json
from pathlib import Path

from test_close_lifecycle import CloseLifecycleCase
import test_task_workflow_lifecycle as workflow_module
import sys

from test_task_cards import record
from test_playbook import playbook


class QuickRouteTest(CloseLifecycleCase):
    def setUp(self):
        super().setUp()
        self.project_file = self.root / 'ai-dev-playbook/project.json'
        self.settings = json.loads(self.project_file.read_text())
        self.settings['task_workflow'] = {'schema_version': 2, 'registry': [{
            'id': 'unit-tests', 'kind': 'command', 'purpose': 'Behavior',
            'argv': [sys.executable, '-c',
                "from pathlib import Path; p=Path('work/count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"],
            'timeout_seconds': 30, 'default_inputs': ['src'], 'applicability': {'kind': 'always'}}]}
        (self.root / 'src').mkdir()
        (self.root / 'src/value.txt').write_text('original')
        self.save_project()
        self.create_v2_task(status='draft', scope=['src'], checks=[{
            'id': 'unit-tests', 'kind': 'command', 'purpose': 'Behavior', 'registry_id': 'unit-tests',
            'inputs': ['src', {'type': 'task_projection'}, {'type': 'policy_projection'}]}])

    save_project = workflow_module.TaskWorkflowLifecycleTest.save_project
    create_v2_task = workflow_module.TaskWorkflowLifecycleTest.create_v2_task
    add_registry_check = workflow_module.TaskWorkflowLifecycleTest.add_registry_check
    card = workflow_module.TaskWorkflowLifecycleTest.card
    def meta(self):
        return playbook.task_cards.metadata(playbook.documents.read_text(self.card))
    trans = workflow_module.TaskWorkflowLifecycleTest.trans
    def engineer_accept(self):
        self.card.write_text(self.card.read_text() + record('AUDIT', actor='Engineer', **{'Модель': '—'}))
        result = self.cli('lifecycle', 'accept', 'DEV-1', '--actor', 'Engineer', '--model', '—',
                          '--role', 'engineer', '--result', 'accepted',
                          '--note', 'Engineer explicitly accepts this version')
        self.assertEqual(result[0], 0, result[1] + result[2])
        return result

    def create(self, *extra, apply=True):
        return self.cli('card', 'create', 'DEV-1', '--title', 'Local button spacing',
                        '--template', 'quick', '--reason', 'One local visual change, no behavior',
                        '--verification-plan', 'work/plan.json', *extra,
                        *(['--apply'] if apply else []))

    def prepare(self):
        plan = self.meta()['verification_plan']
        self.card.unlink()
        self.assertEqual(self.cli('index')[0], 0)
        (self.root / 'work').mkdir(exist_ok=True)
        (self.root / 'work/plan.json').write_text(json.dumps(plan))

    def fill(self):
        text = self.card.read_text().replace(
            'TODO: локальный результат без нового поведения; что не входит.', 'Local spacing only.')
        text = text.replace('TODO: наблюдаемый результат последней версии.', 'Visible spacing on latest version.')
        self.card.write_text(text)

    def test_preview_create_and_incomplete_start(self):
        self.prepare()
        self.assertEqual(self.create(apply=False)[0], 0)
        self.assertFalse(self.card.exists())
        self.assertEqual(self.create()[0], 0)
        sidecar = self.card.with_suffix('.meta.json')
        self.assertTrue(sidecar.is_file())
        self.assertEqual(json.loads(sidecar.read_text())['workflow']['route'], 'quick')
        self.assertEqual(playbook.task_cards.parse(self.card.read_text())['card_format_version'], 2)
        self.assertEqual(self.trans('start', ok=False)[0], 1)
        self.assertNotIn('workflow-baseline', sidecar.read_text())
        before = self.card.read_bytes()
        self.assertEqual(self.create()[0], 1)
        self.assertEqual(self.card.read_bytes(), before)

    def test_legacy_refusal_and_standard_creation_unchanged(self):
        self.prepare()
        del self.settings['task_workflow']
        self.save_project()
        self.assertEqual(self.create()[0], 1)
        self.assertFalse(self.card.exists())
        self.assertEqual(self.cli('card', 'create', 'DEV-1', '--title', 'Standard', '--apply')[0], 0)
        self.assertEqual(json.loads(self.card.with_suffix('.meta.json').read_text())['schema_version'], 1)

    def test_bad_plan_and_missing_reason_leave_no_card(self):
        self.prepare()
        (self.root / 'work/plan.json').write_text('{}')
        self.assertEqual(self.create()[0], 1)
        self.assertFalse(self.card.exists())
        self.assertEqual(self.cli('card', 'create', 'DEV-1', '--title', 'Bad', '--template', 'quick',
                                  '--verification-plan', 'work/plan.json', '--apply')[0], 1)
        self.assertFalse(self.card.with_suffix('.meta.json').exists())

    def test_three_iterations_one_id_then_explicit_engineer_acceptance(self):
        self.prepare()
        self.assertEqual(self.create()[0], 0)
        self.fill()
        self.trans('start')
        for iteration in range(1, 4):
            (self.root / 'src/value.txt').write_text('spacing ' + str(iteration))
            if iteration == 3:
                self.card.write_text(self.card.read_text() + record('EXEC'))
            self.verify()
            self.assertIn('Status: in_progress', self.card.read_text())
            self.assertNotIn('handoff', self.meta())
            self.assertFalse((self.root / 'docs/archive/tasks/DEV-1.md').exists())
        self.card.write_text(self.card.read_text() + record('CHECK') + record('HANDOFF'))
        self.assertEqual(self.handoff()[0], 0)
        self.assertIn('Status: in_review', self.card.read_text())
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 1)
        self.assertEqual(self.accept('--result', 'accepted')[0], 1)
        self.engineer_accept()
        self.assertEqual(self.cli('close', 'DEV-1')[0], 0)
        self.assertTrue((self.root / 'docs/archive/tasks/DEV-1.meta.json').exists())
        self.assertEqual((self.root / 'work/count').read_text(), '3')

    def test_review_revise_and_raise_same_card_preserve_history(self):
        self.prepare()
        self.assertEqual(self.create()[0], 0)
        self.fill()
        self.trans('start')
        self.card.write_text(self.card.read_text() + record('EXEC'))
        self.verify()
        self.card.write_text(self.card.read_text() + record('CHECK') + record('HANDOFF'))
        self.assertEqual(self.handoff()[0], 0)
        self.engineer_accept()
        previous = self.meta()['handoff']
        self.trans('revise')
        self.assertNotIn('acceptance', self.meta())
        self.assertEqual(self.meta()['workflow']['transitions'][-1]['handoff'], previous)
        self.trans('route', '--to', 'small')
        self.assertEqual(self.meta()['workflow']['route'], 'small')
        self.assertEqual(self.meta()['workflow']['history'][-1]['from_route'], 'quick')
        self.assertEqual(self.trans('route', '--to', 'quick', ok=False)[0], 1)
        self.assertTrue(self.card.with_suffix('.meta.json').exists())
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 1)
