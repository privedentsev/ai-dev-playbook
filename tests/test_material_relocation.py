"""PB-072: early archive-destination collisions and explicit material relocation."""
import hashlib
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from test_close_lifecycle import CloseLifecycleCase, fops, lifecycle, playbook
from test_documents import tree_fingerprint

documents = playbook.documents
document_links = playbook.document_links


class CollisionCase(CloseLifecycleCase):
    """Runner report + manual copy registered under two material IDs (PB-060)."""

    def runner_dir(self, ident='DEV-1'):
        dirs = [p for p in (self.root / 'work/verification' / ident).iterdir()
                if p.is_dir()]
        self.assertEqual(len(dirs), 1)
        return dirs[0]

    def register(self, source, ident, *flags, task='DEV-1'):
        return self.cli('lifecycle', 'register-material', task, source,
                        '--id', ident, '--role', 'report', '--immutable',
                        '--required', *flags)

    def relocate(self, ident, to, *flags, task='DEV-1'):
        return self.cli('lifecycle', 'relocate-material', task, '--id', ident,
                        '--to', to, '--reason', 'разнести коллизию',
                        '--actor', 'Devin', '--model', 'SWE-2 High', *flags)

    def copy_report(self):
        """Manual copy of the runner report inside owner-evidence (as PB-060)."""
        run_dir = self.runner_dir()
        report = run_dir / 'report.json'
        dest_dir = (self.tasks_dir() / 'DEV-1-evidence' / 'work' /
                    'verification' / 'DEV-1' / run_dir.name)
        dest_dir.mkdir(parents=True)
        shutil.copy(report, dest_dir / 'report.json')
        return run_dir, dest_dir

    def inject_materials(self, materials):
        """Colliding state from before the early control existed."""
        path = self.tasks_dir() / 'DEV-1.md'
        meta = self.meta_of(path)
        meta['materials'] = materials
        path.write_text(lifecycle._splice_meta(
            path.read_text(encoding='utf-8'), meta), encoding='utf-8')

    def collision_state(self, same_bytes=True):
        self.managed_task()
        self.verify()
        run_dir, dest_dir = self.copy_report()
        if not same_bytes:
            (dest_dir / 'report.json').write_text('{"changed": true}')
        run_rel = f'work/verification/DEV-1/{run_dir.name}'
        copy_rel = str(dest_dir.relative_to(self.root))
        self.inject_materials([
            {'id': 'manual-copy', 'role': 'report',
             'source': copy_rel + '/report.json',
             'storage': 'local', 'required': True, 'immutable': True,
             'used_by': ['DEV-1']},
            {'id': f'verify-{run_dir.name}', 'role': 'report',
             'source': run_rel, 'storage': 'local', 'required': True,
             'immutable': True, 'used_by': ['DEV-1']}])
        return run_dir, copy_rel

    def counter(self):
        project = self.root / 'ai-dev-playbook/project.json'
        data = json.loads(project.read_text())
        data['verification']['checks'] = [{'id': 'count', 'argv': [
            sys.executable, '-c',
            "from pathlib import Path; p=Path('work/count'); "
            "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"],
            'timeout_seconds': 30}]
        project.write_text(json.dumps(data))

    def count(self):
        path = self.root / 'work/count'
        return int(path.read_text()) if path.exists() else 0


