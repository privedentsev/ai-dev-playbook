import sys,json
sys.path[:0]=['tests','.']
from test_checks_e2e import TrustBoundaryLifecycleAndMigrationE2ETest
x=TrustBoundaryLifecycleAndMigrationE2ETest();x.setUp()
original=x.cli
class Done(Exception): pass
def cli(*args):
 result=original(*args)
 if args[:2]==('lifecycle','handoff') and result[0]==0:
  print('HANDOFF_OK',result)
  paths=list((x.root/'work/checks/handoffs/DEV-09').rglob('report.json'))
  old=json.loads(paths[0].read_text());print('OLD_RESULTS',{k:v.get('result_id') for k,v in old['checks'].items()})
  x.write('src/feature.py',"MODE = 'v2'\n")
  print('RERUN',original('checks','run','DEV-09','--scope','project','--mode','stale'))
  print('CONFIRM',original('checks','confirm','DEV-09','--check','manual-ux','--who','Инженер','--statement','v2','--observation','v2'))
  print('AUDIT_READY_WITHOUT_NEW_HANDOFF',original('verify','DEV-09','--audit-ready'))
  print('REPORT_UNCHANGED',json.loads(paths[0].read_text())==old)
  raise Done()
 return result
x.cli=cli
try:
 try: x.test_ac09_manual_and_task_criteria_unified_gate_and_archive_provenance()
 except Done: pass
finally:x.doCleanups()
