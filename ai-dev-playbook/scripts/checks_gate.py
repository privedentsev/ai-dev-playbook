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


def _has_task_input(check):
    return any(isinstance(entry, dict)
               and entry.get("type") == "task_projection"
               for entry in check.get("inputs") or [])


def _protected_receipts(store, approved, policy, candidate_digest,
                        task_digest, check_ids, by_id, now=None):
    """Покрытие по доверенной истории verifier'а (A-03/A-04 аудита PB-062).

    Источник истины — квитанции доверенного дома, а не проектные копии
    ``work/checks/receipts``: их имена, порядок и наличие не влияют на
    вердикт, поэтому удаление или переименование копии не возвращает
    и не отменяет покрытие. Учитываются только квитанции текущего
    кандидата и одобренной политики — остальные относятся к прежним
    эпохам и пропускаются.

    Для каждой проверки актуальный исход — доверенный результат с
    максимальным ``finished_at`` среди валидных квитанций: поздний
    failed/timeout блокирует старый passed, а просроченная по ``ttl``
    квитанция отклоняется, только когда она актуальная — старая
    просроченная не блокирует более свежее успешное доказательство.
    Порядок при равных ``finished_at`` определяется устойчивой
    доверенной последовательностью исходных запусков (A-05 аудита
    PB-062): ``exec_seq`` — позиция receipt-события run-квитанции в
    журнале для каждого результата; aggregate — не новое исполнение,
    его исход наследует ``exec_seq`` исходного run и не может
    переопределить более поздний failed. Для проверок с входом
    ``task_projection`` дополнительно требуется совпадение
    ``task_requirements_digest`` результата с текущей проекцией задачи
    (R-12). Возвращает ``(covered, rejections, notes)``:
    ``covered`` — check_id → result_digest актуальных passed.
    """
    covered, rejections, notes = {}, [], []
    receipts_dir = store["home"] / checks_trust.RECEIPTS_DIR
    if not receipts_dir.is_dir():
        return covered, rejections, notes
    wanted = set(check_ids)
    journal_seq = {}
    for index, entry in enumerate(checks_trust.journal_entries(store)):
        if entry.get("event") == "receipt" and entry.get("receipt_id"):
            journal_seq[entry["receipt_id"]] = index
    docs = {}
    outcomes = {}
    skipped = 0
    exec_seq = {}
    current = []
    for path in sorted(receipts_dir.glob("*.json")):
        if path.is_symlink():
            rejections.append(f"{path.name}: символическая ссылка — "
                              "пропущено")
            continue
        try:
            receipt = checks_trust._read_json(path, "receipt")
        except ValueError as error:
            rejections.append(f"{path.name}: {error}")
            continue
        if not isinstance(receipt, dict):
            rejections.append(f"{path.name}: квитанция не объект")
            continue
        is_current = \
            receipt.get("candidate_digest") == candidate_digest and \
            receipt.get("policy_digest") == \
            approved["policy_digest"]
        verdict = checks_verifier.verify(
            store["home"], receipt,
            expect_candidate=candidate_digest if is_current else None,
            expect_policy=approved["policy_digest"]
            if is_current else None)
        if not verdict["ok"]:
            if is_current:
                rejections.append(
                    f"{path.name}: {'; '.join(verdict['reasons'])}")
            continue
        if (receipt.get("subject") or {}).get("kind") == "run":
            seq = journal_seq.get(receipt["receipt_id"], -1)
            for item in verdict["results"]:
                exec_seq.setdefault(item.get("result_digest"), seq)
        if is_current:
            current.append((path.name, receipt, verdict))
        else:
            skipped += 1
    for name, receipt, verdict in current:
        for item in verdict["results"]:
            digest = item.get("result_digest")
            if digest not in docs:
                try:
                    docs[digest] = checks_verifier._load_result(
                        store, digest)
                except ValueError as error:
                    docs[digest] = error
            doc = docs[digest]
            if isinstance(doc, ValueError):
                rejections.append(
                    f"{name}: результат {item.get('check_id')}: {doc}")
                continue
            outcomes.setdefault(item.get("check_id"), []).append({
                "doc": doc, "status": doc["status"],
                "finished_at": doc.get("finished_at") or "",
                "result_digest": digest,
                "receipt_id": receipt["receipt_id"],
                "seq": exec_seq.get(
                    digest, journal_seq.get(receipt["receipt_id"], -1))})
    if skipped:
        notes.append(
            f"квитанций вне текущего кандидата/политики: {skipped} — "
            "не учитываются")
    for cid in wanted:
        items = outcomes.get(cid)
        if not items:
            continue
        check = by_id.get(cid)
        if check is None or check["kind"] == "manual":
            continue
        latest = max(items, key=lambda o: (
            o["finished_at"], o["seq"], o["result_digest"]))
        if latest["status"] != "passed":
            notes.append(
                f"{cid}: последний доверенный исход "
                f"{latest['status']} ({latest['receipt_id']}) — "
                "старые passed не покрывают")
            continue
        doc = latest["doc"]
        if _has_task_input(check) and \
                doc["binding"].get("task_requirements_digest") != \
                task_digest:
            notes.append(
                f"{cid}: последний результат привязан к другой "
                "проекции задачи — покрытие отклонено")
            continue
        stale = checks_runner._external_freshness(
            check, policy, None, doc, now)
        if stale is not None:
            rejections.append(
                f"{latest['receipt_id']}: {cid} — {stale}")
            continue
        covered[cid] = latest["result_digest"]
    return covered, rejections, notes


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
        previous=previous, results=results, task_digest=task_digest,
        checkpoint=checkpoint, now=now, projections=projections)
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