class EarlyDestinationCheckTest(CollisionCase):
    """Ранние проверки коллизий архива при регистрации, handoff,
    preflight и verify."""
    def test_register_rejects_same_archive_destination_equal_bytes(self):
        """Регистрация отклоняет то же архивное назначение при равных байтах"""
        self.managed_task()
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        _, dest_dir = self.copy_report()
        rel = str(dest_dir.relative_to(self.root))
        for flags in ([], ['--apply']):
            before = self.project_fingerprint()
            code, out, err = self.register(rel, 'manual-copy', *flags)
            self.assertEqual(code, 1)
            self.assertIn('manual-copy', err)
            self.assertIn('назначены', err)
            self.assertIn('relocate-material', err)
            self.assertEqual(before, self.project_fingerprint())

    def test_register_rejects_same_destination_different_bytes(self):
        """Регистрация отклоняет то же назначение при разных байтах"""
        self.managed_task()
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        _, dest_dir = self.copy_report()
        (dest_dir / 'report.json').write_text('{"different": true}')
        rel = str(dest_dir.relative_to(self.root))
        result = self.register(rel, 'manual-copy', '--apply')
        self.assertEqual(result[0], 1, result)
        self.assertIn('назначены', result[2])

    def test_handoff_rejects_when_auto_registration_collides(self):
        """Handoff отклоняет коллизию авто-регистрации"""
        self.managed_task()
        self.verify()
        _, dest_dir = self.copy_report()
        result = self.register(str(dest_dir.relative_to(self.root)),
                               'manual-copy', '--apply')
        self.assertEqual(result[0], 0, result)
        meta_before = self.meta_of(self.tasks_dir() / 'DEV-1.md')
        before = self.project_fingerprint()
        code, _, err = self.handoff()
        self.assertEqual(code, 1)
        self.assertIn('назначены', err)
        self.assertIn('manual-copy', err)
        self.assertEqual(before, self.project_fingerprint())
        self.assertEqual(meta_before,
                         self.meta_of(self.tasks_dir() / 'DEV-1.md'))

    def test_preflight_and_verify_stop_before_subprocess(self):
        """Preflight и verify останавливаются до subprocess при коллизии"""
        run_dir, copy_rel = self.collision_state()
        self.counter()
        for args in (('verify', 'DEV-1', '--preflight'), ('verify', 'DEV-1')):
            before = self.project_fingerprint()
            code, out, err = self.cli(*args)
            self.assertEqual(code, 1, args)
            self.assertIn('назначены', err)
            self.assertIn('manual-copy', err)
            self.assertIn(f'verify-{run_dir.name}', err)
            self.assertIn('report.json', err)
            self.assertEqual(before, self.project_fingerprint())
        self.assertEqual(self.count(), 0)
        self.assertIsNone(fops.pending_info(self.root))

    def test_same_bytes_different_destinations_pass(self):
        """Равные байты в разных назначениях проходят"""
        self.managed_task()
        self.verify()
        report = self.runner_dir() / 'report.json'
        first = self.tasks_dir() / 'DEV-1-evidence' / 'manual' / 'copy.json'
        first.parent.mkdir(parents=True)
        shutil.copy(report, first)
        result = self.register('docs/tasks/DEV-1-evidence/manual/copy.json',
                               'manual-copy', '--apply')
        self.assertEqual(result[0], 0, result)
        self.assertEqual(self.handoff()[0], 0)

    def test_nested_directory_collision_and_legit_overlap(self):
        """Вложенная коллизия каталогов и легитимное пересечение"""
        self.managed_task()
        self.verify()
        _, dest_dir = self.copy_report()
        evidence = str((self.tasks_dir() / 'DEV-1-evidence')
                       .relative_to(self.root))
        # Весь evidence-каталог как материал: вложенный report.json попадает
        # в то же назначение, что и runner-каталог после handoff.
        result = self.register(evidence, 'whole-evidence', '--apply')
        self.assertEqual(result[0], 0, result)
        code, _, err = self.handoff()
        self.assertEqual(code, 1)
        self.assertIn('назначены', err)
        # Законное повторное представление одного источника (каталог и файл
        # внутри него) не даёт ложной коллизии.
        self.managed_task('DEV-2', status='in_progress')
        dir2 = self.tasks_dir() / 'DEV-2-evidence'
        (dir2 / 'sub').mkdir(parents=True)
        (dir2 / 'sub' / 'note.txt').write_text('n')
        path2 = self.tasks_dir() / 'DEV-2.md'
        meta2 = self.meta_of(path2)
        meta2['materials'] = [
            {'id': 'ev', 'role': 'other',
             'source': 'docs/tasks/DEV-2-evidence', 'storage': 'local',
             'required': True, 'immutable': True, 'used_by': ['DEV-2']},
            {'id': 'note', 'role': 'other',
             'source': 'docs/tasks/DEV-2-evidence/sub/note.txt',
             'storage': 'local', 'required': True, 'immutable': True,
             'used_by': ['DEV-2']}]
        path2.write_text(lifecycle._splice_meta(
            path2.read_text(encoding='utf-8'), meta2), encoding='utf-8')
        result = self.cli('verify', 'DEV-2', '--preflight')
        self.assertEqual(result[0], 0, result[1] + result[2])
        # Занятое архивное назначение отклоняется (материал вне
        # evidence-каталога: архивный evidence сам по себе был бы ошибкой
        # противоположной стороны).
        (self.tasks_dir() / 'loose.txt').write_text('y')
        occupied = self.archive_dir() / 'loose.txt'
        occupied.parent.mkdir(parents=True, exist_ok=True)
        occupied.write_text('x')
        meta2['materials'].append(
            {'id': 'man', 'role': 'other', 'source': 'docs/tasks/loose.txt',
             'storage': 'local', 'required': True, 'immutable': True,
             'used_by': ['DEV-2']})
        path2.write_text(lifecycle._splice_meta(
            path2.read_text(encoding='utf-8'), meta2), encoding='utf-8')
        result = self.cli('verify', 'DEV-2', '--preflight')
        self.assertEqual(result[0], 1, result)
        self.assertIn('Назначение занято', result[2])

    def test_prepare_audit_and_foreign_owner(self):
        """Prepare audit и чужой владелец при коллизиях"""
        self.collision_state()
        # Чужая коллизия не блокирует допустимые операции DEV-2.
        self.managed_task('DEV-2', status='in_progress')
        result = self.cli('lifecycle', 'prepare-audit', 'DEV-2', '--id',
                          'audit-01')
        self.assertEqual(result[0], 0, result)
        # Собственная коллизия DEV-1 отклоняет prepare-audit рано.
        before = self.project_fingerprint()
        result = self.cli('lifecycle', 'prepare-audit', 'DEV-1', '--id',
                          'audit-01')
        self.assertEqual(result[0], 1, result)
        self.assertIn('назначены', result[2])
        self.assertEqual(before, self.project_fingerprint())


