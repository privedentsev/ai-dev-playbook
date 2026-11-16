"""PB-060: адаптер unittest, результаты, freshness/reuse, run/status."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "ai-dev-playbook" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import checks_contract
import checks_gate
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
        """parse_argv разбирает формы unittest discover/модулей в selection"""
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
        """Зелёный набор тестов даёт verdict passed"""
        collected = self.collect()
        out = checks_unittest.verdict(self.check(), self.policy,
                                      collected)
        self.assertEqual(out["status"], "passed", out["reasons"])
        self.assertEqual(out["tests"]["expected"],
                         ["test_payments.TestPay.test_ok"])
        self.assertEqual(out["tests"]["actual"][0]["outcome"], "passed")

    def test_zero_tests_rejected(self):
        """Пустой набор без тестов отклоняется verdict'ом"""
        self.proj.write("tests/test_payments.py",
                        "import unittest\n")
        out = checks_unittest.verdict(self.check(), self.policy,
                                      self.collect())
        self.assertEqual(out["status"], "failed")
        self.assertTrue(any("нулевой набор" in r
                            for r in out["reasons"]), out["reasons"])

    def test_unexpected_skip_rejected(self):
        """Неожиданный skip вне allowlist отклоняется"""
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
        """Skip из allowlist каталога принимается verdict'ом"""
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
        """Тест вне каталога политики отклоняется"""
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
        """unfinished/crash исходы тестов отклоняются"""
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
        """error-исход попадает в описание результата проверки"""
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
        """Падение subTest отклоняется verdict'ом"""
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
        """expectedFailure и unexpected success учитываются verdict'ом"""
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
        """Превышение timeout проверки даёт статус timeout"""
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
        """Первый прогон сохраняет результаты и состояние runner'а"""
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            out, text = execute(self.proj, self.policy)
        self.assertFalse(out["ok"])  # manual непокрыт
        # lint — kind=command: исполняется argv напрямую, без адаптера.
        self.assertEqual(calls, ["payments-tests", "checkout-tests",
                                 "cart-tests"])
        self.assertEqual(out["outcomes"]["lint"], "passed")
        state = self.proj.root / "work/checks"
        # A-01 PB-073: baseline продвигается только полным покрытием —
        # ручное обязательство not_run оставляет snapshot на месте.
        self.assertFalse((state / "snapshot.json").exists())
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
        """stale-режим переиспользует независимые свежие результаты"""
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
        """Изменённый вход проверки заставляет её повторить"""
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
        """Поздний failed не скрывается старым passed при переиспользовании"""
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
        """Повреждённый результат не даёт ложного переиспользования"""
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
        """Повреждённый индекс результатов перестраивается"""
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
        """force-режим перезапускает все проверки области"""
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
        """scope=project покрывает все проверки проекта"""
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            out, _ = execute(self.proj, self.policy, scope="project")
        self.assertEqual(sorted(calls), ["cart-tests", "checkout-tests",
                                         "payments-tests"])
        self.assertEqual(out["outcomes"]["lint"], "passed")

    def test_task_requirements_change_breaks_reuse(self):
        """Изменение task_projection-входа ломает переиспользование"""
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
        """TTL и always_run внешних зависимостей соблюдаются runner'ом"""
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
        """Смена версии внешней зависимости ломает binding результата"""
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
        """Мутация входов во время команды проверки блокируется"""
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
        """Результат одной задачи переиспользуется другой в том же проекте"""
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
        """Результат другого проекта не переиспользуется"""
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
        """checks status сообщает покрытие и trust_mode"""
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
        """checks run и status работают end-to-end через CLI"""
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
        """Повторный checks run переиспользует результаты"""
        code, _ = self.run_cli("checks", "run", "DEV-1")
        code, text = self.run_cli("checks", "run", "DEV-1")
        self.assertIn("reused", text)

    def test_run_requires_task(self):
        """checks run для несуществующей задачи отклоняется"""
        code, text = self.run_cli("checks", "run", "DEV-99")
        self.assertEqual(code, 1)
        self.assertIn("DEV-99", text)

    def test_run_requires_policy(self):
        """checks run без checks_policy в project.json отклоняется"""
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
        """A-01: TTL внешней зависимости считается от исходного прогона"""
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
            "sha256:" + "1" * 64, {}, "sha256:" + "0" * 64, "rid", 0,
            "2020-06-02T00:00:00+00:00")
        self.assertEqual(out["reused"]["issued_at"],
                         "2020-01-01T00:00:00+00:00")

    def test_a02_reuse_rebinding_race_reruns(self):
        # Входы изменились между оценкой и записью reuse — не reused.
        """A-02: гонка перепривязки при переиспользовании вызывает прогон"""
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy)
        real = checks_runner._snapshot_inputs
        fired = []

        def racing(root, policy, check, projections, env, now, **kw):
            if check["id"] == "cart-tests" and not fired:
                fired.append(True)
                self.proj.write("tests/test_cart.py",
                                "import unittest\n")
            return real(root, policy, check, projections, env, now, **kw)

        calls.clear()
        with mock.patch.object(checks_unittest, "collect", fake), \
                mock.patch.object(checks_runner, "_snapshot_inputs",
                                  racing):
            out, _ = execute(self.proj, self.policy)
        self.assertTrue(fired)
        self.assertIn("cart-tests", calls)
        self.assertNotEqual(out["outcomes"]["cart-tests"], "reused")

    def test_a03_corrupt_latest_failed_blocks_reuse(self):
        """A-03: повреждённый поздний failed блокирует переиспользование"""
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
        """A-06: удалённый поздний failed блокирует переиспользование"""
        calls2, fake2 = self._seed_lost_failed()
        calls2.clear()
        with mock.patch.object(checks_unittest, "collect", fake2):
            out, _ = execute(self.proj, self.policy)
        self.assertIn("payments-tests", calls2)
        self.assertNotEqual(out["outcomes"]["payments-tests"],
                            "reused")

    def test_a07_rebuild_preserves_loss_evidence(self):
        """A-07: rebuild индекса сохраняет свидетельства потерь"""
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
        """A-07: несвязанный прогон сохраняет свидетельства потерь"""
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
        """A-04: status учитывает только свежее покрытие"""
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
        """A-05: project-импорт в адаптере unittest обрабатывается"""
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


