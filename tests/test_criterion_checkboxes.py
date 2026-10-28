"""PB-040: computed criterion markers, proof freshness and transactional sync."""
import copy
import json
import unittest

from test_task_cards import BODY, record, finding, text, cards
from test_close_lifecycle import CloseLifecycleCase, TASK_META, playbook, fops, lifecycle
from test_documents import tree_fingerprint


class CriterionParserTest(unittest.TestCase):
    def test_only_real_markers_are_normalized(self):
        original = text(BODY.replace('Локальный проект.', 'Локальный проект.\n```markdown\n- [x] AC-99: пример.\n```'))
        checked = original.replace('- AC-01:', '- [x] AC-01:')
        unchecked = checked.replace('- [x] AC-01:', '- [ ] AC-01:')
        self.assertEqual(lifecycle.card_projection(checked), lifecycle.card_projection(original))
        self.assertEqual(lifecycle.card_projection(checked), lifecycle.card_projection(unchecked))
        self.assertNotEqual(lifecycle.card_projection(checked), lifecycle.card_projection(checked.replace('повтор безопасен.', 'требование изменено.')))
        self.assertNotEqual(lifecycle.card_projection(checked), lifecycle.card_projection(checked.replace('AC-99: пример.', 'AC-99: другой пример.')))
        self.assertIn('- [x] AC-99: пример.', cards.normalize_checkboxes(checked))

    def test_new_finding_requires_valid_criterion_links(self):
        body = BODY.replace('- AC-01:', '- [ ] AC-01:') + record('EXEC') + record('CHECK') + record('HANDOFF')
        body += record('AUDIT', **{'Результат': 'changes_requested'}) + finding(1)
        self.assertEqual(cards.parse(text(body))['entries'][-1]['findings'][0]['criteria'], ['AC-01'])
        for altered in (body.replace('Критерии: AC-01\nПроблема:', 'Критерии: AC-99\nПроблема:'),
                        body.replace('Критерии: AC-01\nПроблема:', 'Проблема:')):
            with self.assertRaises(ValueError): cards.parse(text(altered))