class AncestorDestinationTest(CollisionCase):
    """A-01: файл, ставший родителем чужого архивного назначения."""

    def ancestor_pair(self):
        self.managed_task()
        (self.tasks_dir() / 'DEV-1-evidence').mkdir(parents=True)
        (self.tasks_dir() / 'DEV-1-evidence' / 'docs').write_text('file')
        (self.root / 'docs' / 'misc').mkdir(parents=True)
        (self.root / 'docs' / 'misc' / 'note.txt').write_text('note')

    def test_file_parent_destination_rejected_both_orders(self):
        # Первая очередность: ancestor, затем child.
        """A-01: файл-родитель чужого назначения отклонён в обоих порядках"""
        self.ancestor_pair()
        self.assertEqual(
            self.register('docs/tasks/DEV-1-evidence/docs', 'ancestor',
                          '--apply')[0], 0)
        result = self.register('docs/misc/note.txt', 'child', '--apply')
        self.assertEqual(result[0], 1, result)
        self.assertIn('родителем', result[2])
        self.assertIn('ancestor', result[2])
        self.assertIn('child', result[2])
        self.assertIn('relocate-material', result[2])
        # Вторая очередность: child, затем ancestor.
        self.managed_task('DEV-2', status='in_progress')
        (self.tasks_dir() / 'DEV-2-evidence').mkdir(parents=True)
        (self.tasks_dir() / 'DEV-2-evidence' / 'docs').write_text('f2')
        (self.root / 'docs' / 'misc2').mkdir(parents=True)
        (self.root / 'docs' / 'misc2' / 'n.txt').write_text('n')
        self.assertEqual(
            self.register('docs/misc2/n.txt', 'child', '--apply',
                          task='DEV-2')[0], 0)
        result = self.register('docs/tasks/DEV-2-evidence/docs',
                               'ancestor', '--apply', task='DEV-2')
        self.assertEqual(result[0], 1, result)
        self.assertIn('родителем', result[2])
        # Унаследованное состояние: preflight отклоняет до subprocess.
        path = self.tasks_dir() / 'DEV-1.md'
        meta = self.meta_of(path)
        meta['materials'].append({'id': 'child', 'role': 'other',
                                  'source': 'docs/misc/note.txt',
                                  'storage': 'local', 'required': True,
                                  'immutable': True, 'used_by': ['DEV-1']})
        path.write_text(lifecycle._splice_meta(
            path.read_text(encoding='utf-8'), meta), encoding='utf-8')
        self.counter()
        result = self.cli('verify', 'DEV-1', '--preflight')
        self.assertEqual(result[0], 1, result)
        self.assertIn('родителем', result[2])
        self.assertEqual(self.count(), 0)

    def test_directory_parent_destination_allowed(self):
        # Каталог-предок законен: внешний файл может лечь внутрь его
        # архивной области.
        """A-01: каталог-родитель назначения допустим"""
        self.managed_task()
        evidence = self.tasks_dir() / 'DEV-1-evidence'
        evidence.mkdir(parents=True)
        (self.root / 'docs' / 'misc').mkdir(parents=True)
        (self.root / 'docs' / 'misc' / 'note.txt').write_text('note')
        self.assertEqual(self.register('docs/tasks/DEV-1-evidence',
                                       'ev', '--apply')[0], 0)
        self.assertEqual(self.register('docs/misc/note.txt', 'note',
                                       '--apply')[0], 0)
        self.verify()
        self.assertEqual(self.handoff()[0], 0)


