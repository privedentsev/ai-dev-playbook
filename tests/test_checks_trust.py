"""PB-061: доверенный контур — дом verifier, квитанции, изоляция, gate."""
import contextlib
import errno
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "ai-dev-playbook" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import checks_contract
import checks_results
import checks_select
import checks_trust
import checks_unittest
import checks_verifier
import verification_policy
from test_checks_run import run_policy, run_project, TASK_MD
from test_checks_select import make_policy, Project


def req(condition, message):
    if not condition:
        raise ValueError(message)


def trust_policy(**kw):
    """Политика protected с тремя unittest-проверками и командой."""
    policy = run_policy(trust_mode="protected", **kw)
    policy["checks"] = [c for c in policy["checks"]
                      if c["kind"] != "manual"]
    return policy


def make_home(case):
    """Verifier home вне проекта (временный каталог)."""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    return Path(tmp.name) / "vh"


def init_home(case, policy, project=None):
    """Проект, дом и одобренная политика; вернуть (proj, home)."""
    proj = project or run_project(case)
    home = make_home(case)
    checks_trust.init_home(home, project_id=policy["project_id"],
                           actor="Devin", basis_detail="fixture")
    proj.write("checks-policy.json",
               json.dumps(policy, ensure_ascii=False))
    store = checks_trust.load_home(home)
    checks_trust.approve_policy(store, policy, actor="Devin")
    return proj, home


