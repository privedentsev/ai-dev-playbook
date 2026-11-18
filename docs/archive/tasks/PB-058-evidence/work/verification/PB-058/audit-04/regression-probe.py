import sys,tempfile,json
from pathlib import Path
root_repo=Path.cwd()
sys.path[:0]=[str(root_repo/'ai-dev-playbook/scripts'),str(root_repo/'tests')]
import checks_inventory as ci
import checks_contract as cc
from test_checks_inventory import make_policy,req

def policy(paths,inputs=None,excludes=None):
 p=make_policy();p['modules'][0]['paths']=paths
 c={'id':'c1','title':'audit','purpose':'audit','kind':'command','modules':['payments'],'argv':['python3','-V'],'timeout_seconds':5}
 if inputs is not None:c['inputs']=inputs
 p['checks']=[c];p['excludes']=excludes or []
 cc.validate_policy(p,req)
 return p,c

def case(name,fn):
 try: print(name,json.dumps(fn(),ensure_ascii=False))
 except ValueError as e: print(name,'REJECTED',str(e))

with tempfile.TemporaryDirectory() as tmp:
 r=Path(tmp); d=r/'src/payments';d.mkdir(parents=True)
 (d/'pay.py').write_text('old');(d/'notes.md').write_text('old')
 p,c=policy(['src/payments/*.py'],['src/payments']); inp=ci.resolved_inputs(p,c)
 b=ci.snapshot(r,p,inp);(d/'notes.md').write_text('new')
 case('A-01',lambda:ci.diff_snapshots(b,ci.snapshot(r,p,inp)))
 p,c=policy(['src/payments/**'],[{'type':'file','path':'a-missing'},{'type':'file','path':'src/payments/pay.py'}])
 def create(path):
  h=ci.project_view.digest_file(path);(r/'a-missing').write_text('late');return h
 case('A-02',lambda:ci.snapshot(r,p,c['inputs'],hash_fn=create)['entries'])
 p,c=policy(['src/payments/pay.py'])
 case('A-03',lambda:ci.snapshot(r,p,ci.resolved_inputs(p,c))['entries'])
 p,c=policy(['src/payments/*.py'],excludes=[{'pattern':'src/**/fixtures.bin','reason':'generated','origin':'manual'}])
 (d/'fixtures.bin').write_text('f')
 case('A-04',lambda:ci.snapshot(r,p,ci.resolved_inputs(p,c))['entries'])
 # New race in excluded subtree inspected by A-04 fix.
 (d/'gen').mkdir()
 p,c=policy(['src/payments/**/*.py'],excludes=[{'pattern':'src/**/gen','reason':'generated','origin':'manual'}])
 def populate(path):
  h=ci.project_view.digest_file(path);(d/'gen/out.py').write_text('new input');return h
 case('excluded-subtree-race',lambda:ci.snapshot(r,p,ci.resolved_inputs(p,c),hash_fn=populate)['entries'])
 case('excluded-subtree-after-race',lambda:ci.snapshot(r,p,ci.resolved_inputs(p,c))['entries'])
 # Initial absent ancestor is overwritten by later resolve of same prefix.
 p,c=policy(['src/payments/**'])
 inputs=[{'type':'directory','path':'missing/x'},{'type':'directory','path':'src/payments'},{'type':'file','path':'missing/y'}]
 cc._validate_inputs(inputs,'inputs',req)
 def parent(path):
  h=ci.project_view.digest_file(path);(r/'missing').mkdir(exist_ok=True);return h
 case('ancestor-overwrite-race',lambda:ci.snapshot(r,p,inputs,hash_fn=parent)['entries'])
