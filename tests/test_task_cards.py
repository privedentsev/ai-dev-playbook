"""Behavioral contract for the client format and lifecycle bindings."""
import copy
import json
import unittest

from test_close_lifecycle import CloseLifecycleCase, TASK_META, CARD
from test_documents import tree_fingerprint
from test_playbook import playbook

cards = playbook.task_cards
STAMP = '2026-10-24T12:00:00+03:00'
BODY = '''
## Задача

### Цель и границы
Безопасно применить изменения.
### Контекст
Локальный проект.
### Критерии приёмки
- AC-01: повтор безопасен.
### План
1. Выполнить и проверить.
### Ресурсы
Python.

## Журнал
'''


def record(prefix, number=1, title=None, actor='Devin', **values):
    titles = {'EXEC': 'Выполнение', 'CHECK': 'Проверка', 'HANDOFF': 'Передача', 'AUDIT': 'Аудит'}
    base = {'Время': STAMP, 'Исполнитель': actor, 'Модель': 'SWE-2 High'}
    defaults = {
        'EXEC': {'Изменения': 'Добавлен apply.', 'Файлы': '- example.py'},
        'CHECK': {'Результат': 'passed', 'Критерии': 'AC-01', 'Сценарий': 'Повтор apply.',
                  'Среда': 'Python; временный проект.', 'Факт': 'Повтор успешен.',
                  'Доказательства': 'Локальная CLI-проба.', 'Ограничения': 'Нет.'},
        'HANDOFF': {'Выполнение': 'EXEC-01', 'Проверки': 'CHECK-01',
                    'Состояние': 'Dirty tree.', 'Фокус аудита': 'Повтор apply.'},
        'AUDIT': {'Передача': 'HANDOFF-01', 'Результат': 'accepted', 'Критерии': 'AC-01',
                  'Независимо проверено': 'Повтор apply.', 'Доказательства': 'CLI-проба.',
                  'Ограничения': 'Нет.'},
    }
    base.update(defaults[prefix]); base.update(values)
    return '\n### %s-%02d — %s\n\n%s\n' % (
        prefix, number, title or titles[prefix], '\n'.join(k + ': ' + v for k, v in base.items()))


def finding(number):
    return f'''
#### A-{number:02d} — Замечание
Серьёзность: P2
Критерии: AC-01
Проблема: повтор не работает.
Воспроизведение: дважды apply.
Факт: ошибка.
Влияние: повтор запрещён.
Критерий исправления: повтор успешен.
'''


def text(body=BODY):
    return CARD.format(ident='DEV-1', status='in_progress', deps='-', closed='-') + '\n<!-- playbook-meta:start -->\n```json\n' + json.dumps(
        {'schema_version': 1, 'card_format_version': 2}) + '\n```\n<!-- playbook-meta:end -->\n' + body


