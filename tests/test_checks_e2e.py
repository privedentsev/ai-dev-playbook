"""PB-064: сквозные сценарии AC-01–11 выбора проверок без Git с CLI и счётчиками."""
import contextlib
import hashlib
import io
import json
import os
import platform
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "ai-dev-playbook" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import checks_contract
import checks_gate
import checks_trust
import file_operations
import playbook
import verification_policy
from test_checks_select import make_policy


COUNTER_HELPER = """import os
from pathlib import Path

def bump(name):
    folder = os.environ.get("PB064_COUNTERS")
    if not folder:
        return
    path = Path(folder) / f"{name}.count"
    current = int(path.read_text()) if path.is_file() else 0
    path.write_text(str(current + 1))
"""


def _stub_catalog():
    return {
        "adapter": "python-unittest",
        "fingerprint": "sha256:" + "0" * 64,
        "entries": [{
            "id": "tests.test_placeholder.PlaceholderTest.test_ok",
            "description": "Служебная запись каталога для политик command/manual.",
        }],
    }


def _basis(detail="Одобрено инженером для сквозного теста PB-064"):
    return {
        "who": "Инженер фикстуры",
        "at": "2026-11-14T12:00:00+03:00",
        "source": "PB-064 E2E fixture",
        "detail": detail,
    }


