"""Real trusted executions: older pass followed by failure, no history mutation."""
import sys,json,subprocess
from pathlib import Path
ROOT=Path(sys.argv[1]).resolve();sys.path[:0]=[str(ROOT/'ai-dev-playbook/scripts'),str(ROOT/'tests')]
from test_checks_gate import ProtectedLatestOutcomeTest
from test_checks_trust import verifier_run
import checks_verifier,checks_trust
case=ProtectedLatestOutcomeTest();case.setUp()
try:
 a=verifier_run(case.proj,case.home,now='2026-11-17T10:00:00+00:00')
 case.flag.write_text('1')
 b=verifier_run(case.proj,case.home,now='2026-11-17T10:01:00+00:00')
 combined=case.evaluate(now='2026-11-17T10:01:01+00:00')
 project=case.evaluate(scope='project',now='2026-11-17T10:01:01+00:00')
 reference=checks_verifier.gate(case.proj.root,case.home,emit=lambda s:None)
 argv=[sys.executable,str(ROOT/'ai-dev-playbook/scripts/playbook.py'),'--root',str(case.proj.root),'verifier','gate','--home',str(case.home)]
 cli=subprocess.run(argv,capture_output=True,text=True)
 print(json.dumps({'fixture':'Real verifier subprocess commands and signed receipts; external flag changes command outcome without changing candidate. No file loss, no receipt edits, no mocks. Same-user trusted home: capability local.','runs':[{'receipt':o['receipt'],'receipt_verified':checks_verifier.verify(case.home,o['receipt'])['ok'],'candidate_digest':o['candidate_digest'],'results':o['outcome']['results']} for o in [a,b]],'task_gate':combined,'project_gate':project,'verifier_gate':reference,'cli':{'argv':argv,'rc':cli.returncode,'stdout':cli.stdout,'stderr':cli.stderr}},ensure_ascii=False,indent=2))
finally:case.doCleanups()
