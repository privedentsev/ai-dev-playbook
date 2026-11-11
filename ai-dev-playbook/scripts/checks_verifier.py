"""Эталонный verifier и consumer доверенного контура (PB-061).

Протокол (design §8, R-17–20):

1. Инженер через независимый канал одобряет политику
   (``checks_trust.approve_policy``) — verifier работает с доверенной
   копией, а не с редактируемым файлом проекта.
2. ``run``: verifier сверяет объявленную политику проекта с одобренным
   digest, сам вычисляет candidate manifest (агентский digest не
   принимается), материализует изолированный снимок в ``arena`` дома,
   запускает закреплённый runner с очищенным окружением и sandbox,
   валидирует результаты и подписывает receipt схемы 1. Клиентский итог
   никогда не подписывается как собственный запуск.
3. ``verify``: формат, подпись известным неотозванным ключом, факт
   выдачи в доверенном журнале, project_id, engine identity, отзывы и
   ожидаемые candidate/policy digest. Подмена, replay для других входов,
   cross-project, отозванный ключ/политика и неизвестный key_id
   отклоняются (AC-02).
4. ``aggregate``: для нового кандидата verifier заново сверяет per-check
   входы, граф и политику и выпускает агрегированное подтверждение со
   ссылками на исходные receipts; старый receipt не переименовывается.
5. ``gate``: эталонный consumer — покрытие обязательств валидными
   receipts текущего кандидата. Probe честно различает local/protected;
   без доказанной границы OS identity защищённая эксплуатация не
   заявляется (AC-03).

Хранилище результатов — дом verifier (``results/``): журнал не читает
редактируемую агентом историю ``work/checks/results`` как доверенную.
"""
import io
import shutil
import uuid
from pathlib import Path

import checks_contract
import checks_inventory
import checks_results
import checks_runner
import checks_select
import checks_trust
import project_view
import verification_policy


RESULT_INDEX = "index.json"
RECEIPT_PREFIX = "rc-"


def _raise(condition, message):
    if not condition:
        raise ValueError(message)


def _fingerprint(value):
    return verification_policy.fingerprint(value)


def _policy_path(root):
    """Путь объявленной политики: ссылка project.json либо имя у корня."""
    link = root / "ai-dev-playbook" / "project.json"
    if link.is_file():
        data = checks_trust._read_json(link, "project.json")
        rel = data.get(checks_contract.PROJECT_LINK_FIELD)
        if rel:
            return root / rel
    return root / checks_contract.POLICY_FILENAME


def _declared_policy(root):
    path = _policy_path(root)
    _raise(path.is_file() and not path.is_symlink(),
           f"verifier: файл политики не найден ({path})")
    policy = checks_trust._read_json(path, "checks-policy")
    return checks_contract.validate_policy(policy, _raise)


def _hash_file(path):
    return project_view.digest_file(path)


def candidate_manifest(root, policy, *, now=None):
    """Самостоятельный снимок кандидата: все неисключённые файлы.

    Inventory строится заново (symlink/спецфайл/конфликт исключений —
    отказ), каждый файл хешируется потоково. Дайджест, сообщённый
    агентом, не принимается: verifier сверяет байты сам (R-17).
    """
    inventory = checks_inventory.build_inventory(root, policy, now=now)
    files = []
    for rel, entry in sorted(inventory["entries"].items()):
        if entry["type"] != "file":
            continue
        files.append({"path": rel,
                      "sha256": _hash_file(Path(root) / rel)})
    # Service-исключения (work/, .git, __pycache__, .DS_Store) — runtime,
    # их появление не меняет кандидата; policy-исключения входят в
    # digest консервативно.
    manifest = {"schema_version": 1,
                "kind": "checks-candidate-manifest",
                "project_id": policy["project_id"],
                "policy_digest": _fingerprint(policy),
                "files": files,
                "excluded": [{"path": e["path"], "reason": e["reason"]}
                             for e in inventory["excluded"]
                             if e["origin"] != "service"]}
    return manifest, _fingerprint(manifest)


