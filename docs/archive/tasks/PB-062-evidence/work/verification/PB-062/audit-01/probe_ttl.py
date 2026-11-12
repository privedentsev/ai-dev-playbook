import sys,unittest,json
from unittest import mock
sys.path[:0]=['tests','ai-dev-playbook/scripts']
from test_checks_trust import command_policy,init_home,verifier_run
import checks_gate, checks_trust
c=unittest.TestCase()
try:
 p=command_policy([sys.executable,'-c','pass'])
 p['environment']['external']=[{'id':'api','freshness':{'kind':'ttl','ttl_seconds':60}}]
 p['checks'][0]['required_environment']=['api']
 proj,home=init_home(c,p)
 r=verifier_run(proj,home)
 print('run_status',r['outcome']['outcomes'])
 with mock.patch.object(checks_trust,'probe',return_value={'configured':True,'capability':'protected','boundary':'separate','notes':[]}):
  v=checks_gate.evaluate(proj.root,p,task_id='DEV-1',verifier_home=home,now='2099-01-01T00:00:00+00:00')
 print(json.dumps({k:v[k] for k in ['ok','covered','uncovered','rejections']},ensure_ascii=False))
finally:c.doCleanups()
