import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_cli import CliCase, REPO_VERSION
from test_playbook import playbook, write_card
import project_view


class FutureViewTest(unittest.TestCase):
    """FutureView: соответствие физическому oracle, свежесть байтов,
    symlink/hardlink и коллизии."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'root'
        self.staged = self.base / 'staged'
        self.root.mkdir(); self.staged.mkdir()

    def put(self, directory, rel, content):
        p = directory / rel; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
        return p

    def test_future_matches_physical_oracle(self):
        """FutureView соответствует физическому oracle"""
        self.put(self.root, 'docs/old/card.md', '[target](../asset.txt)')
        self.put(self.root, 'docs/asset.txt', 'asset')
        self.put(self.root, 'unchanged.txt', 'untouched')
        self.put(self.staged, 'docs/new/card.md', '[target](../asset.txt)')
        view = project_view.FutureView(self.root, self.staged, ['docs/new/card.md'],
            ['docs/old/card.md'], ['empty'], ['docs/old'])
        expected = self.base / 'oracle'; shutil.copytree(self.root, expected)
        shutil.rmtree(expected / 'docs/old'); (expected / 'docs/new').mkdir()
        shutil.copyfile(self.staged / 'docs/new/card.md', expected / 'docs/new/card.md')
        (expected / 'empty').mkdir()
        actual = {p.relative_to(view.path).as_posix(): p.read_bytes() if p.is_file() else None
                  for p in view.path.rglob('*')}
        wanted = {p.relative_to(expected).as_posix(): p.read_bytes() if p.is_file() else None
                  for p in expected.rglob('*')}
        self.assertEqual(actual, wanted)
        p = view.path / 'docs/new/card.md'
        self.assertEqual(playbook.document_links.check_text(p.read_text(), 'new', p.parent, view.path), [])
        self.assertFalse((view.path / 'docs/old/card.md').exists())

    def test_actual_file_bytes_absence_and_membership_are_fresh(self):
        """Байты/отсутствие/членство файлов свежи в view"""
        self.put(self.root, 'docs/a', 'old')
        view = project_view.ProjectView(self.root)
        self.assertEqual((view.path/'docs/a').read_bytes(), b'old')
        self.assertFalse((view.path/'missing').exists())
        list((view.path/'docs').iterdir()); view.read_set.recheck()
        self.put(self.root, 'docs/a', 'new')
        with self.assertRaisesRegex(ValueError, 'stale input bytes'): view.read_set.recheck()
        self.put(self.root, 'docs/a', 'old'); self.put(self.root, 'missing', 'added')
        with self.assertRaisesRegex(ValueError, 'stale input state'): view.read_set.recheck()
        (self.root/'missing').unlink(); self.put(self.root, 'docs/extra', 'new')
        with self.assertRaisesRegex(ValueError, 'stale collection'): view.read_set.recheck()
        (self.root/'docs/extra').unlink(); (self.root/'docs/a').unlink()
        with self.assertRaises(ValueError): view.read_set.recheck()

    def test_read_hash_is_not_taken_from_unrelated_second_read(self):
        """Хеш чтения не берётся из несвязанного второго чтения"""
        p=self.put(self.root, 'a', 'original'); v=project_view.ProjectView(self.root)
        with v.open_binary('a') as reader:
            self.assertEqual(reader.read(3), b'ori')
        self.assertEqual(v.read_set.files['a'], 'sha256:'+hashlib.sha256(b'original').hexdigest())
        p.write_text('changed')
        with self.assertRaises(ValueError): (v.path/'a').read_bytes()

    def test_no_implicit_live_path_conversion_or_write(self):
        """Нет неявной конверсии live-пути или записи"""
        v=project_view.ProjectView(self.root)
        with self.assertRaises(TypeError): Path(v.path)
        with self.assertRaises(AttributeError): (v.path/'a').write_text('bad')
        with self.assertRaises(ValueError): v.path/'../../outside'
        with self.assertRaises(ValueError): v.read_bytes('../outside')

    def test_symlinks_and_staged_hardlinks_rejected(self):
        """Symlink и staged hardlinks отклоняются"""
        original=self.put(self.root,'a','original')
        (self.staged/'a').hardlink_to(original)
        with self.assertRaisesRegex(ValueError,'linked'): project_view.FutureView(self.root,self.staged,['a'])
        (self.staged/'a').unlink(); (self.staged/'a').symlink_to(original)
        with self.assertRaisesRegex(ValueError,'symlink'): project_view.FutureView(self.root,self.staged,['a'])
        (self.root/'link').symlink_to(original)
        with self.assertRaises(ValueError): (project_view.ProjectView(self.root).path/'link').read_bytes()

    def test_file_directory_collision_is_not_hidden_by_overlay(self):
        """Коллизия файл/каталог не скрывается overlay"""
        self.put(self.root, 'blocked', 'file')
        self.put(self.staged, 'blocked/child', 'new')
        with self.assertRaisesRegex(ValueError, 'collision'):
            project_view.FutureView(self.root, self.staged, ['blocked/child'])

    def test_streaming_payload_does_not_copy_unrelated_trees(self):
        """Потоковый payload не копирует несвязанные деревья"""
        source=self.root/'large'
        with source.open('wb') as f:
            for _ in range(10): f.write(b'x'*project_view.CHUNK_SIZE)
        for rel in ['src/code','media/movie','node_modules/pkg/code']:
            self.put(self.root,rel,'unrelated')
        with patch('shutil.copytree', side_effect=AssertionError('full copy')):
            result=project_view.prepare_payload(self.staged,'evidence/large',source)
            v=project_view.FutureView(self.root,self.staged,['evidence/large'])
            with v.open_binary('evidence/large') as stream:
                self.assertEqual(len(stream.read(19)),19)
        self.assertEqual(result['size'], 10*project_view.CHUNK_SIZE)
        self.assertEqual([p.relative_to(self.staged).as_posix() for p in self.staged.rglob('*') if p.is_file()], ['evidence/large'])
        self.assertFalse(any(p.startswith(('src/','media/','node_modules/')) for p in v.read_set.files))

    def test_write_cannot_turn_live_directory_into_file_with_live_children(self):
        """Запись не превращает live-каталог в файл"""
        self.put(self.root, 'directory/child', 'old')
        self.put(self.staged, 'directory', 'new')
        for removed in ((), ('directory',)):
            with self.assertRaisesRegex(ValueError, 'collision|contradictory'):
                project_view.FutureView(self.root,self.staged,['directory'],remove_dirs=removed)

    def test_file_to_directory_replacement_and_unlisted_children_are_rejected(self):
        """Замена файла на каталог с детьми отклонена"""
        self.put(self.root, 'file', 'old')
        self.put(self.staged, 'file/child', 'new')
        with self.assertRaisesRegex(ValueError,'collision'):
            project_view.FutureView(self.root,self.staged,['file/child'],['file'])
        with self.assertRaisesRegex(ValueError,'contradictory'):
            project_view.FutureView(self.root,self.staged,removes=['file'],create_dirs=['file'])
        self.put(self.root, 'directory/child', 'old')
        with self.assertRaisesRegex(ValueError,'would not be empty'):
            project_view.FutureView(self.root,self.staged,remove_dirs=['directory'])

    def test_staged_symlink_root_and_ancestor_never_receive_payload(self):
        """Staged symlink root/ancestor не получают payload"""
        source=self.put(self.root,'source','payload')
        outside=self.base/'outside';outside.mkdir()
        alias=self.base/'alias';alias.symlink_to(outside,target_is_directory=True)
        for staging in (alias,alias/'nested'):
            with self.assertRaisesRegex(ValueError,'symlink'):
                project_view.prepare_payload(staging,'new',source)
            with self.assertRaisesRegex(ValueError,'symlink'):
                project_view.FutureView(self.root,staging,())
        self.assertEqual(list(outside.iterdir()),[])
        source_alias=self.base/'source-alias';source_alias.symlink_to(self.root,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'symlink'):
            project_view.prepare_payload(self.staged,'new',source_alias/'source')
        self.assertFalse((self.staged/'new').exists())

    def test_file_descendant_is_absent_and_produces_broken_link_error(self):
        """Потомок файла absent → broken link error"""
        self.put(self.root,'file','old')
        for view in (project_view.ProjectView(self.root),project_view.FutureView(self.root,self.staged)):
            self.assertFalse((view.path/'file/missing').exists())
            errors=playbook.document_links.check_text('[broken](file/missing)','probe',view.path,view.path)
            self.assertEqual(len(errors),1)
            self.assertIn('file/missing',errors[0])
            view.read_set.recheck()
            outside_errors=playbook.document_links.check_text('[broken](../../outside)','probe',view.path,view.path)
            self.assertEqual(len(outside_errors),1)


    def test_root_mutations_and_late_staged_symlink_are_rejected(self):
        """Мутации корня и поздний staged symlink отклонены"""
        for kwargs in ({'removes':['.']},{'remove_dirs':['.']},{'create_dirs':['.']}):
            with self.assertRaisesRegex(ValueError,'project root'):
                project_view.FutureView(self.root,self.staged,**kwargs)
        self.put(self.staged,'payload','new')
        view=project_view.FutureView(self.root,self.staged,['payload'])
        (self.staged/'payload').unlink()
        outside=self.put(self.base,'outside-file','outside')
        (self.staged/'payload').symlink_to(outside)
        with self.assertRaisesRegex(ValueError,'symlink'):
            (view.path/'payload').read_bytes()


    def test_dotdot_staged_alias_cannot_reclassify_live_bytes(self):
        """Dotdot staged-алиас не реклассифицирует live-байты"""
        original=self.put(self.root,'payload','original')
        alias=self.root/'..'/self.root.name
        with self.assertRaisesRegex(ValueError,'noncanonical'):
            project_view.FutureView(self.root,alias,['payload'])
        with self.assertRaisesRegex(ValueError,'noncanonical'):
            project_view.prepare_payload(alias,'new',original)
        self.assertFalse((self.root/'new').exists())
        self.assertEqual(original.read_text(),'original')
        canonical=self.root.resolve()
        with self.assertRaisesRegex(ValueError,'separate'):
            project_view.FutureView(self.root,canonical,['payload'])


class FutureValidationTest(CliCase):
    """Валидация через view: зависимости, ссылки, sidecar и moved
    pair."""
    def setUp(self):
        super().setUp(); self.install(version=REPO_VERSION)
        self.stage_tmp=tempfile.TemporaryDirectory();self.addCleanup(self.stage_tmp.cleanup)
        self.staged=Path(self.stage_tmp.name).resolve()
        self.add_task('DEV-1','ready')

    def overlay(self,writes,removes=(),create_dirs=(),remove_dirs=()):
        for rel,body in writes.items():
            p=self.staged/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(body,encoding='utf-8')
        return project_view.FutureView(self.root,self.staged,writes,removes,create_dirs,remove_dirs)

    def test_catalog_tasks_and_indices_match_oracle(self):
        """Каталог задач и индексы совпадают с oracle"""
        old='docs/tasks/DEV-1.md'; new='docs/archive/tasks/DEV-1.md'
        body=(self.root/old).read_text().replace('Status: ready','Status: done').replace('Closed: -','Closed: 2026-02-01T00:00:00Z')
        view=self.overlay({new:body},[old]); data,paths=playbook.config(view.path)
        records=playbook.read_tasks(view.path,paths)
        self.assertTrue(records['DEV-1']['archived'])
        index=playbook.render_index(records,paths['tasks'])
        p=self.staged/'docs/tasks/index.md';p.parent.mkdir(parents=True,exist_ok=True);p.write_text(index)
        view=project_view.FutureView(self.root,self.staged,[new,'docs/tasks/index.md'],[old])
        check=playbook.lifecycle._consistency(playbook._lifecycle_ctx(None),[],True,'future',playbook.safe_path,playbook.require)
        check(view.path)
        self.assertTrue((self.root/old).exists());self.assertFalse((self.root/new).exists())

    def test_missing_dependency_and_future_broken_link_are_not_masked(self):
        """Отсутствующая зависимость и broken link не маскируются"""
        rel='docs/tasks/DEV-1.md';text=(self.root/rel).read_text()
        view=self.overlay({rel:text.replace('Depends-On: -','Depends-On: MISSING-1')})
        _,paths=playbook.config(view.path)
        with self.assertRaisesRegex(ValueError,'отсутствует зависимость'):playbook.read_tasks(view.path,paths)
        (self.staged/rel).write_text(text+'\n[broken](absent.md)\n')
        view=project_view.FutureView(self.root,self.staged,[rel]);data,paths=playbook.config(view.path)
        records=playbook.read_tasks(view.path,paths)
        catalog=playbook.documents.load_catalog(view.path,data,paths,records,playbook.safe_path,playbook.require)
        with self.assertRaisesRegex(ValueError,'absent.md'):playbook.check_links(view.path,paths,catalog)

    def test_future_index_and_document_metadata_are_checked(self):
        """Индекс и meta документов проверяются через view"""
        view=self.overlay({'docs/tasks/index.md':'stale index'})
        check=playbook.lifecycle._consistency(playbook._lifecycle_ctx(None),[],True,'future',playbook.safe_path,playbook.require)
        with self.assertRaisesRegex(ValueError,'индекс задач'):check(view.path)
        from test_documents import doc_text, doc_meta
        rel='docs/specs/future.md'
        view=self.overlay({rel:doc_text('future',doc_meta(relations=[{'relation':'based_on','target':'spec:missing'}]))})
        data,paths=playbook.config(view.path);records=playbook.read_tasks(view.path,paths)
        with self.assertRaisesRegex(ValueError,'spec:missing'):
            playbook.documents.load_catalog(view.path,data,paths,records,playbook.safe_path,playbook.require)

    def test_external_card_sidecar_is_read_through_view(self):
        """Sidecar внешней карточки читается через view"""
        code,_,err=self.cli('card','create','DEV-2','--title','external','--apply');self.assertEqual(code,0,err)
        rel='docs/tasks/DEV-2.meta.json';raw=json.loads((self.root/rel).read_text());raw['card_format_version']=None
        view=self.overlay({rel:json.dumps(raw)})
        _,paths=playbook.config(view.path)
        with self.assertRaisesRegex(ValueError,'card_format_version'):playbook.read_tasks(view.path,paths)
        view=project_view.FutureView(self.root,self.staged,[],[rel])
        _,paths=playbook.config(view.path)
        with self.assertRaisesRegex(ValueError,'отсутствует'):playbook.read_tasks(view.path,paths)

    def test_moved_external_pair_uses_final_sidecar_path(self):
        """Перемещённая внешняя пара использует финальный путь sidecar"""
        code,_,err=self.cli('card','create','DEV-2','--title','external','--apply');self.assertEqual(code,0,err)
        old='docs/tasks/DEV-2.md';new='docs/archive/tasks/DEV-2.md'
        old_json='docs/tasks/DEV-2.meta.json';new_json='docs/archive/tasks/DEV-2.meta.json'
        view=self.overlay({new:(self.root/old).read_text(),new_json:(self.root/old_json).read_text()},[old,old_json])
        hydrated=playbook.documents.read_text(view.path/new)
        self.assertIn('card_format_version',hydrated)
        self.assertFalse((view.path/old_json).exists())
        self.assertTrue((self.root/old_json).exists())
