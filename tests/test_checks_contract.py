"""PB-057: контракты policy/graph/result/receipt, классификация и CLI."""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "ai-dev-playbook" / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "checks_contract"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import checks_contract as cc
import documents
from test_lifecycle import LifecycleCase
from test_playbook import playbook


def req(condition, message):
    if not condition:
        raise ValueError(message)


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"),
                      object_pairs_hook=documents._unique_pairs)


def mutate(base, path, value):
    """Копия документа с правкой по пути из ключей; None удаляет ключ."""
    data = copy.deepcopy(base)
    node = data
    for key in path[:-1]:
        node = node[key]
    if value is None:
        node.pop(path[-1], None)
    else:
        node[path[-1]] = value
    return data


class SchemaCase(unittest.TestCase):
    def check(self, validator, doc, fragment=None):
        if fragment is None:
            self.assertIs(validator(doc, req), doc)
        else:
            with self.assertRaisesRegex(ValueError, fragment):
                validator(doc, req)


class PolicyFixtureTest(SchemaCase):
    def test_valid_policy(self):
        self.check(cc.validate_policy, fixture("policy.json"))

    def test_version_constants(self):
        self.assertEqual(cc.POLICY_SCHEMA_VERSION, 1)
        self.assertEqual(cc.GRAPH_SCHEMA_VERSION, 1)
        self.assertEqual(cc.RESULT_SCHEMA_VERSION, 1)
        self.assertEqual(cc.RECEIPT_SCHEMA_VERSION, 1)
        self.assertEqual(cc.ADAPTER_PROTOCOL_VERSION, 1)
        self.assertEqual(cc.PROJECT_LINK_FIELD, "checks_policy")