def verifier_run(proj, home, **kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        return checks_verifier.run(proj.root, home, emit=out.write, **kw)


def read_receipt(home, receipt_id):
    path = home / checks_trust.RECEIPTS_DIR / f"{receipt_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def command_policy(argv, **kw):
    """Protected-политика с единственной command-проверкой.

    Command-результаты наблюдаемы родителем (spawn+exit) — единственный
    вид, который verifier может подтверждать (A-01/AUDIT-02).
    """
    policy = trust_policy(**kw)
    policy["checks"] = [
        {"id": "probe", "title": "t", "purpose": "p",
         "kind": "command", "argv": argv, "timeout_seconds": 30,
         "inputs": ["src"], "applicability": "always"}]
    return policy


def forge(receipt, mutate=None):
    """Копия квитанции, подписанная чужим ключом."""
    import hashlib
    import hmac
    doc = json.loads(json.dumps(receipt))
    if mutate:
        mutate(doc)
    payload = {key: value for key, value in doc.items()
               if key != "signature"}
    doc["signature"]["value"] = hmac.new(
        b"attacker", json.dumps(payload, sort_keys=True,
                                ensure_ascii=False,
                                separators=(",", ":")).encode(),
        hashlib.sha256).hexdigest()
    return doc


class HomeTest(unittest.TestCase):
    """Дом, ключи, привязка проекта, probe (AC-02/03)."""

    def setUp(self):
        self.policy = trust_policy()
        self.proj = run_project(self)

    def test_init_and_binding(self):
        home = make_home(self)
        doc = checks_trust.init_home(
            home, project_id="shop", actor="Devin",
            basis_detail="fixture")
        self.assertEqual(doc["engine"], checks_trust.ENGINE)
        for name in (checks_trust.VERIFIER_DOC,
                     checks_trust.REVOCATIONS_DOC,
                     checks_trust.JOURNAL_FILE):
            self.assertTrue((home / name).is_file(), name)
        store = checks_trust.load_home(home)
        self.assertEqual(store["doc"]["project_id"], "shop")
        with self.assertRaisesRegex(ValueError, "привязан"):
            checks_trust.load_home(home, project_id="other")
        with self.assertRaisesRegex(ValueError, "инициализирован"):
            checks_trust.init_home(home, project_id="shop",
                                   actor="Devin", basis_detail="x")

    def test_probe_without_home_is_local(self):
        report = checks_trust.probe(self.proj.root, None)
        self.assertEqual(report["capability"], "local")
        self.assertFalse(report["configured"])

    def test_probe_inside_project_is_local(self):
        home = self.proj.root / ".vh"
        checks_trust.init_home(home, project_id="shop", actor="Devin",
                               basis_detail="fixture")
        report = checks_trust.probe(self.proj.root, home)
        self.assertEqual(report["capability"], "local")
        self.assertTrue(report["inside_project"])

    def test_probe_same_uid_is_honest_local(self):
        home = make_home(self)
        checks_trust.init_home(home, project_id="shop", actor="Devin",
                               basis_detail="fixture")
        report = checks_trust.probe(self.proj.root, home)
        # Тот же UID: запись в дом возможна — защищённость не заявляется.
        self.assertEqual(report["capability"], "local")
        self.assertTrue(report["agent_can_write"])
        self.assertEqual(report["boundary"], "none")


class ApproveTest(unittest.TestCase):
    """Одобрение политики через канал; расширение ≠ новые команды (AC-02)."""

    def setUp(self):
        self.policy = trust_policy()
        self.home = make_home(self)
        checks_trust.init_home(self.home, project_id="shop",
                               actor="Devin", basis_detail="fixture")

    def approve(self, policy, **kw):
        store = checks_trust.load_home(self.home)
        return checks_trust.approve_policy(store, policy, actor="Devin",
                                           **kw)

    def test_initial_and_require(self):
        store = checks_trust.load_home(self.home)
        with self.assertRaisesRegex(ValueError, "не одобрена"):
            checks_trust.require_approved(store)
        verdict = self.approve(self.policy)
        self.assertEqual(verdict["verdict"], "initial")
        approved = checks_trust.require_approved(
            checks_trust.load_home(self.home))
        self.assertEqual(approved["policy"]["policy_id"],
                         self.policy["policy_id"])

    def test_extension_without_argv_is_routine(self):
        self.approve(self.policy)
        new = json.loads(json.dumps(self.policy))
        new["revision"] = 2
        new["checks"].append(
            {"id": "docs-review", "title": "t", "purpose": "p",
             "kind": "manual", "scenario": "сценарий"})
        verdict = self.approve(new)
        self.assertEqual(verdict["verdict"], "extension")

    def test_new_argv_is_weakening(self):
        """Обычное расширение не разрешает новые недоверенные команды."""
        self.approve(self.policy)
        new = json.loads(json.dumps(self.policy))
        new["revision"] = 2
        new["checks"].append(
            {"id": "evil", "title": "t", "purpose": "p", "kind": "command",
             "argv": ["curl", "https://evil.example"], "timeout_seconds": 5})
        with self.assertRaisesRegex(ValueError, "ослабляет"):
            self.approve(new)
        # Явное решение инженера — отдельный акт одобрения.
        verdict = self.approve(new, reapprove=True)
        self.assertEqual(verdict["verdict"], "weakening")

    def test_removed_check_is_weakening(self):
        self.approve(self.policy)
        new = json.loads(json.dumps(self.policy))
        new["revision"] = 2
        new["checks"] = new["checks"][:-1]
        with self.assertRaisesRegex(ValueError, "ослабляет"):
            self.approve(new)

    def test_revoked_policy_digest_refused(self):
        self.approve(self.policy)
        digest = verification_policy.fingerprint(self.policy)
        store = checks_trust.load_home(self.home)
        checks_trust.revoke(store, policy_digest=digest, actor="Devin")
        with self.assertRaisesRegex(ValueError, "отозван"):
            checks_trust.require_approved(
                checks_trust.load_home(self.home))
        with self.assertRaisesRegex(ValueError, "отозван"):
            self.approve(self.policy)


class VerifierRunTest(unittest.TestCase):
    """AC-01: одобренная политика, независимый пересчёт, изоляция, receipt."""

    def setUp(self):
        self.policy = command_policy([sys.executable, "-c", "pass"])

    def test_run_issues_valid_receipt(self):
        proj, home = init_home(self, self.policy)
        outcome = verifier_run(proj, home)
        receipt = outcome["receipt"]
        checks_contract.validate_receipt(receipt, req)
        self.assertEqual(receipt["project_id"], "shop")
        self.assertEqual(receipt["subject"]["kind"], "run")
        statuses = {item["check_id"]: item["status"]
                    for item in receipt["subject"]["results"]}
        self.assertEqual(statuses["probe"], "passed")
        # Копия квитанции опубликована в проект для consumer.
        published = proj.root / checks_trust.PROJECT_RECEIPTS_DIR / \
            f"{receipt['receipt_id']}.json"
        self.assertTrue(published.is_file())
        verdict = checks_verifier.verify(home, receipt)
        self.assertTrue(verdict["ok"], verdict["reasons"])

    def test_unapproved_policy_refused(self):
        proj = run_project(self)
        proj.write("checks-policy.json", json.dumps(self.policy))
        home = make_home(self)
        checks_trust.init_home(home, project_id="shop", actor="Devin",
                               basis_detail="fixture")
        with self.assertRaisesRegex(ValueError, "не одобрена"):
            verifier_run(proj, home)

    def test_editable_approved_is_not_authorization(self):
        """approved_basis в файле проекта не заменяет одобрение дома."""
        proj = run_project(self)
        claimed = json.loads(json.dumps(self.policy))
        claimed["approved_basis"]["detail"] = "я одобрил, честно"
        proj.write("checks-policy.json", json.dumps(claimed))
        home = make_home(self)
        checks_trust.init_home(home, project_id="shop", actor="Devin",
                               basis_detail="fixture")
        store = checks_trust.load_home(home)
        checks_trust.approve_policy(store, self.policy, actor="Devin")
        with self.assertRaisesRegex(ValueError, "не совпадает"):
            verifier_run(proj, home)

    def test_policy_edit_after_approval_refused(self):
        proj, home = init_home(self, self.policy)
        edited = json.loads(json.dumps(self.policy))
        edited["revision"] = 99
        proj.write("checks-policy.json", json.dumps(edited))
        with self.assertRaisesRegex(ValueError, "не совпадает"):
            verifier_run(proj, home)

    def test_client_results_never_signed(self):
        """Файл, подброшенный агентом, не становится квитанцией."""
        proj, home = init_home(self, self.policy)
        fake = forge({"schema_version": 1,
                      "receipt_id": "rc-fake",
                      "project_id": "shop",
                      "candidate_digest": "sha256:" + "0" * 64,
                      "policy_digest": "sha256:" + "0" * 64,
                      "policy_revision": 1,
                      "engine": dict(checks_trust.ENGINE),
                      "issuer": "verifier:fake",
                      "issued_at": "2026-11-10T00:00:00+03:00",
                      "subject": {"kind": "run",
                                  "results": [{"check_id": "x",
                                               "result_digest":
                                               "sha256:" + "1" * 64,
                                               "status": "passed"}]},
                      "signature": {"algorithm": "hmac-sha256",
                                    "key_id": "k-fake", "value": "0"}})
        verdict = checks_verifier.verify(home, fake)
        self.assertFalse(verdict["ok"])
        # Публикация чужого файла в проекте не обманывает gate.
        target = proj.root / checks_trust.PROJECT_RECEIPTS_DIR / \
            "rc-fake.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(fake), encoding="utf-8")
        verdict = checks_verifier.gate(proj.root, home, emit=lambda s: None)
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("rc-fake" in r for r in verdict["rejections"]))

    def test_snapshot_substitution_detected(self):
        """AC-07: изменение после прогона — replay отклонён."""
        proj, home = init_home(self, self.policy)
        outcome = verifier_run(proj, home)
        proj.write("src/payments/pay.py", "x = 2\n")
        verdict = checks_verifier.verify(
            home, outcome["receipt"],
            expect_candidate=checks_verifier.candidate_manifest(
                proj.root, self.policy)[1])
        self.assertFalse(verdict["ok"])
        gate = checks_verifier.gate(proj.root, home, emit=lambda s: None)
        self.assertFalse(gate["ok"])
        self.assertTrue(any("replay" in r for r in gate["rejections"]))


