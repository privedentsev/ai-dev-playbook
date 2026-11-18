import sys,json,time
sys.path[:0]=['tests','.']
from test_checks_e2e import TrustBoundaryLifecycleAndMigrationE2ETest
class Done(Exception):pass
for attempt in range(4):
 x=TrustBoundaryLifecycleAndMigrationE2ETest();x.setUp();original=x.cli
 def cli(*args):
  if args[:2]==('lifecycle','handoff'):
   # Старт в начале секунды; штатные timestamps и CLI не подменяются.
   time.sleep((1.02-time.time()%1)%1)
   print('ATTEMPT',attempt,'FORCE',original('checks','run','DEV-11','--scope','project','--mode','force'),flush=True)
   result=original(*args);print('HANDOFF',result,flush=True)
   paths=list((x.root/'work/checks/handoffs/DEV-11').rglob('report.json'))
   if paths:
    report=json.loads(paths[-1].read_text());print('REPORT_TIME',report['finished_at'],'STORED',report['checks'].get('other-suite'),flush=True)
    for p in (x.root/'work/checks/results').rglob('*.json'):
     doc=json.loads(p.read_text())
     if doc.get('check_id')=='other-suite':print('OTHER_RESULT',doc['result_id'],doc['finished_at'],flush=True)
   ready=original('verify','DEV-11','--audit-ready');print('READY',ready,flush=True)
   raise Done(ready[0])
  return original(*args)
 x.cli=cli
 try:
  try:x.test_coverage_report_rejects_post_report_rerun_of_not_applicable()
  except Done as d:
   if d.args[0]!=0:break
 finally:x.doCleanups()