class PolicySchemaTest(SchemaCase):
    def setUp(self):
        self.policy = fixture("policy.json")

    def check(self, doc, fragment=None):
        super().check(cc.validate_policy, doc, fragment)

    def test_unknown_version(self):
        self.check(mutate(self.policy, ["schema_version"], 2),
                   "schema_version")
        self.check(mutate(self.policy, ["schema_version"], "1"),
                   "schema_version")
        self.check(mutate(self.policy, ["schema_version"], True),
                   "schema_version")

    def test_unknown_fields_rejected(self):
        self.check(mutate(self.policy, ["rogue"], 1), "неизвестные поля")
        self.check(mutate(self.policy, ["approved"], True),
                   "неизвестные поля")
        self.check(mutate(self.policy, ["modules", 0, "rogue"], 1),
                   "неизвестные поля")
        self.check(mutate(self.policy, ["checks", 0, "rogue"], 1),
                   "неизвестные поля")

    def test_required_fields(self):
        for field in ("policy_id", "revision", "project_id", "trust_mode",
                      "approved_basis", "modules", "subsystems", "checks",
                      "dependencies", "excludes", "environment",
                      "fallback", "test_catalog"):
            self.check(mutate(self.policy, [field], None), field)

    def test_ids_and_revision(self):
        self.check(mutate(self.policy, ["policy_id"], "BAD-ID"),
                   "policy_id")
        self.check(mutate(self.policy, ["revision"], 0), "revision")
        self.check(mutate(self.policy, ["revision"], "1"), "revision")
        self.check(mutate(self.policy, ["revision"], True), "revision")
        self.check(mutate(self.policy, ["revision"], 1.5), "revision")

    def test_trust_mode(self):
        self.check(mutate(self.policy, ["trust_mode"], "trusted"),
                   "trust_mode")
        self.check(mutate(self.policy, ["trust_mode"], "protected"))

    def test_approved_basis(self):
        for field in ("who", "at", "source", "detail"):
            self.check(mutate(self.policy, ["approved_basis", field], None),
                       field)
        self.check(mutate(self.policy, ["approved_basis", "at"], "no-time"),
                   "at")
        self.check(mutate(self.policy, ["approved_basis", "extra"], "x"),
                   "неизвестные поля")

    def test_modules_rules(self):
        self.check(mutate(self.policy, ["modules"], []), "modules")
        self.check(mutate(self.policy, ["modules", 0, "subsystem_id"],
                          "nope"), "subsystem_id")
        self.check(mutate(self.policy, ["modules", 0, "paths"], []), "paths")
        self.check(mutate(self.policy, ["modules", 0, "paths"],
                          ["/abs/**"]), "путь")
        self.check(mutate(self.policy, ["modules", 0, "paths"],
                          ["../up/**"]), "путь")
        self.check(mutate(self.policy, ["modules", 0, "paths"],
                          ["src\\\\win"]), "путь")
        dup = dict(self.policy["modules"][0], paths=["src/other/**"])
        self.check(mutate(self.policy, ["modules"],
                          self.policy["modules"] + [dup]), "повторный ID")

    def test_module_path_overlap(self):
        overlap = {"id": "copy", "title": "Копия", "subsystem_id": "billing",
                   "paths": ["src/payments/**"]}
        self.check(mutate(self.policy, ["modules"],
                          self.policy["modules"] + [overlap]),
                   "пересекается")
        shared = dict(overlap, shared=True)
        base = mutate(self.policy, ["modules", 0, "shared"], True)
        self.check(mutate(base, ["modules"],
                          base["modules"] + [shared]))

    def test_checks_rules(self):
        self.check(mutate(self.policy, ["checks"], []), "checks")
        self.check(mutate(self.policy, ["checks", 0, "kind"], "probe"),
                   "kind")
        self.check(mutate(self.policy, ["checks", 0, "modules"],
                          ["ghost"]), "modules")
        self.check(mutate(self.policy, ["checks", 0, "adapter"], "jest"),
                   "adapter")
        self.check(mutate(self.policy, ["checks", 0, "adapter"], None),
                   "adapter")
        self.check(mutate(self.policy, ["checks", 1, "adapter"],
                          "python-unittest"), "adapter")
        self.check(mutate(self.policy, ["checks", 0, "argv"], []), "argv")
        self.check(mutate(self.policy, ["checks", 0, "argv"], None), "argv")
        self.check(mutate(self.policy, ["checks", 2, "argv"],
                          ["x"]), "argv")
        self.check(mutate(self.policy, ["checks", 2, "scenario"], None),
                   "scenario")
        self.check(mutate(self.policy, ["checks", 0, "scenario"], "x"),
                   "scenario")

    def test_limits_bool_nan(self):
        for bad in (True, False, float("nan"), float("inf"), 0, -5, "60"):
            self.check(mutate(self.policy,
                              ["checks", 0, "timeout_seconds"], bad),
                       "timeout_seconds")
        for bad in (True, float("nan"), -1, "x"):
            self.check(mutate(self.policy,
                              ["fallback", "limits", "max_seconds"], bad),
                       "max_seconds")
        self.check(mutate(self.policy, ["fallback", "limits", "max_bytes"],
                          True), "max_bytes")
        self.check(mutate(self.policy, ["fallback", "limits", "max_bytes"],
                          0.5), "max_bytes")
        self.check(mutate(self.policy,
                          ["environment", "external", 0, "freshness",
                           "ttl_seconds"], float("nan")), "ttl_seconds")
        self.check(mutate(self.policy,
                          ["environment", "external", 0, "freshness",
                           "ttl_seconds"], True), "ttl_seconds")

    def test_inputs_and_applicability(self):
        self.check(mutate(self.policy, ["checks", 0, "inputs"],
                          [{"type": "ghost", "path": "x"}]), "тип входа")
        self.check(mutate(self.policy, ["checks", 0, "inputs"],
                          [{"type": "file"}]), "path")
        self.check(mutate(self.policy, ["checks", 0, "inputs"],
                          ["/abs"]), "путь")
        self.check(mutate(self.policy, ["checks", 0, "inputs"],
                          ["..\\up"]), "путь")
        self.check(mutate(self.policy, ["checks", 0, "applicability"],
                          {"kind": "maybe"}), "kind")
        self.check(mutate(self.policy, ["checks", 0, "applicability"],
                          {"kind": "when_paths", "paths": []}), "paths")
        self.check(mutate(self.policy, ["checks", 0, "applicability"],
                          "sometimes"), "применимость")

    def test_dependencies_rules(self):
        edge = {"consumer": "module:ghost", "dependency": "module:cart",
                "kind": "import", "source": "explicit"}
        self.check(mutate(self.policy, ["dependencies"],
                          self.policy["dependencies"] + [edge]),
                   "consumer")
        edge = {"consumer": "module:cart", "dependency": "module:cart",
                "kind": "import", "source": "explicit"}
        self.check(mutate(self.policy, ["dependencies"],
                          self.policy["dependencies"] + [edge]),
                   "самоссылка")
        existing = dict(self.policy["dependencies"][0])
        self.check(mutate(self.policy, ["dependencies"],
                          self.policy["dependencies"] + [existing]),
                   "повторное ребро")
        edge = {"consumer": "cart", "dependency": "module:payments",
                "kind": "import", "source": "explicit"}
        self.check(mutate(self.policy, ["dependencies"],
                          self.policy["dependencies"] + [edge]),
                   "consumer")
        edge = {"consumer": "module:cart", "dependency": "module:payments",
                "kind": "magic", "source": "explicit"}
        self.check(mutate(self.policy, ["dependencies"],
                          self.policy["dependencies"] + [edge]), "kind")
        edge = {"consumer": "module:cart", "dependency": "module:payments",
                "kind": "import", "source": "analyzer"}
        self.check(mutate(self.policy, ["dependencies"],
                          self.policy["dependencies"] + [edge]),
                   "analyzer_version")

    def test_excludes_rules(self):
        for pattern in ("src/**", "src"):
            self.check(mutate(self.policy, ["excludes"],
                              [{"pattern": pattern, "reason": "x",
                                "origin": "manual"}]),
                       "конфликтует")
        self.check(mutate(self.policy, ["excludes"],
                          [{"pattern": "src/payments/gen/**",
                            "reason": "x", "origin": "manual"}]),
                   "конфликтует")
        self.check(mutate(self.policy, ["excludes"],
                          [{"pattern": "tests/**", "reason": "x",
                            "origin": "manual"}]), "конфликтует")
        self.check(mutate(self.policy, ["excludes"],
                          [{"pattern": "gen/**", "reason": "",
                            "origin": "manual"}]), "reason")
        self.check(mutate(self.policy, ["excludes"],
                          [{"pattern": "gen/**", "reason": "x",
                            "origin": "elsewhere"}]), "origin")
        self.check(mutate(self.policy, ["excludes"],
                          [{"pattern": "gen/**", "reason": "x",
                            "origin": "manual", "approved": True}]),
                   "неизвестные поля")

    def test_excludes_unanchored_glob(self):
        """A-01: glob без литерального префикса наблюдает корень."""
        for pattern in ("**", "**/*", "**/payments/**", "*"):
            self.check(mutate(self.policy, ["excludes"],
                              [{"pattern": pattern, "reason": "x",
                                "origin": "manual"}]),
                       "конфликтует")
        # Селективный незаякоренный glob допустим, как и заякоренный.
        self.check(mutate(self.policy, ["excludes"],
                          [{"pattern": "**/fixtures.bin", "reason": "x",
                            "origin": "manual"}]))

    def test_excludes_exact_input_match(self):
        """A-05: glob, совпадающий с явным входом, — коллизия."""
        for pattern in ("**/test_payments.py", "tests/test_payments.p?",
                        "tests/test_payments.py", "*/test_payments.py"):
            self.check(mutate(self.policy, ["excludes"],
                              [{"pattern": pattern, "reason": "x",
                                "origin": "manual"}]),
                       "конфликтует")
        # Селективный образец внутри области, не совпадающий с явным
        # входом, допустим.
        for pattern in ("tests/*.log", "src/payments/*.py"):
            self.check(mutate(self.policy, ["excludes"],
                              [{"pattern": pattern, "reason": "x",
                                "origin": "manual"}]))

    def test_environment_rules(self):
        self.check(mutate(self.policy,
                          ["environment", "external", 0, "freshness",
                           "kind"], "weekly"), "kind")
        self.check(mutate(self.policy,
                          ["environment", "external", 0, "freshness",
                           "ttl_seconds"], None), "ttl_seconds")
        self.check(mutate(self.policy,
                          ["environment", "external", 0, "freshness",
                           "version"], "2"), "version")
        self.check(mutate(self.policy,
                          ["environment", "external", 1, "freshness",
                           "ttl_seconds"], 5), "ttl_seconds")
        self.check(mutate(self.policy,
                          ["environment", "toolchain"],
                          self.policy["environment"]["toolchain"] * 2),
                   "повторный ID")

    def test_fallback_rules(self):
        self.check(mutate(self.policy,
                          ["fallback", "expansion_order"],
                          ["subsystem", "module", "project"]),
                   "expansion_order")
        self.check(mutate(self.policy, ["fallback", "unknown_scope"],
                          "galaxy"), "unknown_scope")
        self.check(mutate(self.policy, ["fallback", "approved_commands"],
                          [["ok"], []]), "argv")
        self.check(mutate(self.policy, ["fallback", "limits", "extra"], 1),
                   "неизвестные поля")

    def test_catalog_rules(self):
        self.check(mutate(self.policy, ["test_catalog", "adapter"], "x"),
                   "adapter")
        self.check(mutate(self.policy, ["test_catalog", "fingerprint"],
                          "md5:abc"), "fingerprint")
        self.check(mutate(self.policy, ["test_catalog", "entries"], []),
                   "entries")
        self.check(mutate(self.policy, ["test_catalog", "entries", 0,
                                        "description"], ""), "description")
        self.check(mutate(self.policy, ["test_catalog", "entries", 0,
                                        "description"], None), "description")
        entries = self.policy["test_catalog"]["entries"]
        self.check(mutate(self.policy, ["test_catalog", "entries"],
                          entries + [entries[0]]), "повторный test_id")
        self.check(mutate(self.policy, ["test_catalog", "entries", 1,
                                        "skip", "allowed"], "yes"),
                   "allowed")
        self.check(mutate(self.policy, ["test_catalog", "entries", 1,
                                        "skip", "reason"], None), "reason")
        self.check(mutate(self.policy, ["test_catalog", "entries", 0,
                                        "skip"],
                          {"allowed": False, "extra": 1}),
                   "неизвестные поля")

    def test_catalog_expected_failure(self):
        """A-04: явная политика допуска expected_failure отдельно от skip."""
        entry = {"id": "t.new", "description": "d",
                 "expected_failure": {"allowed": True, "reason": "r"}}
        entries = self.policy["test_catalog"]["entries"] + [entry]
        self.check(mutate(self.policy, ["test_catalog", "entries"],
                          entries))
        self.check(mutate(self.policy, ["test_catalog", "entries", 3,
                                        "expected_failure", "allowed"],
                          "yes"), "allowed")
        self.check(mutate(self.policy, ["test_catalog", "entries", 3,
                                        "expected_failure", "reason"],
                          None), "reason")
        self.check(mutate(self.policy, ["test_catalog", "entries", 3,
                                        "expected_failure", "extra"], 1),
                   "неизвестные поля")


