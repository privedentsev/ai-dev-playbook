"""PB-059 AC-01: Python AST-анализатор, provenance-граф, union и unknown."""
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
import checks_graph as cg


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
            {"id": "shared-util", "title": "Утилиты",
             "subsystem_id": "catalog", "paths": ["src/shared/**"]},
        ],
        "checks": [{
            "id": "payments-tests", "title": "t", "purpose": "p",
            "kind": "test", "modules": ["payments"],
            "adapter": "python-unittest",
            "argv": ["python3", "-m", "unittest", "tests.test_payments"],
            "timeout_seconds": 5}],
        "dependencies": [],
        "excludes": [],
        "environment": {"toolchain": [], "external": []},
        "fallback": {"expansion_order": ["module", "subsystem", "project"],
                     "approved_commands": [],
                     "limits": {"max_seconds": 10},
                     "unknown_scope": "project"},
        "test_catalog": {"adapter": "python-unittest",
                         "fingerprint": "sha256:" + "a" * 64,
                         "entries": [{"id": "t.t", "description": "d"}]},
    }
    policy.update(overrides)
    return policy


class Project:
    """Фикстурный проект для графа: файлы и policy на диске."""
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


def edge_set(graph):
    return {(e["consumer"], e["dependency"], e["kind"], e["source"])
            for e in graph["edges"]}


class AnalyzerTest(unittest.TestCase):
    """Разбор одного файла: разрешение импортов и динамика."""

    def test_direct_from_relative_imports(self):
        """Прямые, from- и относительные импорты разрешаются в файлы проекта"""
        files = {"src/payments/pay.py", "src/shared/util.py",
                 "src/shared/__init__.py", "src/payments/__init__.py",
                 "src/payments/helper.py"}
        source = ("import shared.util\n"
                  "from shared import util\n"
                  "from . import helper\n"
                  "from .helper import fn\n")
        found = cg.analyze_python("src/payments/pay.py", source, files)
        self.assertEqual(set(found["targets"]),
                         {"src/shared/util.py", "src/shared/__init__.py",
                          "src/payments/helper.py",
                          "src/payments/__init__.py"})
        self.assertEqual(found["dynamic"], [])

    def test_src_layout_from_unrelated_dir(self):
        """src-layout разрешается из несвязанного каталога тестов"""
        files = {"tests/test_x.py", "src/payments/pay.py"}
        found = cg.analyze_python("tests/test_x.py",
                                  "import payments.pay\n", files)
        self.assertEqual(set(found["targets"]),
                         {"src/payments/pay.py"})

    def test_parent_relative_import(self):
        """Родительский относительный импорт from ..payments разрешается"""
        files = {"src/checkout/order.py", "src/payments/pay.py"}
        found = cg.analyze_python(
            "src/checkout/order.py",
            "from ..payments import pay\n", files)
        self.assertIn("src/payments/pay.py", found["targets"])

    def test_stdlib_and_external_ignored(self):
        """Stdlib и внешние пакеты не создают рёбер графа"""
        files = {"a/x.py"}
        found = cg.analyze_python(
            "a/x.py",
            "import os, sys, json\nimport requests\n", files)
        self.assertEqual(found["targets"], {})
        self.assertEqual(found["dynamic"], [])

    def test_dynamic_import_flagged(self):
        """importlib.import_module и __import__ с переменной помечаются dynamic"""
        files = {"a/x.py"}
        source = ("import importlib\n"
                  "importlib.import_module(name)\n"
                  "__import__(var)\n")
        found = cg.analyze_python("a/x.py", source, files)
        self.assertEqual(len(found["dynamic"]), 2)

    def test_dynamic_alias_flagged(self):
        """from importlib import import_module с переменной помечается dynamic"""
        files = {"a/x.py"}
        found = cg.analyze_python(
            "a/x.py",
            "from importlib import import_module\n"
            "import_module(chosen)\n", files)
        self.assertEqual(len(found["dynamic"]), 1)

    def test_dynamic_import_alias(self):
        # A-03: локальный alias исходной функции — динамика, не край.
        """import_module as load с переменной помечается dynamic"""
        files = {"a/x.py"}
        found = cg.analyze_python(
            "a/x.py",
            "from importlib import import_module as load\n"
            "load(chosen)\n", files)
        self.assertEqual(len(found["dynamic"]), 1)
        self.assertEqual(found["targets"], {})

    def test_dynamic_module_alias(self):
        # A-03: import importlib as il; il.import_module — динамика.
        """importlib as il + il.import_module(переменная) помечается dynamic"""
        files = {"a/x.py"}
        found = cg.analyze_python(
            "a/x.py",
            "import importlib as il\n"
            "il.import_module(chosen)\n", files)
        self.assertEqual(len(found["dynamic"]), 1)

    def test_literal_dynamic_resolved(self):
        """import_module с литералом разрешается в обычное ребро"""
        files = {"a/x.py", "b/mod.py"}
        found = cg.analyze_python(
            "a/x.py",
            "import importlib\n"
            "importlib.import_module('b.mod')\n", files)
        self.assertIn("b/mod.py", found["targets"])
        self.assertEqual(found["dynamic"], [])

    def test_syntax_error_raises(self):
        """Синтаксическая ошибка в анализируемом файле поднимает SyntaxError"""
        with self.assertRaises(SyntaxError):
            cg.analyze_python("a/x.py", "def broken(:\n", {"a/x.py"})


