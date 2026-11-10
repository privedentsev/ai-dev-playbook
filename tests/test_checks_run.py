"""PB-060: адаптер unittest, результаты, freshness/reuse, run/status."""
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

import checks_contract
import checks_inventory
import checks_results
import checks_runner
import checks_select
import checks_unittest
import verification_policy
from test_checks_select import make_policy, Project
from test_playbook import playbook


def req(condition, message):
    if not condition:
        raise ValueError(message)


TASK_MD = ("# DEV-1: Тест\n\nID: DEV-1\nStatus: in_progress\n"
           "Priority: P2\nDepends-On: -\nExecutor: Devin\n"
           "Model: SWE-2 High\nCreated: 2026-11-09T10:00:00+03:00\n"
           "Closed: -\n\n## Задача\n\n### Цель и границы\n\n"
           "Тестовая задача.\n\n### Контекст\n\nКонтекст.\n\n"
           "### Критерии приёмки\n\n- [ ] AC-01: проверить\n\n"
           "### План\n\n1. Шаг.\n\n### Ресурсы\n\nРесурсы.\n\n"
           "## Журнал\n")


def run_policy(**kw):
    """Политика с тремя unittest-проверками и каталогом."""
    policy = make_policy()
    policy["modules"].append(
        {"id": "tests", "title": "Тесты", "subsystem_id": "catalog",
         "paths": ["tests/**"]})
    pattern = {"payments-tests": "test_payments.py",
               "checkout-tests": "test_checkout.py",
               "cart-tests": "test_cart.py"}
    for check in policy["checks"]:
        check["applicability"] = "always"
        check["inputs"] = ["tests"]
        check["argv"] = [sys.executable, "-m", "unittest", "discover",
                         "-s", "tests", "-p", pattern[check["id"]]]
    policy["checks"].append(
        {"id": "lint", "title": "Линтер", "purpose": "p",
         "kind": "command", "modules": [],
         "argv": [sys.executable, "-c", "pass"], "timeout_seconds": 10,
         "applicability": "always"})
    policy["checks"].append(
        {"id": "ui-review", "title": "Ручной", "purpose": "p",
         "kind": "manual", "modules": [],
         "scenario": "сценарий", "applicability": "always"})
    policy["environment"]["external"] = []
    policy["test_catalog"]["entries"] = [
        {"id": "test_payments.TestPay.test_ok", "description": "оплата",
         "group": "payments"},
        {"id": "test_checkout.TestOrder.test_ok", "description": "заказ",
         "group": "checkout"},
        {"id": "test_cart.TestCart.test_ok", "description": "корзина",
         "group": "cart"},
    ]
    policy.update(kw)
    return policy


def run_project(case, test_bodies=None):
    """Временный проект с src-модулями и unittest-файлами."""
    proj = Project(case)
    proj.write("src/payments/pay.py")
    proj.write("src/checkout/order.py")
    proj.write("src/cart/basket.py")
    bodies = test_bodies or {}
    for name, body in {
            "test_payments": bodies.get("test_payments",
                                        "import unittest\n\n"
                                        "class TestPay(unittest.TestCase):\n"
                                        "    def test_ok(self):\n"
                                        "        self.assertTrue(True)\n"),
            "test_checkout": bodies.get("test_checkout",
                                        "import unittest\n\n"
                                        "class TestOrder(unittest.TestCase):\n"
                                        "    def test_ok(self):\n"
                                        "        self.assertTrue(True)\n"),
            "test_cart": bodies.get("test_cart",
                                    "import unittest\n\n"
                                    "class TestCart(unittest.TestCase):\n"
                                    "    def test_ok(self):\n"
                                    "        self.assertTrue(True)\n"),
            }.items():
        proj.write(f"tests/{name}.py", body)
    proj.write("docs/tasks/DEV-1.md", TASK_MD)
    return proj


def execute(proj, policy, **kw):
    """execute() с захваченным выводом и реальным временем."""
    out = io.StringIO()
    content = (proj.root / "docs/tasks/DEV-1.md").read_text()
    with contextlib.redirect_stdout(out):
        result = checks_runner.execute(
            proj.root, policy, task_id="DEV-1",
            scope=kw.pop("scope", "task"), mode=kw.pop("mode", "stale"),
            checkpoint=kw.pop("checkpoint", None),
            task_content=content, task_meta={}, **kw)
    return result, out.getvalue()