class GraphSchemaTest(SchemaCase):
    def setUp(self):
        self.graph = fixture("graph.json")

    def check(self, doc, fragment=None, **kw):
        super().check(lambda d, r: cc.validate_graph(d, r, **kw),
                      doc, fragment)

    def test_valid(self):
        self.check(self.graph)

    def test_known_refs(self):
        refs = {("module", "checkout"), ("module", "payments"),
                ("module", "cart"), ("module", "shared-util")}
        cc.validate_graph(self.graph, req, known_refs=refs)
        with self.assertRaisesRegex(ValueError, "consumer"):
            cc.validate_graph(self.graph, req, known_refs=set())

    def test_schema_version(self):
        self.check(mutate(self.graph, ["schema_version"], 2),
                   "schema_version")
        self.check(mutate(self.graph, ["schema_version"], True),
                   "schema_version")

    def test_unknown_fields(self):
        self.check(mutate(self.graph, ["rogue"], 1), "неизвестные поля")
        self.check(mutate(self.graph, ["edges", 0, "rogue"], 1),
                   "неизвестные поля")

    def test_edges(self):
        edge = {"consumer": "module:a", "dependency": "module:a",
                "kind": "import", "source": "explicit"}
        self.check(mutate(self.graph, ["edges"],
                          self.graph["edges"] + [edge]), "самоссылка")
        self.check(mutate(self.graph, ["edges"],
                          self.graph["edges"] + [self.graph["edges"][0]]),
                   "повторное ребро")
        self.check(mutate(self.graph, ["edges", 0, "source"], "heard"),
                   "source")
        self.check(mutate(self.graph, ["edges", 0, "analyzer_version"],
                          None), "analyzer_version")

    def test_unknown_scope(self):
        self.check(mutate(self.graph, ["unknown", 0, "scope", "kind"],
                          "file"), "kind")
        self.check(mutate(self.graph, ["unknown", 1, "scope", "id"],
                          "x"), "id")
        self.check(mutate(self.graph, ["unknown", 0, "scope", "id"], None),
                   "id")
        self.check(mutate(self.graph, ["unknown", 0, "reason"], ""),
                   "reason")


