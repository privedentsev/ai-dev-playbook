"""PB-065: контракты политики приёмки, графа и общего evaluator."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "ai-dev-playbook" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import block_workflow
import documents
from test_close_lifecycle import CloseLifecycleCase, managed_card
from test_documents import doc_meta, write_doc, tree_fingerprint
from test_lifecycle import LifecycleCase
from test_playbook import playbook

bw = block_workflow


def policy(mode="final", members=("DEV-1", "DEV-2"), audit="AUDIT-1",
           checkpoints=None, edges=None, **extra):
    data = {"schema_version": 1, "policy_id": "block-x", "revision": 1,
            "mode": mode,
            "approved_basis": {"who": "Инженер",
                               "at": "2026-11-07T00:00:00+03:00",
                               "source": "поручение"},
            "members": list(members), "final_audit": audit}
    if checkpoints is not None:
        data["checkpoints"] = checkpoints
    if edges is not None:
        data["implementation_edges"] = edges
    data.update(extra)
    return data


def edges(*pairs):
    return [{"from": a, "to": b, "required": "verified_handoff"}
            for a, b in pairs]


def valid(mode="final"):
    kwargs = {}
    if mode == "final":
        kwargs["edges"] = edges(("DEV-1", "DEV-2"))
    elif mode == "mixed":
        kwargs["edges"] = edges(("DEV-1", "DEV-2"))
        kwargs["checkpoints"] = [{"id": "cp1", "tasks": ["DEV-1"]},
                                 {"id": "cp2", "tasks": ["DEV-2"]}]
    errors = []
    result = bw.validate_policy(policy(mode, **kwargs), "policy", errors)
    assert not errors, errors
    return result


class PolicySchemaTest(unittest.TestCase):
    def check(self, payload, fragment=None):
        errors = []
        bw.validate_policy(payload, "p", errors)
        if fragment is None:
            self.assertEqual(errors, [])
        else:
            self.assertTrue(any(fragment in e for e in errors),
                            f"{fragment!r} not in {errors}")

    def test_valid_modes(self):
        self.check(policy("per_task"))
        self.check(policy("final", edges=edges(("DEV-1", "DEV-2"))))
        self.check(policy("mixed",
                          checkpoints=[{"id": "cp1", "tasks": ["DEV-1"]},
                                       {"id": "cp2", "tasks": ["DEV-2"]}],
                          edges=edges(("DEV-1", "DEV-2"))))

    def test_unknown_version_rejected(self):
        self.check(policy(schema_version=2), "неизвестная версия")
        self.check(policy(schema_version="1"), "неизвестная версия")

    def test_unknown_fields_rejected(self):
        self.check(policy(rogue=1), "неизвестные поля")

    def test_ids_revision_mode(self):
        self.check(policy(policy_id="BAD"), "policy_id")
        self.check(policy(revision=0), "revision")
        self.check(policy(revision="1"), "revision")
        self.check(policy(mode="bulk"), "mode")

    def test_basis_required(self):
        bad = policy()
        del bad["approved_basis"]
        self.check(bad, "approved_basis")
        self.check(policy(approved_basis={"who": "", "at": "x",
                                          "source": "y"}), "who")
        self.check(policy(approved_basis={"who": "w", "at": "no-time",
                                          "source": "y"}), "at")

    def test_members_rules(self):
        self.check(policy(members=[]), "members")
        self.check(policy(members=["DEV-1", "DEV-1"]), "повтор")
        self.check(policy(members=["dev-1"]), "некорректный ID")
        self.check(policy(audit="DEV-1"), "final_audit не входит")

    def test_checkpoints_partition_for_mixed(self):
        self.check(policy("mixed"), "mixed требует")
        self.check(policy("mixed",
                          checkpoints=[{"id": "c", "tasks": ["DEV-1"]}]),
                   "неполное покрытие")
        self.check(policy("mixed",
                          checkpoints=[{"id": "c1", "tasks": ["DEV-1", "DEV-2"]},
                                       {"id": "c2", "tasks": ["DEV-2"]}]),
                   "повторное членство")
        self.check(policy("mixed",
                          checkpoints=[{"id": "c1", "tasks": ["DEV-1"]},
                                       {"id": "c2", "tasks": ["DEV-9"]}]),
                   "не из members")
        self.check(policy("final",
                          checkpoints=[{"id": "c", "tasks": ["DEV-1"]}]),
                   "недопустимы при mode")

    def test_edges_rules(self):
        self.check(policy("per_task", edges=edges(("DEV-1", "DEV-2"))),
                   "недопустимы при per_task")
        self.check(policy("final", edges=edges(("DEV-1", "DEV-3"))),
                   "не из members")
        self.check(policy("final", edges=edges(("DEV-1", "DEV-1"))),
                   "в саму задачу")
        self.check(policy("final", edges=[{"from": "DEV-1", "to": "DEV-2",
                                          "required": "done"}]), "required")
        self.check(policy("final", edges=edges(("DEV-1", "DEV-2"),
                                               ("DEV-1", "DEV-2"))),
                   "повторное ребро")
        self.check(policy("final",
                          members=["A-1", "B-1", "C-1"],
                          edges=edges(("A-1", "B-1"), ("B-1", "C-1"),
                                      ("C-1", "A-1"))), "цикл")


class CardBlockTest(unittest.TestCase):
    def block(self, **extra):
        data = {"schema_version": 1, "document": "plan:block-x",
                "policy_id": "block-x", "revision": 1}
        data.update(extra)
        return data

    def check(self, payload, fragment=None):
        errors = []
        bw.validate_card_block(payload, "c", errors)
        if fragment is None:
            self.assertEqual(errors, [])
        else:
            self.assertTrue(any(fragment in e for e in errors),
                            f"{fragment!r} not in {errors}")

    def test_valid_refs(self):
        self.check(self.block())
        self.check(self.block(role="member"))
        self.check(self.block(role="final_audit"))
        self.check(self.block(role="fix",
                              fix={"report_id": "report-1", "iteration": 1,
                                   "findings": ["F-01"]}))

    def test_ref_errors(self):
        self.check(self.block(schema_version=9), "неизвестная версия")
        self.check(self.block(document="spec:x"), "plan:<slug>")
        self.check(self.block(revision=0), "revision")
        self.check(self.block(role="lead"), "role")
        self.check(self.block(role="fix"), "задаются вместе")
        self.check(self.block(fix={"report_id": "r", "iteration": 1,
                                   "findings": ["F-01"]}), "задаются вместе")
        self.check(self.block(role="fix",
                              fix={"report_id": "r", "iteration": 0,
                                   "findings": ["F-01"]}), "iteration")
        self.check(self.block(role="fix",
                              fix={"report_id": "r", "iteration": 1,
                                   "findings": ["F-1"]}), "findings")


class GraphTest(unittest.TestCase):
    """validate_block_graph: противоречия, членство, циклы, AUDIT↔."""

    def run_graph(self, pols, blocks, records, fragment=None):
        errors = []
        bw.validate_block_graph(pols, blocks, records, errors)
        if fragment is None:
            self.assertEqual(errors, [])
            return
        self.assertTrue(any(fragment in e for e in errors),
                        f"{fragment!r} not in {errors}")

    def rec(self, status="draft", deps=(), archived=False):
        return {"status": status, "deps": list(deps), "archived": archived,
                "path": Path(f"docs/tasks/{id(self)}.md")}

    def records(self, specs):
        return {ident: {"status": status, "deps": list(deps),
                        "archived": False,
                        "path": Path(f"docs/tasks/{ident}.md")}
                for ident, (status, deps) in specs.items()}

    def pols(self, entry, archived=False):
        return {"plan:block-x": {"policy": entry, "path": "docs/plans/x.md",
                                 "archived": archived}}

    def refs(self, ids, **overrides):
        result = {}
        for ident in ids:
            ref = {"schema_version": 1, "document": "plan:block-x",
                   "policy_id": "block-x", "revision": 1,
                   "role": overrides.get(ident), "fix": None}
            result[ident] = ref
        return result

    def test_members_must_ref(self):
        pol = valid()
        records = self.records({"DEV-1": ("draft", ()),
                                "DEV-2": ("draft", ()),
                                "AUDIT-1": ("draft", ())})
        self.run_graph(self.pols(pol), {}, records, "не имеет ссылки block")

    def test_happy_membership(self):
        pol = valid()
        records = self.records({"DEV-1": ("draft", ()),
                                "DEV-2": ("draft", ()),
                                "AUDIT-1": ("draft", ())})
        self.run_graph(self.pols(pol), self.refs(pol["members"]
                       + [pol["final_audit"]]), records)

    def test_ref_revision_stale(self):
        pol = valid()
        records = self.records({"DEV-1": ("draft", ()),
                                "DEV-2": ("draft", ()),
                                "AUDIT-1": ("draft", ())})
        blocks = self.refs(pol["members"] + [pol["final_audit"]])
        blocks["DEV-1"]["revision"] = 2
        self.run_graph(self.pols(pol), blocks, records, "устарела")

    def test_repeated_membership(self):
        pol = valid()
        other = dict(valid())
        other = {"policy": {**valid(), "policy_id": "block-y"},
                 "path": "docs/plans/y.md", "archived": False}
        pols = self.pols(pol)
        pols["plan:block-y"] = other
        records = self.records({"DEV-1": ("draft", ()),
                                "DEV-2": ("draft", ()),
                                "AUDIT-1": ("draft", ())})
        self.run_graph(pols, self.refs(pol["members"] + [pol["final_audit"]]),
                       records, "повторное членство")

    def test_dep_edge_contradiction(self):
        pol = valid()
        records = self.records({"DEV-1": ("draft", ()),
                                "DEV-2": ("draft", ("DEV-1",)),
                                "AUDIT-1": ("draft", ())})
        self.run_graph(self.pols(pol),
                       self.refs(pol["members"] + [pol["final_audit"]]),
                       records, "Depends-On и implementation_edge")

    def test_audit_dep_cycle_rejected(self):
        pol = valid()
        records = self.records({"DEV-1": ("draft", ()),
                                "DEV-2": ("draft", ()),
                                "AUDIT-1": ("draft", ("DEV-1",))})
        self.run_graph(self.pols(pol),
                       self.refs(pol["members"] + [pol["final_audit"]]),
                       records, "AUDIT ↔ members")

    def test_per_task_audit_dep_allowed(self):
        pol = valid("per_task")
        records = self.records({"DEV-1": ("draft", ()),
                                "DEV-2": ("draft", ("DEV-1",)),
                                "AUDIT-1": ("draft", ("DEV-1", "DEV-2"))})
        self.run_graph(self.pols(pol),
                       self.refs(pol["members"] + [pol["final_audit"]]),
                       records)

    def test_combined_cycle_rejected(self):
        pol = valid()
        records = self.records({"DEV-1": ("draft", ()),
                                "DEV-2": ("draft", ()),
                                "AUDIT-1": ("draft", ())})
        # impl edge DEV-1→DEV-2 + строгое DEV-1→DEV-2 отсутствует, но
        # Depends-On DEV-2→DEV-1 образует цикл с impl ребром.
        records["DEV-1"]["deps"] = ["DEV-2"]
        self.run_graph(self.pols(pol),
                       self.refs(pol["members"] + [pol["final_audit"]]),
                       records, "цикл")

    def test_fix_no_strict_edges(self):
        pol = valid()
        records = self.records({"DEV-1": ("draft", ()),
                                "DEV-2": ("draft", ()),
                                "AUDIT-1": ("draft", ()),
                                "FIX-1": ("draft", ("DEV-1",))})
        blocks = self.refs(pol["members"] + [pol["final_audit"]])
        blocks["FIX-1"] = {"schema_version": 1, "document": "plan:block-x",
                           "policy_id": "block-x", "revision": 1,
                           "role": "fix",
                           "fix": {"report_id": "r1", "iteration": 1,
                                   "findings": ["F-01"]}}
        self.run_graph(self.pols(pol), blocks, records, "у исправления нет")

    def test_unknown_card_in_policy(self):
        pol = valid()
        records = self.records({"DEV-1": ("draft", ()),
                                "DEV-2": ("draft", ()),
                                "AUDIT-1": ("draft", ()),
                                "EXT-1": ("draft", ())})
        blocks = self.refs(pol["members"] + [pol["final_audit"]]
                           + ["EXT-1"])
        self.run_graph(self.pols(pol), blocks, records, "не входит в members")


class EvaluateTest(unittest.TestCase):
    def task(self, ident, deps=(), block=None):
        return {"id": ident, "status": "in_progress", "deps": list(deps),
                "block": block}

    def records(self, statuses):
        return {ident: {"status": s, "deps": [], "archived": s == "done"}
                for ident, s in statuses.items()}

    def test_legacy_strict_deps(self):
        task = self.task("DEV-2", deps=["DEV-1"])
        records = self.records({"DEV-1": "in_progress", "DEV-2": "draft"})
        verdict = bw.evaluate("check", task, None, records, {})
        self.assertFalse(verdict["allowed"])
        self.assertIn(bw.DEPENDENCY_NOT_DONE, verdict["reason_codes"])
        records["DEV-1"]["status"] = "done"
        self.assertTrue(bw.evaluate("check", task, None, records,
                                    {})["allowed"])
        records["DEV-1"]["status"] = "cancelled"
        verdict = bw.evaluate("check", task, None, records, {})
        self.assertIn(bw.DEPENDENCY_CANCELLED, verdict["reason_codes"])

    def test_final_chain_by_handoffs(self):
        pol = valid()
        records = self.records({"DEV-1": "in_review", "DEV-2": "draft"})
        task = self.task("DEV-2")
        no_evidence = bw.evaluate("check", task, pol, records, {})
        self.assertIn(bw.PREDECESSOR_HANDOFF_MISSING,
                      no_evidence["reason_codes"])
        evidence = {"handoffs": {"DEV-1": {"current": True, "id": "H-1"}}}
        verdict = bw.evaluate("check", task, pol, records, evidence)
        self.assertTrue(verdict["allowed"])
        self.assertEqual(verdict["consumed_receipts"],
                         [{"kind": "handoff", "task": "DEV-1",
                           "reference": "H-1"}])
        stale = {"handoffs": {"DEV-1": {"current": False}}}
        verdict = bw.evaluate("check", task, pol, records, stale)
        self.assertIn(bw.PREDECESSOR_HANDOFF_STALE,
                      verdict["reason_codes"])

    def test_member_audit_accept_close_final(self):
        pol = valid()
        records = self.records({"DEV-1": "in_review"})
        task = self.task("DEV-1")
        evidence = {"handoffs": {"DEV-1": {"current": True}}}
        audit = bw.evaluate("audit_ready", task, pol, records, evidence)
        self.assertIn(bw.INDIVIDUAL_AUDIT_NOT_USED, audit["reason_codes"])
        accept = bw.evaluate("accept", task, pol, records, evidence)
        self.assertIn(bw.BLOCK_ACCEPTANCE_REQUIRED, accept["reason_codes"])
        close = bw.evaluate("close", task, pol, records, evidence)
        self.assertIn(bw.BLOCK_ACCEPTANCE_REQUIRED, close["reason_codes"])
        evidence["block_receipt"] = {"accepted": True,
                                     "candidate_sha256": "sha256:" + "a" * 64}
        self.assertTrue(bw.evaluate("close", task, pol, records,
                                    evidence)["allowed"])
        evidence["candidate_sha256"] = "sha256:" + "b" * 64
        close = bw.evaluate("close", task, pol, records, evidence)
        self.assertIn(bw.CANDIDATE_STALE, close["reason_codes"])

    def test_mixed_checkpoint_gate(self):
        pol = valid("mixed")
        pol["checkpoints"] = [{"id": "cp1", "tasks": ["DEV-1"]},
                              {"id": "cp2", "tasks": ["DEV-2"]}]
        pol["implementation_edges"] = edges(("DEV-1", "DEV-2"))
        records = self.records({"DEV-1": "in_review", "DEV-2": "draft"})
        task = self.task("DEV-2")
        evidence = {"handoffs": {"DEV-1": {"current": True}}}
        verdict = bw.evaluate("check", task, pol, records, evidence)
        self.assertIn(bw.CHECKPOINT_PENDING, verdict["reason_codes"])
        evidence["checkpoints"] = {"cp1": {"accepted": True, "receipt": "r"}}
        verdict = bw.evaluate("check", task, pol, records, evidence)
        self.assertTrue(verdict["allowed"])
        self.assertIn({"kind": "checkpoint", "id": "cp1", "reference": "r"},
                      verdict["consumed_receipts"])

    def test_audit_card_readiness(self):
        pol = valid()
        records = self.records({"DEV-1": "in_review", "DEV-2": "in_review",
                                "AUDIT-1": "ready"})
        audit_task = self.task("AUDIT-1")
        verdict = bw.evaluate("check", audit_task, pol, records, {})
        self.assertFalse(verdict["allowed"])
        evidence = {"handoffs": {"DEV-1": {"current": True},
                                 "DEV-2": {"current": True}}}
        self.assertTrue(bw.evaluate("check", audit_task, pol, records,
                                    evidence)["allowed"])
        ready = bw.evaluate("audit_ready", audit_task, pol, records, evidence)
        self.assertIn(bw.REVIEW_PACKAGE_MISSING, ready["reason_codes"])
        evidence["review_package"] = {"candidate_sha256": None}
        self.assertTrue(bw.evaluate("audit_ready", audit_task, pol,
                                    records, evidence)["allowed"])
        accept = bw.evaluate("accept", audit_task, pol, records,
                             dict(evidence, findings=[{"id": "F-01",
                                                       "resolved": False}]))
        self.assertIn(bw.FINDINGS_OPEN, accept["reason_codes"])
        accept = bw.evaluate("accept", audit_task, pol, records,
                             dict(evidence,
                                  findings=[{"id": "F-01", "resolved": True}],
                                  fixes={"FIX-1": {"accepted": False}}))
        self.assertIn(bw.FIX_PENDING, accept["reason_codes"])
        self.assertTrue(bw.evaluate("accept", audit_task, pol, records,
                                    evidence)["allowed"])

    def test_per_task_semantics(self):
        pol = valid("per_task")
        records = self.records({"DEV-1": "in_review"})
        task = self.task("DEV-1")
        verdict = bw.evaluate("audit_ready", task, pol, records, {})
        self.assertIn(bw.HANDOFF_NOT_CURRENT, verdict["reason_codes"])
        evidence = {"handoffs": {"DEV-1": {"current": True}}}
        self.assertTrue(bw.evaluate("audit_ready", task, pol, records,
                                    evidence)["allowed"])
        close = bw.evaluate("close", task, pol, records, evidence)
        self.assertIn(bw.TASK_NOT_ACCEPTED, close["reason_codes"])
        evidence["acceptances"] = {"DEV-1": {"at": "x"}}
        self.assertTrue(bw.evaluate("close", task, pol, records,
                                    evidence)["allowed"])

    def test_fix_card_readiness(self):
        pol = valid()
        records = self.records({"FIX-1": "in_review"})
        task = self.task("FIX-1",
                         block={"role": "fix", "revision": 1})
        self.assertTrue(bw.evaluate("check", task, pol, records,
                                    {})["allowed"])
        verdict = bw.evaluate("audit_ready", task, pol, records, {})
        self.assertIn(bw.HANDOFF_NOT_CURRENT, verdict["reason_codes"])
        evidence = {"handoffs": {"FIX-1": {"current": True}},
                    "acceptances": {"FIX-1": {}}}
        self.assertTrue(bw.evaluate("accept", task, pol, records,
                                    evidence)["allowed"])
        self.assertTrue(bw.evaluate("close", task, pol, records,
                                    evidence)["allowed"])

    def test_stale_ref_and_outsider(self):
        pol = valid()
        records = self.records({"DEV-9": "draft", "DEV-1": "in_progress"})
        verdict = bw.evaluate("check", self.task("DEV-9"), pol, records, {})
        self.assertIn(bw.POLICY_MISSING, verdict["reason_codes"])
        verdict = bw.evaluate("check",
                              self.task("DEV-1", block={"revision": 2}),
                              pol, records, {})
        self.assertIn(bw.POLICY_STALE, verdict["reason_codes"])

    def test_revise_allowed(self):
        pol = valid()
        records = self.records({"DEV-2": "in_review"})
        self.assertTrue(bw.evaluate("revise", self.task("DEV-2"), pol,
                                    records, {})["allowed"])
        with self.assertRaises(ValueError):
            bw.evaluate("explode", self.task("DEV-2"), pol, records, {})


class BlockStatusTest(unittest.TestCase):
    def test_progression(self):
        pol = valid()
        records = {"DEV-1": {"status": "in_progress", "deps": [],
                             "archived": False},
                   "DEV-2": {"status": "draft", "deps": [],
                             "archived": False}}
        self.assertEqual(bw.block_status(pol, records, {})["status"],
                         "awaiting_work")
        evidence = {"handoffs": {"DEV-1": {"current": True},
                                 "DEV-2": {"current": True}},
                    "review_package": {"candidate_sha256": None}}
        self.assertEqual(bw.block_status(pol, records, evidence)["status"],
                         "review_ready")
        self.assertEqual(bw.block_status(
            pol, records, dict(evidence,
                               findings=[{"id": "F-1",
                                          "resolved": False}]))["status"],
            "changes_requested")
        self.assertEqual(bw.block_status(
            pol, records, dict(evidence,
                               block_receipt={"accepted": True}))["status"],
            "accepted")
        self.assertEqual(bw.block_status(
            pol, {t: {**r, "status": "done", "archived": True}
                  for t, r in records.items()}, {})["status"], "archived")


class FormatValidatorsTest(unittest.TestCase):
    def check(self, fn, payload, fragment=None):
        errors = []
        fn(payload, "doc", errors)
        if fragment is None:
            self.assertEqual(errors, [])
        else:
            self.assertTrue(any(fragment in e for e in errors),
                            f"{fragment!r} not in {errors}")

    def block_ref(self):
        return {"document": "plan:x", "policy_id": "x", "revision": 1}

    def test_review_package(self):
        pkg = {"schema_version": 1, "package_id": "pkg-1",
               "block": self.block_ref(),
               "policy_sha256": "sha256:" + "a" * 64,
               "members": ["DEV-1"],
               "receipts": {"DEV-1": {"handoff": "H-1"}},
               "inputs": {"files": {}}, "environment": {},
               "matrix": [{"task": "DEV-1", "criterion": "AC-01",
                           "result": "passed"}],
               "candidate_sha256": "sha256:" + "b" * 64,
               "created_at": "2026-11-07T00:00:00+03:00"}
        self.check(bw.validate_review_package, pkg)
        self.check(bw.validate_review_package, {**pkg, "schema_version": 2},
                   "неизвестная версия")
        self.check(bw.validate_review_package,
                   {**pkg, "matrix": [{"task": "DEV-9", "criterion": "AC-01",
                                       "result": "passed"}]},
                   "вне members")
        self.check(bw.validate_review_package,
                   {**pkg, "candidate_sha256": "bad"}, "sha256")

    def test_findings_report(self):
        rep = {"schema_version": 1, "report_id": "rep-1", "iteration": 1,
               "block": self.block_ref(),
               "author": {"name": "Auditor", "model": "M"},
               "verdict": "changes_requested",
               "candidate_sha256": "sha256:" + "c" * 64,
               "at": "2026-11-07T00:00:00+03:00",
               "findings": [{"id": "F-01", "source_tasks": ["DEV-1"],
                             "affected_ac": ["AC-01"],
                             "reproducer": "команда",
                             "evidence": ["log.txt"],
                             "expected": "поведение",
                             "severity": "P1"}]}
        self.check(bw.validate_findings_report, rep)
        self.check(bw.validate_findings_report,
                   {**rep, "verdict": "accepted"}, "verdict")
        self.check(bw.validate_findings_report, {**rep, "findings": []},
                   "findings")
        bad = dict(rep)
        bad["findings"] = [dict(rep["findings"][0], severity="P9")]
        self.check(bw.validate_findings_report, bad, "severity")

    def test_block_receipt(self):
        receipt = {"schema_version": 1, "receipt_id": "rc-1",
                   "block": self.block_ref(),
                   "package": {"id": "pkg-1", "sha256": "sha256:" + "a" * 64},
                   "policy_revision": 1,
                   "candidate_sha256": "sha256:" + "b" * 64,
                   "verdict": {"actor": {"name": "Auditor", "model": "M"},
                               "at": "2026-11-07T00:00:00+03:00"},
                   "acceptances": {"DEV-1": {"entry": "A-1"}},
                   "at": "2026-11-07T00:00:00+03:00"}
        self.check(bw.validate_block_receipt, receipt)
        self.check(bw.validate_block_receipt,
                   {**receipt, "acceptances": {}}, "acceptances")
        self.check(bw.validate_block_receipt,
                   {**receipt, "policy_revision": 0}, "policy_revision")


class CatalogIntegrationTest(CloseLifecycleCase):
    """Capability, план с политикой и карточки на временном проекте."""

    def setUp(self):
        super().setUp()
        self.plans = self.root / "docs" / "plans"
        self.plans.mkdir(parents=True, exist_ok=True)
        path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(path.read_text(encoding="utf-8"))
        project["documents"]["collections"]["plan"] = {
            "active": "docs/plans", "archive": "docs/archive/plans",
            "index": "docs/plans/index.md"}
        project["documents"]["capabilities"] = ["block_acceptance"]
        path.write_text(json.dumps(project, ensure_ascii=False, indent=2)
                        + "\n", encoding="utf-8")

    def block_ref(self, role=None, fix=None):
        ref = {"schema_version": 1, "document": "plan:block-x",
               "policy_id": "block-x", "revision": 1}
        if role:
            ref["role"] = role
        if fix:
            ref["fix"] = fix
        return ref

    def plan_with_policy(self, payload=None):
        data = doc_meta(document_id="plan:block-x", kind="plan",
                        state="draft", tasks=["DEV-1", "DEV-2", "AUDIT-1"],
                        acceptance_policy=payload or policy())
        write_doc(self.plans, "block-x.md", data)
        (self.plans / "index.md").write_text(
            "# Индекс планов\n\n<!-- playbook-index:start -->\n"
            "<!-- playbook-index:end -->\n", encoding="utf-8")

    def member_meta(self, role=None, revision=1):
        ref = self.block_ref(role)
        ref["revision"] = revision
        meta = {"schema_version": 1, "relations": [], "materials": [],
                "moves": [], "block": ref}
        return meta

    def test_capability_gates_fields(self):
        path = self.root / "ai-dev-playbook" / "project.json"
        project = json.loads(path.read_text(encoding="utf-8"))
        project["documents"].pop("capabilities")
        path.write_text(json.dumps(project, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        self.plan_with_policy()
        with self.assertRaisesRegex(ValueError, "capability"):
            self.catalog()
        project["documents"]["capabilities"] = ["bogus"]
        path.write_text(json.dumps(project, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "неизвестные значения"):
            self.catalog()

    def test_member_without_ref_rejected(self):
        self.plan_with_policy()
        managed_card(self.tasks_dir(), "DEV-1", "draft")
        managed_card(self.tasks_dir(), "DEV-2", "draft")
        managed_card(self.tasks_dir(), "AUDIT-1", "draft")
        with self.assertRaisesRegex(ValueError, "не имеет ссылки block"):
            self.catalog()

    def test_policy_preview_and_apply(self):
        self.plan_with_policy()
        for ident in ("DEV-1", "DEV-2", "AUDIT-1"):
            managed_card(self.tasks_dir(), ident, "draft",
                         meta=self.member_meta())
        self.assertEqual(self.cli("index")[0], 0)
        updated = policy(revision=2,
                         edges=edges(("DEV-1", "DEV-2")))
        # Новая ревизия синхронно переписывает ссылки карточек той же
        # операцией; preview показывает все записи без изменений дерева.
        before = tree_fingerprint(self.root)
        code, out, err = self.cli("policy", "preview", "plan:block-x",
                                  "--policy", json.dumps(updated))
        self.assertEqual(code, 0, err)
        self.assertIn("revision\": 2", out)
        self.assertIn("block", out)
        self.assertEqual(tree_fingerprint(self.root), before)
        code, out, err = self.cli("policy", "apply", "plan:block-x",
                                  "--policy", json.dumps(updated))
        self.assertEqual(code, 0, err + out)
        meta = self.meta_of(self.plans / "block-x.md")
        self.assertEqual(meta["acceptance_policy"]["revision"], 2)
        # Повторная запись идемпотентна; понижение ревизии отклонено.
        code, out, err = self.cli("policy", "apply", "plan:block-x",
                                  "--policy", json.dumps(updated))
        self.assertEqual(code, 0, err + out)
        self.assertIn("уже записана", out)
        code, out, err = self.cli("policy", "apply", "plan:block-x",
                                  "--policy", json.dumps(policy(revision=1)))
        self.assertEqual(code, 1)
        self.assertIn("новой ревизии", err)

    def test_block_status_and_deferred(self):
        self.plan_with_policy()
        for ident in ("DEV-1", "DEV-2", "AUDIT-1"):
            managed_card(self.tasks_dir(), ident, "draft",
                         meta=self.member_meta())
        self.assertEqual(self.cli("index")[0], 0)
        code, out, err = self.cli("block", "status", "plan:block-x")
        self.assertEqual(code, 0, err)
        self.assertIn("awaiting_work", out)
        code, out, err = self.cli("block", "status", "DEV-1", "--json")
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["block"]["status"], "awaiting_work")
        for deferred in (("prepare-review", "PB-067"),
                         ("revalidate", "PB-067"),
                         ("accept", "PB-069"), ("close", "PB-069")):
            code, out, err = self.cli("block", deferred[0], "AUDIT-1")
            self.assertEqual(code, 1)
            self.assertIn(deferred[1], err)
        code, out, err = self.cli("block", "create-fix", "AUDIT-1",
                                  "--report", "work/r.json", "--task",
                                  "FIX-1")
        self.assertEqual(code, 1)
        self.assertIn("PB-068", err)

    def test_external_meta_card_block(self):
        """Sidecar-метаданные: block в .meta.json работает так же."""
        self.plan_with_policy()
        for ident in ("DEV-1", "DEV-2", "AUDIT-1"):
            card = self.tasks_dir() / f"{ident}.md"
            card.write_text(
                f"# {ident}: test\n\nID: {ident}\nStatus: draft\n"
                "Priority: P2\nDepends-On: -\n"
                "Created: 2026-01-01T00:00:00Z\nClosed: -\n\n"
                f"{documents.EXTERNAL_META}\n", encoding="utf-8")
            card.with_name(f"{ident}.meta.json").write_text(
                json.dumps({**self.member_meta(), "card_format_version": 1},
                           ensure_ascii=False, indent=2),
                encoding="utf-8")
        data, paths = playbook.config(self.root)
        records = playbook.read_tasks(self.root, paths)
        catalog = documents.load_catalog(self.root, data, paths, records,
                                         playbook.safe_path, playbook.require)
        self.assertEqual(catalog["task_meta"]["DEV-1"]["block"]["document"],
                         "plan:block-x")


class FormatsContractTest(unittest.TestCase):
    def test_version_constants(self):
        self.assertEqual(bw.POLICY_SCHEMA_VERSION, 1)
        self.assertEqual(bw.REVIEW_PACKAGE_VERSION, 1)
        self.assertEqual(bw.FINDINGS_REPORT_VERSION, 1)
        self.assertEqual(bw.BLOCK_RECEIPT_VERSION, 1)
        self.assertIn("block_acceptance", bw.KNOWN_CAPABILITIES)


if __name__ == "__main__":
    unittest.main()
