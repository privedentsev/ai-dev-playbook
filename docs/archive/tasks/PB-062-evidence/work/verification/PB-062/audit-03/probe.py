import sys,unittest,tempfile,json
from pathlib import Path
from unittest import mock
sys.path[:0]=['tests','ai-dev-playbook/scripts']
from test_checks_trust import command_policy,init_home,verifier_run
import checks_gate,checks_trust,checks_verifier
c=unittest.TestCase()
try:
 f=tempfile.NamedTemporaryFile(delete=False);f.close();flag=Path(f.name);c.addCleanup(lambda:flag.unlink(missing_ok=True));flag.write_text('0')
 p=command_policy([sys.executable,'-c','import sys;from pathlib import Path;sys.exit(int(Path(sys.argv[1]).read_text()))',str(flag)])
 proj,home=init_home(c,p)
 stamp='2026-11-12T12:00:00+03:00'
 a=verifier_run(proj,home,now=stamp)
 flag.write_text('1');b=verifier_run(proj,home,now=stamp)
 def gate():
  with mock.patch.object(checks_trust,'probe',return_value={'configured':True,'capability':'protected','boundary':'separate','notes':[]}):
   v=checks_gate.evaluate(proj.root,p,task_id='DEV-1',verifier_home=home)
  return {k:v[k] for k in ['ok','covered','uncovered','notes','rejections']}
 print('real_outcomes',a['outcome']['outcomes'],b['outcome']['outcomes'],'same_candidate',a['candidate_digest']==b['candidate_digest'])
 print('before_aggregate',json.dumps(gate(),ensure_ascii=False))
 checks_verifier.aggregate(home,proj.root,receipt_ids=[a['receipt']['receipt_id']],now=stamp,emit=lambda s:None)
 print('after_aggregate',json.dumps(gate(),ensure_ascii=False))
finally:c.doCleanups()