class ResultSchemaTest(SchemaCase):
    def setUp(self):
        self.result = fixture("result.json")

    def check(self, doc, fragment=None):
        super().check(cc.validate_result, doc, fragment)

    def test_valid(self):
        self.check(self.result)

    def test_version_and_status(self):
        self.check(mutate(self.result, ["schema_version"], 0),
                   "schema_version")
        self.check(mutate(self.result, ["status"], "green"), "status")
        self.check(mutate(self.result, ["kind"], "probe"), "kind")

    def test_binding_required(self):
        for field in ("policy_digest", "definition_digest", "adapter",
                      "runner", "input_digest",
                      "dependency_closure_digest", "environment"):
            self.check(mutate(self.result, ["binding", field], None),
                           field)
        self.check(mutate(self.result, ["binding", "policy_digest"],
                          "md5:x"), "policy_digest")
        self.check(mutate(self.result, ["binding", "adapter"],
                          {"id": "x"}), "version")
        self.check(mutate(self.result, ["binding", "rogue"], 1),
                   "неизвестные поля")

    def test_tests_contract(self):
        self.check(mutate(self.result, ["tests"], None), "tests")
        self.check(mutate(self.result, ["tests", "expected"], "x"),
                   "expected")
        self.check(mutate(self.result,
                          ["tests", "actual", 0, "outcome"], "meh"),
                   "outcome")
        self.check(mutate(self.result,
                          ["tests", "actual", 1, "reason"], None), "reason")
        actual = self.result["tests"]["actual"]
        self.check(mutate(self.result, ["tests", "actual"],
                          actual + [actual[0]]), "повторный test_id")
        expected = self.result["tests"]["expected"]
        self.check(mutate(self.result, ["tests", "expected"],
                          expected + [expected[0]]), "повторный test_id")

    def test_kind_specific(self):
        cmd = mutate(self.result, ["kind"], "command")
        cmd["command"] = {"exit_code": 0, "stdout_log": "o",
                          "stderr_log": "e"}
        cmd.pop("tests")
        self.check(cmd)
        self.check(mutate(cmd, ["command", "exit_code"], True),
                   "exit_code")
        self.check(mutate(cmd, ["command", "exit_code"], "0"),
                   "exit_code")
        manual = mutate(self.result, ["kind"], "manual")
        manual["manual"] = {
            "observation": "итог совпал",
            "scenario_digest": "sha256:" + "0" * 64,
            "confirmation": {"who": "Инженер",
                             "at": "2026-11-07T12:00:00+03:00",
                             "statement": "проверено вручную"}}
        manual.pop("tests")
        self.check(manual)

    def test_status_reason(self):
        for status in ("blocked", "not_applicable"):
            doc = mutate(self.result, ["status"], status)
            doc.pop("tests")
            self.check(mutate(doc, ["reason"], "почему"))
            self.check(mutate(doc, ["reason"], None), "reason")
            self.check(mutate(doc, ["reason"], ""), "reason")

    def test_reused(self):
        doc = mutate(self.result, ["reused"],
                     {"result_id": "res-0", "issued_at":
                      "2026-11-06T10:00:00+03:00",
                      "input_digest": "sha256:" + "9" * 64})
        self.check(doc)
        self.check(mutate(doc, ["reused", "input_digest"], "x"),
                   "input_digest")
        self.check(mutate(doc, ["reused", "rogue"], 1),
                   "неизвестные поля")