def _materialize(root, manifest, arena):
    """Скопировать кандидата в arena; мутация при подготовке — отказ.

    Каждый файл хешируется повторно после копирования: изменившийся
    вход обнаруживается, снимок не претендует на текущий passed (AC-07).
    """
    arena = Path(arena)
    if arena.exists():
        shutil.rmtree(arena)
    arena.mkdir(parents=True)
    for item in manifest["files"]:
        src = Path(root) / item["path"]
        dst = arena / item["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        _raise(_hash_file(src) == item["sha256"],
               f"вход {item['path']!r} изменился во время подготовки "
               "кандидата — прогон отменён")
        _raise(_hash_file(dst) == item["sha256"],
               f"снимок {item['path']!r} не воспроизводит байты "
               "кандидата")
    return arena


def _result_index_path(store):
    return store["home"] / checks_trust.RESULTS_DIR / RESULT_INDEX


def _index_results(store, run_id, results):
    """Зарегистрировать результаты прогона в доверенном индексе дома."""
    index_path = _result_index_path(store)
    index = {}
    if index_path.is_file():
        index = checks_trust._read_json(index_path, "results-index")
    run_dir = store["home"] / checks_trust.RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    for result in results:
        digest = checks_results.result_digest(result)
        rel = f"{run_id}/{result['check_id']}.json"
        checks_trust._write_json(run_dir / f"{result['check_id']}.json",
                                 result)
        index[digest] = {"path": rel, "check_id": result["check_id"],
                         "run_id": run_id}
    checks_trust._write_json(index_path, index)
    return index


def _load_result(store, result_digest):
    index_path = _result_index_path(store)
    _raise(index_path.is_file(), "verifier: индекс результатов пуст")
    index = checks_trust._read_json(index_path, "results-index")
    entry = index.get(result_digest)
    _raise(entry is not None,
           f"verifier: результат {result_digest} не найден в доме")
    path = store["home"] / checks_trust.RESULTS_DIR / entry["path"]
    doc = checks_trust._read_json(path, "result")
    checks_contract.validate_result(doc, _raise)
    _raise(checks_results.result_digest(doc) == result_digest,
           f"verifier: результат {entry['path']} не воспроизводит "
           f"digest {result_digest}")
    return doc


def _issue_receipt(store, subject, *, candidate_digest, now):
    receipt = {"schema_version": checks_contract.RECEIPT_SCHEMA_VERSION,
               "receipt_id": RECEIPT_PREFIX + uuid.uuid4().hex[:16],
               "project_id": store["doc"]["project_id"],
               "candidate_digest": candidate_digest,
               "policy_digest": store["approved"]["policy_digest"],
               "policy_revision": store["approved"]["revision"],
               "engine": dict(checks_trust.ENGINE),
               "issuer": f"verifier:{store['active_key']}",
               "issued_at": checks_trust._now(now),
               "subject": subject}
    receipt["signature"] = checks_trust.sign(
        store, {key: value for key, value in receipt.items()
                if key != "signature"})
    checks_contract.validate_receipt(receipt, _raise)
    path = store["home"] / checks_trust.RECEIPTS_DIR / \
        f"{receipt['receipt_id']}.json"
    checks_trust._write_json(path, receipt)
    checks_trust._journal(
        store, "receipt", receipt_id=receipt["receipt_id"],
        kind=subject["kind"], candidate_digest=candidate_digest,
        policy_digest=receipt["policy_digest"],
        results=len(subject["results"]))
    return receipt


def _publish_receipt(root, receipt):
    """Копия квитанции для проекта: читаемая, не являющаяся источником."""
    target = Path(root) / checks_trust.PROJECT_RECEIPTS_DIR / \
        f"{receipt['receipt_id']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    checks_trust._write_json(target, receipt)
    return target


