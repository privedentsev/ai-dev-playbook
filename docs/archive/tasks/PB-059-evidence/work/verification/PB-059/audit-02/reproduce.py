"""Run from repository root; independent re-audit assertions."""
from pathlib import Path
import sys, tempfile
sys.path[:0]=[str(Path.cwd()/'ai-dev-playbook/scripts'),str(Path.cwd()/'tests')]
import checks_graph as cg
import checks_select as cs
from test_checks_select import make_policy,graph_with
p=make_policy()
def edge(c,d):
 return {'consumer':'module:'+c,'dependency':'module:'+d,'kind':'import','source':'analyzer','analyzer_version':'ast-v1','evidence':'fixture:1'}
edges=[edge('checkout','payments'),edge('cart','checkout')]
s=cs.select_checks(p,changed=['src/payments/pay.py'],edges=edges,unknown=[])
assert all(v['status']=='run' for v in s['checks'].values())
assert len(s['chains']['module:cart'])==2
cycle=edges+[edge('payments','cart')]
a=cs.closure_digest(cycle,p['checks'][1]); b=cs.closure_digest(list(reversed(cycle)),p['checks'][1])
assert a==b and a!=cs.closure_digest([],p['checks'][1])
print('A-01/A-02: chain, explain, cycle and stable digest PASS')
for source in ['from importlib import import_module as load\nload(chosen)\n', 'import importlib as il\nil.import_module(chosen)\n']:
 assert cg.analyze_python('src/cart/basket.py',source,{'src/cart/basket.py'})['dynamic']
print('A-03: both dynamic aliases PASS')
with tempfile.TemporaryDirectory() as directory:
 root=Path(directory); f=root/'src/payments/pay.py'; f.parent.mkdir(parents=True); f.write_text('x=123456\n')
 p['fallback']['limits']['max_bytes']=1
 reads=[]
 def reader(rel):
  reads.append(rel); return (root/rel).read_text()
 out=cs.plan(root,p,read_file=reader)
 assert out['bytes_exceeded'] and out['budget_blocked']
 assert out['summary']['run']==0 and out['summary']['blocked']==3
 assert reads==[]
 print('A-04: AST+snapshot budget, blocked before reading PASS',out['summary'])