class ReceiptVerifyTest(unittest.TestCase):
    """AC-02: forgery/replay/cross-project/revocation/key replacement."""

    def setUp(self):
        self.policy = command_policy([sys.executable, "-c", "pass"])
        self.proj, self.home = init_home(self, self.policy)
        self.outcome = verifier_run(self.proj, self.home)
        self.receipt = self.outcome["receipt"]

    def verify(self, receipt=None, **kw):
        return checks_verifier.verify(self.home, receipt or self.receipt,
                                      **kw)

    def test_forged_field_rejected(self):
        doc = json.loads(json.dumps(self.receipt))
        doc["subject"]["results"][0]["status"] = "blocked"
        verdict = self.verify(doc)
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("подпись" in r for r in verdict["reasons"]))

    def test_attacker_signature_rejected(self):
        verdict = self.verify(forge(self.receipt))
        self.assertFalse(verdict["ok"])

    def test_unknown_key_replacement_rejected(self):
        doc = forge(self.receipt,
                    lambda d: d["signature"].update(
                        key_id="k-attacker"))
        verdict = self.verify(doc)
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("неизвестен" in r or "отозван" in r
                            for r in verdict["reasons"]))

    def test_revoked_key_rejected(self):
        store = checks_trust.load_home(self.home)
        checks_trust.revoke(store, key_id=store["active_key"],
                            actor="Devin")
        verdict = self.verify()
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("отозван" in r for r in verdict["reasons"]))

    def test_revoked_receipt_rejected(self):
        store = checks_trust.load_home(self.home)
        checks_trust.revoke(store,
                            receipt_id=self.receipt["receipt_id"],
                            actor="Devin")
        verdict = self.verify()
        self.assertFalse(verdict["ok"])

    def test_revoked_policy_rejected(self):
        store = checks_trust.load_home(self.home)
        checks_trust.revoke(
            store, policy_digest=self.receipt["policy_digest"],
            actor="Devin")
        verdict = self.verify()
        self.assertFalse(verdict["ok"])

    def test_cross_project_rejected(self):
        """Квитанция чужого проекта не принимается этим домом."""
        other_policy = command_policy(
            [sys.executable, "-c", "pass"], project_id="mall",
            policy_id="mall-checks")
        other = run_project(self)
        other_home = make_home(self)
        checks_trust.init_home(other_home, project_id="mall",
                               actor="Devin", basis_detail="fixture")
        other.write("checks-policy.json", json.dumps(other_policy))
        store = checks_trust.load_home(other_home)
        checks_trust.approve_policy(store, other_policy, actor="Devin")
        outcome = verifier_run(other, other_home)
        verdict = self.verify(outcome["receipt"])
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("cross-project" in r
                            for r in verdict["reasons"]))

    def test_foreign_home_receipt_rejected(self):
        """Квитанция другого дома того же проекта — журнал/ключ иные."""
        other_home = make_home(self)
        checks_trust.init_home(other_home, project_id="shop",
                               actor="Devin", basis_detail="fixture")
        store = checks_trust.load_home(other_home)
        checks_trust.approve_policy(store, self.policy, actor="Devin")
        outcome = verifier_run(self.proj, other_home)
        verdict = self.verify(outcome["receipt"])
        self.assertFalse(verdict["ok"])

    def test_replay_for_other_candidate_rejected(self):
        verdict = self.verify(
            expect_candidate="sha256:" + "f" * 64)
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("replay" in r for r in verdict["reasons"]))

    def test_aggregate_confirms_unchanged(self):
        result = checks_verifier.aggregate(
            self.home, self.proj.root,
            receipt_ids=[self.receipt["receipt_id"]],
            emit=lambda s: None)
        receipt = result["receipt"]
        self.assertEqual(receipt["subject"]["kind"], "aggregate")
        self.assertEqual(receipt["subject"]["source_receipts"],
                         [self.receipt["receipt_id"]])
        self.assertTrue(checks_verifier.verify(self.home, receipt)["ok"])

    def test_aggregate_skips_changed_inputs(self):
        # Изменение внутри объявленных входов: пересчёт не сходится,
        # единственная проверка пропускается — aggregate отклоняется.
        self.proj.write("src/payments/pay.py", "x = 3\n")
        with self.assertRaisesRegex(ValueError, "ни одна проверка"):
            checks_verifier.aggregate(
                self.home, self.proj.root,
                receipt_ids=[self.receipt["receipt_id"]],
                emit=lambda s: None)


