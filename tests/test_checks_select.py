"""PB-059 AC-02/03: расширение до доказанной границы, explain, бюджет."""
import io
import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "ai-dev-playbook" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import checks_contract
import checks_graph
import checks_inventory
import checks_select as cs
import verification_policy
import project_view
from test_playbook import playbook


def req(condition, message):
    if not condition:
        raise ValueError(message)


def make_policy(**overrides):
    policy = {
        "schema_version": 1,
        "policy_id": "shop-checks",
        "revision": 1,
        "project_id": "shop",
        "trust_mode": "local",
        "approved_basis": {"who": "Инженер",
                           "at": "2026-11-08T10:00:00+03:00",
                           "source": "тест", "detail": "fixture"},
        "subsystems": [{"id": "billing", "title": "Биллинг"},
                       {"id": "catalog", "title": "Каталог"}],
        "modules": [
            {"id": "payments", "title": "Платежи",
             "subsystem_id": "billing", "paths": ["src/payments/**"]},
            {"id": "checkout", "title": "Заказ",
             "subsystem_id": "billing", "paths": ["src/checkout/**"]},
            {"id": "cart", "title": "Корзина",
             "subsystem_id": "catalog", "paths": ["src/cart/**"]},
        ],
        "checks": [
            {"id": "payments-tests", "title": "t", "purpose": "p",
             "kind": "test", "modules": ["payments"],
             "adapter": "python-unittest",
             "argv": ["python3", "-m", "unittest", "test_payments"],
             "timeout_seconds": 5},
            {"id": "checkout-tests", "title": "t", "purpose": "p",
             "kind": "test", "modules": ["checkout"],
             "adapter": "python-unittest",
             "argv": ["python3", "-m", "unittest", "test_checkout"],
             "timeout_seconds": 5},
            {"id": "cart-tests", "title": "t", "purpose": "p",
             "kind": "test", "modules": ["cart"],
             "adapter": "python-unittest",
             "argv": ["python3", "-m", "unittest", "test_cart"],
             "timeout_seconds": 5},
        ],
        "dependencies": [],
        "excludes": [],
        "environment": {"toolchain": [], "external": []},
        "fallback": {"expansion_order": ["module", "subsystem", "project"],
                     "approved_commands": [],
                     "limits": {"max_seconds": 100, "max_bytes": 10_000_000},
                     "unknown_scope": "project"},
        "test_catalog": {"adapter": "python-unittest",
                         "fingerprint": "sha256:" + "a" * 64,
                         "entries": [{"id": "t.t", "description": "d"}]},
    }
    policy.update(overrides)
    return policy


