"""Контракт совместной приёмки блока задач (PB-065).

Версии схем, политика приёмки, ссылки карточек, форматы review package,
findings report и block receipt, общий evaluator графа зависимостей.
Runner подготовки review, create-fix и групповое закрытие реализуются
последующими карточками блока; здесь — контракты и валидаторы.

Политика приёмки — единственный источник в метаданных plan-документа:
``acceptance_policy`` доступна только при явной capability
``block_acceptance`` в ``project.json → documents.capabilities``.
Проект без capability сохраняет прежние строгие зависимости; неизвестные
версии схем отклоняются, а не игнорируются.

``acceptance_policy`` (schema_version 1):
- ``policy_id``: стабильный slug ``[a-z0-9][a-z0-9_-]*``, неизменяемый;
- ``revision``: положительное целое; правка политики — новая ревизия;
- ``mode``: ``per_task`` (отдельная приёмка каждой задачи), ``final``
  (последовательная реализация по проверенным передачам, итоговый аудит)
  или ``mixed`` (передачи внутри этапа + независимая приёмка checkpoint);
- ``approved_basis``: ``{who, at, source, detail}`` — основание инженера;
- ``members``: непустой список ID задач без повторов; задача состоит
  не более чем в одном активном блоке;
- ``final_audit``: ID карточки итогового аудита, не из members;
- ``checkpoints``: для mixed — разбиение members на этапы ``{id, tasks}``,
  каждый member ровно в одном checkpoint; для per_task/final — отсутствует;
- ``implementation_edges``: ``{from, to, required}``, единственный вид
  ``required=verified_handoff``; только для final/mixed, ациклично,
  не дублирует пару из Depends-On.

Карточка ссылается на политику полем ``block`` (schema_version 1):
``{document, policy_id, revision, role?, fix?}``. ``role`` —
``member | final_audit | fix``; без ``role`` принадлежность выводится
из политики. ``fix`` — ``{report_id, iteration, findings}`` — только
у ``role=fix``: исправление ссылается на протокол находок, а не на done
итогового AUDIT.

Depends-On сохраняет семантику done для внешних и строгих внутренних
рёбер. Противоречия отклоняются: одна и та же упорядоченная пара не может
быть одновременно Depends-On и implementation_edge; в режимах final/mixed
строгое ребро, инцидентное final_audit (в любую сторону), запрещено —
иначе цикл «AUDIT ждёт done членов, члены ждут приёмки блока»; у fix-карты
строгих рёбер внутри блока нет в любом режиме.

Общий evaluator: ``evaluate(operation, task, policy, records, evidence)``
→ ``{allowed, reason_codes, obligations, consumed_receipts}``.
Потребители: ``check``/``read_tasks``, ``lifecycle start|revise``,
``verify``/``handoff``, ``verify --audit-ready``, ``lifecycle accept``,
``close`` и ``lifecycle plan/apply`` (оценка будущего состояния), а также
``block status`` (диагностика). Упрощённые обходные проверки отдельных
команд не допускаются. Виды readiness: implementation (start/check/
handoff), audit (audit_ready), acceptance (accept), close (close/plan).

``evidence`` — факты, собранные вызывающей стороной:
- ``handoffs``: ``{task_id: {"current": bool, "id": str|None}}`` —
  актуальность проверенной передачи (journal/CLI-привязка);
- ``checkpoints``: ``{cp_id: {"accepted": bool, "receipt": str|None}}``;
- ``findings``: ``[{"id": str, "resolved": bool}]`` — находки блока;
- ``fixes``: ``{task_id: {"accepted": bool}}`` — приёмка исправлений;
- ``review_package``: ``{"candidate_sha256": str}`` либо None;
- ``block_receipt``: ``{"candidate_sha256": str, "accepted": bool}``;
- ``candidate_sha256``: отпечаток конечного candidate либо None;
- ``acceptances``: ``{task_id: object}`` — индивидуальные приёмки.

Форматы (все — JSON-объекты с ``schema_version`` 1, неизвестные поля
отклоняются):
- review package: immutable пакет конечного candidate у владельца AUDIT —
  ``{package_id, block{document, policy_id, revision}, policy_sha256,
  members, receipts, inputs, environment, matrix, candidate_sha256,
  created_at}``; matrix — ``[{task, criterion, result, report, evidence}]``;
- findings report: отрицательный протокол итогового аудита —
  ``{report_id, iteration, block{...}, author{name, model}, session,
  verdict="changes_requested", candidate_sha256, at, findings[]}``;
  finding — ``{id, source_tasks, affected_ac, reproducer, evidence,
  expected, severity}``;
- block receipt: общая квитанция приёмки — ``{receipt_id, block{...},
  package{id, sha256}, policy_revision, candidate_sha256,
  verdict{actor, at, results}, acceptances, checkpoints, at}``.
"""

import re

import documents


POLICY_SCHEMA_VERSION = 1
CARD_BLOCK_VERSION = 1
REVIEW_PACKAGE_VERSION = 1
FINDINGS_REPORT_VERSION = 1
BLOCK_RECEIPT_VERSION = 1

CAPABILITY = "block_acceptance"
KNOWN_CAPABILITIES = {CAPABILITY}

MODES = {"per_task", "final", "mixed"}
EDGE_REQUIRED = {"verified_handoff"}
BLOCK_ROLES = {"member", "final_audit", "fix"}

# Операции, оцениваемые общим evaluator; потребители перечислены в docstring.
OPERATIONS = ("start", "revise", "check", "handoff", "audit_ready",
              "accept", "close", "plan")

BLOCK_STATUSES = ("awaiting_work", "awaiting_checkpoint", "review_ready",
                  "changes_requested", "accepted", "archived")

