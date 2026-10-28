"""PB-023 review 4: fixed probes plus a real competing standalone restore."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile

out=io.StringIO()
with contextlib.redirect_stdout(out):
    ns=runpy.run_path(str(Path(__file__).with_name('review3_probes.py')))
# review3 exports the review2 namespace as ns.
helpers=ns['ns']
f=helpers['fo']
results={'previous_probes':json.loads(out.getvalue()),'independent_checks':[]}
with tempfile.TemporaryDirectory(prefix='pb023-review4-') as folder:
    root=Path(folder).resolve(); (root/'docs').mkdir(); (root/'docs/a.md').write_bytes(b'OLD')
    applied=helpers['apply'](root)
    op=root/applied['op_dir']
    original=f.find_completed
    calls=[]
    def overlap(project, plan_id):
        prior=original(project,plan_id)
        # Actual standalone restore attempts to enter precisely after receipt read.
        r=subprocess.run([sys.executable,str(op/'restore.py')],capture_output=True,text=True)
        assert r.returncode != 0 and 'блокировку записи' in r.stderr, (r.returncode,r.stderr)
        calls.append('standalone_restore_refused_by_gate')
        return prior
    f.find_completed=overlap
    try: repeated=helpers['apply'](root)
    finally: f.find_completed=original
    assert repeated['status']=='already_completed' and calls
    assert (root/'docs/a.md').read_bytes()==b'NEW'
    results['independent_checks'].append({'case':'receipt_read_vs_standalone_restore','status':repeated['status'],'restore':'refused_by_gate','content':'NEW','passed':True})
    # Even a previously completed plan cannot bypass either pending subsystem.
    for marker_name in (f.PENDING_FILE,f.UPDATE_PENDING_FILE):
        marker=root/marker_name; marker.parent.mkdir(parents=True,exist_ok=True)
        marker.write_text(json.dumps({'op_id':'audit-pending','op_dir':'work/lifecycle/audit-pending'}))
        try:
            try: helpers['apply'](root)
            except ValueError as e: assert 'Незаверш' in str(e),str(e)
            else: raise AssertionError('completed receipt bypassed pending')
            assert marker.is_file() and (root/'docs/a.md').read_bytes()==b'NEW'
        finally: marker.unlink()
        results['independent_checks'].append({'case':'completed_plan_with_pending','marker':marker_name,'refused':True})
print(json.dumps(results,ensure_ascii=False,indent=2))
