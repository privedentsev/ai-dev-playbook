import sys,tempfile,json,runpy
from pathlib import Path
repo=Path.cwd();sys.path[:0]=[str(repo/'ai-dev-playbook/scripts'),str(repo/'tests')]
import checks_inventory as ci
import checks_contract as cc
from test_checks_inventory import make_policy,req
# Reproduce original independent probes; rejection is expected for race cases.
runpy.run_path(str(Path(__file__).resolve().parent/'regression-probe.py'),run_name='__main__')
try:runpy.run_path(str(Path(__file__).resolve().parent/'overlap-probe.py'),run_name='__main__')
except ValueError as e:print('A-06 REJECTED',str(e))
# Adjacent case: multiple computed directory scopes must retain union of filters.
with tempfile.TemporaryDirectory() as tmp:
 r=Path(tmp);d=r/'src/payments';(d/'gen').mkdir(parents=True)
 (d/'pay.py').write_text('p');(d/'notes.md').write_text('m')
 p=make_policy();p['modules'][0]['paths']=['src/**/*.py','src/payments/*.md'];p['modules']=p['modules'][:1];p['modules'][0]['shared']=True
 c={'id':'c','title':'audit','purpose':'audit','kind':'command','modules':['payments'],'argv':['python3','-V'],'timeout_seconds':5};p['checks']=[c]
 p['excludes']=[{'pattern':'src/**/gen','reason':'generated','origin':'manual'}]
 cc.validate_policy(p,req)
 def race(path):
  h=ci.project_view.digest_file(path)
  if path.name=='pay.py':(d/'gen/out.py').write_text('late')
  return h
 try:
  s=ci.snapshot(r,p,ci.resolved_inputs(p,c),hash_fn=race)
  print('A-05-overlapping-scopes SUCCESS',json.dumps(s['entries']))
 except ValueError as e:print('A-05-overlapping-scopes REJECTED',str(e))
 try:ci.snapshot(r,p,ci.resolved_inputs(p,c))
 except ValueError as e:print('A-05-overlapping-scopes-next-snapshot REJECTED',str(e))