class OccupiedParentDestinationTest(CollisionCase):
    """A-05: реальный файл на месте родительского каталога назначения."""

    def test_file_at_destination_parent_rejected_early(self):
        """A-05: файл на месте родительского каталога отклонён рано"""
        self.managed_task()
        (self.tasks_dir() / 'loose').mkdir(parents=True)
        (self.tasks_dir() / 'loose' / 'note.txt').write_text('n')
        occupied = self.archive_dir() / 'loose'
        occupied.parent.mkdir(parents=True, exist_ok=True)
        occupied.write_text('existing file')
        for flags in ([], ['--apply']):
            before = self.project_fingerprint()
            result = self.register('docs/tasks/loose/note.txt', 'note',
                                   *flags)
            self.assertEqual(result[0], 1, result)
            self.assertIn('родительский путь', result[2])
            self.assertIn('loose', result[2])
            self.assertEqual(before, self.project_fingerprint())
        # Унаследованное состояние: preflight/verify без subprocess.
        path = self.tasks_dir() / 'DEV-1.md'
        meta = self.meta_of(path)
        meta['materials'] = [{'id': 'note', 'role': 'other',
                              'source': 'docs/tasks/loose/note.txt',
                              'storage': 'local', 'required': True,
                              'immutable': True, 'used_by': ['DEV-1']}]
        path.write_text(lifecycle._splice_meta(
            path.read_text(encoding='utf-8'), meta), encoding='utf-8')
        self.counter()
        for args in (('verify', 'DEV-1', '--preflight'),
                     ('verify', 'DEV-1')):
            result = self.cli(*args)
            self.assertEqual(result[0], 1, args)
            self.assertIn('родительский путь', result[2])
        self.assertEqual(self.count(), 0)

    def test_occupied_parent_via_installed_cli(self):
        # A-05: сценарий аудита через установленный playbook.py.
        """A-05: занятый родитель через установленный CLI"""
        self.managed_task()
        (self.tasks_dir() / 'loose').mkdir(parents=True)
        (self.tasks_dir() / 'loose' / 'note.txt').write_text('n')
        occupied = self.archive_dir() / 'loose'
        occupied.parent.mkdir(parents=True, exist_ok=True)
        occupied.write_text('existing file')
        script = (self.root / 'ai-dev-playbook' / 'scripts'
                  / 'playbook.py').resolve()
        self.assertTrue(script.is_file())
        self.counter()
        before = self.project_fingerprint()
        for args in (('lifecycle', 'register-material', 'DEV-1',
                      'docs/tasks/loose/note.txt', '--id', 'note',
                      '--role', 'report', '--immutable', '--required'),
                     ('lifecycle', 'register-material', 'DEV-1',
                      'docs/tasks/loose/note.txt', '--id', 'note',
                      '--role', 'report', '--immutable', '--required',
                      '--apply')):
            result = subprocess.run(
                [sys.executable, str(script), *args], cwd=self.root,
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, args)
            self.assertIn('родительский путь', result.stderr, args)
        # Унаследованное зарегистрированное состояние: preflight
        # установленного CLI отклоняет до subprocess.
        path = self.tasks_dir() / 'DEV-1.md'
        meta = self.meta_of(path)
        meta['materials'] = [{'id': 'note', 'role': 'other',
                              'source': 'docs/tasks/loose/note.txt',
                              'storage': 'local', 'required': True,
                              'immutable': True, 'used_by': ['DEV-1']}]
        path.write_text(lifecycle._splice_meta(
            path.read_text(encoding='utf-8'), meta), encoding='utf-8')
        before = self.project_fingerprint()
        for args in (('verify', 'DEV-1', '--preflight'),
                     ('verify', 'DEV-1')):
            result = subprocess.run(
                [sys.executable, str(script), *args], cwd=self.root,
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, args)
            self.assertIn('родительский путь', result.stderr, args)
        self.assertEqual(before, self.project_fingerprint())
        self.assertEqual(self.count(), 0)
        self.assertTrue(occupied.is_file())

    def test_existing_directory_parent_allowed(self):
        # Существующий обычный каталог-предок законен — файл дописывается
        # внутрь при архивировании.
        """A-05: существующий каталог-родитель допустим"""
        self.managed_task()
        parent = self.archive_dir() / 'shared'
        parent.mkdir(parents=True, exist_ok=True)
        (self.tasks_dir() / 'shared').mkdir(parents=True)
        (self.tasks_dir() / 'shared' / 'x.txt').write_text('x')
        result = self.register('docs/tasks/shared/x.txt', 'x', '--apply')
        self.assertEqual(result[0], 0, result)
        self.verify()
        self.assertEqual(self.handoff()[0], 0)


