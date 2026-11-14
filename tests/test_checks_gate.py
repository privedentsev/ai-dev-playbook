"""PB-062: единый selector+coverage для handoff/accept/release-ready."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "ai-dev-playbook" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import checks_gate
import checks_runner
import checks_trust
import checks_unittest
import checks_verifier
import lifecycle
from test_checks_run import (run_policy, run_project, execute,
                             canned_collect, green_report)
from test_checks_trust import (command_policy, init_home,
                               verifier_run, forge)


def safe_path(root, rel):
    return Path(root) / rel


def evaluate(proj, policy, **kw):
    content = (proj.root / "docs/tasks/DEV-1.md").read_text()
    return checks_gate.evaluate(
        proj.root, policy, task_id=kw.pop("task_id", "DEV-1"),
        task_content=content, task_meta={}, **kw)


def collect_green(policy, proj):
    """Подмена unittest collect зелёными отчётами по check_id."""
    reports = {
        "payments-tests": green_report(
            ["test_payments.TestPay.test_ok"]),
        "checkout-tests": green_report(
            ["test_checkout.TestOrder.test_ok"]),
        "cart-tests": green_report(
            ["test_cart.TestCart.test_ok"]),
    }

    def collect(root, check, run_dir, timeout, **kw):
        return canned_collect(reports[check["id"]])

    return mock.patch.object(checks_unittest, "collect", collect)


def confirm_manual(proj, policy):
    return checks_runner.confirm(
        proj.root, policy, task_id="DEV-1",
        task_content=(proj.root / "docs/tasks/DEV-1.md").read_text(),
        task_meta={}, check_id="ui-review", who="Инженер",
        statement="просмотрен сценарий", observation="факт",
        emit=lambda s: None)


class LocalGateTest(unittest.TestCase):
    """AC-01/03: локальный режим — свежие результаты закрывают."""

    def setUp(self):
        self.proj = run_project(self)
        self.policy = run_policy()

    def test_uncovered_before_run(self):
        """До прогона область не покрыта — evaluate возвращает ok=false"""
        verdict = evaluate(self.proj, self.policy)
        self.assertFalse(verdict["ok"])
        self.assertIn("payments-tests", verdict["uncovered"])
        self.assertIn("ui-review", verdict["manual"])

    def test_manual_without_confirm_blocks_ok(self):
        """Ручная проверка без confirm блокирует ok даже при зелёном прогоне"""
        with collect_green(self.policy, self.proj):
            execute(self.proj, self.policy)
        verdict = evaluate(self.proj, self.policy)
        self.assertFalse(verdict["ok"])
        self.assertIn("ui-review", verdict["manual"])
        self.assertNotIn("payments-tests", verdict["uncovered"])

    def test_confirm_covers_and_survives_rerun(self):
        """Confirm ручной проверки покрывает её и переживает повторный прогон"""
        with collect_green(self.policy, self.proj):
            execute(self.proj, self.policy)
            confirm_manual(self.proj, self.policy)
            # Повторный run пишет not_run для manual — не должен
            # маскировать подтверждение (AC-03).
            execute(self.proj, self.policy)
        verdict = evaluate(self.proj, self.policy)
        self.assertTrue(verdict["ok"],
                        verdict["uncovered"] + verdict["manual"])
        self.assertEqual(verdict["checks"]["ui-review"]["status"],
                         "covered")

    def test_input_change_uncovers(self):
        """Изменение входа после прогона снимает покрытие проверки"""
        with collect_green(self.policy, self.proj):
            execute(self.proj, self.policy)
        self.proj.write("src/payments/pay.py", "x = 2\n")
        verdict = evaluate(self.proj, self.policy)
        self.assertFalse(verdict["ok"])
        self.assertIn("payments-tests", verdict["uncovered"])

    def test_release_scope_reuses_cross_task(self):
        """AC-02/05: project-область — весь набор; результаты задачи
        без task_projection-входов переиспользуются другой карточкой."""
        with collect_green(self.policy, self.proj):
            execute(self.proj, self.policy)
            confirm_manual(self.proj, self.policy)
        verdict = checks_gate.evaluate(
            self.proj.root, self.policy, task_id="REL-1", scope="project",
            checkpoint="release",
            task_content=(self.proj.root /
                          "docs/tasks/DEV-1.md").read_text(),
            task_meta={})
        for cid in ("payments-tests", "checkout-tests", "cart-tests",
                    "lint", "ui-review"):
            self.assertEqual(
                verdict["checks"][cid]["status"], "covered",
                f"{cid}: {verdict['checks'][cid]}")
        self.assertTrue(verdict["ok"], verdict["uncovered"]
                        + verdict["manual"] + verdict["blocked"])


class ProtectedGateTest(unittest.TestCase):
    """AC-01/08: protected — только квитанции verifier'а."""

    def setUp(self):
        self.policy = command_policy([sys.executable, "-c", "pass"])
        self.proj, self.home = init_home(self, self.policy)

    def evaluate(self, **kw):
        return checks_gate.evaluate(
            self.proj.root, self.policy, task_id="DEV-1",
            task_content="", task_meta={}, verifier_home=self.home,
            **kw)

    def test_local_results_do_not_cover_protected(self):
        """Локальные результаты не покрывают protected-политику"""
        checks_runner.execute(
            self.proj.root, self.policy, task_id="verifier-x",
            scope="project", mode="stale", checkpoint=None,
            task_content="", task_meta={}, emit=lambda s: None)
        verdict = self.evaluate()
        self.assertFalse(verdict["ok"])
        self.assertIn("probe", verdict["uncovered"])

    def test_receipt_covers_but_boundary_required(self):
        """Квитанция verifier покрывает проверку, но граница обязательна"""
        verifier_run(self.proj, self.home)
        # capability=local на этом стенде — покрытие есть, ok=False.
        verdict = self.evaluate()
        self.assertFalse(verdict["ok"])
        self.assertFalse(verdict["protected_confirmed"])
        self.assertIn("probe", verdict["covered"])
        # Доказанная граница — защищённое подтверждение.
        with mock.patch.object(
                checks_trust, "probe",
                return_value={"configured": True,
                              "capability": "protected",
                              "boundary": "separate", "notes": []}):
            verdict = self.evaluate()
        self.assertTrue(verdict["ok"], verdict["rejections"])
        self.assertTrue(verdict["protected_confirmed"])

    def test_forged_receipt_rejected(self):
        """Поддельная квитанция verifier отклоняется evaluate"""
        outcome = verifier_run(self.proj, self.home)
        doc = json.loads(json.dumps(outcome["receipt"]))
        fake = forge(doc)
        target = Path(self.home) / checks_trust.RECEIPTS_DIR / \
            "rc-forged.json"
        target.write_text(json.dumps(fake), encoding="utf-8")
        verdict = self.evaluate()
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("rc-forged" in r
                            for r in verdict["rejections"]))

    def test_stale_candidate_receipt_not_evidence(self):
        """Квитанция прежнего кандидата — история, не отказ и не
        покрытие: обязательство просто остаётся непокрытым."""
        verifier_run(self.proj, self.home)
        self.proj.write("src/payments/pay.py", "x = 99\n")
        verdict = self.evaluate()
        self.assertFalse(verdict["ok"])
        self.assertIn("probe", verdict["uncovered"])
        self.assertNotIn("probe", verdict["covered"])


