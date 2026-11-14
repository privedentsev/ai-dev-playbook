"""PB-058: inventory и снимки входов без Git, исключения, гонки."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "ai-dev-playbook" / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "checks_inventory"
CONTRACT_FIXTURES = ROOT / "tests" / "fixtures" / "checks_contract"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import checks_inventory as ci
import verification_policy


def req(condition, message):
    if not condition:
        raise ValueError(message)


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


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
        "subsystems": [{"id": "billing", "title": "Биллинг"}],
        "modules": [
            {"id": "payments", "title": "Платежи",
             "subsystem_id": "billing", "paths": ["src/payments/**"]},
            {"id": "checkout", "title": "Заказ",
             "subsystem_id": "billing", "paths": ["src/checkout/**"]},
        ],
        "checks": [],
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
    """Изолированное файловое дерево проекта."""

    def __init__(self, case):
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir()

    def write(self, rel, data=b"x"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def mkdir(self, rel):
        (self.root / rel).mkdir(parents=True, exist_ok=True)

    def remove(self, rel):
        (self.root / rel).unlink()

    def rename(self, old, new):
        (self.root / old).rename(self.root / new)

    def utime(self, rel, ns):
        os.utime(self.root / rel, ns=(ns, ns))


class InventoryTest(unittest.TestCase):
    """Inventory проекта: классификация модулей, unknown-пути,
    служебные исключения, симлинки и policy.excludes."""
    def setUp(self):
        self.proj = Project(self)
        self.policy = make_policy()
        self.proj.write("src/payments/pay.py", b"pay")
        self.proj.write("src/checkout/order.py", b"order")

    def inventory(self, **kw):
        return ci.build_inventory(self.proj.root, self.policy,
                                  now="2026-11-08T12:00:00+03:00", **kw)

    def test_module_classification(self):
        """Inventory классифицирует файлы по модулям и возвращает kind"""
        inv = self.inventory()
        self.assertEqual(inv["kind"], ci.INVENTORY_KIND)
        self.assertEqual(inv["entries"]["src/payments/pay.py"]["modules"],
                         ["payments"])
        self.assertEqual(inv["entries"]["src/checkout/order.py"]["modules"],
                         ["checkout"])
        self.assertEqual(inv["entries"]["src/payments"]["type"], "dir")
        ci.validate_inventory(inv, req)

    def test_unknown_paths(self):
        """Пути вне модулей попадают в unknown с unknown_scope"""
        self.proj.write("docs/readme.md", b"doc")
        inv = self.inventory()
        self.assertEqual(inv["unknown_paths"], ["docs/readme.md"])
        self.assertEqual(inv["entries"]["docs/readme.md"]["modules"], [])

    def test_no_git_needed(self):
        """Inventory работает без Git-репозитория"""
        self.proj.write(".git/config", b"secret")
        inv = self.inventory()
        self.assertNotIn(".git/config", inv["entries"])
        self.assertIn({"path": ".git", "reason": "служебный runtime-каталог",
                       "origin": "service"}, inv["excluded"])

    def test_service_dirs_skipped(self):
        """Служебные каталоги work/__pycache__ пропускаются при обходе"""
        self.proj.write("work/verification/report.json", b"r")
        self.proj.write("src/payments/__pycache__/pay.cpython-312.pyc", b"p")
        self.proj.write(".pytest_cache/v", b"v")
        self.proj.write("src/payments/.DS_Store", b"d")
        inv = self.inventory()
        for rel in ("work", "src/payments/__pycache__", ".pytest_cache",
                    "src/payments/.DS_Store"):
            self.assertNotIn(rel, inv["entries"], rel)
        excluded = {e["path"] for e in inv["excluded"]}
        self.assertIn("work", excluded)
        self.assertIn("src/payments/__pycache__", excluded)
        self.assertIn("src/payments/.DS_Store", excluded)

    def test_policy_excludes_have_reason(self):
        """Policy excludes с reason исключают пути из inventory"""
        self.policy["excludes"] = [
            {"pattern": "build/**", "reason": "артефакты сборки",
             "origin": "manual"}]
        self.proj.write("build/out.bin", b"b")
        inv = self.inventory()
        self.assertEqual(inv["excluded"],
                         [{"path": "build", "reason": "артефакты сборки",
                           "origin": "manual"}])
        self.assertNotIn("build/out.bin", inv["entries"])

    def test_symlink_rejected(self):
        """Симлинк внутри проекта отклоняется inventory"""
        self.proj.write("src/payments/real.py", b"r")
        (self.proj.root / "src/payments/link.py").symlink_to(
            self.proj.root / "src/payments/real.py")
        with self.assertRaisesRegex(ValueError, "ссылка"):
            self.inventory()

    def test_symlink_escape_rejected(self):
        """Симлинк за пределы проекта отклоняется"""
        outside = Path(self.proj.tmp.name) / "outside.txt"
        outside.write_bytes(b"out")
        (self.proj.root / "escape").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "ссылка"):
            self.inventory()

    def test_symlink_cycle_rejected(self):
        """Циклический симлинк отклоняется без зависания"""
        link = self.proj.root / "loop"
        link.symlink_to(Path("."), target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "ссылка"):
            self.inventory()

    def test_root_symlink_rejected(self):
        """Симлинк на корень проекта отклоняется"""
        alias = Path(self.proj.tmp.name) / "alias"
        alias.symlink_to(self.proj.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "ссылка"):
            ci.build_inventory(alias, self.policy)

    def test_special_file_rejected(self):
        """FIFO/специальный файл отклоняется inventory"""
        os.mkfifo(self.proj.root / "src/payments/pipe")
        with self.assertRaisesRegex(ValueError, "специальный"):
            self.inventory()

    def test_read_error_propagates(self):
        """Ошибка чтения каталога распространяется, а не глотается"""
        blocked = self.proj.root / "src/payments/closed"
        blocked.mkdir()
        blocked.chmod(0)
        try:
            with self.assertRaises(OSError):
                self.inventory()
        finally:
            blocked.chmod(0o755)

    def test_exclude_conflict_with_discovered_input(self):
        """Exclude, конфликтующий с найденным входом проверки, отклоняется"""
        self.policy["checks"] = [{
            "id": "payments-tests", "title": "t", "purpose": "p",
            "kind": "command", "argv": ["run"], "timeout_seconds": 5,
            "inputs": ["src/payments"]}]
        self.policy["excludes"] = [
            {"pattern": "src/**/fixtures.bin", "reason": "генерируемый",
             "origin": "manual"}]
        self.proj.write("src/payments/fixtures.bin", b"f")
        with self.assertRaisesRegex(ValueError, "конфликтует"):
            self.inventory()

    def test_exclude_outside_inputs_ok(self):
        """Exclude вне входов проверок допустим"""
        self.policy["checks"] = [{
            "id": "payments-tests", "title": "t", "purpose": "p",
            "kind": "command", "argv": ["run"], "timeout_seconds": 5,
            "inputs": ["src/payments"]}]
        self.policy["excludes"] = [
            {"pattern": "src/**/fixtures.bin", "reason": "генерируемый",
             "origin": "manual"}]
        self.proj.write("src/checkout/fixtures.bin", b"f")
        inv = self.inventory()
        self.assertIn({"path": "src/checkout/fixtures.bin",
                       "reason": "генерируемый", "origin": "manual"},
                      inv["excluded"])

    def test_diff_add_delete_rename(self):
        """diff_inventories видит добавление, удаление и переименование"""
        base = self.inventory()
        self.proj.write("src/payments/new.py", b"n")
        self.proj.remove("src/checkout/order.py")
        self.proj.rename("src/payments/pay.py", "src/payments/pay2.py")
        diff = ci.diff_inventories(base, self.inventory())
        self.assertTrue(diff["complete"])
        self.assertIn("src/payments/new.py", diff["added"])
        self.assertIn("src/payments/pay2.py", diff["added"])
        self.assertIn("src/checkout/order.py", diff["removed"])
        self.assertIn("src/payments/pay.py", diff["removed"])
        self.assertFalse(diff["fresh"])

    def test_diff_missing_baseline_conservative(self):
        """Отсутствие baseline делает diff неполным консервативно"""
        diff = ci.diff_inventories(None, self.inventory())
        self.assertFalse(diff["complete"])
        self.assertFalse(diff["fresh"])

    def test_diff_corrupt_baseline_conservative(self):
        """Повреждённый baseline делает diff неполным консервативно"""
        diff = ci.diff_inventories({"kind": "other"}, self.inventory())
        self.assertFalse(diff["complete"])
        self.assertIn("повреждён", diff["reason"])

    def test_diff_policy_change_blocks(self):
        """Смена policy revision блокирует diff inventory"""
        base = self.inventory()
        self.policy["revision"] = 2
        diff = ci.diff_inventories(base, self.inventory())
        self.assertFalse(diff["complete"])

    def test_validate_fixture(self):
        """Фикстура inventory.json проходит validate_inventory"""
        doc = load_fixture("inventory.json")
        self.assertIs(ci.validate_inventory(doc, req), doc)


class SnapshotTest(unittest.TestCase):
    """Снимки входов: файлы/каталоги, absent→present, mtime/size/bytes,
    обнаружение add/delete/rename и хеширование."""
    def setUp(self):
        self.proj = Project(self)
        self.policy = make_policy()
        self.proj.write("src/payments/pay.py", b"pay")
        self.proj.write("src/payments/util.py", b"util")
        self.proj.write("tests/test_payments.py", b"test")

    def snap(self, inputs, **kw):
        kw.setdefault("now", "2026-11-08T12:00:00+03:00")
        return ci.snapshot(self.proj.root, self.policy, inputs, **kw)

    def test_directory_and_file_inputs(self):
        """Снимок покрывает входы-файлы и входы-каталоги"""
        doc = self.snap(["src/payments", "tests/test_payments.py"])
        ci.validate_snapshot(doc, req)
        self.assertEqual(doc["kind"], ci.SNAPSHOT_KIND)
        entry = doc["entries"]["path:src/payments"]
        self.assertEqual(entry["type"], "directory")
        self.assertEqual(entry["members"]["pay.py"]["type"], "file")
        self.assertTrue(entry["members"]["pay.py"]["sha256"]
                        .startswith("sha256:"))
        self.assertEqual(
            doc["entries"]["path:tests/test_payments.py"]["type"], "file")

    def test_absent_input_recorded(self):
        """Отсутствующий вход записывается как type=absent"""
        doc = self.snap(["missing.py", "src/payments"])
        self.assertEqual(doc["entries"]["path:missing.py"],
                         {"type": "absent"})
        diff = ci.diff_snapshots(doc, self.snap(["missing.py",
                                                 "src/payments"]))
        self.assertTrue(diff["fresh"])

    def test_absent_then_present_is_change(self):
        """Появление ранее absent-входа определяется как изменение"""
        base = self.snap(["new.py"])
        self.proj.write("new.py", b"n")
        diff = ci.diff_snapshots(base, self.snap(["new.py"]))
        self.assertTrue(diff["complete"])
        self.assertIn("new.py", diff["changed"])

    def test_same_mtime_size_different_bytes(self):
        """Одинаковые mtime/size при разных байтах всё равно изменение"""
        path = self.proj.write("src/payments/pay.py", b"AAAA")
        self.proj.utime("src/payments/pay.py", 1_000_000_000)
        base = self.snap(["src/payments"])
        self.proj.write("src/payments/pay.py", b"BBBB")
        self.proj.utime("src/payments/pay.py", 1_000_000_000)
        self.assertEqual(path.stat().st_size, 4)
        diff = ci.diff_snapshots(base, self.snap(["src/payments"]))
        self.assertIn("src/payments/pay.py", diff["changed"])

    def test_add_delete_rename_detected(self):
        """Снимок обнаруживает add/delete/rename внутри каталога-входа"""
        base = self.snap(["src/payments"])
        self.proj.rename("src/payments/pay.py", "src/payments/pay2.py")
        self.proj.write("src/payments/extra.py", b"e")
        diff = ci.diff_snapshots(base, self.snap(["src/payments"]))
        self.assertIn("src/payments/pay.py", diff["removed"])
        self.assertIn("src/payments/pay2.py", diff["added"])
        self.assertIn("src/payments/extra.py", diff["added"])
        self.assertNotIn("src/payments/util.py", diff["changed"])

    def test_hash_once_per_file(self):
        """Каждый файл хешируется ровно один раз за снимок"""
        calls = []
        real = ci.project_view.digest_file

        def counting(path):
            calls.append(str(path))
            return real(path)

        self.snap(["src/payments", "src/payments/pay.py",
                   {"type": "file", "path": "src/payments/util.py"}],
                  hash_fn=counting)
        self.assertEqual(len(calls), len(set(calls)))
        self.assertEqual(len(calls), 2)

    def test_streaming_default_hasher(self):
        """Дефолтный потоковый hasher обрабатывает большие файлы"""
        big = self.proj.write("src/payments/big.bin", b"z" * (3 << 20))
        doc = self.snap(["src/payments"])
        member = doc["entries"]["path:src/payments"]["members"]["big.bin"]
        import hashlib
        self.assertEqual(member["sha256"],
                         "sha256:" + hashlib.sha256(
                             big.read_bytes()).hexdigest())

    def test_read_error_is_failure(self):
        """Ошибка чтения при хешировании — failure, не молчаливый пропуск"""
        def broken(path):
            raise OSError("disc unreadable")
        with self.assertRaises(OSError):
            self.snap(["src/payments/pay.py"], hash_fn=broken)

    def test_used_resources_not_lost(self):
        """PNG/ZIP-ресурсы входов не теряются при снимке"""
        self.proj.write("src/payments/logo.png", b"\x89PNG")
        self.proj.write("src/payments/pack.zip", b"PK\x03\x04")
        doc = self.snap(["src/payments"])
        members = doc["entries"]["path:src/payments"]["members"]
        self.assertTrue(members["logo.png"]["sha256"].startswith("sha256:"))
        self.assertTrue(members["pack.zip"]["sha256"].startswith("sha256:"))

    def test_type_mismatch_rejected(self):
        """Вход file на каталоге и directory на файле отклоняются"""
        with self.assertRaisesRegex(ValueError, "не является"):
            self.snap([{"type": "file", "path": "src/payments"}])
        with self.assertRaisesRegex(ValueError, "не является"):
            self.snap([{"type": "directory",
                        "path": "tests/test_payments.py"}])

    def test_contradictory_inputs_rejected(self):
        """Один путь как file и directory одновременно отклоняется"""
        with self.assertRaisesRegex(ValueError, "файлом, и каталогом"):
            self.snap([{"type": "file", "path": "src/payments"},
                       {"type": "directory", "path": "src/payments"}])

    def test_service_paths_not_read(self):
        """Служебные пути __pycache__ не читаются при снимке"""
        self.proj.write("src/payments/__pycache__/pay.pyc", b"p")
        doc = self.snap(["src/payments"])
        self.assertNotIn("__pycache__/pay.pyc",
                         doc["entries"]["path:src/payments"]["members"])

    def test_service_area_input_rejected(self):
        """Вход в служебную область отклоняется"""
        with self.assertRaisesRegex(ValueError, "служебной"):
            self.snap(["work/report.json"])
        with self.assertRaisesRegex(ValueError, "служебной"):
            self.snap(["src/payments/__pycache__"])

    def test_excluded_input_rejected(self):
        """Вход под policy exclude отклоняется"""
        self.policy["excludes"] = [
            {"pattern": "tmp/**", "reason": "временное", "origin": "manual"}]
        self.proj.write("tmp/a.txt", b"a")
        with self.assertRaisesRegex(ValueError, "исключён"):
            self.snap(["tmp"])

    def test_exclude_conflict_inside_input(self):
        """Exclude внутри входа-каталога отклоняется"""
        self.policy["excludes"] = [
            {"pattern": "src/**/fixtures.bin", "reason": "генерируемый",
             "origin": "manual"}]
        self.proj.write("src/payments/fixtures.bin", b"f")
        with self.assertRaisesRegex(ValueError, "конфликтует"):
            self.snap(["src/payments"])

    def test_symlink_input_rejected(self):
        """Симлинк как прямой вход отклоняется"""
        self.proj.write("real.txt", b"r")
        (self.proj.root / "link.txt").symlink_to(self.proj.root / "real.txt")
        with self.assertRaisesRegex(ValueError, "ссылка"):
            self.snap(["link.txt"])
        (self.proj.root / "src/linkdir").symlink_to(
            self.proj.root / "src/payments", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "ссылка"):
            self.snap(["src/linkdir/pay.py"])

    def test_symlink_inside_dir_input_rejected(self):
        """Симлинк внутри входа-каталога отклоняется"""
        self.proj.write("outside.txt", b"o")
        (self.proj.root / "src/payments/out.py").symlink_to(
            self.proj.root / "outside.txt")
        with self.assertRaisesRegex(ValueError, "ссылка"):
            self.snap(["src/payments"])

    def test_environment_input(self):
        """environment-вход записывает значения указанных переменных"""
        doc = self.snap([{"type": "environment", "names": ["CI", "MODE"]}],
                        environment={"CI": True, "MODE": "x"})
        self.assertIn("environment:CI", doc["entries"])
        self.assertIn("environment:MODE", doc["entries"])
        with self.assertRaisesRegex(ValueError, "неизвестный факт"):
            self.snap([{"type": "environment", "names": ["MISSING"]}],
                      environment={})

    def test_projection_inputs(self):
        """projection-вход фиксирует проекцию карточки и meta"""
        card = "# T\n\n## Задача\n\n### Цель\n\nтекст\n\n## Журнал\n\nx\n"
        meta = {"materials": [{"id": "m"}], "workflow": {"history": [1]}}
        proj_value = verification_policy.task_projection(card, meta)
        doc = self.snap([{"type": "task_projection"}],
                        projections={"task_projection": proj_value})
        self.assertEqual(doc["entries"]["task_projection"]["type"],
                         "projection")
        with self.assertRaisesRegex(ValueError, "проекция"):
            self.snap([{"type": "task_projection"}])

    def test_task_projection_ignores_service_fields(self):
        """task_projection игнорирует служебные поля карточки"""
        card = "# T\n\n## Задача\n\n### Цель\n\nодно\n\n## Журнал\n\n### EXEC-01\nx\n"
        base_meta = {"materials": [], "workflow": {"history": []}}
        same_meta = {"materials": [{"id": "new"}], "handoff": {"at": "1"},
                     "workflow": {"history": [1, 2]}}
        changed_card = card.replace("одно", "другое")
        inputs = [{"type": "task_projection"}]
        doc_a = self.snap(inputs, projections={
            "task_projection": verification_policy.task_projection(
                card, base_meta)})
        doc_b = self.snap(inputs, projections={
            "task_projection": verification_policy.task_projection(
                card, same_meta)})
        doc_c = self.snap(inputs, projections={
            "task_projection": verification_policy.task_projection(
                changed_card, base_meta)})
        self.assertTrue(ci.diff_snapshots(doc_a, doc_b)["fresh"])
        self.assertIn("task_projection",
                      ci.diff_snapshots(doc_a, doc_c)["changed"])

    def test_race_during_hash_detected(self):
        """Изменение файла во время хеширования обнаруживается как race"""
        real = ci.project_view.digest_file
        victim = self.proj.root / "src/payments/util.py"

        def racy(path):
            result = real(path)
            victim.write_bytes(b"mutated mid-snapshot")
            return result

        with self.assertRaisesRegex(ValueError, "изменился во время"):
            self.snap(["src/payments"], hash_fn=racy)

    def test_race_new_member_detected(self):
        """Новый файл, появившийся во время снимка, обнаруживается"""
        real = ci.project_view.digest_file
        new_file = self.proj.root / "src/payments/late.py"

        def racy(path):
            result = real(path)
            new_file.write_bytes(b"late")
            return result

        with self.assertRaisesRegex(ValueError, "изменился во время"):
            self.snap(["src/payments"], hash_fn=racy)

    def test_absent_input_appearing_is_race(self):
        # A-02: появление absent-входа во время снимка — изменение дерева.
        """Появление absent-входа во время снимка — race"""
        real = ci.project_view.digest_file
        late = self.proj.root / "late.py"

        def racy(path):
            result = real(path)
            late.write_bytes(b"late")
            return result

        with self.assertRaisesRegex(ValueError, "изменился во время"):
            self.snap(["late.py", "src/payments"], hash_fn=racy)

    def test_absent_ancestor_appearing_is_race(self):
        # A-02: появление отсутствовавшего предка — тоже гонка.
        """Появление предка absent-входа во время снимка — race"""
        real = ci.project_view.digest_file
        ancestor = self.proj.root / "missing"

        def racy(path):
            result = real(path)
            ancestor.mkdir(exist_ok=True)
            return result

        with self.assertRaisesRegex(ValueError, "изменился во время"):
            self.snap(["missing/x.py", "src/payments"], hash_fn=racy)

    def test_shared_prefix_state_not_overwritten(self):
        # A-02: поздний resolve общего предка не стирает его absent-запись.
        """Общие префиксы состояний входов не перезаписываются"""
        real = ci.project_view.digest_file
        missing = self.proj.root / "missing"

        def racy(path):
            result = real(path)
            missing.mkdir(exist_ok=True)
            return result

        inputs = ["missing/a.py",
                  {"type": "file", "path": "a-racy/z.py"},
                  {"type": "file", "path": "missing/b.py"}]
        self.proj.write("a-racy/z.py", b"z")
        with self.assertRaisesRegex(ValueError, "изменился во время"):
            self.snap(inputs, hash_fn=racy)

    def test_excluded_subtree_race_detected(self):
        # A-05: совпадающий файл в проверенном исключённом поддереве —
        # изменение дерева, а не успешный снимок.
        """Гонка в исключённом поддереве внутри входа обнаруживается"""
        self.policy["modules"][0]["paths"] = ["src/payments/**/*.py"]
        self.policy["excludes"] = [
            {"pattern": "src/**/gen", "reason": "генерируемый код",
             "origin": "manual"}]
        self.proj.mkdir("src/payments/gen")
        real = ci.project_view.digest_file
        new_file = self.proj.root / "src/payments/gen/out.py"

        def racy(path):
            result = real(path)
            new_file.write_bytes(b"g")
            return result

        check = {"id": "c1", "kind": "test", "modules": ["payments"]}
        with self.assertRaisesRegex(ValueError, "изменился во время"):
            ci.snapshot(self.proj.root, self.policy,
                        ci.resolved_inputs(self.policy, check),
                        hash_fn=racy,
                        now="2026-11-08T12:00:00+03:00")

    def test_overlapping_inputs_keep_first_hash_state(self):
        # A-06: path-вход после каталога не перезаписывает метаданные
        # первого хеширования; мутация файла отклоняется.
        """Пересекающиеся входы сохраняют первое hash-состояние"""
        real = ci.project_view.digest_file
        victim = self.proj.root / "src/payments/pay.py"
        done = []

        def racy(path):
            result = real(path)
            if path == victim and not done:
                done.append(True)
                victim.write_bytes(b"mutated longer bytes")
            return result

        with self.assertRaisesRegex(ValueError, "изменился во время"):
            self.snap(["src/payments", "src/payments/pay.py"],
                      hash_fn=racy)

    def test_corrupt_snapshot_not_reusable(self):
        """Повреждённый снимок не переиспользуется как валидный"""
        doc = self.snap(["src/payments"])
        doc2 = self.snap(["src/payments"])
        self.assertTrue(ci.diff_snapshots(doc, doc2)["fresh"])
        bad = dict(doc, kind="report")
        self.assertFalse(ci.diff_snapshots(bad, doc2)["complete"])
        self.assertFalse(ci.diff_snapshots(None, doc2)["complete"])

    def test_policy_change_invalidates(self):
        """Смена policy revision инвалидирует снимок"""
        doc = self.snap(["src/payments"])
        self.policy["revision"] = 2
        doc2 = self.snap(["src/payments"])
        diff = ci.diff_snapshots(doc, doc2)
        self.assertFalse(diff["complete"])
        self.assertFalse(ci.snapshot_compatible(doc, self.policy)
                         ["compatible"])

    def test_snapshot_compatible_ok(self):
        """Валидный снимок совместим с текущей policy"""
        doc = self.snap(["src/payments"])
        self.assertTrue(ci.snapshot_compatible(doc, self.policy)
                        ["compatible"])
        self.assertFalse(ci.snapshot_compatible(
            {"kind": "report"}, self.policy)["compatible"])

    def test_snapshot_is_not_lifecycle_snapshot(self):
        """Lifecycle-формат snapshot отклоняется validate_snapshot"""
        lifecycle_like = {"files": {"path:a": "sha256:" + "0" * 64}}
        with self.assertRaises(ValueError):
            ci.validate_snapshot(lifecycle_like, req)
        report_like = {"schema_version": 1, "kind": "report",
                       "project_id": "x",
                       "policy_digest": "sha256:" + "0" * 64,
                       "created_at": "2026-11-08T12:00:00+03:00",
                       "entries": {}}
        with self.assertRaisesRegex(ValueError, "kind"):
            ci.validate_snapshot(report_like, req)

    def test_load_snapshot_strict(self):
        """load_snapshot строго валидирует файл снимка"""
        doc = self.snap(["src/payments"])
        target = Path(self.proj.tmp.name) / "snap.json"
        target.write_text(json.dumps(doc), encoding="utf-8")
        self.assertEqual(ci.load_snapshot(target), doc)
        target.write_text("{ not json")
        with self.assertRaises(ValueError):
            ci.load_snapshot(target)
        target.write_text(json.dumps({"files": {}}))
        with self.assertRaisesRegex(ValueError, "kind|schema_version|поля"):
            ci.load_snapshot(target)
        with self.assertRaises(FileNotFoundError):
            ci.load_snapshot(Path(self.proj.tmp.name) / "none.json")

    def test_snapshot_digest_stable(self):
        """snapshot_digest стабилен и имеет префикс sha256:"""
        doc = self.snap(["src/payments"])
        self.assertTrue(ci.snapshot_digest(doc).startswith("sha256:"))
        doc2 = self.snap(["src/payments"])
        self.assertEqual(ci.snapshot_digest(doc), ci.snapshot_digest(doc2))
        self.proj.write("src/payments/pay.py", b"changed")
        self.assertNotEqual(ci.snapshot_digest(doc),
                            ci.snapshot_digest(self.snap(["src/payments"])))

    def test_validate_fixture(self):
        """Фикстура snapshot.json проходит validate_snapshot"""
        doc = load_fixture("snapshot.json")
        self.assertIs(ci.validate_snapshot(doc, req), doc)


class ResolvedInputsTest(unittest.TestCase):
    """resolved_inputs: module paths → inputs, фильтры хеширования,
    declared dir inputs и excludes."""
    def setUp(self):
        self.proj = Project(self)
        self.policy = make_policy()
        self.proj.write("src/payments/pay.py", b"p")
        self.proj.write("src/payments/notes.md", b"n")
        self.proj.write("tests/test_payments.py", b"t")

    def test_module_paths_become_inputs(self):
        """Пути модулей разворачиваются в inputs проверки"""
        check = {"id": "c1", "kind": "test", "modules": ["payments"],
                 "inputs": ["tests/test_payments.py"]}
        resolved = ci.resolved_inputs(self.policy, check)
        self.assertIn("tests/test_payments.py", resolved)
        module_inputs = [i for i in resolved if isinstance(i, dict)]
        self.assertEqual(module_inputs,
                         [{"type": "directory", "path": "src/payments",
                           "patterns": ["src/payments/**"]}])

    def test_module_filter_hashes_only_matching(self):
        """Модульный фильтр хеширует только совпадающие файлы"""
        self.policy["modules"][0]["paths"] = ["src/payments/*.py"]
        check = {"id": "c1", "kind": "test", "modules": ["payments"]}
        doc = ci.snapshot(self.proj.root, self.policy,
                          ci.resolved_inputs(self.policy, check),
                          now="2026-11-08T12:00:00+03:00")
        members = doc["entries"]["path:src/payments"]["members"]
        self.assertIn("sha256", members["pay.py"])
        self.assertNotIn("sha256", members["notes.md"])
        self.proj.write("src/payments/new.py", b"x")
        diff = ci.diff_snapshots(doc, ci.snapshot(
            self.proj.root, self.policy,
            ci.resolved_inputs(self.policy, check),
            now="2026-11-08T12:00:00+03:00"))
        self.assertIn("src/payments/new.py", diff["added"])

    def test_resolved_validation(self):
        """Неканонический путь в resolved inputs отклоняется"""
        check = {"id": "c1", "kind": "test", "modules": ["payments"],
                 "inputs": [{"type": "file", "path": "a/../b"}]}
        with self.assertRaisesRegex(ValueError, "неканонический"):
            ci.resolved_inputs(self.policy, check)

    def test_literal_module_path_is_input(self):
        """Литеральный путь модуля становится входом"""
        self.policy["modules"][0]["paths"] = ["src/payments/pay.py"]
        check = {"id": "c1", "kind": "test", "modules": ["payments"]}
        resolved = ci.resolved_inputs(self.policy, check)
        self.assertEqual(resolved, [{"type": "path",
                                     "path": "src/payments/pay.py"}])
        doc = ci.snapshot(self.proj.root, self.policy, resolved,
                          now="2026-11-08T12:00:00+03:00")
        ci.validate_snapshot(doc, req)
        entry = doc["entries"]["path:src/payments/pay.py"]
        self.assertEqual(entry["type"], "file")
        self.assertTrue(entry["sha256"].startswith("sha256:"))
        self.proj.write("src/payments/pay.py", b"changed")
        diff = ci.diff_snapshots(doc, ci.snapshot(
            self.proj.root, self.policy, resolved,
            now="2026-11-08T12:00:00+03:00"))
        self.assertIn("src/payments/pay.py", diff["changed"])

    def test_declared_dir_input_not_narrowed_by_module(self):
        # A-01: явный вход-каталог покрывает всё поддерево; фильтр
        # связанного модуля не сужает его.
        """Объявленный dir-вход не сужается модульным фильтром"""
        self.policy["modules"][0]["paths"] = ["src/payments/*.py"]
        check = {"id": "c1", "kind": "test", "modules": ["payments"],
                 "inputs": ["src/payments"]}
        resolved = ci.resolved_inputs(self.policy, check)
        doc = ci.snapshot(self.proj.root, self.policy, resolved,
                          now="2026-11-08T12:00:00+03:00")
        members = doc["entries"]["path:src/payments"]["members"]
        self.assertIn("sha256", members["notes.md"])
        self.proj.write("src/payments/notes.md", b"changed")
        diff = ci.diff_snapshots(doc, ci.snapshot(
            self.proj.root, self.policy, resolved,
            now="2026-11-08T12:00:00+03:00"))
        self.assertIn("src/payments/notes.md", diff["changed"])

    def test_exclude_outside_module_filter_allowed(self):
        # A-04: исключённый файл вне выбранного glob пропускается.
        """Exclude вне модульного фильтра допустим"""
        self.policy["modules"][0]["paths"] = ["src/payments/*.py"]
        self.policy["excludes"] = [
            {"pattern": "src/**/fixtures.bin", "reason": "генерируемый",
             "origin": "manual"}]
        self.proj.write("src/payments/fixtures.bin", b"f")
        check = {"id": "c1", "kind": "test", "modules": ["payments"]}
        resolved = ci.resolved_inputs(self.policy, check)
        doc = ci.snapshot(self.proj.root, self.policy, resolved,
                          now="2026-11-08T12:00:00+03:00")
        members = doc["entries"]["path:src/payments"]["members"]
        self.assertNotIn("fixtures.bin", members)
        self.assertIn("sha256", members["pay.py"])

    def test_exclude_inside_module_filter_conflicts(self):
        # A-04: файл, совпадающий и с фильтром, и с исключением — ошибка.
        """Exclude внутри модульного фильтра конфликтует"""
        self.policy["modules"][0]["paths"] = ["src/payments/*.py"]
        self.policy["excludes"] = [
            {"pattern": "src/**/skip.py", "reason": "точечный",
             "origin": "manual"}]
        self.proj.write("src/payments/skip.py", b"s")
        check = {"id": "c1", "kind": "test", "modules": ["payments"]}
        with self.assertRaisesRegex(ValueError, "конфликтует"):
            ci.snapshot(self.proj.root, self.policy,
                        ci.resolved_inputs(self.policy, check),
                        now="2026-11-08T12:00:00+03:00")

    def test_excluded_subtree_holding_input_conflicts(self):
        # A-04: исключённый каталог с совпадающими потомками — ошибка.
        """Исключённое поддерево с входом конфликтует"""
        self.policy["modules"][0]["paths"] = ["src/payments/**"]
        self.policy["excludes"] = [
            {"pattern": "src/**/gen", "reason": "генерируемый код",
             "origin": "manual"}]
        self.proj.write("src/payments/gen/out.py", b"g")
        check = {"id": "c1", "kind": "test", "modules": ["payments"]}
        with self.assertRaisesRegex(ValueError, "конфликтует"):
            ci.snapshot(self.proj.root, self.policy,
                        ci.resolved_inputs(self.policy, check),
                        now="2026-11-08T12:00:00+03:00")

    def test_end_to_end_policy_inventory_snapshot(self):
        # A-04: policy → inventory → resolved_inputs → snapshot.
        """Сквозной путь policy→inventory→snapshot с excludes"""
        policy = make_policy(
            excludes=[{"pattern": "src/**/fixtures.bin",
                       "reason": "генерируемый", "origin": "manual"}])
        policy["modules"][0]["paths"] = ["src/payments/*.py"]
        policy["checks"] = [{
            "id": "payments-tests", "title": "t", "purpose": "p",
            "kind": "test", "modules": ["payments"],
            "adapter": "python-unittest", "argv": ["python3", "-m",
             "unittest", "tests.test_payments"], "timeout_seconds": 5,
            "inputs": ["tests/test_payments.py"]}]
        import checks_contract
        checks_contract.validate_policy(policy, req)
        self.proj.write("src/checkout/fixtures.bin", b"f")
        inv = ci.build_inventory(self.proj.root, policy,
                                 now="2026-11-08T12:00:00+03:00")
        ci.validate_inventory(inv, req)
        self.assertIn("src/payments/pay.py", inv["entries"])
        check = policy["checks"][0]
        resolved = ci.resolved_inputs(policy, check)
        doc = ci.snapshot(self.proj.root, policy, resolved,
                          now="2026-11-08T12:00:00+03:00")
        ci.validate_snapshot(doc, req)
        members = doc["entries"]["path:src/payments"]["members"]
        self.assertIn("sha256", members["pay.py"])
        self.assertNotIn("sha256", members["notes.md"])

    def test_unanchored_module_glob_scans_root(self):
        """Незаякоренный модульный глоб **/*.py сканирует корень"""
        self.policy["modules"][0]["paths"] = ["**/*.py"]
        check = {"id": "c1", "kind": "test", "modules": ["payments"]}
        resolved = ci.resolved_inputs(self.policy, check)
        self.assertEqual(resolved, [{"type": "directory", "path": ".",
                                     "patterns": ["**/*.py"]}])
        self.proj.write("top.py", b"top")
        doc = ci.snapshot(self.proj.root, self.policy, resolved,
                          now="2026-11-08T12:00:00+03:00")
        ci.validate_snapshot(doc, req)
        members = doc["entries"]["path:."]["members"]
        self.assertIn("sha256", members["src/payments/pay.py"])
        self.assertIn("sha256", members["top.py"])
        self.assertNotIn("sha256", members["src/payments/notes.md"])


if __name__ == "__main__":
    unittest.main()