def run(root, home, *, checkpoint=None, scope="project", mode="stale",
        task_id=None, task_content="", task_meta=None, now=None,
        emit=print):
    """Изолированный доверенный прогон; возвратить receipt и итог.

    Объявленная политика проекта сверяется с одобренной копией дома;
    runner исполняет одобренный набор на материализованном снимке с
    очищенным окружением и доступным механизмом изоляции.
    """
    root = Path(root)
    store = checks_trust.load_home(home)
    approved = checks_trust.require_approved(store)
    policy = approved["policy"]
    _raise(store["doc"]["project_id"] == policy["project_id"],
           f"дом привязан к {store['doc']['project_id']!r}, политика — "
           f"к {policy['project_id']!r}")
    declared = _declared_policy(root)
    _raise(_fingerprint(declared) == approved["policy_digest"],
           "объявленная политика проекта не совпадает с одобренной "
           f"(ожидался {approved['policy_digest']}) — выполнение "
           "под чужой политикой отклонено")
    # A-01/AUDIT-02: исходы python-unittest self-reported — тестовый
    # код делит процесс с адаптером и может подделать любой канал
    # (файл, stdout, ack). Доказанное происхождение недостижимо, поэтому
    # verifier не подтверждает такие проверки: защищённое подтверждение
    # только для наблюдаемых результатов (command). Локальный
    # `checks run` остаётся диагностическим путём.
    obligated, _manual = _obligations(policy, checkpoint)
    self_reported = [check["id"] for check in obligated
                     if check["kind"] == "test"]
    _raise(not self_reported,
           "verifier: проверки " + ", ".join(self_reported) +
           " используют адаптер python-unittest — исходы "
           "self-reported, доказанное происхождение недостижимо; "
           "verifier подтверждает только наблюдаемые результаты. "
           "Требуется command-проверка или адаптер с внешним "
           "источником фактов (ограничение PB-061)")
    manifest, candidate_digest = candidate_manifest(root, policy,
                                                    now=now)
    ticket = uuid.uuid4().hex
    arena = _materialize(
        root, manifest,
        store["home"] / checks_trust.ARENA_DIR /
        f"{candidate_digest.split(':', 1)[1][:16]}-{ticket[:8]}")
    env = checks_trust.scrub_env(arena)
    prefix = checks_trust.sandbox_prefix(root, store["home"], arena)
    checks_trust._journal(
        store, "run", ticket=ticket, candidate_digest=candidate_digest,
        scope=scope, mode=mode, checkpoint=checkpoint,
        sandbox=bool(prefix))
    emit(f"verifier run: candidate {candidate_digest} "
         f"({len(manifest['files'])} файлов), arena {arena.name}, "
         f"sandbox={'да' if prefix else 'нет'}")
    capture = io.StringIO()
    outcome = checks_runner.execute(
        arena, policy, task_id=task_id or f"verifier-{ticket[:8]}",
        scope=scope, mode=mode, checkpoint=checkpoint,
        task_content=task_content, task_meta=task_meta or {},
        env=env, argv_prefix=prefix, now=now, emit=capture.write)
    emit(capture.getvalue().rstrip())
    run_id = outcome["run_id"]
    results = outcome["results"]
    for result in results:
        checks_contract.validate_result(result, _raise)
        _raise(result["result_id"].startswith(run_id)
               and result.get("evidence", {}).get("run_id") == run_id,
               f"результат {result['result_id']} не принадлежит "
               f"прогону {run_id} — подпись чужих данных отклонена")
    _index_results(store, run_id, results)
    receipt = _issue_receipt(
        store, {"kind": "run",
                "results": _result_subject(results)},
        candidate_digest=candidate_digest, now=now)
    published = _publish_receipt(root, receipt)
    checks_trust._journal(store, "publish",
                          receipt_id=receipt["receipt_id"],
                          target=str(published))
    emit(f"verifier: receipt {receipt['receipt_id']} → "
         f"{checks_trust.PROJECT_RECEIPTS_DIR}/; "
         f"ok={outcome['ok']}")
    return {"receipt": receipt, "outcome": outcome,
            "candidate_digest": candidate_digest, "run_id": run_id,
            "log": capture.getvalue()}


def _result_subject(results):
    return [{"check_id": result["check_id"],
             "result_digest": checks_results.result_digest(result),
             "status": result["status"]}
            for result in results]