class ProtectedTtlTest(unittest.TestCase):
    """A-01 аудита PB-062: TTL/always_run внешних зависимостей в
    protected-режиме — просроченная квитанция не покрывает."""

    FUTURE = "2099-01-01T00:00:00+00:00"

    def setUp(self):
        self.policy = command_policy([sys.executable, "-c", "pass"])
        self.policy["environment"]["external"] = [
            {"id": "api",
             "freshness": {"kind": "ttl", "ttl_seconds": 60}}]
        self.policy["checks"][0]["required_environment"] = ["api"]
        self.proj, self.home = init_home(self, self.policy)

    def evaluate(self, **kw):
        return checks_gate.evaluate(
            self.proj.root, self.policy, task_id="DEV-1",
            task_content="", task_meta={}, verifier_home=self.home,
            **kw)

    def test_run_receipt_ttl_expiry_rejected(self):
        """Просроченная по TTL run-квитанция не закрывает обязательство"""
        verifier_run(self.proj, self.home)
        fresh = self.evaluate()
        self.assertIn("probe", fresh["covered"])
        stale = self.evaluate(now=self.FUTURE)
        self.assertFalse(stale["ok"])
        self.assertIn("probe", stale["uncovered"])
        self.assertNotIn("probe", stale["covered"])
        self.assertTrue(any("ttl" in r for r in stale["rejections"]))

    def test_aggregate_receipt_ttl_expiry_rejected(self):
        """Просроченная aggregate-квитанция не закрывает обязательство"""
        outcome = verifier_run(self.proj, self.home)
        checks_verifier.aggregate(
            self.home, self.proj.root,
            receipt_ids=[outcome["receipt"]["receipt_id"]],
            emit=lambda s: None)
        fresh = self.evaluate()
        self.assertIn("probe", fresh["covered"])
        stale = self.evaluate(now=self.FUTURE)
        self.assertFalse(stale["ok"])
        self.assertIn("probe", stale["uncovered"])
        self.assertTrue(any("ttl" in r for r in stale["rejections"]))

    def test_older_expired_does_not_block_fresh(self):
        """A-04: просроченная квитанция не блокирует более свежее
        успешное доказательство; вердикт не зависит от имён и
        наличия проектных копий."""
        verifier_run(self.proj, self.home,
                     now="2026-01-01T00:00:00+00:00")
        verifier_run(self.proj, self.home,
                     now="2026-01-01T00:02:00+00:00")
        verdict = self.evaluate(now="2026-01-01T00:02:01+00:00")
        self.assertIn("probe", verdict["covered"])
        self.assertFalse(any("ttl" in r for r in verdict["rejections"]))
        receipts = self.proj.root / checks_trust.PROJECT_RECEIPTS_DIR
        for path in receipts.glob("*.json"):
            path.rename(path.with_name("x-" + path.name))
        renamed = self.evaluate(now="2026-01-01T00:02:01+00:00")
        self.assertEqual(verdict["covered"], renamed["covered"])
        self.assertEqual(verdict["rejections"], renamed["rejections"])
        for path in receipts.glob("*.json"):
            path.unlink()
        deleted = self.evaluate(now="2026-01-01T00:02:01+00:00")
        self.assertEqual(verdict["covered"], deleted["covered"])
        self.assertEqual(verdict["rejections"], deleted["rejections"])

    def test_older_expired_aggregate_does_not_block_fresh(self):
        """A-04: aggregate подтверждает исходное время исполнения —
        просроченный aggregate не блокирует свежий run."""
        outcome = verifier_run(self.proj, self.home,
                               now="2026-01-01T00:00:00+00:00")
        checks_verifier.aggregate(
            self.home, self.proj.root,
            receipt_ids=[outcome["receipt"]["receipt_id"]],
            now="2026-01-01T00:00:30+00:00", emit=lambda s: None)
        verifier_run(self.proj, self.home,
                     now="2026-01-01T00:02:00+00:00")
        verdict = self.evaluate(now="2026-01-01T00:02:01+00:00")
        self.assertIn("probe", verdict["covered"])
        self.assertFalse(any("ttl" in r for r in verdict["rejections"]))