class IsolationTest(unittest.TestCase):
    """AC-02/03: изоляция подпроцесса и честная граница прав."""

    def setUp(self):
        self.policy = trust_policy()

    def _run_command(self, argv):
        policy = command_policy(argv)
        proj, home = init_home(self, policy)
        outcome = verifier_run(proj, home)
        statuses = {item["check_id"]: item["status"]
                    for item in outcome["receipt"]["subject"]["results"]}
        arena = (home / checks_trust.ARENA_DIR)
        run_dir = next(arena.glob("*/work/checks/runs/*/"),
                       None)
        return outcome, statuses, run_dir

    def test_env_scrubbed_for_test_process(self):
        """Переменные доверенной стороны не доходят до подпроцесса."""
        marker = "pb061-secret-marker"
        os.environ["PLAYBOOK_TEST_SECRET"] = marker
        try:
            code = ("import os; "
                    "print('LEAKED' if "
                    "os.environ.get('PLAYBOOK_TEST_SECRET') else 'clean')"
                    )
            outcome, statuses, run_dir = self._run_command(
                [sys.executable, "-c", code])
            self.assertIsNotNone(run_dir)
            stdout = next(run_dir.glob("probe.stdout.log"))
            self.assertIn("clean", stdout.read_text())
            self.assertNotIn(marker, stdout.read_text())
        finally:
            os.environ.pop("PLAYBOOK_TEST_SECRET", None)

    @unittest.skipUnless(shutil.which("sandbox-exec"),
                         "sandbox-exec недоступен")
    def test_sandbox_denies_home_access(self):
        """Реальная граница на стенде: запись/чтение дома запрещены."""
        proj, home = init_home(self, command_policy(
            [sys.executable, "-c", "pass"]))
        arena = home / checks_trust.ARENA_DIR / "arena-test"
        arena.mkdir(parents=True)
        prefix = checks_trust.sandbox_prefix(proj.root, home, arena)
        code_write = (f"from pathlib import Path; "
                      f"Path({str(home)!r}/'x').write_text('x')")
        import subprocess
        result = subprocess.run(
            prefix + [sys.executable, "-c", code_write],
            capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0,
                            result.stderr[-300:])
        code_read = (f"print(open({str(home / 'keys' / 'active')!r})"
                     ".read())")
        result = subprocess.run(
            prefix + [sys.executable, "-c", code_read],
            capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Operation not permitted", result.stderr)


class AuditFixesTest(unittest.TestCase):
    """Регрессии AUDIT-01/02: A-01 self-reported блокировка,
    A-02 probe, A-03 разделение env, A-04 отзыв источника,
    A-05 запись probe в каталоги."""

    def setUp(self):
        self.policy = trust_policy()

    # --- A-01: подделка отчёта адаптера тестовым процессом ---

    def _evil_project(self, body):
        proj = run_project(self)
        proj.write("tests/test_payments.py", body)
        return proj

    def _run_and_status(self, proj, policy=None):
        policy = policy or self.policy
        home = make_home(self)
        checks_trust.init_home(home, project_id="shop", actor="Devin",
                               basis_detail="fixture")
        proj.write("checks-policy.json", json.dumps(policy))
        store = checks_trust.load_home(home)
        checks_trust.approve_policy(store, policy, actor="Devin")
        outcome = verifier_run(proj, home)
        statuses = {item["check_id"]: item["status"]
                    for item in outcome["receipt"]["subject"]["results"]}
        return statuses, outcome, home

    def test_self_reported_policy_refused(self):
        """Политика с test-проверками не получает квитанцию (A-01/AUDIT-02).

        Критерий аудита: блокировать защищённое подтверждение для
        адаптера, чьи события self-reported. Подделка ack-протокола
        (тест читает ack и делает os._exit(0)) невозможна в принципе —
        run отклоняется до исполнения.
        """
        proj, home = init_home(self, self.policy)
        with self.assertRaisesRegex(ValueError, "self-reported"):
            verifier_run(proj, home)
        # Consumer не покрыт: квитанций нет.
        gate = checks_verifier.gate(proj.root, home, emit=lambda s: None)
        self.assertFalse(gate["ok"])

    def test_forged_report_file_rejected(self):
        """Поддельный adapter.json + os._exit — квитанция не выдаётся."""
        body = (
            "import glob, json, os\n"
            "for p in glob.glob('work/checks/runs/*/*.adapter.json'):\n"
            "    open(p, 'w').write(json.dumps({\n"
            "        'protocol': 1, 'expected': ['x'], 'actual': [],\n"
            "        'exit_code': 0}))\n"
            "os._exit(0)\n")
        proj = self._evil_project(body)
        with self.assertRaisesRegex(ValueError, "self-reported"):
            self._run_and_status(proj)

    def test_forged_stdout_sentinel_posthumous(self):
        """Строка отчёта, за которой процесс умер — квитанция не выдаётся."""
        sentinel = checks_unittest.REPORT_SENTINEL
        fake = json.dumps({"protocol": 1, "expected": ["x"],
                           "actual": [], "exit_code": 0})
        body = (f"import os\nprint({sentinel!r} + {fake!r})\n"
                "os._exit(0)\n")
        proj = self._evil_project(body)
        with self.assertRaisesRegex(ValueError, "self-reported"):
            self._run_and_status(proj)

    def test_duplicate_report_lines_rejected(self):
        """Подделка поверх настоящего отчёта — квитанция не выдаётся."""
        sentinel = checks_unittest.REPORT_SENTINEL
        fake = json.dumps({"protocol": 1, "expected": ["x"],
                           "actual": [], "exit_code": 0})
        body = (f"import unittest\n"
                f"print({sentinel!r} + {fake!r}, flush=True)\n"
                "class T(unittest.TestCase):\n"
                "    def test_x(self):\n"
                "        self.assertTrue(True)\n")
        proj = self._evil_project(body)
        with self.assertRaisesRegex(ValueError, "self-reported"):
            self._run_and_status(proj)

    def test_legacy_self_reported_receipt_rejected(self):
        """Квитанция на kind=test результат отклоняется verify (A-01).

        Моделирует квитанцию, выданную до блокировки run: подписана
        домом и есть в журнале, но ссылается на self-reported результат.
        """
        policy = command_policy([sys.executable, "-c", "pass"])
        proj, home = init_home(self, policy)
        outcome = verifier_run(proj, home)
        store = checks_trust.load_home(home)
        index_path = home / checks_trust.RESULTS_DIR / \
            checks_verifier.RESULT_INDEX
        index = checks_trust._read_json(index_path, "results-index")
        old_digest = outcome["receipt"]["subject"]["results"][0][
            "result_digest"]
        entry = index[old_digest]
        src_path = home / checks_trust.RESULTS_DIR / entry["path"]
        doc = checks_trust._read_json(src_path, "result")
        doc["kind"] = "test"
        doc.pop("command", None)
        doc["result_id"] += "-selfreported"
        doc["tests"] = {
            "expected": ["test_payments.TestX.test_ok"],
            "actual": [{"id": "test_payments.TestX.test_ok",
                        "outcome": "passed"}]}
        new_digest = checks_results.result_digest(doc)
        rel = entry["path"].replace(".json", "-selfreported.json")
        checks_trust._write_json(
            home / checks_trust.RESULTS_DIR / rel, doc)
        index[new_digest] = {"path": rel, "check_id": doc["check_id"],
                             "run_id": entry["run_id"]}
        checks_trust._write_json(index_path, index)
        receipt = {"schema_version": 1,
                   "receipt_id": "rc-selfreported",
                   "project_id": "shop",
                   "candidate_digest":
                       outcome["receipt"]["candidate_digest"],
                   "policy_digest": outcome["receipt"]["policy_digest"],
                   "policy_revision":
                       outcome["receipt"]["policy_revision"],
                   "engine": dict(checks_trust.ENGINE),
                   "issuer": outcome["receipt"]["issuer"],
                   "issued_at": "2026-11-10T22:00:00+03:00",
                   "subject": {"kind": "run", "results": [
                       {"check_id": doc["check_id"],
                        "result_digest": new_digest,
                        "status": "passed"}]}}
        receipt["signature"] = checks_trust.sign(
            store, {key: value for key, value in receipt.items()
                    if key != "signature"})
        checks_trust._write_json(
            home / checks_trust.RECEIPTS_DIR / "rc-selfreported.json",
            receipt)
        checks_trust._journal(
            store, "receipt", receipt_id="rc-selfreported",
            kind="run",
            candidate_digest=receipt["candidate_digest"],
            policy_digest=receipt["policy_digest"], results=1)
        verdict = checks_verifier.verify(home, receipt)
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("self-reported" in r
                            for r in verdict["reasons"]))

    # --- A-02: probe не выводит protected из одного marker ---

    def test_probe_readonly_root_stays_local(self):
        """chmod 0555 на доме того же uid не даёт protected (A-02)."""
        proj, home = init_home(self, self.policy)
        os.chmod(home, 0o555)
        try:
            report = checks_trust.probe(proj.root, home)
            self.assertEqual(report["capability"], "local")
            self.assertFalse(report["separate_identity"])
        finally:
            os.chmod(home, 0o755)

    # --- A-03: env процесса отделён от binding среды ---

    def test_scrubbed_env_reaches_subprocess(self):
        """HOME/TMPDIR внутри arena, PATH есть; binding env не подменяет."""
        policy = command_policy(
            [sys.executable, "-c",
             "import os; print('HOME=' + os.environ.get('HOME', '')); "
             "print('PATH=' + ('set' if os.environ.get('PATH') else '')); "
             "print('PYTHONPATH=' + os.environ.get('PYTHONPATH', ''))"])
        proj, home = init_home(self, policy)
        outcome = verifier_run(proj, home)
        statuses = {item["check_id"]: item["status"]
                    for item in outcome["receipt"]["subject"]["results"]}
        self.assertEqual(statuses["probe"], "passed")
        arena = next((home / checks_trust.ARENA_DIR).glob("*/"))
        run_dir = next(arena.glob("work/checks/runs/*/"))
        out = (run_dir / "probe.stdout.log").read_text()
        self.assertIn(f"HOME={arena}/home", out)
        self.assertIn("PATH=set", out)

    def test_external_always_run_does_not_break_env(self):
        """required_environment с dict-значением не ломает subprocess (A-03)."""
        policy = command_policy(
            [sys.executable, "-c", "pass"])
        policy["environment"]["external"] = [
            {"id": "api", "freshness": {"kind": "always_run"}}]
        policy["checks"][0]["required_environment"] = ["api"]
        proj, home = init_home(self, policy)
        outcome = verifier_run(proj, home)
        statuses = {item["check_id"]: item["status"]
                    for item in outcome["receipt"]["subject"]["results"]}
        self.assertEqual(statuses["probe"], "passed")

    # --- A-04: отзыв источника распространяется на aggregate ---

    def test_aggregate_source_revocation_transitive(self):
        policy = command_policy([sys.executable, "-c", "pass"])
        proj, home = init_home(self, policy)
        outcome = verifier_run(proj, home)
        run_id = outcome["receipt"]["receipt_id"]
        agg = checks_verifier.aggregate(
            home, proj.root, receipt_ids=[run_id], emit=lambda s: None)
        agg2 = checks_verifier.aggregate(
            home, proj.root,
            receipt_ids=[agg["receipt"]["receipt_id"]],
            emit=lambda s: None)
        # Отзыв исходного run: обе цепочки aggregate обязаны упасть.
        store = checks_trust.load_home(home)
        checks_trust.revoke(store, receipt_id=run_id, actor="Devin")
        for receipt in (agg["receipt"], agg2["receipt"]):
            verdict = checks_verifier.verify(home, receipt)
            self.assertFalse(verdict["ok"])
            self.assertTrue(any("источник" in r or "отозван" in r
                                for r in verdict["reasons"]))
        gate = checks_verifier.gate(proj.root, home, emit=lambda s: None)
        self.assertFalse(gate["ok"])


