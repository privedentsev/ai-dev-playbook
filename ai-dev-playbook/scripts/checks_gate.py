"""Единый расчёт покрытия проверок для жизненного цикла (PB-062).

Handoff, приёмка и готовность релиза используют один selector+coverage:
``checks_select.plan`` плюс оценка свежести результатов — тот же
расчёт, что у ``checks run --mode stale`` и ``checks status``.

``trust_mode=local``: покрытие дают свежие локальные результаты
``work/checks/results`` — диагностические, без заявления защиты
(R-20). ``trust_mode=protected``: локальные результаты и поддельные
отчёты обязательств не закрывают — покрытие дают только квитанции
verifier'а, проверенные в доверенном доме на текущем кандидате
(R-17, AC-01); ``ok`` дополнительно требует доказанной границы
OS/CI identity. Ручные обязательства (kind=manual) подтверждаются
записью ``checks confirm`` в обоих режимах.

Модуль read-only: ничего не исполняет и не пишет (probe границы может
создавать и удалять временные marker-файлы — как у verifier gate).
"""
import json
import os
from pathlib import Path

import checks_contract
from checks_coverage import protected_receipts as _protected_receipts
import checks_inventory
import checks_results
import checks_runner
import checks_select
import checks_trust
import checks_verifier
import verification_policy


def _raise(condition, message):
    if not condition:
        raise ValueError(message)


def load_policy(root):
    """Объявленная checks-политика проекта; None — контракт не подключён.

    Читает ``ai-dev-playbook/project.json`` и ссылку ``checks_policy``;
    путь к verifier home сюда не входит и не может быть подменён
    редактируемым файлом (R-17).
    """
    link = Path(root) / "ai-dev-playbook" / "project.json"
    if not link.is_file():
        return None
    data = json.loads(link.read_text(encoding="utf-8"),
                      object_pairs_hook=checks_results._unique_pairs)
    rel = data.get(checks_contract.PROJECT_LINK_FIELD)
    if not rel:
        return None
    path = Path(root) / rel
    _raise(path.is_file() and not path.is_symlink(),
           f"{checks_contract.PROJECT_LINK_FIELD}: нет файла {rel}")
    policy = json.loads(path.read_text(encoding="utf-8"),
                        object_pairs_hook=checks_results._unique_pairs)
    return checks_contract.validate_policy(policy, _raise)



def evaluate(root, policy, *, task_id, scope="task", checkpoint=None,
             task_content="", task_meta=None, verifier_home=None,
             now=None):
    """Read-only вердикт покрытия; ``ok`` — критерий потребителя.

    ``scope='task'`` — обязательства задачи (baseline+изменения);
    ``scope='project'`` — весь обязательный набор (релиз). Статусы
    проверок: ``covered`` — пригодное доказательство есть,
    ``uncovered`` — обязательство без доказательства, ``manual`` —
    ручное обязательство без подтверждения, ``blocked`` — блокировка
    бюджета/неизвестности, ``not_applicable`` — вне обязательств.
    """
    root = Path(root)
    task_meta = task_meta or {}
    if isinstance(task_meta, dict) and "has_block" in task_meta:
        task_meta = {
            k: v for k, v in task_meta.items()
            if k != "has_block" and (k != "block" or task_meta["has_block"])
        }
    task_digest = verification_policy.fingerprint(
        verification_policy.task_projection(task_content, task_meta))
    projections = checks_runner._projections(policy, task_content,
                                              task_meta)
    baseline, previous, notes = checks_runner._load_state(root)
    results, load_notes, corrupt_ids = checks_results.load_results(root)
    notes.extend(load_notes)
    if corrupt_ids:
        results = [r for r in results
                   if r["check_id"] not in corrupt_ids]
        notes.append("повреждённая история check_id "
                     f"{sorted(corrupt_ids)} — результаты не "
                     f"переиспользуются")
    planned = checks_select.plan(
        root, policy, baseline=baseline if scope == "task" else None,
        previous=previous, results=results, corrupt_ids=corrupt_ids,
        task_digest=task_digest, checkpoint=checkpoint, now=now,
        projections=projections)
    checks = {cid: dict(entry)
              for cid, entry in planned["checks"].items()}
    if scope == "project":
        for check in policy["checks"]:
            entry = checks[check["id"]]
            if entry["status"] in {"run", "reused"}:
                entry["explain"] = entry["explain"] + \
                    ["область project — весь обязательный набор"]
    checks_runner._apply_external_freshness(checks, results, policy, now)
    latest = checks_select.evidence_latest(results)

    verdict = {"ok": False, "scope": scope, "checkpoint": checkpoint,
               "task_id": task_id, "trust_mode": policy["trust_mode"],
               "checks": {}, "covered": [], "uncovered": [],
               "manual": [], "blocked": [], "not_applicable": [],
               "notes": notes, "rejections": [],
               "hashed_bytes": planned["hashed_bytes"],
               "hashed_files": planned["hashed_files"],
               "budget_reasons": planned["budget_reasons"]}
    by_id = {check["id"]: check for check in policy["checks"]}
    local_covered = set()
    for check in policy["checks"]:
        entry = checks[check["id"]]
        status = entry["status"]
        record = {"kind": check["kind"], "explain": entry["explain"]}
        if status == "not_applicable":
            verdict["not_applicable"].append(check["id"])
            record["status"] = "not_applicable"
        elif status == "reused":
            result = latest.get(check["id"])
            record["status"] = "covered"
            record["result_id"] = (result or {}).get("result_id")
            verdict["covered"].append(check["id"])
            local_covered.add(check["id"])
        elif status == "blocked":
            verdict["blocked"].append(check["id"])
            record["status"] = "blocked"
        elif check["kind"] == "manual":
            verdict["manual"].append(check["id"])
            record["status"] = "manual"
        else:
            verdict["uncovered"].append(check["id"])
            record["status"] = "uncovered"
        verdict["checks"][check["id"]] = record

    if policy["trust_mode"] == "protected":
        _protected_verdict(root, policy, verdict, by_id, task_digest,
                           verifier_home, now)
    else:
        verdict["ok"] = not (verdict["uncovered"]
                             or verdict["blocked"]
                             or verdict["manual"])
        verdict["trust"] = {"mode": "local", "capability": "local",
                            "note": checks_runner.TRUST_NOTE_LOCAL}
        verdict["protected_confirmed"] = False
    return verdict