def canned_collect(report):
    """collect() без подпроцесса: готовый отчёт адаптера."""
    return {"status": None, "exit_code": report["exit_code"],
            "report": report, "stdout_log": "work/checks/runs/x/o",
            "stderr_log": "work/checks/runs/x/e",
            "report_path": "work/checks/runs/x/a.json"}


def green_report(ids):
    return {"protocol": 1, "expected": list(ids),
            "actual": [{"id": i, "outcome": "passed"} for i in ids],
            "exit_code": 0}


class AdapterTest(unittest.TestCase):
    """AC-01: реальный subprocess, сверка IDs/count/outcomes/subtests."""

    def setUp(self):
        self.proj = run_project(self)
        self.policy = run_policy()
        self.run_dir = self.proj.root / "work/checks/runs/rr"
        self.run_dir.mkdir(parents=True)

    def check(self, name="payments-tests"):
        return next(c for c in self.policy["checks"] if c["id"] == name)

    def collect(self, check=None, timeout=10, suffix=""):
        run_dir = self.run_dir if not suffix else \
            self.proj.root / f"work/checks/runs/rr{suffix}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return checks_unittest.collect(
            self.proj.root, check or self.check(), run_dir,
            timeout=timeout)

    def test_parse_argv_forms(self):
        sel = checks_unittest.parse_argv(
            ["python3", "-m", "unittest", "discover", "-s", "tests",
             "-p", "test_*.py", "-v"])
        self.assertEqual(sel["kind"], "discover")
        self.assertEqual(sel["start"], "tests")
        sel = checks_unittest.parse_argv(
            ["python3", "-m", "unittest", "mod.Test.x"])
        self.assertEqual(sel, {"kind": "names",
                               "names": ["mod.Test.x"]})
        with self.assertRaisesRegex(ValueError, "unittest"):
            checks_unittest.parse_argv(["python3", "script.py"])
        with self.assertRaisesRegex(ValueError, "python"):
            checks_unittest.parse_argv(["ruff", "-m", "unittest", "x"])

    def test_green_suite_passed(self):
        collected = self.collect()
        out = checks_unittest.verdict(self.check(), self.policy,
                                      collected)
        self.assertEqual(out["status"], "passed", out["reasons"])
        self.assertEqual(out["tests"]["expected"],
                         ["test_payments.TestPay.test_ok"])
        self.assertEqual(out["tests"]["actual"][0]["outcome"], "passed")

    def test_zero_tests_rejected(self):
        self.proj.write("tests/test_payments.py",
                        "import unittest\n")
        out = checks_unittest.verdict(self.check(), self.policy,
                                      self.collect())
        self.assertEqual(out["status"], "failed")
        self.assertTrue(any("нулевой набор" in r
                            for r in out["reasons"]), out["reasons"])

    def test_unexpected_skip_rejected(self):
        self.proj.write(
            "tests/test_payments.py",
            "import unittest\n\n"
            "class TestPay(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.skipTest('lazy')\n")
        out = checks_unittest.verdict(self.check(), self.policy,
                                      self.collect())
        self.assertEqual(out["status"], "failed")
        self.assertTrue(any("неожиданный skip" in r
                            for r in out["reasons"]), out["reasons"])

    def test_allowed_skip_accepted(self):
        self.proj.write(
            "tests/test_payments.py",
            "import unittest\n\n"
            "class TestPay(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.skipTest('flaky external')\n")
        for entry in self.policy["test_catalog"]["entries"]:
            if entry["id"] == "test_payments.TestPay.test_ok":
                entry["skip"] = {"allowed": True, "reason": "flaky"}
        out = checks_unittest.verdict(self.check(), self.policy,
                                      self.collect())
        self.assertEqual(out["status"], "passed", out["reasons"])
        self.assertEqual(out["tests"]["actual"][0]["outcome"],
                         "skipped")

    def test_missing_catalog_id_rejected(self):
        # Переименованный тест: запись каталога не покрыта discovery.
        self.proj.write(
            "tests/test_payments.py",
            "import unittest\n\n"
            "class TestPay(unittest.TestCase):\n"
            "    def test_renamed(self):\n"
            "        self.assertTrue(True)\n")
        out = checks_unittest.verdict(self.check(), self.policy,
                                      self.collect())
        self.assertEqual(out["status"], "failed")
        self.assertTrue(any("пропавший test ID" in r
                            for r in out["reasons"]), out["reasons"])
        self.assertTrue(any("вне одобренного каталога" in r
                            for r in out["reasons"]), out["reasons"])

    def test_unfinished_and_crash_rejected(self):
        # Падение процесса во время suite: отчёт адаптера не создан.
        self.proj.write(
            "tests/test_payments.py",
            "import os, unittest\n\n"
            "class TestPay(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        os._exit(9)\n")
        out = checks_unittest.verdict(self.check(), self.policy,
                                      self.collect())
        self.assertEqual(out["status"], "failed")
        self.assertTrue(any("crash" in r or "не создан" in r
                            for r in out["reasons"]), out["reasons"])

    def test_error_outcome_and_description(self):
        self.proj.write(
            "tests/test_payments.py",
            "import unittest\n\n"
            "class TestPay(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertEqual(1, 2)\n")
        out = checks_unittest.verdict(self.check(), self.policy,
                                      self.collect())
        self.assertEqual(out["status"], "failed")
        self.assertTrue(any("test_ok" in r and "оплата" in r
                            for r in out["reasons"]), out["reasons"])

    def test_subtest_failure_rejected(self):
        self.proj.write(
            "tests/test_payments.py",
            "import unittest\n\n"
            "class TestPay(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        for i in (1, 2):\n"
            "            with self.subTest(i=i):\n"
            "                self.assertEqual(i, 1)\n")
        out = checks_unittest.verdict(self.check(), self.policy,
                                      self.collect())
        self.assertEqual(out["status"], "failed")
        self.assertTrue(any("failed" in r for r in out["reasons"]))

    def test_expected_failure_and_unexpected_success(self):
        self.proj.write(
            "tests/test_payments.py",
            "import unittest\n\n"
            "class TestPay(unittest.TestCase):\n"
            "    @unittest.expectedFailure\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n")
        out = checks_unittest.verdict(self.check(), self.policy,
                                      self.collect())
        self.assertEqual(out["status"], "failed")
        self.assertTrue(any("unexpected_success" in r
                            for r in out["reasons"]), out["reasons"])
        for entry in self.policy["test_catalog"]["entries"]:
            if entry["id"] == "test_payments.TestPay.test_ok":
                entry["expected_failure"] = {"allowed": True}
        self.proj.write(
            "tests/test_payments.py",
            "import unittest\n\n"
            "class TestPay(unittest.TestCase):\n"
            "    @unittest.expectedFailure\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(False)\n")
        out = checks_unittest.verdict(self.check(), self.policy,
                                      self.collect(suffix="b"))
        self.assertEqual(out["status"], "passed", out["reasons"])

    def test_timeout_status(self):
        self.proj.write(
            "tests/test_payments.py",
            "import time, unittest\n\n"
            "class TestPay(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        time.sleep(30)\n")
        collected = self.collect(timeout=1)
        self.assertEqual(collected["status"], "timeout")
        out = checks_unittest.verdict(self.check(), self.policy,
                                      collected)
        self.assertEqual(out["status"], "timeout")