class ProbeWritabilityTest(unittest.TestCase):
    """A-05/AUDIT-02: запись в каталоги проверяется marker-файлом."""

    def test_writable_dir_marker_created_and_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(
                checks_trust._writable(directory, directory=True))
            # Probe убирает только свой marker — каталог пуст.
            self.assertEqual(os.listdir(directory), [])

    def test_denied_dir_returns_false(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o555)
            try:
                self.assertFalse(
                    checks_trust._writable(directory, directory=True))
            finally:
                os.chmod(directory, 0o755)

    def test_unknown_error_returns_none(self):
        real_open = os.open

        def fake_open(path, flags, *args):
            raise OSError(errno.EIO, "искусственная ошибка")

        with tempfile.TemporaryDirectory() as directory:
            with unittest.mock.patch.object(os, "open", fake_open):
                self.assertIsNone(
                    checks_trust._writable(directory, directory=True))

    def test_probe_reports_writable_home_points(self):
        """Доступный для записи дом: writable_points заполнены,
        agent_can_write=True; защищённость не заявляется (нет
        отдельной OS identity)."""
        proj = run_project(self)
        policy = command_policy([sys.executable, "-c", "pass"])
        proj.write("checks-policy.json", json.dumps(policy))
        home = make_home(self)
        checks_trust.init_home(home, project_id="shop",
                               actor="Devin", basis_detail="fixture")
        report = checks_trust.probe(proj.root, home)
        self.assertIn("дом", report["writable_points"])
        self.assertIn("ключи", report["writable_points"])
        self.assertTrue(report["agent_can_write"])
        self.assertEqual(report["capability"], "local")
        # Marker-файлы probe не должны остаться в доме.
        leftovers = [p.name for p in home.rglob(".probe-*")]
        self.assertEqual(leftovers, [])


