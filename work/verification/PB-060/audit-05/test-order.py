import sys,unittest,types
from pathlib import Path
from unittest import mock
sys.path[:0]=[str(Path.cwd()/'tests'),str(Path.cwd()/'ai-dev-playbook/scripts')]
import test_checks_run,checks_runner
names=['test_a03_corrupt_latest_failed_blocks_reuse','test_a06_deleted_latest_failed_blocks_reuse','test_a07_rebuild_preserves_loss_evidence','test_a07_unrelated_run_keeps_evidence']
all_ok=True
for label,first,second in [('ascending','0','f'),('descending','f','0')]:
 for name in names:
  print('UUID ORDER:',label,'TEST:',name,flush=True)
  suite=unittest.TestLoader().loadTestsFromName('AuditRegressionTest.'+name,test_checks_run)
  ids=[types.SimpleNamespace(hex=c*12+'4'+c*3+'8'+c*15) for c in [first,second,'a','b','c']]
  with mock.patch.object(checks_runner.uuid,'uuid4',side_effect=ids):
   result=unittest.TextTestRunner(verbosity=2).run(suite)
  all_ok=all_ok and result.wasSuccessful()
print('ALL 8 CASES PASSED:',all_ok,flush=True)
sys.exit(0 if all_ok else 1)