class RelocateMaterialTest(CollisionCase):
    """relocate-material: preview без записи, apply, отказы,
    rollback и общий гейт."""
    def test_preview_shows_plan_without_writes(self):
        """relocate-material preview показывает план без записи"""
        run_dir, copy_rel = self.collision_state()
        before = self.project_fingerprint()
        code, out, err = self.relocate(
            'manual-copy',
            'docs/tasks/DEV-1-evidence/manual-reports/copy-report.json')
        self.assertEqual(code, 0, err)
        self.assertIn('Предпросмотр', out)
        self.assertIn(copy_rel, out)
        self.assertIn('manual-reports/copy-report.json', out)
        self.assertEqual(before, self.project_fingerprint())

    def test_apply_moves_bytes_updates_links_and_companion(self):
        """relocate apply переносит байты, обновляет ссылки и companion"""
        run_dir, copy_rel = self.collision_state()
        card = self.tasks_dir() / 'DEV-1.md'
        card.write_text(card.read_text(encoding='utf-8')
                        + f'\n[копия]({copy_rel.split("docs/tasks/",1)[1]}/report.json)\n',
                        encoding='utf-8')
        original = (self.root / copy_rel / 'report.json').read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        to = 'docs/tasks/DEV-1-evidence/manual-reports/copy-report.json'
        code, out, err = self.relocate('manual-copy', to, '--apply')
        self.assertEqual(code, 0, err + out)
        moved = self.root / to
        self.assertTrue(moved.is_file())
        self.assertEqual(moved.read_bytes(), original)
        self.assertFalse((self.root / copy_rel / 'report.json').exists())
        meta = self.meta_of(card)
        sources = {m['id']: m['source'] for m in meta['materials']}
        self.assertEqual(sources['manual-copy'], to)
        companion_ids = [i for i in sources if i.startswith('relocate-')]
        self.assertEqual(len(companion_ids), 1)
        companion = json.loads(
            (self.root / sources[companion_ids[0]]).read_text())
        self.assertEqual(companion['type'], 'lifecycle-relocate-material')
        self.assertEqual(companion['material'], 'manual-copy')
        self.assertEqual(companion['files'][0]['sha256'], 'sha256:' + digest)
        self.assertEqual(companion['reason'], 'разнести коллизию')
        self.assertEqual(companion['actor'],
                         {'name': 'Devin', 'model': 'SWE-2 High'})
        moves = {(m['from'], m['to']) for m in meta['moves']}
        self.assertIn((f'{copy_rel}/report.json', to), moves)
        self.assertIn('(DEV-1-evidence/manual-reports/copy-report.json)',
                      card.read_text(encoding='utf-8'))
        result = self.cli('verify', 'DEV-1', '--preflight')
        self.assertEqual(result[0], 0, result[2] + result[1])

    def test_relocate_rejects_invalid_materials_and_targets(self):
        """relocate отклоняет невалидные материалы и цели"""
        self.collision_state()
        to = 'docs/tasks/DEV-1-evidence/moved.json'
        cases = [
            ('no-such', to, 'нет материала'),
            ('manual-copy', 'docs/tasks/outside.json', 'внутри'),
            ('manual-copy', 'docs/tasks/DEV-1-evidence', 'файлом внутри'),
            ('manual-copy', 'docs/other/DEV-1-evidence/x.json', 'внутри'),
        ]
        for ident, target, fragment in cases:
            with self.subTest(ident=ident, target=target):
                before = self.project_fingerprint()
                result = self.relocate(ident, target, '--apply')
                self.assertEqual(result[0], 1, result)
                self.assertIn(fragment, result[2])
                self.assertEqual(before, self.project_fingerprint())
        # Занятое назначение и пересечение с другим материалом.
        busy = self.tasks_dir() / 'DEV-1-evidence' / 'busy.json'
        busy.write_text('x')
        result = self.relocate('manual-copy',
                               'docs/tasks/DEV-1-evidence/busy.json', '--apply')
        self.assertEqual(result[0], 1, result)
        self.assertIn('занято', result[2])
        # Каталог и symlink не переносятся.
        path = self.tasks_dir() / 'DEV-1.md'
        meta = self.meta_of(path)
        meta['materials'].append(
            {'id': 'dir-mat', 'role': 'other',
             'source': 'docs/tasks/DEV-1-evidence/work',
             'storage': 'local', 'required': True, 'immutable': False,
             'used_by': ['DEV-1']})
        path.write_text(lifecycle._splice_meta(
            path.read_text(encoding='utf-8'), meta), encoding='utf-8')
        result = self.relocate('dir-mat', to, '--apply')
        self.assertEqual(result[0], 1, result)
        self.assertIn('обычный файл', result[2])
        link = self.tasks_dir() / 'DEV-1-evidence' / 'linked.json'
        link.symlink_to(busy)
        meta['materials'] = [{'id': 'link-mat', 'role': 'other',
                              'source': 'docs/tasks/DEV-1-evidence/linked.json',
                              'storage': 'local', 'required': True,
                              'immutable': False, 'used_by': ['DEV-1']}]
        path.write_text(lifecycle._splice_meta(
            path.read_text(encoding='utf-8'), meta), encoding='utf-8')
        # Синтетический symlink-материал: каталог сам по себе его отклонит,
        # но команда обязана отказать раньше и без записи.
        before = self.project_fingerprint()
        result = self.relocate('link-mat', to, '--apply')
        self.assertEqual(result[0], 1, result)
        self.assertEqual(before, self.project_fingerprint())

    def test_foreign_immutable_link_rejected_own_preserved(self):
        """Чужая immutable-ссылка отклонена, своя сохранена"""
        run_dir, copy_rel = self.collision_state()
        # Чужой immutable-материал со ссылкой на переносимый файл.
        other = self.tasks_dir() / 'DEV-2-evidence'
        other.mkdir(parents=True)
        note = other / 'note.md'
        note.write_text(
            f'[copy](../DEV-1-evidence/work/verification/DEV-1/'
            f'{run_dir.name}/report.json)\n', encoding='utf-8')
        self.managed_task('DEV-2', status='in_progress')
        path2 = self.tasks_dir() / 'DEV-2.md'
        meta2 = self.meta_of(path2)
        meta2['materials'] = [{'id': 'note', 'role': 'other',
                              'source': 'docs/tasks/DEV-2-evidence',
                              'storage': 'local', 'required': True,
                              'immutable': True, 'used_by': ['DEV-2']}]
        path2.write_text(lifecycle._splice_meta(
            path2.read_text(encoding='utf-8'), meta2), encoding='utf-8')
        before = self.project_fingerprint()
        result = self.relocate(
            'manual-copy',
            'docs/tasks/DEV-1-evidence/manual-reports/copy-report.json',
            '--apply')
        self.assertEqual(result[0], 1, result)
        self.assertIn('чужих immutable', result[2])
        self.assertEqual(before, self.project_fingerprint())
        # Убираем чужую ссылку: собственный immutable с такой же ссылкой
        # остаётся побайтно неизменным, а перенос проходит.
        note.write_text('собственный текст без ссылки\n', encoding='utf-8')
        own = self.tasks_dir() / 'DEV-1-evidence' / 'frozen' / 'keep.md'
        own.parent.mkdir(parents=True)
        own.write_text(
            f'[copy](../work/verification/DEV-1/{run_dir.name}/report.json)\n',
            encoding='utf-8')
        path = self.tasks_dir() / 'DEV-1.md'
        meta = self.meta_of(path)
        meta['materials'].append({'id': 'frozen-note', 'role': 'other',
                                  'source': 'docs/tasks/DEV-1-evidence/frozen',
                                  'storage': 'local', 'required': True,
                                  'immutable': True, 'used_by': ['DEV-1']})
        path.write_text(lifecycle._splice_meta(
            path.read_text(encoding='utf-8'), meta), encoding='utf-8')
        frozen_bytes = own.read_bytes()
        result = self.relocate(
            'manual-copy',
            'docs/tasks/DEV-1-evidence/manual-reports/copy-report.json',
            '--apply')
        self.assertEqual(result[0], 0, result[1] + result[2])
        self.assertEqual(own.read_bytes(), frozen_bytes)
        result = self.cli('check', '--ready')
        self.assertEqual(result[0], 0, result[1] + result[2])

    def test_gate_and_final_failure_restore(self):
        """Сбой gate/final при relocate восстанавливает дерево"""
        self.collision_state()
        to = 'docs/tasks/DEV-1-evidence/manual-reports/copy-report.json'
        before = self.project_fingerprint()
        gate = fops.acquire_gate(self.root, playbook.safe_path,
                                 playbook.require, owner='test holder')
        try:
            result = self.relocate('manual-copy', to, '--apply')
            self.assertEqual(result[0], 1, result)
            self.assertEqual(before, self.project_fingerprint())
        finally:
            gate.close()
        ctx = dict(playbook._lifecycle_ctx(None))
        original = ctx['run_operation']

        def fail_final(root, spec):
            spec = dict(spec)
            spec['final_checks'] = [lambda _: (_ for _ in ()).throw(
                ValueError('injected final failure'))]
            return original(root, spec)

        ctx['run_operation'] = fail_final
        with self.assertRaisesRegex(ValueError, 'injected final failure'):
            lifecycle.relocate_material_command(
                self.root, 'DEV-1', 'manual-copy', to, 'разнести коллизию',
                True, 'Devin', 'SWE-2 High', ctx,
                playbook.safe_path, playbook.require)
        self.assertEqual(before, self.project_fingerprint())
        self.assertIsNone(fops.pending_info(self.root))
        result = self.relocate('manual-copy', to, '--apply')
        self.assertEqual(result[0], 0, result[1] + result[2])

    def test_target_inside_own_evidence_root_material(self):
        # A-02: собственный материал-корень owner-evidence — законное
        # повторное представление, перенос внутрь него разрешён.
        """Цель внутри собственного evidence-root материала допустима"""
        run_dir, copy_rel = self.collision_state()
        path = self.tasks_dir() / 'DEV-1.md'
        meta = self.meta_of(path)
        meta['materials'].append(
            {'id': 'whole-evidence', 'role': 'other',
             'source': 'docs/tasks/DEV-1-evidence', 'storage': 'local',
             'required': True, 'immutable': True, 'used_by': ['DEV-1']})
        path.write_text(lifecycle._splice_meta(
            path.read_text(encoding='utf-8'), meta), encoding='utf-8')
        to = 'docs/tasks/DEV-1-evidence/manual/copy.json'
        original = (self.root / copy_rel / 'report.json').read_bytes()
        result = self.relocate('manual-copy', to)
        self.assertEqual(result[0], 0, result[1] + result[2])
        before = self.project_fingerprint()
        result = self.relocate('manual-copy', to, '--apply')
        self.assertEqual(result[0], 0, result[1] + result[2])
        self.assertNotEqual(before, self.project_fingerprint())
        self.assertEqual((self.root / to).read_bytes(), original)
        result = self.cli('verify', 'DEV-1', '--preflight')
        self.assertEqual(result[0], 0, result[1] + result[2])

    def test_stale_source_or_target_rejected_without_writes(self):
        # A-03: изменение входов между подготовкой плана и записью —
        # контролируемый отказ, дерево не меняется.
        """Устаревший источник/цель отклонены без записи"""
        run_dir, copy_rel = self.collision_state()
        to = 'docs/tasks/DEV-1-evidence/manual-reports/copy-report.json'
        for mutate in ('source', 'target'):
            with self.subTest(mutate=mutate):
                ctx = dict(playbook._lifecycle_ctx(None))
                original = ctx['run_operation']
                keep = (self.root / copy_rel / 'report.json').read_bytes()

                def wrapper(root, spec, mutate=mutate, keep=keep):
                    if mutate == 'source':
                        (self.root / copy_rel / 'report.json').write_text(
                            '{"stale": true}')
                    else:
                        target = self.root / to
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text('occupied')
                    try:
                        return original(root, spec)
                    finally:
                        if mutate == 'source':
                            (self.root / copy_rel
                             / 'report.json').write_bytes(keep)
                        else:
                            target.unlink()

                ctx['run_operation'] = wrapper
                before = self.project_fingerprint()
                with self.assertRaises(ValueError):
                    lifecycle.relocate_material_command(
                        self.root, 'DEV-1', 'manual-copy', to,
                        'разнести коллизию', True, 'Devin', 'SWE-2 High',
                        ctx, playbook.safe_path, playbook.require)
                self.assertEqual(before, self.project_fingerprint())
                self.assertIsNone(fops.pending_info(self.root))

    def test_interruption_and_separate_restore(self):
        # A-03: прерывание применения оставляет pending; штатный
        # restore возвращает пред-образ и снимает маркер.
        """Прерывание relocate и отдельный restore"""
        run_dir, copy_rel = self.collision_state()
        to = 'docs/tasks/DEV-1-evidence/manual-reports/copy-report.json'
        ctx = dict(playbook._lifecycle_ctx(None))
        original = ctx['run_operation']

        def wrapper(root, spec):
            spec = dict(spec)
            spec['final_checks'] = [
                lambda _: (_ for _ in ()).throw(KeyboardInterrupt())]
            return original(root, spec)

        ctx['run_operation'] = wrapper
        before = self.project_fingerprint()
        with self.assertRaises(KeyboardInterrupt):
            lifecycle.relocate_material_command(
                self.root, 'DEV-1', 'manual-copy', to, 'разнести коллизию',
                True, 'Devin', 'SWE-2 High', ctx,
                playbook.safe_path, playbook.require)
        self.assertIsNotNone(fops.pending_info(self.root))
        result = fops.restore_operation(self.root, playbook.safe_path,
                                        playbook.require)
        self.assertEqual(result['status'], 'restored')
        self.assertEqual(before, self.project_fingerprint())
        self.assertIsNone(fops.pending_info(self.root))
        result = self.relocate('manual-copy', to, '--apply')
        self.assertEqual(result[0], 0, result[1] + result[2])

    def installed_script(self):
        script = (self.root / 'ai-dev-playbook' / 'scripts'
                  / 'playbook.py').resolve()
        self.assertTrue(script.is_file(), 'установленный CLI отсутствует')
        return script

    def test_installed_cli_subprocess(self):
        # A-03: реальный subprocess установленного playbook.py проекта.
        """relocate через установленный CLI subprocess"""
        run_dir, copy_rel = self.collision_state()
        script = self.installed_script()
        to = 'docs/tasks/DEV-1-evidence/manual-reports/copy-report.json'
        cmd = [sys.executable, str(script), 'lifecycle',
               'relocate-material', 'DEV-1', '--id', 'manual-copy',
               '--to', to, '--reason', 'проверка subprocess CLI',
               '--actor', 'Devin', '--model', 'SWE-2 High']
        before = self.project_fingerprint()
        result = subprocess.run(cmd, cwd=self.root, capture_output=True,
                                text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Предпросмотр', result.stdout)
        self.assertEqual(before, self.project_fingerprint())
        result = subprocess.run(cmd + ['--apply'], cwd=self.root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        moved = self.root / to
        self.assertTrue(moved.is_file())
        self.assertFalse((self.root / copy_rel / 'report.json').exists())

    def test_installed_cli_early_rejection_without_runner(self):
        # A-03: original/copy/runner через установленный CLI — ранний
        # отказ до записи и до subprocess runner (счётчик = 0).
        """Ранний отказ relocate на установленном CLI без runner"""
        run_dir, copy_rel = self.collision_state()
        script = self.installed_script()
        self.counter()
        before = self.project_fingerprint()
        result = subprocess.run(
            [sys.executable, str(script), 'verify', 'DEV-1',
             '--preflight'], cwd=self.root, capture_output=True,
            text=True)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('назначены', result.stderr)
        result = subprocess.run(
            [sys.executable, str(script), 'verify', 'DEV-1'],
            cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('назначены', result.stderr)
        self.assertEqual(before, self.project_fingerprint())
        self.assertEqual(self.count(), 0)
        self.assertIsNone(fops.pending_info(self.root))
        # Ранняя регистрация на чистой задаче: файл-предок и
        # файл-потомок назначений — вторая отклоняется до записи.
        self.managed_task('DEV-2', status='in_progress')
        (self.tasks_dir() / 'DEV-2-evidence').mkdir(parents=True)
        (self.tasks_dir() / 'DEV-2-evidence' / 'docs').write_text('f')
        (self.root / 'docs' / 'misc').mkdir(parents=True)
        (self.root / 'docs' / 'misc' / 'note.txt').write_text('n')
        base = [sys.executable, str(script), 'lifecycle',
                'register-material', 'DEV-2']
        result = subprocess.run(
            base + ['docs/tasks/DEV-2-evidence/docs', '--id', 'ancestor',
                    '--role', 'other', '--immutable', '--required',
                    '--apply'], cwd=self.root, capture_output=True,
            text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        before = self.project_fingerprint()
        result = subprocess.run(
            base + ['docs/misc/note.txt', '--id', 'child', '--role',
                    'other', '--immutable', '--required', '--apply'],
            cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('родителем', result.stderr)
        self.assertIn('ancestor', result.stderr)
        self.assertIn('child', result.stderr)
        self.assertEqual(before, self.project_fingerprint())

    def test_repeat_is_safe_message_without_new_copy(self):
        """Повторный relocate — безопасное сообщение без новой копии"""
        self.collision_state()
        to = 'docs/tasks/DEV-1-evidence/manual-reports/copy-report.json'
        self.assertEqual(self.relocate('manual-copy', to, '--apply')[0], 0)
        after = self.project_fingerprint()
        code, out, err = self.relocate('manual-copy', to, '--apply')
        self.assertEqual(code, 0, err)
        self.assertIn('уже размещён', out)
        self.assertEqual(after, self.project_fingerprint())
        companions = list((self.tasks_dir() / 'DEV-1-evidence')
                          .glob('relocate-*.json'))
        self.assertEqual(len(companions), 1)

    def test_full_pb060_equivalent_close_dry_run_and_close(self):
        """Полный эквивалент PB-060: dry-run и close после relocate"""
        run_dir, copy_rel = self.collision_state()
        to = 'docs/tasks/DEV-1-evidence/manual-reports/copy-report.json'
        self.assertEqual(self.relocate('manual-copy', to, '--apply')[0], 0)
        # Новый прогон после изменения входов; обе копии сохранены.
        self.verify()
        self.assertEqual(self.handoff()[0], 0)
        self.assertEqual(self.accept('--result', 'accepted')[0], 0)
        before = self.project_fingerprint()
        result = self.cli('close', 'DEV-1', '--dry-run')
        self.assertEqual(result[0], 0, result[1] + result[2])
        self.assertEqual(before, self.project_fingerprint())
        result = self.cli('close', 'DEV-1')
        self.assertEqual(result[0], 0, result[1] + result[2])
        archive = self.archive_dir() / 'DEV-1-evidence'
        runner_copy = archive / 'work' / 'verification' / 'DEV-1' / \
            run_dir.name / 'report.json'
        manual_copy = archive / 'manual-reports' / 'copy-report.json'
        self.assertTrue(runner_copy.is_file())
        self.assertTrue(manual_copy.is_file())
        self.assertEqual(runner_copy.read_bytes(),
                         manual_copy.read_bytes())
        meta = self.meta_of(self.archive_dir() / 'DEV-1.md')
        sources = {m['id']: m['source'] for m in meta['materials']}
        self.assertEqual(sources['manual-copy'],
                         'docs/archive/tasks/DEV-1-evidence/manual-reports/'
                         'copy-report.json')
        self.assertIn(f'verify-{run_dir.name}', sources)

    def test_relocate_on_external_metadata_card(self):
        # Пара Markdown + .meta.json (PB-041): relocate обновляет обе части.
        """relocate на карточке с external metadata"""
        code, _, err = self.cli('card', 'create', 'DEV-1', '--title',
                                'Внешняя карточка', '--apply')
        self.assertEqual(code, 0, err)
        card = self.tasks_dir() / 'DEV-1.md'
        card.write_text(card.read_text(encoding='utf-8').replace(
            'Status: draft', 'Status: in_progress'), encoding='utf-8')
        self.assertEqual(self.cli('index')[0], 0)
        self.verify()
        _, dest_dir = self.copy_report()
        copy_rel = str(dest_dir.relative_to(self.root)) + '/report.json'
        code, _, err = self.register(copy_rel, 'manual-copy', '--apply')
        self.assertEqual(code, 0, err)
        to = 'docs/tasks/DEV-1-evidence/manual-reports/copy-report.json'
        result = self.relocate('manual-copy', to, '--apply')
        self.assertEqual(result[0], 0, result[1] + result[2])
        meta = json.loads(
            card.with_suffix('.meta.json').read_text(encoding='utf-8'))
        sources = {m['id']: m['source'] for m in meta['materials']}
        self.assertEqual(sources['manual-copy'], to)
        errors = []
        documents.parse_meta(card.read_text(encoding='utf-8'),
                             'docs/tasks/DEV-1.md', errors)
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