def _receipt_reasons(store, receipt, *, expect_candidate,
                     expect_policy, visited):
    """Причины недоверия квитанции; источники aggregate — рекурсивно.

    ``visited`` — множество receipt_id в текущей цепочке: циклы
    отклоняются. Ожидаемые digest применяются только к проверяемой
    квитанции, не к её историческим источникам.
    """
    reasons = []

    def fail(reason):
        reasons.append(reason)

    if receipt["project_id"] != store["doc"]["project_id"]:
        fail(f"cross-project: квитанция для "
             f"{receipt['project_id']!r}, дом привязан к "
             f"{store['doc']['project_id']!r}")
    if receipt["engine"] != checks_trust.ENGINE:
        fail(f"engine {receipt['engine']} ≠ закреплённый "
             f"{checks_trust.ENGINE}")
    try:
        checks_trust.check_signature(
            store, receipt["signature"],
            {key: value for key, value in receipt.items()
             if key != "signature"})
    except ValueError as error:
        fail(str(error))
    issued = any(
        entry.get("event") == "receipt"
        and entry.get("receipt_id") == receipt["receipt_id"]
        for entry in checks_trust.journal_entries(store))
    if not issued:
        fail(f"квитанция {receipt['receipt_id']} отсутствует в "
             "доверенном журнале выдачи")
    revoked = store["revocations"]
    if receipt["receipt_id"] in revoked["receipts"]:
        fail("квитанция отозвана")
    if receipt["policy_digest"] in revoked["policy_digests"]:
        fail("политика квитанции отозвана")
    if expect_candidate is not None \
            and receipt["candidate_digest"] != expect_candidate:
        fail("candidate_digest не совпадает — replay для другого "
             "состояния")
    if expect_policy is not None \
            and receipt["policy_digest"] != expect_policy:
        fail("policy_digest не совпадает с ожидаемой политикой")
    # A-01/AUDIT-02: квитанция не подтверждает self-reported исходы.
    # Результат разрешается в хранилище дома по digest и проверяется
    # по kind — этим же отклоняются квитанции, выпущенные до блокировки
    # run для test-проверок.
    for item in receipt["subject"].get("results") or []:
        try:
            result = _load_result(store, item["result_digest"])
        except ValueError as error:
            fail(f"результат {item.get('check_id')}: {error}")
            continue
        if result.get("kind") == "test":
            fail(f"результат {item['check_id']}: kind=test — "
                 "self-reported адаптер verifier'ом не подтверждается")
    if receipt["subject"].get("kind") == "aggregate":
        # A-04 аудита PB-061: отзыв исходной квитанции распространяется
        # на aggregate транзитивно — источник проверяется полностью,
        # включая его собственные источники (цепочка run→aggregate→…).
        for source_id in receipt["subject"].get("source_receipts") or []:
            if source_id in visited:
                fail(f"цикл источников aggregate: {source_id}")
                continue
            path = store["home"] / checks_trust.RECEIPTS_DIR / \
                f"{source_id}.json"
            if not path.is_file() or path.is_symlink():
                fail(f"источник aggregate {source_id} отсутствует "
                     "в доме")
                continue
            try:
                source = checks_trust._read_json(path, "receipt")
                checks_contract.validate_receipt(source, _raise)
            except ValueError as error:
                fail(f"источник aggregate {source_id}: формат: "
                     f"{error}")
                continue
            sub = _receipt_reasons(
                store, source, expect_candidate=None,
                expect_policy=None,
                visited=visited | {source_id})
            for reason in sub:
                fail(f"источник aggregate {source_id}: {reason}")
    return reasons


def verify(home, receipt, *, expect_candidate=None, expect_policy=None):
    """Проверить квитанцию в доверенном контуре; вернуть вердикт.

    Отказы: неверный формат, чужой project_id, иной engine, неизвестный
    или отозванный ключ, несовпавшая подпись, отсутствие выдачи в
    журнале, отозванная политика/квитанция, расхождение ожидаемых
    digest (replay/подмена состояния), недоверенный источник
    aggregate. Клиентский документ без валидной выдачи доверием не
    наделяется.
    """
    store = checks_trust.load_home(home)
    try:
        checks_contract.validate_receipt(receipt, _raise)
    except ValueError as error:
        return {"ok": False, "reasons": [f"формат: {error}"],
                "results": []}
    reasons = _receipt_reasons(
        store, receipt, expect_candidate=expect_candidate,
        expect_policy=expect_policy,
        visited={receipt["receipt_id"]})
    return {"ok": not reasons, "reasons": reasons,
            "results": receipt["subject"]["results"],
            "kind": receipt["subject"]["kind"]}