class GateTest(unittest.TestCase):
    """Эталонный consumer: покрытие только валидными квитанциями (AC-02)."""

    def setUp(self):
        self.policy = command_policy([sys.executable, "-c", "pass"])
        self.proj, self.home = init_home(self, self.policy)

    def gate(self):
        return checks_verifier.gate(self.proj.root, self.home,
                                    emit=lambda s: None)

    def test_no_receipts_uncovered(self):
        verdict = self.gate()
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["obligated"],
                         len(self.policy["checks"]))
        self.assertFalse(verdict["protected_confirmed"])

    def test_covered_by_run_receipt(self):
        verifier_run(self.proj, self.home)
        verdict = self.gate()
        self.assertTrue(verdict["ok"], verdict["rejections"])
        self.assertEqual(verdict["covered"], verdict["obligated"])
        # Локальный стенд: покрытие есть, защищённость не заявлена.
        self.assertFalse(verdict["protected_confirmed"])
        self.assertEqual(verdict["capability"], "local")

    def test_forged_file_in_project_rejected(self):
        verifier_run(self.proj, self.home)
        forged = {"schema_version": 1, "receipt_id": "rc-evil",
                  "project_id": "shop",
                  "candidate_digest": "sha256:" + "0" * 64,
                  "policy_digest": "sha256:" + "0" * 64,
                  "policy_revision": 1,
                  "engine": dict(checks_trust.ENGINE),
                  "issuer": "verifier:x",
                  "issued_at": "2026-11-10T00:00:00+03:00",
                  "subject": {"kind": "run", "results": [
                      {"check_id": "payments-tests",
                       "result_digest": "sha256:" + "1" * 64,
                       "status": "passed"}]},
                  "signature": {"algorithm": "hmac-sha256",
                                "key_id": "k-evil", "value": "0"}}
        target = self.proj.root / checks_trust.PROJECT_RECEIPTS_DIR / \
            "rc-evil.json"
        target.write_text(json.dumps(forge(forged)), encoding="utf-8")
        verdict = self.gate()
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("rc-evil" in r
                            for r in verdict["rejections"]))


if __name__ == "__main__":
    unittest.main()