class NoGitChecksE2ECase(unittest.TestCase):
    """Базовый временный проект без .git с реальным CLI и счётчиками запусков."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="pb064-e2e-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = self.base / "project"
        self.counters_dir = self.base / "counters"
        self.counters_dir.mkdir(parents=True, exist_ok=True)
        prev = os.environ.get("PB064_COUNTERS")
        os.environ["PB064_COUNTERS"] = str(self.counters_dir)
        self.addCleanup(
            lambda: os.environ.pop("PB064_COUNTERS", None)
            if prev is None else os.environ.__setitem__("PB064_COUNTERS", prev)
        )
        code, _, err = self.raw_cli("init", str(self.root), "--apply")
        self.assertEqual(code, 0, err)
        self.assertFalse((self.root / ".git").exists())
        proj_path = self.root / "ai-dev-playbook" / "project.json"
        proj = json.loads(proj_path.read_text(encoding="utf-8"))
        proj["name"] = "no-git-shop"
        proj["setup_status"] = "configured"
        proj["commands"]["test"] = "python3 -m unittest discover -s tests -v"
        proj_path.write_text(
            json.dumps(proj, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        for name in ("index.md", "product.md", "architecture.md", "code-map.md", "roadmap.md"):
            doc = self.root / "docs" / name
            doc.write_text(doc.read_text(encoding="utf-8").replace("TODO:", "Заполнено:"),
                           encoding="utf-8")

    def raw_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = playbook.main(list(args))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def cli(self, *args):
        return self.raw_cli("--root", str(self.root), *args)

    def write(self, rel, content):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def count(self, name):
        path = self.counters_dir / f"{name}.count"
        return int(path.read_text()) if path.is_file() else 0

    def counts(self, *names):
        return {name: self.count(name) for name in names}

    def create_card(self, task_id, title="Сквозная задача", status="in_progress",
                    ac_text="Проверяемое поведение подтверждено."):
        code, _, err = self.cli("card", "create", task_id, "--title", title, "--apply")
        self.assertEqual(code, 0, err)
        card = self.root / "docs" / "tasks" / f"{task_id}.md"
        text = card.read_text(encoding="utf-8")
        text = text.replace("Status: draft", f"Status: {status}", 1)
        text = text.replace("Executor: -", "Executor: Devin", 1)
        text = text.replace("Model: -", "Model: Gemini 4 Carbon Medium", 1)
        text = text.replace("TODO: результат и границы задачи.",
                            "Проверка сценария без Git.")
        text = text.replace("TODO: ссылки на требования, решения и исходные данные.",
                            "Временный проект PB-064.")
        text = text.replace("TODO: наблюдаемый результат и способ проверки.",
                            ac_text)
        text = text.replace("TODO: реализация и постоянные тесты; EXEC — изменения, причины решений и файлы.",
                            "Выполнить проверки и сверить счётчики.")
        text = text.replace("TODO: компетенции, среда, policy_revision и бюджет при запуске.",
                            "Python stdlib и временный каталог.")
        card.write_text(text, encoding="utf-8")
        self.assertEqual(self.cli("index")[0], 0)
        return card

    def command_argv(self, counter_name, extra_code=""):
        script = (
            "import os; from pathlib import Path; "
            f"d=Path(os.environ['PB064_COUNTERS']); p=d/'{counter_name}.count'; "
            "p.write_text(str((int(p.read_text()) if p.is_file() else 0)+1))"
        )
        if extra_code:
            script += "; " + extra_code
        return [sys.executable, "-c", script]


class ShopGraphE2ETest(NoGitChecksE2ECase):
    """AC-01 и AC-02 спецификации: граф cart/payments/discounts без Git и защита от ложного reuse."""

    def _build_shop(self):
        self.write("src/discounts/__init__.py", "")
        self.write(
            "src/discounts/calc.py",
            "RATE = 10\ndef apply_discount(total):\n    return total - RATE\n",
        )
        self.write("src/cart/__init__.py", "")
        self.write(
            "src/cart/basket.py",
            "from src.discounts.calc import apply_discount\n"
            "def cart_total(items):\n    return apply_discount(sum(items))\n",
        )
        self.write("src/payments/__init__.py", "")
        self.write(
            "src/payments/charge.py",
            "from src.discounts.calc import apply_discount\n"
            "FEE = 2\ndef charge_amount(raw):\n    return apply_discount(raw) + FEE\n",
        )
        self.write("src/checkout/__init__.py", "")
        self.write(
            "src/checkout/order.py",
            "from src.cart.basket import cart_total\n"
            "from src.payments.charge import charge_amount\n"
            "def finalize(items):\n    return charge_amount(cart_total(items) + 10)\n",
        )
        self.write("tests/counter_helper.py", COUNTER_HELPER)
        self.write("tests/cart/__init__.py", "")
        self.write(
            "tests/cart/test_cart.py",
            'import unittest\nfrom counter_helper import bump\n'
            'from src.cart.basket import cart_total\n\n'
            'class CartTest(unittest.TestCase):\n'
            '    def test_cart_total(self):\n'
            '        """Сумма корзины учитывает скидку модуля discounts."""\n'
            '        bump("cart")\n'
            '        self.assertIsInstance(cart_total([20, 30]), int)\n',
        )
        self.write("tests/payments/__init__.py", "")
        self.write(
            "tests/payments/test_payments.py",
            'import unittest\nfrom counter_helper import bump\n'
            'from src.payments.charge import charge_amount\n\n'
            'class PaymentsTest(unittest.TestCase):\n'
            '    def test_charge_amount(self):\n'
            '        """Расчёт оплаты учитывает скидку и комиссию."""\n'
            '        bump("payments")\n'
            '        self.assertIsInstance(charge_amount(50), int)\n',
        )
        self.write("tests/checkout/__init__.py", "")
        self.write(
            "tests/checkout/test_checkout.py",
            'import unittest\nfrom counter_helper import bump\n'
            'from src.checkout.order import finalize\n\n'
            'class CheckoutTest(unittest.TestCase):\n'
            '    def test_finalize(self):\n'
            '        """Оформление заказа объединяет корзину и оплату."""\n'
            '        bump("checkout")\n'
            '        self.assertIsInstance(finalize([20, 30]), int)\n',
        )
        self.create_card("DEV-01", title="Корзина и оплата")
        code, out, err = self.cli("setup", "preview")
        self.assertEqual(code, 0, err)
        self.assertFalse((self.root / "checks-policy.json").exists())
        self.assertIn("Это ПРЕДЛОЖЕНИЕ", out)

        policy = {
            "schema_version": 1,
            "policy_id": "shop-policy",
            "revision": 1,
            "project_id": "no-git-shop",
            "trust_mode": "local",
            "approved_basis": _basis(),
            "subsystems": [{"id": "commerce", "title": "Торговое ядро"}],
            "modules": [
                {"id": "discounts", "title": "Скидки", "subsystem_id": "commerce",
                 "paths": ["src/discounts/**"]},
                {"id": "cart", "title": "Корзина", "subsystem_id": "commerce",
                 "paths": ["src/cart/**", "tests/cart/**"]},
                {"id": "payments", "title": "Оплата", "subsystem_id": "commerce",
                 "paths": ["src/payments/**", "tests/payments/**"]},
                {"id": "checkout", "title": "Оформление", "subsystem_id": "commerce",
                 "paths": ["src/checkout/**", "tests/checkout/**"]},
                {"id": "test-support", "title": "Помощники тестов",
                 "subsystem_id": "commerce", "paths": ["tests/counter_helper.py"]},
                {"id": "project-meta", "title": "Документы и настройки",
                 "subsystem_id": "commerce",
                 "paths": ["ai-dev-playbook/**", "docs/**", "AGENTS.md", "CHANGELOG.md"]},
            ],
            "checks": [
                {
                    "id": "cart-tests",
                    "title": "Тесты корзины",
                    "purpose": "Проверка расчёта корзины",
                    "kind": "test",
                    "modules": ["cart"],
                    "adapter": "python-unittest",
                    "argv": [sys.executable, "-m", "unittest", "discover",
                             "-s", "tests/cart", "-t", "."],
                    "timeout_seconds": 30,
                },
                {
                    "id": "payments-tests",
                    "title": "Тесты оплаты",
                    "purpose": "Проверка расчёта оплаты",
                    "kind": "test",
                    "modules": ["payments"],
                    "adapter": "python-unittest",
                    "argv": [sys.executable, "-m", "unittest", "discover",
                             "-s", "tests/payments", "-t", "."],
                    "timeout_seconds": 30,
                },
                {
                    "id": "checkout-tests",
                    "title": "Тесты оформления",
                    "purpose": "Проверка сквозного оформления заказа",
                    "kind": "test",
                    "modules": ["checkout"],
                    "adapter": "python-unittest",
                    "argv": [sys.executable, "-m", "unittest", "discover",
                             "-s", "tests/checkout", "-t", "."],
                    "timeout_seconds": 30,
                },
            ],
            "dependencies": [],
            "excludes": [
                {"pattern": "logs/**", "reason": "Локальные журналы стенда",
                 "origin": "manual"},
            ],
            "environment": {
                "toolchain": [{"id": "python", "version": platform.python_version()}],
                "external": [],
            },
            "fallback": {
                "expansion_order": ["module", "subsystem", "project"],
                "approved_commands": [],
                "limits": {"max_seconds": 300, "max_bytes": 5_000_000},
                "unknown_scope": "project",
            },
            "test_catalog": {
                "adapter": "python-unittest",
                "fingerprint": "sha256:" + "a" * 64,
                "entries": [
                    {
                        "id": "tests.cart.test_cart.CartTest.test_cart_total",
                        "group": "cart-tests",
                        "description": "Сумма корзины учитывает скидку модуля discounts.",
                        "skip": {"allowed": False},
                        "expected_failure": {"allowed": False},
                    },
                    {
                        "id": "tests.payments.test_payments.PaymentsTest.test_charge_amount",
                        "group": "payments-tests",
                        "description": "Расчёт оплаты учитывает скидку и комиссию.",
                        "skip": {"allowed": False},
                        "expected_failure": {"allowed": False},
                    },
                    {
                        "id": "tests.checkout.test_checkout.CheckoutTest.test_finalize",
                        "group": "checkout-tests",
                        "description": "Оформление заказа объединяет корзину и оплату.",
                        "skip": {"allowed": False},
                        "expected_failure": {"allowed": False},
                    },
                ],
            },
        }
        # ПодтягиваемPYTHONPATH tests/ для counter_helper через sys.path в тестах:
        for rel in ("tests/cart/test_cart.py", "tests/payments/test_payments.py",
                    "tests/checkout/test_checkout.py"):
            p = self.root / rel
            p.write_text(
                "import sys; from pathlib import Path; "
                "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
                + p.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        self.write("checks-policy.json", json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
        code, _, err = self.cli("setup", "apply")
        self.assertEqual(code, 0, err)
        return policy

    def test_ac01_no_git_manual_edits_payments_and_discounts_counters(self):
        """AC-01: без .git правка оплаты запускает оплату и checkout при reuse корзины, правка discounts запускает все три."""
        self._build_shop()
        self.assertFalse((self.root / ".git").exists())

        code, _, err = self.cli("checks", "run", "DEV-01", "--scope", "task", "--mode", "stale")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.counts("cart", "payments", "checkout"),
                         {"cart": 1, "payments": 1, "checkout": 1})

        # Ручная правка оплаты запускает payments и транзитивный checkout; cart остаётся reused.
        self.write(
            "src/payments/charge.py",
            "from src.discounts.calc import apply_discount\n"
            "FEE = 5\ndef charge_amount(raw):\n    return apply_discount(raw) + FEE\n",
        )
        code, plan_out, err = self.cli("checks", "plan", "DEV-01")
        self.assertEqual(code, 0, err)
        self.assertIn("cart-tests: not_applicable", plan_out)
        self.assertIn("payments-tests: run", plan_out)
        self.assertIn("checkout-tests: run", plan_out)

        code, _, err = self.cli("checks", "run", "DEV-01", "--scope", "task", "--mode", "stale")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.counts("cart", "payments", "checkout"),
                         {"cart": 1, "payments": 2, "checkout": 2})

        # В области project корзина явно отмечается reused без запуска подпроцесса.
        code, status_out, err = self.cli("checks", "status", "DEV-01")
        self.assertEqual(code, 0, err)
        self.assertIn("payments-tests", status_out)

        # Правка общих скидок затрагивает cart, payments и checkout одновременно.
        self.write(
            "src/discounts/calc.py",
            "RATE = 15\ndef apply_discount(total):\n    return total - RATE\n",
        )
        code, _, err = self.cli("checks", "run", "DEV-01", "--scope", "task", "--mode", "stale")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.counts("cart", "payments", "checkout"),
                         {"cart": 2, "payments": 3, "checkout": 3})

    def test_ac02_same_mtime_size_add_delete_rename_and_missing_baseline(self):
        """AC-02: равные mtime/size при других байтах, add/delete/rename файла и импорта и потеря baseline исключают ложный reuse."""
        self._build_shop()
        self.assertEqual(self.cli("checks", "run", "DEV-01")[0], 0)
        self.assertEqual(self.counts("cart", "payments", "checkout"),
                         {"cart": 1, "payments": 1, "checkout": 1})

        # 1. Одинаковые mtime и размер файла при иных байтах -> SHA-256 ловит правку.
        pay_file = self.root / "src/payments/charge.py"
        before_bytes = pay_file.read_bytes()
        before_stat = pay_file.stat()
        replaced = before_bytes.replace(b"FEE = 2", b"FEE = 9")
        self.assertEqual(len(before_bytes), len(replaced))
        pay_file.write_bytes(replaced)
        os.utime(pay_file, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
        self.assertEqual(self.cli("checks", "run", "DEV-01")[0], 0)
        self.assertEqual(self.counts("cart", "payments", "checkout"),
                         {"cart": 1, "payments": 2, "checkout": 2})

        # 2. Добавление нового файла в модуль cart запускает cart и checkout.
        extra = self.write("src/cart/extra.py", "BONUS = 1\n")
        self.assertEqual(self.cli("checks", "run", "DEV-01")[0], 0)
        self.assertEqual(self.counts("cart", "payments", "checkout"),
                         {"cart": 2, "payments": 2, "checkout": 3})

        # 3. Переименование (delete + add) внутри модуля cart обнаруживается без Git.
        renamed = self.root / "src/cart/renamed_extra.py"
        extra.rename(renamed)
        self.assertEqual(self.cli("checks", "run", "DEV-01")[0], 0)
        self.assertEqual(self.counts("cart", "payments", "checkout"),
                         {"cart": 3, "payments": 2, "checkout": 4})

        # 4. Удаление файла и удаление импорта сохраняют прежнего потребителя через union_graph.
        renamed.unlink()
        self.write(
            "src/checkout/order.py",
            "from src.cart.basket import cart_total\n"
            "def finalize(items):\n    return cart_total(items)\n",
        )
        self.write(
            "src/payments/charge.py",
            "from src.discounts.calc import apply_discount\n"
            "FEE = 7\ndef charge_amount(raw):\n    return apply_discount(raw) + FEE\n",
        )
        self.assertEqual(self.cli("checks", "run", "DEV-01")[0], 0)
        # Даже после удаления импорта payments из checkout прежний граф сохраняет ребро на переход!
        self.assertEqual(self.counts("cart", "payments", "checkout"),
                         {"cart": 4, "payments": 3, "checkout": 5})

        # 5. Потеря baseline при изменённом входе ведёт к консервативному выбору без ложного reuse.
        (self.root / "work/checks/snapshot.json").unlink()
        self.write("src/cart/basket.py",
                   "from src.discounts.calc import apply_discount\n"
                   "def cart_total(items):\n    return apply_discount(sum(items)) + 1\n")
        code, plan_out, err = self.cli("checks", "plan", "DEV-01")
        self.assertEqual(code, 0, err)
        self.assertIn("baseline отсутствует", plan_out)
        self.assertEqual(self.cli("checks", "run", "DEV-01")[0], 0)
        self.assertEqual(self.counts("cart", "payments", "checkout"),
                         {"cart": 5, "payments": 3, "checkout": 6})

    def test_ac02_import_cycle_and_dynamic_import_expand_conservatively(self):
        """AC-02: циклы импортов не зацикливаются, а dynamic import помечается unknown и исключает ложный пропуск."""
        self._build_shop()
        # Цикл src/cart/a.py <-> src/cart/b.py внутри модуля cart не зацикливает анализатор и замыкание
        self.write("src/cart/a.py", "if False:\n    import src.cart.b\nA = 1\n")
        self.write("src/cart/b.py", "if False:\n    import src.cart.a\nB = 2\n")
        self.write(
            "src/cart/basket.py",
            "from src.discounts.calc import apply_discount\n"
            "import src.cart.a\n"
            "def cart_total(items):\n    return apply_discount(sum(items))\n",
        )
        self.assertEqual(self.cli("checks", "run", "DEV-01")[0], 0)
        self.assertEqual(self.counts("cart", "payments", "checkout"),
                         {"cart": 1, "payments": 1, "checkout": 1})

        # Динамический импорт в cart делает независимость недоказанной (unknown):
        # сначала выполняется добавление plugin.py в cart (запускаются cart и checkout),
        # а затем правка payments — из-за unknown в cart не даёт ложного reuse cart!
        self.write(
            "src/cart/plugin.py",
            "import importlib\ndef load_hook(name):\n    return importlib.import_module(name)\n",
        )
        code, plan_out, err = self.cli("checks", "plan", "DEV-01")
        self.assertEqual(code, 0, err)
        self.assertNotIn("unknown-областей: 0", plan_out)
        self.assertEqual(self.cli("checks", "run", "DEV-01")[0], 0)
        self.assertEqual(self.counts("cart", "payments", "checkout"),
                         {"cart": 2, "payments": 1, "checkout": 2})

        self.write(
            "src/payments/charge.py",
            "from src.discounts.calc import apply_discount\n"
            "FEE = 11\ndef charge_amount(raw):\n    return apply_discount(raw) + FEE\n",
        )
        self.assertEqual(self.cli("checks", "run", "DEV-01")[0], 0)
        self.assertEqual(self.counts("cart", "payments", "checkout"),
                         {"cart": 3, "payments": 2, "checkout": 3})

    def test_check_dependency_chain_transitive_input_rerun(self):
        """A-01: цепочка check→check→module — изменение транзитивного
        входа перезапускает и зависимую проверку, а не только прямую."""
        for name in "abc":
            self.write(f"src/{name}/v.txt", "v1")
        self.create_card("DEV-98")
        policy = {
            "schema_version": 1,
            "policy_id": "chain",
            "revision": 1,
            "project_id": "no-git-shop",
            "trust_mode": "local",
            "approved_basis": _basis(),
            "subsystems": [{"id": "core", "title": "Core"}],
            "modules": [
                {"id": n, "title": n, "subsystem_id": "core",
                 "paths": [f"src/{n}/**"]} for n in "abc"
            ] + [{"id": "meta", "title": "Meta", "subsystem_id": "core",
                  "paths": ["ai-dev-playbook/**", "docs/**",
                            "AGENTS.md", "CHANGELOG.md"]}],
            "checks": [
                {"id": n, "title": n, "purpose": n, "kind": "command",
                 "modules": [n], "argv": self.command_argv(n),
                 "timeout_seconds": 10} for n in "ab"
            ],
            "dependencies": [
                {"consumer": "check:a", "dependency": "check:b",
                 "kind": "api", "source": "explicit",
                 "evidence": "declared chain"},
                {"consumer": "check:b", "dependency": "module:c",
                 "kind": "api", "source": "explicit",
                 "evidence": "declared input"},
            ],
            "excludes": [],
            "environment": {"toolchain": [
                {"id": "python", "version": platform.python_version()}],
                "external": []},
            "fallback": {
                "expansion_order": ["module", "subsystem", "project"],
                "approved_commands": [], "unknown_scope": "project",
                "limits": {"max_seconds": 100, "max_bytes": 5_000_000}},
            "test_catalog": _stub_catalog(),
        }
        self.write("checks-policy.json",
                   json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("setup", "apply")[0], 0)
        self.assertEqual(
            self.cli("checks", "run", "DEV-98", "--scope", "project")[0], 0)
        self.assertEqual(self.counts("a", "b"), {"a": 1, "b": 1})

        self.write("src/c/v.txt", "v2")
        code, out, err = self.cli(
            "checks", "run", "DEV-98", "--scope", "project", "--mode", "stale")
        self.assertEqual(code, 0, out + err)
        # a зависит от b, b — от module:c: изменение c обязано
        # перезапустить обе проверки, а не выдать a по старому reuse.
        self.assertEqual(self.counts("a", "b"), {"a": 2, "b": 2})
        self.assertEqual(self.cli("checks", "release-ready", "DEV-98")[0], 0)


class InputsExclusionsFallbackBudgetE2ETest(NoGitChecksE2ECase):
    """AC-03 и AC-04 спецификации: исключения, бинарные входы, проекция карточки, fallback 8/2 и бюджет."""

    def test_ac03_excluded_logs_card_projection_binary_inputs_and_unknown_file(self):
        """AC-03: лог не запускает тесты, карточка разделяет структуру и критерии, PNG/архив учитываются, неизвестный файл не теряется."""
        self.write("src/app.py", "VALUE = 1\n")
        self.write("assets/logo.png", b"\x89PNG\r\n\x1a\nv1")
        self.write("assets/fixture.tar.gz", b"\x1f\x8b\x08\x00archive-v1")
        self.write("logs/runtime.log", "boot\n")
        card = self.create_card("DEV-03", ac_text="Исходное требование к логотипу и архиву.")

        policy = {
            "schema_version": 1,
            "policy_id": "inputs-policy",
            "revision": 1,
            "project_id": "no-git-shop",
            "trust_mode": "local",
            "approved_basis": _basis(),
            "subsystems": [{"id": "app", "title": "Приложение"}],
            "modules": [
                {"id": "code", "title": "Код", "subsystem_id": "app",
                 "paths": ["src/**"]},
                {"id": "assets", "title": "Ресурсы", "subsystem_id": "app",
                 "paths": ["assets/**"]},
                {"id": "meta", "title": "Метаданные проекта", "subsystem_id": "app",
                 "paths": ["ai-dev-playbook/**", "docs/**", "AGENTS.md", "CHANGELOG.md",
                           "README.md", "checks-policy.json"]},
            ],
            "checks": [
                {
                    "id": "product-check",
                    "title": "Продуктовая проверка",
                    "purpose": "Проверка кода и бинарных входов",
                    "kind": "command",
                    "modules": ["code", "assets"],
                    "argv": self.command_argv("product"),
                    "inputs": ["assets/logo.png", "assets/fixture.tar.gz"],
                    "timeout_seconds": 20,
                },
                {
                    "id": "acceptance-check",
                    "title": "Проверка критериев задачи",
                    "purpose": "Содержательная проверка требований карточки",
                    "kind": "command",
                    "modules": ["code"],
                    "argv": self.command_argv("acceptance"),
                    "inputs": [{"type": "task_projection"}],
                    "timeout_seconds": 20,
                },
                {
                    "id": "card-structure",
                    "title": "Структурная проверка карточек",
                    "purpose": "Проверка структуры карточек и ссылок",
                    "kind": "command",
                    "modules": ["meta"],
                    "argv": self.command_argv("structure"),
                    "inputs": ["docs/tasks/DEV-03.md"],
                    "timeout_seconds": 20,
                },
            ],
            "dependencies": [],
            "excludes": [
                {"pattern": "logs/**", "reason": "Одобренные runtime-логи",
                 "origin": "manual"},
            ],
            "environment": {"toolchain": [{"id": "python", "version": platform.python_version()}], "external": []},
            "fallback": {
                "expansion_order": ["module", "subsystem", "project"],
                "approved_commands": [],
                "unknown_scope": "subsystem",
                "limits": {"max_seconds": 120, "max_bytes": 10_000_000},
            },
            "test_catalog": _stub_catalog(),
        }
        self.write("checks-policy.json", json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("setup", "apply")[0], 0)
        self.assertEqual(self.cli("checks", "run", "DEV-03")[0], 0)
        self.assertEqual(self.counts("product", "acceptance", "structure"),
                         {"product": 1, "acceptance": 1, "structure": 1})

        # 1. Изменение исключённого лога не запускает продуктовые и структурные проверки.
        self.write("logs/runtime.log", "boot\nmore log lines\n")
        self.assertEqual(self.cli("checks", "run", "DEV-03")[0], 0)
        self.assertEqual(self.counts("product", "acceptance", "structure"),
                         {"product": 1, "acceptance": 1, "structure": 1})

        # 2. Служебная запись в журнале карточки запускает только card-structure, не product/acceptance.
        card.write_text(
            card.read_text(encoding="utf-8")
            + "\n### EXEC-01 — Выполнение\n"
            "Время: 2026-11-14T12:10:00+03:00\n"
            "Исполнитель: Devin\n"
            "Модель: Gemini 4 Carbon Medium\n"
            "Изменения: записан шаг.\n"
            "Файлы:\n- src/app.py\n",
            encoding="utf-8",
        )
        self.assertEqual(self.cli("checks", "run", "DEV-03")[0], 0)
        self.assertEqual(self.counts("product", "acceptance", "structure"),
                         {"product": 1, "acceptance": 1, "structure": 2})

        # 3. Изменение критерия приёмки запускает acceptance-check и card-structure, но не product-check.
        card.write_text(
            card.read_text(encoding="utf-8").replace(
                "Исходное требование к логотипу и архиву.",
                "Обновлённое требование к логотипу, прозрачности и архиву.",
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.cli("checks", "run", "DEV-03")[0], 0)
        self.assertEqual(self.counts("product", "acceptance", "structure"),
                         {"product": 1, "acceptance": 2, "structure": 3})

        # 4. Изменение бинарного PNG и архива учитывается входами и запускает product-check.
        self.write("assets/logo.png", b"\x89PNG\r\n\x1a\nv2-updated")
        self.assertEqual(self.cli("checks", "run", "DEV-03")[0], 0)
        self.assertEqual(self.counts("product", "acceptance", "structure"),
                         {"product": 2, "acceptance": 2, "structure": 3})
        self.write("assets/fixture.tar.gz", b"\x1f\x8b\x08\x00archive-v2")
        self.assertEqual(self.cli("checks", "run", "DEV-03")[0], 0)
        self.assertEqual(self.counts("product", "acceptance", "structure"),
                         {"product": 3, "acceptance": 2, "structure": 3})

        # 5. Попытка исключить объявленный вход (assets/logo.png) отклоняется контрактом.
        bad_policy = json.loads((self.root / "checks-policy.json").read_text(encoding="utf-8"))
        bad_policy["excludes"].append({
            "pattern": "assets/logo.png",
            "reason": "Незаконная попытка скрыть вход",
            "origin": "manual",
        })
        with self.assertRaises(ValueError):
            checks_contract.validate_policy(
                bad_policy,
                lambda cond, msg: (_ for _ in ()).throw(ValueError(msg)) if not cond else None,
            )

        # 6. Неизвестный файл в корне не теряется: при unknown_scope=subsystem блокирует запуск.
        self.write("mystery_unattributed.dat", b"unknown-payload")
        code, plan_out, _ = self.cli("checks", "plan", "DEV-03")
        self.assertEqual(code, 0)
        self.assertIn("mystery_unattributed.dat", plan_out)
        self.assertIn("BLOCKED", plan_out)
        self.assertEqual(self.cli("checks", "run", "DEV-03")[0], 1)
        self.assertEqual(self.counts("product", "acceptance", "structure"),
                         {"product": 3, "acceptance": 2, "structure": 3})

    def test_ac04_subsystem_boundary_eight_reused_two_run_and_budget_stop(self):
        """AC-04: подсистема расширяет набор в доказанных границах (8 reused, 2 run), при недоказанной независимости reuse запрещён, бюджет блокирует запуск."""
        modules = [
            {"id": "meta", "title": "Мета", "subsystem_id": "infra",
             "paths": ["ai-dev-playbook/**", "docs/**", "AGENTS.md", "CHANGELOG.md"]},
        ]
        checks = []
        # 2 модуля в подсистеме billing и 8 независимых модулей в подсистеме catalog
        for idx in range(2):
            mid = f"bill-{idx}"
            self.write(f"src/{mid}/mod.py", f"X = {idx}\n")
            modules.append({"id": mid, "title": mid, "subsystem_id": "billing",
                            "paths": [f"src/{mid}/**"]})
            checks.append({
                "id": f"chk-{mid}",
                "title": f"Проверка {mid}",
                "purpose": f"Проверка модуля {mid}",
                "kind": "command",
                "modules": [mid],
                "argv": self.command_argv(mid),
                "timeout_seconds": 25,
            })
        for idx in range(8):
            mid = f"ind-{idx}"
            self.write(f"src/{mid}/mod.py", f"Y = {idx}\n")
            modules.append({"id": mid, "title": mid, "subsystem_id": "catalog",
                            "paths": [f"src/{mid}/**"]})
            checks.append({
                "id": f"chk-{mid}",
                "title": f"Проверка {mid}",
                "purpose": f"Независимая проверка {mid}",
                "kind": "command",
                "modules": [mid],
                "argv": self.command_argv(mid),
                "timeout_seconds": 25,
            })
        self.create_card("DEV-04")
        policy = {
            "schema_version": 1,
            "policy_id": "ten-checks",
            "revision": 1,
            "project_id": "no-git-shop",
            "trust_mode": "local",
            "approved_basis": _basis(),
            "subsystems": [
                {"id": "infra", "title": "Инфраструктура"},
                {"id": "billing", "title": "Биллинг"},
                {"id": "catalog", "title": "Каталог"},
            ],
            "modules": modules,
            "checks": checks,
            "dependencies": [
                # Подсистема billing внутренне связана: изменение bill-0 затрагивает всю подсистему billing (2 проверки)
                {"consumer": "subsystem:billing", "dependency": "module:bill-0",
                 "kind": "api", "source": "explicit", "evidence": "shared billing bus"},
            ],
            "excludes": [],
            "environment": {"toolchain": [{"id": "python", "version": platform.python_version()}], "external": []},
            "fallback": {
                "expansion_order": ["module", "subsystem", "project"],
                "approved_commands": [],
                "unknown_scope": "project",
                "limits": {"max_seconds": 500, "max_bytes": 5_000_000},
            },
            "test_catalog": _stub_catalog(),
        }
        self.write("checks-policy.json", json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("setup", "apply")[0], 0)

        # Первичный прогон выполняет все 10 проверок.
        self.assertEqual(self.cli("checks", "run", "DEV-04")[0], 0)
        all_names = [f"bill-{i}" for i in range(2)] + [f"ind-{i}" for i in range(8)]
        self.assertEqual(self.counts(*all_names), {name: 1 for name in all_names})

        # Правка bill-0 расширяет набор по границе подсистемы billing: 2 выполняются, 8 независимых reused!
        self.write("src/bill-0/mod.py", "X = 100\n")
        self.assertEqual(self.cli("checks", "run", "DEV-04", "--scope", "project", "--mode", "stale")[0], 0)
        expected = {f"bill-{i}": 2 for i in range(2)}
        expected.update({f"ind-{i}": 1 for i in range(8)})
        self.assertEqual(self.counts(*all_names), expected)

        # Недоказанная независимость (смена графа зависимостей + общий unknown_scope=project): reuse запрещён.
        policy["revision"] = 2
        policy["dependencies"].extend(
            {"consumer": f"module:ind-{i}", "dependency": "module:bill-0",
             "kind": "api", "source": "explicit", "evidence": "global coupling"}
            for i in range(8)
        )
        self.write("checks-policy.json", json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("checks", "run", "DEV-04", "--scope", "project", "--mode", "stale")[0], 0)
        self.assertEqual(self.counts(*all_names), {name: 3 if name.startswith("bill-") else 2 for name in all_names})

        # Бюджет останавливает запуск ДО порождения подпроцессов (оценка 10 * 25 = 250с > max_seconds=100с).
        before_budget = self.counts(*all_names)
        policy["revision"] = 3
        policy["fallback"]["limits"]["max_seconds"] = 100
        self.write("checks-policy.json", json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
        code, run_out, _ = self.cli("checks", "run", "DEV-04", "--scope", "project", "--mode", "force")
        self.assertEqual(code, 1, run_out)
        self.assertEqual(self.counts(*all_names), before_budget)


class ReleaseModesFreshnessAndAdapterE2ETest(NoGitChecksE2ECase):
    """AC-05 и AC-06 спецификации: релизный cross-task reuse, stale/force, инвалидация и негативные проверки адаптера."""

    def test_ac05_release_cross_task_stale_force_invalidation_and_late_failed(self):
        """AC-05: релиз собирает обязательства разных задач, stale сохраняет свежие, force запускает всё, смена среды/TTL/команды/фикстуры и поздний failed блокируют reuse."""
        self.write("src/cart/item.py", "PRICE = 10\n")
        self.write("src/pay/tx.py", "TAX = 1\n")
        self.write("fixtures/rates.json", '{"usd": 1}\n')
        flag = self.base / "pay_fail.flag"
        flag.write_text("0", encoding="utf-8")

        self.create_card("DEV-51", title="Задача корзины")
        self.create_card("DEV-52", title="Задача оплаты")
        self.create_card("REL-01", title="Подготовка релиза")

        pay_script = (
            "import os, sys; from pathlib import Path; "
            "d=Path(os.environ['PB064_COUNTERS']); p=d/'pay.count'; "
            "p.write_text(str((int(p.read_text()) if p.is_file() else 0)+1)); "
            f"sys.exit(int(Path({str(flag)!r}).read_text()))"
        )
        policy = {
            "schema_version": 1,
            "policy_id": "release-policy",
            "revision": 1,
            "project_id": "no-git-shop",
            "trust_mode": "local",
            "approved_basis": _basis(),
            "subsystems": [{"id": "core", "title": "Ядро"}],
            "modules": [
                {"id": "cart", "title": "Корзина", "subsystem_id": "core", "paths": ["src/cart/**"]},
                {"id": "pay", "title": "Оплата", "subsystem_id": "core", "paths": ["src/pay/**"]},
                {"id": "meta", "title": "Мета", "subsystem_id": "core",
                 "paths": ["ai-dev-playbook/**", "docs/**", "fixtures/**", "AGENTS.md", "CHANGELOG.md"]},
            ],
            "checks": [
                {
                    "id": "cart-check",
                    "title": "Проверка корзины",
                    "purpose": "Модуль корзины",
                    "kind": "command",
                    "modules": ["cart"],
                    "argv": self.command_argv("cart"),
                    "inputs": ["fixtures/rates.json"],
                    "required_environment": ["pricing-api"],
                    "timeout_seconds": 20,
                },
                {
                    "id": "pay-check",
                    "title": "Проверка оплаты",
                    "purpose": "Модуль оплаты",
                    "kind": "command",
                    "modules": ["pay"],
                    "argv": [sys.executable, "-c", pay_script],
                    "required_environment": ["gateway-ttl"],
                    "timeout_seconds": 20,
                },
                {
                    "id": "release-gate",
                    "title": "Релизная контрольная проверка",
                    "purpose": "Обязательна на контрольной точке release",
                    "kind": "command",
                    "modules": [],
                    "inputs": ["src/cart", "src/pay"],
                    "applicability": {"kind": "checkpoint", "checkpoint": "release"},
                    "argv": self.command_argv("release"),
                    "timeout_seconds": 20,
                },
            ],
            "dependencies": [],
            "excludes": [],
            "environment": {
                "toolchain": [{"id": "python", "version": platform.python_version()}],
                "external": [
                    {"id": "pricing-api", "freshness": {"kind": "version", "version": "v1"}},
                    {"id": "gateway-ttl", "freshness": {"kind": "ttl", "ttl_seconds": 3600}},
                ],
            },
            "fallback": {
                "expansion_order": ["module", "subsystem", "project"],
                "approved_commands": [],
                "unknown_scope": "project",
                "limits": {"max_seconds": 300, "max_bytes": 5_000_000},
            },
            "test_catalog": _stub_catalog(),
        }
        self.write("checks-policy.json", json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("setup", "apply")[0], 0)

        # Первичный базовый прогон без release checkpoint: cart=1, pay=1, release=0.
        self.assertEqual(self.cli("checks", "run", "DEV-51")[0], 0)
        self.assertEqual(self.counts("cart", "pay", "release"), {"cart": 1, "pay": 1, "release": 0})

        # DEV-51 меняет cart, DEV-52 меняет pay в отдельных задачах.
        self.write("src/cart/item.py", "PRICE = 12\n")
        self.assertEqual(self.cli("checks", "run", "DEV-51", "--scope", "task", "--mode", "stale")[0], 0)
        self.write("src/pay/tx.py", "TAX = 2\n")
        self.assertEqual(self.cli("checks", "run", "DEV-52", "--scope", "task", "--mode", "stale")[0], 0)
        self.assertEqual(self.counts("cart", "pay", "release"), {"cart": 2, "pay": 2, "release": 0})

        # До запуска release-gate готовность релиза отклоняется; stale переиспользует cart и pay из разных задач!
        self.assertEqual(self.cli("checks", "release-ready", "REL-01")[0], 1)
        self.assertEqual(
            self.cli("checks", "run", "REL-01", "--scope", "project",
                     "--checkpoint", "release", "--mode", "stale")[0],
            0,
        )
        self.assertEqual(self.counts("cart", "pay", "release"), {"cart": 2, "pay": 2, "release": 1})
        self.assertEqual(self.cli("checks", "release-ready", "REL-01")[0], 0)

        # force перезапускает весь запрошенный набор заново.
        self.assertEqual(
            self.cli("checks", "run", "REL-01", "--scope", "project",
                     "--checkpoint", "release", "--mode", "force")[0],
            0,
        )
        self.assertEqual(self.counts("cart", "pay", "release"), {"cart": 3, "pay": 3, "release": 2})

        # Смена fixture обесценивает связанный результат cart-check.
        self.write("fixtures/rates.json", '{"usd": 2}\n')
        self.assertEqual(self.cli("checks", "release-ready", "REL-01")[0], 1)
        self.assertEqual(self.cli("checks", "run", "REL-01", "--scope", "project", "--mode", "stale")[0], 0)
        self.assertEqual(self.counts("cart", "pay", "release"), {"cart": 4, "pay": 3, "release": 2})

        # Смена API-версии (v1 -> v2), команды argv и истечение TTL обесценивают соответствующие результаты.
        policy["revision"] = 2
        policy["environment"]["external"][0]["freshness"]["version"] = "v2"
        policy["environment"]["external"][1]["freshness"]["ttl_seconds"] = 1
        policy["checks"][2]["argv"] = self.command_argv("release", "x=1")
        self.write("checks-policy.json", json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(
            self.cli("checks", "run", "REL-01", "--scope", "project",
                     "--checkpoint", "release", "--mode", "stale")[0],
            0,
        )
        self.assertEqual(self.counts("cart", "pay", "release"), {"cart": 5, "pay": 4, "release": 3})
        time.sleep(1.1)
        self.assertEqual(self.cli("checks", "release-ready", "REL-01")[0], 1)

        # Поздний failed для pay-check не скрывается прежним passed.
        policy["revision"] = 3
        policy["environment"]["external"][1]["freshness"]["ttl_seconds"] = 3600
        self.write("checks-policy.json", json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(
            self.cli("checks", "run", "REL-01", "--scope", "project",
                     "--checkpoint", "release", "--mode", "stale")[0],
            0,
        )
        self.assertEqual(self.cli("checks", "release-ready", "REL-01")[0], 0)
        flag.write_text("1", encoding="utf-8")
        self.assertEqual(self.cli("checks", "run", "DEV-52", "--scope", "project", "--mode", "force")[0], 1)
        self.assertEqual(self.cli("checks", "release-ready", "REL-01")[0], 1)

    def test_ac06_adapter_negatives_descriptions_reused_time_and_label_rename(self):
        """AC-06: exit 0 с 0 тестов, пропавший ID, лишний skip, дубликат и порча отчёта отклоняются; описания и исходное время reused сохраняются."""
        self.write("src/__init__.py", "")
        self.write("src/calc.py", "def add(a, b):\n    return a + b\n")
        self.write("tests/__init__.py", "")
        self.write("tests/counter_helper.py", COUNTER_HELPER)
        self.write(
            "tests/test_calc.py",
            'import unittest\nfrom tests.counter_helper import bump\nfrom src.calc import add\n\n'
            'class CalcTest(unittest.TestCase):\n'
            '    def test_add(self):\n'
            '        """Сложение двух положительных чисел возвращает сумму."""\n'
            '        bump("calc")\n'
            '        self.assertEqual(add(2, 3), 5)\n',
        )
        self.create_card("DEV-06")
        policy = {
            "schema_version": 1,
            "policy_id": "adapter-policy",
            "revision": 1,
            "project_id": "no-git-shop",
            "trust_mode": "local",
            "approved_basis": _basis(),
            "subsystems": [{"id": "core", "title": "Ядро"}],
            "modules": [
                {"id": "calc", "title": "Вычисления", "subsystem_id": "core",
                 "paths": ["src/**", "tests/**"]},
                {"id": "meta", "title": "Мета", "subsystem_id": "core",
                 "paths": ["ai-dev-playbook/**", "docs/**", "AGENTS.md", "CHANGELOG.md"]},
            ],
            "checks": [{
                "id": "calc-tests",
                "title": "Тесты вычислений",
                "purpose": "Проверка сложения",
                "kind": "test",
                "modules": ["calc"],
                "adapter": "python-unittest",
                "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                "timeout_seconds": 30,
            }],
            "dependencies": [],
            "excludes": [],
            "environment": {"toolchain": [{"id": "python", "version": platform.python_version()}], "external": []},
            "fallback": {
                "expansion_order": ["module", "subsystem", "project"],
                "approved_commands": [],
                "unknown_scope": "project",
                "limits": {"max_seconds": 120, "max_bytes": 10_000_000},
            },
            "test_catalog": {
                "adapter": "python-unittest",
                "fingerprint": "sha256:" + "b" * 64,
                "entries": [{
                    "id": "tests.test_calc.CalcTest.test_add",
                    "group": "calc-tests",
                    "description": "Сложение двух положительных чисел возвращает сумму.",
                    "skip": {"allowed": False},
                    "expected_failure": {"allowed": False},
                }],
            },
        }
        self.write("checks-policy.json", json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("setup", "apply")[0], 0)

        # Позитивный запуск и последующий reuse сохраняют описание и исходное время issued_at.
        code, out, err = self.cli("checks", "run", "DEV-06")
        self.assertEqual(code, 0, out + err)
        first_results = sorted((self.root / "work/checks/results").rglob("*.json"))
        self.assertEqual(len(first_results), 1)
        first_doc = json.loads(first_results[0].read_text(encoding="utf-8"))
        issued_at = first_doc["finished_at"]
        time.sleep(1.05)
        self.assertEqual(self.cli("checks", "run", "DEV-06", "--scope", "project", "--mode", "stale")[0], 0)
        self.assertEqual(self.count("calc"), 1)
        code, status_out, err = self.cli("checks", "status", "DEV-06")
        self.assertEqual(code, 0, err)
        self.assertIn("tests.test_calc.CalcTest.test_add", status_out)
        self.assertIn("Сложение двух положительных чисел возвращает сумму.", status_out)
        self.assertIn(issued_at, status_out)

        # Rename label (title/description) не выдаётся за новый test ID.
        policy["revision"] = 2
        policy["checks"][0]["title"] = "Переименованная группа вычислений"
        policy["test_catalog"]["entries"][0]["description"] = "Обновлённое описание того же стабильного теста."
        self.write("checks-policy.json", json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("checks", "run", "DEV-06", "--scope", "project", "--mode", "force")[0], 0)
        code, status_out, _ = self.cli("checks", "status", "DEV-06")
        self.assertEqual(code, 0)
        self.assertIn("Обновлённое описание того же стабильного теста.", status_out)

        # 1. Лишний skip отклонён.
        self.write(
            "tests/test_calc.py",
            'import unittest\n\nclass CalcTest(unittest.TestCase):\n'
            '    @unittest.skip("неразрешённый пропуск")\n'
            '    def test_add(self):\n        pass\n',
        )
        self.assertEqual(self.cli("checks", "run", "DEV-06", "--scope", "project", "--mode", "force")[0], 1)

        # 2. Пропавший test ID (другой метод вместо одобренного) отклонён.
        self.write(
            "tests/test_calc.py",
            'import unittest\n\nclass CalcTest(unittest.TestCase):\n'
            '    def test_other(self):\n        pass\n',
        )
        self.assertEqual(self.cli("checks", "run", "DEV-06", "--scope", "project", "--mode", "force")[0], 1)

        # 3. exit 0 с нулём тестов (пустой тестовый модуль) отклонён.
        self.write("tests/test_calc.py", "# no tests here\n")
        self.assertEqual(self.cli("checks", "run", "DEV-06", "--scope", "project", "--mode", "force")[0], 1)

        # 4. Дубликат события одного test ID в отчёте адаптера отклонён.
        self.write(
            "tests/test_calc.py",
            'import unittest\nfrom tests.counter_helper import bump\nfrom src.calc import add\n\n'
            'class CalcTest(unittest.TestCase):\n'
            '    def test_add(self):\n'
            '        """Сложение работает штатно."""\n'
            '        bump("calc")\n'
            '        self.assertEqual(add(2, 3), 5)\n\n'
            'def load_tests(loader, standard_tests, pattern):\n'
            '    suite = unittest.TestSuite()\n'
            '    suite.addTests(standard_tests)\n'
            '    suite.addTests(loader.loadTestsFromTestCase(CalcTest))\n'
            '    return suite\n',
        )
        self.assertEqual(self.cli("checks", "run", "DEV-06", "--scope", "project", "--mode", "force")[0], 1)

        # 5. Повреждённый JSON результата в истории не даёт ложного reuse — вызывает перезапуск.
        self.write(
            "tests/test_calc.py",
            'import unittest\nfrom tests.counter_helper import bump\nfrom src.calc import add\n\n'
            'class CalcTest(unittest.TestCase):\n'
            '    def test_add(self):\n'
            '        """Сложение работает штатно."""\n'
            '        bump("calc")\n'
            '        self.assertEqual(add(2, 3), 5)\n',
        )
        self.assertEqual(self.cli("checks", "run", "DEV-06", "--scope", "project", "--mode", "force")[0], 0)
        before_corrupt = self.count("calc")
        latest_json = max((self.root / "work/checks/results").rglob("*.json"),
                          key=lambda p: p.stat().st_mtime_ns)
        latest_json.write_text("{corrupt-json", encoding="utf-8")
        self.assertEqual(self.cli("checks", "run", "DEV-06", "--scope", "project", "--mode", "stale")[0], 0)
        self.assertEqual(self.count("calc"), before_corrupt + 1)


class TrustBoundaryLifecycleAndMigrationE2ETest(NoGitChecksE2ECase):
    """AC-07–10 спецификации и AC-03 карточки: граница доверия, обходы, lifecycle/архив и миграция."""

    def _protected_command_policy(self, argv, project_id="no-git-shop"):
        return {
            "schema_version": 1,
            "policy_id": "protected-policy",
            "revision": 1,
            "project_id": project_id,
            "trust_mode": "protected",
            "approved_basis": _basis(),
            "subsystems": [{"id": "core", "title": "Ядро"}],
            "modules": [
                {"id": "code", "title": "Код", "subsystem_id": "core", "paths": ["src/**"]},
                {"id": "meta", "title": "Мета", "subsystem_id": "core",
                 "paths": ["ai-dev-playbook/**", "docs/**", "AGENTS.md", "CHANGELOG.md"]},
            ],
            "checks": [{
                "id": "cmd-verify",
                "title": "Наблюдаемая команда",
                "purpose": "Проверка кода в изолированном контуре",
                "kind": "command",
                "modules": ["code"],
                "argv": argv,
                "timeout_seconds": 20,
            }],
            "dependencies": [
                {"consumer": "check:cmd-verify", "dependency": "module:code",
                 "kind": "api", "source": "explicit", "evidence": "direct check"},
            ],
            "excludes": [],
            "environment": {"toolchain": [{"id": "python", "version": platform.python_version()}], "external": []},
            "fallback": {
                "expansion_order": ["module", "subsystem", "project"],
                "approved_commands": [],
                "unknown_scope": "project",
                "limits": {"max_seconds": 120, "max_bytes": 10_000_000},
            },
            "test_catalog": _stub_catalog(),
        }

    def test_ac07_input_race_post_run_drift_arena_isolation_and_receipt_tamper(self):
        """AC-07: мутация входа во время и после запуска блокирует passed; изолированный кандидат и подмена квитанции проверяются."""
        self.write("src/service.py", "STATE = 1\n")
        self.create_card("DEV-07")

        # 1. Мутация собственного входа во время локального запуска даёт blocked, а не passed.
        mutating = [
            sys.executable, "-c",
            "from pathlib import Path; Path('src/service.py').write_text('STATE = 2\\n')",
        ]
        local_policy = self._protected_command_policy(mutating)
        local_policy["trust_mode"] = "local"
        self.write("checks-policy.json", json.dumps(local_policy, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("setup", "apply")[0], 0)
        code, out, _ = self.cli("checks", "run", "DEV-07", "--scope", "project", "--mode", "force")
        self.assertEqual(code, 1, out)

        # 2. Чистый запуск покрывает задачу, но правка файла ПОСЛЕ запуска блокирует release-ready.
        local_policy["revision"] = 2
        local_policy["checks"][0]["argv"] = self.command_argv("svc")
        self.write("checks-policy.json", json.dumps(local_policy, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("checks", "run", "DEV-07", "--scope", "project", "--mode", "force")[0], 0)
        self.assertEqual(self.cli("checks", "release-ready", "DEV-07", "--checkpoint", "release")[0], 0)
        self.write("src/service.py", "STATE = 3\n")
        self.assertEqual(self.cli("checks", "release-ready", "DEV-07", "--checkpoint", "release")[0], 1)

        # 3. Verifier исполняет команду в изолированном снимке arena и связывает квитанцию с candidate_digest.
        home = self.base / "verifier-home"
        prot_policy = self._protected_command_policy(
            [sys.executable, "-c", "from pathlib import Path; assert Path('src/service.py').is_file()"]
        )
        self.write("checks-policy.json", json.dumps(prot_policy, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("verifier", "init", "--home", str(home), "--project-id", "no-git-shop")[0], 0)
        self.assertEqual(self.cli("verifier", "approve", "--home", str(home))[0], 0)
        self.assertEqual(self.cli("verifier", "run", "--home", str(home), "--scope", "project")[0], 0)

        receipt_file = next((home / "receipts").glob("*.json"))
        self.assertEqual(self.cli("verifier", "verify", "--home", str(home), "--receipt", str(receipt_file))[0], 0)

        # Подмена квитанции и подмена кандидата после выпуска обнаруживаются.
        tampered = self.base / "tampered-receipt.json"
        doc = json.loads(receipt_file.read_text(encoding="utf-8"))
        doc["candidate_digest"] = "sha256:" + "f" * 64
        tampered.write_text(json.dumps(doc), encoding="utf-8")
        self.assertEqual(self.cli("verifier", "verify", "--home", str(home), "--receipt", str(tampered))[0], 1)

        self.write("src/service.py", "STATE = 4\n")
        _, new_cand = playbook.checks_verifier.candidate_manifest(self.root, prot_policy)
        self.assertEqual(
            self.cli("verifier", "verify", "--home", str(home),
                     "--receipt", str(receipt_file), "--candidate-digest", new_cand)[0],
            1,
        )

    def test_ac08_and_card_ac03_bypass_attempts_and_local_vs_protected_boundary(self):
        """AC-08 и AC-03 карточки: обходы доверенного gate отклоняются, границы local и protected проверяются раздельно."""
        self.write("src/guard.py", "OK = True\n")
        self.create_card("DEV-08")

        # 1. Поле approved=true в редактируемом JSON отвергается схемой политики.
        bad = self._protected_command_policy([sys.executable, "-c", "pass"])
        bad["approved"] = True
        self.write("checks-policy.json", json.dumps(bad, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("setup", "apply")[0], 1)

        # 2. Инициализация дома verifier и одобрение базовой политики.
        policy = self._protected_command_policy(
            [sys.executable, "-c", "from pathlib import Path; assert Path('src/guard.py').is_file()"]
        )
        self.write("checks-policy.json", json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("setup", "apply")[0], 0)
        home = self.base / "vhome"
        self.assertEqual(self.cli("verifier", "init", "--home", str(home), "--project-id", "no-git-shop")[0], 0)
        self.assertEqual(self.cli("verifier", "approve", "--home", str(home))[0], 0)
        self.assertEqual(self.cli("verifier", "run", "--home", str(home), "--scope", "project")[0], 0)
        receipt_path = next((home / "receipts").glob("*.json"))
        receipt_doc = json.loads(receipt_path.read_text(encoding="utf-8"))

        # 3. Ослабление политики (удаление связи, удаление проверки или новая команда) без --reapprove блокируется.
        weak_edge = json.loads(json.dumps(policy))
        weak_edge["revision"] = 2
        weak_edge["dependencies"] = []
        self.write("checks-policy.json", json.dumps(weak_edge, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("verifier", "approve", "--home", str(home))[0], 1)
        self.assertEqual(self.cli("verifier", "run", "--home", str(home), "--scope", "project")[0], 1)

        weak_cmd = json.loads(json.dumps(policy))
        weak_cmd["revision"] = 2
        weak_cmd["checks"][0]["argv"] = [sys.executable, "-c", "print('unapproved')"]
        self.write("checks-policy.json", json.dumps(weak_cmd, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("verifier", "approve", "--home", str(home))[0], 1)

        # Возвращаем одобренную политику и проверяем попытку записи подпроцесса в дом verifier.
        attack_policy = json.loads(json.dumps(policy))
        attack_policy["revision"] = 2
        attack_script = (
            f"from pathlib import Path; p = Path({str(home / 'keys' / 'pwned')!r}); "
            "p.write_text('hacked')"
        )
        attack_policy["checks"][0]["argv"] = [sys.executable, "-c", attack_script]
        self.write("checks-policy.json", json.dumps(attack_policy, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("verifier", "approve", "--home", str(home), "--reapprove")[0], 0)
        # Если sandbox-exec доступен на стенде macOS — запись в дом блокируется и команда падает!
        if shutil.which("sandbox-exec"):
            self.assertEqual(self.cli("verifier", "run", "--home", str(home), "--scope", "project")[0], 1)
            self.assertFalse((home / "keys" / "pwned").exists())

        # Восстанавливаем исходную политику rev 3 для проверок подделки, отзыва, другого project_id и replay.
        clean_policy = json.loads(json.dumps(policy))
        clean_policy["revision"] = 3
        self.write("checks-policy.json", json.dumps(clean_policy, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("verifier", "approve", "--home", str(home), "--reapprove")[0], 0)
        self.assertEqual(self.cli("verifier", "run", "--home", str(home), "--scope", "project")[0], 0)
        fresh_receipt = max((home / "receipts").glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
        fresh_doc = json.loads(fresh_receipt.read_text(encoding="utf-8"))

        # Поддельная квитанция в проектной папке не даёт покрытия.
        forged_dir = self.root / "work/checks/receipts"
        forged_dir.mkdir(parents=True, exist_ok=True)
        forged = dict(fresh_doc, signature="00" * 32)
        forged_file = forged_dir / "rc-forged.json"
        forged_file.write_text(json.dumps(forged), encoding="utf-8")
        self.assertEqual(self.cli("verifier", "verify", "--home", str(home), "--receipt", str(forged_file))[0], 1)

        # Другой project_id отклоняется.
        foreign_home = self.base / "foreign-home"
        self.assertEqual(self.cli("verifier", "init", "--home", str(foreign_home), "--project-id", "other-project")[0], 0)
        self.assertEqual(self.cli("verifier", "verify", "--home", str(foreign_home), "--receipt", str(fresh_receipt))[0], 1)

        # Раздельная проверка local vs protected границ:
        # На однопользовательском стенде (st_uid == euid) probe честно сообщает capability=local,
        # поэтому protected-готовность блокируется в gate и release-ready (R-20).
        code, probe_out, _ = self.cli("verifier", "probe", "--home", str(home))
        self.assertEqual(code, 0)
        self.assertIn("capability=local", probe_out)
        prev_home = os.environ.get("PLAYBOOK_VERIFIER_HOME")
        os.environ["PLAYBOOK_VERIFIER_HOME"] = str(home)
        try:
            code, rr_out, _ = self.cli("checks", "release-ready", "DEV-08")
            self.assertEqual(code, 1, rr_out)
            self.assertIn("не готово", rr_out)
        finally:
            if prev_home is None:
                os.environ.pop("PLAYBOOK_VERIFIER_HOME", None)
            else:
                os.environ["PLAYBOOK_VERIFIER_HOME"] = prev_home

        # При доказанной отдельной OS/CI границе тот же валидный набор квитанций проходит protected-gate,
        # но сразу блокируется при replay на изменённом кандидате или при отзыве квитанции/политики/ключа!
        with mock.patch.object(
            checks_trust, "probe",
            return_value={"configured": True, "capability": "protected",
                          "boundary": "distinct-uid-fixture", "notes": []},
        ):
            verdict_ok = checks_gate.evaluate(
                self.root, clean_policy, task_id="DEV-08", scope="project",
                verifier_home=home,
            )
            self.assertTrue(verdict_ok["ok"], verdict_ok)
            self.assertTrue(verdict_ok["protected_confirmed"])

            # Replay для других входов блокируется.
            self.write("src/guard.py", "OK = False\n")
            verdict_replay = checks_gate.evaluate(
                self.root, clean_policy, task_id="DEV-08", scope="project",
                verifier_home=home,
            )
            self.assertFalse(verdict_replay["ok"])
            self.write("src/guard.py", "OK = True\n")

            # Отзыв квитанции и ключа блокирует доверие немедленно.
            self.assertEqual(
                self.cli("verifier", "revoke", "--home", str(home),
                         "--receipt", fresh_doc["receipt_id"])[0],
                0,
            )
            verdict_revoked = checks_gate.evaluate(
                self.root, clean_policy, task_id="DEV-08", scope="project",
                verifier_home=home,
            )
            self.assertFalse(verdict_revoked["ok"])

        active_key = (home / "keys" / "active").read_text(encoding="utf-8").strip()
        self.assertEqual(self.cli("verifier", "revoke", "--home", str(home), "--key", active_key)[0], 0)
        self.assertEqual(self.cli("verifier", "verify", "--home", str(home), "--receipt", str(receipt_doc and fresh_receipt))[0], 1)

    def test_ac09_manual_and_task_criteria_unified_gate_and_archive_provenance(self):
        """AC-09: старый suite не закрывает новое требование и ручную проверку; handoff/accept/release-ready едины, архив сохраняет provenance."""
        self.write("src/feature.py", "MODE = 'v1'\n")
        card = self.create_card("DEV-09", ac_text="Пользователь видит подтверждение заказа.")

        policy = {
            "schema_version": 1,
            "policy_id": "lifecycle-policy",
            "revision": 1,
            "project_id": "no-git-shop",
            "trust_mode": "local",
            "approved_basis": _basis(),
            "subsystems": [{"id": "core", "title": "Ядро"}],
            "modules": [
                {"id": "code", "title": "Код", "subsystem_id": "core", "paths": ["src/**"]},
                {"id": "docs-specs", "title": "Спецификации", "subsystem_id": "core",
                 "paths": ["ai-dev-playbook/**", "docs/specs/**", "docs/designs/**",
                           "docs/adr/**", "docs/plans/**", "AGENTS.md", "CHANGELOG.md"]},
            ],
            "checks": [
                {
                    "id": "old-suite",
                    "title": "Старый автоматический набор",
                    "purpose": "Проверка кода",
                    "kind": "command",
                    "modules": ["code"],
                    "argv": self.command_argv("old_suite"),
                    "timeout_seconds": 20,
                },
                {
                    "id": "scenario-check",
                    "title": "Проверка сценария приёмки",
                    "purpose": "Связана с task_projection карточки",
                    "kind": "command",
                    "modules": ["code"],
                    "inputs": [{"type": "task_projection"}],
                    "argv": self.command_argv("scenario"),
                    "timeout_seconds": 20,
                },
                {
                    "id": "manual-ux",
                    "title": "Ручная проверка интерфейса",
                    "purpose": "Подтверждение отображения инженером",
                    "kind": "manual",
                    "modules": ["code"],
                    "scenario": "Проверить отображение подтверждения заказа и кода отмены.",
                    "inputs": ["src/feature.py", {"type": "task_projection"}],
                },
            ],
            "dependencies": [],
            "excludes": [],
            "environment": {"toolchain": [{"id": "python", "version": platform.python_version()}], "external": []},
            "fallback": {
                "expansion_order": ["module", "subsystem", "project"],
                "approved_commands": [],
                "unknown_scope": "project",
                "limits": {"max_seconds": 120, "max_bytes": 10_000_000},
            },
            "test_catalog": _stub_catalog(),
        }
        self.write("checks-policy.json", json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("setup", "apply")[0], 0)

        # Полный прогон автоматического suite оставляет manual-ux непокрытым -> run/release-ready/handoff отказывают!
        self.assertEqual(self.cli("checks", "run", "DEV-09", "--scope", "project", "--mode", "force")[0], 1)
        self.assertEqual(self.cli("checks", "release-ready", "DEV-09")[0], 1)

        card.write_text(
            card.read_text(encoding="utf-8").replace("Status: in_progress", "Status: in_review", 1)
            + "\n### EXEC-01 — Выполнение\n"
            "Время: 2026-11-14T13:00:00+03:00\n"
            "Исполнитель: Devin\n"
            "Модель: Gemini 4 Carbon Medium\n"
            "Изменения: реализована возможность.\n"
            "Файлы:\n- src/feature.py\n\n"
            "### CHECK-01 — Проверка\n"
            "Время: 2026-11-14T13:05:00+03:00\n"
            "Исполнитель: Devin\n"
            "Модель: Gemini 4 Carbon Medium\n"
            "Результат: passed\n"
            "Критерии: AC-01\n"
            "Команда: `playbook.py checks run DEV-09`\n"
            "Среда: Python временный стенд без Git.\n"
            "Факт: проверены обязательства.\n"
            "Доказательства: журнал checks.\n"
            "Ограничения: trust_mode=local.\n\n"
            "### HANDOFF-01 — Передача\n"
            "Время: 2026-11-14T13:06:00+03:00\n"
            "Исполнитель: Devin\n"
            "Модель: Gemini 4 Carbon Medium\n"
            "Выполнение: EXEC-01\n"
            "Проверки: CHECK-01\n"
            "Состояние: in_review; ожидание независимого аудита.\n"
            "Фокус аудита: покрытие и ручное подтверждение.\n",
            encoding="utf-8",
        )
        self.assertEqual(self.cli("index")[0], 0)
        # Без подтверждения manual-ux передача отклоняется единым расчётом checks_gate.
        self.assertEqual(
            self.cli("lifecycle", "handoff", "DEV-09", "--summary", "HANDOFF-01",
                     "--actor", "Devin", "--model", "Gemini 4 Carbon Medium")[0],
            1,
        )

        # Подтверждаем ручное обязательство, но затем добавляем новое поведение в критерии:
        # прежние результаты scenario-check и manual-ux устаревают, старый old-suite не доказывает новое поведение!
        self.assertEqual(
            self.cli("checks", "confirm", "DEV-09", "--check", "manual-ux",
                     "--who", "Инженер", "--statement", "Подтверждено v1",
                     "--observation", "Экран проверен")[0],
            0,
        )
        card.write_text(
            card.read_text(encoding="utf-8").replace(
                "Пользователь видит подтверждение заказа.",
                "Пользователь видит подтверждение заказа и код отмены.",
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.cli("checks", "release-ready", "DEV-09")[0], 1)
        self.assertEqual(
            self.cli("lifecycle", "handoff", "DEV-09", "--summary", "HANDOFF-01",
                     "--actor", "Devin", "--model", "Gemini 4 Carbon Medium")[0],
            1,
        )

        # Выполняем только устаревший scenario-check (old-suite reused!) и подтверждаем manual-ux для новых требований.
        before_old = self.count("old_suite")
        self.assertEqual(self.cli("checks", "run", "DEV-09", "--scope", "task", "--mode", "stale")[0], 1)
        self.assertEqual(self.count("old_suite"), before_old)
        # plan после прогона: оценка reuse проверок с task_projection
        # требует проекций карточки и политики (регрессия PB-064).
        self.assertEqual(self.cli("checks", "plan", "DEV-09")[0], 0)
        self.assertEqual(
            self.cli("checks", "confirm", "DEV-09", "--check", "manual-ux",
                     "--who", "Инженер", "--statement", "Подтверждено с кодом отмены",
                     "--observation", "Проверены оба состояния")[0],
            0,
        )
        code, rr_out, rr_err = self.cli("checks", "release-ready", "DEV-09")
        self.assertEqual(code, 0, rr_out + rr_err)

        # Передача, audit-ready, независимая приёмка и закрытие работают согласованно и архивируют provenance.
        code, _, err = self.cli(
            "lifecycle", "handoff", "DEV-09", "--summary", "HANDOFF-01",
            "--actor", "Devin", "--model", "Gemini 4 Carbon Medium",
        )
        self.assertEqual(code, 0, err)
        self.assertIn("- [x] AC-01:", card.read_text(encoding="utf-8"))

        code, ar_out, err = self.cli("verify", "DEV-09", "--audit-ready")
        self.assertEqual(code, 0, err)
        self.assertIn("audit-ready:", ar_out)

        card.write_text(
            card.read_text(encoding="utf-8")
            + "\n### AUDIT-01 — Аудит\n"
            "Время: 2026-11-14T13:20:00+03:00\n"
            "Исполнитель: Codex\n"
            "Модель: GPT-6.1 Sol\n"
            "Передача: HANDOFF-01\n"
            "Результат: accepted\n"
            "Критерии: AC-01\n"
            "Независимо проверено: воспроизведены checks status и release-ready.\n"
            "Доказательства: отчёт покрытия задачи.\n"
            "Ограничения: синтетический стенд PB-064.\n",
            encoding="utf-8",
        )
        code, _, err = self.cli(
            "lifecycle", "accept", "DEV-09", "--result", "accepted",
            "--kind", "acceptance", "--role", "auditor",
            "--actor", "Codex", "--model", "GPT-6.1 Sol",
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.cli("close", "DEV-09", "--dry-run")[0], 0)
        code, _, err = self.cli("close", "DEV-09")
        self.assertEqual(code, 0, err)

        archived_reports = list(
            (self.root / "docs/archive/tasks/DEV-09-evidence/work/checks/handoffs/DEV-09").rglob("report.json")
        )
        self.assertEqual(len(archived_reports), 1)
        archived_doc = json.loads(archived_reports[0].read_text(encoding="utf-8"))
        self.assertEqual(archived_doc["kind"], "checks-coverage-report")
        self.assertEqual(archived_doc["policy_id"], "lifecycle-policy")
        self.assertIn("manual-ux", archived_doc["covered"])
        self.assertIn("scenario-check", archived_doc["covered"])
        self.assertEqual(self.cli("check", "--ready")[0], 0)

    def test_coverage_report_binds_terminal_evidence(self):
        """A-02: checks-coverage-report привязан к терминальным
        результатам — новый прогон после смены входа требует новой
        передачи; законный reuse свежесть сохраняет."""
        self.write("src/feature.py", "MODE = 'v1'\n")
        card = self.create_card("DEV-10")
        policy = {
            "schema_version": 1,
            "policy_id": "freshness-policy",
            "revision": 1,
            "project_id": "no-git-shop",
            "trust_mode": "local",
            "approved_basis": _basis(),
            "subsystems": [{"id": "core", "title": "Ядро"}],
            "modules": [
                {"id": "code", "title": "Код", "subsystem_id": "core",
                 "paths": ["src/**"]},
                {"id": "meta", "title": "Мета", "subsystem_id": "core",
                 "paths": ["ai-dev-playbook/**", "docs/**",
                           "AGENTS.md", "CHANGELOG.md"]},
            ],
            "checks": [{
                "id": "feat-check", "title": "Проверка функции",
                "purpose": "Покрытие кода", "kind": "command",
                "modules": ["code"],
                "inputs": [{"type": "task_projection"}],
                "argv": self.command_argv("feat"), "timeout_seconds": 20,
            }],
            "dependencies": [],
            "excludes": [],
            "environment": {"toolchain": [
                {"id": "python", "version": platform.python_version()}],
                "external": []},
            "fallback": {
                "expansion_order": ["module", "subsystem", "project"],
                "approved_commands": [], "unknown_scope": "project",
                "limits": {"max_seconds": 120, "max_bytes": 10_000_000}},
            "test_catalog": _stub_catalog(),
        }
        self.write("checks-policy.json",
                   json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("setup", "apply")[0], 0)
        self.assertEqual(
            self.cli("checks", "run", "DEV-10", "--scope", "project",
                     "--mode", "force")[0], 0)
        card.write_text(
            card.read_text(encoding="utf-8").replace(
                "Status: in_progress", "Status: in_review", 1)
            + "\n### EXEC-01 — Выполнение\n"
            "Время: 2026-11-15T08:00:00+03:00\n"
            "Исполнитель: Devin\n"
            "Модель: Gemini 4 Carbon Medium\n"
            "Изменения: реализована функция.\n"
            "Файлы:\n- src/feature.py\n\n"
            "### CHECK-01 — Проверка\n"
            "Время: 2026-11-15T08:05:00+03:00\n"
            "Исполнитель: Devin\n"
            "Модель: Gemini 4 Carbon Medium\n"
            "Результат: passed\n"
            "Критерии: AC-01\n"
            "Команда: `playbook.py checks run DEV-10`\n"
            "Среда: Python временный стенд без Git.\n"
            "Факт: проверены обязательства.\n"
            "Доказательства: журнал checks.\n"
            "Ограничения: trust_mode=local.\n\n"
            "### HANDOFF-01 — Передача\n"
            "Время: 2026-11-15T08:06:00+03:00\n"
            "Исполнитель: Devin\n"
            "Модель: Gemini 4 Carbon Medium\n"
            "Выполнение: EXEC-01\n"
            "Проверки: CHECK-01\n"
            "Состояние: in_review; ожидание независимого аудита.\n"
            "Фокус аудита: свежесть покрытия.\n",
            encoding="utf-8",
        )
        self.assertEqual(self.cli("index")[0], 0)
        code, _, err = self.cli(
            "lifecycle", "handoff", "DEV-10", "--summary",
            "HANDOFF-01", "--actor", "Devin",
            "--model", "Gemini 4 Carbon Medium")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("verify", "DEV-10", "--audit-ready")
        self.assertEqual(code, 0, err)

        # Законный reuse: повторный прогон без изменений пишет
        # -reused записи на те же терминальные результаты — передача
        # остаётся свежей.
        self.assertEqual(
            self.cli("checks", "run", "DEV-10", "--scope", "project",
                     "--mode", "stale")[0], 0)
        code, _, err = self.cli("verify", "DEV-10", "--audit-ready")
        self.assertEqual(code, 0, err)

        # Изменение значимого входа и новый прогон делают прежний
        # coverage-report устаревшим: audit-ready отказывает до новой
        # передачи (аудит A-02, R-12/R-15).
        self.write("src/feature.py", "MODE = 'v2'\n")
        self.assertEqual(
            self.cli("checks", "run", "DEV-10", "--scope", "project",
                     "--mode", "stale")[0], 0)
        self.assertEqual(self.count("feat"), 2)
        code, _, err = self.cli("verify", "DEV-10", "--audit-ready")
        self.assertEqual(code, 1, err)

        # Новая запись CHECK привязывается к свежему отчёту; передача
        # фиксирует новые доказательства — аудит снова готов.
        card.write_text(
            card.read_text(encoding="utf-8")
            + "\n### CHECK-02 — Проверка\n"
            "Время: 2026-11-15T08:10:00+03:00\n"
            "Исполнитель: Devin\n"
            "Модель: Gemini 4 Carbon Medium\n"
            "Результат: passed\n"
            "Критерии: AC-01\n"
            "Команда: `playbook.py checks run DEV-10`\n"
            "Среда: Python временный стенд без Git.\n"
            "Факт: входы изменены, проверка повторена.\n"
            "Доказательства: журнал checks.\n"
            "Ограничения: trust_mode=local.\n\n"
            "### HANDOFF-02 — Передача\n"
            "Время: 2026-11-15T08:11:00+03:00\n"
            "Исполнитель: Devin\n"
            "Модель: Gemini 4 Carbon Medium\n"
            "Выполнение: EXEC-01\n"
            "Проверки: CHECK-02\n"
            "Состояние: in_review; ожидание независимого аудита.\n"
            "Фокус аудита: свежесть покрытия после повторного прогона.\n",
            encoding="utf-8",
        )
        self.assertEqual(self.cli("index")[0], 0)
        code, _, err = self.cli(
            "lifecycle", "handoff", "DEV-10", "--summary",
            "HANDOFF-02", "--actor", "Devin",
            "--model", "Gemini 4 Carbon Medium")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("verify", "DEV-10", "--audit-ready")
        self.assertEqual(code, 0, err)

    def test_coverage_report_rejects_post_report_rerun_of_not_applicable(self):
        """A-04: переход not_applicable → новый прогон после отчёта
        делает передачу устаревшей; законный reuse старых доказательств
        свежесть сохраняет."""
        self.write("src/feature.py", "MODE = 'v1'\n")
        self.write("other/service.txt", "v1")
        card = self.create_card("DEV-11")
        policy = {
            "schema_version": 1,
            "policy_id": "growth-policy",
            "revision": 1,
            "project_id": "no-git-shop",
            "trust_mode": "local",
            "approved_basis": _basis(),
            "subsystems": [{"id": "core", "title": "Ядро"}],
            "modules": [
                {"id": "code", "title": "Код", "subsystem_id": "core",
                 "paths": ["src/**"]},
                {"id": "other", "title": "Прочее", "subsystem_id": "core",
                 "paths": ["other/**"]},
                {"id": "meta", "title": "Мета", "subsystem_id": "core",
                 "paths": ["ai-dev-playbook/**", "docs/**",
                           "AGENTS.md", "CHANGELOG.md"]},
            ],
            "checks": [
                {
                    "id": "feat-check", "title": "Проверка функции",
                    "purpose": "Покрытие кода", "kind": "command",
                    "modules": ["code"],
                    "inputs": [{"type": "task_projection"}],
                    "argv": self.command_argv("feat"),
                    "timeout_seconds": 20,
                },
                {
                    "id": "other-suite", "title": "Набор прочих областей",
                    "purpose": "Вне задачи", "kind": "command",
                    "modules": ["other"],
                    "argv": self.command_argv("other"),
                    "timeout_seconds": 20,
                },
            ],
            "dependencies": [],
            "excludes": [],
            "environment": {"toolchain": [
                {"id": "python", "version": platform.python_version()}],
                "external": []},
            "fallback": {
                "expansion_order": ["module", "subsystem", "project"],
                "approved_commands": [], "unknown_scope": "project",
                "limits": {"max_seconds": 120, "max_bytes": 10_000_000}},
            "test_catalog": _stub_catalog(),
        }
        self.write("checks-policy.json",
                   json.dumps(policy, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual(self.cli("setup", "apply")[0], 0)
        self.assertEqual(
            self.cli("checks", "run", "DEV-11", "--scope", "project",
                     "--mode", "force")[0], 0)
        self.assertEqual(self.counts("feat", "other"),
                         {"feat": 1, "other": 1})
        card.write_text(
            card.read_text(encoding="utf-8").replace(
                "Status: in_progress", "Status: in_review", 1)
            + "\n### EXEC-01 — Выполнение\n"
            "Время: 2026-11-15T09:00:00+03:00\n"
            "Исполнитель: Devin\n"
            "Модель: Gemini 4 Carbon Medium\n"
            "Изменения: реализована функция.\n"
            "Файлы:\n- src/feature.py\n\n"
            "### CHECK-01 — Проверка\n"
            "Время: 2026-11-15T09:05:00+03:00\n"
            "Исполнитель: Devin\n"
            "Модель: Gemini 4 Carbon Medium\n"
            "Результат: passed\n"
            "Критерии: AC-01\n"
            "Команда: `playbook.py checks run DEV-11`\n"
            "Среда: Python временный стенд без Git.\n"
            "Факт: обязательства проверены.\n"
            "Доказательства: журнал checks.\n"
            "Ограничения: trust_mode=local.\n\n"
            "### HANDOFF-01 — Передача\n"
            "Время: 2026-11-15T09:06:00+03:00\n"
            "Исполнитель: Devin\n"
            "Модель: Gemini 4 Carbon Medium\n"
            "Выполнение: EXEC-01\n"
            "Проверки: CHECK-01\n"
            "Состояние: in_review; ожидание независимого аудита.\n"
            "Фокус аудита: свежесть покрытия.\n",
            encoding="utf-8",
        )
        self.assertEqual(self.cli("index")[0], 0)
        code, _, err = self.cli(
            "lifecycle", "handoff", "DEV-11", "--summary",
            "HANDOFF-01", "--actor", "Devin",
            "--model", "Gemini 4 Carbon Medium")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("verify", "DEV-11", "--audit-ready")
        self.assertEqual(code, 0, err)

        # Изменение входа ранее not_applicable-проверки и её новый
        # прогон после отчёта: audit-ready обязан отказать до новой
        # передачи (аудит A-04, R-12/R-15).
        self.write("other/service.txt", "v2")
        self.assertEqual(
            self.cli("checks", "run", "DEV-11", "--scope", "project",
                     "--mode", "stale")[0], 0)
        self.assertEqual(self.count("other"), 2)
        self.assertEqual(self.count("feat"), 1)
        code, _, err = self.cli("verify", "DEV-11", "--audit-ready")
        self.assertEqual(code, 1, err)

        # Повтор без изменений — законный reuse: other-suite не
        # исполняется повторно, передача остаётся устаревшей до нового
        # CHECK/HANDOFF.
        self.assertEqual(
            self.cli("checks", "run", "DEV-11", "--scope", "project",
                     "--mode", "stale")[0], 0)
        self.assertEqual(self.count("other"), 2)
        code, _, err = self.cli("verify", "DEV-11", "--audit-ready")
        self.assertEqual(code, 1, err)

        # Новая запись CHECK/HANDOFF фиксирует новые доказательства —
        # аудит снова готов.
        card.write_text(
            card.read_text(encoding="utf-8")
            + "\n### CHECK-02 — Проверка\n"
            "Время: 2026-11-15T09:10:00+03:00\n"
            "Исполнитель: Devin\n"
            "Модель: Gemini 4 Carbon Medium\n"
            "Результат: passed\n"
            "Критерии: AC-01\n"
            "Команда: `playbook.py checks run DEV-11`\n"
            "Среда: Python временный стенд без Git.\n"
            "Факт: входы изменены, проверка повторена.\n"
            "Доказательства: журнал checks.\n"
            "Ограничения: trust_mode=local.\n\n"
            "### HANDOFF-02 — Передача\n"
            "Время: 2026-11-15T09:11:00+03:00\n"
            "Исполнитель: Devin\n"
            "Модель: Gemini 4 Carbon Medium\n"
            "Выполнение: EXEC-01\n"
            "Проверки: CHECK-02\n"
            "Состояние: in_review; ожидание независимого аудита.\n"
            "Фокус аудита: свежесть покрытия после повторного прогона.\n",
            encoding="utf-8",
        )
        self.assertEqual(self.cli("index")[0], 0)
        code, _, err = self.cli(
            "lifecycle", "handoff", "DEV-11", "--summary",
            "HANDOFF-02", "--actor", "Devin",
            "--model", "Gemini 4 Carbon Medium")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("verify", "DEV-11", "--audit-ready")
        self.assertEqual(code, 0, err)

    def test_ac10_legacy_intact_preview_readonly_unsupported_and_restore(self):
        """AC-10: legacy без checks_policy неизменен, preview не исполняет код, неподдержанные версии и прерывание безопасны."""
        # 1. До opt-in проект работает по прежнему legacy-контракту verify.
        proj_path = self.root / "ai-dev-playbook" / "project.json"
        proj = json.loads(proj_path.read_text(encoding="utf-8"))
        proj["verification"] = {
            "inputs": ["AGENTS.md"],
            "checks": [{
                "id": "legacy-smoke",
                "argv": self.command_argv("legacy"),
                "timeout_seconds": 20,
            }],
        }
        proj_path.write_text(json.dumps(proj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.create_card("DEV-10")
        self.assertEqual(self.cli("verify", "DEV-10")[0], 0)
        self.assertEqual(self.count("legacy"), 1)

        # 2. setup preview строго read-only и не импортирует/не исполняет тестовые файлы с побочным эффектом.
        sentinel = self.base / "preview-side-effect.flag"
        self.write(
            "tests/test_danger.py",
            f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n"
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_x(self):\n        '''Описание безопасного теста.'''\n        pass\n",
        )
        before_snapshot = {
            p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(self.root.rglob("*")) if p.is_file()
        }
        code, _, err = self.cli("setup", "preview")
        self.assertEqual(code, 0, err)
        self.assertFalse(sentinel.exists())
        after_snapshot = {
            p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(self.root.rglob("*")) if p.is_file()
        }
        self.assertEqual(before_snapshot, after_snapshot)
        (self.root / "tests/test_danger.py").unlink()

        # 3. Неподдержанная schema_version политики и TODO-approved_basis отклоняются без записи project.json.
        unsupported = {
            "schema_version": 99,
            "policy_id": "bad",
            "revision": 1,
            "project_id": "no-git-shop",
            "trust_mode": "local",
            "approved_basis": _basis(),
            "subsystems": [{"id": "s", "title": "s"}],
            "modules": [{"id": "m", "title": "m", "subsystem_id": "s", "paths": ["AGENTS.md"]}],
            "checks": [],
            "dependencies": [],
            "excludes": [],
            "environment": {"toolchain": [], "external": []},
            "fallback": {"expansion_order": ["module", "subsystem", "project"],
                         "approved_commands": [],
                         "unknown_scope": "project",
                         "limits": {"max_seconds": 60, "max_bytes": 100000}},
            "test_catalog": _stub_catalog(),
        }
        self.write("checks-policy.json", json.dumps(unsupported) + "\n")
        self.assertEqual(self.cli("setup", "apply")[0], 1)
        self.assertNotIn("checks_policy", json.loads(proj_path.read_text(encoding="utf-8")))

        # 4. Прерывание setup apply на final_checks оставляет pending и восстанавливается через lifecycle restore.
        valid_policy = dict(unsupported, schema_version=1, checks=[{
            "id": "legacy-preserved",
            "title": "Сохранённая обязательная проверка",
            "purpose": "Миграция обязательств",
            "kind": "command",
            "modules": ["m"],
            "argv": self.command_argv("legacy"),
            "timeout_seconds": 20,
        }])
        self.write("checks-policy.json", json.dumps(valid_policy, ensure_ascii=False, indent=2) + "\n")
        pre_proj_bytes = proj_path.read_bytes()
        orig_linked = playbook._checks_policy_linked
        calls = {"n": 0}

        def interrupt_on_final(root_arg):
            calls["n"] += 1
            orig_linked(root_arg)
            if calls["n"] >= 2:
                raise KeyboardInterrupt("simulated crash during setup apply")

        with mock.patch.object(playbook, "_checks_policy_linked", side_effect=interrupt_on_final):
            with self.assertRaises(KeyboardInterrupt):
                self.cli("setup", "apply")
        self.assertIsNotNone(file_operations.pending_info(self.root))
        self.assertEqual(self.cli("check", "--ready")[0], 1)
        self.assertEqual(self.cli("lifecycle", "restore")[0], 0)
        self.assertIsNone(file_operations.pending_info(self.root))
        self.assertEqual(proj_path.read_bytes(), pre_proj_bytes)

        # Повторное штатное применение проходит, сохраняя раздел verification.
        self.assertEqual(self.cli("setup", "apply")[0], 0)
        merged_proj = json.loads(proj_path.read_text(encoding="utf-8"))
        self.assertIn("verification", merged_proj)
        self.assertEqual(merged_proj["checks_policy"], "checks-policy.json")


class Audit009BaselineFailureE2ETest(NoGitChecksE2ECase):
    """A-01 повторного аудита PB-073: непокрытое обязательство не
    исчезает при уже актуальном baseline — passed → failed →
    task stale обязан исполнить проверку снова, а gate — закрыт."""

    def setUp(self):
        super().setUp()
        self.allow = self.base / "allow"
        self.write("src/payments/pay.py", "value = 1\n")

    def _payment_argv(self, mode):
        """Команда payment со счётчиком попыток; поведение по режиму.

        ``flag`` — exit 0 при наличии флага ``allow``; ``timeout`` —
        то же, но без флага спит до таймаута; ``mutate`` — вторая
        попытка портит входы и завершается кодом 0 (blocked).
        """
        base, counters = repr(str(self.base)), repr(str(self.counters_dir))
        bump = (f"c=Path({counters})/'payment.count'; "
                "n=int(c.read_text()) if c.exists() else 0; n+=1; "
                "c.write_text(str(n)); ")
        if mode == "flag":
            tail = (f"sys.exit(0 if (Path({base})/'allow').exists() "
                    "else 1)")
        elif mode == "timeout":
            tail = (f"sys.exit(0 if (Path({base})/'allow').exists() "
                    "else (time.sleep(30), 1)[1])")
        elif mode == "mutate":
            tail = ("Path('src/payments/junk.tmp').write_text('x') "
                    "if n == 2 else None; sys.exit(0)")
        else:
            raise AssertionError(mode)
        imports = "import sys, time; from pathlib import Path; " \
            if mode == "timeout" else \
            "import sys; from pathlib import Path; "
        return [sys.executable, "-c", imports + bump + tail]

    def _payment_policy(self, mode="flag", extra_checks=(),
                        timeout_seconds=10):
        policy = make_policy()
        check = {"id": "payment", "title": "Payment",
                 "purpose": "Must pass", "kind": "command",
                 "modules": ["payments"],
                 "argv": self._payment_argv(mode),
                 "timeout_seconds": timeout_seconds}
        policy["checks"] = [check, *extra_checks]
        return policy

    def _install_policy(self, policy):
        self.write("checks-policy.json",
                   json.dumps(policy, ensure_ascii=False, indent=2)
                   + "\n")
        self.assertEqual(self.cli("setup", "apply")[0], 0)

    def _gate(self, task_id):
        card = self.root / "docs" / "tasks" / f"{task_id}.md"
        meta = json.loads(
            card.with_suffix(".meta.json").read_text(encoding="utf-8"))
        return checks_gate.evaluate(
            self.root, checks_gate.load_policy(self.root),
            task_id=task_id,
            task_content=card.read_text(encoding="utf-8"),
            task_meta=meta)

    def _project_pass_then_fail(self):
        """passed project → failed project: baseline остаётся на
        покрытом состоянии, последний исход — failed."""
        self.allow.write_text("1\n")
        code, _, err = self.cli("checks", "run", "DEV-01",
                                "--scope", "project", "--mode", "force")
        self.assertEqual(code, 0, err)
        self.allow.unlink()
        code, _, err = self.cli("checks", "run", "DEV-01",
                                "--scope", "project", "--mode", "force")
        self.assertEqual(code, 1, err)
        self.assertEqual(self.count("payment"), 2)

    def test_failed_after_baseline_reopens_task_obligation(self):
        """passed → failed при неизменных входах: task stale снова
        исполняет payment, gate закрыт; новый passed восстанавливает."""
        self.create_card("DEV-01")
        self._install_policy(self._payment_policy())
        self._project_pass_then_fail()

        code, out, err = self.cli("checks", "run", "DEV-01",
                                  "--scope", "task", "--mode", "stale")
        self.assertEqual(code, 1, err)
        self.assertEqual(self.count("payment"), 3)
        self.assertIn("payment: failed", out)
        gate = self._gate("DEV-01")
        self.assertFalse(gate["ok"])
        self.assertIn("payment", gate["uncovered"])
        self.assertNotIn("payment", gate["not_applicable"])

        # Исправление окружения: новый успешный прогон покрывает
        # обязательство, gate открывается.
        self.allow.write_text("1\n")
        code, _, err = self.cli("checks", "run", "DEV-01",
                                "--scope", "task", "--mode", "stale")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.count("payment"), 4)
        gate = self._gate("DEV-01")
        self.assertTrue(gate["ok"], gate)

    def test_timeout_after_baseline_reopens_task_obligation(self):
        """timeout-прогон при актуальном baseline: обязательство
        остаётся и повторно исполняется."""
        self.create_card("DEV-01")
        self._install_policy(
            self._payment_policy(mode="timeout", timeout_seconds=2))
        self._project_pass_then_fail()

        code, _, err = self.cli("checks", "run", "DEV-01",
                                "--scope", "task", "--mode", "stale")
        self.assertEqual(code, 1, err)
        self.assertEqual(self.count("payment"), 3)
        gate = self._gate("DEV-01")
        self.assertFalse(gate["ok"])
        self.assertIn("payment", gate["uncovered"])

    def test_blocked_after_baseline_reopens_task_obligation(self):
        """blocked-прогон (входы испорчены и возвращены): обязательство
        не теряется даже когда diff пуст."""
        self.create_card("DEV-01")
        self._install_policy(self._payment_policy(mode="mutate"))
        self.allow.write_text("1\n")
        code, _, err = self.cli("checks", "run", "DEV-01",
                                "--scope", "project", "--mode", "force")
        self.assertEqual(code, 0, err)
        # Вторая попытка портит входы → blocked; baseline не двигается.
        code, _, err = self.cli("checks", "run", "DEV-01",
                                "--scope", "project", "--mode", "force")
        self.assertEqual(code, 1, err)
        self.assertEqual(self.count("payment"), 2)
        junk = self.root / "src/payments/junk.tmp"
        self.assertTrue(junk.is_file())
        junk.unlink()
        # Входы вернулись к baseline: diff пуст, но последний исход —
        # blocked, обязательство обязано исполниться снова.
        code, _, err = self.cli("checks", "run", "DEV-01",
                                "--scope", "task", "--mode", "stale")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.count("payment"), 3)
        gate = self._gate("DEV-01")
        self.assertTrue(gate["ok"], gate)

    def test_manual_obligation_survives_until_confirm(self):
        """Ручная проверка без подтверждения держит run и gate
        закрытыми; confirm восстанавливает покрытие."""
        self.create_card("DEV-01")
        review = {"id": "review", "title": "Ревью", "purpose": "Глазами",
                  "kind": "manual", "modules": ["payments"],
                  "scenario": "Проверить сценарий оплаты глазами"}
        self._install_policy(self._payment_policy(extra_checks=(review,)))
        self.allow.write_text("1\n")

        code, _, err = self.cli("checks", "run", "DEV-01",
                                "--scope", "project", "--mode", "force")
        self.assertEqual(code, 1, err)  # review: not_run
        code, _, err = self.cli("checks", "run", "DEV-01",
                                "--scope", "task", "--mode", "stale")
        self.assertEqual(code, 1, err)
        gate = self._gate("DEV-01")
        self.assertFalse(gate["ok"])
        self.assertIn("review", gate["manual"])

        code, _, err = self.cli(
            "checks", "confirm", "DEV-01", "--check", "review",
            "--who", "Инженер", "--statement", "Сценарий проверен",
            "--observation", "Оплата работает штатно")
        self.assertEqual(code, 0, err)
        code, _, err = self.cli("checks", "run", "DEV-01",
                                "--scope", "task", "--mode", "stale")
        self.assertEqual(code, 0, err)
        gate = self._gate("DEV-01")
        self.assertTrue(gate["ok"], gate)

    def test_other_task_sees_failed_obligation_and_reuse_recovers(self):
        """Запуск другой задачи не скрывает failed: DEV-02 stale тоже
        исполняет payment; последующий passed закрывает обе задачи
        законным cross-task reuse."""
        self.create_card("DEV-01")
        self.create_card("DEV-02", title="Вторая задача")
        self._install_policy(self._payment_policy())
        self._project_pass_then_fail()

        code, _, err = self.cli("checks", "run", "DEV-02",
                                "--scope", "task", "--mode", "stale")
        self.assertEqual(code, 1, err)
        self.assertEqual(self.count("payment"), 3)
        self.assertFalse(self._gate("DEV-01")["ok"])
        self.assertFalse(self._gate("DEV-02")["ok"])

        self.allow.write_text("1\n")
        code, _, err = self.cli("checks", "run", "DEV-02",
                                "--scope", "task", "--mode", "stale")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.count("payment"), 4)
        # Прошёл один и тот же check на тех же входах — покрытие
        # законно переиспользуется задачей DEV-01.
        self.assertTrue(self._gate("DEV-01")["ok"])
        self.assertTrue(self._gate("DEV-02")["ok"])


if __name__ == "__main__":
    unittest.main()
