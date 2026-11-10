import sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_close_lifecycle import CloseLifecycleCase
class AuditProbes(CloseLifecycleCase):
    def reg(self, source, *extra):
        return self.cli('lifecycle','register-material','DEV-1',source,'--id','proof','--role','report','--immutable','--required',*extra)
    def test_external_to_docs_full_route(self):
        self.managed_task()
        path=self.root/'artifacts/proof.bin'; path.parent.mkdir(); path.write_bytes(bytes(range(256)))
        before=self.project_fingerprint(); self.assertEqual(self.reg('artifacts/proof.bin')[0],0); self.assertEqual(before,self.project_fingerprint())
        self.assertEqual(self.reg('artifacts/proof.bin','--apply')[0],0)
        self.verify(); self.assertEqual(self.handoff()[0],0)
        self.assertEqual(self.cli('close','DEV-1','--dry-run')[0],1)
        self.assertEqual(self.accept('--result','accepted')[0],0)
        before=self.project_fingerprint(); result=self.cli('close','DEV-1','--dry-run'); self.assertEqual(result[0],0,result); self.assertEqual(before,self.project_fingerprint())
        result=self.cli('close','DEV-1'); self.assertEqual(result[0],0,result)
        matches=[p for p in self.archive_dir().rglob('proof.bin')]; self.assertEqual(len(matches),1); self.assertEqual(matches[0].read_bytes(),bytes(range(256)))
        self.assertEqual(self.cli('check','--ready')[0],0)
    def test_nested_symlink_no_write(self):
        self.managed_task(); base=self.root/'artifacts'; base.mkdir(); (base/'link').symlink_to(self.tasks_dir()/'DEV-1.md')
        before=self.project_fingerprint(); result=self.reg('artifacts','--apply'); self.assertEqual(result[0],1,result); self.assertEqual(before,self.project_fingerprint())
    def test_flag_groups_required(self):
        self.managed_task(); p=self.root/'proof'; p.write_text('x')
        before=self.project_fingerprint()
        with self.assertRaises(SystemExit) as caught:
            self.cli('lifecycle','register-material','DEV-1','proof','--id','proof','--role','report','--apply')
        self.assertEqual(caught.exception.code,2); self.assertEqual(before,self.project_fingerprint())
if __name__=='__main__': unittest.main(verbosity=2)