def aggregate(home, root, *, receipt_ids, task_projection=None,
              now=None, emit=print):
    """Агрегированное подтверждение текущего кандидата (reuse по R-17).

    Для каждой исходной квитанции: подпись/выдача/отзыв проверяются,
    политика обязана совпадать с текущей одобренной. Для каждой проверки
    verifier заново вычисляет входы, граф-замыкание и привязку на
    байтах текущего кандидата; старый receipt не переименовывается —
    выпускается новый ``kind=aggregate`` со ссылками на источники.
    """
    root = Path(root)
    store = checks_trust.load_home(home)
    approved = checks_trust.require_approved(store)
    policy = approved["policy"]
    task_digest = None
    projections = None
    if task_projection is not None:
        task_digest = _fingerprint(task_projection)
        projections = {"task_projection": task_projection,
                       "policy_projection": policy}
    manifest, candidate_digest = candidate_manifest(root, policy,
                                                    now=now)
    sources = []
    covered = {}
    for receipt_id in receipt_ids:
        path = store["home"] / checks_trust.RECEIPTS_DIR / \
            f"{receipt_id}.json"
        _raise(path.is_file() and not path.is_symlink(),
               f"verifier: исходная квитанция {receipt_id} не найдена "
               "в доме")
        receipt = checks_trust._read_json(path, "receipt")
        verdict = verify(home, receipt)
        _raise(verdict["ok"],
               f"исходная квитанция {receipt_id} не проходит: "
               f"{verdict['reasons']}")
        _raise(receipt["policy_digest"] == approved["policy_digest"],
               f"квитанция {receipt_id} выдана под прежней политикой — "
               "требуется новое выполнение")
        sources.append(receipt_id)
        for item in receipt["subject"]["results"]:
            covered[item["check_id"]] = item
    planned = checks_select.plan(
        root, policy, baseline=None, results=[], now=now,
        projections=projections)
    edges = planned["graph"]["edges"]
    confirmed, skipped = [], []
    for check in policy["checks"]:
        item = covered.get(check["id"])
        if item is None:
            continue
        source = _load_result(store, item["result_digest"])
        binding = source["binding"]
        inputs = checks_inventory.resolved_inputs(policy, check)
        env = checks_select.binding_environment(policy, check)
        reason = None
        try:
            snap = checks_inventory.snapshot(
                root, policy, inputs, environment=env,
                projections=projections, now=now)
            if checks_inventory.snapshot_digest(snap) != \
                    binding["input_digest"]:
                reason = "входы изменились с исходного запуска"
        except (ValueError, OSError) as error:
            reason = f"входы не доказаны: {error}"
        if reason is None:
            expected = checks_runner._binding(
                policy, check, edges, binding["input_digest"],
                task_digest, env)
            for field in set(expected) | set(binding):
                if binding.get(field) != expected.get(field):
                    reason = (f"привязка {field} не воспроизводится "
                              "на текущем кандидате")
                    break
        if reason is None and source["status"] != "passed":
            reason = f"исходный статус {source['status']}"
        if reason is not None:
            skipped.append(f"{check['id']}: {reason}")
            emit(f"  {check['id']}: пропущен — {reason}")
            continue
        confirmed.append({"check_id": check["id"],
                          "result_digest": item["result_digest"],
                          "status": "passed"})
        emit(f"  {check['id']}: пересчёт входов/привязки совпал — "
             "подтверждено")
    _raise(confirmed, "агрегирование: ни одна проверка не подтвердилась "
                      "на текущем кандидате")
    receipt = _issue_receipt(
        store, {"kind": "aggregate", "results": confirmed,
                "source_receipts": sources},
        candidate_digest=candidate_digest, now=now)
    published = _publish_receipt(root, receipt)
    checks_trust._journal(store, "aggregate",
                          receipt_id=receipt["receipt_id"],
                          sources=sources, skipped=skipped)
    emit(f"verifier: aggregate {receipt['receipt_id']} — "
         f"{len(confirmed)} проверок, пропущено {len(skipped)} → "
         f"{checks_trust.PROJECT_RECEIPTS_DIR}/")
    return {"receipt": receipt, "confirmed": confirmed,
            "skipped": skipped, "candidate_digest": candidate_digest}