class Project:
    def __init__(self, case):
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir()

    def write(self, rel, text="x = 1\n"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def snapshot_tree(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in sorted(self.root.rglob("*")) if p.is_file()}


def shop_project(case):
    proj = Project(case)
    proj.write("src/payments/pay.py")
    proj.write("src/checkout/order.py")
    proj.write("src/cart/basket.py")
    return proj


def graph_with(edges, unknown=None):
    return {"schema_version": 1, "project_id": "shop",
            "policy_digest": "sha256:" + "0" * 64,
            "created_at": "2026-11-08T12:00:00+03:00",
            "analyzer_version": "ast-v1", "edges": edges,
            "unknown": unknown or []}


class SelectTest(unittest.TestCase):
    """Чистый selector: границы расширения, explain, обязательства."""

    def setUp(self):
        self.policy = make_policy()

    def select(self, changed, edges=None, unknown=None, **kw):
        return cs.select_checks(self.policy, changed=changed,
                                edges=edges or [], unknown=unknown or [],
                                **kw)

    def test_module_change_selects_own_and_consumers(self):
        edges = [{"consumer": "module:checkout",
                  "dependency": "module:payments", "kind": "import",
                  "source": "analyzer", "analyzer_version": "ast-v1",
                  "evidence": "src/checkout/order.py:1"}]
        out = self.select(["src/payments/pay.py"], edges)
        self.assertEqual(out["checks"]["payments-tests"]["status"], "run")
        self.assertEqual(out["checks"]["checkout-tests"]["status"], "run")
        self.assertEqual(out["checks"]["cart-tests"]["status"],
                         "not_applicable")
        explain = out["checks"]["checkout-tests"]["explain"]
        self.assertTrue(any("module:payments" in line for line in explain),
                        explain)

    def test_transitive_two_edge_chain(self):
        # A-01: обход не обрывается после первого ребра — цепь
        # checkout→payments, cart→checkout достигает cart.
        edges = [
            {"consumer": "module:checkout",
             "dependency": "module:payments", "kind": "import",
             "source": "analyzer", "analyzer_version": "ast-v1",
             "evidence": "src/checkout/order.py:1"},
            {"consumer": "module:cart",
             "dependency": "module:checkout", "kind": "import",
             "source": "analyzer", "analyzer_version": "ast-v1",
             "evidence": "src/cart/basket.py:1"}]
        out = self.select(["src/payments/pay.py"], edges)
        self.assertTrue(all(e["status"] == "run"
                            for e in out["checks"].values()), out)
        chain = out["checks"]["cart-tests"]["explain"]
        self.assertTrue(any("module:checkout" in line for line in chain))

    def test_closure_digest_chain_cycle_change(self):
        # A-02: digest стабилен для цепей и циклов, меняется с графом.
        check = self.policy["checks"][2]
        chain = [{"consumer": "module:cart",
                  "dependency": "module:checkout", "kind": "import",
                  "source": "analyzer",
                  "analyzer_version": "ast-v1"},
                 {"consumer": "module:checkout",
                  "dependency": "module:payments", "kind": "import",
                  "source": "analyzer",
                  "analyzer_version": "ast-v1"}]
        first = cs.closure_digest(chain, check)
        self.assertTrue(first.startswith("sha256:"))
        cyclic = chain + [{"consumer": "module:payments",
                           "dependency": "module:cart",
                           "kind": "import", "source": "analyzer",
                           "analyzer_version": "ast-v1"}]
        self.assertTrue(cs.closure_digest(cyclic, check)
                        .startswith("sha256:"))
        self.assertNotEqual(first, cs.closure_digest(cyclic, check))
        self.assertNotEqual(
            first, cs.closure_digest(
                chain[:1], self.policy["checks"][0]))

    def test_change_only_inside_module_no_spill(self):
        out = self.select(["src/cart/basket.py"])
        self.assertEqual(out["checks"]["payments-tests"]["status"],
                         "not_applicable")
        self.assertEqual(out["checks"]["checkout-tests"]["status"],
                         "not_applicable")
        self.assertEqual(out["checks"]["cart-tests"]["status"], "run")

    def test_unproven_change_widens(self):
        out = self.select(None)
        self.assertTrue(all(e["status"] == "run"
                            for e in out["checks"].values()))

    def test_unknown_module_always_affected(self):
        unknown = [{"scope": {"kind": "module", "id": "cart"},
                    "reason": "динамический импорт"}]
        out = self.select(["src/payments/pay.py"], unknown=unknown)
        self.assertEqual(out["checks"]["cart-tests"]["status"], "run")
        self.assertEqual(out["checks"]["payments-tests"]["status"], "run")
        self.assertEqual(out["checks"]["checkout-tests"]["status"],
                         "not_applicable")

    def test_unknown_project_scope_widens(self):
        unknown = [{"scope": {"kind": "project"},
                    "reason": "динамическая загрузка"}]
        out = self.select(["src/payments/pay.py"], unknown=unknown)
        self.assertTrue(all(e["status"] == "run"
                            for e in out["checks"].values()))

    def test_unattributed_path_project_scope(self):
        out = self.select(["docs/notes.md"])
        self.assertEqual(out["unattributed"], ["docs/notes.md"])
        self.assertTrue(all(e["status"] == "run"
                            for e in out["checks"].values()))

    def test_unattributed_path_blocked_by_scope(self):
        self.policy["fallback"]["unknown_scope"] = "subsystem"
        out = self.select(["docs/notes.md"])
        self.assertIsNotNone(out["blocked"])
        self.assertTrue(all(e["status"] == "blocked"
                            for e in out["checks"].values()))

    def test_unattributed_module_scope_blocked(self):
        self.policy["fallback"]["unknown_scope"] = "module"
        out = self.select(["docs/notes.md"])
        self.assertTrue(all(e["status"] == "blocked"
                            for e in out["checks"].values()))

    def test_subsystem_consumer_expands(self):
        # check:ops зависит от целой подсистемы catalog.
        self.policy["checks"].append(
            {"id": "ops", "title": "t", "purpose": "p", "kind": "manual",
             "scenario": "s"})
        edges = [{"consumer": "check:ops",
                  "dependency": "subsystem:catalog", "kind": "resource",
                  "source": "explicit"}]
        out = self.select(["src/cart/basket.py"], edges)
        # subsystem:catalog — потребитель cart? Нет: ребро check→subsystem
        # означает ops зависит от catalog; изменение cart затрагивает ops.
        self.assertEqual(out["checks"]["ops"]["status"], "run")
        self.assertTrue(any("subsystem:catalog" in line or
                            "ребро" in line
                            for line in out["checks"]["ops"]["explain"]))

    def test_always_and_task_obligations_add(self):
        self.policy["checks"].extend([
            {"id": "lint", "title": "t", "purpose": "p", "kind": "command",
             "argv": ["lint"], "timeout_seconds": 5,
             "applicability": "always"},
            {"id": "card-check", "title": "t", "purpose": "p",
             "kind": "command", "argv": ["check"], "timeout_seconds": 5,
             "applicability": {"kind": "when_paths",
                               "paths": ["src/**"]},
             "inputs": [{"type": "task_projection"}]}])
        out = self.select(["src/cart/basket.py"])
        self.assertEqual(out["checks"]["lint"]["status"], "run")
        entry = out["checks"]["card-check"]
        self.assertEqual(entry["status"], "run")
        self.assertTrue(any("task_projection" in line
                            for line in entry["explain"]))
        self.assertTrue(any("when_paths" in line
                            for line in entry["explain"]))

    def test_when_paths_unmatched_not_required(self):
        self.policy["checks"].append(
            {"id": "docs-check", "title": "t", "purpose": "p",
             "kind": "command", "argv": ["d"], "timeout_seconds": 5,
             "applicability": {"kind": "when_paths", "paths": ["docs/**"]}})
        out = self.select(["src/cart/basket.py"])
        self.assertEqual(out["checks"]["docs-check"]["status"],
                         "not_applicable")

    def test_checkpoint_not_required_without_flag(self):
        self.policy["checks"].append(
            {"id": "gate", "title": "t", "purpose": "p", "kind": "command",
             "argv": ["g"], "timeout_seconds": 5,
             "applicability": {"kind": "checkpoint",
                               "checkpoint": "release"}})
        out = self.select(None)
        self.assertEqual(out["checks"]["gate"]["status"],
                         "not_applicable")
        out = self.select(None, checkpoint="release")
        self.assertEqual(out["checks"]["gate"]["status"], "run")

    def test_obligations_never_narrow(self):
        # Обязательства задачи дополняют вычисленный набор: явная
        # when_paths не отменяет графовую обязательность.
        self.policy["checks"][2]["applicability"] = \
            {"kind": "when_paths", "paths": ["nothing/**"]}
        out = self.select(["src/cart/basket.py"])
        self.assertEqual(out["checks"]["cart-tests"]["status"], "run")


class PlanTest(unittest.TestCase):
    """Сквозной план: снимок universe, reuse-оценка, бюджет, read-only."""

    def setUp(self):
        self.proj = shop_project(self)
        self.policy = make_policy()
        checks_contract.validate_policy(self.policy, req)

    def graph(self, **kw):
        kw.setdefault("now", "2026-11-08T12:00:00+03:00")
        return checks_graph.build_graph(self.proj.root, self.policy, **kw)

    def plan(self, **kw):
        kw.setdefault("now", "2026-11-08T12:05:00+03:00")
        return cs.plan(self.proj.root, self.policy, **kw)

    def baseline(self, **kw):
        kw.setdefault("now", "2026-11-08T12:00:00+03:00")
        return checks_inventory.snapshot(
            self.proj.root, self.policy,
            cs.universe_inputs(self.policy), **kw)

    def test_first_plan_no_baseline_requires_all(self):
        out = self.plan(graph=self.graph())
        self.assertIsNone(out["changed"])
        self.assertEqual(out["summary"]["run"], 3)
        self.assertFalse(out["budget_blocked"])

    def test_read_only_no_writes(self):
        before = self.proj.snapshot_tree()
        self.plan(graph=self.graph(), baseline=self.baseline())
        self.assertEqual(before, self.proj.snapshot_tree())

    def test_edit_selects_consumer_chain(self):
        # checkout импортирует payments: правка payments требует
        # оба набора, cart доказанно независим.
        self.proj.write("src/checkout/order.py",
                        "import payments.pay\n")
        baseline = self.baseline()
        graph = self.graph()
        self.proj.write("src/payments/pay.py", "x = 2\n")
        out = self.plan(baseline=baseline, graph=graph)
        self.assertEqual(out["checks"]["payments-tests"]["status"], "run")
        self.assertEqual(out["checks"]["checkout-tests"]["status"], "run")
        self.assertEqual(out["checks"]["cart-tests"]["status"],
                         "not_applicable")

    def test_content_edit_detected(self):
        graph = self.graph()
        baseline = self.baseline()
        self.proj.write("src/payments/pay.py", "x = 2\n")
        out = self.plan(baseline=baseline, graph=graph)
        self.assertIn("src/payments/pay.py", out["changed"])
        self.assertEqual(out["checks"]["payments-tests"]["status"], "run")
        self.assertEqual(out["checks"]["cart-tests"]["status"],
                         "not_applicable")

    def test_irrelevant_edit_outside_modules(self):
        graph = self.graph()
        baseline = self.baseline()
        self.proj.write("logs/run.log", "log\n")
        out = self.plan(baseline=baseline, graph=graph)
        self.assertEqual(out["unattributed"], ["logs/run.log"])
        self.assertEqual(out["summary"]["run"], 3)

    def test_deleted_input_required(self):
        baseline = self.baseline()
        (self.proj.root / "src/payments/pay.py").unlink()
        out = self.plan(baseline=baseline, graph=self.graph())
        self.assertIn("src/payments/pay.py", out["changed"])
        self.assertEqual(out["checks"]["payments-tests"]["status"], "run")

    def test_corrupt_baseline_conservative(self):
        bad = dict(self.baseline(), kind="report")
        out = self.plan(baseline=bad, graph=self.graph())
        self.assertIsNone(out["changed"])
        self.assertEqual(out["summary"]["run"], 3)

    def test_hash_counter_expected_set(self):
        calls = []
        real = project_view.digest_file

        def counting(path):
            calls.append(str(path))
            return real(path)

        self.plan(graph=self.graph(), hash_fn=counting)
        # Universe-снимок хеширует каждый значимый файл один раз.
        self.assertEqual(len(calls), len(set(calls)))
        hashed = sorted(calls)
        self.assertEqual(len(hashed), 3)
        self.assertTrue(all(name.endswith(".py") for name in hashed))

    def test_seconds_budget_blocks_before_run(self):
        self.policy["fallback"]["limits"]["max_seconds"] = 10
        out = self.plan(graph=self.graph())
        self.assertTrue(out["budget_blocked"])
        self.assertEqual(out["estimate_seconds"], 15)
        self.assertTrue(all(e["status"] == "blocked"
                            for e in out["checks"].values()))

    def test_bytes_budget_conservative(self):
        # A-04: превышение max_bytes блокирует обязательства до решения
        # инженера, а не расширяет запуск.
        self.policy["fallback"]["limits"]["max_bytes"] = 3
        out = self.plan(graph=self.graph())
        self.assertTrue(out["bytes_exceeded"])
        self.assertTrue(out["budget_blocked"])
        self.assertIsNone(out["changed"])
        self.assertEqual(out["summary"]["blocked"], 3)
        self.assertEqual(out["summary"]["run"], 0)

    def test_ast_reads_within_same_budget(self):
        # A-04: внутреннее построение графа читает исходники под тем же
        # лимитом байтов, что и снимок входов.
        self.policy["fallback"]["limits"]["max_bytes"] = 3
        out = cs.plan(self.proj.root, self.policy)
        self.assertTrue(out["bytes_exceeded"])
        self.assertTrue(out["budget_blocked"])
        self.assertEqual(out["summary"]["blocked"], 3)
        self.assertIsNone(out["changed"])

    def _result(self, check, policy, graph, snap, *,
                    status="passed", result_id="r1",
                    finished="2026-11-08T12:30:00+03:00",
                    env=None):
        return {
            "schema_version": 1, "result_id": result_id,
            "project_id": "shop", "check_id": check["id"],
            "kind": check["kind"],
            "binding": {
                "policy_digest": verification_policy.fingerprint(policy),
                "definition_digest": cs.definition_digest(check),
                "adapter": {"id": "python-unittest", "version": "1"},
                "runner": dict(cs.RUNNER),
                "input_digest": checks_inventory.snapshot_digest(snap),
                "dependency_closure_digest":
                    cs.closure_digest(graph["edges"], check),
                "environment": env or cs.environment_identity(policy),
            },
            "status": status,
            "started_at": finished, "finished_at": finished,
            "tests": {"expected": ["t.t"],
                      "actual": [{"id": "t.t", "outcome": "passed"}]},
        }

    def test_reuse_valid_result(self):
        # payments-tests обязателен (always): свежий результат закрывает
        # обязательство без нового запуска; незатронутый cart — нет.
        self.policy["checks"][0]["applicability"] = "always"
        graph = self.graph()
        baseline = self.baseline()
        check = self.policy["checks"][0]
        snap = checks_inventory.snapshot(
            self.proj.root, self.policy,
            checks_inventory.resolved_inputs(self.policy, check),
            now="2026-11-08T12:00:00+03:00")
        result = self._result(check, self.policy, graph, snap)
        checks_contract.validate_result(result, req)
        out = self.plan(baseline=baseline, graph=graph,
                        results=[result])
        self.assertEqual(out["checks"]["payments-tests"]["status"],
                         "reused")
        self.assertEqual(out["checks"]["cart-tests"]["status"],
                         "not_applicable")

    def test_stale_input_not_reused(self):
        self.policy["checks"][0]["applicability"] = "always"
        graph = self.graph()
        baseline = self.baseline()
        check = self.policy["checks"][0]
        snap = checks_inventory.snapshot(
            self.proj.root, self.policy,
            checks_inventory.resolved_inputs(self.policy, check),
            now="2026-11-08T12:00:00+03:00")
        result = self._result(check, self.policy, graph, snap)
        self.proj.write("src/payments/pay.py", "x = 2\n")
        out = self.plan(baseline=baseline, graph=self.graph(),
                        results=[result])
        self.assertEqual(out["checks"]["payments-tests"]["status"],
                         "run")

    def test_newer_failed_hides_older_passed(self):
        self.policy["checks"][0]["applicability"] = "always"
        graph = self.graph()
        baseline = self.baseline()
        check = self.policy["checks"][0]
        snap = checks_inventory.snapshot(
            self.proj.root, self.policy,
            checks_inventory.resolved_inputs(self.policy, check),
            now="2026-11-08T12:00:00+03:00")
        ok = self._result(check, self.policy, graph, snap,
                          finished="2026-11-08T12:30:00+03:00")
        fail = self._result(check, self.policy, graph, snap,
                            status="failed", result_id="r2",
                            finished="2026-11-08T13:00:00+03:00")
        fail["tests"] = {"expected": ["t.t"],
                         "actual": [{"id": "t.t", "outcome": "failed"}]}
        checks_contract.validate_result(fail, req)
        out = self.plan(baseline=baseline, graph=graph,
                        results=[ok, fail])
        self.assertEqual(out["checks"]["payments-tests"]["status"],
                         "run")

    def test_environment_mismatch_not_reused(self):
        self.policy["checks"][0]["applicability"] = "always"
        graph = self.graph()
        baseline = self.baseline()
        check = self.policy["checks"][0]
        snap = checks_inventory.snapshot(
            self.proj.root, self.policy,
            checks_inventory.resolved_inputs(self.policy, check),
            now="2026-11-08T12:00:00+03:00")
        result = self._result(check, self.policy, graph, snap,
                              env={"platform": "other",
                                   "python": "0.0"})
        out = self.plan(baseline=baseline, graph=graph,
                        results=[result])
        self.assertEqual(out["checks"]["payments-tests"]["status"],
                         "run")


class CliPlanTest(unittest.TestCase):
    """checks plan на установленном проекте (CLI, read-only)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self._install()

    def _install(self):
        code = playbook.main(["init", str(self.root), "--apply"])
        self.assertEqual(code, 0)
        proj_file = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(proj_file.read_text())
        project["setup_status"] = "configured"
        project["checks_policy"] = "checks-policy.json"
        proj_file.write_text(json.dumps(project, ensure_ascii=False,
                                        indent=2) + "\n")
        policy = make_policy()
        (self.root / "checks-policy.json").write_text(
            json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
        for name in ("index.md", "product.md", "architecture.md",
                     "code-map.md", "roadmap.md"):
            doc = self.root / "docs" / name
            doc.write_text(doc.read_text().replace("TODO:", "OK:"))
        for rel in ("src/payments/pay.py", "src/checkout/order.py",
                    "src/cart/basket.py"):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x = 1\n")
        task_dir = self.root / "docs" / "tasks"
        (task_dir / "DEV-1.md").write_text(
            "# DEV-1: Тест\n\nID: DEV-1\nStatus: ready\nPriority: P2\n"
            "Depends-On: -\nExecutor: -\nModel: -\n"
            "Created: 2026-11-08T10:00:00+03:00\nClosed: -\n\n"
            "## Задача\n\n### Цель\n\nЦель задачи.\n\n"
            "### Критерии приёмки\n\n- [ ] проверить\n\n## Журнал\n")
        with contextlib.redirect_stdout(io.StringIO()):
            code = playbook.main(["--root", str(self.root), "index"])
        self.assertEqual(code, 0)

    def run_plan(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = playbook.main(
                ["--root", str(self.root), "checks", "plan", "DEV-1"])
        return code, out.getvalue() + err.getvalue()

    def test_plan_prints_selection(self):
        code, text = self.run_plan()
        self.assertEqual(code, 0, text)
        self.assertIn("checks plan DEV-1", text)
        self.assertIn("payments-tests: run", text)
        self.assertIn("Итог:", text)

    def test_plan_read_only(self):
        before = {str(p.relative_to(self.root))
                  for p in self.root.rglob("*") if p.is_file()}
        self.run_plan()
        after = {str(p.relative_to(self.root))
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_plan_requires_policy_link(self):
        proj_file = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(proj_file.read_text())
        project.pop("checks_policy")
        proj_file.write_text(json.dumps(project))
        code, _ = self.run_plan()
        self.assertEqual(code, 1)

    def test_plan_requires_task(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = playbook.main(
                ["--root", str(self.root), "checks", "plan", "DEV-99"])
        self.assertEqual(code, 1)
        self.assertIn("DEV-99", err.getvalue())


if __name__ == "__main__":
    unittest.main()
