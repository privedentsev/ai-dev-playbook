import sys, tempfile, json
from pathlib import Path
ROOT = Path('/Users/privedentsev1/Dev/ai-dev-playbook')
sys.path[:0] = [str(ROOT/'ai-dev-playbook/scripts'), str(ROOT/'tests')]
import checks_inventory as ci
import checks_contract as cc
from test_checks_inventory import make_policy, req

def policy(paths, inputs=None, excludes=None):
    p=make_policy()
    p['modules'][0]['paths']=paths
    c={'id':'c1','title':'c','purpose':'audit','kind':'command','modules':['payments'],'argv':['python3','-V'],'timeout_seconds':5}
    if inputs is not None: c['inputs']=inputs
    p['checks']=[c]
    p['excludes']=excludes or []
    cc.validate_policy(p,req)
    return p,c

with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp); (root/'src/payments').mkdir(parents=True)
    py=root/'src/payments/pay.py'; py.write_text('old')
    note=root/'src/payments/notes.md'; note.write_text('old')
    # A: explicit directory loses bytes through module filter.
    p,c=policy(['src/payments/*.py'], ['src/payments'])
    inp=ci.resolved_inputs(p,c)
    before=ci.snapshot(root,p,inp); note.write_text('new')
    after=ci.snapshot(root,p,inp)
    print('A explicit-directory', json.dumps({'inputs':inp,'notes_entry':after['entries']['path:src/payments']['members']['notes.md'],'diff':ci.diff_snapshots(before,after)}))
    # B: absent path created later during same snapshot.
    p,c=policy(['src/payments/**'], [{'type':'file','path':'a-missing'}, {'type':'file','path':'src/payments/pay.py'}])
    def create_missing(path):
        result=ci.project_view.digest_file(path)
        (root/'a-missing').write_text('appeared')
        return result
    result=ci.snapshot(root,p,c['inputs'],hash_fn=create_missing)
    print('B absent-race',json.dumps({'snapshot':result['entries']['path:a-missing'],'exists':(root/'a-missing').exists()}))
    # C: literal module path treated as directory.
    p,c=policy(['src/payments/pay.py'])
    try: ci.snapshot(root,p,ci.resolved_inputs(p,c))
    except ValueError as e: print('C literal-module',str(e))
    # D: valid selective exclusion accepted by inventory but blocks snapshot.
    p,c=policy(['src/payments/*.py'],excludes=[{'pattern':'src/**/fixtures.bin','reason':'generated','origin':'manual'}])
    (root/'src/payments/fixtures.bin').write_text('generated')
    print('D inventory', ci.build_inventory(root,p)['excluded'])
    try: ci.snapshot(root,p,ci.resolved_inputs(p,c))
    except ValueError as e: print('D selective-exclude',str(e))