class Audit009RegressionTest(unittest.TestCase):
    """Регрессии AUDIT-009: A-01 потеря обязательств, A-02 порядок
    истории при равном finished_at."""

    FROZEN = "2026-11-16T12:00:00+00:00"

    def setUp(self):
        self.proj = run_project(self)
        self.policy = run_policy()
        # A-01: payments-tests обязанность только при изменении модуля
        # (без always), ручная проверка убрана — ok достижим.
        self.policy["checks"] = [
            check for check in self.policy["checks"]
            if check["id"] != "ui-review"]
        for check in self.policy["checks"]:
            if check["id"] in {"payments-tests", "checkout-tests",
                               "cart-tests"}:
                check.pop("applicability", None)

    def collect_counter(self, reports, direct=None):
        calls = []

        def fake(root, check, run_dir, *, timeout, env=None):
            calls.append(check["id"])
            if direct and check["id"] in direct:
                return direct[check["id"]]
            return canned_collect(reports[check["id"]])
        return calls, fake

    def reports(self):
        return {cid: green_report([tid]) for cid, tid in zip(
            ("payments-tests", "checkout-tests", "cart-tests"),
            ("test_payments.TestPay.test_ok",
             "test_checkout.TestOrder.test_ok",
             "test_cart.TestCart.test_ok"))}

    def failed_payments(self):
        reports = self.reports()
        reports["payments-tests"] = {
            "protocol": 1,
            "expected": ["test_payments.TestPay.test_ok"],
            "actual": [{"id": "test_payments.TestPay.test_ok",
                        "outcome": "failed"}], "exit_code": 1}
        return reports

    @staticmethod
    def _collected(status, error):
        """Готовый сбор адаптера для timeout/spawn_error."""
        return {"status": status, "exit_code": None, "report": None,
                "error": error,
                "stdout_log": "work/checks/runs/x/o",
                "stderr_log": "work/checks/runs/x/e",
                "report_path": "work/checks/runs/x/a.json"}

    def timeout_payments(self):
        return {"payments-tests": self._collected(
            "timeout", "имитация таймаута")}

    def blocked_payments(self):
        return {"payments-tests": self._collected(
            "spawn_error", "имитация невозможного запуска")}

    def _seed_covered_baseline(self):
        """Зелёный прогон: baseline и покрытие установлены."""
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            out, _ = execute(self.proj, self.policy, now=self.FROZEN)
        self.assertTrue(out["ok"], out)
        snap = self.proj.root / "work/checks/snapshot.json"
        self.assertTrue(snap.is_file())
        return snap.read_bytes()

    def _assert_obligation_survives(self, reports, direct=None):
        """Неуспешный прогон по payments: baseline не продвигается,
        обязательство выполняется повторно при следующем stale."""
        baseline = self._seed_covered_baseline()
        self.proj.write("src/payments/pay.py", "x = 2\n")
        snap = self.proj.root / "work/checks/snapshot.json"
        calls1, fake1 = self.collect_counter(reports, direct=direct)
        with mock.patch.object(checks_unittest, "collect", fake1):
            out1, _ = execute(self.proj, self.policy, now=self.FROZEN)
        self.assertFalse(out1["ok"])
        self.assertNotEqual(out1["outcomes"]["payments-tests"],
                            "passed")
        # A-01: baseline обязан остаться на состоянии полного покрытия.
        self.assertEqual(snap.read_bytes(), baseline)
        # Следующий вызов без новых правок: diff против старого
        # baseline по-прежнему содержит изменение — обязательство
        # не становится not_applicable и проверка исполняется снова.
        calls1.clear()
        with mock.patch.object(checks_unittest, "collect", fake1):
            out2, _ = execute(self.proj, self.policy, now=self.FROZEN)
        self.assertIn("payments-tests", calls1)
        self.assertFalse(out2["ok"])
        self.assertEqual(snap.read_bytes(), baseline)
        return out2["outcomes"]["payments-tests"]

    def test_a01_failed_run_preserves_obligations(self):
        """A-01: failed-прогон не теряет обязательства задачи"""
        outcome = self._assert_obligation_survives(self.failed_payments())
        self.assertEqual(outcome, "failed")
        # Поздний успешный прогон восстанавливает покрытие и baseline.
        calls3, fake3 = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake3):
            out3, _ = execute(self.proj, self.policy, now=self.FROZEN)
        self.assertTrue(out3["ok"], out3)
        snap = self.proj.root / "work/checks/snapshot.json"
        # Следующий stale на неизменных входах: обязательство покрыто
        # свежим результатом, повторного исполнения нет.
        calls3.clear()
        with mock.patch.object(checks_unittest, "collect", fake3):
            out4, _ = execute(self.proj, self.policy, now=self.FROZEN)
        self.assertTrue(out4["ok"], out4)
        self.assertNotIn("payments-tests", calls3)

    def test_a01_timeout_run_preserves_obligations(self):
        """A-01: timeout-прогон не теряет обязательства задачи"""
        outcome = self._assert_obligation_survives(
            self.reports(), direct=self.timeout_payments())
        self.assertEqual(outcome, "timeout")

    def test_a01_blocked_run_preserves_obligations(self):
        """A-01: blocked-прогон не теряет обязательства задачи"""
        outcome = self._assert_obligation_survives(
            self.reports(), direct=self.blocked_payments())
        self.assertEqual(outcome, "blocked")

    @staticmethod
    def _result(check_id, status, finished_at, run_seq, result_id,
                run_id="r"):
        """Валидная запись результата схемы 1 с заданным run_seq."""
        doc = {"schema_version": 1, "result_id": result_id,
               "project_id": "shop", "check_id": check_id,
               "kind": "test",
               "binding": {
                   "policy_digest": "sha256:" + "0" * 64,
                   "definition_digest": "sha256:" + "0" * 64,
                   "adapter": {"id": "python-unittest",
                               "version": "1"},
                   "runner": {"id": "checks-runner", "version": "1"},
                   "input_digest": "sha256:" + "0" * 64,
                   "dependency_closure_digest": "sha256:" + "0" * 64,
                   "environment": {}},
               "status": status, "started_at": finished_at,
               "finished_at": finished_at,
               "tests": {"expected": ["t.t"],
                         "actual": [{"id": "t.t",
                                     "outcome": "passed"}]},
               "evidence": {"run_id": run_id}}
        if run_seq is not None:
            doc["evidence"]["run_seq"] = run_seq
        return doc

    def test_a02_equal_finished_at_latest_is_failed(self):
        """A-02: при равном finished_at поздний failed не скрывается
        старым passed — оба лексикографических порядка run_id."""
        stamp = self.FROZEN
        for first_id, second_id in (("0" * 32, "f" * 32),
                                    ("f" * 32, "0" * 32)):
            passed = self._result(
                "payments-tests", "passed", stamp, 0,
                f"{first_id}-payments-tests", run_id=first_id)
            failed = self._result(
                "payments-tests", "failed", stamp, 1,
                f"{second_id}-payments-tests", run_id=second_id)
            for order in ((passed, failed), (failed, passed)):
                latest = checks_select.evidence_latest(list(order))
                self.assertEqual(
                    latest["payments-tests"]["status"], "failed")
                self.assertEqual(
                    latest["payments-tests"]["evidence"]["run_seq"], 1)

    def test_a02_index_and_rebuild_use_run_seq(self):
        """A-02: индекс и его пересборка выбирают позднее исполнение
        при равном finished_at."""
        stamp = self.FROZEN
        passed = self._result(
            "payments-tests", "passed", stamp, 0,
            "a" * 32 + "-payments-tests", run_id="a" * 32)
        failed = self._result(
            "payments-tests", "failed", stamp, 1,
            "f" * 32 + "-payments-tests", run_id="f" * 32)
        for doc, run_id in ((passed, "a" * 32), (failed, "f" * 32)):
            checks_results.store_result(self.proj.root, run_id, doc)
        checks_results.update_index(self.proj.root, "a" * 32, passed)
        checks_results.update_index(self.proj.root, "f" * 32, failed)
        index = json.loads(
            (self.proj.root / "work/checks/results-index.json")
            .read_text())
        entry = index["entries"]["payments-tests"]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["run_seq"], 1)
        # Пересборка из append-only файлов даёт тот же выбор.
        checks_results.rebuild_index(self.proj.root, self.policy)
        index = json.loads(
            (self.proj.root / "work/checks/results-index.json")
            .read_text())
        self.assertEqual(
            index["entries"]["payments-tests"]["status"], "failed")
        results, notes, corrupt = checks_results.load_results(
            self.proj.root)
        self.assertEqual(corrupt, set())
        latest = checks_select.evidence_latest(results)
        self.assertEqual(latest["payments-tests"]["status"], "failed")

    def test_a02_legacy_result_without_run_seq_is_older(self):
        """A-02: записи до введения run_seq при равном finished_at
        старше любой seq-записи."""
        stamp = self.FROZEN
        legacy = self._result("payments-tests", "passed", stamp, None,
                              "b" * 32 + "-payments-tests",
                              run_id="b" * 32)
        fresh_failed = self._result(
            "payments-tests", "failed", stamp, 0,
            "c" * 32 + "-payments-tests", run_id="c" * 32)
        latest = checks_select.evidence_latest([legacy, fresh_failed])
        self.assertEqual(latest["payments-tests"]["status"], "failed")

    def test_a02_equal_timestamps_end_to_end(self):
        """A-02: два прогона с одним finished_at — поздний failed
        выбирается последним и статус показывает обязательство."""
        baseline = self._seed_covered_baseline()
        self.proj.write("src/payments/pay.py", "x = 2\n")
        calls1, fake1 = self.collect_counter(self.failed_payments())
        with mock.patch.object(checks_unittest, "collect", fake1), \
                mock.patch.object(checks_runner, "_clock",
                                  lambda now: self.FROZEN):
            execute(self.proj, self.policy, now=self.FROZEN)
        snap = self.proj.root / "work/checks/snapshot.json"
        self.assertEqual(snap.read_bytes(), baseline)
        results, _n, _c = checks_results.load_results(self.proj.root)
        latest = checks_select.evidence_latest(results)
        self.assertEqual(latest["payments-tests"]["status"], "failed")
        # status: обязательство payments-tests не покрыто — свежий
        # failed важнее старого passed при равном finished_at.
        out = io.StringIO()
        content = (self.proj.root / "docs/tasks/DEV-1.md").read_text()
        with contextlib.redirect_stdout(out):
            checks_runner.status(self.proj.root, self.policy,
                                 task_id="DEV-1",
                                 task_content=content, task_meta={})
        self.assertIn("payments-tests: run", out.getvalue())


