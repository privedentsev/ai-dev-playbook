"""PB-063: setup preview/apply — обследование, одобренная политика, recovery.

AC-01: preview не пишет и не исполняет код проекта, предсказывает модули,
проверки, исключения и unknown; apply пишет только одобренную политику
через восстановимую операцию.
AC-02: legacy-поля и task_workflow v2 project.json сохраняются; повторное
применение, невалидная схема, TODO-основание и прерывание отклоняются или
откатываются.
AC-03: новые файлы наследуют модульные пути и исключения политики.
"""
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
import checks_inventory
import checks_setup
import documents
import file_operations
from test_cli import CliCase
from test_playbook import playbook

TEST_A = '''"""Модуль тестов."""
import unittest


class TestA(unittest.TestCase):
    """Класс тестов."""

    def test_one(self):
        """Первый сценарий."""

    def test_two(self):
        pass
'''

TEST_B = '''import unittest


class TB(unittest.TestCase):
    def test_b(self):
        pass
'''


def discover_ids(root, start):
    """Фактические stable test ID unittest discovery (как адаптер)."""
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(root / start),
                            top_level_dir=str(root / start))
    ids = []

    def flat(items):
        for item in items:
            if isinstance(item, unittest.TestSuite):
                flat(item)
            else:
                ids.append(item.id())
    flat(suite)
    return set(ids)


def snapshot_tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