class ReceiptSchemaTest(SchemaCase):
    def setUp(self):
        self.receipt = fixture("receipt.json")

    def check(self, doc, fragment=None):
        super().check(cc.validate_receipt, doc, fragment)

    def test_valid(self):
        self.check(self.receipt)

    def test_version_and_digests(self):
        self.check(mutate(self.receipt, ["schema_version"], "1"),
                   "schema_version")
        self.check(mutate(self.receipt, ["candidate_digest"], "x"),
                   "candidate_digest")
        self.check(mutate(self.receipt, ["policy_revision"], True),
                   "policy_revision")
        self.check(mutate(self.receipt, ["policy_revision"], 0),
                   "policy_revision")

    def test_subject(self):
        self.check(mutate(self.receipt, ["subject", "kind"], "batch"),
                   "kind")
        self.check(mutate(self.receipt, ["subject", "results"], []),
                   "results")
        results = self.receipt["subject"]["results"]
        self.check(mutate(self.receipt, ["subject", "results"],
                          results + [results[0]]), "повторный check_id")
        self.check(mutate(self.receipt,
                          ["subject", "results", 0, "status"], "ok"),
                   "status")
        self.check(mutate(self.receipt,
                          ["subject", "source_receipts"], ["r1"]),
                   "source_receipts")

    def test_aggregate(self):
        doc = mutate(self.receipt, ["subject", "kind"], "aggregate")
        self.check(mutate(doc, ["subject", "source_receipts"],
                          ["rcpt-0"]))
        self.check(mutate(doc, ["subject", "source_receipts"], None),
                   "source_receipts")
        self.check(mutate(doc, ["subject", "source_receipts"], []),
                   "source_receipts")

    def test_signature(self):
        self.check(mutate(self.receipt, ["signature", "algorithm"],
                          "rsa"), "algorithm")
        self.check(mutate(self.receipt, ["signature", "value"], ""),
                   "value")
        self.check(mutate(self.receipt, ["signature", "extra"], 1),
                   "неизвестные поля")


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.policy = fixture("policy.json")

    def verdict(self, new):
        return cc.classify_policy_change(self.policy, new)["verdict"]

    def test_unchanged(self):
        rev = mutate(self.policy, ["revision"], 4)
        self.assertEqual(self.verdict(rev), "unchanged")
        self.assertEqual(cc.classify_policy_change(
            self.policy, rev)["changes"], [])

    def test_identity_change_rejected(self):
        for field in ("policy_id", "project_id"):
            with self.assertRaisesRegex(ValueError, field):
                cc.classify_policy_change(
                    self.policy, mutate(self.policy, [field], "other"))

    def test_extension(self):
        new = copy.deepcopy(self.policy)
        # argv из ранее одобренных команд — расширение допустимо.
        new["checks"].append({
            "id": "cart-tests", "title": "Тесты корзины",
            "purpose": "поведение корзины", "kind": "test",
            "modules": ["cart"], "adapter": "python-unittest",
            "argv": ["python3", "-m", "unittest", "discover",
                     "-s", "tests", "-v"],
            "timeout_seconds": 60})
        new["dependencies"].append({
            "consumer": "check:cart-tests", "dependency": "module:cart",
            "kind": "import", "source": "explicit"})
        new["modules"].append({"id": "search", "title": "Поиск",
                               "subsystem_id": "catalog",
                               "paths": ["src/search/**"]})
        new["test_catalog"]["entries"].append(
            {"id": "tests.test_cart.TestCart.test_remove",
             "description": "удаление позиции"})
        result = cc.classify_policy_change(self.policy, new)
        self.assertEqual(result["verdict"], "extension")
        self.assertTrue(all(c["classification"] == "extension"
                            for c in result["changes"]))

    def test_weakening(self):
        cases = []

        doc = copy.deepcopy(self.policy)
        del doc["checks"][0]
        cases.append((doc, "checks"))

        doc = copy.deepcopy(self.policy)
        del doc["dependencies"][0]
        cases.append((doc, "dependencies"))

        doc = copy.deepcopy(self.policy)
        doc["excludes"].append({"pattern": "gen/**", "reason": "r",
                                "origin": "manual"})
        cases.append((doc, "excludes"))

        doc = copy.deepcopy(self.policy)
        doc["checks"][0]["timeout_seconds"] = 999
        cases.append((doc, "checks"))

        doc = copy.deepcopy(self.policy)
        doc["checks"][0]["argv"] = ["python3", "-m", "pytest"]
        cases.append((doc, "checks"))

        doc = copy.deepcopy(self.policy)
        doc["checks"][0]["adapter"] = "other"
        cases.append((doc, "checks"))

        doc = copy.deepcopy(self.policy)
        doc["test_catalog"]["entries"][1]["skip"] = {"allowed": False}
        doc["test_catalog"]["entries"][0]["skip"] = {"allowed": True,
                                                     "reason": "r"}
        cases.append((doc, "test_catalog"))

        doc = copy.deepcopy(self.policy)
        doc["fallback"]["limits"]["max_seconds"] = 7200
        cases.append((doc, "fallback"))

        doc = copy.deepcopy(self.policy)
        doc["fallback"]["approved_commands"].append(["make", "test"])
        cases.append((doc, "fallback"))

        doc = copy.deepcopy(self.policy)
        doc["fallback"]["unknown_scope"] = "module"
        cases.append((doc, "fallback"))

        doc = copy.deepcopy(self.policy)
        doc["trust_mode"] = "protected"
        cases.append((doc, "trust_mode"))

        doc = copy.deepcopy(self.policy)
        del doc["modules"][0]["paths"][0]
        doc["modules"][0]["paths"] = ["src/payments/core/**"]
        cases.append((doc, "modules"))

        doc = copy.deepcopy(self.policy)
        del doc["test_catalog"]["entries"][0]
        cases.append((doc, "test_catalog"))

        for doc, kind in cases:
            result = cc.classify_policy_change(self.policy, doc)
            self.assertEqual(result["verdict"], "weakening",
                             f"{kind}: {result}")
            self.assertTrue(any(c["kind"] == kind
                                for c in result["changes"]), result)

    def test_extension_mixed_with_weakening(self):
        doc = copy.deepcopy(self.policy)
        doc["checks"].append({
            "id": "new-one", "title": "t", "purpose": "p", "kind": "test",
            "modules": ["cart"], "adapter": "python-unittest",
            "argv": ["python3", "-m", "unittest", "discover",
                     "-s", "tests", "-v"], "timeout_seconds": 10})
        del doc["checks"][1]
        result = cc.classify_policy_change(self.policy, doc)
        self.assertEqual(result["verdict"], "weakening")
        kinds = {c["classification"] for c in result["changes"]}
        self.assertEqual(kinds, {"extension", "weakening"})

    def test_new_check_argv_allowlist(self):
        """A-02: новая исполняемая команда требует решения инженера."""
        novel = copy.deepcopy(self.policy)
        novel["checks"].append({
            "id": "hack", "title": "t", "purpose": "p",
            "kind": "command", "modules": [],
            "argv": ["unapproved-tool", "--execute"],
            "timeout_seconds": 60})
        result = cc.classify_policy_change(self.policy, novel)
        self.assertEqual(result["verdict"], "weakening")
        self.assertTrue(any("allowlist" in c["detail"]
                            for c in result["changes"]))

        # argv из approved_commands fallback — допустимое расширение.
        approved = copy.deepcopy(self.policy)
        approved["checks"].append({
            "id": "discover", "title": "t", "purpose": "p",
            "kind": "test", "modules": ["cart"],
            "adapter": "python-unittest",
            "argv": ["python3", "-m", "unittest", "discover",
                     "-s", "tests", "-v"],
            "timeout_seconds": 60})
        result = cc.classify_policy_change(self.policy, approved)
        self.assertEqual(result["verdict"], "extension")

        # argv существующей проверки тоже входит в allowlist.
        reused = copy.deepcopy(self.policy)
        reused["checks"].append({
            "id": "lint2", "title": "t", "purpose": "p",
            "kind": "command", "modules": [],
            "argv": ["python3", "-m", "ruff", "check", "src"],
            "timeout_seconds": 60})
        self.assertEqual(self.verdict(reused), "extension")

        # manual-проверка без argv — обычное расширение.
        manual = copy.deepcopy(self.policy)
        manual["checks"].append({
            "id": "review", "title": "t", "purpose": "p",
            "kind": "manual", "modules": ["cart"],
            "scenario": "просмотр"})
        self.assertEqual(self.verdict(manual), "extension")

    def test_expected_failure_classification(self):
        """A-04: расширение допуска expected_failure — ослабление."""
        doc = copy.deepcopy(self.policy)
        doc["test_catalog"]["entries"][3]["expected_failure"] = {
            "allowed": False}
        doc["test_catalog"]["entries"][0]["expected_failure"] = {
            "allowed": True, "reason": "r"}
        result = cc.classify_policy_change(self.policy, doc)
        self.assertEqual(result["verdict"], "weakening")
        self.assertTrue(any("expected_failure" in c["detail"]
                            for c in result["changes"]))

        doc = copy.deepcopy(self.policy)
        doc["test_catalog"]["entries"][3]["expected_failure"] = {
            "allowed": False}
        self.assertEqual(self.verdict(doc), "extension")

    def test_new_catalog_entry_with_allowance(self):
        """A-06: новый ID сразу с допуском — ослабление."""
        for rule in ("skip", "expected_failure"):
            doc = copy.deepcopy(self.policy)
            doc["test_catalog"]["entries"].append({
                "id": "tests.New.test_case", "description": "d",
                rule: {"allowed": True, "reason": "audit"}})
            result = cc.classify_policy_change(self.policy, doc)
            self.assertEqual(result["verdict"], "weakening")
            self.assertTrue(any(rule in c["detail"]
                                for c in result["changes"]), result)

        # Новый тест без допусков или с allowed=false — extension.
        doc = copy.deepcopy(self.policy)
        doc["test_catalog"]["entries"].append(
            {"id": "tests.New.test_case", "description": "d"})
        self.assertEqual(self.verdict(doc), "extension")
        doc["test_catalog"]["entries"][-1]["skip"] = {"allowed": False}
        self.assertEqual(self.verdict(doc), "extension")

    def test_tightening_limits_is_extension(self):
        doc = copy.deepcopy(self.policy)
        doc["checks"][0]["timeout_seconds"] = 30
        doc["fallback"]["limits"]["max_seconds"] = 600
        result = cc.classify_policy_change(self.policy, doc)
        self.assertEqual(result["verdict"], "extension")


