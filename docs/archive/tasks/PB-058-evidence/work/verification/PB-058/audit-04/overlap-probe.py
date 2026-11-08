import sys,tempfile,json
from pathlib import Path
sys.path[:0]=[str(Path.cwd()/'ai-dev-playbook/scripts'),str(Path.cwd()/'tests')]
import checks_inventory as ci
import checks_contract as cc
from test_checks_inventory import make_policy,req
with tempfile.TemporaryDirectory() as tmp:
 r=Path(tmp); d=r/'src/payments';d.mkdir(parents=True)
 f=d/'pay.py';f.write_text('old')
 p=make_policy(); c={'id':'c','title':'audit','purpose':'audit','kind':'command','argv':['python3','-V'],'timeout_seconds':5,'modules':['payments'],'inputs':['src/payments','src/payments/pay.py']};p['checks']=[c]
 cc.validate_policy(p,req)
 def race(path):
  h=ci.project_view.digest_file(path)
  if path==f: f.write_text('mutated longer')
  return h
 s=ci.snapshot(r,p,ci.resolved_inputs(p,c),hash_fn=race)
 print(json.dumps({'snapshot':s['entries'],'actual_sha256':ci.project_view.digest_file(f)},ensure_ascii=False))
