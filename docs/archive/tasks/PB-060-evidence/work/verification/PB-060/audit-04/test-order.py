import sys,unittest,types
from pathlib import Path
from unittest import mock
sys.path[:0]=[str(Path.cwd()/'tests'),str(Path.cwd()/'ai-dev-playbook/scripts')]
import test_checks_run,checks_runner
for label,first,second in [('ascending','0','f'),('descending','f','0')]:
 print('UUID ORDER:',label,flush=True)
 suite=unittest.TestLoader().loadTestsFromName('AuditRegressionTest.test_a07_rebuild_preserves_loss_evidence',test_checks_run)
 ids=[types.SimpleNamespace(hex=c*12+"4"+c*3+"8"+c*15) for c in [first,second,'a','b']]
 with mock.patch.object(checks_runner.uuid,'uuid4',side_effect=ids):
  result=unittest.TextTestRunner(verbosity=2).run(suite)
 print('RESULT',label,'success=',result.wasSuccessful(),flush=True)