def _protected_verdict(root, policy, verdict, by_id, task_digest,
                       verifier_home, now):
    """Protected-режим: только квитанции verifier'а текущего кандидата.

    Локальные результаты не закрывают обязательства; ручные — по
    подтверждению ``checks confirm``. ``ok`` требует покрытия всех
    обязательств, отсутствия отказов квитанций и доказанной границы
    OS/CI identity (R-17, R-20).
    """
    home = verifier_home or os.environ.get(checks_trust.HOME_ENV)
    probe = checks_trust.probe(root, home)
    verdict["trust"] = {"mode": "protected",
                        "capability": probe["capability"],
                        "boundary": probe["boundary"],
                        "notes": probe["notes"]}
    verdict["protected_confirmed"] = False
    # Ручные подтверждения (kind=manual) остаются покрытием и в
    # protected-режиме; локальный reuse исполняемых проверок — нет.
    executable = [cid for cid, record in verdict["checks"].items()
                  if record["kind"] != "manual"
                  and record["status"] in {"covered", "uncovered"}]
    verdict["covered"] = [cid for cid in verdict["covered"]
                          if by_id[cid]["kind"] == "manual"]
    for cid in executable:
        verdict["checks"][cid]["status"] = "uncovered"
        verdict["uncovered"].append(cid)
    verdict["uncovered"] = sorted(set(verdict["uncovered"]))
    verdict["covered"] = sorted(set(verdict["covered"]))
    if not probe["configured"]:
        verdict["rejections"].append(
            "verifier home не настроен — protected-покрытие недоказано")
        return
    try:
        store = checks_trust.load_home(home)
        approved = checks_trust.require_approved(store)
    except ValueError as error:
        verdict["rejections"].append(str(error))
        return
    declared_digest = verification_policy.fingerprint(policy)
    if declared_digest != approved["policy_digest"]:
        verdict["rejections"].append(
            "объявленная политика не совпадает с одобренной в доме — "
            "protected-покрытие недоказано")
        return
    try:
        _manifest, candidate_digest = checks_verifier.candidate_manifest(
            root, policy, now=now)
    except (ValueError, OSError) as error:
        verdict["rejections"].append(f"кандидат не построен: {error}")
        return
    verdict["candidate_digest"] = candidate_digest
    covered, rejections, gate_notes = _protected_receipts(
        store, approved, policy, candidate_digest, task_digest,
        executable, by_id, now)
    verdict["rejections"].extend(rejections)
    verdict["notes"].extend(gate_notes)
    for cid in executable:
        if cid in covered:
            verdict["checks"][cid]["status"] = "covered"
            verdict["checks"][cid]["result_id"] = covered[cid]
            verdict["covered"].append(cid)
            verdict["uncovered"].remove(cid)
    verdict["covered"] = sorted(set(verdict["covered"]))
    verdict["uncovered"] = sorted(verdict["uncovered"])
    coverage_ok = not (verdict["uncovered"] or verdict["blocked"]
                       or verdict["manual"] or verdict["rejections"])
    verdict["protected_confirmed"] = \
        coverage_ok and probe["capability"] == "protected"
    verdict["ok"] = verdict["protected_confirmed"]
    if coverage_ok and probe["capability"] != "protected":
        verdict["notes"].append(
            "покрытие квитанциями полное, но граница OS/CI identity не "
            "доказана — защищённое утверждение заблокировано (R-20)")
