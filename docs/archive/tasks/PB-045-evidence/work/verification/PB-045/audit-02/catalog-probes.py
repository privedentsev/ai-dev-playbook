import sys,json,pathlib,tempfile,hashlib,unittest
sys.path.insert(0,'/Users/privedentsev1/Dev/ai-dev-playbook/tests')
from test_cli import CliCase,REPO_VERSION
from test_playbook import playbook
from test_documents import doc_text,doc_meta
import project_view as pv
class CatalogProbe(CliCase):
    def setUp(self):
        super().setUp();self.install(version=REPO_VERSION)
        self.stage_tmp=tempfile.TemporaryDirectory(dir='/private/tmp');self.addCleanup(self.stage_tmp.cleanup);self.stage=pathlib.Path(self.stage_tmp.name)
        code,out,err=self.cli('card','create','DEV-10','--title','Independent external card','--apply');self.assertEqual(code,0,err)
    def put(self,rel,content):
        p=self.stage/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content);return p
    def fingerprint(self):return {str(p.relative_to(self.root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    def catalog(self,v):
        data,paths=playbook.config(v.path);records=playbook.read_tasks(v.path,paths)
        return data,paths,records,playbook.documents.load_catalog(v.path,data,paths,records,playbook.safe_path,playbook.require)
    def test_deleted_card_with_retained_sidecar_rejected_without_live_write(self):
        before=self.fingerprint();v=pv.FutureView(self.root,self.stage,removes=['docs/tasks/DEV-10.md'])
        with self.assertRaisesRegex(ValueError,'orphan'):self.catalog(v)
        self.assertEqual(before,self.fingerprint())
    def test_new_document_with_invalid_relation_is_not_hidden(self):
        before=self.fingerprint();rel='docs/specs/independent.md'
        self.put(rel,doc_text('Independent',doc_meta(document_id='spec:independent',relations=[{'relation':'based_on','target':'spec:missing'}])))
        v=pv.FutureView(self.root,self.stage,writes=[rel])
        with self.assertRaisesRegex(ValueError,'spec:missing'):self.catalog(v)
        self.assertEqual(before,self.fingerprint())
    def test_future_removed_target_is_reported_from_new_document(self):
        before=self.fingerprint();rel='docs/specs/independent.md'
        self.put(rel,doc_text('Independent',doc_meta(document_id='spec:independent'),body='[removed](../tasks/DEV-10.md)\n'))
        v=pv.FutureView(self.root,self.stage,writes=[rel],removes=['docs/tasks/DEV-10.md','docs/tasks/DEV-10.meta.json'])
        data,paths,records,catalog=self.catalog(v)
        with self.assertRaisesRegex(ValueError,'DEV-10.md'):playbook.check_links(v.path,paths,catalog)
        self.assertEqual(before,self.fingerprint())
    def test_catalog_read_set_catches_new_document_after_scan(self):
        v=pv.FutureView(self.root,self.stage);self.catalog(v);v.read_set.recheck()
        (self.root/'docs/specs/external-edit.md').write_text(doc_text('New edit',doc_meta(document_id='spec:new-edit')))
        with self.assertRaisesRegex(ValueError,'stale collection'):v.read_set.recheck()
if __name__=='__main__':unittest.main(verbosity=2)
