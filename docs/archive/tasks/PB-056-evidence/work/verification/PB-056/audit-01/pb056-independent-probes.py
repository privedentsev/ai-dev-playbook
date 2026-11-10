import sys,json,math,copy,unittest
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_verification_readiness import VerificationReadinessTest
from test_task_cards import BODY,record
from test_documents import tree_fingerprint
from test_close_lifecycle import playbook

class IndependentProbes(VerificationReadinessTest):
    def test_custom_archive_timeout_equality(self):
        self.settings['paths']['archive']='docs/history/tasks'
        self.save_settings()
        (self.root/'docs/history/tasks').mkdir(parents=True)
        result=self.cli('index');self.assertEqual(result[0],0,result)
        report=self.history(duration=30)
        dest=self.root/'docs/history/tasks/DEV-OLD-evidence/old-run/report.json'
        dest.parent.mkdir(parents=True);report.replace(dest);report.parent.rmdir()
        self.assert_early_failure('не превышает прежний успешный прогон')
    def test_malformed_history_shapes_are_ignored(self):
        report=self.history(); baseline=json.loads(report.read_text())
        for value in [[],None,{'schema_version':1},dict(baseline,checks=[None,7,{}]),dict(baseline,inputs_unchanged=False),dict(baseline,checks=[{'status':'passed','argv':self.settings['verification']['checks'][0]['argv'],'duration_seconds':float('nan')}])]:
            report.write_text(json.dumps(value))
            result=self.cli('verify','DEV-1','--preflight')
            self.assertEqual(result[0],0,result)
    def test_markdown_v2_audit_ready_and_journal_integrity(self):
        self.managed_task(meta={'schema_version':1,'card_format_version':2,'relations':[],'materials':[]},body=BODY.replace('- AC-01:','- [ ] AC-01:')+record('EXEC'))
        self.verify()
        path=self.tasks_dir()/'DEV-1.md'
        path.write_text(path.read_text()+record('CHECK')+record('HANDOFF'))
        self.assertEqual(self.handoff()[0],0)
        before=tree_fingerprint(self.root)
        result=self.cli('verify','DEV-1','--audit-ready');self.assertEqual(result[0],0,result)
        self.assertEqual(before,tree_fingerprint(self.root)); self.assertEqual((self.root/'work/count').read_text(),'1')
        path.write_text(path.read_text().replace('Добавлен apply.','Изменён результат.'))
        before=tree_fingerprint(self.root)
        result=self.cli('verify','DEV-1','--audit-ready');self.assertEqual(result[0],1,result)
        self.assertEqual(before,tree_fingerprint(self.root));self.assertEqual((self.root/'work/count').read_text(),'1')

suite=unittest.TestSuite(IndependentProbes(n) for n in ['test_custom_archive_timeout_equality','test_malformed_history_shapes_are_ignored','test_markdown_v2_audit_ready_and_journal_integrity'])
r=unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not r.wasSuccessful())