class ProtectedLatestOutcomeTest(unittest.TestCase):
    """A-03 аудита PB-062: актуальный исход по доверенной истории —
    поздний failed/timeout блокирует старый passed."""

    def setUp(self):
        flag = tempfile.NamedTemporaryFile(delete=False)
        flag.close()
        self.addCleanup(lambda: Path(flag.name).unlink(missing_ok=True))
        self.flag = Path(flag.name)
        self.flag.write_text("0")
        code = ("import sys;from pathlib import Path;"
                "sys.exit(int(Path(sys.argv[1]).read_text()))")
        self.policy = command_policy(
            [sys.executable, "-c", code, str(self.flag)])
        self.proj, self.home = init_home(self, self.policy)

    def evaluate(self, **kw):
        return checks_gate.evaluate(
            self.proj.root, self.policy, task_id="DEV-1",
            task_content="", task_meta={}, verifier_home=self.home,
            **kw)

    def test_later_failed_blocks_earlier_passed(self):
        """Поздний failed блокирует ранний passed в защищённой истории"""
        verifier_run(self.proj, self.home,
                     now="2026-01-01T00:00:00+00:00")
        self.assertIn("probe", self.evaluate()["covered"])
        self.flag.write_text("1")
        verifier_run(self.proj, self.home,
                     now="2026-01-01T00:01:00+00:00")
        verdict = self.evaluate(now="2026-01-01T00:01:01+00:00")
        self.assertIn("probe", verdict["uncovered"])
        self.assertNotIn("probe", verdict["covered"])
        self.assertFalse(verdict["ok"])
        # Удаление проектных копий не воскрешает старый passed.
        receipts = self.proj.root / checks_trust.PROJECT_RECEIPTS_DIR
        for path in receipts.glob("*.json"):
            path.unlink()
        verdict = self.evaluate(now="2026-01-01T00:01:01+00:00")
        self.assertIn("probe", verdict["uncovered"])
        # Успешное восстановление: новый passed — снова покрытие.
        self.flag.write_text("0")
        verifier_run(self.proj, self.home,
                     now="2026-01-01T00:02:00+00:00")
        verdict = self.evaluate(now="2026-01-01T00:02:01+00:00")
        self.assertIn("probe", verdict["covered"])
        self.assertNotIn("probe", verdict["uncovered"])

    def test_aggregate_cannot_override_later_failure(self):
        """Aggregate подтверждает старый passed-результат, но не
        отменяет более поздний failed того же кандидата."""
        outcome = verifier_run(self.proj, self.home,
                               now="2026-01-01T00:00:00+00:00")
        self.flag.write_text("1")
        verifier_run(self.proj, self.home,
                     now="2026-01-01T00:01:00+00:00")
        checks_verifier.aggregate(
            self.home, self.proj.root,
            receipt_ids=[outcome["receipt"]["receipt_id"]],
            now="2026-01-01T00:02:00+00:00", emit=lambda s: None)
        verdict = self.evaluate(now="2026-01-01T00:02:30+00:00")
        self.assertIn("probe", verdict["uncovered"])
        self.assertNotIn("probe", verdict["covered"])

    def test_same_timestamp_aggregate_keeps_failed_latest(self):
        """A-05 аудита PB-062: при равных finished_at порядок задаёт
        доверенная последовательность исходных запусков — aggregate
        старого passed не отменяет поздний failed и не становится
        новым исполнением."""
        stamp = "2026-01-01T00:00:00+00:00"
        outcome = verifier_run(self.proj, self.home, now=stamp)
        self.flag.write_text("1")
        verifier_run(self.proj, self.home, now=stamp)
        checks_verifier.aggregate(
            self.home, self.proj.root,
            receipt_ids=[outcome["receipt"]["receipt_id"]],
            now=stamp, emit=lambda s: None)
        verdict = self.evaluate(now="2026-01-01T00:00:30+00:00")
        self.assertIn("probe", verdict["uncovered"])
        self.assertNotIn("probe", verdict["covered"])
        # Восстановление — только новым реальным passed (тоже с
        # совпадающей меткой: порядок восстанавливает exec_seq).
        self.flag.write_text("0")
        verifier_run(self.proj, self.home, now=stamp)
        verdict = self.evaluate(now="2026-01-01T00:00:30+00:00")
        self.assertIn("probe", verdict["covered"])