class CriterionCliTest(CloseLifecycleCase):
    def setup_card(self, two=False, linked=True):
        body = BODY.replace('- AC-01:', '- [ ] AC-01:')
        if two:
            body = body.replace('### План', '- [ ] AC-02: preview без записи.\n### План')
        body += record('EXEC') + record('CHECK', **{'Критерии': 'AC-01, AC-02' if two and linked else 'AC-01'})
        self.managed_task(meta={**copy.deepcopy(TASK_META), 'card_format_version': 2}, body=body)
        self.path = self.tasks_dir() / 'DEV-1.md'

    def sync(self, apply=True):
        return self.cli('card', 'sync', 'DEV-1', *(['--apply'] if apply else []))

    def view(self):
        result = self.cli('card', 'show', 'DEV-1', '--json')
        self.assertEqual(result[0], 0, result)
        return json.loads(result[1])

    def append(self, value):
        self.path.write_text(self.path.read_text() + value)

    def mark(self, ident):
        return cards.parse(self.path.read_text())['criterion_markers'][ident]

    def project(self):
        return {k: v for k,v in tree_fingerprint(self.root).items() if not k.startswith('work/')}

    def test_preview_read_only_partial_coverage_and_repeat(self):
        self.setup_card(two=True, linked=False); self.verify()
        before = tree_fingerprint(self.root)
        self.assertEqual(self.sync(False)[0], 0)
        self.assertEqual(before, tree_fingerprint(self.root))
        self.assertEqual(self.sync()[0], 0)
        self.assertTrue(self.mark('AC-01')); self.assertFalse(self.mark('AC-02'))
        before = tree_fingerprint(self.root)
        view = self.view()
        self.assertTrue(view['criterion_states']['AC-01']['checked'])
        self.assertFalse(view['criterion_states']['AC-02']['checked'])
        self.assertEqual(before, tree_fingerprint(self.root))
        self.assertEqual(self.sync()[0], 0)
        self.assertEqual(before, tree_fingerprint(self.root))
        self.assertEqual(self.cli('check', '--ready')[0], 0)

    def test_latest_failed_or_not_run_overrides_passed(self):
        self.setup_card(); self.verify(); self.assertEqual(self.sync()[0], 0)
        for number, result in enumerate(('failed', 'not_run'), 2):
            self.append(record('CHECK', number, **{'Результат': result}))
            view = self.view()
            self.assertFalse(view['criterion_states']['AC-01']['checked'])
            self.assertEqual(view['criterion_states']['AC-01']['reason'], result)
            if number == 2:
                self.assertIn('AC-01', view['checkbox_mismatches'])
            self.assertEqual(self.sync()[0], 0)
            self.assertFalse(self.mark('AC-01'))

    def test_finding_affects_only_linked_criteria_and_fix_needs_check_and_audit(self):
        self.setup_card(two=True); self.verify()
        self.append(record('HANDOFF'))
        self.assertEqual(self.handoff()[0], 0)
        self.append(record('AUDIT', **{'Результат': 'changes_requested'}) + finding(1))
        view = self.view()
        self.assertFalse(view['criterion_states']['AC-01']['checked'])
        self.assertTrue(view['criterion_states']['AC-02']['checked'])
        self.assertEqual(self.sync()[0], 0)
        self.append(record('EXEC', 2, title='Исправление', **{'Замечания': 'A-01'}))
        self.assertFalse(self.view()['criterion_states']['AC-01']['checked'])
        self.append(record('CHECK', 2, **{'Критерии': 'AC-01, AC-02'}))
        self.verify(); self.assertEqual(self.sync()[0], 0)
        self.assertFalse(self.mark('AC-01'))  # A passed test does not close a finding.
        self.append(record('HANDOFF', 2, **{'Выполнение': 'EXEC-02', 'Проверки': 'CHECK-02'}))
        self.assertEqual(self.handoff()[0], 0)
        self.append(record('AUDIT', 2, actor='Auditor', **{'Передача': 'HANDOFF-02', 'Закрытые замечания': 'A-01'}))
        self.assertEqual(self.accept('--result', 'accepted')[0], 0)
        self.assertTrue(self.mark('AC-01')); self.assertTrue(self.mark('AC-02'))
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 0)

    def test_manual_marker_tamper_does_not_open_acceptance(self):
        self.setup_card()
        self.path.write_text(self.path.read_text().replace('- [ ] AC-01:', '- [x] AC-01:'))
        before = tree_fingerprint(self.root)
        self.assertIn('AC-01', self.view()['checkbox_mismatches'])
        self.assertEqual(before, tree_fingerprint(self.root))
        self.assertEqual(self.cli('check', '--ready')[0], 1)
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 1)
        self.assertEqual(self.sync()[0], 0)
        self.assertFalse(self.mark('AC-01'))

    def test_stale_inputs_do_not_rebind_existing_check(self):
        self.setup_card(); self.verify(); self.assertEqual(self.sync()[0], 0)
        confirmed_text = self.path.read_text()
        (self.root / 'docs/new-input.txt').write_text('changed')
        self.assertFalse(self.view()['criterion_states']['AC-01']['checked'])
        self.assertEqual(self.cli('check', '--ready')[0], 1)
        self.assertEqual(self.sync()[0], 0); self.assertFalse(self.mark('AC-01'))
        self.assertEqual(cards.normalize_checkboxes(confirmed_text), cards.normalize_checkboxes(self.path.read_text()))
        self.verify()  # New proof alone must not rewrite the historical CHECK binding.
        self.assertEqual(self.sync()[0], 0); self.assertFalse(self.mark('AC-01'))
        self.append(record('CHECK', 2)); self.assertEqual(self.sync()[0], 0)
        self.assertTrue(self.mark('AC-01'))

    def test_snapshot_and_handoff_ignore_only_checkbox_rendering(self):
        self.setup_card(); self.verify(); self.append(record('HANDOFF'))
        self.assertEqual(self.handoff()[0], 0)
        meta = cards.metadata(self.path.read_text())
        self.append(record('AUDIT', actor='Auditor'))
        self.assertEqual(self.accept('--result', 'accepted')[0], 0)
        projection = lifecycle.card_projection(self.path.read_text())
        settings = {'inputs': ['docs']}
        before = playbook.verification.snapshot(self.root, settings['inputs'], playbook.safe_path, playbook.require)
        self.path.write_text(self.path.read_text().replace('- [x] AC-01:', '- [ ] AC-01:'))
        after = playbook.verification.snapshot(self.root, settings['inputs'], playbook.safe_path, playbook.require)
        self.assertEqual(before, after)
        self.assertEqual(self.sync()[0], 0)
        self.assertEqual(projection, lifecycle.card_projection(self.path.read_text()))
        self.assertEqual(meta['handoff'], cards.metadata(self.path.read_text())['handoff'])
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 0)

    def test_sync_gate_and_final_failure_rollback(self):
        self.setup_card(); self.verify()
        before = self.project()
        gate = fops.acquire_gate(self.root, playbook.safe_path, playbook.require, owner='test')
        try:
            self.assertEqual(self.sync()[0], 1)
        finally:
            gate.close()
        self.assertEqual(before, self.project())
        ctx = dict(playbook._lifecycle_ctx(None)); original = ctx['run_operation']
        def fail_final(root, spec):
            spec = dict(spec)
            def fail(_): raise ValueError('injected final failure')
            spec['final_checks'] = [fail]
            return original(root, spec)
        ctx['run_operation'] = fail_final
        with self.assertRaisesRegex(ValueError, 'injected final failure'):
            lifecycle.sync_criteria_command(self.root, 'DEV-1', True, ctx, playbook.safe_path, playbook.require)
        self.assertEqual(before, self.project())
        self.assertIsNone(fops.pending_info(self.root))

    def test_legacy_v2_findings_preserved_on_explicit_sync(self):
        body = BODY + record('EXEC') + record('CHECK') + record('HANDOFF')
        body += record('AUDIT', **{'Результат': 'changes_requested'}) + finding(1).replace('Критерии: AC-01\n', '')
        self.managed_task(meta={**copy.deepcopy(TASK_META), 'card_format_version': 2}, body=body)
        self.path = self.tasks_dir() / 'DEV-1.md'
        history = self.path.read_text().split('## Журнал',1)[1]
        self.assertEqual(self.sync()[0], 0)
        self.assertEqual(history, self.path.read_text().split('## Журнал',1)[1])
        self.assertEqual(self.view()['open_findings'], ['A-01'])
        self.assertTrue(self.view()['entries'][-1]['findings'][0]['criteria_inferred'])

    def test_requirement_or_work_change_without_verify_rejects_check_and_close(self):
        self.setup_card()
        self.verify()
        self.assertEqual(self.sync()[0], 0)
        self.assertTrue(self.mark('AC-01'))
        old_report = cards.metadata(self.path.read_text())['criterion_evidence']['CHECK-01']['report_id']

        # 1. Negative scenario: change requirements + new EXEC-02 + CHECK-02 without verify
        self.path.write_text(self.path.read_text().replace('повтор безопасен.', 'совершенно новое требование.'))
        self.append(record('EXEC', 2))
        self.append(record('CHECK', 2))
        self.assertEqual(self.sync()[0], 0)
        view = self.view()
        self.assertFalse(view['criterion_states']['AC-01']['checked'])
        self.assertEqual(view['criterion_states']['AC-01']['reason'], 'unbound_or_changed_check')
        self.assertNotIn('CHECK-02', cards.metadata(self.path.read_text()).get('criterion_evidence', {}))
        self.assertFalse(self.mark('AC-01'))

        # handoff and close without verify fail
        self.append(record('HANDOFF', 2, **{'Выполнение': 'EXEC-02', 'Проверки': 'CHECK-02'}))
        self.assertEqual(self.handoff()[0], 1)
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 1)

        # 2. Positive scenario: run verify after changes
        self.verify()
        self.assertEqual(self.sync()[0], 0)
        self.assertTrue(self.mark('AC-01'))
        meta = cards.metadata(self.path.read_text())
        new_report = meta['criterion_evidence']['CHECK-02']['report_id']
        self.assertNotEqual(old_report, new_report)

        # handoff, accept and close succeed
        self.assertEqual(self.handoff()[0], 0)
        self.append(record('AUDIT', 2, actor='Auditor', **{'Передача': 'HANDOFF-02'}))
        self.assertEqual(self.accept('--result', 'accepted')[0], 0)
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 0)

    def test_altered_exec_invalidates_existing_binding_and_blocks_close_until_new_verify(self):
        # A-03: изменение проверенной EXEC делает существующую привязку CHECK неактуальной
        self.setup_card()
        self.verify()
        self.assertEqual(self.sync()[0], 0)
        self.append(record('HANDOFF'))
        self.assertEqual(self.handoff()[0], 0)

        # 1. Изменение текста существующей EXEC-01 без повторного verify
        self.path.write_text(self.path.read_text().replace('Добавлен apply.', 'Изменена реализация после проверки.'))
        view = self.view()
        self.assertFalse(view['criterion_states']['AC-01']['checked'])
        self.assertEqual(view['criterion_states']['AC-01']['reason'], 'evidence_stale')

        # Добавление HANDOFF-02 и попытка передачи без нового verify должны отказать
        self.append(record('HANDOFF', 2))
        self.assertEqual(self.handoff()[0], 1)
        self.append(record('AUDIT', actor='Auditor', **{'Передача': 'HANDOFF-02'}))
        self.assertEqual(self.accept('--result', 'accepted')[0], 1)
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 1)

        # 2. Успешный путь: запуск verify + новая запись CHECK-02
        self.verify()
        self.append(record('CHECK', 2))
        self.assertEqual(self.sync()[0], 0)
        view = self.view()
        self.assertTrue(view['criterion_states']['AC-01']['checked'])
        self.assertEqual(view['criterion_states']['AC-01']['check_id'], 'CHECK-02')

        # Старая привязка CHECK-01 не перезаписана, обе привязки сохранены
        meta = cards.metadata(self.path.read_text())
        self.assertIn('CHECK-01', meta['criterion_evidence'])
        self.assertIn('CHECK-02', meta['criterion_evidence'])

        # Свежая передача, приёмка и закрытие проходят успешно
        self.append(record('HANDOFF', 3, **{'Проверки': 'CHECK-02'}))
        self.assertEqual(self.handoff()[0], 0)
        self.append(record('AUDIT', 2, actor='Auditor', **{'Передача': 'HANDOFF-03'}))
        self.assertEqual(self.accept('--result', 'accepted')[0], 0)
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 0)

    def test_altered_earlier_exec_with_subsequent_exec_invalidates_binding_and_blocks_close(self):
        # A-04: изменение ранней EXEC-01 при наличии последующей EXEC-02 делает
        # совокупный execution_state недействительным и блокирует handoff/close до нового verify
        self.setup_card()
        self.append(record('EXEC', 2, **{'Изменения': 'Вторая часть реализации.'}))
        self.append(record('CHECK', 2))
        self.verify()
        self.assertEqual(self.sync()[0], 0)
        self.append(record('HANDOFF', **{'Выполнение': 'EXEC-01, EXEC-02', 'Проверки': 'CHECK-02'}))
        self.assertEqual(self.handoff()[0], 0)

        # 1. Изменение текста ранней EXEC-01 при неизменной EXEC-02
        self.path.write_text(self.path.read_text().replace('Добавлен apply.', 'Изменено раннее проверенное выполнение.'))
        view = self.view()
        self.assertFalse(view['criterion_states']['AC-01']['checked'])
        self.assertEqual(view['criterion_states']['AC-01']['reason'], 'evidence_stale')

        # Передача, приёмка и закрытие блокируются
        self.append(record('HANDOFF', 2, **{'Выполнение': 'EXEC-01, EXEC-02', 'Проверки': 'CHECK-02'}))
        self.assertEqual(self.handoff()[0], 1)
        self.append(record('AUDIT', actor='Auditor', **{'Передача': 'HANDOFF-02'}))
        self.assertEqual(self.accept('--result', 'accepted')[0], 1)
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 1)

        # 2. Успешный путь: повторный verify фиксирует обновлённый совокупный execution_state
        self.verify()
        self.append(record('CHECK', 3))
        self.assertEqual(self.sync()[0], 0)
        view = self.view()
        self.assertTrue(view['criterion_states']['AC-01']['checked'])
        self.assertEqual(view['criterion_states']['AC-01']['check_id'], 'CHECK-03')

        self.append(record('HANDOFF', 3, **{'Выполнение': 'EXEC-01, EXEC-02', 'Проверки': 'CHECK-03'}))
        self.assertEqual(self.handoff()[0], 0)
        self.append(record('AUDIT', 2, actor='Auditor', **{'Передача': 'HANDOFF-03'}))
        self.assertEqual(self.accept('--result', 'accepted')[0], 0)
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 0)

    def test_close_card_with_relative_link_preserves_historical_checkboxes_and_supports_reopen(self):
        # A-05: закрытие карточки с относительными ссылками сохраняет вычисляемые галочки в архиве,
        # а возврат (reopen) сохраняет исторические доказательства без коллизий в каталоге материалов
        self.setup_card()
        self.path.write_text(self.path.read_text().replace(
            'Локальный проект.',
            'Локальный проект. [Config](../../ai-dev-playbook/project.json)'))
        evidence_dir = self.tasks_dir() / 'DEV-1-evidence'
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / 'proof.txt').write_text('evidence proof\n')
        self.assertEqual(self.cli(
            'lifecycle', 'register-material', 'DEV-1', 'docs/tasks/DEV-1-evidence',
            '--id', 'dev1-evidence', '--role', 'report', '--immutable', '--required', '--apply')[0], 0)

        # 1. verify -> handoff -> accept -> close --dry-run -> close
        self.verify()
        self.append(record('HANDOFF'))
        self.assertEqual(self.handoff()[0], 0)
        self.append(record('AUDIT', actor='Auditor'))
        self.assertEqual(self.accept('--result', 'accepted')[0], 0)
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 0)
        self.assertEqual(self.cli('close', 'DEV-1')[0], 0)

        # 2. check --ready и card show в архиве подтверждают исторические галочки
        self.assertEqual(self.cli('check', '--ready')[0], 0)
        view = self.view()
        self.assertTrue(view['criterion_states']['AC-01']['checked'])
        self.assertEqual(view['criterion_states']['AC-01']['reason'], 'historical')

        # 3. reopen: возврат карточки и каталога доказательств в работу
        req_path = self.root / 'work/reopen-req.json'
        req_path.parent.mkdir(parents=True, exist_ok=True)
        req_path.write_text(json.dumps({
            'schema_version': 1,
            'actor': {'name': 'cli', 'role': 'cli'},
            'basis': 'reopen test',
            'scope': {'tasks': ['DEV-1']},
            'actions': [{'action': 'reopen', 'task': 'DEV-1', 'status': 'in_progress', 'reason': 'audit reopen'}]
        }))
        plan_code, plan_out, plan_err = self.cli('lifecycle', 'plan', 'work/reopen-req.json', '--save')
        self.assertEqual(plan_code, 0, plan_err)
        plan_id = plan_out.split()[1]
        apply_code, apply_out, apply_err = self.cli('lifecycle', 'apply', f'work/lifecycle/plans/{plan_id}.json')
        self.assertEqual(apply_code, 0, apply_err)

        # 4. Проверка сохранности исторических доказательств и сброса галочек в in_progress
        reopened_card = self.tasks_dir() / 'DEV-1.md'
        self.assertTrue(reopened_card.is_file())
        self.assertIn('- [ ] AC-01:', reopened_card.read_text())
        self.assertTrue((self.tasks_dir() / 'DEV-1-evidence/proof.txt').is_file())
        self.assertEqual((self.tasks_dir() / 'DEV-1-evidence/proof.txt').read_text(), 'evidence proof\n')
        self.assertTrue(any((self.tasks_dir() / 'DEV-1-evidence').glob('work/verification/DEV-1/*/report.json')))

        # 5. Новый цикл verify -> sync -> handoff проходит успешно без пересечения материалов
        self.append(record('EXEC', number=2))
        self.append(record('CHECK', number=2))
        self.append(record('HANDOFF', number=2, actor='Devin', **{
            'Выполнение': 'EXEC-01, EXEC-02', 'Проверки': 'CHECK-01, CHECK-02'}))
        self.verify()
        sync_code, sync_out, sync_err = self.cli('card', 'sync', 'DEV-1', '--apply')
        self.assertEqual(sync_code, 0, sync_err)
        handoff_code, handoff_out, handoff_err = self.handoff()
        self.assertEqual(handoff_code, 0, handoff_err)
        self.assertEqual(self.cli('check', '--ready')[0], 0)

    def test_close_card_with_relative_links_in_journal_entries_preserves_historical_bindings_and_supports_second_close(self):
        # A-06: относительные ссылки внутри EXEC/CHECK/HANDOFF/AUDIT и доказательств
        # переписываются при close, но исторические привязки и вычисляемые галочки
        # остаются валидными в архиве; повторный цикл reopen -> verify -> handoff -> accept -> close
        # выполняется штатно, а immutable-доказательства сохраняются побайтно неизменными
        import hashlib
        self.setup_card()
        evidence_dir = self.tasks_dir() / 'DEV-1-evidence'
        evidence_dir.mkdir(parents=True, exist_ok=True)
        proof_file = evidence_dir / 'proof.txt'
        proof_file.write_text('evidence proof initial bytes\n')
        proof_sha_initial = hashlib.sha256(proof_file.read_bytes()).hexdigest()

        self.assertEqual(self.cli(
            'lifecycle', 'register-material', 'DEV-1', 'docs/tasks/DEV-1-evidence',
            '--id', 'dev1-evidence', '--role', 'report', '--immutable', '--required', '--apply')[0], 0)

        # 1. Заменяем содержимое журнала ссылками на Config (внешний) и proof.txt (в evidence)
        text = self.path.read_text()
        text = text.replace(
            'Изменения: Базовая реализация.',
            'Изменения: Базовая реализация [Config](../../ai-dev-playbook/project.json).')
        text = text.replace(
            'Доказательства: Локальная CLI-проба.',
            'Доказательства: [Config](../../ai-dev-playbook/project.json) и [Proof](DEV-1-evidence/proof.txt)')
        self.path.write_text(text)

        # 2. verify -> handoff -> accept -> close
        self.verify()
        self.append(record('HANDOFF', **{
            'Фокус аудита': 'Проверить [Config](../../ai-dev-playbook/project.json) и [Proof](DEV-1-evidence/proof.txt)'}))
        self.assertEqual(self.handoff()[0], 0)
        self.append(record('AUDIT', actor='Auditor', **{
            'Доказательства': 'Проверено [Config](../../ai-dev-playbook/project.json) и [Proof](DEV-1-evidence/proof.txt)'}))
        self.assertEqual(self.accept('--result', 'accepted')[0], 0)
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 0)
        self.assertEqual(self.cli('close', 'DEV-1')[0], 0)

        # 3. Архив: check --ready успешен, галочки подтверждены как historical
        self.assertEqual(self.cli('check', '--ready')[0], 0)
        view = self.view()
        self.assertTrue(view['criterion_states']['AC-01']['checked'])
        self.assertEqual(view['criterion_states']['AC-01']['reason'], 'historical')
        archived_proof = self.archive_dir() / 'DEV-1-evidence/proof.txt'
        self.assertTrue(archived_proof.is_file())
        self.assertEqual(hashlib.sha256(archived_proof.read_bytes()).hexdigest(), proof_sha_initial)

        # 4. reopen: карточка и каталог возвращаются в работу
        req_path = self.root / 'work/reopen-req.json'
        req_path.parent.mkdir(parents=True, exist_ok=True)
        req_path.write_text(json.dumps({
            'schema_version': 1,
            'actor': {'name': 'cli', 'role': 'cli'},
            'basis': 'reopen test for A-06',
            'scope': {'tasks': ['DEV-1']},
            'actions': [{'action': 'reopen', 'task': 'DEV-1', 'status': 'in_progress', 'reason': 'second pass'}]
        }))
        plan_code, plan_out, plan_err = self.cli('lifecycle', 'plan', 'work/reopen-req.json', '--save')
        self.assertEqual(plan_code, 0, plan_err)
        plan_id = plan_out.split()[1]
        apply_code, apply_out, apply_err = self.cli('lifecycle', 'apply', f'work/lifecycle/plans/{plan_id}.json')
        self.assertEqual(apply_code, 0, apply_err)

        reopened_proof = self.tasks_dir() / 'DEV-1-evidence/proof.txt'
        self.assertTrue(reopened_proof.is_file())
        self.assertEqual(hashlib.sha256(reopened_proof.read_bytes()).hexdigest(), proof_sha_initial)

        # 5. Второй полный цикл: EXEC-02, CHECK-02, HANDOFF-02, verify, sync, accept, close
        self.append(record('EXEC', number=2, **{
            'Изменения': 'Вторая итерация [Config](../../ai-dev-playbook/project.json).'}))
        self.append(record('CHECK', number=2, **{
            'Доказательства': '[Config](../../ai-dev-playbook/project.json) и [Proof](DEV-1-evidence/proof.txt)'}))
        self.append(record('HANDOFF', number=2, actor='Devin', **{
            'Выполнение': 'EXEC-01, EXEC-02', 'Проверки': 'CHECK-01, CHECK-02',
            'Фокус аудита': 'Второй аудит [Config](../../ai-dev-playbook/project.json)'}))
        self.verify()
        sync_code, sync_out, sync_err = self.cli('card', 'sync', 'DEV-1', '--apply')
        self.assertEqual(sync_code, 0, sync_err)
        self.assertEqual(self.handoff()[0], 0)
        # Переводим карточку в in_review для аудита и закрытия
        reopened_card = self.tasks_dir() / 'DEV-1.md'
        reopened_card.write_text(reopened_card.read_text().replace('Status: in_progress', 'Status: in_review'))
        self.append(record('AUDIT', number=2, actor='Auditor', **{
            'Передача': 'HANDOFF-02',
            'Доказательства': 'Второй аудит [Proof](DEV-1-evidence/proof.txt)'}))
        self.assertEqual(self.accept('--result', 'accepted')[0], 0)
        close_code, close_out, close_err = self.cli('close', 'DEV-1')
        self.assertEqual(close_code, 0, f"STDOUT:\n{close_out}\nSTDERR:\n{close_err}")

        # 6. Второе закрытие: check --ready успешен, proof.txt побайтно неизменен
        self.assertEqual(self.cli('check', '--ready')[0], 0)
        view2 = self.view()
        self.assertTrue(view2['criterion_states']['AC-01']['checked'])
        self.assertEqual(view2['criterion_states']['AC-01']['reason'], 'historical')
        archived_proof2 = self.archive_dir() / 'DEV-1-evidence/proof.txt'
        self.assertEqual(hashlib.sha256(archived_proof2.read_bytes()).hexdigest(), proof_sha_initial)

        # 7. Защита от произвольного изменения истории в архиве:
        # если кто-то подменил текст CHECK в архиве, check --ready бракует карточку
        archived_card = self.archive_dir() / 'DEV-1.md'
        card_content = archived_card.read_text()
        self.assertIn('Факт: Повтор успешен.', card_content)
        tampered_content = card_content.replace('Факт: Повтор успешен.', 'Факт: Повтор провален.')
        archived_card.write_text(tampered_content)
        tampered_check = self.cli('check', '--ready')
        self.assertEqual(tampered_check[0], 1)
        self.assertIn('чекбоксы не соответствуют журналу/свежести', tampered_check[2])
        # Возвращаем исходный текст архива
        archived_card.write_text(card_content)
        self.assertEqual(self.cli('check', '--ready')[0], 0)




