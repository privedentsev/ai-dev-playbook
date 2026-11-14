"""PB-034: real CLI transitions, coverage, acceptance and operation recovery."""
import copy
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from test_close_lifecycle import CloseLifecycleCase
import test_verification_v2 as runner_fixture
from test_playbook import playbook


class TaskWorkflowLifecycleTest(CloseLifecycleCase):
    """Lifecycle workflow v2: start/route/revise, service-cycle,
    stale-отказы и покрытие."""
    save_project = runner_fixture.VerificationV2Test.save_project
    add_registry_check = runner_fixture.VerificationV2Test.add_registry_check
    create_v2_task = runner_fixture.VerificationV2Test.create_v2_task

    def setUp(self):
        super().setUp()
        self.project_file = self.root / 'ai-dev-playbook/project.json'
        self.settings = json.loads(self.project_file.read_text())
        self.settings['task_workflow'] = {'schema_version': 2, 'registry': [{
            'id': 'unit-tests', 'kind': 'command', 'purpose': 'Behavior',
            'argv': [sys.executable, '-c', 'pass'], 'timeout_seconds': 30,
            'default_inputs': ['src'], 'applicability': {'kind': 'always'}}]}
        (self.root / 'src').mkdir()
        (self.root / 'src/value.txt').write_text('original')
        self.settings['task_workflow']['registry'][0]['argv'] = [sys.executable, '-c',
            "from pathlib import Path; p=Path('work/count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"]
        self.save_project()
        self.create_v2_task(status='draft', scope=['src'], checks=[{
            'id': 'unit-tests', 'kind': 'command', 'purpose': 'Behavior', 'registry_id': 'unit-tests',
            'inputs': ['src', {'type': 'task_projection'}, {'type': 'policy_projection'}]}])
        p = self.card
        p.write_text(p.read_text() + '\n## Цель и границы\nСогласованная цель.\n\n## Приёмка\n\n- [ ] Проверяемый результат.\n')

    @property
    def card(self):
        return self.root / 'docs/tasks/DEV-1.md'

    def trans(self, action, *args, ok=True):
        result = self.cli('lifecycle', action, 'DEV-1', '--actor', 'Executor', '--model', 'Test model',
                          '--reason', 'Engineer authorized task', *args)
        if ok:
            self.assertEqual(result[0], 0, result[1] + result[2])
        return result

    def meta(self):
        return playbook.task_cards.metadata(self.card.read_text())

    def start_verify_handoff(self):
        self.trans('start')
        self.verify()
        code, out, err = self.handoff()
        self.assertEqual(code, 0, out + err)

    def engineer_accept(self, ok=True):
        result = self.accept('--result', 'accepted', '--role', 'engineer', '--note', 'Engineer explicitly accepts this version')
        if ok:
            self.assertEqual(result[0], 0, result[1] + result[2])
        return result

    def test_start_no_git_dirty_git_and_idempotence(self):
        """start без Git, с dirty Git и идемпотентность"""
        for git in (False, True):
            if git:
                self.assertEqual(subprocess.run(['git', 'init', '-q', str(self.root)]).returncode, 0)
                (self.root / 'src/value.txt').write_text('dirty git')
                self.trans('revise', ok=False)  # no review, does not alter baseline
                # previous start stays idempotent even after external changes
            self.trans('start')
            baseline = self.meta()['workflow']['baseline']
            data = (self.root / baseline['source']).read_bytes()
            self.trans('start')
            self.assertEqual((self.root / baseline['source']).read_bytes(), data)
            self.assertIn('src/value.txt', json.loads(data)['files'])
        self.assertIn('in_progress', self.card.read_text())
        self.assertIn('| in_progress |', (self.root / 'docs/tasks/index.md').read_text())

    def test_service_cycle_does_not_rerun_and_preserves_immutable_bytes(self):
        """Сервисный цикл не перезапускает и сохраняет immutable-байты"""
        self.start_verify_handoff()
        before = {p: p.read_bytes() for p in (self.root / 'work/verification/DEV-1').glob('*/report.json')}
        first = self.meta()['handoff']
        self.assertEqual(self.handoff()[0], 0)
        self.assertEqual(self.meta()['handoff'], first)
        self.assertEqual(self.accept('--result', 'accepted')[0], 1)  # quick needs engineer
        self.engineer_accept()
        for args in [('close', 'DEV-1', '--dry-run'), ('close', 'DEV-1')]:
            code, out, err = self.cli(*args)
            self.assertEqual(code, 0, out + err)
        self.assertEqual((self.root / 'work/count').read_text(), '1')
        archive = self.root / 'docs/archive/tasks/DEV-1-evidence'
        for path, content in before.items():
            self.assertEqual(next(archive.rglob(path.parent.name + '/report.json')).read_bytes(), content)

    def test_revise_preserves_history_and_stale_acceptance_is_blocked(self):
        """revise сохраняет историю, stale-приёмка блокируется"""
        self.start_verify_handoff()
        self.engineer_accept()
        old = copy.deepcopy(self.meta())
        self.trans('revise')
        self.assertNotIn('handoff', self.meta())
        self.assertEqual(self.meta()['workflow']['transitions'][-1]['handoff'], old['handoff'])
        self.assertEqual(self.meta()['workflow']['transitions'][-1]['acceptance'], old['acceptance'])
        self.trans('revise')
        (self.root / 'src/value.txt').write_text('new result')
        self.assertEqual(self.handoff()[0], 1)
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 1)
        self.engineer_accept()
        self.assertEqual((self.root / 'work/count').read_text(), '2')

    def test_stale_source_plan_policy_and_report_reject_acceptance(self):
        """Устаревшие source/plan/policy/report отклоняют приёмку"""
        self.start_verify_handoff()
        source = self.root / 'src/value.txt'
        source.write_text('changed')
        self.assertEqual(self.engineer_accept(ok=False)[0], 1)
        source.write_text('original')
        report = next((self.root / 'work/verification/DEV-1').glob('*/report.json'))
        report.write_text(report.read_text() + ' ')
        self.assertEqual(self.engineer_accept(ok=False)[0], 1)

    def test_route_requires_approval_and_keeps_failures(self):
        """route требует одобрения и сохраняет отказы"""
        self.trans('start')
        self.trans('route', '--to', 'large')
        self.assertEqual(self.trans('route', '--to', 'quick', ok=False)[0], 1)
        self.trans('route', '--to', 'quick', '--engineer-approval', 'Engineer: scope checked, lower risk')
        self.assertEqual(len(self.meta()['workflow']['history']), 2)

    def test_start_missing_goal_or_dependency_refuses_without_writes(self):
        """start без цели/зависимости отклоняется без записи"""
        self.card.write_text(self.card.read_text().replace('## Цель и границы', '## Unclear'))
        before = self.card.read_bytes()
        self.assertEqual(self.trans('start', ok=False)[0], 1)
        self.assertEqual(self.card.read_bytes(), before)

    def test_handoff_with_no_or_failed_results_refuses(self):
        """handoff без результатов или с failed отклоняется"""
        self.trans('start')
        self.assertEqual(self.handoff()[0], 1)
        self.settings['task_workflow']['registry'][0]['argv'] = [sys.executable, '-c', 'raise SystemExit(1)']
        self.save_project()
        self.assertEqual(self.cli('verify', 'DEV-1')[0], 1)
        self.assertEqual(self.handoff()[0], 1)
        self.assertIn('Status: in_progress', self.card.read_text())

    def test_conflict_after_preparation_refuses_and_keeps_external_edit(self):
        """Конфликт после подготовки отклоняется, внешняя правка сохранена"""
        original = playbook.lifecycle._write_card_meta
        def inject(root, *args, **kwargs):
            (self.root / 'src/value.txt').write_text('external edit')
            return original(root, *args, **kwargs)
        before = self.card.read_bytes()
        with patch.object(playbook.lifecycle, '_write_card_meta', side_effect=inject):
            self.assertEqual(self.trans('start', ok=False)[0], 1)
        self.assertEqual(self.card.read_bytes(), before)
        self.assertEqual((self.root / 'src/value.txt').read_text(), 'external edit')

    def test_latest_failure_cannot_be_hidden_by_explicit_old_report(self):
        """Поздний failure не скрывается явным старым отчётом"""
        self.trans('start')
        self.verify()
        report = next((self.root / 'work/verification/DEV-1').glob('*/report.json'))
        self.settings['task_workflow']['registry'][0]['argv'] = [sys.executable, '-c', 'raise SystemExit(1)']
        self.save_project()
        self.assertEqual(self.cli('verify', 'DEV-1')[0], 1)
        self.assertEqual(self.handoff('--report', str(report.relative_to(self.root)))[0], 1)

    def test_audit_package_and_accept_repeat_do_not_repeat_expensive_check(self):
        """Audit package и повторный accept не повторяют дорогую проверку"""
        self.start_verify_handoff()
        self.assertEqual(self.cli('lifecycle', 'prepare-audit', 'DEV-1', '--id', 'audit-01', '--apply')[0], 0)
        package = self.root / 'work/verification/DEV-1/audit-01'
        (package / 'audit.txt').write_text('Independent result for engineer')
        self.engineer_accept()
        acceptance = self.meta()['acceptance']
        self.engineer_accept()
        self.assertEqual(self.meta()['acceptance'], acceptance)
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 0)
        self.assertEqual((self.root / 'work/count').read_text(), '1')

    def test_log_bytes_and_baseline_corruption_reject_receipt(self):
        """Повреждение байтов лога и baseline отклоняет receipt"""
        self.start_verify_handoff()
        log = next((self.root / 'work/verification/DEV-1').rglob('*.stdout.log'))
        log.write_text('changed')
        self.assertEqual(self.engineer_accept(ok=False)[0], 1)
        baseline = self.root / self.meta()['workflow']['baseline']['source']
        baseline.write_text('{}')
        self.assertEqual(self.cli('verify', 'DEV-1', '--preflight')[0], 1)

    def test_changed_paths_from_real_baseline_add_project_obligations(self):
        """Изменённые пути от baseline добавляют project-обязательства"""
        self.add_registry_check({'id': 'backend', 'kind': 'command', 'purpose': 'Backend',
            'argv': [sys.executable, '-c', 'pass'], 'timeout_seconds': 30,
            'default_inputs': ['backend'], 'applicability': {'kind': 'when_paths', 'paths': ['backend/**']}})
        self.trans('start')
        self.assertEqual(self.cli('verify', 'DEV-1', '--preflight')[0], 0)
        (self.root / 'backend').mkdir()
        (self.root / 'backend/new.py').write_text('changed')
        result = self.cli('verify', 'DEV-1', '--preflight')
        self.assertEqual(result[0], 1)
        self.assertIn('backend', result[1] + result[2])

    def test_pending_and_foreign_lock_prevent_handoff(self):
        """Pending и чужой lock предотвращают handoff"""
        self.trans('start')
        self.verify()
        lock = playbook.file_operations._operation_lock(self.root, playbook.safe_path)
        try:
            result = self.handoff()
            self.assertEqual(result[0], 1)
            self.assertIn('другим процессом', result[2])
        finally:
            lock.close()
        self.assertNotIn('"result_hash"', self.card.read_text())

    def test_real_sigkill_staged_and_mid_write_restore_and_repeat(self):
        """Реальный SIGKILL staged/mid-write → restore и повтор"""
        self.trans('start')
        self.verify()
        before = self.card.read_bytes()
        index = (self.root / 'docs/tasks/index.md').read_bytes()
        script = r'''
import os, signal, sys
sys.path.insert(0, SCRIPTS)
import playbook
fops = playbook.file_operations
if HOOK == 'staged':
    original = fops._prepare_staged
    def kill(*a, **kw):
        original(*a, **kw)
        os.kill(os.getpid(), signal.SIGKILL)
    fops._prepare_staged = kill
else:
    original = fops._atomic_write
    count = [0]
    def kill(*a, **kw):
        count[0] += 1
        if count[0] == 2:
            os.kill(os.getpid(), signal.SIGKILL)
        return original(*a, **kw)
    fops._atomic_write = kill
playbook.main(['--root', ROOT, 'lifecycle', 'handoff', 'DEV-1', '--summary', 'ready', '--actor', 'Devin', '--model', 'SWE-2 High'])
'''
        for hook in ('staged', 'write'):
            child = 'SCRIPTS = %r\nROOT = %r\nHOOK = %r\n' % (str(Path(playbook.__file__).parent), str(self.root), hook) + script
            result = subprocess.run([sys.executable, '-c', child], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, -signal.SIGKILL, result.stdout + result.stderr)
            self.assertEqual(self.handoff()[0], 1)
            self.assertEqual(self.engineer_accept(ok=False)[0], 1)
            result = self.cli('lifecycle', 'restore')
            self.assertEqual(result[0], 0, result)
            result = self.cli('lifecycle', 'restore')
            self.assertEqual(result[0], 1, result)
            self.assertIn('Нет незавершённой операции', result[2])
            self.assertEqual(self.card.read_bytes(), before)
            self.assertEqual((self.root / 'docs/tasks/index.md').read_bytes(), index)
        self.assertEqual(self.handoff()[0], 0)
        self.engineer_accept()
        self.assertEqual((self.root / 'work/count').read_text(), '1')

    def test_markdown_v2_journal_coverage_and_close(self):
        """Markdown v2: покрытие журнала и close"""
        import test_task_cards as journal
        meta = self.meta()
        meta['card_format_version'] = 2
        self.managed_task(status='draft', meta=meta, body=journal.BODY.replace('- AC-01:', '- [ ] AC-01:'))
        self.trans('start')
        self.card.write_text(self.card.read_text() + journal.record('EXEC'))
        self.verify()
        self.card.write_text(self.card.read_text() + journal.record('CHECK') + journal.record('HANDOFF'))
        code, out, err = self.handoff()
        self.assertEqual(code, 0, out + err)
        self.card.write_text(self.card.read_text() + journal.record('AUDIT', actor='Auditor'))
        self.engineer_accept()
        result = self.cli('close', 'DEV-1', '--dry-run')
        self.assertEqual(result[0], 0, result)

    def test_manual_only_path_closes_without_command_run(self):
        """Manual-only путь закрывается без запуска команд"""
        self.settings['task_workflow']['registry'] = [{
            'id': 'visual', 'kind': 'manual', 'purpose': 'Visual check',
            'scenario': 'Inspect the current displayed result.', 'default_inputs': ['src'],
            'applicability': {'kind': 'always'}}]
        self.save_project()
        meta = self.meta()
        meta['verification_plan']['checks'] = [{'id': 'visual', 'kind': 'manual', 'purpose': 'Visual check',
            'registry_id': 'visual', 'inputs': ['src', {'type': 'task_projection'}]}]
        self.card.write_text(playbook.lifecycle._splice_meta(self.card.read_text(), meta))
        self.trans('start')
        self.assertEqual(self.cli('verify', 'DEV-1')[0], 1)
        self.assertEqual(self.handoff()[0], 1)
        report = json.loads(next((self.root / 'work/verification/DEV-1').glob('*/report.json')).read_text())
        statement = {'check_id': 'visual', 'status': 'passed', 'observation': 'Displayed version checked',
            'environment': {'browser': 'Test browser'},
            'candidate_snapshot': report['results'][0]['candidate_snapshot'],
            'engineer_confirmation': {'who': 'Engineer', 'at': '2026-10-29T20:00:00+03:00',
                                      'statement': 'The displayed candidate was checked'}}
        (self.root / 'work/manual.json').write_text(json.dumps(statement))
        result = self.cli('verify', 'DEV-1', '--record-manual', 'work/manual.json')
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.handoff()[0], 0)
        self.engineer_accept()
        result = self.cli('close', 'DEV-1', '--dry-run')
        self.assertEqual(result[0], 0, result)
        self.assertFalse((self.root / 'work/count').exists())

    def test_external_metadata_pair_start_handoff_and_archive(self):
        """External metadata пара: start/handoff/archive"""
        content = self.card.read_text()
        meta = self.meta()
        meta['card_format_version'] = 1
        start, end = playbook.documents._meta_positions(content.split('\n'))
        lines = content.split('\n')
        self.card.write_text('\n'.join(lines[:start] + ['<!-- playbook-meta:external -->'] + lines[end + 1:]))
        self.card.with_suffix('.meta.json').write_text(json.dumps(meta))
        self.start_verify_handoff()
        self.engineer_accept()
        result = self.cli('close', 'DEV-1')
        self.assertEqual(result[0], 0, result)
        self.assertFalse(self.card.with_suffix('.meta.json').exists())
        self.assertTrue((self.root / 'docs/archive/tasks/DEV-1.meta.json').is_file())

    def test_changed_read_material_requires_its_check_again(self):
        """Изменённый read-материал требует повторной проверки"""
        source = self.root / 'docs/tasks/DEV-1-evidence/read.txt'
        source.parent.mkdir()
        source.write_text('input material')
        meta = self.meta()
        meta['verification_plan']['checks'][0]['inputs'].append('docs/tasks/DEV-1-evidence/read.txt')
        self.card.write_text(playbook.lifecycle._splice_meta(self.card.read_text(), meta))
        result = self.cli('lifecycle', 'register-material', 'DEV-1', str(source.relative_to(self.root)),
                          '--id', 'read-material', '--role', 'report', '--mutable', '--required', '--apply')
        self.assertEqual(result[0], 0, result)
        self.start_verify_handoff()
        source.write_text('changed input material')
        self.assertEqual(self.handoff()[0], 1)
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        self.engineer_accept()
        self.assertEqual((self.root / 'work/count').read_text(), '2')

    def test_damaged_latest_run_requires_replacement_and_preserves_history(self):
        """Повреждённый поздний прогон требует замены, история сохранена"""
        self.trans('start')
        self.verify()
        first = next((self.root / 'work/verification/DEV-1').glob('*/report.json'))
        # A more recent damaged UUID runner result cannot reveal an older pass.
        directory = self.root / 'work/verification/DEV-1/00000000000000000000000000000000'
        directory.mkdir()
        damaged = directory / 'report.json'
        damaged.write_text('{broken')
        self.assertEqual(self.handoff()[0], 1)
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        self.assertEqual(damaged.read_text(), '{broken')
        self.assertIn('verify-' + directory.name, {m['id'] for m in self.meta()['materials']})
        self.assertEqual((self.root / 'work/count').read_text(), '2')

    def test_immutable_material_file_directory_swap_cannot_preserve_receipt(self):
        """Swap файл↔каталог immutable-материала не сохраняет receipt"""
        material = self.root / 'docs/tasks/DEV-1-evidence/empty'
        material.mkdir(parents=True)
        result = self.cli('lifecycle', 'register-material', 'DEV-1', str(material.relative_to(self.root)),
                          '--id', 'empty-proof', '--role', 'report', '--immutable', '--required', '--apply')
        self.assertEqual(result[0], 0, result)
        self.start_verify_handoff()
        material.rmdir()
        material.write_text('{}')
        self.assertEqual(self.engineer_accept(ok=False)[0], 1)

    def test_start_service_writes_do_not_make_docs_obligation_applicable(self):
        """Сервисные записи start не делают docs-обязательство применимым"""
        self.add_registry_check({'id': 'docs', 'kind': 'command', 'purpose': 'Documentation',
            'argv': [sys.executable, '-c', 'pass'], 'timeout_seconds': 30,
            'default_inputs': ['docs'], 'applicability': {'kind': 'when_paths', 'paths': ['docs/**']}})
        self.trans('start')
        result = self.cli('verify', 'DEV-1', '--preflight')
        self.assertEqual(result[0], 0, result)
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        self.engineer_accept()
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 0)
        # Requirements stay visible despite the service projection.
        self.card.write_text(self.card.read_text().replace('Согласованная цель.', 'Новая цель.'))
        result = self.cli('verify', 'DEV-1', '--preflight')
        self.assertEqual(result[0], 1, result)
        self.assertIn('docs', result[1] + result[2])

    def test_new_unknown_proof_requires_new_handoff(self):
        """Новый unknown-proof требует нового handoff"""
        self.start_verify_handoff()
        proof = self.root / 'docs/tasks/DEV-1-evidence/additional.txt'
        proof.write_text('Additional evidence')
        result = self.cli('lifecycle', 'register-material', 'DEV-1', str(proof.relative_to(self.root)),
                          '--id', 'additional', '--role', 'report', '--immutable', '--required', '--apply')
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.engineer_accept(ok=False)[0], 1)
        self.assertEqual(self.handoff()[0], 0)
        self.engineer_accept()
        self.assertEqual((self.root / 'work/count').read_text(), '1')

    def test_first_start_captures_existing_dirty_git_bytes(self):
        """Первый start захватывает существующие dirty Git-байты"""
        import hashlib
        self.assertEqual(subprocess.run(['git', 'init', '-q', str(self.root)]).returncode, 0)
        self.assertEqual(subprocess.run(['git', '-C', str(self.root), 'add', 'src/value.txt']).returncode, 0)
        source = self.root / 'src/value.txt'
        source.write_text('dirty bytes differing from the Git index')
        self.trans('start')
        baseline = json.loads((self.root / self.meta()['workflow']['baseline']['source']).read_text())
        self.assertEqual(baseline['files']['src/value.txt'], 'sha256:' + hashlib.sha256(source.read_bytes()).hexdigest())
