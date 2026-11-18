import sys, json, unittest, threading, concurrent.futures, subprocess
from pathlib import Path
ROOT = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(ROOT/'tests'), str(ROOT/'ai-dev-playbook/scripts')]
from test_checks_trust import init_home, command_policy
import checks_verifier as verifier
import checks_gate
case = unittest.TestCase()
try:
    import tempfile
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    flag = Path(tmp.name)/'flag'
    flag.write_text('0')
    policy = command_policy([sys.executable, '-c', 'import sys;from pathlib import Path;sys.exit(int(Path(sys.argv[1]).read_text()))', str(flag)])
    proj, home = init_home(case, policy)
    original = verifier._issue_receipt
    finished = threading.Event()
    release = threading.Event()
    def scheduled_issue(store, subject, **kwargs):
        if threading.current_thread().name.startswith('delayed-pass'):
            finished.set()
            assert release.wait(20), 'issue scheduling timeout'
        return original(store, subject, **kwargs)
    verifier._issue_receipt = scheduled_issue
    with concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='delayed-pass') as pool:
        pending = pool.submit(verifier.run, proj.root, home, emit=lambda _: None)
        assert finished.wait(20), 'execution completion timeout'
        flag.write_text('1')
        failure = verifier.run(proj.root, home, emit=lambda _: None)
        release.set()
        success = pending.result(timeout=20)
    verifier._issue_receipt = original
    assert success['receipt']['subject']['results'][0]['status'] == 'passed'
    assert failure['receipt']['subject']['results'][0]['status'] == 'failed'
    assert success['candidate_digest'] == failure['candidate_digest']
    assert verifier.verify(home, success['receipt'])['ok']
    assert verifier.verify(home, failure['receipt'])['ok']
    v = verifier.gate(proj.root, home, emit=lambda _: None)
    main = checks_gate.evaluate(proj.root, policy, task_id='DEV-1', verifier_home=home)
    cli = subprocess.run([sys.executable, str(ROOT/'ai-dev-playbook/scripts/playbook.py'), '--root', str(proj.root), 'verifier', 'gate', '--home', str(home)], capture_output=True, text=True)
    assert not v['ok'] and v['covered'] == 0 and main['covered'] == [] and cli.returncode == 1
    flag.write_text('0')
    recovery = verifier.run(proj.root, home, emit=lambda _: None)
    restored = verifier.gate(proj.root, home, emit=lambda _: None)
    assert restored['ok'] and restored['covered'] == 1
    print(json.dumps({'scenario':'passed execution finishes first; issue delayed until after failed receipt; genuine subprocess, valid receipts; scheduling wrapper only, no receipt/result mutation', 'passed':success['receipt'], 'failed':failure['receipt'], 'gate':v, 'main_gate':main, 'cli_exit':cli.returncode, 'recovery':restored}, ensure_ascii=False, indent=2))
finally:
    case.doCleanups()
