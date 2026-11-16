"""PB-073: поведенческая проба исправлений AUDIT-009 A-01/A-02 через реальный CLI.

Отличие от проб AUDIT-009: те воспроизводят дефект и содержат старые
ожидания; этот скрипт утверждает исправленное поведение и завершается
rc=0 только при успехе. Проект временный, без Git.

Сценарий A-01: команда payment падает дважды — обязательство обязано
исполняться повторно (baseline не продвигается), gate остаётся
закрытым; третья попытка проходит — покрытие восстанавливается.
Сценарий cross-task: вторая задача DEV-02 законно переиспользует
свежий результат без повторного исполнения (provenance/reuse целы).

Запуск из корня репозитория:
    python3 docs/tasks/PB-073-evidence/probe_fixes.py .
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else \
    Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "tests"),
                str(ROOT / "ai-dev-playbook/scripts")]
from test_checks_e2e import NoGitChecksE2ECase
from test_checks_select import make_policy
import checks_gate
import checks_select

case = NoGitChecksE2ECase()
case.setUp()
try:
    root = case.root
    card = case.create_card("DEV-01")
    case.write("src/payments/pay.py", "value = 1\n")
    counter = case.base / "attempts.count"
    # Команда падает первые две попытки и проходит третью: успешный
    # прогон возможен только после двух неуспешных.
    command = (
        "from pathlib import Path; import sys\n"
        f"p = Path({str(counter)!r})\n"
        "n = int(p.read_text()) + 1 if p.exists() else 1\n"
        "p.write_text(str(n))\n"
        "raise SystemExit(0 if n >= 3 else 1)"
    )
    policy = make_policy()
    policy["checks"] = [
        {"id": "payment", "title": "Payment", "purpose": "Must pass",
         "kind": "command", "modules": ["payments"],
         "argv": [sys.executable, "-c", command],
         "timeout_seconds": 5}]
    case.write("checks-policy.json",
               json.dumps(policy, ensure_ascii=False))
    events = []

    def cli(*args):
        proc = subprocess.run(
            [sys.executable,
             str(ROOT / "ai-dev-playbook/scripts/playbook.py"),
             "--root", str(root), *args],
            capture_output=True, text=True)
        events.append({"argv": args, "returncode": proc.returncode,
                       "stdout": proc.stdout, "stderr": proc.stderr})
        return proc

    def gate(task_id, card_path):
        meta = json.loads(
            card_path.with_suffix(".meta.json").read_text())
        # scope=project — как у checks release-ready: весь
        # обязательный набор, а не только diff области задачи.
        return checks_gate.evaluate(
            root, policy, task_id=task_id, scope="project",
            task_content=card_path.read_text(), task_meta=meta)

    assert cli("setup", "apply").returncode == 0, events[-1]

    # --- A-01: failed не теряет обязательство ---
    assert cli("checks", "run", "DEV-01", "--scope", "task",
               "--mode", "stale").returncode == 1, events[-1]
    assert cli("checks", "status", "DEV-01").returncode == 0
    # Повторный stale без новых правок: команда исполняется снова
    # (счётчик растёт), а не отчитывается not_applicable/успехом.
    assert cli("checks", "run", "DEV-01", "--scope", "task",
               "--mode", "stale").returncode == 1, events[-1]
    assert int(counter.read_text()) == 2, \
        f"ожидалось 2 исполнения, факт {counter.read_text()}"
    gate1 = gate("DEV-01", card)
    assert gate1["ok"] is False, gate1
    assert "payment" in gate1["uncovered"], gate1
    cli("checks", "release-ready", "DEV-01")

    # Третья попытка проходит: покрытие и baseline восстанавливаются.
    assert cli("checks", "run", "DEV-01", "--scope", "task",
               "--mode", "stale").returncode == 0, events[-1]
    assert int(counter.read_text()) == 3
    gate2 = gate("DEV-01", card)
    assert gate2["ok"] is True, gate2
    assert "payment" in gate2["covered"], gate2

    # --- Cross-task: законный reuse для DEV-02 без исполнения ---
    card2 = case.create_card("DEV-02")
    assert cli("index").returncode == 0, events[-1]
    assert cli("checks", "run", "DEV-02", "--scope", "task",
               "--mode", "stale").returncode == 0, events[-1]
    assert int(counter.read_text()) == 3, \
        "cross-task reuse не должен повторять исполнение"
    # Порядок истории при равном finished_at: позднейший результат
    # выбирается по run_seq, а не по лексике run_id (A-02).
    results, _notes, _corrupt = __import__(
        "checks_results").load_results(root)
    latest = checks_select.evidence_latest(results)
    seqs = [r["evidence"].get("run_seq")
            for r in results if r["check_id"] == "payment"]
    assert all(type(s) is int for s in seqs), seqs
    assert latest["payment"]["status"] == "passed"
    gate3 = gate("DEV-02", card2)
    assert gate3["ok"] is True, gate3

    print(json.dumps({
        "no_git": not (root / ".git").exists(),
        "attempts": int(counter.read_text()),
        "gate_after_two_failures": {
            "ok": gate1["ok"], "uncovered": gate1["uncovered"]},
        "gate_after_success": {"ok": gate2["ok"],
                               "covered": gate2["covered"]},
        "cross_task": {"dev02_ok": gate3["ok"],
                       "reused_without_execution": True,
                       "run_seqs": seqs},
        "commands": events,
    }, ensure_ascii=False, indent=2))
finally:
    case.doCleanups()