class ParserTest(unittest.TestCase):
    """Парсер карточек: шаблон, маркеры, grammar, заголовки и
    ссылки."""
    def test_default_template_is_supported(self):
        """Шаблон task.md по умолчанию поддерживается парсером"""
        from pathlib import Path
        template = Path(playbook.__file__).parents[1] / 'templates/task.md'
        parsed = cards.parse(template.read_text())
        self.assertEqual(parsed['card_format_version'], 2)
        self.assertEqual(parsed['entries'], [])

    def test_two_fixes_three_audits(self):
        """Две правки и три аудита парсятся корректно"""
        body = BODY
        for i in range(1, 4):
            body += record('EXEC', i, title='Исправление' if i > 1 else None,
                           **({'Замечания': f'A-{i-1:02d}'} if i > 1 else {}))
            body += record('CHECK', i)
            body += record('HANDOFF', i, **{'Выполнение': f'EXEC-{i:02d}', 'Проверки': f'CHECK-{i:02d}'})
            body += record('AUDIT', i, **{'Передача': f'HANDOFF-{i:02d}',
                            'Результат': 'accepted' if i == 3 else 'changes_requested',
                            **({'Закрытые замечания': f'A-{i-1:02d}'} if i > 1 else {})})
            if i < 3:
                body += finding(i)
        parsed = cards.parse(text(body))
        self.assertEqual(len(parsed['entries']), 12)
        self.assertEqual(parsed['latest_audit'], 'AUDIT-03')
        self.assertEqual(parsed['current_verdict'], 'accepted')
        self.assertEqual(parsed['header']['id'], 'DEV-1')
        self.assertEqual(parsed['open_findings'], [])

    def test_unsupported_versions_and_bad_markers(self):
        """Неподдерживаемые версии и битые маркеры отклоняются"""
        for version in (1, 3, 99):
            result = cards.parse(text().replace('"card_format_version": 2', f'"card_format_version": {version}'), 'old.md')
            self.assertEqual(result['status'], 'unsupported')
            self.assertEqual(result['markdown_path'], 'old.md')
            self.assertNotIn('entries', result)
        self.assertEqual(cards.parse('# old')['card_format_version'], 1)
        for value in ('true', 'null', '"2"', '0'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cards.parse(text().replace('"card_format_version": 2', '"card_format_version": ' + value))

    def test_fenced_examples_and_duplicate_fields(self):
        """Примеры в fence и дубли полей игнорируются"""
        body = BODY + record('EXEC', **{'Изменения': '\n```markdown\n### EXEC-99 — Выполнение\nВремя: fake\n```'})
        self.assertEqual(len(cards.parse(text(body))['entries']), 1)
        with self.assertRaisesRegex(ValueError, 'повтор поля'):
            cards.parse(text(body + 'Файлы: twice\n'))

    def test_markdown_colons_urls_and_paragraphs_are_not_fields(self):
        """Markdown-двоеточия, URL и абзацы не поля"""
        value = '\nПервый абзац.\n\nВторой абзац.\n- CLI: добавлен preview.\n- [Отчёт](https://example.test/proof)\nhttps://example.test/proof'
        parsed = cards.parse(text(BODY + record('EXEC', **{'Изменения': value})))
        self.assertEqual(parsed['entries'][0]['fields']['Изменения'], value.strip())

    def test_negative_grammar_and_links(self):
        """Отрицательная грамматика и ссылки записей отклоняются"""
        good = BODY + record('EXEC') + record('CHECK') + record('HANDOFF')
        invalid = [good + record('EXEC'), good.replace('AC-01\n', 'AC-99\n'),
                   good.replace('Результат: passed', 'Результат: green'),
                   good.replace('Время: ' + STAMP, 'Время: 2026-10-24'),
                   good.replace('Среда: Python; временный проект.\n', ''),
                   good.replace('Проверки: CHECK-01', 'Проверки: EXEC-01'),
                   good.replace('Результат: passed', 'Результат: failed'),
                   good.replace('Состояние: Dirty tree.', 'Unknown: data'),
                   good + '\n## New\ntext\n',
                   good + record('AUDIT', **{'Результат': 'changes_requested'}),
                   good + record('AUDIT') + finding(1)]
        with self.assertRaisesRegex(ValueError, 'Status'):
            cards.parse(text(good).replace('Status: in_progress', 'Status: invalid'))
        for body in invalid:
            with self.subTest(body=body[-100:]), self.assertRaises(ValueError):
                cards.parse(text(body))

    def test_unknown_header_field_and_extra_task_heading_rejected(self):
        """Неизвестное поле шапки и лишний заголовок отклонены"""
        with self.assertRaisesRegex(ValueError, 'неизвестное поле шапки Unknown'):
            cards.parse(text().replace('Closed: -', 'Closed: -\nUnknown: ignored'))
        with self.assertRaisesRegex(ValueError, 'повтор поля шапки Closed'):
            cards.parse(text().replace('Closed: -', 'Closed: -\nClosed: -'))
        with self.assertRaisesRegex(ValueError, 'недопустимый заголовок в разделе задачи'):
            cards.parse(text(BODY.replace('Локальный проект.', 'Локальный проект.\n#### Лишний заголовок\nТекст.')))


    def test_projection_and_journal_binding(self):
        """Проекция карточки и привязка журнала"""
        base = text(BODY + record('EXEC') + record('CHECK') + record('HANDOFF'))
        entry = cards.parse(base)['entries'][-1]
        meta = {'handoff': {'entry_id': entry['id'], 'journal_sha256': entry['journal_sha256']}}
        audit = base + record('AUDIT')
        cards.receipt(audit, meta)
        self.assertEqual(playbook.lifecycle.card_projection(base), playbook.lifecycle.card_projection(audit))
        for altered in (audit.replace('Добавлен apply.', 'Удалён apply.'), audit + record('CHECK', 2)):
            with self.assertRaises(ValueError): cards.receipt(altered, meta)
        self.assertNotEqual(playbook.lifecycle.card_projection(base),
                            playbook.lifecycle.card_projection(base.replace('повтор безопасен.', 'другие требования.')))


class CardLifecycleTest(CloseLifecycleCase):
    """CLI жизненного цикла карточек: show/handoff/accept/close и
    tamper-защита."""
    def setup_card(self):
        self.body = BODY + record('EXEC') + record('CHECK') + record('HANDOFF')
        self.managed_task(meta={**copy.deepcopy(TASK_META), 'card_format_version': 2}, body=self.body)
        self.path = self.tasks_dir() / 'DEV-1.md'

    def append(self, value):
        with self.path.open('a') as stream: stream.write(value)

    def test_cli_show_legacy_and_future(self):
        """card show работает для legacy и future версий"""
        self.managed_task()
        for version in (None, 9):
            if version:
                self.managed_task(meta={**TASK_META, 'card_format_version': version})
            result = self.cli('card', 'show', 'DEV-1', '--json')
            self.assertEqual(result[0], 0, result)
            self.assertEqual(json.loads(result[1])['status'], 'unsupported')

    def test_handoff_accept_close_and_tamper(self):
        """handoff/accept/close и tamper-защита карточки"""
        self.setup_card()
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        # A textual verdict alone never opens the gate.
        self.append(record('AUDIT', actor='Auditor'))
        result = self.cli('close', 'DEV-1', '--dry-run')
        self.assertEqual(result[0], 1)
        result = self.accept('--result', 'accepted')
        self.assertEqual(result[0], 0, result)
        before = tree_fingerprint(self.root)
        result = self.cli('card', 'show', 'DEV-1', '--json')
        self.assertEqual(before, tree_fingerprint(self.root))
        self.assertEqual(json.loads(result[1])['acceptance_state'], 'current', result)
        original = self.path.read_text()
        for before, after in [('Добавлен apply.', 'Изменён apply.'),
                              ('Независимо проверено: Повтор apply.', 'Независимо проверено: другое.'),
                              ('повтор безопасен.', 'новые требования.')]:
            self.path.write_text(original.replace(before, after))
            result = self.cli('close', 'DEV-1', '--dry-run')
            self.assertEqual(result[0], 1, result)
            self.path.write_text(original)
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 0)
        result = self.cli('close', 'DEV-1')
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.cli('check', '--ready')[0], 0)
        result = self.cli('card', 'show', 'DEV-1', '--json')
        self.assertEqual(json.loads(result[1])['acceptance_state'], 'historical')
        from test_lifecycle import task_request
        request = task_request('DEV-1', 'reopen', status='in_progress', reason='Регрессия')
        result = self.apply_request(request)
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.cli('check', '--ready')[0], 0)
        result = self.cli('card', 'show', 'DEV-1', '--json')
        self.assertEqual(json.loads(result[1])['acceptance_state'], 'not_current')
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 1)

    def test_return_fix_new_handoff_requires_new_audit(self):
        """Возврат на доработку требует новый handoff и аудит"""
        self.setup_card(); self.verify()
        self.assertEqual(self.handoff()[0], 0)
        self.append(record('AUDIT', actor='Auditor', **{'Результат': 'changes_requested'}) + finding(1))
        self.assertEqual(self.accept('--result', 'accepted')[0], 1)
        self.append(record('EXEC', 2, title='Исправление', **{'Замечания': 'A-01'}) + record('CHECK', 2)
                    + record('HANDOFF', 2, **{'Выполнение': 'EXEC-02', 'Проверки': 'CHECK-02'}))
        self.verify(); self.assertEqual(self.handoff()[0], 0)
        self.assertEqual(self.accept('--result', 'accepted')[0], 1)
        self.append(record('AUDIT', 2, actor='Auditor', **{'Передача': 'HANDOFF-02', 'Закрытые замечания': 'A-01'}))
        self.assertEqual(self.accept('--result', 'accepted')[0], 0)
        self.assertEqual(self.cli('close', 'DEV-1', '--dry-run')[0], 0)
