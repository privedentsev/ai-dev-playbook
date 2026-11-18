import sys,unittest,json,datetime,tempfile
from pathlib import Path
from unittest import mock
sys.path[:0]=['tests','ai-dev-playbook/scripts']
from test_checks_trust import command_policy,init_home,verifier_run
import checks_gate,checks_trust
BOUNDARY={'configured':True,'capability':'protected','boundary':'separate','notes':[]}
def evaluate(proj,p,home,now):
 with mock.patch.object(checks_trust,'probe',return_value=BOUNDARY):
  v=checks_gate.evaluate(proj.root,p,task_id='DEV-1',verifier_home=home,now=now)
 return {k:v[k] for k in ['ok','covered','uncovered','rejections']}
c=unittest.TestCase()
try:
 p=command_policy([sys.executable,'-c','pass'])
 p['environment']['external']=[{'id':'api','freshness':{'kind':'ttl','ttl_seconds':60}}]
 p['checks'][0]['required_environment']=['api']
 proj,home=init_home(c,p)
 r1=verifier_run(proj,home,now='2026-01-01T00:00:00+00:00')
 d=proj.root/checks_trust.PROJECT_RECEIPTS_DIR
 (d/(r1['receipt']['receipt_id']+'.json')).rename(d/'000-old.json')
 print('expired_only',evaluate(proj,p,home,'2026-01-01T00:02:00+00:00'))
 r2=verifier_run(proj,home,now='2026-01-01T00:02:00+00:00')
 (d/(r2['receipt']['receipt_id']+'.json')).rename(d/'999-new.json')
 print('runs',r1['outcome']['outcomes'],r2['outcome']['outcomes'])
 print('old_then_fresh',evaluate(proj,p,home,'2026-01-01T00:02:01+00:00'))
 (d/'000-old.json').rename(d/'zzz-old.json')
 print('fresh_then_old',evaluate(proj,p,home,'2026-01-01T00:02:01+00:00'))
finally:c.doCleanups()
c=unittest.TestCase()
try:
 flag=tempfile.NamedTemporaryFile(delete=False); flag.close(); flagpath=Path(flag.name);c.addCleanup(lambda:flagpath.unlink(missing_ok=True));flagpath.write_text('0')
 code="import sys;from pathlib import Path;sys.exit(int(Path(sys.argv[1]).read_text()))"
 p=command_policy([sys.executable,'-c',code,str(flagpath)])
 proj,home=init_home(c,p)
 r1=verifier_run(proj,home,now='2026-01-01T00:00:00+00:00')
 flagpath.write_text('1')
 r2=verifier_run(proj,home,now='2026-01-01T00:01:00+00:00')
 print('passed_then_failed_runs',r1['outcome']['outcomes'],r2['outcome']['outcomes'])
 print('same_candidate',r1['candidate_digest']==r2['candidate_digest'])
 print('passed_then_failed_gate',evaluate(proj,p,home,'2026-01-01T00:01:01+00:00'))
finally:c.doCleanups()