class GraphBuildTest(unittest.TestCase):
    """Построение графа: analyzer-рёбра provenance, объединение
    policy-рёбер, отсутствие самоссылок, циклы и свежесть рёбер."""
    def setUp(self):
        self.proj = Project(self)
        self.policy = make_policy()
        self.proj.write("src/payments/__init__.py", "")
        self.proj.write("src/payments/pay.py",
                        "import shared.util\n")
        self.proj.write("src/checkout/__init__.py", "")
        self.proj.write("src/checkout/order.py",
                        "import payments.pay\n")
        self.proj.write("src/cart/__init__.py", "")
        self.proj.write("src/shared/__init__.py", "")
        self.proj.write("src/shared/util.py", "")

    def build(self, **kw):
        kw.setdefault("now", "2026-11-08T12:00:00+03:00")
        return cg.build_graph(self.proj.root, self.policy, **kw)

    def test_analyzer_edges_provenance(self):
        """Рёбра анализатора получают provenance и проходят схему графа"""
        graph = self.build()
        checks_contract.validate_graph(graph, req)
        self.assertEqual(graph["analyzer_version"], cg.ANALYZER_VERSION)
        edges = {e["consumer"]: e for e in graph["edges"]
                 if e["source"] == "analyzer"}
        self.assertEqual(edges["module:checkout"]["dependency"],
                         "module:payments")
        self.assertEqual(edges["module:checkout"]["analyzer_version"],
                         "ast-v1")
        self.assertTrue(edges["module:checkout"]["evidence"]
                        .startswith("src/checkout/order.py:"))
        self.assertEqual(edges["module:payments"]["dependency"],
                         "module:shared-util")

    def test_policy_edges_merged(self):
        """Policy-рёбра объединяются с analyzer-рёбрами в графе"""
        self.policy["dependencies"] = [
            {"consumer": "module:checkout",
             "dependency": "module:cart", "kind": "api",
             "source": "explicit"},
            {"consumer": "check:payments-tests",
             "dependency": "module:payments", "kind": "resource",
             "source": "observed", "evidence": "run"}]
        graph = self.build()
        keys = {(e["consumer"], e["dependency"], e["kind"], e["source"])
                for e in graph["edges"]}
        self.assertIn(("module:checkout", "module:cart", "api",
                       "explicit"), keys)
        self.assertIn(("check:payments-tests", "module:payments",
                       "resource", "observed"), keys)

    def test_same_module_import_no_edge(self):
        """Импорт внутри того же модуля не создаёт ребра"""
        self.proj.write("src/payments/more.py",
                        "from . import pay\n")
        graph = self.build()
        self.assertNotIn(("module:payments", "module:payments",
                          "import", "analyzer"), edge_set(graph))

    def test_cycle_no_hang(self):
        """Циклические импорты между модулями не вешают построение графа"""
        self.proj.write("src/payments/peer.py",
                        "import checkout.order\n")
        self.proj.write("src/checkout/order.py",
                        "import payments.pay\nimport payments.peer\n")
        graph = self.build()
        self.assertIn(("module:checkout", "module:payments",
                       "import", "analyzer"), edge_set(graph))
        self.assertIn(("module:payments", "module:checkout",
                       "import", "analyzer"), edge_set(graph))
        reached = cg.reverse_closure(graph["edges"], "module:cart")
        self.assertEqual(set(reached), {"module:cart"})
        reached = cg.reverse_closure(graph["edges"],
                                     "module:payments")
        self.assertIn("module:checkout", reached)
        self.assertIn("module:payments", reached)  # цикл замкнут

    def test_deleted_file_keeps_old_consumers_via_union(self):
        """Удалённый файл сохраняет старых потребителей через union"""
        first = self.build()
        self.assertIn(("module:checkout", "module:payments",
                       "import", "analyzer"), edge_set(first))
        (self.proj.root / "src/payments/pay.py").unlink()
        second = self.build(previous=first)
        # Свежий анализ не нашёл импорта, но union сохранил ребро.
        self.assertIn(("module:checkout", "module:payments",
                       "import", "analyzer"), edge_set(second))
        reached = cg.reverse_closure(second["edges"],
                                     "module:payments")
        self.assertIn("module:checkout", reached)

    def test_fresh_edge_wins_same_key(self):
        """Свежая analyzer-edge побеждает устаревшую policy-edge с тем же ключом"""
        first = self.build()
        stale = dict(first)
        stale["edges"] = [dict(e, evidence="old:1")
                          if e["consumer"] == "module:checkout"
                          else e for e in first["edges"]]
        second = self.build(previous=stale)
        edge = [e for e in second["edges"]
                if e["consumer"] == "module:checkout"
                and e["source"] == "analyzer"][0]
        self.assertNotEqual(edge["evidence"], "old:1")

    def test_union_merges_unknown(self):
        """Union объединяет unknown-записи двух версий графа"""
        first = self.build()
        stale_unknown = dict(first)
        stale_unknown["unknown"] = [
            {"scope": {"kind": "module", "id": "cart"},
             "reason": "был динамический импорт"}]
        second = self.build(previous=stale_unknown)
        self.assertIn({"scope": {"kind": "module", "id": "cart"},
                       "reason": "был динамический импорт"},
                      second["unknown"])

    def test_dynamic_import_unknown(self):
        """Динамический импорт попадает в unknown графа"""
        self.proj.write("src/checkout/plugins.py",
                        "import importlib\n"
                        "importlib.import_module(name)\n")
        graph = self.build()
        self.assertIn({"scope": {"kind": "module", "id": "checkout"},
                       "reason": "src/checkout/plugins.py: динамический "
                                 "импорт, строка 2"},
                      graph["unknown"])

    def test_syntax_error_unknown(self):
        """Синтаксическая ошибка файла попадает в unknown графа"""
        self.proj.write("src/cart/broken.py", "def broken(:\n")
        graph = self.build()
        unknown = [u for u in graph["unknown"]
                   if u["scope"] == {"kind": "module", "id": "cart"}]
        self.assertEqual(len(unknown), 1)
        self.assertIn("неразбираемый Python", unknown[0]["reason"])

    def test_import_outside_modules_unknown(self):
        """Импорт вне путей модулей попадает в unknown"""
        self.proj.write("misc/tool.py", "Y = 2\n")
        self.proj.write("src/payments/use_tool.py",
                        "import misc.tool\n")
        # misc/ — верхнеуровневый базис разрешения, файл найден, но
        # владельца нет: зависимость вне модулей — unknown, не край.
        graph = self.build()
        self.assertIn(
            {"scope": {"kind": "module", "id": "payments"},
             "reason": "src/payments/use_tool.py: импорт вне модулей "
                       "misc/tool.py"},
            graph["unknown"])

    def test_dynamic_outside_modules_project_scope(self):
        """Динамический импорт вне модулей при scope=project помечается"""
        self.proj.write("tools/loader.py",
                        "__import__(choice)\n")
        graph = self.build()
        self.assertEqual([u["scope"] for u in graph["unknown"]],
                         [{"kind": "project"}])

    def test_graph_compatible(self):
        """Построенный граф совместим с policy"""
        graph = self.build()
        self.assertTrue(cg.graph_compatible(graph, self.policy)
                        ["compatible"])
        self.policy["revision"] = 2
        verdict = cg.graph_compatible(graph, self.policy)
        self.assertFalse(verdict["compatible"])
        other = dict(graph, project_id="elsewhere")
        self.assertFalse(cg.graph_compatible(other, self.policy)
                         ["compatible"])

    def test_foreign_graph_not_unionized(self):
        """Граф другого project_id не объединяется union'ом"""
        first = self.build()
        foreign = dict(first, project_id="elsewhere")
        second = self.build(previous=foreign)
        self.assertEqual(second["edges"], first["edges"])

    def test_load_graph_strict(self):
        """load_graph строго валидирует и отклоняет повреждённый граф"""
        graph = self.build()
        target = self.proj.root / "graph.json"
        target.write_text(json.dumps(graph), encoding="utf-8")
        self.assertEqual(cg.load_graph(target), graph)
        target.write_text(json.dumps({"kind": "report"}))
        with self.assertRaises(ValueError):
            cg.load_graph(target)


if __name__ == "__main__":
    unittest.main()
