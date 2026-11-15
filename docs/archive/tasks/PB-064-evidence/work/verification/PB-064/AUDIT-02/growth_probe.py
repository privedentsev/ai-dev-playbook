import sys,json
sys.path[:0]=['tests','.']
from test_checks_e2e import TrustBoundaryLifecycleAndMigrationE2ETest
x=TrustBoundaryLifecycleAndMigrationE2ETest();x.setUp()
original=x.cli
write=x.write
class Done(Exception):pass
def custom_write(rel,content):
 if rel=='checks-policy.json':
  p=json.loads(content)
  if p['policy_id']=='lifecycle-policy':
   write('other/service.txt','v1')
   p['modules'].append({'id':'other','title':'Other','subsystem_id':'core','paths':['other/**']})
   p['checks'][0]['modules']=['other']
   content=json.dumps(p)
 return write(rel,content)
x.write=custom_write
def cli(*args):
 result=original(*args)
 if args[:2]==('lifecycle','handoff') and result[0]==0:
  paths=list((x.root/'work/checks/handoffs/DEV-09').rglob('report.json'))
  old=json.loads(paths[0].read_text());print('STORED_CHECKS',old['checks'])
  print('BEFORE',x.count('old_suite'))
  x.write('other/service.txt','v2')
  print('RUN',original('checks','run','DEV-09','--scope','project','--mode','stale'))
  print('AFTER',x.count('old_suite'))
  print('AUDIT_READY_WITHOUT_NEW_HANDOFF',original('verify','DEV-09','--audit-ready'))
  print('REPORT_UNCHANGED',json.loads(paths[0].read_text())==old)
  raise Done()
 return result
x.cli=cli
try:
 try:x.test_ac09_manual_and_task_criteria_unified_gate_and_archive_provenance()
 except Done:pass
finally:x.doCleanups()