class PreviewCase(unittest.TestCase):
    """setup preview: read-only обследование, предсказанный каталог vs
    фактический discover, unknowns, load_tests/parse errors и импорты."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_catalog_matches_real_discovery(self):
        """Предсказанный каталог setup preview совпадает с unittest discover"""
        self.write("src/pkg/__init__.py", "")
        self.write("src/pkg/mod.py", "X = 1\n")
        self.write("tests/test_a.py", TEST_A)
        self.write("tests/sub/__init__.py", "")
        self.write("tests/sub/test_b.py", TEST_B)
        result = checks_setup.survey(self.root)
        starts = {c["start_dir"]: c for c in result["checks"]}
        self.assertIn("tests", starts)
        predicted = {e["id"] for e in result["catalog_entries"]
                     if e["group"] == starts["tests"]["id"]}
        self.assertEqual(predicted, discover_ids(self.root, "tests"))
        self.assertIn("test_a.TestA.test_one", predicted)
        self.assertIn("sub.test_b.TB.test_b", predicted)
        # Модули и исключения предложены.
        self.assertEqual({m["dir"] for m in result["modules"]},
                         {"src", "tests"})
        policy = checks_setup.build_policy(result, self.root.name)
        checks_contract.validate_policy(policy, playbook.require)

    def test_preview_writes_nothing_and_runs_no_code(self):
        """Preview ничего не пишет и не исполняет код проекта"""
        sentinel = self.root / "tests" / "sentinel.txt"
        self.write("tests/test_side.py",
                   "import pathlib\nimport unittest\n"
                   f"pathlib.Path({str(sentinel)!r}).write_text('x')\n"
                   "class T(unittest.TestCase):\n"
                   "    def test_a(self):\n"
                   "        pass\n")
        before = snapshot_tree(self.root)
        lines = []
        checks_setup.preview(self.root, out=lines)
        self.assertFalse(sentinel.exists(),
                         "preview исполнил код тестового файла")
        self.assertEqual(before, snapshot_tree(self.root),
                         "preview изменил дерево проекта")
        self.assertTrue(any("test_side.T.test_a" in line
                            for line in lines))

    def test_preview_reports_excludes_unknowns_and_problems(self):
        """Preview перечисляет excludes, unknowns и проблемы"""
        self.write(".venv/lib.py", "X = 1\n")
        self.write("node_modules/pkg.py", "X = 1\n")
        self.write("README.md", "doc\n")
        self.write("tests/test_ok.py", TEST_B)
        (self.root / "link.py").symlink_to("tests/test_ok.py")
        result = checks_setup.survey(self.root)
        patterns = {e["pattern"] for e in result["excludes"]}
        self.assertTrue({".venv/**", "node_modules/**"} <= patterns)
        self.assertIn("README.md", result["unknowns"]["root_files"])
        self.assertTrue(any("символическая ссылка link.py" in p
                            for p in result["unknowns"]["problems"]))
        self.assertNotIn(".venv", {m["dir"] for m in result["modules"]})
        # Тест внутри исключаемой области не получает проверку.
        self.write(".github/test_x.py", TEST_B)
        result = checks_setup.survey(self.root)
        self.assertIn(".github/test_x.py",
                      result["unknowns"]["orphan_test_files"])

    def test_load_tests_and_parse_errors_surfaced(self):
        """load_tests-файлы и ошибки разбора попадают в unknowns"""
        self.write("tests/test_smoke.py",
                   "import unittest\n\n"
                   "def load_tests(loader, tests, pattern):\n"
                   "    return tests\n\n"
                   "class T(unittest.TestCase):\n"
                   "    def test_a(self):\n"
                   "        pass\n")
        self.write("tests/test_broken.py", "def broken(:\n")
        result = checks_setup.survey(self.root)
        self.assertEqual(result["unknowns"]["load_tests_files"],
                         ["tests/test_smoke.py"])
        self.assertTrue(any("test_broken.py" in e
                            for e in result["unknowns"]["parse_errors"]))
        # load_tests-файл не даёт предсказанных ID.
        self.assertFalse(any(e["id"].startswith("test_smoke.")
                             for e in result["catalog_entries"]))

    def test_unresolved_and_ambiguous_imports_reported(self):
        """Неразрешённые и неоднозначные импорты перечислены в unknowns"""
        self.write("src/a.py", "import missing_pkg\n")
        self.write("src/b.py", "import os\nimport json\n")
        self.write("lib/one.py", "")
        lib2 = self.root / "other" / "one.py"
        lib2.parent.mkdir(parents=True)
        lib2.write_text("", encoding="utf-8")
        self.write("src/c.py", "import one\n")
        result = checks_setup.survey(self.root)
        self.assertIn("missing_pkg",
                      result["unknowns"]["unresolved_imports"])
        self.assertTrue(any("import one" in a and "владельцы" in a
                            for a in result["unknowns"]["ambiguous_imports"]))
        deps = {(e["consumer"], e["dependency"]) for e in result["dependencies"]}
        self.assertNotIn(("module:src", "module:src"), deps)

    def test_inherited_testcase_methods_match_discovery(self):
        # A-01: Base/Child в одном файле — унаследованные test-методы
        # собираются discover'ом под именем конкретного класса.
        """Унаследованные TestCase-методы совпадают с discover"""
        self.write("tests/test_inherit.py",
                   "import unittest\n\n\n"
                   "class InhBase(unittest.TestCase):\n"
                   "    def test_ok(self):\n"
                   "        pass\n\n\n"
                   "class InhChild(InhBase):\n"
                   "    pass\n")
        result = checks_setup.survey(self.root)
        predicted = {e["id"] for e in result["catalog_entries"]}
        self.assertEqual(predicted, discover_ids(self.root, "tests"))
        self.assertIn("test_inherit.InhChild.test_ok", predicted)
        self.assertEqual(result["unknowns"]["class_uncertainties"], [])

    def test_cross_file_base_class_and_mixin(self):
        # База в соседнем файле и mixin с test-методом: ID совпадают
        # с реальным discover.
        """База из соседнего файла и mixin дают ID как у discover"""
        self.write("tests/xbase_inh.py",
                   "import unittest\n\n\n"
                   "class XBaseCase(unittest.TestCase):\n"
                   "    def test_shared(self):\n"
                   "        pass\n")
        self.write("tests/xmix_inh.py",
                   "class XMix:\n"
                   "    def test_mixin(self):\n"
                   "        pass\n")
        self.write("tests/test_xfile.py",
                   "import unittest\n"
                   "from xbase_inh import XBaseCase\n"
                   "from xmix_inh import XMix\n\n\n"
                   "class XT(XBaseCase, XMix):\n"
                   "    def test_own(self):\n"
                   "        pass\n")
        result = checks_setup.survey(self.root)
        predicted = {e["id"] for e in result["catalog_entries"]}
        self.assertEqual(predicted, discover_ids(self.root, "tests"))
        for expected in ("test_xfile.XT.test_shared",
                         "test_xfile.XT.test_mixin",
                         "test_xfile.XT.test_own",
                         "xbase_inh.XBaseCase.test_shared"):
            self.assertIn(expected, predicted)

    def test_imported_testcase_class_collected_in_module(self):
        # TestCase-класс, импортированный в тестовый модуль, собирается
        # discover'ом под именем класса в namespace модуля.
        """Импортированный TestCase-класс собирается по __module__ определения"""
        self.write("tests/xhelpers_imp.py",
                   "import unittest\n\n\n"
                   "class XHelperTests(unittest.TestCase):\n"
                   "    def test_helper(self):\n"
                   "        pass\n")
        self.write("tests/test_ximp.py",
                   "from xhelpers_imp import XHelperTests\n")
        result = checks_setup.survey(self.root)
        predicted = {e["id"] for e in result["catalog_entries"]}
        self.assertEqual(predicted, discover_ids(self.root, "tests"))
        self.assertIn("xhelpers_imp.XHelperTests.test_helper", predicted)

    def test_unresolvable_base_surfaced_in_unknowns(self):
        """Неразрешённая база класса попадает в class_uncertainties"""
        self.write("tests/test_xunres.py",
                   "import unittest\n\n\n"
                   "class T(unittest.TestCase, ExternalBase):\n"
                   "    def test_ok(self):\n"
                   "        pass\n")
        result = checks_setup.survey(self.root)
        self.assertTrue(any("test_xunres.py:T" in u and "ExternalBase" in u
                            for u in
                            result["unknowns"]["class_uncertainties"]))

    def test_unresolvable_class_like_import_surfaced(self):
        """Неразрешённый class-like импорт попадает в class_uncertainties"""
        self.write("tests/test_ximp2.py",
                   "import unittest\n"
                   "from missing_pkg import BaseTests, helper_fn\n\n\n"
                   "class T(unittest.TestCase):\n"
                   "    def test_ok(self):\n"
                   "        pass\n")
        result = checks_setup.survey(self.root)
        self.assertTrue(any("missing_pkg" in u and "BaseTests" in u
                            for u in
                            result["unknowns"]["class_uncertainties"]))
        # lowercase-функции не помечаются как возможные классы.
        self.assertFalse(any("helper_fn" in u
                             for u in
                             result["unknowns"]["class_uncertainties"]))

    def test_reexport_chain_matches_discovery(self):
        # A-03: base.py определяет Base, helpers.py реэкспортирует,
        # test_a.py импортирует из helpers — ID по __module__ базы.
        self.write("tests/base_re.py",
                   "import unittest\n\n\n"
                   "class ReBase(unittest.TestCase):\n"
                   "    def test_ok(self):\n"
                   "        pass\n")
        self.write("tests/xhelpers_re.py",
                   "from base_re import ReBase\n")
        self.write("tests/test_xre.py",
                   "from xhelpers_re import ReBase\n")
        result = checks_setup.survey(self.root)
        predicted = {e["id"] for e in result["catalog_entries"]}
        self.assertEqual(predicted, discover_ids(self.root, "tests"))
        self.assertIn("base_re.ReBase.test_ok", predicted)
        self.assertEqual(result["unknowns"]["class_uncertainties"], [])

    def test_reexport_cycle_and_unresolvable_surfaced(self):
        # Цикл реэкспорта и неразрешённое имя в разрешённом модуле —
        # в class_uncertainties, без зацикливания.
        self.write("tests/xcyc_a.py", "from xcyc_b import CycBase\n")
        self.write("tests/xcyc_b.py", "from xcyc_a import CycBase\n")
        self.write("tests/xhelpers_ur.py",
                   "from missing_deep import UrBase\n")
        self.write("tests/test_xur.py",
                   "import unittest\n"
                   "from xcyc_a import CycBase\n"
                   "from xhelpers_ur import UrBase\n\n\n"
                   "class T(unittest.TestCase):\n"
                   "    def test_ok(self):\n"
                   "        pass\n")
        result = checks_setup.survey(self.root)
        unc = result["unknowns"]["class_uncertainties"]
        self.assertTrue(any("CycBase" in u for u in unc))
        self.assertTrue(any("UrBase" in u for u in unc))

    def test_shadowed_test_method_removed_from_catalog(self):
        # A-04: Child.test_ok = None затеняет унаследованный метод —
        # discover его не собирает, каталог не должен предлагать.
        self.write("tests/test_shadow.py",
                   "import unittest\n\n\n"
                   "class ShBase(unittest.TestCase):\n"
                   "    def test_ok(self):\n"
                   "        pass\n\n\n"
                   "class ShChild(ShBase):\n"
                   "    test_ok = None\n")
        result = checks_setup.survey(self.root)
        predicted = {e["id"] for e in result["catalog_entries"]}
        self.assertEqual(predicted, discover_ids(self.root, "tests"))
        self.assertIn("test_shadow.ShBase.test_ok", predicted)
        self.assertNotIn("test_shadow.ShChild.test_ok", predicted)

    def test_lambda_assignment_is_collected_test(self):
        # test_x = lambda self: ... — callable, discover собирает.
        self.write("tests/test_lam.py",
                   "import unittest\n\n\n"
                   "class LamCase(unittest.TestCase):\n"
                   "    test_dyn = lambda self: None\n")
        result = checks_setup.survey(self.root)
        predicted = {e["id"] for e in result["catalog_entries"]}
        self.assertEqual(predicted, discover_ids(self.root, "tests"))
        self.assertIn("test_lam.LamCase.test_dyn", predicted)

    def test_unknown_expr_shadow_surfaced_in_unknowns(self):
        # test_ok = make() — вызываемость не определена статически:
        # метод исключён из каталога и помечен в class_uncertainties.
        self.write("tests/test_unk.py",
                   "import unittest\n\n\n"
                   "def make():\n"
                   "    return None\n\n\n"
                   "class UnkBase(unittest.TestCase):\n"
                   "    def test_ok(self):\n"
                   "        pass\n\n\n"
                   "class UnkChild(UnkBase):\n"
                   "    test_ok = make()\n")
        result = checks_setup.survey(self.root)
        self.assertTrue(any("test_unk.py:UnkChild" in u and "test_ok" in u
                            for u in
                            result["unknowns"]["class_uncertainties"]))
        predicted = {e["id"] for e in result["catalog_entries"]}
        self.assertNotIn("test_unk.UnkChild.test_ok", predicted)

    def test_mro_diamond_shadowing_matches_discovery(self):
        # A-04/MRO: Child(Left, Right), Left.test_ok = None затеняет
        # унаследованный от Base метод по C3-порядку — правая ветвь
        # не должна возвращать его в каталог.
        self.write("tests/test_mro.py",
                   "import unittest\n\n\n"
                   "class MroBase(unittest.TestCase):\n"
                   "    def test_ok(self):\n"
                   "        pass\n\n\n"
                   "class MroLeft(MroBase):\n"
                   "    test_ok = None\n\n\n"
                   "class MroRight(MroBase):\n"
                   "    pass\n\n\n"
                   "class MroChild(MroLeft, MroRight):\n"
                   "    pass\n")
        result = checks_setup.survey(self.root)
        predicted = {e["id"] for e in result["catalog_entries"]}
        self.assertEqual(predicted, discover_ids(self.root, "tests"))
        self.assertIn("test_mro.MroBase.test_ok", predicted)
        self.assertIn("test_mro.MroRight.test_ok", predicted)
        self.assertNotIn("test_mro.MroChild.test_ok", predicted)
        self.assertNotIn("test_mro.MroLeft.test_ok", predicted)

    def test_factory_testcase_class_surfaced_in_unknowns(self):
        # A-05: Base = type("Base", (TestCase,), {...}) — вызов может
        # дать класс: имя не считается доказанно не-классом, preview
        # не молчит, а помечает class-like импорт неопределённостью.
        self.write("tests/factory_base.py",
                   "import unittest\n\n\n"
                   "FactoryBase = type(\"FactoryBase\",\n"
                   "                  (unittest.TestCase,),\n"
                   "                  {\"__module__\": __name__,\n"
                   "                   \"test_ok\": lambda self: None})\n")
        self.write("tests/test_xfac.py",
                   "from factory_base import FactoryBase\n")
        result = checks_setup.survey(self.root)
        unc = result["unknowns"]["class_uncertainties"]
        self.assertTrue(any("FactoryBase" in u for u in unc), unc)
        predicted = {e["id"] for e in result["catalog_entries"]}
        self.assertNotIn("factory_base.FactoryBase.test_ok", predicted)

    def test_container_element_import_surfaced_in_unknowns(self):
        # A-05: Base = [Original][0] — элемент коллекции может быть
        # TestCase-классом; Subscript/Attribute не доказывают не-класс,
        # поэтому class-like импорт помечается неопределённостью.
        self.write("tests/base_el.py",
                   "import unittest\n\n\n"
                   "class Original(unittest.TestCase):\n"
                   "    def test_ok(self):\n"
                   "        pass\n\n\n"
                   "Base = [Original][0]\n")
        self.write("tests/test_el.py",
                   "from base_el import Base\n")
        result = checks_setup.survey(self.root)
        unc = result["unknowns"]["class_uncertainties"]
        self.assertTrue(any("Base" in u for u in unc), unc)

    def test_module_rebind_drops_class_from_catalog(self):
        # A-06: class Base(TestCase) с последующим Base = None не
        # остаётся в namespace — discover его не собирает, каталог
        # не должен предлагать записи.
        self.write("tests/test_rebind.py",
                   "import unittest\n\n\n"
                   "class RebindBase(unittest.TestCase):\n"
                   "    def test_ok(self):\n"
                   "        pass\n\n\n"
                   "RebindBase = None\n")
        result = checks_setup.survey(self.root)
        predicted = {e["id"] for e in result["catalog_entries"]}
        self.assertEqual(predicted, discover_ids(self.root, "tests"))
        self.assertFalse(any("RebindBase" in i for i in predicted))
        self.assertFalse(
            result["unknowns"]["class_uncertainties"])

    def test_module_rebind_unknown_surfaced_in_unknowns(self):
        # A-06: Base = make() после class Base — имя перепривязано
        # выражением неизвестного типа: записи нет, но есть
        # неопределённость вместо молчаливого расхождения.
        self.write("tests/test_rebind_unk.py",
                   "import unittest\n\n\n"
                   "def make():\n"
                   "    return None\n\n\n"
                   "class RebindUnk(unittest.TestCase):\n"
                   "    def test_ok(self):\n"
                   "        pass\n\n\n"
                   "RebindUnk = make()\n")
        result = checks_setup.survey(self.root)
        predicted = {e["id"] for e in result["catalog_entries"]}
        self.assertFalse(any("RebindUnk" in i for i in predicted))
        unc = result["unknowns"]["class_uncertainties"]
        self.assertTrue(any("RebindUnk" in u for u in unc), unc)

    def test_imported_class_rebind_drops_from_catalog(self):
        # A-06: from base import Base; Base = None — импортированное
        # имя вытеснено литералом: записей нет и неопределённости нет.
        self.write("tests/rebind_base.py",
                   "import unittest\n\n\n"
                   "class OrigBase(unittest.TestCase):\n"
                   "    def test_ok(self):\n"
                   "        pass\n")
        self.write("tests/test_irebind.py",
                   "from rebind_base import OrigBase as Base\n\n"
                   "Base = None\n")
        result = checks_setup.survey(self.root)
        predicted = {e["id"] for e in result["catalog_entries"]}
        self.assertEqual(predicted, discover_ids(self.root, "tests"))
        self.assertFalse(any("OrigBase" in i for i in predicted))


def policy_fixture(**overrides):
    policy = {
        "schema_version": checks_contract.POLICY_SCHEMA_VERSION,
        "policy_id": "fixture-checks",
        "revision": 1,
        "project_id": "fixture",
        "trust_mode": "local",
        "approved_basis": {
            "who": "Engineer", "at": "2026-01-01T00:00:00+00:00",
            "source": "test", "detail": "одобрено в тесте"},
        "subsystems": [{"id": "core", "title": "Core"}],
        "modules": [
            {"id": "app", "title": "App", "subsystem_id": "core",
             "paths": ["src/**"]},
            {"id": "tests", "title": "Tests", "subsystem_id": "core",
             "paths": ["tests/**"]},
        ],
        "checks": [{
            "id": "unit", "title": "Unit", "purpose": "поведение",
            "kind": "test", "modules": ["app", "tests"],
            "adapter": checks_contract.BUILTIN_ADAPTER,
            "argv": ["python3", "-m", "unittest", "discover",
                     "-s", "tests", "-v"],
            "timeout_seconds": 60}],
        "dependencies": [],
        "excludes": [{"pattern": ".venv/**",
                      "reason": "окружение", "origin": "manual"}],
        "environment": {"toolchain": [], "external": []},
        "fallback": {
            "expansion_order": list(checks_contract.EXPANSION_ORDER),
            "approved_commands": [],
            "limits": {"max_seconds": 600},
            "unknown_scope": "project"},
        "test_catalog": {
            "adapter": checks_contract.BUILTIN_ADAPTER,
            "fingerprint": "sha256:" + "0" * 64,
            "entries": [{"id": "test_app.TestApp.test_ok",
                         "description": "сценарий → результат",
                         "group": "unit"}]},
    }
    policy.update(overrides)
    return policy


class ApplyCase(CliCase):
    """setup apply: запись policy и связи project.json, сохранение
    v2/legacy, отказы, recovery и наследование правил модулями."""
    def setUp(self):
        super().setUp()
        self.install()
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("X = 1\n",
                                                 encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_app.py").write_text(
            "import unittest\n\n\nclass TestApp(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        pass\n", encoding="utf-8")
        self.project_file = self.root / "ai-dev-playbook" / "project.json"

    def project(self):
        return json.loads(self.project_file.read_text(encoding="utf-8"))

    def write_policy(self, policy, name="checks-policy.json"):
        (self.root / name).write_text(
            json.dumps(policy, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

    def test_apply_writes_policy_and_link(self):
        """Apply записывает checks-policy.json и checks_policy в project.json"""
        before = self.project()
        self.write_policy(policy_fixture())
        code, out, err = self.cli("setup", "apply")
        self.assertEqual(code, 0, err)
        policy_path = self.root / "checks-policy.json"
        self.assertTrue(policy_path.is_file())
        written = json.loads(policy_path.read_text(encoding="utf-8"))
        checks_contract.validate_policy(written, playbook.require)
        after = self.project()
        self.assertEqual(after.pop("checks_policy"), "checks-policy.json")
        self.assertEqual(after, before, "apply изменил legacy-поля")
        # Связь читается штатным загрузчиком; индекс пересобирается.
        data, _ = playbook.config(self.root)
        self.assertEqual(playbook._load_policy(self.root, data)["policy_id"],
                         "fixture-checks")
        code, _, err = self.cli("index")
        self.assertEqual(code, 0, err)

    def test_apply_preserves_task_workflow_v2(self):
        """Apply сохраняет schema_version=2 и task_workflow"""
        data = self.project()
        data["schema_version"] = 2
        data["task_workflow"] = {"schema_version": 2, "registry": [{
            "id": "unit-tests", "kind": "command", "purpose": "Behavior",
            "argv": [sys.executable, "-c", "pass"],
            "timeout_seconds": 30,
            "default_inputs": ["src"],
            "applicability": {"kind": "always"}}]}
        self.project_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        self.write_policy(policy_fixture())
        code, _, err = self.cli("setup", "apply")
        self.assertEqual(code, 0, err)
        after = self.project()
        self.assertEqual(after["schema_version"], 2)
        self.assertEqual(after["task_workflow"], data["task_workflow"])
        self.assertEqual(after["checks_policy"], "checks-policy.json")

    def test_apply_rejects_unapproved_basis(self):
        """Apply отклоняет политику с TODO в approved_basis"""
        policy = policy_fixture()
        policy["approved_basis"] = {"who": "TODO", "at":
                                    "2026-01-01T00:00:00+00:00",
                                    "source": "TODO", "detail": "TODO"}
        draft = json.dumps(policy, ensure_ascii=False, indent=2) + "\n"
        self.write_policy(policy)
        code, _, err = self.cli("setup", "apply")
        self.assertEqual(code, 1)
        self.assertIn("TODO", err)
        self.assertEqual((self.root / "checks-policy.json")
                         .read_text(encoding="utf-8"), draft)
        self.assertNotIn("checks_policy", self.project())

    def test_apply_rejects_unsupported_schema(self):
        """Apply отклоняет schema_version вне {1,2}"""
        policy = policy_fixture(schema_version=2)
        self.write_policy(policy)
        code, _, err = self.cli("setup", "apply")
        self.assertEqual(code, 1)
        self.assertIn("schema_version", err)
        self.assertNotIn("checks_policy", self.project())

    def test_apply_rejects_missing_and_second_application(self):
        """Apply без политики и повторный apply отклоняются"""
        code, _, err = self.cli("setup", "apply")
        self.assertEqual(code, 1)
        self.assertIn("нет файла политики", err)
        self.write_policy(policy_fixture())
        self.assertEqual(self.cli("setup", "apply")[0], 0)
        code, _, err = self.cli("setup", "apply")
        self.assertEqual(code, 1)
        self.assertIn("уже подключена", err)

    def test_apply_uses_alternate_policy_source(self):
        """--policy читает альтернативный источник и пишет канонический файл"""
        self.write_policy(policy_fixture(), name="draft-policy.json")
        code, _, err = self.cli("setup", "apply", "--policy",
                                "draft-policy.json")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.root / "checks-policy.json").is_file())
        self.assertTrue((self.root / "draft-policy.json").is_file())
        self.assertEqual(self.project()["checks_policy"],
                         "checks-policy.json")

    def test_apply_interruption_restores_preimage(self):
        """Прерывание apply восстанавливает пред-образ через restore"""
        self.write_policy(policy_fixture())
        before_project = self.project_file.read_bytes()
        before_policy = (self.root / "checks-policy.json").read_bytes()
        calls = []
        saved = playbook._checks_policy_linked

        def flaky(staged):
            calls.append(staged)
            if len(calls) > 1:
                raise KeyboardInterrupt()
            return saved(staged)

        playbook._checks_policy_linked = flaky
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.cli("setup", "apply")
        finally:
            playbook._checks_policy_linked = saved
        pending = file_operations.pending_info(self.root)
        self.assertIsNotNone(pending)
        result = file_operations.restore_operation(
            self.root, playbook.safe_path, playbook.require)
        self.assertEqual(result["status"], "restored")
        self.assertEqual((self.root / "checks-policy.json").read_bytes(),
                         before_policy)
        self.assertEqual(self.project_file.read_bytes(), before_project)
        self.assertIsNone(file_operations.pending_info(self.root))

    def test_new_files_inherit_module_rules(self):
        """Новые файлы под путями модулей наследуют правила inventory"""
        self.write_policy(policy_fixture())
        code, _, err = self.cli("setup", "apply")
        self.assertEqual(code, 0, err)
        data, _ = playbook.config(self.root)
        policy = playbook._load_policy(self.root, data)
        (self.root / "src" / "new_mod.py").write_text("Y = 2\n",
                                                    encoding="utf-8")
        (self.root / "tests" / "test_new.py").write_text(TEST_B,
                                                        encoding="utf-8")
        (self.root / ".venv").mkdir()
        (self.root / ".venv" / "junk.py").write_text("Z = 3\n",
                                                   encoding="utf-8")
        inventory = checks_inventory.build_inventory(self.root, policy)
        self.assertEqual(inventory["entries"]["src/new_mod.py"]["modules"],
                         ["app"])
        self.assertEqual(
            inventory["entries"]["tests/test_new.py"]["modules"],
            ["tests"])
        self.assertTrue(any(item["path"] == ".venv"
                            for item in inventory["excluded"]))
        self.assertNotIn(".venv/junk.py", inventory["entries"])


if __name__ == "__main__":
    unittest.main()