class CliContractTest(LifecycleCase):
    def test_deferred_commands(self):
        code, _, err = self.cli("checks", "plan", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("checks_policy", err)
        code, _, err = self.cli("checks", "run", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("PB-060", err)
        code, _, err = self.cli("checks", "run", "DEV-1",
                                "--scope", "project", "--mode", "force",
                                "--checkpoint", "release")
        self.assertEqual(code, 1)
        self.assertIn("PB-060", err)
        code, _, err = self.cli("checks", "status", "DEV-1")
        self.assertEqual(code, 1)
        self.assertIn("PB-060", err)
        code, _, err = self.cli("setup", "preview")
        self.assertEqual(code, 1)
        self.assertIn("PB-063", err)
        code, _, err = self.cli("setup", "apply")
        self.assertEqual(code, 1)
        self.assertIn("PB-063", err)

    def test_scope_mode_axes(self):
        """Область и режим — независимые оси; неизвестные значения отклонены."""
        for args in (("checks", "run", "DEV-1", "--scope", "all"),
                     ("checks", "run", "DEV-1", "--mode", "quick"),
                     ("checks", "run", "DEV-1", "--force")):
            with self.assertRaises(SystemExit) as ctx:
                self.cli(*args)
            self.assertEqual(ctx.exception.code, 2)

    def test_policy_link_validation(self):
        """project.json → checks_policy валидирует файл политики."""
        path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(path.read_text(encoding="utf-8"))
        project["checks_policy"] = "ai-dev-playbook/checks-policy.json"
        policy = fixture("policy.json")
        target = self.root / "ai-dev-playbook" / "checks-policy.json"
        target.write_text(json.dumps(policy, ensure_ascii=False),
                          encoding="utf-8")
        path.write_text(json.dumps(project, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        data, _ = playbook.config(self.root)
        self.assertEqual(data["checks_policy"],
                         "ai-dev-playbook/checks-policy.json")

        policy["checks"][0]["timeout_seconds"] = True
        target.write_text(json.dumps(policy, ensure_ascii=False),
                          encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            playbook.config(self.root)

        policy = fixture("policy.json")
        policy["approved"] = True
        target.write_text(json.dumps(policy, ensure_ascii=False),
                          encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "неизвестные поля"):
            playbook.config(self.root)

    def test_policy_link_path_safety(self):
        path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(path.read_text(encoding="utf-8"))
        project["checks_policy"] = "../outside.json"
        path.write_text(json.dumps(project, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "путь"):
            playbook.config(self.root)
        project["checks_policy"] = "ai-dev-playbook/missing.json"
        path.write_text(json.dumps(project, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "нет файла"):
            playbook.config(self.root)

    def test_backward_compat(self):
        """Проект без checks_policy и task_workflow работает как прежде."""
        data, _ = playbook.config(self.root)
        self.assertNotIn("checks_policy", data)
        self.assertIsNone(data.get("task_workflow"))
        code, _, err = self.cli("check")
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