def _obligations(policy, checkpoint):
    """Обязательные для кандидата проверки: исполняемые и применимые."""
    obligated, manual = [], []
    for check in policy["checks"]:
        if check["kind"] == "manual":
            manual.append(check)
            continue
        applicable, _ = checks_select._check_applicability(
            check.get("applicability") or "always", None, checkpoint)
        if applicable:
            obligated.append(check)
    return obligated, manual


def gate(root, home, *, checkpoint=None, emit=print):
    """Эталонный consumer: покрытие обязательств валидными квитанциями.

    Receipt принимается только при совпадении candidate/policy digest
    текущего состояния; локальные надписи и файлы проекта доверия не
    создают. ``protected_confirmed`` истинно только при доказанной
    границе OS identity — иначе честный диагностический вердикт.
    """
    root = Path(root)
    report = checks_trust.probe(root, home)
    verdict = {"ok": False, "capability": report["capability"],
               "protected_confirmed": False, "obligated": 0,
               "covered": 0, "uncovered": [], "manual": [],
               "receipts": [], "rejections": [],
               "notes": list(report["notes"])}
    if not report["configured"]:
        verdict["rejections"].append("verifier home не настроен — "
                                     "покрытие недоказано")
        return verdict
    store = checks_trust.load_home(home)
    try:
        approved = checks_trust.require_approved(store)
    except ValueError as error:
        verdict["rejections"].append(str(error))
        return verdict
    policy = approved["policy"]
    manifest, candidate_digest = candidate_manifest(root, policy)
    verdict["candidate_digest"] = candidate_digest
    obligated, manual = _obligations(policy, checkpoint)
    verdict["obligated"] = len(obligated)
    verdict["manual"] = [check["id"] for check in manual]
    verified = {}
    receipts_dir = root / checks_trust.PROJECT_RECEIPTS_DIR
    if receipts_dir.is_dir():
        for path in sorted(receipts_dir.glob("*.json")):
            if path.is_symlink():
                verdict["rejections"].append(
                    f"{path.name}: символическая ссылка — пропущено")
                continue
            try:
                receipt = checks_trust._read_json(path, "receipt")
            except ValueError as error:
                verdict["rejections"].append(f"{path.name}: {error}")
                continue
            result = verify(
                home, receipt, expect_candidate=candidate_digest,
                expect_policy=approved["policy_digest"])
            if not result["ok"]:
                verdict["rejections"].append(
                    f"{path.name}: {'; '.join(result['reasons'])}")
                continue
            verdict["receipts"].append(receipt["receipt_id"])
            for item in result["results"]:
                if item["status"] == "passed" \
                        and item["check_id"] not in verified:
                    verified[item["check_id"]] = receipt["receipt_id"]
    for check in obligated:
        if check["id"] in verified:
            verdict["covered"] += 1
        else:
            verdict["uncovered"].append(check["id"])
    verdict["ok"] = not verdict["uncovered"] \
        and not verdict["rejections"]
    verdict["protected_confirmed"] = \
        verdict["ok"] and report["capability"] == "protected"
    if verdict["ok"] and report["capability"] != "protected":
        verdict["notes"].append(
            "покрытие подтверждено, но граница OS identity не доказана "
            "— диагностический local, защищённая эксплуатация не "
            "заявляется")
    emit(f"verifier gate: capability={report['capability']} "
         f"isolation={report['isolation']} "
         f"candidate={candidate_digest}")
    emit(f"  обязательств {verdict['obligated']}, покрыто "
         f"{verdict['covered']}, квитанций {len(verdict['receipts'])}")
    for check_id in verdict["uncovered"]:
        emit(f"  не покрыто: {check_id}")
    for check_id in verdict["manual"]:
        emit(f"  ручная проверка вне контура: {check_id}")
    for reason in verdict["rejections"]:
        emit(f"  отказ: {reason}")
    for note in verdict["notes"]:
        emit(f"  примечание: {note}")
    return verdict