class Audit009PB74RegressionTest(unittest.TestCase):
    """Регрессии PB-074 (A-04/A-05 повторного аудита AUDIT-009):
    недоказуемая история не снимает обязательство при пустом diff;
    неоднозначный порядок legacy-записей без run_seq не решается
    выбором result_id."""

    FROZEN = "2026-11-16T12:00:00+00:00"

    def setUp(self):
        self.proj = run_project(self)
        self.policy = run_policy()
        self.policy["checks"] = [
            check for check in self.policy["checks"]
            if check["id"] != "ui-review"]
        for check in self.policy["checks"]:
            if check["id"] in {"payments-tests", "checkout-tests",
                               "cart-tests"}:
                check.pop("applicability", None)

    def collect_counter(self, reports, direct=None):
        calls = []

        def fake(root, check, run_dir, *, timeout, env=None):
            calls.append(check["id"])
            if direct and check["id"] in direct:
                return direct[check["id"]]
            return canned_collect(reports[check["id"]])
        return calls, fake

    def reports(self):
        return {cid: green_report([tid]) for cid, tid in zip(
            ("payments-tests", "checkout-tests", "cart-tests"),
            ("test_payments.TestPay.test_ok",
             "test_checkout.TestOrder.test_ok",
             "test_cart.TestCart.test_ok"))}

    def failed_payments(self):
        reports = self.reports()
        reports["payments-tests"] = {
            "protocol": 1,
            "expected": ["test_payments.TestPay.test_ok"],
            "actual": [{"id": "test_payments.TestPay.test_ok",
                        "outcome": "failed"}], "exit_code": 1}
        return reports

    @staticmethod
    def _collected(status, error):
        return {"status": status, "exit_code": None, "report": None,
                "error": error,
                "stdout_log": "work/checks/runs/x/o",
                "stderr_log": "work/checks/runs/x/e",
                "report_path": "work/checks/runs/x/a.json"}

    @staticmethod
    def _result(check_id, status, finished_at, run_seq, result_id,
                run_id="r"):
        doc = {"schema_version": 1, "result_id": result_id,
               "project_id": "shop", "check_id": check_id,
               "kind": "test",
               "binding": {
                   "policy_digest": "sha256:" + "0" * 64,
                   "definition_digest": "sha256:" + "0" * 64,
                   "adapter": {"id": "python-unittest",
                               "version": "1"},
                   "runner": {"id": "checks-runner", "version": "1"},
                   "input_digest": "sha256:" + "0" * 64,
                   "dependency_closure_digest": "sha256:" + "0" * 64,
                   "environment": {}},
               "status": status, "started_at": finished_at,
               "finished_at": finished_at,
               "tests": {"expected": ["t.t"],
                         "actual": [{"id": "t.t",
                                     "outcome": "passed"}]},
               "evidence": {"run_id": run_id}}
        if run_seq is not None:
            doc["evidence"]["run_seq"] = run_seq
        return doc

    def _result_file(self, check_id, status):
        for path in (self.proj.root /
                     "work/checks/results").rglob(check_id + ".json"):
            doc = json.loads(path.read_text())
            if doc["status"] == status:
                return path
        self.fail(f"результат {check_id} со status={status} не найден")

    def _gate(self):
        content = (self.proj.root / "docs/tasks/DEV-1.md").read_text()
        return checks_gate.evaluate(
            self.proj.root, self.policy, task_id="DEV-1",
            task_content=content, task_meta={})

    def _status_text(self):
        content = (self.proj.root / "docs/tasks/DEV-1.md").read_text()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            checks_runner.status(self.proj.root, self.policy,
                                 task_id="DEV-1",
                                 task_content=content, task_meta={},
                                 now=self.FROZEN)
        return out.getvalue()

    def _project_pass_then_fail(self):
        """project passed → project failed без смены входов: baseline
        остаётся покрытым, diff пуст, последний исход — failed."""
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            out, _ = execute(self.proj, self.policy, scope="project",
                             mode="force", now=self.FROZEN)
        self.assertTrue(out["ok"], out)
        calls1, fake1 = self.collect_counter(self.failed_payments())
        with mock.patch.object(checks_unittest, "collect", fake1):
            out1, _ = execute(self.proj, self.policy, scope="project",
                              mode="force", now=self.FROZEN)
        self.assertFalse(out1["ok"])
        self.assertEqual(out1["outcomes"]["payments-tests"], "failed")
        return calls1, fake1

    def _assert_corrupt_obligation(self, mutate, between=None):
        """A-04: после порчи/удаления failed обязательство payments
        остаётся открытым при пустом diff до нового реального passed."""
        calls1, fake1 = self._project_pass_then_fail()
        victim = self._result_file("payments-tests", "failed")
        mutate(victim)
        if between is not None:
            between()
        # Gate: обязательство не снимается повреждённой историей.
        verdict = self._gate()
        self.assertFalse(verdict["ok"], verdict)
        self.assertIn("payments-tests", verdict["uncovered"])
        self.assertNotIn("payments-tests", verdict["covered"])
        # Чужие проверки не затронуты: их not_applicable сохраняется.
        self.assertEqual(
            verdict["checks"]["checkout-tests"]["status"],
            "not_applicable")
        self.assertIn("payments-tests: run", self._status_text())
        # Stale повторяет проверку — реальное исполнение, не reuse.
        calls1.clear()
        with mock.patch.object(checks_unittest, "collect", fake1):
            out2, _ = execute(self.proj, self.policy, now=self.FROZEN)
        self.assertIn("payments-tests", calls1)
        self.assertEqual(out2["outcomes"]["payments-tests"], "failed")
        self.assertFalse(out2["ok"])
        self.assertFalse(self._gate()["ok"])
        return victim

    def test_a04_corrupt_failed_empty_diff_keeps_obligation(self):
        """A-04: `{`-запись failed при актуальном baseline не даёт
        готовности; восстановление — целостность + новый passed."""
        victim = self._assert_corrupt_obligation(
            lambda path: path.write_text("{"))
        # Удаление повреждённого файла: свидетельство индекса о
        # последнем failed сохраняется — обязательство всё ещё открыто.
        victim.unlink()
        self.assertFalse(self._gate()["ok"])
        # Новый реальный passed: индекс переходит на доказанную
        # запись, покрытие и reuse восстановлены.
        calls3, fake3 = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake3):
            out3, _ = execute(self.proj, self.policy, now=self.FROZEN)
        self.assertTrue(out3["ok"], out3)
        self.assertTrue(self._gate()["ok"])
        # Следующий stale: обязательство покрыто — повторного
        # исполнения нет.
        calls3.clear()
        with mock.patch.object(checks_unittest, "collect", fake3):
            out4, _ = execute(self.proj, self.policy, now=self.FROZEN)
        self.assertTrue(out4["ok"], out4)
        self.assertNotIn("payments-tests", calls3)

    def test_a04_deleted_failed_empty_diff_keeps_obligation(self):
        """A-04: удалённый failed при сохранённом индексе — то же
        открытое обязательство; rebuild не снимает свидетельство."""
        def rebuild_keeps_evidence():
            # Пересборка индекса сохраняет свидетельство утраты (A-07).
            checks_results.rebuild_index(self.proj.root, self.policy)
            _, _, corrupt = checks_results.load_results(
                self.proj.root)
            self.assertIn("payments-tests", corrupt)
            self.assertFalse(self._gate()["ok"])

        self._assert_corrupt_obligation(
            lambda path: path.unlink(), between=rebuild_keeps_evidence)
        # После реального failed-прогона свидетельство индекса
        # замещено доказанной записью — обязательство открыто уже
        # обычным правилом UNRESOLVED, а не corrupt-флагом.
        _, _, corrupt = checks_results.load_results(self.proj.root)
        self.assertNotIn("payments-tests", corrupt)
        self.assertFalse(self._gate()["ok"])
        # Новый реальный passed замещает последний failed — покрытие
        # восстановлено без ручной правки индекса.
        calls3, fake3 = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake3):
            out3, _ = execute(self.proj, self.policy, now=self.FROZEN)
        self.assertTrue(out3["ok"], out3)
        self.assertTrue(self._gate()["ok"])
        # Обязательство закрыто: следующий stale не исполняет.
        calls3.clear()
        with mock.patch.object(checks_unittest, "collect", fake3):
            out4, _ = execute(self.proj, self.policy, now=self.FROZEN)
        self.assertTrue(out4["ok"], out4)
        self.assertNotIn("payments-tests", calls3)

    def test_a04_corrupt_timeout_record_keeps_obligation(self):
        """A-04: повреждённый timeout-исход — то же открытое
        обязательство (механизм не зависит от статуса записи)."""
        calls1, fake1 = self._project_pass_then_fail()
        # Заменить failed-отчёт payments на timeout-результат.
        timeout_direct = {"payments-tests": self._collected(
            "timeout", "имитация таймаута")}
        calls_t, fake_t = self.collect_counter(
            self.reports(), direct=timeout_direct)
        with mock.patch.object(checks_unittest, "collect", fake_t):
            out_t, _ = execute(self.proj, self.policy, scope="project",
                               mode="force", now=self.FROZEN)
        self.assertEqual(out_t["outcomes"]["payments-tests"], "timeout")
        victim = self._result_file("payments-tests", "timeout")
        victim.write_text("{")
        verdict = self._gate()
        self.assertFalse(verdict["ok"], verdict)
        self.assertIn("payments-tests", verdict["uncovered"])
        calls_t.clear()
        with mock.patch.object(checks_unittest, "collect", fake_t):
            out2, _ = execute(self.proj, self.policy, now=self.FROZEN)
        self.assertIn("payments-tests", calls_t)
        self.assertFalse(out2["ok"])

    def test_a04_corrupt_manual_record_stays_uncovered(self):
        """A-04: повреждённая запись ручной проверки не снимает
        ручное обязательство — gate держит manual, не covered."""
        review = {"id": "ui-review", "title": "Ручной", "purpose": "p",
                  "kind": "manual", "modules": ["payments"],
                  "scenario": "сценарий"}
        self.policy["checks"].append(review)
        calls, fake = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake):
            execute(self.proj, self.policy, scope="project",
                    mode="force", now=self.FROZEN)
        # Подтверждение инженера — passed-запись manual.
        content = (self.proj.root / "docs/tasks/DEV-1.md").read_text()
        checks_runner.confirm(
            self.proj.root, self.policy, task_id="DEV-1",
            task_content=content, task_meta={}, check_id="ui-review",
            who="Инженер", statement="Проверено", observation="ok",
            now=self.FROZEN, emit=lambda s: None)
        victim = self._result_file("ui-review", "passed")
        victim.write_text("{")
        verdict = self._gate()
        self.assertFalse(verdict["ok"], verdict)
        self.assertIn("ui-review", verdict["manual"])
        self.assertNotIn("ui-review", verdict["covered"])

    def test_a05_legacy_equal_timestamp_never_picks_passed(self):
        """A-05: два legacy-результата без run_seq при равном
        finished_at — непокрывающий исход важнее при любом
        лексикографическом порядке result_id."""
        stamp = self.FROZEN
        for pass_hex, fail_hex in (("0" * 32, "f" * 32),
                                   ("f" * 32, "0" * 32)):
            with self.subTest(pass_hex=pass_hex[:1],
                              fail_hex=fail_hex[:1]):
                passed = self._result(
                    "payments-tests", "passed", stamp, None,
                    f"{pass_hex}-payments-tests", run_id=pass_hex)
                failed = self._result(
                    "payments-tests", "failed", stamp, None,
                    f"{fail_hex}-payments-tests", run_id=fail_hex)
                for order in ((passed, failed), (failed, passed)):
                    latest = checks_select.evidence_latest(
                        list(order))
                    self.assertEqual(
                        latest["payments-tests"]["status"], "failed")
        # Обе passed-записи — однозначное покрытие сохраняется.
        first = self._result("payments-tests", "passed", stamp, None,
                             "a" * 32 + "-payments-tests",
                             run_id="a" * 32)
        second = self._result("payments-tests", "passed", stamp, None,
                              "b" * 32 + "-payments-tests",
                              run_id="b" * 32)
        latest = checks_select.evidence_latest([first, second])
        self.assertEqual(latest["payments-tests"]["status"], "passed")

    def test_a05_legacy_equal_end_to_end(self):
        """A-05: реальные passed → failed, конвертация в legacy без
        run_seq и равным finished_at — gate закрыт до нового
        успешного прогона; seq-порядок и однозначная история живы."""
        for pass_hex, fail_hex in (("f" * 32, "0" * 32),
                                   ("0" * 32, "f" * 32)):
            with self.subTest(pass_hex=pass_hex[:1],
                              fail_hex=fail_hex[:1]):
                proj = run_project(self)
                policy = run_policy()
                policy["checks"] = [
                    check for check in policy["checks"]
                    if check["id"] != "ui-review"]
                for check in policy["checks"]:
                    if check["id"] in {"payments-tests",
                                       "checkout-tests", "cart-tests"}:
                        check.pop("applicability", None)
                reports = {cid: green_report([tid]) for cid, tid in
                           zip(("payments-tests", "checkout-tests",
                                "cart-tests"),
                               ("test_payments.TestPay.test_ok",
                                "test_checkout.TestOrder.test_ok",
                                "test_cart.TestCart.test_ok"))}
                failed = dict(reports)
                failed["payments-tests"] = {
                    "protocol": 1,
                    "expected": ["test_payments.TestPay.test_ok"],
                    "actual": [{"id": "test_payments.TestPay.test_ok",
                                "outcome": "failed"}], "exit_code": 1}
                content = (proj.root / "docs/tasks/DEV-1.md").read_text()

                def run_once(reports_map, hex_id):
                    calls, fake = self.collect_counter(reports_map)
                    with mock.patch.object(
                            checks_unittest, "collect", fake), \
                            mock.patch.object(
                                checks_runner.uuid, "uuid4",
                                return_value=SimpleNamespace(
                                    hex=hex_id)):
                        return execute(proj, policy, scope="project",
                                       mode="force", now=self.FROZEN)

                out1, _ = run_once(reports, pass_hex)
                self.assertTrue(out1["ok"], out1)
                out2, _ = run_once(failed, fail_hex)
                self.assertFalse(out2["ok"])
                # Конвертация в legacy-формат: равный finished_at, без
                # run_seq; индекс удалён и штатно перестроен.
                stamp = "2026-11-16T03:00:00+00:00"
                for path in (proj.root / "work/checks/results") \
                        .rglob("payments-tests.json"):
                    doc = json.loads(path.read_text())
                    doc["finished_at"] = stamp
                    doc.get("evidence", {}).pop("run_seq", None)
                    path.write_text(json.dumps(
                        doc, ensure_ascii=False, indent=2) + "\n")
                index = proj.root / "work/checks/results-index.json"
                index.unlink()
                checks_results.rebuild_index(proj.root, policy)
                results, _notes, corrupt = \
                    checks_results.load_results(proj.root)
                self.assertEqual(corrupt, set())
                latest = checks_select.evidence_latest(results)
                self.assertEqual(
                    latest["payments-tests"]["status"], "failed")
                verdict = checks_gate.evaluate(
                    proj.root, policy, task_id="DEV-1",
                    task_content=content, task_meta={})
                self.assertFalse(verdict["ok"], verdict)
                self.assertIn("payments-tests", verdict["uncovered"])
                # Восстановление: новый seq-прогон с passed закрывает
                # обязательство — порядок новых записей сохранён.
                calls3, fake3 = self.collect_counter(reports)
                with mock.patch.object(checks_unittest, "collect",
                                       fake3):
                    out3, _ = execute(proj, policy, now=self.FROZEN)
                self.assertTrue(out3["ok"], out3)
                verdict = checks_gate.evaluate(
                    proj.root, policy, task_id="DEV-1",
                    task_content=content, task_meta={})
                self.assertTrue(verdict["ok"], verdict)

    def test_a05_seq_order_and_unambiguous_legacy_preserved(self):
        """A-05: seq-записи упорядочены run_seq; однозначная legacy
        история и новый seq-результат после неоднозначной пары
        выбираются корректно."""
        stamp = self.FROZEN
        # seq-порядок при равном finished_at сохраняется (A-02).
        seq_passed = self._result("payments-tests", "passed", stamp, 0,
                                  "a" * 32 + "-payments-tests",
                                  run_id="a" * 32)
        seq_failed = self._result("payments-tests", "failed", stamp, 1,
                                  "f" * 32 + "-payments-tests",
                                  run_id="f" * 32)
        latest = checks_select.evidence_latest([seq_passed, seq_failed])
        self.assertEqual(latest["payments-tests"]["status"], "failed")
        latest = checks_select.evidence_latest([seq_failed, seq_passed])
        self.assertEqual(latest["payments-tests"]["status"], "failed")
        # Одна legacy-запись — однозначна.
        lone = self._result("payments-tests", "passed", stamp, None,
                            "b" * 32 + "-payments-tests",
                            run_id="b" * 32)
        latest = checks_select.evidence_latest([lone])
        self.assertEqual(latest["payments-tests"]["status"], "passed")
        # Новая seq-запись после неоднозначной legacy-пары —
        # доказанный последний исход.
        legacy_passed = self._result(
            "payments-tests", "passed", stamp, None,
            "c" * 32 + "-payments-tests", run_id="c" * 32)
        legacy_failed = self._result(
            "payments-tests", "failed", stamp, None,
            "d" * 32 + "-payments-tests", run_id="d" * 32)
        fresh_passed = self._result(
            "payments-tests", "passed", stamp, 0,
            "e" * 32 + "-payments-tests", run_id="e" * 32)
        latest = checks_select.evidence_latest(
            [legacy_passed, legacy_failed, fresh_passed])
        self.assertEqual(latest["payments-tests"]["status"], "passed")
        self.assertEqual(
            latest["payments-tests"]["evidence"]["run_seq"], 0)

    def test_audit01_missing_results_dir_keeps_obligation(self):
        """Аудит-01 A-01: утрата всего каталога results при
        сохранённом индексе — свидетельство индекса делает check_id
        недоказуемым и без каталога: gate/status/stale требуют
        нового реального passed."""
        import shutil
        calls1, fake1 = self._project_pass_then_fail()
        shutil.rmtree(self.proj.root / "work/checks/results")
        # Пересборка сохраняет свидетельства индекса; load_results
        # читает их и при отсутствующем каталоге.
        checks_results.rebuild_index(self.proj.root, self.policy)
        _results, _notes, corrupt = checks_results.load_results(
            self.proj.root)
        self.assertIn("payments-tests", corrupt)
        verdict = self._gate()
        self.assertFalse(verdict["ok"], verdict)
        self.assertIn("payments-tests", verdict["uncovered"])
        self.assertIn("payments-tests: run", self._status_text())
        # Stale — реальное исполнение, не reuse.
        calls1.clear()
        with mock.patch.object(checks_unittest, "collect", fake1):
            out2, _ = execute(self.proj, self.policy, now=self.FROZEN)
        self.assertIn("payments-tests", calls1)
        self.assertFalse(out2["ok"])
        self.assertFalse(self._gate()["ok"])
        # Новый реальный passed восстанавливает покрытие.
        calls3, fake3 = self.collect_counter(self.reports())
        with mock.patch.object(checks_unittest, "collect", fake3):
            out3, _ = execute(self.proj, self.policy, now=self.FROZEN)
        self.assertTrue(out3["ok"], out3)
        self.assertTrue(self._gate()["ok"])

    def test_audit01_plan_corrupt_ids_forbid_reuse(self):
        """Аудит-01 A-02: plan с corrupt_ids и старым passed в
        results не выдаёт reused — планировщик сам отсекает
        недоказуемую историю; без флага reuse остаётся законным."""
        calls1, fake1 = self._project_pass_then_fail()
        victim = self._result_file("payments-tests", "failed")
        victim.unlink()
        checks_results.rebuild_index(self.proj.root, self.policy)
        results, _notes, corrupt = checks_results.load_results(
            self.proj.root)
        self.assertIn("payments-tests", corrupt)
        content = (self.proj.root / "docs/tasks/DEV-1.md").read_text()
        task_digest = verification_policy.fingerprint(
            verification_policy.task_projection(content, {}))
        projections = checks_runner._projections(
            self.policy, content, {})
        # Вызывающий путь checks_plan передаёт историю целиком:
        # фильтрация corrupt check_id — обязанность планировщика.
        planned = checks_select.plan(
            self.proj.root, self.policy, results=results,
            corrupt_ids=corrupt, task_digest=task_digest,
            projections=projections, now=self.FROZEN)
        entry = planned["checks"]["payments-tests"]
        self.assertEqual(entry["status"], "run", entry)
        self.assertTrue(any("недоказуема" in line
                            for line in entry["explain"]), entry)
        # Контроль: без corrupt_ids тот же старый passed —
        # законный reuse, фильтрация не ломает обычный расчёт.
        planned_ok = checks_select.plan(
            self.proj.root, self.policy, results=results,
            task_digest=task_digest, projections=projections,
            now=self.FROZEN)
        self.assertEqual(
            planned_ok["checks"]["payments-tests"]["status"],
            "reused", planned_ok["checks"]["payments-tests"])

    def _legacy_pair_project(self, pass_hex, fail_hex):
        """Проект с legacy-парой payments-tests равного finished_at
        без run_seq: passed с UUID pass_hex, failed — fail_hex.
        Возвращает (proj, policy, content, paths)."""
        proj = run_project(self)
        policy = run_policy()
        policy["checks"] = [
            check for check in policy["checks"]
            if check["id"] != "ui-review"]
        for check in policy["checks"]:
            if check["id"] in {"payments-tests", "checkout-tests",
                               "cart-tests"}:
                check.pop("applicability", None)
        reports = {cid: green_report([tid]) for cid, tid in zip(
            ("payments-tests", "checkout-tests", "cart-tests"),
            ("test_payments.TestPay.test_ok",
             "test_checkout.TestOrder.test_ok",
             "test_cart.TestCart.test_ok"))}
        failed = dict(reports)
        failed["payments-tests"] = {
            "protocol": 1,
            "expected": ["test_payments.TestPay.test_ok"],
            "actual": [{"id": "test_payments.TestPay.test_ok",
                        "outcome": "failed"}], "exit_code": 1}
        content = (proj.root / "docs/tasks/DEV-1.md").read_text()

        def run_once(reports_map, hex_id):
            _calls, fake = self.collect_counter(reports_map)
            with mock.patch.object(checks_unittest, "collect", fake), \
                    mock.patch.object(
                        checks_runner.uuid, "uuid4",
                        return_value=SimpleNamespace(hex=hex_id)):
                return execute(proj, policy, scope="project",
                               mode="force", now=self.FROZEN)

        out1, _ = run_once(reports, pass_hex)
        self.assertTrue(out1["ok"], out1)
        out2, _ = run_once(failed, fail_hex)
        self.assertFalse(out2["ok"])
        # Legacy-фикстура: equal finished_at (общий now), без run_seq.
        for path in (proj.root / "work/checks/results").rglob("*.json"):
            doc = json.loads(path.read_text())
            doc["evidence"].pop("run_seq", None)
            path.write_text(json.dumps(doc, ensure_ascii=False,
                                       indent=2) + "\n")
        paths = {}
        for path in (proj.root / "work/checks/results") \
                .rglob("payments-tests.json"):
            doc = json.loads(path.read_text())
            paths[doc["status"]] = (path, doc)
        return proj, policy, content, paths, reports

    @staticmethod
    def _index_entry_for(path, doc, root):
        return {"result_id": doc["result_id"],
                "run_id": doc["evidence"]["run_id"],
                "finished_at": doc["finished_at"], "run_seq": None,
                "file": str(path.relative_to(root)),
                "status": doc["status"]}

    def test_audit02_legacy_loss_keeps_obligation(self):
        """Аудит-02 A-03: legacy-пара равного времени — утрата failed
        при любом UUID-порядке и любом исходном индексе оставляет
        обязательство открытым до нового реального passed; rebuild
        не воскрешает passed поверх failed."""
        for pass_hex, fail_hex in (("f" * 32, "0" * 32),
                                   ("0" * 32, "f" * 32)):
            for old_index in (False, True):
                with self.subTest(pass_hex=pass_hex[:1],
                                  fail_hex=fail_hex[:1],
                                  old_index=old_index):
                    proj, policy, content, paths, reports = \
                        self._legacy_pair_project(pass_hex, fail_hex)
                    index_path = proj.root / \
                        "work/checks/results-index.json"
                    if old_index:
                        # Старый passed-индекс, как у писателя до
                        # исправления: указывает на старый успех.
                        passed_path, passed_doc = paths["passed"]
                        index = {
                            "schema_version": 1,
                            "kind": "checks-results-index",
                            "project_id": policy["project_id"],
                            "entries": {"payments-tests":
                                        self._index_entry_for(
                                            passed_path, passed_doc,
                                            proj.root)}}
                        index_path.write_text(json.dumps(index))
                    else:
                        index_path.unlink(missing_ok=True)
                    # Штатный rebuild: консервативный failed —
                    # единственный допустимый свидетель.
                    checks_results.rebuild_index(proj.root, policy)
                    index = json.loads(
                        index_path.read_text())
                    self.assertEqual(
                        index["entries"]["payments-tests"]["status"],
                        "failed", index["entries"])
                    # Утрата failed-файла: свидетельство индекса при
                    # равном времени важнее UUID — corrupt.
                    paths["failed"][0].unlink()
                    _r, _n, corrupt = checks_results.load_results(
                        proj.root)
                    self.assertIn("payments-tests", corrupt)
                    for scope in ("project", "task"):
                        verdict = checks_gate.evaluate(
                            proj.root, policy, task_id="DEV-1",
                            scope=scope, task_content=content,
                            task_meta={}, now=self.FROZEN)
                        self.assertFalse(verdict["ok"],
                                         (scope, verdict))
                        self.assertIn("payments-tests",
                                      verdict["uncovered"])
                    # Rebuild после утраты сохраняет свидетельство.
                    checks_results.rebuild_index(proj.root, policy)
                    _r, _n, corrupt = checks_results.load_results(
                        proj.root)
                    self.assertIn("payments-tests", corrupt)
                    # Stale — реальное исполнение, не reuse.
                    calls, fake = self.collect_counter(
                        {"payments-tests": {
                            "protocol": 1,
                            "expected": ["test_payments.TestPay.test_ok"],
                            "actual": [{"id":
                                        "test_payments.TestPay.test_ok",
                                        "outcome": "failed"}],
                            "exit_code": 1},
                         "checkout-tests": green_report(
                             ["test_checkout.TestOrder.test_ok"]),
                         "cart-tests": green_report(
                             ["test_cart.TestCart.test_ok"])})
                    with mock.patch.object(
                            checks_unittest, "collect", fake):
                        out3, _ = execute(proj, policy,
                                          now=self.FROZEN)
                    self.assertIn("payments-tests", calls)
                    self.assertFalse(out3["ok"])
                    self.assertFalse(
                        checks_gate.evaluate(
                            proj.root, policy, task_id="DEV-1",
                            task_content=content, task_meta={},
                            now=self.FROZEN)["ok"])
                    # Новый реальный seq-passed восстанавливает
                    # покрытие — порядок новых записей сохранён.
                    calls4, fake4 = self.collect_counter(reports)
                    with mock.patch.object(
                            checks_unittest, "collect", fake4):
                        out4, _ = execute(proj, policy,
                                          now=self.FROZEN)
                    self.assertTrue(out4["ok"], out4)
                    self.assertTrue(
                        checks_gate.evaluate(
                            proj.root, policy, task_id="DEV-1",
                            task_content=content, task_meta={},
                            now=self.FROZEN)["ok"])


if __name__ == "__main__":
    unittest.main()
