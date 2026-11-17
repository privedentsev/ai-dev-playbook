"""Общее актуальное покрытие по доверенной истории verifier (PB-079)."""
import checks_runner
import checks_select
import checks_trust
import checks_verifier


def _has_task_input(check):
    return any(isinstance(entry, dict)
               and entry.get("type") == "task_projection"
               for entry in check.get("inputs") or [])


def protected_receipts(store, approved, policy, candidate_digest,
                       task_digest, check_ids, by_id, now=None, *,
                       receipt_ids=None):
    """Покрытие по доверенной истории verifier'а (A-03/A-04 аудита PB-062).

    Источник истины — квитанции доверенного дома, а не проектные копии
    ``work/checks/receipts``: их имена, порядок и наличие не влияют на
    вердикт, поэтому удаление или переименование копии не возвращает
    и не отменяет покрытие. Учитываются только квитанции текущего
    кандидата и одобренной политики — остальные относятся к прежним
    эпохам и пропускаются.

    Для каждой проверки актуальный исход выбирается тем же
    состоянием порядка, что у локальной истории
    (``checks_select``): ``exec_seq`` — позиция receipt-события в
    журнале, порядок коммита квитанций, сам по себе не доказывающий
    порядок завершения перекрывающихся прогонов; ``finished_at`` —
    метка исполнения и при откате wall clock. Покрывающий ``passed``
    принимается только когда ``exec_seq`` строго больше ``exec_seq``
    любой непокрывающей квитанции **и** ``finished_at`` не
    противоречит меткам непокрывающих (A-06/A-01 PB-078); при
    равном ``exec_seq`` (параллельные квитанции с общим исходным
    запуском) взаимный порядок недоказан — консервативно важнее
    непокрывающий исход. Просроченная по ``ttl`` квитанция
    отклоняется, только когда она актуальная — старая просроченная
    не блокирует более свежее успешное доказательство.
    Для проверок с входом
    ``task_projection`` дополнительно требуется совпадение
    ``task_requirements_digest`` результата с текущей проекцией задачи
    (R-12). Возвращает ``(covered, rejections, notes)``:
    ``covered`` — check_id → result_digest актуальных passed.
    """
    covered, rejections, notes = {}, [], []
    receipts_dir = store["home"] / checks_trust.RECEIPTS_DIR
    wanted = set(check_ids)
    journal_seq = {}
    issued = {}
    try:
        for index, entry in enumerate(checks_trust.journal_entries(store)):
            if not isinstance(entry, dict):
                raise ValueError("событие журнала не объект")
            if entry.get("event") == "receipt":
                rid = entry.get("receipt_id")
                if not isinstance(rid, str) or not rid or rid in journal_seq:
                    raise ValueError("непригодный/повторный receipt_id в журнале")
                journal_seq[rid] = index
                issued[rid] = entry
    except (ValueError, OSError) as error:
        return {}, [f"доверенная история непригодна: {error}"], []
    # A lost issued receipt may have been a later failure. Never resurrect
    # an older success merely because its trusted source disappeared.
    paths = {path.name: path for path in receipts_dir.glob("*.json")}
    for rid in journal_seq:
        name = f"{rid}.json"
        if name not in paths:
            rejections.append(f"{rid}: выданная квитанция отсутствует в доме")
    docs = {}
    outcomes = {}
    skipped = 0
    exec_seq = {}
    current = []
    for path in sorted(paths.values()):
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
        event = issued.get(receipt.get("receipt_id"))
        if event is not None and any(
                receipt.get(key) != event.get(key)
                for key in ("candidate_digest", "policy_digest")):
            rejections.append(f"{path.name}: привязка квитанции не совпадает "
                              "с доверенным журналом выдачи")
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
            if receipt_ids is not None:
                receipt_ids.append(receipt["receipt_id"])
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
        # exec_seq — порядок коммита квитанций в журнале; покрытие
        # требует согласия exec_seq и finished_at с каждым
        # непокрывающим исходом — то же состояние порядка, что у
        # локальной истории (A-06/A-01 PB-078).
        state = checks_select._new_order_state()
        for item in items:
            checks_select._merge_order_state(state, {
                "check_id": cid, "status": item["status"],
                "finished_at": item["finished_at"],
                "result_id": item["result_digest"],
                "result_digest": item["result_digest"],
                "run_seq": item["seq"],
                "doc": item["doc"],
                "receipt_id": item["receipt_id"]})
        latest = checks_select._choose_order_state(state)
        if latest is None:
            continue
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

