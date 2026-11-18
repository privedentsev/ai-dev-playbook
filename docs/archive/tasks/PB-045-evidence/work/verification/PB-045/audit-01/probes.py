import contextlib
import hashlib
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, '/Users/privedentsev1/Dev/ai-dev-playbook/ai-dev-playbook/scripts')
import project_view as pv
import document_links

class Probe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.root = self.base / 'root'; self.root.mkdir()
        self.stage = self.base / 'stage'; self.stage.mkdir()
    def put(self, base, name, data=b'bytes'):
        p = base/name; p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);return p
    def test_overlay_matches_independent_physical_tree(self):
        self.put(self.root,'docs/old/card',b'old');self.put(self.root,'docs/keep',b'keep')
        self.put(self.stage,'archive/new/card',b'new')
        v=pv.FutureView(self.root,self.stage,writes=['archive/new/card'],removes=['docs/old/card'],create_dirs=['empty/nested'],remove_dirs=['docs/old'])
        expected={'archive':None,'archive/new':None,'archive/new/card':b'new','docs':None,'docs/keep':b'keep','empty':None,'empty/nested':None}
        actual={str(p.relative_to(v.path)):p.read_bytes() if p.is_file() else None for p in v.path.rglob('*')}
        self.assertEqual(actual,expected)
    def test_unannounced_write_over_directory_rejected(self):
        self.put(self.root,'target/child');self.put(self.stage,'target')
        with self.assertRaisesRegex(ValueError,'collision|directory|каталог'):
            pv.FutureView(self.root,self.stage,writes=['target'])
    def test_obsolete_child_is_not_valid_link_below_future_file(self):
        self.put(self.root,'target/child');self.put(self.stage,'target')
        try:v=pv.FutureView(self.root,self.stage,writes=['target'])
        except ValueError:return
        self.assertTrue(document_links.check_text('[obsolete](target/child)','probe',v.path,v.path))
    def test_explicit_file_to_empty_directory_matches_oracle(self):
        self.put(self.root,'x')
        v=pv.FutureView(self.root,self.stage,removes=['x'],create_dirs=['x'])
        self.assertTrue((v.path/'x').is_dir());self.assertEqual(list((v.path/'x').iterdir()),[])
    def test_explicit_file_to_directory_with_child_matches_oracle(self):
        self.put(self.root,'x');self.put(self.stage,'x/new',b'new')
        v=pv.FutureView(self.root,self.stage,writes=['x/new'],removes=['x'])
        self.assertEqual({str(p.relative_to(v.path)):p.read_bytes() if p.is_file() else None for p in v.path.rglob('*')},{'x':None,'x/new':b'new'})
    def test_symlink_staging_root_rejected_before_write(self):
        outside=self.base/'outside';outside.mkdir();self.stage.rmdir();self.stage.symlink_to(outside,target_is_directory=True)
        source=self.put(self.root,'source')
        with self.assertRaisesRegex(ValueError,'symlink|symbolic|link'):
            pv.prepare_payload(self.stage,'written',source)
        self.assertFalse((outside/'written').exists())
    def test_symlink_staging_ancestor_rejected_before_write(self):
        outside=self.base/'outside';outside.mkdir();(outside/'nested').mkdir();alias=self.base/'alias';alias.symlink_to(outside,target_is_directory=True)
        source=self.put(self.root,'source')
        with self.assertRaisesRegex(ValueError,'symlink|symbolic|link'):
            pv.prepare_payload(alias/'nested','written',source)
        self.assertFalse((outside/'nested/written').exists())
    def test_symlink_staging_ancestor_rejected_by_view(self):
        outside=self.base/'outside';outside.mkdir();self.put(outside,'nested/written');alias=self.base/'alias';alias.symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'symlink|symbolic|link'):
            pv.FutureView(self.root,alias/'nested',writes=['written'])
    def test_non_directory_ancestor_is_an_absent_child(self):
        self.put(self.root,'file')
        v=pv.ProjectView(self.root)
        self.assertFalse((v.path/'file/child').exists())
        self.assertTrue(document_links.check_text('[broken](file/child)','probe',v.path,v.path))
    def test_read_set_binds_actual_stream_and_new_absence(self):
        p=self.put(self.root,'read',b'abcdef');v=pv.ProjectView(self.root)
        with v.open_binary('read') as stream:self.assertEqual(stream.read(1),b'a')
        self.assertEqual(v.read_set.files['read'],'sha256:'+hashlib.sha256(b'abcdef').hexdigest())
        self.assertFalse((v.path/'missing/new').exists());v.read_set.recheck()
        self.put(self.root,'missing/new')
        with self.assertRaises(ValueError):v.read_set.recheck()
    def test_read_set_observes_document_collection_changes(self):
        self.put(self.root,'docs/first');v=pv.ProjectView(self.root);list((v.path/'docs').iterdir());v.read_set.recheck()
        self.put(self.root,'docs/second')
        with self.assertRaises(ValueError):v.read_set.recheck()
    def test_payload_reads_are_bounded_and_no_live_links(self):
        src=self.put(self.root,'large',b'x'*(3*pv.CHUNK_SIZE+7));sizes=[];original=pathlib.Path.open
        class Guard:
            def __init__(self,wrapped):self.wrapped=wrapped
            def __enter__(self):self.wrapped.__enter__();return self
            def __exit__(self,*args):return self.wrapped.__exit__(*args)
            def read(self,n=-1):
                sizes.append(n)
                if not 0 < n <= pv.CHUNK_SIZE:raise AssertionError(f'unbounded read: {n}')
                return self.wrapped.read(n)
        def guarded(path,*args,**kwargs):
            stream=original(path,*args,**kwargs)
            return Guard(stream) if path==src and args and args[0]=='rb' else stream
        with patch.object(pathlib.Path,'open',guarded),patch('shutil.copytree',side_effect=AssertionError('full tree copied')):
            result=pv.prepare_payload(self.stage,'payload',src)
        self.assertTrue(sizes);self.assertEqual(result['size'],3*pv.CHUNK_SIZE+7)
        self.assertEqual((self.stage/'payload').stat().st_nlink,1)
        self.assertNotEqual(src.stat().st_ino,(self.stage/'payload').stat().st_ino)
    def test_live_parent_symlink_and_payload_hardlink_rejected(self):
        source=self.put(self.root,'source');(self.root/'link').symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError):(pv.ProjectView(self.root).path/'link/source').read_bytes()
        (self.stage/'payload').hardlink_to(source)
        with self.assertRaises(ValueError):pv.FutureView(self.root,self.stage,writes=['payload'])

if __name__=='__main__':unittest.main(verbosity=2)