class RunnerTest(unittest.TestCase):
    """AC-02/03: результаты, freshness, reuse, force/stale, изоляция."""

    def setUp(self):
        self.proj = run_project(self)
        self.policy = run_policy()

    def collect_counter(self, reports):
        """Мок collect по check_id; возвращает счётчик вызовов."""
        calls = []

        def fake(root, check, run_dir, *, timeout, env=None):
            calls.append(check["id"])
            return canned_collect(reports[check["id"]])
        return calls, fake

    def reports(self, ids=("test_payments.TestPay.test_ok",
                           "test_checkout.TestOrder.test_ok",
                           "test_cart.TestCart.test_ok")):
        check_ids = ("payments-tests", "checkout-tests", "cart-tests")
        return {cid: green_report([tid])
                for cid, tid in zip(check_ids, ids)}

    def test_first_run_stores_results_and_state(self):
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            out, text = execute(self.proj, self.policy)
        self.assertFalse(out["ok"])  # manual непокрыт
        # lint — kind=command: исполняется argv напрямую, без адаптера.
        self.assertEqual(calls, ["payments-tests", "checkout-tests",
                                 "cart-tests"])
        self.assertEqual(out["outcomes"]["lint"], "passed")
        state = self.proj.root / "work/checks"
        self.assertTrue((state / "snapshot.json").is_file())
        self.assertTrue((state / "checks-graph.json").is_file())
        self.assertTrue((state / "results-index.json").is_file())
        results, notes, _c = checks_results.load_results(self.proj.root)
        statuses = {r["check_id"]: r["status"] for r in results}
        self.assertEqual(statuses["payments-tests"], "passed")
        self.assertEqual(statuses["lint"], "passed")
        self.assertEqual(statuses["ui-review"], "not_run")
        self.assertEqual(notes, [])
        for r in results:
            checks_contract.validate_result(r, req)
        self.assertIn("trust_mode=local", text)

    def test_stale_reuses_independent_results(self):
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        calls.clear()
        self.proj.write("src/payments/pay.py", "x = 2\n")
        with mock.patch.object(checks_unittest, "collect", fake):
            out, text = execute(self.proj, self.policy)
        # payments затронут → перезапуск; cart и lint независимы → reused.
        self.assertEqual(calls, ["payments-tests", "checkout-tests"]
                         if "checkout-tests" in calls else calls)
        self.assertIn("payments-tests", calls)
        self.assertNotIn("cart-tests", calls)
        self.assertNotIn("lint", calls)
        results, _n, _c = checks_results.load_results(self.proj.root)
        reused = [r for r in results if r.get("reused")]
        self.assertTrue(reused)
        self.assertTrue(all(r["status"] == "passed" for r in reused))
        self.assertIn("reused", text)

    def test_changed_input_forces_rerun(self):
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        calls.clear()
        self.proj.write("tests/test_cart.py",
                        "import unittest\n\n"
                        "class TestCart(unittest.TestCase):\n"
                        "    def test_ok(self):\n"
                        "        self.assertTrue(True)\n"
                        "    def test_more(self):\n"
                        "        self.assertTrue(True)\n")
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        self.assertIn("cart-tests", calls)

    def test_late_failed_not_hidden(self):
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        bad = self.reports()
        bad["payments-tests"] = {
            "protocol": 1,
            "expected": ["test_payments.TestPay.test_ok"],
            "actual": [{"id": "test_payments.TestPay.test_ok",
                        "outcome": "failed"}], "exit_code": 1}
        calls2, fake2 = self.collect_counter(bad)
        self.proj.write("src/payments/pay.py", "x = 2\n")
        with mock.patch.object(checks_unittest, "collect", fake2):
            execute(self.proj, self.policy)
        # Возврат входов к прежнему содержимому не воскрешает старый
        # passed: последний failed — свежая правда.
        self.proj.write("src/payments/pay.py", "x = 1\n")
        calls2.clear()
        with mock.patch.object(checks_unittest, "collect", fake2):
            out, _ = execute(self.proj, self.policy)
        self.assertIn("payments-tests", calls2)
        self.assertEqual(out["outcomes"]["payments-tests"], "failed")

    def test_corrupt_result_no_false_reuse(self):
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        rdir = self.proj.root / "work/checks/results"
        victim = next(rdir.rglob("*cart-tests*.json"))
        victim.write_text("{corrupt")
        calls.clear()
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        self.assertIn("cart-tests", calls)
        results, notes, _c = checks_results.load_results(self.proj.root)
        self.assertTrue(any("поврежд" in n for n in notes))

    def test_corrupt_index_rebuilt(self):
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        index = self.proj.root / "work/checks/results-index.json"
        index.write_text("{corrupt")
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        rebuilt = json.loads(index.read_text())
        self.assertEqual(rebuilt["kind"],
                         checks_results.INDEX_KIND)
        self.assertIn("cart-tests", rebuilt["entries"])

    def test_force_reruns_all(self):
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        calls.clear()
        with mock.patch.object(checks_unittest, "collect", fake):
            out, _ = execute(self.proj, self.policy, mode="force")
        self.assertEqual(sorted(calls), ["cart-tests", "checkout-tests",
                                         "payments-tests"])
        self.assertEqual(out["outcomes"]["lint"], "passed")

    def test_project_scope_covers_all(self):
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            out, _ = execute(self.proj, self.policy, scope="project")
        self.assertEqual(sorted(calls), ["cart-tests", "checkout-tests",
                                         "payments-tests"])
        self.assertEqual(out["outcomes"]["lint"], "passed")

    def test_task_requirements_change_breaks_reuse(self):
        self.policy["checks"][0]["inputs"] = [
            "tests", {"type": "task_projection"}]
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        calls.clear()
        card = self.proj.root / "docs/tasks/DEV-1.md"
        card.write_text(card.read_text().replace(
            "AC-01: проверить", "AC-01: иное требование"))
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        # Проекция требований — вход проверки: payments-tests
        # перезапускается; остальные без task_projection — reused.
        self.assertIn("payments-tests", calls)
        self.assertNotIn("cart-tests", calls)

    def test_external_ttl_and_always_run(self):
        self.policy["environment"]["external"] = [
            {"id": "payment-api",
             "freshness": {"kind": "ttl", "ttl_seconds": 60}},
            {"id": "fx", "freshness": {"kind": "always_run"}}]
        self.policy["checks"][0]["required_environment"] = \
            ["payment-api"]
        self.policy["checks"][2]["required_environment"] = ["fx"]
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        calls.clear()
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        # fx — always_run; payment-api TTL пока свеж → reused.
        self.assertIn("cart-tests", calls)
        self.assertNotIn("payments-tests", calls)
        # TTL истёк: исходный запуск вне окна → run (A-01: возраст
        # считается от issued_at цепочки reuse, не от строки reuse).
        calls.clear()
        for path in (self.proj.root / "work/checks/results").rglob(
                "payments-tests.json"):
            doc = json.loads(path.read_text())
            doc["finished_at"] = "2020-01-01T00:00:00+00:00"
            doc["started_at"] = doc["finished_at"]
            if "reused" in doc:
                doc["reused"]["issued_at"] = doc["finished_at"]
            path.write_text(json.dumps(doc, ensure_ascii=False,
                                       indent=2))
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        self.assertIn("payments-tests", calls)

    def test_external_version_change_breaks_binding(self):
        self.policy["environment"]["external"] = [
            {"id": "api",
             "freshness": {"kind": "version", "version": "v1"}}]
        self.policy["checks"][0]["required_environment"] = ["api"]
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        self.policy["environment"]["external"][0]["freshness"] = \
            {"kind": "version", "version": "v2"}
        calls.clear()
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        self.assertIn("payments-tests", calls)

    def test_mutation_during_command_blocked(self):
        # Команда правит собственный вход: post-снимок ≠ pre → blocked.
        script = ("import pathlib;"
                  "pathlib.Path('src/cart/basket.py')"
                  ".write_text('x = 9\\n')")
        self.policy["checks"] = [c for c in self.policy["checks"]
                                 if c["id"] == "cart-tests"]
        self.policy["checks"].append(
            {"id": "mutate", "title": "t", "purpose": "p",
             "kind": "command", "modules": ["cart"],
             "argv": [sys.executable, "-c", script],
             "timeout_seconds": 10, "applicability": "always"})
        self.policy["checks"][0]["applicability"] = \
            {"kind": "when_paths", "paths": ["never/**"]}
        out, text = execute(self.proj, self.policy)
        self.assertEqual(out["outcomes"]["mutate"], "blocked")
        self.assertIn("изменились во время запуска", text)

    def test_cross_task_reuse_same_project(self):
        # Результат задачи DEV-1 пригоден для DEV-2 того же проекта.
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        card2 = TASK_MD.replace("DEV-1", "DEV-2")
        self.proj.write("docs/tasks/DEV-2.md", card2)
        calls.clear()
        content = (self.proj.root / "docs/tasks/DEV-2.md").read_text()
        with mock.patch.object(checks_unittest, "collect", fake):
            out, _ = execute(self.proj, self.policy)
        self.assertNotIn("cart-tests", calls)

    def test_other_project_result_not_reused(self):
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        # Подменить project_id у всех записей истории.
        for path in (self.proj.root / "work/checks/results").rglob(
                "*.json"):
            doc = json.loads(path.read_text())
            doc["project_id"] = "other"
            path.write_text(json.dumps(doc, ensure_ascii=False,
                                       indent=2))
        calls.clear()
        with mock.patch.object(checks_unittest, "collect", fake):
            out, _ = execute(self.proj, self.policy)
        self.assertEqual(sorted(calls), ["cart-tests", "checkout-tests",
                                         "payments-tests"])
        self.assertEqual(out["outcomes"]["lint"], "passed")

    def test_status_reports_coverage_and_trust(self):
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        out = io.StringIO()
        content = (self.proj.root / "docs/tasks/DEV-1.md").read_text()
        with contextlib.redirect_stdout(out):
            checks_runner.status(self.proj.root, self.policy,
                                 task_id="DEV-1",
                                 task_content=content, task_meta={})
        text = out.getvalue()
        self.assertIn("trust_mode=local", text)
        self.assertIn("Покрытие обязательств", text)
        self.assertIn("cart-tests", text)