class LifecycleGateTest(unittest.TestCase):
    """AC-01: lifecycle использует тот же расчёт; без policy — legacy."""

    def setUp(self):
        self.proj = run_project(self)
        (self.proj.root / "ai-dev-playbook").mkdir(parents=True,
                                                    exist_ok=True)

    def write_link(self, policy=None):
        link = {"schema_version": 1, "name": "x"}
        if policy is not None:
            link["checks_policy"] = "checks-policy.json"
            (self.proj.root / "checks-policy.json").write_text(
                json.dumps(policy, ensure_ascii=False))
        (self.proj.root / "ai-dev-playbook" / "project.json").write_text(
            json.dumps(link))

    def test_no_policy_returns_none(self):
        """Без checks_policy lifecycle возвращает None — legacy-контракт"""
        self.write_link()
        verdict = lifecycle._checks_gate(
            self.proj.root, "DEV-1", "docs/tasks/DEV-1.md",
            (self.proj.root / "docs/tasks/DEV-1.md").read_text(), {},
            safe_path, lambda c, m: (_ for _ in ()).throw(
                ValueError(m)) if not c else None)
        self.assertIsNone(verdict)

    def test_policy_requires_coverage(self):
        """С checks_policy lifecycle требует покрытия области задачи"""
        self.write_link(run_policy())
        text = (self.proj.root / "docs/tasks/DEV-1.md").read_text()
        with self.assertRaises(ValueError):
            lifecycle._checks_gate(
                self.proj.root, "DEV-1", "docs/tasks/DEV-1.md", text,
                {}, safe_path, lambda c, m: (_ for _ in ()).throw(
                    ValueError(m)) if not c else None)


if __name__ == "__main__":
    unittest.main()