# Стабильные коды причин evaluator; тексты объяснений — за потребителем.
POLICY_MISSING = "policy_missing"
POLICY_STALE = "policy_stale"
DEPENDENCY_NOT_DONE = "dependency_not_done"
DEPENDENCY_CANCELLED = "dependency_cancelled"
PREDECESSOR_HANDOFF_MISSING = "predecessor_handoff_missing"
PREDECESSOR_HANDOFF_STALE = "predecessor_handoff_stale"
CHECKPOINT_PENDING = "checkpoint_pending"
FINDINGS_OPEN = "findings_open"
FIX_PENDING = "fix_pending"
REVIEW_PACKAGE_MISSING = "review_package_missing"
CANDIDATE_STALE = "candidate_stale"
BLOCK_ACCEPTANCE_REQUIRED = "block_acceptance_required"
INDIVIDUAL_AUDIT_NOT_USED = "individual_audit_not_used"
HANDOFF_NOT_CURRENT = "handoff_not_current"
TASK_NOT_ACCEPTED = "task_not_accepted"

TASK_ID = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-[0-9]+\Z")
DOC_ID = re.compile(r"[a-z][a-z0-9_-]*:[a-z0-9][a-z0-9_-]*\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
POLICY_ID = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
CHECKPOINT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
FINDING_ID = re.compile(r"F-[0-9]{2,}\Z")
REPORT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")

POLICY_FIELDS = {"schema_version", "policy_id", "revision", "mode",
                 "approved_basis", "members", "final_audit",
                 "checkpoints", "implementation_edges"}
POLICY_BASIS_FIELDS = {"who", "at", "source", "detail"}
CHECKPOINT_FIELDS = {"id", "tasks"}
EDGE_FIELDS = {"from", "to", "required"}

CARD_BLOCK_FIELDS = {"schema_version", "document", "policy_id",
                     "revision", "role", "fix"}
FIX_FIELDS = {"report_id", "iteration", "findings"}

REVIEW_PACKAGE_FIELDS = {"schema_version", "package_id", "block",
                         "policy_sha256", "members", "receipts", "inputs",
                         "environment", "matrix", "candidate_sha256",
                         "created_at"}
MATRIX_FIELDS = {"task", "criterion", "result", "report", "evidence"}
FINDINGS_REPORT_FIELDS = {"schema_version", "report_id", "iteration",
                          "block", "author", "session", "verdict",
                          "candidate_sha256", "at", "findings"}
FINDING_FIELDS = {"id", "source_tasks", "affected_ac", "reproducer",
                  "evidence", "expected", "severity"}
RECEIPT_FIELDS = {"schema_version", "receipt_id", "block", "package",
                  "policy_revision", "candidate_sha256", "verdict",
                  "acceptances", "checkpoints", "at"}
RECEIPT_PACKAGE_FIELDS = {"id", "sha256"}
RECEIPT_VERDICT_FIELDS = {"actor", "at", "results"}
BLOCK_REF_FIELDS = {"document", "policy_id", "revision"}
SEVERITIES = {"P0", "P1", "P2", "P3"}


def _err(errors, label, message, field=None):
    if field is not None:
        errors.append(f"{label}: поле '{field}': {message}")
    else:
        errors.append(f"{label}: {message}")


def _iso(value):
    return documents._iso(value)


def _fields(obj, allowed, required, label, errors, field=None):
    if not isinstance(obj, dict):
        _err(errors, label, "ожидается объект", field=field)
        return False
    unknown = sorted(set(obj) - allowed)
    if unknown:
        _err(errors, label, f"неизвестные поля {unknown}", field=field)
    missing = sorted(required - set(obj))
    if missing:
        _err(errors, label, f"отсутствуют поля {missing}", field=field)
    return not unknown and not missing


def _task_id_list(value, label, errors, field):
    if not isinstance(value, list) or not value:
        _err(errors, label, "ожидается непустой список ID задач", field=field)
        return []
    result = []
    for item in value:
        if not isinstance(item, str) or TASK_ID.fullmatch(item) is None:
            _err(errors, label, f"некорректный ID задачи {item!r}", field=field)
            continue
        result.append(item)
    if len(result) != len(set(result)):
        _err(errors, label, "повторный ID задачи", field=field)
    return result


def _block_ref(value, label, errors, field):
    """Ссылка на политику {document, policy_id, revision} → dict | None.

    Набор полей объекта проверяет вызывающий валидатор; здесь — типы."""
    if not isinstance(value, dict):
        _err(errors, label, "ожидается объект", field=field)
        return None
    target = value.get("document")
    if not isinstance(target, str) or DOC_ID.fullmatch(target) is None \
            or not target.startswith("plan:"):
        _err(errors, label, "document ожидается 'plan:<slug>'", field=field)
        target = None
    policy_id = value.get("policy_id")
    if not isinstance(policy_id, str) or POLICY_ID.fullmatch(policy_id) is None:
        _err(errors, label, f"некорректный policy_id {policy_id!r}", field=field)
        policy_id = None
    revision = value.get("revision")
    if type(revision) is not int or revision < 1:
        _err(errors, label, "revision — положительное целое", field=field)
    return {"document": target, "policy_id": policy_id, "revision": revision}


def validate_policy(policy, label, errors):
    """Структурная валидация acceptance_policy → нормализованный dict | None.

    Самодостаточные проверки графа (циклы implementation_edges, разбиение
    checkpoints) выполняются здесь; ссылки на карточки и междокументные
    конфликты — в validate_block_graph.
    """
    if not _fields(policy, POLICY_FIELDS,
                   POLICY_FIELDS - {"checkpoints", "implementation_edges"},
                   label, errors, field="acceptance_policy"):
        return None
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        _err(errors, label,
             f"неизвестная версия {policy.get('schema_version')!r}",
             field="acceptance_policy.schema_version")
    policy_id = policy.get("policy_id")
    if not isinstance(policy_id, str) or POLICY_ID.fullmatch(policy_id) is None:
        _err(errors, label, f"некорректный policy_id {policy_id!r}",
             field="acceptance_policy.policy_id")
        policy_id = None
    revision = policy.get("revision")
    if type(revision) is not int or revision < 1:
        _err(errors, label, "revision — положительное целое",
             field="acceptance_policy.revision")
    mode = policy.get("mode")
    if mode not in MODES:
        _err(errors, label, f"mode ожидается {sorted(MODES)}",
             field="acceptance_policy.mode")
    basis = policy.get("approved_basis")
    if _fields(basis, POLICY_BASIS_FIELDS, {"who", "at", "source"}, label,
               errors, field="acceptance_policy.approved_basis"):
        if not documents._str(basis.get("who")):
            _err(errors, label, "who — непустая строка",
                 field="acceptance_policy.approved_basis")
        if _iso(basis.get("at")) is None:
            _err(errors, label, "at — ISO-время с часовым поясом",
                 field="acceptance_policy.approved_basis")
        if not documents._str(basis.get("source")):
            _err(errors, label, "source — непустая строка",
                 field="acceptance_policy.approved_basis")
        if "detail" in basis and not documents._str(basis.get("detail")):
            _err(errors, label, "detail — непустая строка",
                 field="acceptance_policy.approved_basis")
    members = _task_id_list(policy.get("members"), label, errors,
                            "acceptance_policy.members")
    final_audit = policy.get("final_audit")
    if not isinstance(final_audit, str) or TASK_ID.fullmatch(final_audit) is None:
        _err(errors, label, f"некорректный final_audit {final_audit!r}",
             field="acceptance_policy.final_audit")
        final_audit = None
    elif final_audit in members:
        _err(errors, label, "final_audit не входит в members",
             field="acceptance_policy.final_audit")
    checkpoints = policy.get("checkpoints")
    if checkpoints is None:
        checkpoints = []
    if not isinstance(checkpoints, list):
        _err(errors, label, "ожидается список",
             field="acceptance_policy.checkpoints")
        checkpoints = []
    cp_ids, covered = set(), set()
    for index, cp in enumerate(checkpoints):
        item_label = f"acceptance_policy.checkpoints[{index}]"
        if not _fields(cp, CHECKPOINT_FIELDS, CHECKPOINT_FIELDS, label, errors,
                       field=item_label):
            continue
        cp_id = cp.get("id")
        if not isinstance(cp_id, str) or CHECKPOINT_ID.fullmatch(cp_id) is None:
            _err(errors, label, f"некорректный id checkpoint {cp_id!r}",
                 field=item_label)
        elif cp_id in cp_ids:
            _err(errors, label, f"повторный id checkpoint '{cp_id}'",
                 field=item_label)
        else:
            cp_ids.add(cp_id)
        for task in _task_id_list(cp.get("tasks"), label, errors,
                                  f"{item_label}.tasks"):
            if task not in members:
                _err(errors, label, f"checkpoint '{cp_id}': '{task}' "
                     "не из members", field=item_label)
            elif task in covered:
                _err(errors, label, f"'{task}' в нескольких checkpoints — "
                     "повторное членство", field=item_label)
            covered.add(task)
    if mode == "mixed":
        if not checkpoints:
            _err(errors, label, "mixed требует непустые checkpoints",
                 field="acceptance_policy.checkpoints")
        missing = [task for task in members if task not in covered]
        if missing:
            _err(errors, label, "mixed: members вне checkpoints "
                 f"{missing} — неполное покрытие",
                 field="acceptance_policy.checkpoints")
    elif checkpoints:
        _err(errors, label, f"checkpoints недопустимы при mode '{mode}'",
             field="acceptance_policy.checkpoints")
    edges = policy.get("implementation_edges")
    if edges is None:
        edges = []
    if not isinstance(edges, list):
        _err(errors, label, "ожидается список",
             field="acceptance_policy.implementation_edges")
        edges = []
    pairs = set()
    adjacency = {task: [] for task in members}
    for index, edge in enumerate(edges):
        item_label = f"acceptance_policy.implementation_edges[{index}]"
        if not _fields(edge, EDGE_FIELDS, EDGE_FIELDS, label, errors,
                       field=item_label):
            continue
        ends = []
        for key in ("from", "to"):
            value = edge.get(key)
            if not isinstance(value, str) or TASK_ID.fullmatch(value) is None:
                _err(errors, label, f"некорректный '{key}' {value!r}",
                     field=item_label)
                value = None
            elif value not in members:
                _err(errors, label, f"'{key}' '{value}' не из members",
                     field=item_label)
                value = None
            ends.append(value)
        if edge.get("required") not in EDGE_REQUIRED:
            _err(errors, label, f"required ожидается {sorted(EDGE_REQUIRED)}",
                 field=item_label)
        if ends[0] is not None and ends[0] == ends[1]:
            _err(errors, label, "ребро в саму задачу", field=item_label)
        if all(ends):
            pair = (ends[0], ends[1])
            if pair in pairs:
                _err(errors, label, f"повторное ребро {ends[0]} → {ends[1]}",
                     field=item_label)
            pairs.add(pair)
            adjacency[ends[0]].append(ends[1])
    if mode == "per_task" and edges:
        _err(errors, label, "implementation_edges недопустимы при per_task — "
             "порядок задаёт строгий Depends-On",
             field="acceptance_policy.implementation_edges")
    # Ацикличность implementation_edges (DFS без рекурсии).
    state = {}
    for first in adjacency:
        if state.get(first) == "done":
            continue
        pending, active = [(first, False)], set()
        while pending:
            node, finished = pending.pop()
            if finished:
                active.discard(node)
                state[node] = "done"
            elif state.get(node) != "done":
                if node in active:
                    _err(errors, label, f"цикл implementation_edges: {node}",
                         field="acceptance_policy.implementation_edges")
                    for mark in active:
                        state[mark] = "done"
                    break
                active.add(node)
                pending.append((node, True))
                pending.extend((dep, False) for dep in reversed(adjacency[node]))
    return {"schema_version": policy.get("schema_version"),
            "policy_id": policy_id, "revision": revision, "mode": mode,
            "approved_basis": basis if isinstance(basis, dict) else None,
            "members": members, "final_audit": final_audit,
            "checkpoints": checkpoints, "implementation_edges": edges}


def validate_card_block(value, label, errors):
    """Поле ``block`` карточки → нормализованный dict | None."""
    if not _fields(value, CARD_BLOCK_FIELDS,
                   CARD_BLOCK_FIELDS - {"role", "fix"}, label, errors,
                   field="block"):
        return None
    if value.get("schema_version") != CARD_BLOCK_VERSION:
        _err(errors, label,
             f"неизвестная версия {value.get('schema_version')!r}",
             field="block.schema_version")
    ref = _block_ref(value, label, errors, "block")
    role = value.get("role")
    if role is not None and role not in BLOCK_ROLES:
        _err(errors, label, f"role ожидается {sorted(BLOCK_ROLES)}",
             field="block.role")
    fix = value.get("fix")
    if fix is not None:
        if _fields(fix, FIX_FIELDS, FIX_FIELDS, label, errors,
                   field="block.fix"):
            if not documents._str(fix.get("report_id")) \
                    or REPORT_ID.fullmatch(fix["report_id"]) is None:
                _err(errors, label, "report_id — стабильный ID протокола",
                     field="block.fix")
            if type(fix.get("iteration")) is not int or fix["iteration"] < 1:
                _err(errors, label, "iteration — положительное целое",
                     field="block.fix")
            findings = fix.get("findings")
            if not isinstance(findings, list) or not findings \
                    or any(not isinstance(f, str)
                           or FINDING_ID.fullmatch(f) is None for f in findings) \
                    or len(findings) != len(set(findings)):
                _err(errors, label, "findings — непустой список "
                     "уникальных F-NN", field="block.fix")
    if (fix is None) == (role == "fix"):
        _err(errors, label, "role 'fix' и объект fix задаются вместе",
             field="block")
    return {"schema_version": value.get("schema_version"), **(ref or {}),
            "role": role, "fix": fix if isinstance(fix, dict) else None}


def _dep_pairs(policy):
    return {(e["from"], e["to"])
            for e in policy.get("implementation_edges") or []}


def validate_block_graph(policies, blocks, records, errors):
    """Междокументные проверки политики и ссылок карточек.

    ``policies``: ``{document_id: {"policy": <validate_policy>, "path": str,
    "archived": bool}}`` — уже структурно валидные активные/архивные планы.
    ``blocks``: ``{task_id: <validate_card_block>}`` — ссылки карточек.
    ``records``: ``{task_id: {"status", "deps", "archived"}}`` — все карточки.
    """
    # Карточки каждого активного члена/аудита обязаны ссылаться на политику;
    # ссылка обязана указывать на существующую политику нужной ревизии.
    member_of = {}
    for doc_id, entry in policies.items():
        policy, archived = entry["policy"], entry["archived"]
        label = entry["path"]
        if policy is None:
            continue
        for task in [*policy["members"],
                     *([policy["final_audit"]] if policy["final_audit"] else [])]:
            if task not in records:
                _err(errors, label, f"политика ссылается на отсутствующую "
                     f"карточку '{task}'", field="acceptance_policy")
                continue
            if task in member_of and member_of[task] != doc_id:
                _err(errors, label, f"'{task}' уже входит в блок "
                     f"'{member_of[task]}' — повторное членство в активных "
                     "блоках", field="acceptance_policy")
            elif not archived:
                member_of[task] = doc_id
            ref = blocks.get(task)
            if archived:
                continue
            if ref is None:
                _err(errors, str(records[task]["path"]),
                     f"карточка входит в политику '{doc_id}', но не имеет "
                     "ссылки block", field="block")
    for task, ref in blocks.items():
        label = str(records[task]["path"])
        if ref is None:
            continue
        doc_id = ref.get("document")
        entry = policies.get(doc_id)
        if entry is None or entry["policy"] is None:
            _err(errors, label, f"ссылка на неизвестную политику "
             f"'{doc_id}'", field="block")
            continue
        policy = entry["policy"]
        if entry["archived"] and not records[task]["archived"]:
            _err(errors, label, f"политика '{doc_id}' архивирована, "
                 "а карточка активна", field="block")
        if ref.get("policy_id") != policy.get("policy_id"):
            _err(errors, label, f"policy_id '{ref.get('policy_id')}' не "
                 f"совпадает с '{policy.get('policy_id')}'", field="block")
        if ref.get("revision") != policy.get("revision"):
            _err(errors, label, f"revision {ref.get('revision')} не "
                 f"соответствует политике {policy.get('revision')} — "
                 "ссылка устарела", field="block")
        role = ref.get("role")
        is_member = task in policy["members"]
        is_audit = task == policy["final_audit"]
        if role is None and not (is_member or is_audit):
            _err(errors, label, f"карточка не входит в members/"
                 f"final_audit политики '{doc_id}'", field="block")
        elif role == "member" and not is_member:
            _err(errors, label, f"карточка не входит в members "
                 f"политики '{doc_id}'", field="block")
        elif role == "final_audit" and not is_audit:
            _err(errors, label, "role 'final_audit' не совпадает "
                 "с политикой", field="block")
        elif role == "fix" and (is_member or is_audit):
            _err(errors, label, "fix не может быть member/final_audit",
                 field="block")
    # Строгие рёбра: противоречия с implementation_edges, запрет инцидентных
    # final_audit/fix ребер в final/mixed; совместная ацикличность.
    for doc_id, entry in policies.items():
        policy = entry["policy"]
        if policy is None:
            continue
        label = entry["path"]
        members = set(policy["members"])
        audit = policy["final_audit"]
        impl = _dep_pairs(policy)
        inside = members | ({audit} if audit else set())
        fixes = {task for task, ref in blocks.items()
                 if ref and ref.get("document") == doc_id
                 and ref.get("role") == "fix"}
        dep_edges = set()
        for task in inside | fixes:
            rec = records.get(task)
            if rec is None:
                continue
            for dep in rec["deps"]:
                if dep in inside or dep in fixes:
                    dep_edges.add((dep, task))
                    if (dep, task) in impl:
                        _err(errors, label, f"{dep} → {task}: одна пара не "
                             "может быть Depends-On и implementation_edge",
                             field="acceptance_policy")
        if policy["mode"] in {"final", "mixed"}:
            for dep, task in sorted(dep_edges):
                if audit in (dep, task):
                    _err(errors, label, f"{dep} → {task}: строгое ребро "
                         "инцидентно final_audit — цикл AUDIT ↔ members/fix "
                         "запрещён; связь задаёт политика",
                         field="acceptance_policy")
        for dep, task in sorted(dep_edges):
            if dep in fixes or task in fixes:
                _err(errors, label, f"{dep} → {task}: у исправления нет "
                     "строгих ребер внутри блока — связь через report/"
                     "iteration", field="acceptance_policy")
        graph = {}
        for dep, task in dep_edges | impl:
            graph.setdefault(dep, []).append(task)
        state = {}
        for first in graph:
            if state.get(first) == "done":
                continue
            pending, active = [(first, False)], set()
            while pending:
                node, finished = pending.pop()
                if finished:
                    active.discard(node)
                    state[node] = "done"
                elif state.get(node) != "done":
                    if node in active:
                        _err(errors, label, f"цикл графа блока: {node}",
                             field="acceptance_policy")
                        for mark in active:
                            state[mark] = "done"
                        break
                    active.add(node)
                    pending.append((node, True))
                    pending.extend((d, False)
                                   for d in reversed(graph.get(node, [])))


def _deny(codes, obligations=(), receipts=()):
    return {"allowed": False, "reason_codes": list(codes),
            "obligations": list(obligations),
            "consumed_receipts": list(receipts)}


def _allow(obligations=(), receipts=()):
    return {"allowed": True, "reason_codes": [],
            "obligations": list(obligations),
            "consumed_receipts": list(receipts)}


def _obligation(code, subject, detail):
    return {"code": code, "subject": subject, "detail": detail}


def _strict_deps(task, records):
    """Depends-On сохраняет done-семантику для внешних и строгих ребер."""
    blocked, cancelled = [], []
    for dep in task.get("deps") or []:
        status = (records.get(dep) or {}).get("status")
        if status == "cancelled":
            cancelled.append(dep)
        elif status != "done":
            blocked.append(dep)
    return blocked, cancelled


def _handoff_current(evidence, task_id):
    record = (evidence.get("handoffs") or {}).get(task_id)
    return bool(record and record.get("current"))


def _predecessors(policy, task_id):
    return [edge["from"] for edge in policy.get("implementation_edges") or []
            if edge["to"] == task_id]


def _checkpoint_of(policy, task_id):
    for cp in policy.get("checkpoints") or []:
        if task_id in (cp.get("tasks") or []):
            return cp
    return None


def _candidate_current(evidence, source):
    recorded = (evidence.get(source) or {}).get("candidate_sha256")
    current = evidence.get("candidate_sha256")
    return recorded is None or current is None or recorded == current


def _implementation_ready(task, policy, records, evidence):
    """Readiness реализации: строгие зависимости + проверенные передачи."""
    codes, obligations, receipts = [], [], []
    blocked, cancelled = _strict_deps(task, records)
    for dep in blocked:
        codes.append(DEPENDENCY_NOT_DONE)
        obligations.append(_obligation(
            DEPENDENCY_NOT_DONE, dep, "Depends-On требует done"))
    for dep in cancelled:
        codes.append(DEPENDENCY_CANCELLED)
        obligations.append(_obligation(
            DEPENDENCY_CANCELLED, dep, "предшественник отменён"))
    if policy is None or task["id"] not in policy["members"]:
        return (codes, obligations, receipts)
    for pred in _predecessors(policy, task["id"]):
        pred_status = (records.get(pred) or {}).get("status")
        if pred_status == "done":
            continue
        if pred_status == "cancelled":
            codes.append(DEPENDENCY_CANCELLED)
            obligations.append(_obligation(
                DEPENDENCY_CANCELLED, pred, "предшественник отменён"))
            continue
        if _handoff_current(evidence, pred):
            handoff = evidence["handoffs"][pred]
            receipts.append({"kind": "handoff", "task": pred,
                             "reference": handoff.get("id")})
        elif (evidence.get("handoffs") or {}).get(pred):
            codes.append(PREDECESSOR_HANDOFF_STALE)
            obligations.append(_obligation(
                PREDECESSOR_HANDOFF_STALE, pred,
                "передача предшественника неактуальна — повторная проверка"))
        else:
            codes.append(PREDECESSOR_HANDOFF_MISSING)
            obligations.append(_obligation(
                PREDECESSOR_HANDOFF_MISSING, pred,
                "нет проверенной передачи предшественника"))
        if policy["mode"] == "mixed":
            boundary = _checkpoint_of(policy, pred)
            here = _checkpoint_of(policy, task["id"])
            if boundary is not None and boundary is not here:
                state = (evidence.get("checkpoints") or {}).get(
                    boundary["id"], {})
                if state.get("accepted"):
                    receipts.append({"kind": "checkpoint",
                                     "id": boundary["id"],
                                     "reference": state.get("receipt")})
                else:
                    codes.append(CHECKPOINT_PENDING)
                    obligations.append(_obligation(
                        CHECKPOINT_PENDING, boundary["id"],
                        "checkpoint требует независимой приёмки перед "
                        "следующим этапом"))
    return codes, obligations, receipts


def evaluate(operation, task, policy, records, evidence):
    """Общий evaluator графа зависимостей → {allowed, reason_codes,
    obligations, consumed_receipts}.

    ``operation`` ∈ OPERATIONS; ``task`` — {id, status, deps, block?};
    ``policy`` — валидированная acceptance_policy карточки либо None;
    ``records`` — {id: {status, deps, archived}}; ``evidence`` — факты по
    контракту из docstring модуля. Evaluator не запускает проверки и не
    пишет файлы; сбор evidence — обязанность вызывающей команды.
    """
    if operation not in OPERATIONS:
        raise ValueError(f"evaluate: неизвестная операция {operation!r}")
    evidence = evidence or {}
    task_id = task["id"]
    role = None
    if policy is not None:
        if task_id in policy["members"]:
            role = "member"
        elif task_id == policy["final_audit"]:
            role = "final_audit"
        elif (task.get("block") or {}).get("role") == "fix":
            role = "fix"
        else:
            return _deny([POLICY_MISSING], [_obligation(
                POLICY_MISSING, task_id,
                "карточка вне members/final_audit политики")])
    if policy is not None and (task.get("block") or {}).get("revision") \
            not in (None, policy.get("revision")):
        return _deny([POLICY_STALE], [_obligation(
            POLICY_STALE, task_id, "revision ссылки устарела")])
    mode = policy["mode"] if policy is not None else None

    if operation in {"start", "revise", "check", "handoff"}:
        if operation == "revise":
            # Revise устраняет stale/failed, а не проходит gate заново.
            return _allow()
        codes, obligations, receipts = _implementation_ready(
            task, policy if role == "member" else None, records, evidence)
        if role == "final_audit":
            if mode == "per_task":
                missing = [m for m in policy["members"]
                           if (records.get(m) or {}).get("status") != "done"]
                for m in missing:
                    codes.append(DEPENDENCY_NOT_DONE)
                    obligations.append(_obligation(
                        DEPENDENCY_NOT_DONE, m,
                        "per_task: member должен быть done"))
            else:
                for m in policy["members"]:
                    if (records.get(m) or {}).get("status") == "done":
                        continue
                    if _handoff_current(evidence, m):
                        receipts.append({"kind": "handoff", "task": m,
                                         "reference": evidence[
                                             "handoffs"][m].get("id")})
                    else:
                        codes.append(PREDECESSOR_HANDOFF_MISSING)
                        obligations.append(_obligation(
                            PREDECESSOR_HANDOFF_MISSING, m,
                            "нет актуальной проверенной передачи"))
                for cp in policy.get("checkpoints") or []:
                    state = (evidence.get("checkpoints") or {}).get(
                        cp["id"], {})
                    if state.get("accepted"):
                        receipts.append({"kind": "checkpoint",
                                         "id": cp["id"],
                                         "reference": state.get("receipt")})
                    else:
                        codes.append(CHECKPOINT_PENDING)
                        obligations.append(_obligation(
                            CHECKPOINT_PENDING, cp["id"],
                            "checkpoint не принят"))
        if codes:
            return _deny(codes, obligations, receipts)
        return _allow(obligations, receipts)

    if operation == "audit_ready":
        if role == "member" and mode in {"final", "mixed"}:
            return _deny([INDIVIDUAL_AUDIT_NOT_USED], [_obligation(
                INDIVIDUAL_AUDIT_NOT_USED, task_id,
                "member блока не проходит отдельный аудит — итоговый "
                "аудит блока")])
        if role == "final_audit" and mode in {"final", "mixed"}:
            readiness = evaluate("check", task, policy, records, evidence)
            if not _review_ready(evidence, policy):
                readiness["allowed"] = False
                readiness["reason_codes"].append(REVIEW_PACKAGE_MISSING)
                readiness["obligations"].append(_obligation(
                    REVIEW_PACKAGE_MISSING, task_id,
                    "нет immutable review package конечного candidate"))
            return readiness
        if not _handoff_current(evidence, task_id):
            return _deny([HANDOFF_NOT_CURRENT], [_obligation(
                HANDOFF_NOT_CURRENT, task_id,
                "нет актуальной проверенной передачи")])
        return _allow()

    if operation == "accept":
        if role == "member" and mode in {"final", "mixed"}:
            return _deny([BLOCK_ACCEPTANCE_REQUIRED], [_obligation(
                BLOCK_ACCEPTANCE_REQUIRED, task_id,
                "member принимается общей квитанцией блока, не "
                "индивидуально")])
        if role == "final_audit" and mode in {"final", "mixed"}:
            codes, obligations = [], []
            if not _review_ready(evidence, policy):
                codes.append(REVIEW_PACKAGE_MISSING)
                obligations.append(_obligation(
                    REVIEW_PACKAGE_MISSING, task_id,
                    "review package отсутствует или устарел"))
            for finding in evidence.get("findings") or []:
                if not finding.get("resolved"):
                    codes.append(FINDINGS_OPEN)
                    obligations.append(_obligation(
                        FINDINGS_OPEN, finding.get("id"),
                        "находка не устранена"))
            for fix_id, state in (evidence.get("fixes") or {}).items():
                if not state.get("accepted"):
                    codes.append(FIX_PENDING)
                    obligations.append(_obligation(
                        FIX_PENDING, fix_id, "исправление не принято"))
            if codes:
                return _deny(codes, obligations)
            return _allow()
        if not _handoff_current(evidence, task_id):
            return _deny([HANDOFF_NOT_CURRENT], [_obligation(
                HANDOFF_NOT_CURRENT, task_id,
                "приёмка требует актуальной передачи")])
        return _allow()

    # close и plan (будущее состояние): строгая готовность закрытия.
    if role == "member" and mode in {"final", "mixed"}:
        receipt = evidence.get("block_receipt") or {}
        if not receipt.get("accepted"):
            return _deny([BLOCK_ACCEPTANCE_REQUIRED], [_obligation(
                BLOCK_ACCEPTANCE_REQUIRED, task_id,
                "нет принятой общей квитанции блока")])
        if not _candidate_current(evidence, "block_receipt"):
            return _deny([CANDIDATE_STALE], [_obligation(
                CANDIDATE_STALE, task_id,
                "candidate изменился после общей приёмки — revalidate")])
        return _allow()
    if role == "final_audit" and mode in {"final", "mixed"}:
        receipt = evidence.get("block_receipt") or {}
        if not receipt.get("accepted"):
            return _deny([BLOCK_ACCEPTANCE_REQUIRED], [_obligation(
                BLOCK_ACCEPTANCE_REQUIRED, task_id,
                "нет принятой общей квитанции блока")])
        if not _candidate_current(evidence, "block_receipt"):
            return _deny([CANDIDATE_STALE], [_obligation(
                CANDIDATE_STALE, task_id,
                "candidate изменился после общей приёмки — revalidate")])
        return _allow()
    accepted = (evidence.get("acceptances") or {}).get(task_id)
    if accepted is None and policy is not None:
        return _deny([TASK_NOT_ACCEPTED], [_obligation(
            TASK_NOT_ACCEPTED, task_id, "нет записи приёмки")])
    return _allow()


def _review_ready(evidence, policy):
    package = evidence.get("review_package")
    if not package:
        return False
    return _candidate_current(evidence, "review_package")


def block_status(policy, records, evidence):
    """Вычисляемый статус блока — отображение, не редактируемое поле.

    → {"status", "tasks", "findings_open", "next"}; statuses из
    BLOCK_STATUSES. Вызывается диагностикой (block status), не gate.
    """
    evidence = evidence or {}
    members = policy["members"]
    states = {t: (records.get(t) or {}).get("status") for t in members}
    receipt = evidence.get("block_receipt") or {}
    if receipt.get("accepted"):
        status = "accepted"
    elif all(s in {"done", "cancelled"} or records.get(t, {}).get("archived")
             for t, s in states.items()):
        status = "archived"
    elif any(not f.get("resolved") for f in evidence.get("findings") or []):
        status = "changes_requested"
    else:
        handed = all(states.get(t) == "done" or _handoff_current(evidence, t)
                     for t in members)
        pending_cp = [cp["id"] for cp in policy.get("checkpoints") or []
                      if not (evidence.get("checkpoints") or {})
                      .get(cp["id"], {}).get("accepted")]
        if handed and not pending_cp and _review_ready(evidence, policy):
            status = "review_ready"
        elif handed and pending_cp:
            status = "awaiting_checkpoint"
        else:
            status = "awaiting_work"
    next_step = {
        "awaiting_work": "реализация и проверенные передачи members",
        "awaiting_checkpoint": "независимая приёмка checkpoint",
        "review_ready": "итоговый независимый аудит по review package",
        "changes_requested": "устранение findings задачами исправлений",
        "accepted": "закрытие по разрешению инженера",
        "archived": "блок завершён",
    }[status]
    return {"status": status, "tasks": states,
            "findings_open": [f["id"] for f in evidence.get("findings") or []
                              if not f.get("resolved")],
            "next": next_step}


def _id_str(value, label, errors, field, pattern=REPORT_ID):
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        _err(errors, label, f"некорректный ID {value!r}", field=field)


def validate_review_package(package, label, errors):
    """Immutable review package конечного candidate (schema_version 1)."""
    if not _fields(package, REVIEW_PACKAGE_FIELDS, REVIEW_PACKAGE_FIELDS,
                   label, errors, field="review_package"):
        return
    if package.get("schema_version") != REVIEW_PACKAGE_VERSION:
        _err(errors, label, "неизвестная версия "
             f"{package.get('schema_version')!r}",
             field="review_package.schema_version")
    _id_str(package.get("package_id"), label, errors,
            "review_package.package_id")
    _block_ref(package.get("block"), label, errors, "review_package.block")
    for key in ("policy_sha256", "candidate_sha256"):
        if not isinstance(package.get(key), str) \
                or DIGEST.fullmatch(package[key]) is None:
            _err(errors, label, f"{key} ожидается sha256:<hex>",
                 field=f"review_package.{key}")
    members = _task_id_list(package.get("members"), label, errors,
                            "review_package.members")
    receipts = package.get("receipts")
    if not isinstance(receipts, dict):
        _err(errors, label, "receipts — объект task → передача",
             field="review_package.receipts")
    else:
        for task, receipt in receipts.items():
            if task not in members:
                _err(errors, label, f"receipt '{task}' вне members",
                     field="review_package.receipts")
            if not isinstance(receipt, dict):
                _err(errors, label, f"receipt '{task}' — объект",
                     field="review_package.receipts")
    if not isinstance(package.get("inputs"), (dict, list)):
        _err(errors, label, "inputs — объект/список значимых входов",
             field="review_package.inputs")
    if not isinstance(package.get("environment"), (dict, list)):
        _err(errors, label, "environment — объект/список фактов среды",
             field="review_package.environment")
    matrix = package.get("matrix")
    if not isinstance(matrix, list) or not matrix:
        _err(errors, label, "matrix — непустой список task+AC",
             field="review_package.matrix")
        matrix = []
    for index, row in enumerate(matrix):
        item_label = f"review_package.matrix[{index}]"
        if not _fields(row, MATRIX_FIELDS, {"task", "criterion", "result"},
                       label, errors, field=item_label):
            continue
        if row.get("task") not in members:
            _err(errors, label, f"task '{row.get('task')}' вне members",
                 field=item_label)
        if not documents._str(row.get("criterion")):
            _err(errors, label, "criterion — ID критерия", field=item_label)
        if not documents._str(row.get("result")):
            _err(errors, label, "result — итог проверки", field=item_label)
        if "evidence" in row and not isinstance(row["evidence"], list):
            _err(errors, label, "evidence — список ссылок", field=item_label)
    if _iso(package.get("created_at")) is None:
        _err(errors, label, "created_at — ISO-время с часовым поясом",
             field="review_package.created_at")


def validate_findings_report(report, label, errors):
    """Отрицательный протокол итогового аудита (schema_version 1)."""
    if not _fields(report, FINDINGS_REPORT_FIELDS,
                   FINDINGS_REPORT_FIELDS - {"session"}, label, errors,
                   field="findings_report"):
        return
    if report.get("schema_version") != FINDINGS_REPORT_VERSION:
        _err(errors, label, "неизвестная версия "
             f"{report.get('schema_version')!r}",
             field="findings_report.schema_version")
    _id_str(report.get("report_id"), label, errors,
            "findings_report.report_id")
    if type(report.get("iteration")) is not int or report["iteration"] < 1:
        _err(errors, label, "iteration — положительное целое",
             field="findings_report.iteration")
    _block_ref(report.get("block"), label, errors, "findings_report.block")
    author = report.get("author")
    if _fields(author, documents.ACTOR_FIELDS, {"name"}, label, errors,
               field="findings_report.author"):
        if not documents._str(author.get("name")):
            _err(errors, label, "name — непустая строка",
                 field="findings_report.author")
    if "session" in report and not documents._str(report.get("session")):
        _err(errors, label, "session — непустая строка",
             field="findings_report.session")
    if report.get("verdict") != "changes_requested":
        _err(errors, label, "verdict — 'changes_requested'",
             field="findings_report.verdict")
    if not isinstance(report.get("candidate_sha256"), str) \
            or DIGEST.fullmatch(report["candidate_sha256"]) is None:
        _err(errors, label, "candidate_sha256 ожидается sha256:<hex>",
             field="findings_report.candidate_sha256")
    if _iso(report.get("at")) is None:
        _err(errors, label, "at — ISO-время с часовым поясом",
             field="findings_report.at")
    findings = report.get("findings")
    if not isinstance(findings, list) or not findings:
        _err(errors, label, "содержательный протокол требует findings",
             field="findings_report.findings")
        findings = []
    seen = set()
    for index, finding in enumerate(findings):
        item_label = f"findings_report.findings[{index}]"
        if not _fields(finding, FINDING_FIELDS, FINDING_FIELDS, label, errors,
                       field=item_label):
            continue
        ident = finding.get("id")
        if not isinstance(ident, str) or FINDING_ID.fullmatch(ident) is None:
            _err(errors, label, "id — F-NN", field=item_label)
        elif ident in seen:
            _err(errors, label, f"повторный id '{ident}'", field=item_label)
        seen.add(ident)
        for key in ("source_tasks",):
            tasks = finding.get(key)
            if not isinstance(tasks, list) or not tasks \
                    or any(not isinstance(t, str)
                           or TASK_ID.fullmatch(t) is None for t in tasks):
                _err(errors, label, f"{key} — непустой список ID задач",
                     field=item_label)
        acs = finding.get("affected_ac")
        if not isinstance(acs, list) or not acs \
                or any(not isinstance(a, str) or not a.strip() for a in acs):
            _err(errors, label, "affected_ac — непустой список критериев",
                 field=item_label)
        for key in ("reproducer", "expected"):
            if not documents._str(finding.get(key)):
                _err(errors, label, f"{key} — непустая строка",
                     field=item_label)
        if not isinstance(finding.get("evidence"), list) \
                or any(not isinstance(e, str) for e in finding["evidence"]):
            _err(errors, label, "evidence — список ссылок на материалы",
                 field=item_label)
        if finding.get("severity") not in SEVERITIES:
            _err(errors, label, f"severity ожидается {sorted(SEVERITIES)}",
                 field=item_label)


def validate_block_receipt(receipt, label, errors):
    """Общая квитанция приёмки блока (schema_version 1)."""
    if not _fields(receipt, RECEIPT_FIELDS,
                   RECEIPT_FIELDS - {"checkpoints"}, label, errors,
                   field="block_receipt"):
        return
    if receipt.get("schema_version") != BLOCK_RECEIPT_VERSION:
        _err(errors, label, "неизвестная версия "
             f"{receipt.get('schema_version')!r}",
             field="block_receipt.schema_version")
    _id_str(receipt.get("receipt_id"), label, errors,
            "block_receipt.receipt_id")
    _block_ref(receipt.get("block"), label, errors, "block_receipt.block")
    package = receipt.get("package")
    if _fields(package, RECEIPT_PACKAGE_FIELDS, RECEIPT_PACKAGE_FIELDS,
               label, errors, field="block_receipt.package"):
        _id_str(package.get("id"), label, errors, "block_receipt.package.id")
        if not isinstance(package.get("sha256"), str) \
                or DIGEST.fullmatch(package["sha256"]) is None:
            _err(errors, label, "sha256 ожидается sha256:<hex>",
                 field="block_receipt.package")
    if type(receipt.get("policy_revision")) is not int \
            or receipt["policy_revision"] < 1:
        _err(errors, label, "policy_revision — положительное целое",
             field="block_receipt.policy_revision")
    if not isinstance(receipt.get("candidate_sha256"), str) \
            or DIGEST.fullmatch(receipt["candidate_sha256"]) is None:
        _err(errors, label, "candidate_sha256 ожидается sha256:<hex>",
             field="block_receipt.candidate_sha256")
    verdict = receipt.get("verdict")
    if _fields(verdict, RECEIPT_VERDICT_FIELDS, {"actor", "at"}, label, errors,
               field="block_receipt.verdict"):
        documents._actor(verdict.get("actor"), label, errors,
                         "block_receipt.verdict")
        if _iso(verdict.get("at")) is None:
            _err(errors, label, "at — ISO-время с часовым поясом",
                 field="block_receipt.verdict")
        if "results" in verdict and not isinstance(verdict["results"], dict):
            _err(errors, label, "results — объект task+AC → verdict",
                 field="block_receipt.verdict")
    acceptances = receipt.get("acceptances")
    if not isinstance(acceptances, dict) or not acceptances:
        _err(errors, label, "acceptances — непустой объект task → запись",
             field="block_receipt.acceptances")
    checkpoints = receipt.get("checkpoints")
    if checkpoints is not None and not isinstance(checkpoints, dict):
        _err(errors, label, "checkpoints — объект cp → квитанция",
             field="block_receipt.checkpoints")
    if _iso(receipt.get("at")) is None:
        _err(errors, label, "at — ISO-время с часовым поясом",
             field="block_receipt.at")