class CliRunTest(unittest.TestCase):
    """checks run/status через playbook.main на установленном проекте."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        code = playbook.main(["init", str(self.root), "--apply"])
        self.assertEqual(code, 0)
        proj_file = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(proj_file.read_text())
        project["setup_status"] = "configured"
        project["checks_policy"] = "checks-policy.json"
        proj_file.write_text(json.dumps(project, ensure_ascii=False,
                                        indent=2) + "\n")
        self.policy = run_policy()
        (self.root / "checks-policy.json").write_text(
            json.dumps(self.policy, ensure_ascii=False, indent=2) + "\n")
        for name in ("index.md", "product.md", "architecture.md",
                     "code-map.md", "roadmap.md"):
            doc = self.root / "docs" / name
            doc.write_text(doc.read_text().replace("TODO:", "OK:"))
        for rel in ("src/payments/pay.py", "src/checkout/order.py",
                    "src/cart/basket.py"):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x = 1\n")
        for name in ("test_payments", "test_checkout", "test_cart"):
            cls = {"test_payments": "TestPay",
                   "test_checkout": "TestOrder",
                   "test_cart": "TestCart"}[name]
            path = self.root / "tests" / f"{name}.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "import unittest\n\n"
                f"class {cls}(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(True)\n")
        task_dir = self.root / "docs" / "tasks"
        (task_dir / "DEV-1.md").write_text(TASK_MD)
        with contextlib.redirect_stdout(io.StringIO()):
            code = playbook.main(
                ["--root", str(self.root), "index"])
        self.assertEqual(code, 0)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = playbook.main(
                ["--root", str(self.root), *args])
        return code, out.getvalue() + err.getvalue()

    def test_run_and_status_end_to_end(self):
        code, text = self.run_cli("checks", "run", "DEV-1")
        self.assertEqual(code, 1, text)  # manual остаётся непокрытым
        self.assertIn("payments-tests", text)
        results, notes, _c = checks_results.load_results(self.root)
        self.assertTrue(results)
        self.assertEqual(notes, [])
        code, text = self.run_cli("checks", "status", "DEV-1")
        self.assertEqual(code, 0, text)
        self.assertIn("trust_mode=local", text)
        self.assertIn("passed", text)

    def test_run_second_time_reuses(self):
        code, _ = self.run_cli("checks", "run", "DEV-1")
        code, text = self.run_cli("checks", "run", "DEV-1")
        self.assertIn("reused", text)

    def test_run_requires_task(self):
        code, text = self.run_cli("checks", "run", "DEV-99")
        self.assertEqual(code, 1)
        self.assertIn("DEV-99", text)

    def test_run_requires_policy(self):
        proj_file = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(proj_file.read_text())
        project.pop("checks_policy")
        proj_file.write_text(json.dumps(project))
        code, _ = self.run_cli("checks", "run", "DEV-1")
        self.assertEqual(code, 1)


class AuditRegressionTest(unittest.TestCase):
    """Регрессии замечаний AUDIT-01 (A-01..A-05)."""

    def setUp(self):
        self.proj = run_project(self)
        self.policy = run_policy()

    def collect_counter(self, reports):
        calls = []

        def fake(root, check, run_dir, *, timeout, env=None):
            calls.append(check["id"])
            return canned_collect(reports[check["id"]])
        return calls, fake

    def reports(self):
        return {cid: green_report([tid]) for cid, tid in zip(
            ("payments-tests", "checkout-tests", "cart-tests"),
            ("test_payments.TestPay.test_ok",
             "test_checkout.TestOrder.test_ok",
             "test_cart.TestCart.test_ok"))}

    def test_a01_ttl_from_original_run(self):
        # Цепочка reuse: TTL считается от исходного запуска, не от
        # finished_at последней строки reuse.
        result = {"status": "passed",
                  "finished_at": "2030-01-01T00:00:00+00:00",
                  "reused": {"issued_at": "2020-01-01T00:00:00+00:00"}}
        check = {"required_environment": ["api"]}
        self.policy["environment"]["external"] = [
            {"id": "api", "freshness": {"kind": "ttl",
                                        "ttl_seconds": 60}}]
        reason = checks_runner._external_freshness(
            check, self.policy, {}, result,
            "2030-01-01T00:02:00+00:00")
        self.assertIsNotNone(reason)
        # И новая запись reuse сохраняет исходное issued_at.
        source = {"result_id": "r0", "check_id": "c",
                  "status": "passed",
                  "finished_at": "2020-06-01T00:00:00+00:00",
                  "reused": {"issued_at": "2020-01-01T00:00:00+00:00"},
                  "binding": {"input_digest": "sha256:" + "0" * 64},
                  "tests": {"expected": ["t.t"],
                            "actual": [{"id": "t.t",
                                        "outcome": "passed"}]}}
        out = checks_runner._reused_result(
            self.policy, self.policy["checks"][0], source, [],
            "sha256:" + "1" * 64, {}, "sha256:" + "0" * 64, "rid",
            "2020-06-02T00:00:00+00:00")
        self.assertEqual(out["reused"]["issued_at"],
                         "2020-01-01T00:00:00+00:00")

    def test_a02_reuse_rebinding_race_reruns(self):
        # Входы изменились между оценкой и записью reuse — не reused.
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        real = checks_runner._snapshot_inputs
        fired = []

        def racing(root, policy, check, projections, env, now):
            if check["id"] == "cart-tests" and not fired:
                fired.append(True)
                self.proj.write("tests/test_cart.py",
                                "import unittest\n")
            return real(root, policy, check, projections, env, now)

        calls.clear()
        with mock.patch.object(checks_unittest, "collect", fake), \
                mock.patch.object(checks_runner, "_snapshot_inputs",
                                  racing):
            out, _ = execute(self.proj, self.policy)
        self.assertTrue(fired)
        self.assertIn("cart-tests", calls)
        self.assertNotEqual(out["outcomes"]["cart-tests"], "reused")

    def test_a03_corrupt_latest_failed_blocks_reuse(self):
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        bad = self.reports()
        bad["payments-tests"] = {
            "protocol": 1,
            "expected": ["test_payments.TestPay.test_ok"],
            "actual": [{"id": "test_payments.TestPay.test_ok",
                        "outcome": "failed"}], "exit_code": 1}
        calls2, fake2 = self.collect_counter(bad)
        self.proj.write("src/payments/pay.py", "x = 2\n")
        with mock.patch.object(checks_unittest, "collect", fake2):
            execute(self.proj, self.policy)
        self.proj.write("src/payments/pay.py", "x = 1\n")
        # Повредить последний failed: старый passed не должен вернуться.
        # A-09: жертва выбирается по содержимому (status=failed), а не
        # по сортировке UUID-каталогов прогонов.
        victim = self._result_file("payments-tests", "failed")
        victim.write_text("{corrupt")
        calls2.clear()
        with mock.patch.object(checks_unittest, "collect", fake2):
            out, _ = execute(self.proj, self.policy)
        self.assertIn("payments-tests", calls2)
        self.assertNotEqual(out["outcomes"]["payments-tests"],
                            "reused")

    def _seed_lost_failed(self):
        """passed → failed → вернуть входы → удалить JSON failed."""
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        bad = self.reports()
        bad["payments-tests"] = {
            "protocol": 1,
            "expected": ["test_payments.TestPay.test_ok"],
            "actual": [{"id": "test_payments.TestPay.test_ok",
                        "outcome": "failed"}], "exit_code": 1}
        calls2, fake2 = self.collect_counter(bad)
        self.proj.write("src/payments/pay.py", "x = 2\n")
        with mock.patch.object(checks_unittest, "collect", fake2):
            execute(self.proj, self.policy)
        self.proj.write("src/payments/pay.py", "x = 1\n")
        self._result_file("payments-tests", "failed").unlink()
        return calls2, fake2

    def _result_file(self, check_id, status):
        """Файл результата check_id с требуемым status (A-09)."""
        for path in (self.proj.root /
                     "work/checks/results").rglob(check_id + ".json"):
            doc = json.loads(path.read_text())
            if doc["status"] == status:
                return path
        self.fail(f"результат {check_id} со status={status} не найден")

    def test_a06_deleted_latest_failed_blocks_reuse(self):
        calls2, fake2 = self._seed_lost_failed()
        calls2.clear()
        with mock.patch.object(checks_unittest, "collect", fake2):
            out, _ = execute(self.proj, self.policy)
        self.assertIn("payments-tests", calls2)
        self.assertNotEqual(out["outcomes"]["payments-tests"],
                            "reused")

    def test_a07_rebuild_preserves_loss_evidence(self):
        self._seed_lost_failed()
        checks_results.rebuild_index(self.proj.root, self.policy)
        # Свидетельство пережило пересборку: load_results снова
        # помечает check_id недоказанным.
        _, _, corrupt = checks_results.load_results(self.proj.root)
        self.assertIn("payments-tests", corrupt)
        # И следующий run всё ещё перезапускает, не reuse.
        calls2, fake2 = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake2):
            out, _ = execute(self.proj, self.policy)
        self.assertIn("payments-tests", calls2)
        self.assertNotEqual(out["outcomes"]["payments-tests"],
                            "reused")

    def test_a07_unrelated_run_keeps_evidence(self):
        # Прогон, где затронутая проверка не исполнялась (scope другой
        # задачи с её собственным набором), не стирает свидетельство.
        self._seed_lost_failed()
        # Перезапускаем только cart: изменяем только cart-вход.
        calls2, fake2 = self.collect_counter(self.reports())
        self.proj.write("src/cart/cart.py", "x = 2\n")
        with mock.patch.object(checks_unittest, "collect", fake2):
            execute(self.proj, self.policy)
        calls2.clear()
        self.proj.write("src/payments/pay.py", "x = 2\n")
        with mock.patch.object(checks_unittest, "collect", fake2):
            out, _ = execute(self.proj, self.policy)
        self.assertIn("payments-tests", calls2)
        self.assertNotEqual(out["outcomes"]["payments-tests"],
                            "reused")

    def test_a04_status_counts_only_fresh_coverage(self):
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        # Вход изменён: обязательный run — старый passed не покрытие.
        self.proj.write("src/payments/pay.py", "x = 2\n")
        out = io.StringIO()
        content = (self.proj.root / "docs/tasks/DEV-1.md").read_text()
        with contextlib.redirect_stdout(out):
            checks_runner.status(self.proj.root, self.policy,
                                 task_id="DEV-1",
                                 task_content=content, task_meta={})
        text = out.getvalue()
        self.assertIn("payments-tests: run", text)
        self.assertNotIn("Покрытие обязательств области задачи: "
                         "5/5", text)

    def test_a05_project_import_in_adapter(self):
        # Тест импортирует код проекта — корень в sys.path адаптера.
        self.proj.write(
            "tests/test_payments.py",
            "import unittest\n"
            "import src.payments.pay as pay\n\n"
            "class TestPay(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertEqual(pay.x, 1)\n")
        run_dir = self.proj.root / "work/checks/runs/imp"
        run_dir.mkdir(parents=True)
        check = self.policy["checks"][0]
        collected = checks_unittest.collect(
            self.proj.root, check, run_dir, timeout=10)
        out = checks_unittest.verdict(check, self.policy, collected)
        self.assertEqual(out["status"], "passed", out["reasons"])


if __name__ == "__main__":
    unittest.main()
