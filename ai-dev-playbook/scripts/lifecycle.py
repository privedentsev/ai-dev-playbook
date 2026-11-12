"""Проверяемые lifecycle-операции документов и карточек: plan/apply/restore.

PB-025 реализует ``archive_document``; PB-026 добавляет действия карточек
``close_task``/``reopen`` и ``promote_change`` документов, а также записи
передачи (handoff) и приёмки (acceptance) — границу независимой приёмки.
Все действия применяются одной операцией через общий engine
file_operations (design §6–7, ADR-0002).

Request — JSON-файл в проекте::

    {"schema_version": 1,
     "actor": {"name": str, "role": str, "model": str|null,
               "session": str|null},
     "basis": "основание операции",
     "scope": {"documents": ["kind:slug", ...], "tasks": ["ID", ...]},
     "actions": [
       {"action": "archive_document", "document": "kind:slug",
        "reason": str, "successor": "kind:slug"|null,
        "confirmed_materials": [id, ...]},
       {"action": "close_task", "task": "ID", "outcome": "done|cancelled",
        "reason": str (обязательна для cancelled),
        "acceptance": str|null, "confirmed_materials": [id, ...]},
       {"action": "reopen", "task": "ID",
        "status": "draft|ready|in_progress|blocked", "reason": str},
       {"action": "repair_task_materials", "task": "ID", "reason": str},
       {"action": "promote_change", "document": "kind:slug",
        "change": "chg-id", "payload": "текст новой редакции",
        "acceptance": "путь к локальному JSON-протоколу приёмки"}]}

``scope`` обязан совпадать с целями ``actions``: документные действия
закрывают ``scope.documents``, карточные — ``scope.tasks``.
``confirmed_materials`` фиксирует подтверждённую доступность обязательных
внешних материалов: неподтверждённый required external блокирует операцию.
CLI проверяет структуру, область и состояние файлов — содержательную
правильность основания и причины подтверждает исполнитель.

``close_task`` переносит карточку и её доказательства в архив. Для
legacy-карточки (без блока метаданных) действует прежняя политика:
``docs/tasks/<ID>-evidence/`` и ``work/verification/<ID>/`` переносятся
целиком. Для карточки с метаданными переход в ``done`` требует записи
``handoff`` (передача: исполнитель, модель, отчёты verify, отпечаток
проекции карточки) и записи ``acceptance`` (независимая приёмка,
привязанная к передаче полем ``handoff_at``); устаревшие входы отчётов и
изменение карточки после передачи отклоняют операцию. ``cancelled``
требует записи приёмки с ``result=cancelled``.

``reopen`` возвращает архивную карточку в работу (``ready``,
``in_progress``, ``draft`` либо ``blocked``), сбрасывает записи
передачи/приёмки в метаданных (история остаётся в архивных материалах и
сопровождающих JSON) и переводит в ``blocked`` активных зависимых. Операция отклоняется, если
после неё граф статусов недопустим (например, done-зависимый при
возвращённой зависимости).

``repair_task_materials`` переносит оставшийся активный evidence-каталог
архивной legacy-карточки без изменения Status/Closed и повторной приёмки.
Байты материалов неизменны; карта переносов и владение регистрируются в
карточке, SHA-256 файлов — в companion. Managed-карточки, чужое владение,
symlink и занятое назначение отклоняются. work/verification не затрагивается.

``promote_change`` принимает изменение с state=implemented: тело
документа заменяется payload'ом, прежняя редакция сохраняется как
неизменяемый материал ``<stem>-evidence/revision-N.md``, изменение
получает state=accepted, current_revision увеличивается. Обязательный
JSON-протокол acceptance связывает accepted/kind=acceptance с document,
change, revision и SHA-256 исходного документа и точного UTF-8 payload.
Копия записи сохраняется в immutable companion; путь протокола включён
во входы plan/apply. Это проверка согласованности, не личности аудитора.

``lifecycle plan request.json [--save]`` строит детерминированный план:
карту переносов, байты всех записей (новые тексты с обновлёнными
метаданными и ссылками, переписанные затронутые файлы, индексы,
сопровождающие JSON) и снимок входов. ``--save`` пишет план в
``work/lifecycle/plans/<plan_id>.json``; в плане — только хеши.

``lifecycle apply <plan.json>`` пересобирает план из сохранённого запроса и
текущего дерева и обязан получить идентичные plan_id, входы и хеши записей:
изменившиеся входы или подмена сохранённого JSON отклоняются до записи.
Затем план применяется file_operations.run_operation — staged-копия,
резервные копии, журнал, итоговые проверки (каталог, индексы затронутых
коллекций и задач, ссылки всего дерева) и квитанция. Повторный apply
завершённого plan_id сообщает already_completed и не создаёт второй
перенос.

``lifecycle status`` показывает маркер незавершённой операции и сохранённые
планы; ``lifecycle restore`` восстанавливает дерево по backup_index через
file_operations.restore_operation. Записи передачи и приёмки создают
отдельные команды playbook.py ``lifecycle handoff``/``accept``.
"""

import copy
import datetime as dt
import difflib
import json
import os
import re
import unicodedata
from pathlib import Path, PurePosixPath

import checks_gate
import document_links
import documents
import task_cards
import file_operations
import verification
import verification_policy
import block_workflow

PLAN_SCHEMA = 1
PLANS_DIR = file_operations.WORK_DIR + "/plans"

# Обязательные и допустимые поля каждого действия (кроме "action").
ACTION_SPECS = {
    "repair_task_materials": ({"task", "reason"}, {"task", "reason"}),
    "archive_document": ({"document", "reason"},
                         {"document", "reason", "successor",
                          "confirmed_materials"}),
    "close_task": ({"task", "outcome"},
                   {"task", "outcome", "reason", "acceptance",
                    "confirmed_materials"}),
    "reopen": ({"task", "status", "reason"},
               {"task", "status", "reason"}),
    "promote_change": ({"document", "change", "payload"},
                       {"document", "change", "payload", "acceptance"}),
}
ACTIONS = frozenset(ACTION_SPECS)
DOCUMENT_ACTIONS = {"archive_document", "promote_change"}
TASK_ACTIONS = {"close_task", "reopen", "repair_task_materials"}

REQUEST_FIELDS = {"schema_version", "actor", "basis", "scope", "actions"}
ACTOR_FIELDS = {"name", "model", "role", "session"}
SCOPE_FIELDS = {"documents", "tasks"}
CLOSE_OUTCOMES = {"done", "cancelled"}
# ready/in_progress — прямой возврат в работу; draft/blocked — для
# зависимых, чьи зависимости возвращены этой же операцией.
REOPEN_STATES = {"draft", "ready", "in_progress", "blocked"}

# Внутри work/ переносимы только зарегистрированные результаты проверок и
# сохранённые запросы/планы; живое состояние операций и обновлений
# материалом не является и не переносится.
MOVABLE_WORK = ("work/verification/", "work/lifecycle/plans/")
FINAL_TASK_STATES = {"done", "cancelled"}
# Зависимые возвращённой карточки с этими статусами переводятся в blocked.
BLOCKABLE_STATES = {"ready", "in_progress", "in_review"}

# Разделы карточки-протокола: их дописывание (записи выполнения, передачи,
# аудита и исправлений) не меняет проверяемую проекцию карточки.
PROTOCOL_SECTIONS = ("Выполнение и проверки", "Передача")
PROTOCOL_PREFIXES = ("Аудит", "Повторный аудит", "Исправлено")


def _rel(root, path):
    base = root if hasattr(path, "view") else file_operations.scoped_operations.physical_root(root)
    return path.relative_to(base).as_posix()


def _unknown_fields(data, allowed, label, require):
    unknown = sorted(set(data) - allowed)
    require(not unknown, f"{label}: неизвестные поля {unknown}")


def _canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _required_str(action, field, label, require):
    value = action.get(field)
    require(isinstance(value, str) and value.strip(),
            f"{label}.{field}: непустая строка")
    return value.strip()


def _optional_str(action, field, label, require):
    value = action.get(field)
    require(value is None or (isinstance(value, str) and value.strip()),
            f"{label}.{field}: непустая строка либо отсутствует")
    return value.strip() if isinstance(value, str) else None


def _confirmed_materials(action, label, require):
    confirmed = action.get("confirmed_materials") or []
    require(isinstance(confirmed, list)
            and all(isinstance(item, str) for item in confirmed),
            f"{label}.confirmed_materials: список id материалов")
    return sorted(set(confirmed))


def _normalize_action(kind, action, label, require):
    """Проверка полей действия → нормализованный объект."""
    item = {"action": kind}
    if kind in DOCUMENT_ACTIONS:
        doc_id = action.get("document")
        require(isinstance(doc_id, str)
                and documents.DOC_ID.fullmatch(doc_id),
                f"{label}.document: ID 'kind:slug'")
        item["document"] = doc_id
    else:
        task = action.get("task")
        require(isinstance(task, str) and documents.TASK_ID.fullmatch(task),
                f"{label}.task: ID карточки")
        item["task"] = task
    if kind == "archive_document":
        item["reason"] = _required_str(action, "reason", label, require)
        successor = action.get("successor")
        require(successor is None
                or (isinstance(successor, str)
                    and documents.DOC_ID.fullmatch(successor)),
                f"{label}.successor: ID документа либо null")
        item["successor"] = successor
        item["confirmed_materials"] = _confirmed_materials(action, label,
                                                         require)
    elif kind == "close_task":
        outcome = action.get("outcome")
        require(outcome in CLOSE_OUTCOMES,
                f"{label}.outcome: {'|'.join(sorted(CLOSE_OUTCOMES))}")
        item["outcome"] = outcome
        # Отмена без записанной причины лишает историю объяснения.
        if outcome == "cancelled":
            item["reason"] = _required_str(action, "reason", label, require)
        else:
            item["reason"] = _optional_str(action, "reason", label, require)
        item["acceptance"] = _optional_str(action, "acceptance", label,
                                           require)
        item["confirmed_materials"] = _confirmed_materials(action, label,
                                                         require)
    elif kind == "repair_task_materials":
        item["reason"] = _required_str(action, "reason", label, require)
    elif kind == "reopen":
        status = action.get("status")
        require(status in REOPEN_STATES,
                f"{label}.status: {'|'.join(sorted(REOPEN_STATES))}")
        item["status"] = status
        item["reason"] = _required_str(action, "reason", label, require)
    else:  # promote_change
        item["change"] = _required_str(action, "change", label, require)
        _required_str(action, "payload", label, require)
        item["payload"] = action["payload"]
        item["acceptance"] = _optional_str(action, "acceptance", label,
                                           require)
    return item


def validate_request(data, require):
    """Нормализованный запрос lifecycle-операции из JSON-объекта."""
    require(isinstance(data, dict), "Запрос должен быть JSON-объектом")
    _unknown_fields(data, REQUEST_FIELDS, "request", require)
    require(data.get("schema_version") == 1,
            f"request.schema_version {data.get('schema_version')!r} "
            "не поддерживается")
    actor = data.get("actor")
    require(isinstance(actor, dict), "request.actor: ожидается объект")
    _unknown_fields(actor, ACTOR_FIELDS, "request.actor", require)
    for field in ("name", "role"):
        require(isinstance(actor.get(field), str) and actor[field].strip(),
                f"request.actor.{field}: непустая строка")
    for field in ("model", "session"):
        require(actor.get(field) is None or isinstance(actor.get(field), str),
                f"request.actor.{field}: строка либо null")
    require(isinstance(data.get("basis"), str) and data["basis"].strip(),
            "request.basis: непустая строка основания")
    scope = data.get("scope")
    require(isinstance(scope, dict), "request.scope: ожидается объект")
    _unknown_fields(scope, SCOPE_FIELDS, "request.scope", require)
    expected = {}
    for key in sorted(set(scope) & SCOPE_FIELDS):
        values = scope[key]
        pattern = documents.DOC_ID if key == "documents" else documents.TASK_ID
        shape = "ID 'kind:slug'" if key == "documents" else "ID карточки"
        require(isinstance(values, list) and values
                and all(isinstance(item, str) and pattern.fullmatch(item)
                        for item in values),
                f"request.scope.{key}: непустой список {shape}")
        require(len(set(values)) == len(values),
                f"request.scope.{key}: повторные ID")
        expected[key] = set(values)
    require(expected, "request.scope: нужен список documents и/или tasks")
    actions = data.get("actions")
    require(isinstance(actions, list) and actions,
            "request.actions: непустой список")
    normalized = []
    targets = {"documents": set(), "tasks": set()}
    for index, action in enumerate(actions):
        label = f"request.actions[{index}]"
        require(isinstance(action, dict), f"{label}: ожидается объект")
        kind = action.get("action")
        require(kind in ACTIONS, f"{label}: неизвестное действие {kind!r}")
        required, allowed = ACTION_SPECS[kind]
        _unknown_fields(action, allowed | {"action"}, label, require)
        missing = sorted(field for field in required
                         if action.get(field) is None)
        require(not missing, f"{label}: нет обязательных полей {missing}")
        item = _normalize_action(kind, action, label, require)
        bucket = "documents" if kind in DOCUMENT_ACTIONS else "tasks"
        target = item.get("document") or item["task"]
        require(target not in targets[bucket],
                f"{label}: повторное действие для {target}")
        targets[bucket].add(target)
        normalized.append(item)
    for key in ("documents", "tasks"):
        wanted, got = expected.get(key, set()), targets[key]
        require(wanted == got,
                f"request.scope.{key} не совпадает с actions: "
                f"без действия {sorted(wanted - got)}, "
                f"вне scope {sorted(got - wanted)}")
    return {"schema_version": 1,
            "actor": {key: actor.get(key)
                      for key in sorted(ACTOR_FIELDS & set(actor))},
            "basis": data["basis"].strip(),
            "scope": {key: sorted(values) for key, values in expected.items()},
            "actions": normalized}


def _movable_material(rel, material, doc_id, require):
    """Служебные области work/ не переносятся как материалы."""
    if rel == "work" or rel.startswith("work/"):
        require(any(rel.startswith(item) for item in MOVABLE_WORK),
                f"{doc_id}: материал '{material.get('id')}' ({rel}) — "
                "служебная область work/; переносимы только "
                "work/verification/<id>/ и work/lifecycle/plans/")


def _protected_files(paths, records, catalog):
    """Индексы, карточки и документы — материал не накрывает их."""
    protected = {paths["task_index"].resolve()}
    protected.update(rec["path"].resolve() for rec in records.values())
    protected.update(documents.meta_path(rec["path"]).resolve() for rec in records.values())
    if catalog is not None:
        protected.update(entry["index"].resolve()
                         for entry in catalog["collections"].values())
        protected.update(item["path"].resolve()
                         for item in catalog["documents"].values())
    return protected


def _free_name(directory, target, root, require):
    """Отказ при коллизии имени назначения (NFC + casefold)."""
    if directory.is_dir():
        folded = unicodedata.normalize("NFC", target.name).casefold()
        for child in directory.iterdir():
            require(unicodedata.normalize("NFC", child.name).casefold()
                    != folded, f"Коллизия имени: {_rel(root, child)}")


def _archive_destination(root, resolved_src, home_dir, dest_dir,
                         evidence_dir):
    """Архивное назначение одного источника — единая формула раскладки.

    Материал внутри home_dir сохраняет путь относительно dest_dir;
    материал вне home_dir уходит в evidence_dir с полным относительным
    путём от корня.
    """
    if resolved_src.is_relative_to(home_dir.resolve()):
        return dest_dir / resolved_src.relative_to(home_dir.resolve())
    return evidence_dir / resolved_src.relative_to(root.resolve())


def material_destinations(root, owner, materials, home_dir, dest_dir,
                          evidence_dir, safe_path, require, exclude=None):
    """Будущая архивная раскладка локальных материалов владельца (PB-072).

    Чистый расчёт по той же формуле, что _move_materials: каталоги
    разворачиваются до вложенных файлов/каталогов, поэтому коллизию
    вложенного report.json видно до записи. Разные источники с одним
    назначением — конфликт независимо от равенства байтов; повторное
    представление одного источника (материал-каталог и файл внутри него)
    законно. Занятое архивное назначение, выход из области назначения и
    файл, ставший родителем другого назначения, отклоняются здесь же;
    каталог-предок допустим. Возвращает
    {destination: (material_id, source, source_is_dir)}.

    ``exclude`` — resolved-пути источников, исключаемые из раскладки
    (проекция дерева после удаления, например переносимый файл).
    """
    exclude = {path.resolve() for path in exclude} if exclude else set()
    destinations = {}
    for material in materials:
        if material.get("storage") != "local":
            continue
        src = material.get("resolved")
        if src is None and material.get("source"):
            src = safe_path(root, material["source"])
        if src is None:
            continue
        resolved_src = src.resolve()
        dst = _archive_destination(root, resolved_src, home_dir,
                                   dest_dir, evidence_dir).resolve()
        items = [] if resolved_src in exclude else [(resolved_src, dst)]
        if resolved_src.is_dir():
            items += [(sub, dst / sub.relative_to(resolved_src))
                      for sub in sorted(resolved_src.rglob("*"))
                      if sub.resolve() not in exclude]
        for old, new in items:
            require(new.is_relative_to(dest_dir.resolve()),
                    f"{owner}: назначение материала "
                    f"'{material.get('id')}' вне области назначения: "
                    f"{_rel(root, new)}")
            require(not new.exists() and not new.is_symlink(),
                    f"Назначение занято: {_rel(root, new)}")
            previous = destinations.setdefault(
                new, (material.get("id"), old, old.is_dir()))
            if previous[1] != old:
                require(False,
                        f"{owner}: материалы '{previous[0]}' "
                        f"({_rel(root, previous[1])}) и "
                        f"'{material.get('id')}' ({_rel(root, old)}) "
                        f"назначены в {_rel(root, new)}. Штатный отчёт "
                        "verify используйте на исходном пути через handoff; "
                        "для уже размещённой копии подготовьте явный "
                        "lifecycle relocate-material.")
    dest_root = dest_dir.resolve()
    for new in sorted(destinations, key=lambda path: len(path.parts)):
        for parent in new.parents:
            if parent == dest_root or not parent.is_relative_to(dest_root):
                break
            ancestor = destinations.get(parent)
            if ancestor is not None and not ancestor[2]:
                require(False,
                        f"{owner}: назначение файла материала "
                        f"'{ancestor[0]}' ({_rel(root, ancestor[1])}) "
                        f"является родителем назначения "
                        f"'{destinations[new][0]}' "
                        f"({_rel(root, destinations[new][1])}): "
                        f"{_rel(root, parent)}. Переместите один из "
                        "источников штатным lifecycle relocate-material "
                        "или исправьте регистрацию.")
            # Реальный файл на месте родительского каталога делает
            # раскладку незаписываемой; существующий каталог законен.
            if ancestor is None and (parent.exists()
                                     or parent.is_symlink()):
                require(parent.is_dir() and not parent.is_symlink(),
                        f"{owner}: родительский путь назначения "
                        f"'{destinations[new][0]}' "
                        f"({_rel(root, destinations[new][1])}) занят "
                        f"файлом: {_rel(root, parent)}. Освободите "
                        "архивный путь или выберите другой источник.")
    return destinations


def _check_destinations(root, task_id, materials, paths, safe_path,
                        require, exclude=None):
    """Ранний контроль коллизий архивных назначений материалов задачи.

    Проверяет только владельца команды — чужие незавершённые задачи не
    блокируются. Финальная сверка объединённого плана остаётся при close.
    """
    evidence_dir = paths["archive"] / f"{task_id}-evidence"
    material_destinations(root, task_id, materials, paths["tasks"],
                          paths["archive"], evidence_dir, safe_path, require,
                          exclude=exclude)


def _move_materials(root, owner, materials, self_path, home_dir, dest_dir,
                    evidence_dir, protected, confirmed, safe_path, require):
    """Карта переносов локальных материалов владельца.

    Материал внутри home_dir сохраняет путь относительно dest_dir;
    материал вне home_dir уходит в evidence_dir с полным относительным
    путём от корня. Назначение обязано оставаться внутри dest_dir.
    """
    moves, dir_moves, file_moves = {}, {}, {}
    top_moves, moved, external = [], [], []
    for material in materials:
        if material.get("storage") != "local":
            external.append({"id": material.get("id"),
                             "source": material.get("source"),
                             "required": bool(material.get("required")),
                             "confirmed": material.get("id") in confirmed})
            continue
        src = material.get("resolved")
        require(src is not None and src.exists(),
                f"{owner}: материал '{material.get('id')}' отсутствует: "
                f"{material.get('source')}")
        rel_src = _rel(root, src)
        _movable_material(rel_src, material, owner, require)
        resolved_src = src.resolve()
        require(resolved_src != self_path.resolve()
                and not self_path.resolve().is_relative_to(resolved_src),
                f"{owner}: материал '{material.get('id')}' накрывает "
                "самого владельца")
        if resolved_src.is_dir():
            for path in sorted(protected):
                require(not path.is_relative_to(resolved_src),
                        f"{owner}: материал '{material.get('id')}' "
                        f"({rel_src}) накрывает служебный файл {path}")
        dst = _archive_destination(root, resolved_src, home_dir, dest_dir,
                                   evidence_dir)
        resolved_dst = dst.resolve()
        require(resolved_dst.is_relative_to(dest_dir.resolve()),
                f"{owner}: назначение материала '{material.get('id')}' "
                f"вне области назначения: {_rel(root, dst)}")
        require(not dst.exists() and not dst.is_symlink(),
                f"Назначение занято: {_rel(root, dst)}")
        moves[resolved_src] = resolved_dst
        top_moves.append((resolved_src, resolved_dst))
        if resolved_src.is_dir():
            for sub in sorted(resolved_src.rglob("*")):
                mapped = resolved_dst / sub.relative_to(resolved_src)
                if sub.is_dir():
                    dir_moves[sub] = mapped
                else:
                    moves[sub] = file_moves[sub] = mapped
            dir_moves[resolved_src] = resolved_dst
        else:
            file_moves[resolved_src] = resolved_dst
        moved.append({"id": material.get("id"), "role": material.get("role"),
                      "storage": "local", "source_old": rel_src,
                      "source_new": resolved_dst.relative_to(root)
                      .as_posix(),
                      "required": bool(material.get("required")),
                      "immutable": bool(material.get("immutable")),
                      "used_by": list(material.get("used_by") or [])})
    return {"moves": moves, "dir_moves": dir_moves, "file_moves": file_moves,
            "top_moves": top_moves, "materials": moved, "external": external}


def _plan_archive(root, catalog, paths, action, at, safe_path, require):
    """Перенос документа и материалов → запись плана действия."""
    doc_id = action["document"]
    entry = catalog["documents"].get(doc_id)
    require(entry is not None,
            f"{doc_id}: нет зарегистрированного документа — legacy сначала "
            "регистрируется командой document register")
    require(not entry["archived"], f"{doc_id}: документ уже в архиве")
    kind = entry["kind"]
    require(entry["state"] in documents.ARCHIVED_STATES,
            f"{doc_id}: состояние '{entry['state']}' не финальное — "
            "архивирование требует одно из "
            f"{sorted(documents.ARCHIVED_STATES & documents.STATES[kind])}")
    successor = action["successor"]
    if successor is not None:
        require(successor != doc_id, f"{doc_id}: преемник совпадает с документом")
        require(successor in catalog["documents"],
                f"{doc_id}: successor '{successor}' — нет такого документа")
    cols = catalog["collections"][kind]
    old_path = entry["path"]
    new_path = cols["archive"] / old_path.name
    require(not new_path.exists() and not new_path.is_symlink(),
            f"Назначение занято: {_rel(root, new_path)}")
    if cols["archive"].is_dir():
        folded = unicodedata.normalize("NFC", new_path.name).casefold()
        for child in cols["archive"].iterdir():
            require(unicodedata.normalize("NFC", child.name).casefold()
                    != folded,
                    f"Коллизия имени в архиве: {_rel(root, child)}")
    stray = [item["path"] for item in catalog["candidates"]
             if item.get("owner") == doc_id]
    require(not stray,
            f"{doc_id}: неучтённые материалы {sorted(stray)} — зарегистрируйте "
            "их в materials либо уберите перед архивированием")
    confirmed = set(action["confirmed_materials"])
    material_ids = {item.get("id") for item in entry["materials"]
                    if isinstance(item.get("id"), str)}
    extra = sorted(confirmed - material_ids)
    require(not extra, f"{doc_id}: confirmed_materials — неизвестные id {extra}")
    unconfirmed = sorted(
        item["id"] for item in entry["materials"]
        if item.get("storage") == "external" and item.get("required")
        and isinstance(item.get("id"), str) and item["id"] not in confirmed)
    require(not unconfirmed,
            f"{doc_id}: обязательные внешние материалы без подтверждения "
            f"доступа {unconfirmed} — перечислите их в confirmed_materials")
    evidence_dir = new_path.parent / f"{new_path.stem}-evidence"
    protected = _protected_files(paths, catalog["records"], catalog)
    shift = _move_materials(root, doc_id, entry["materials"], old_path,
                            old_path.parent, cols["archive"], evidence_dir,
                            protected, confirmed, safe_path, require)
    moves = shift["moves"]
    dir_moves, file_moves = shift["dir_moves"], shift["file_moves"]
    top_moves, materials, external = (shift["top_moves"], shift["materials"],
                                      shift["external"])
    moves[old_path.resolve()] = new_path.resolve()
    file_moves[old_path.resolve()] = new_path.resolve()
    top_moves.append((old_path.resolve(), new_path.resolve()))
    return {"atype": "archive_document", "document": doc_id, "kind": kind,
            "entry": entry, "reason": action["reason"],
            "successor": successor,
            "old_path": old_path, "new_path": new_path,
            "evidence_dir": evidence_dir, "moves": moves,
            "dir_moves": dir_moves, "file_moves": file_moves,
            "top_moves": top_moves,
            "materials": materials, "external": external, "blocked": []}


card_projection = task_cards.card_projection



def _task_meta(path, rel, require):
    """(text, meta) карточки; meta=None для legacy-карточки."""
    text = documents.read_text(path)
    errors = []
    meta = documents.parse_meta(text, rel, errors)
    require(not errors, "; ".join(errors))
    return text, meta


def _task_meta_entry(catalog, task_id, meta, require):
    """Карточка с блоком метаданных требует каталог и запись task_meta."""
    if meta is None:
        return None
    require(catalog is not None and task_id in catalog["task_meta"],
            f"{task_id}: карточка с метаданными требует раздел documents "
            "project.json — в каталоге её запись отсутствует")
    return catalog["task_meta"][task_id]


def _close_text(text, outcome, stamp, task_id, require):
    """Текст закрываемой карточки: статус и Closed."""
    new_text, count = re.subn(r"(?m)^Status: [^\n]+$", f"Status: {outcome}",
                              text, count=1)
    require(count == 1, f"{task_id}: не найдена строка Status")
    new_text, count = re.subn(r"(?m)^Closed: -$", f"Closed: {stamp}",
                              new_text, count=1)
    require(count == 1, f"{task_id}: не найдена строка Closed")
    return new_text


def _reopen_text(text, rec, status, reason, stamp, actor, require):
    """Текст возвращаемой карточки: статус, сброс Closed, запись возврата."""
    new_text, count = re.subn(r"(?m)^Status: [^\n]+$", f"Status: {status}",
                              text, count=1)
    require(count == 1, f"{rec['id']}: не найдена строка Status")
    new_text, count = re.subn(r"(?m)^Closed: [^\n]+$", "Closed: -",
                              new_text, count=1)
    require(count == 1, f"{rec['id']}: не найдена строка Closed")
    if task_cards.version(text) == 2:
        return new_text  # История операции хранится в immutable reopen companion.
    if not new_text.endswith("\n"):
        new_text += "\n"
    new_text += (f"\n## Возврат {stamp}; исполнитель: "
                 f"{_actor_label(actor)}. Причина: {reason}. "
                 f"Прежний исход: {rec['status']} "
                 f"(Closed {rec['closed']}). История закрытия и приёмки "
                 "сохранена в материалах карточки и неизменна.\n")
    return new_text


def _blocked_text(text, dep_id, source_id, stamp, actor, require):
    """Зависимая карточка при возврате зависимости: статус blocked + запись."""
    new_text, count = re.subn(r"(?m)^Status: [^\n]+$", "Status: blocked",
                              text, count=1)
    require(count == 1, f"{dep_id}: не найдена строка Status")
    if task_cards.version(text) == 2:
        meta = task_cards.metadata(new_text)
        meta.pop('criterion_evidence', None)
        new_text = _splice_meta(new_text, meta)
        return task_cards.render_checkboxes(new_text, {
            ident: {'checked': False} for ident in task_cards.parse(new_text)['criteria']})
    if not new_text.endswith("\n"):
        new_text += "\n"
    new_text += (f"\n## Блокировка {stamp}; исполнитель: "
                 f"{_actor_label(actor)}. Зависимость {source_id} "
                 "возвращена в работу — карточка переведена в blocked "
                 "до повторной готовности.\n")
    return new_text


def _checks_gate(root, task_id, rel, text, task_meta, safe_path, require):
    """Покрытие checks-контракта для handoff/accept/close (PB-062).

    ``None`` — проект не подключил ``checks_policy``: прежний контракт
    verify-отчётов действует без изменений. Иначе обязателен единый
    расчёт ``checks_gate.evaluate`` области задачи: свежие результаты
    при ``trust_mode=local``, квитанции verifier'а при ``protected``.
    """
    view = getattr(root, "view", None)
    physical = getattr(view, "root", None)
    if physical is not None:
        # v2-контекст оперирует ProjectView-обёрткой; checks-контракт
        # читает живую файловую систему — нужен физический корень.
        root = physical
    policy = checks_gate.load_policy(root)
    if policy is None:
        return None
    verdict = checks_gate.evaluate(
        root, policy, task_id=task_id, task_content=text,
        task_meta=task_meta, scope="task")
    require(verdict["ok"], _checks_gate_message(task_id, verdict))
    return verdict


def _checks_gate_message(task_id, verdict):
    parts = [f"{task_id}: покрытие проверок не подтверждено "
             f"(scope={verdict['scope']}"
             + (f", checkpoint={verdict['checkpoint']}"
                if verdict["checkpoint"] else "") + ")"]
    if verdict["uncovered"]:
        parts.append("не покрыто: " + ", ".join(verdict["uncovered"]))
    if verdict["manual"]:
        parts.append("ручные обязательства без подтверждения: "
                     + ", ".join(verdict["manual"]))
    if verdict["blocked"]:
        parts.append("заблокировано: " + ", ".join(verdict["blocked"]))
    for reason in verdict["rejections"][:5]:
        parts.append(f"отказ: {reason}")
    if verdict["trust_mode"] == "protected" \
            and not verdict["protected_confirmed"]:
        parts.append("граница verifier не доказана — защищённое "
                     "утверждение заблокировано (R-20)")
    parts.append("выполните checks run checks status и повторите "
                 "операцию")
    return "; ".join(parts)


def _handoff_evidence(root, task_id, rel, text, task_meta, safe_path, require):
    """The same freshness gate for closing and read-only context display."""
    if task_meta.get('schema_version') == 2:
        return _handoff_v2_evidence(root, task_id, rel, text, task_meta, safe_path, require)
    handoff = task_meta.get('handoff')
    require(isinstance(handoff, dict),
            f"{task_id}: нет записи передачи на приёмку — выполните "
            "lifecycle handoff после последних изменений")
    require(card_projection(text) == handoff.get("card_projection"),
            f"{task_id}: карточка изменилась после передачи (цель, "
            "зависимости или критерии приёмки) — повторите затронутую "
            "проверку и lifecycle handoff")
    materials = {item.get("id"): item for item in task_meta["materials"]}
    fresh = []
    for ref in handoff.get("reports") or []:
        material = materials.get(ref)
        require(material is not None,
                f"{task_id}: отчёт '{ref}' не зарегистрирован в materials")
        require(material.get("storage") == "local",
                f"{task_id}: отчёт '{ref}' — внешний материал, свежесть "
                "входов не проверяется")
        src = material.get("resolved")
        require(src is not None and src.exists(),
                f"{task_id}: отчёт '{ref}' отсутствует: "
                f"{material.get('source')}")
        report_path = src / "report.json" if src.is_dir() else src
        report = verification.load_report(report_path, require)
        require(report.get("task_id") == task_id,
                f"{task_id}: отчёт '{ref}' принадлежит задаче "
                f"{report.get('task_id')}")
        if report.get("status") == "passed" \
                and report.get("inputs_unchanged"):
            ok, _ = verification.inputs_fresh(root, report, {rel},
                                              safe_path, require)
            if ok:
                if task_cards.version(text) == 2:
                    if report.get("card_projection") != handoff.get("card_projection"):
                        continue
                    target_exec_id, target_exec_sha = task_cards.execution_state(text, rel)
                    if target_exec_id is not None:
                        if report.get("execution_id") != target_exec_id or report.get("execution_sha256") != target_exec_sha:
                            continue
                    elif report.get("execution_id") is not None:
                        continue
                fresh.append(ref)
    require(not handoff.get("reports") or fresh,
            f"{task_id}: нет свежего успешного прогона verify — входы "
            "изменились после проверки; повторите verify и handoff")
    _checks_gate(root, task_id, rel, text, task_meta, safe_path, require)
    return handoff


def _close_evidence(root, task_id, rec, text, outcome, task_meta,
                    safe_path, require):
    """Граница приёмки карточки с метаданными.

    done требует записи передачи со свежей проекцией карточки, свежего
    успешного прогона verify (если прогоны зарегистрированы) и записи
    приёмки, привязанной к этой передаче. cancelled требует записи
    приёмки с result=cancelled. Diagnostic-протокол принимает только
    карточка аудита без зависимостей — у остальных он приёмкой не
    является.
    """
    rel = _rel(root, rec["path"])
    if task_meta.get('schema_version') == 2 and outcome != 'cancelled':
        _handoff_v2_evidence(root, task_id, rel, text, task_meta, safe_path, require)
        handoff, acceptance = task_meta.get('handoff'), task_meta.get('acceptance')
        require(isinstance(acceptance, dict) and acceptance.get('result_hash') == handoff.get('result_hash'),
                f'{task_id}: приёмка относится к другой версии результата')
        require(acceptance.get('role') == 'engineer' or
                (task_meta['workflow']['route'] != 'quick' and acceptance.get('role') == 'auditor'),
                f'{task_id}: быстрый маршрут требует приёмку инженером')
    if outcome != "cancelled":
        task_cards.receipt(text, task_meta, acceptance=True)
        if task_cards.version(text) == 2:
            states, mismatches = criterion_view(root, task_id, rel, text, task_meta, safe_path, require)
            require(not mismatches, f"{task_id}: чекбоксы неактуальны {mismatches}; выполните card sync {task_id} --apply")
            unchecked = [k for k, s in states.items() if not s["checked"]]
            require(not unchecked, f"{task_id}: не все критерии приёмки подтверждены проверками: {unchecked}")
    handoff = task_meta.get("handoff")
    acceptance = task_meta.get("acceptance")
    if outcome == "cancelled":
        require(isinstance(acceptance, dict)
                and acceptance.get("result") == "cancelled",
                f"{task_id}: отмена карточки с метаданными требует записи "
                "приёмки с result=cancelled — выполните lifecycle accept "
                "--result cancelled")
        return
    _handoff_evidence(root, task_id, rel, text, task_meta, safe_path, require)
    require(isinstance(acceptance, dict),
            f"{task_id}: нет записи приёмки — выполните lifecycle accept")
    require(acceptance.get("result") == "accepted",
            f"{task_id}: приёмка не подтверждена "
            f"(result={acceptance.get('result')!r})")
    require(acceptance.get("handoff_at") == handoff.get("at"),
            f"{task_id}: приёмка привязана к прежней передаче — повторите "
            "lifecycle accept после handoff")
    if not (task_id.startswith("AUDIT") and not rec["deps"]):
        require(acceptance.get("kind") == "acceptance",
                f"{task_id}: diagnostic-протокол не является приёмкой — "
                "нужна запись kind=acceptance")


def _legacy_task_moves(root, paths, task_id, evidence_dir, moves,
                       dir_moves, file_moves, top_moves, safe_path, require,
                       home=None, dest=None, include_verification=True):
    """Legacy-карточка: деревья evidence и прогонов verify переносятся
    целиком, с сохранением внутренней структуры.

    close: home=docs/tasks → dest=docs/archive/tasks; прогоны уходят в
    evidence_dir с полным относительным путём. reopen — наоборот.
    """
    home = home or paths["tasks"]
    dest = dest or paths["archive"]
    if dest == paths["archive"]:
        sources = [home / f"{task_id}-evidence"]
        if include_verification:
            sources.append(safe_path(root, f"work/verification/{task_id}"))
    else:
        sources = [home / f"{task_id}-evidence"]
    for src in sources:
        if not src.exists() and not src.is_symlink():
            continue
        require(src.is_dir() and not src.is_symlink(),
                f"{task_id}: материалы не являются каталогом: "
                f"{_rel(root, src)}")
        resolved_src = src.resolve()
        for link in documents._walk_links(resolved_src):
            require(False, f"{task_id}: символическая ссылка в материалах: "
                    f"{link.relative_to(root)}")
        if resolved_src.is_relative_to(home.resolve()):
            dst = dest / resolved_src.relative_to(home.resolve())
        else:
            dst = evidence_dir / resolved_src.relative_to(root.resolve())
        resolved_dst = dst.resolve()
        require(resolved_dst.is_relative_to(dest.resolve()),
                f"{task_id}: назначение материалов вне области "
                f"назначения: {_rel(root, dst)}")
        require(not resolved_dst.exists()
                and not resolved_dst.is_symlink(),
                f"Назначение занято: {_rel(root, dst)}")
        moves[resolved_src] = resolved_dst
        top_moves.append((resolved_src, resolved_dst))
        for sub in sorted(resolved_src.rglob("*")):
            mapped = resolved_dst / sub.relative_to(resolved_src)
            if sub.is_dir():
                dir_moves[sub] = mapped
            else:
                moves[sub] = file_moves[sub] = mapped
        dir_moves[resolved_src] = resolved_dst


def _plan_repair(root, catalog, paths, records, action, safe_path, require):
    """Repair historical legacy leftovers without reopening/reaccepting a task."""
    task_id = action["task"]
    require(catalog is not None, "Раздел documents не настроен в project.json")
    require(task_id in records, f"Нет задачи: {task_id}")
    rec = records[task_id]
    require(rec["archived"] and rec["status"] in FINAL_TASK_STATES,
            f"{task_id}: восстановление только для закрытой архивной карточки")
    path = rec["path"]
    text, meta = _task_meta(path, _rel(root, path), require)
    require(meta is None,
            f"{task_id}: восстановление только для legacy-карточки без метаданных")
    source = paths["tasks"] / f"{task_id}-evidence"
    safe_path(root, _rel(root, source))
    require(source.is_dir(), f"{task_id}: нет каталога материалов {_rel(root, source)}")
    # Ownership is never inferred from the directory name over an existing claim.
    for owner, materials in catalog["owners"].items():
        for material in materials:
            claimed = material.get("resolved")
            require(claimed is None or not (claimed.is_relative_to(source.resolve())
                    or source.resolve().is_relative_to(claimed)),
                    f"{task_id}: материалы уже принадлежат {owner}")
    evidence_dir = paths["archive"] / f"{task_id}-evidence"
    moves, dir_moves, file_moves, top_moves = {}, {}, {}, []
    _legacy_task_moves(root, paths, task_id, evidence_dir, moves, dir_moves,
                       file_moves, top_moves, safe_path, require,
                       include_verification=False)
    return {"atype": "repair_task_materials", "task": task_id,
            "old_path": path, "new_path": path, "evidence_dir": evidence_dir,
            "moves": moves, "dir_moves": dir_moves, "file_moves": file_moves,
            "top_moves": top_moves, "materials": [], "external": [],
            "reason": action["reason"]}


def _plan_close(root, catalog, paths, records, action, at, ctx,
                safe_path, require):
    """Закрытие карточки: карточка и доказательства → архив."""
    task_id = action["task"]
    rec = records.get(task_id)
    require(rec is not None, f"Нет задачи: {task_id}")
    require(not rec["archived"], f"{task_id}: карточка уже в архиве")
    outcome = action["outcome"]
    require(rec["status"] not in FINAL_TASK_STATES,
            f"{task_id}: уже {rec['status']} — перенесите в архив вручную")
    if outcome == "done":
        require(rec["status"] == "in_review",
                f"{task_id}: done только из in_review (после аудита), "
                f"сейчас {rec['status']}")
    old_path = rec["path"]
    new_path = paths["archive"] / old_path.name
    require(not new_path.exists() and not new_path.is_symlink(),
            f"В архиве уже есть карточка: {new_path.name}")
    _free_name(paths["archive"], new_path, root, require)
    stamp_dt = dt.datetime.fromisoformat(at)
    require(rec["created_ts"] is None or stamp_dt >= rec["created_ts"],
            f"{task_id}: Closed раньше Created")
    rel = _rel(root, old_path)
    text, meta = _task_meta(old_path, rel, require)
    task_meta = _task_meta_entry(catalog, task_id, meta, require)
    if outcome == "done":
        lines = ctx["unconfirmed"](text)
        require(not lines,
                f"{task_id}: модель не подтверждена — маркер "
                f"'{ctx['model_unconfirmed']}' в карточке (строки "
                f"{', '.join(map(str, lines))}). Инженер вписывает точное "
                "название или 'неизвестна' вместо маркера, затем повторяет "
                "close")
    evidence_dir = new_path.parent / f"{task_id}-evidence"
    moves, dir_moves, file_moves = {}, {}, {}
    top_moves, materials, external = [], [], []
    if task_meta is not None:
        _close_evidence(root, task_id, rec, text, outcome, task_meta,
                        safe_path, require)
        confirmed = set(action.get("confirmed_materials") or [])
        material_ids = {item.get("id") for item in task_meta["materials"]
                        if isinstance(item.get("id"), str)}
        extra = sorted(confirmed - material_ids)
        require(not extra,
                f"{task_id}: confirmed_materials — неизвестные id {extra}")
        unconfirmed_ext = sorted(
            item["id"] for item in task_meta["materials"]
            if item.get("storage") == "external" and item.get("required")
            and isinstance(item.get("id"), str)
            and item["id"] not in confirmed)
        require(not unconfirmed_ext,
                f"{task_id}: обязательные внешние материалы без "
                f"подтверждения доступа {unconfirmed_ext} — перечислите их "
                "в confirmed_materials")
        stray = [item["path"] for item in catalog["candidates"]
                 if item.get("owner") == task_id]
        require(not stray,
                f"{task_id}: неучтённые материалы {sorted(stray)} — "
                "зарегистрируйте их в materials либо уберите перед "
                "закрытием (lifecycle handoff регистрирует прогоны verify)")
        protected = _protected_files(paths, records, catalog)
        shift = _move_materials(root, task_id, task_meta["materials"],
                                old_path, paths["tasks"], paths["archive"],
                                evidence_dir, protected, confirmed,
                                safe_path, require)
        moves, dir_moves, file_moves = (shift["moves"], shift["dir_moves"],
                                        shift["file_moves"])
        top_moves = shift["top_moves"]
        materials, external = shift["materials"], shift["external"]
    else:
        _legacy_task_moves(root, paths, task_id, evidence_dir, moves,
                           dir_moves, file_moves, top_moves, safe_path,
                           require)
    moves[old_path.resolve()] = new_path.resolve()
    file_moves[old_path.resolve()] = new_path.resolve()
    top_moves.append((old_path.resolve(), new_path.resolve()))
    return {"atype": "close_task", "task": task_id, "record": rec,
            "outcome": outcome, "reason": action.get("reason"),
            "acceptance": action.get("acceptance"), "meta": meta,
            "task_meta": task_meta,
            "old_path": old_path, "new_path": new_path,
            "evidence_dir": evidence_dir, "moves": moves,
            "dir_moves": dir_moves, "file_moves": file_moves,
            "top_moves": top_moves,
            "materials": materials, "external": external,
            "blocked": []}


def _plan_reopen(root, catalog, paths, records, action, at, ctx,
                 safe_path, require):
    """Возврат архивной карточки в работу; история остаётся в материалах."""
    task_id = action["task"]
    rec = records.get(task_id)
    require(rec is not None, f"Нет задачи: {task_id}")
    require(rec["archived"],
            f"{task_id}: карточка не в архиве — возврат применим "
            "к закрытым")
    old_path = rec["path"]
    new_path = paths["tasks"] / old_path.name
    require(not new_path.exists() and not new_path.is_symlink(),
            f"Назначение занято: {_rel(root, new_path)}")
    _free_name(paths["tasks"], new_path, root, require)
    rel = _rel(root, old_path)
    _, meta = _task_meta(old_path, rel, require)
    task_meta = _task_meta_entry(catalog, task_id, meta, require)
    evidence_dir = new_path.parent / f"{task_id}-evidence"
    moves, dir_moves, file_moves = {}, {}, {}
    top_moves, materials, external = [], [], []
    if task_meta is not None:
        stray = [item["path"] for item in catalog["candidates"]
                 if item.get("owner") == task_id]
        require(not stray,
                f"{task_id}: неучтённые материалы {sorted(stray)} — "
                "зарегистрируйте их в materials либо уберите перед возвратом")
        protected = _protected_files(paths, records, catalog)
        shift = _move_materials(root, task_id, task_meta["materials"],
                                old_path, paths["archive"], paths["tasks"],
                                evidence_dir, protected, set(),
                                safe_path, require)
        moves, dir_moves, file_moves = (shift["moves"], shift["dir_moves"],
                                        shift["file_moves"])
        top_moves = shift["top_moves"]
        materials, external = shift["materials"], shift["external"]
    else:
        _legacy_task_moves(root, paths, task_id, evidence_dir, moves,
                           dir_moves, file_moves, top_moves, safe_path,
                           require, home=paths["archive"],
                           dest=paths["tasks"])
    moves[old_path.resolve()] = new_path.resolve()
    file_moves[old_path.resolve()] = new_path.resolve()
    top_moves.append((old_path.resolve(), new_path.resolve()))
    return {"atype": "reopen", "task": task_id, "record": rec,
            "status": action["status"], "reason": action["reason"],
            "meta": meta, "task_meta": task_meta,
            "old_path": old_path, "new_path": new_path,
            "evidence_dir": evidence_dir, "moves": moves,
            "dir_moves": dir_moves, "file_moves": file_moves,
            "top_moves": top_moves,
            "materials": materials, "external": external,
            "blocked": []}


def _plan_promote(root, catalog, paths, action, at, safe_path, require):
    """Promotion принятого изменения: новая ревизия требований."""
    doc_id = action["document"]
    entry = catalog["documents"].get(doc_id)
    require(entry is not None,
            f"{doc_id}: нет зарегистрированного документа — legacy сначала "
            "регистрируется командой document register")
    require(not entry["archived"],
            f"{doc_id}: документ в архиве — promotion невозможен")
    change_id = action["change"]
    change = next((item for item in entry["changes"]
                   if item.get("id") == change_id), None)
    require(change is not None,
            f"{doc_id}: нет изменения '{change_id}' в метаданных")
    require(change.get("state") == "implemented",
            f"{doc_id}: изменение '{change_id}' в состоянии "
            f"'{change.get('state')}' — promotion требует implemented")
    old_path = entry["path"]
    rel = _rel(root, old_path)
    errors = []
    meta = documents.parse_meta(documents.read_text(old_path),
                                rel, errors)
    require(not errors and meta is not None,
            f"{rel}: метаданные не разбираются")
    reference = action.get("acceptance")
    require(reference is not None, f"{doc_id}: нужна запись приёмки promotion")
    receipt_path = safe_path(root, reference)
    require(receipt_path.is_file(), f"{doc_id}: нет файла приёмки {reference}")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        require(False, f"{doc_id}: неверный JSON приёмки: {exc}")
    expected = {"schema_version": 1, "document": doc_id, "change": change_id,
                "revision": entry["revision"], "result": "accepted",
                "kind": "acceptance",
                "document_sha256": file_operations.sha256(old_path.read_bytes()),
                "payload_sha256": file_operations.sha256(action["payload"].encode("utf-8"))}
    require(isinstance(receipt, dict) and all(receipt.get(k) == v for k, v in expected.items()),
            f"{doc_id}: приёмка не соответствует изменению или редакции")
    actor = receipt.get("actor", {})
    require(isinstance(actor, dict) and actor.get("role") in {"auditor", "engineer"}
            and isinstance(actor.get("name"), str) and actor["name"].strip()
            and isinstance(receipt.get("at"), str)
            and documents._iso(receipt["at"]) is not None,
            f"{doc_id}: приёмка требует автора, роль и время")
    evidence_dir = old_path.parent / f"{old_path.stem}-evidence"
    stray = [item["path"] for item in catalog["candidates"]
             if item.get("owner") == doc_id]
    require(not stray,
            f"{doc_id}: неучтённые материалы {sorted(stray)} — "
            "зарегистрируйте их в materials либо уберите перед promotion")
    return {"atype": "promote_change", "document": doc_id,
            "entry": entry, "change": change_id, "payload": action["payload"],
            "acceptance": reference, "acceptance_record": receipt, "meta": meta,
            "old_path": old_path, "evidence_dir": evidence_dir,
            "revision_from": entry["revision"],
            "revision_to": entry["revision"] + 1,
            "moves": {}, "dir_moves": {}, "file_moves": {}, "top_moves": [],
            "materials": [], "external": [], "blocked": []}


def _splice_meta(text, meta):
    """Заменить блок метаданных новым содержимым, тело не меняется."""
    positions = documents._meta_positions(text.splitlines())
    if positions is None or isinstance(positions, str):
        return None
    start, end = positions
    raw = json.dumps(meta, ensure_ascii=False, indent=2)
    block = [documents.META_START, "```json", *raw.splitlines(), "```",
             documents.META_END]
    lines = text.split("\n")
    return "\n".join(lines[:start] + block + lines[end + 1:])


def _emptied_dirs(root, doc_parent_rel, removes, require):
    """Каталоги под активным каталогом документа, опустевшие после removes."""
    candidates = set()
    parent = PurePosixPath(doc_parent_rel)
    for rel in removes:
        current = PurePosixPath(rel).parent
        while current != parent and current.is_relative_to(parent):
            candidates.add(str(current))
            current = current.parent
    result = []
    for rel in sorted(candidates,
                      key=lambda item: -len(PurePosixPath(item).parts)):
        path = root / rel
        if not path.is_dir():
            continue
        files = [item for item in path.rglob("*") if item.is_file()]
        if files and all(_rel(root, item) in removes for item in files):
            result.append(rel)
    return result



def _validate_moved_task_meta(root, meta, rel, errors, safe_path):
    data = json.loads((root / 'ai-dev-playbook/project.json').read_text(encoding='utf-8'))
    tw = data.get('task_workflow')
    documents._validate_task_meta(meta, rel, errors,
        registry={item['id']: item for item in tw['registry']} if tw else None,
        task_workflow_enabled=tw is not None, root=root, safe_path=safe_path,
        block_enabled=block_workflow.CAPABILITY in documents.capabilities_of(data))

def _remap_meta_materials(meta, moves, companion_rel, plan_id, root,
                          safe_path):
    """Источники локальных материалов по карте переносов + запись
    сопровождающего доказательства этой операции."""
    if meta.get('schema_version') == 2 and meta.get('workflow', {}).get('baseline'):
        meta['workflow'] = copy.deepcopy(meta['workflow'])
        baseline = meta['workflow']['baseline']
        baseline['source'] = document_links.map_path(
            safe_path(root, baseline['source']).resolve(), moves).relative_to(root).as_posix()
    new_materials = []
    for material in meta.get("materials") or []:
        item = dict(material)
        if item.get("storage") == "local":
            mapped = document_links.map_path(
                safe_path(root, item["source"]).resolve(), moves)
            item["source"] = mapped.relative_to(root).as_posix()
        new_materials.append(item)
    new_materials.append({"id": f"lifecycle-{plan_id}", "role": "report",
                          "source": companion_rel, "storage": "local",
                          "required": True, "immutable": True,
                          "used_by": []})
    return new_materials


def _claim_target(inputs, rel, root, safe_path, require):
    """Новый путь операции: свободен и отмечен в снимке входов."""
    require(inputs.get(rel, "missing") == "missing"
            and not safe_path(root, rel).exists(),
            f"Назначение занято: {rel}")
    inputs[rel] = "missing"


def _unit_summary(root, unit):
    """Строка сводки действия для плана и предпросмотра."""
    atype = unit["atype"]
    if atype == "repair_task_materials":
        return (f"repair_task_materials {unit['task']}: "
                f"материалы → {_rel(root, unit['evidence_dir'])}; "
                "статус и Closed сохранены")
    if atype == "archive_document":
        return (f"archive_document {unit['document']}: "
                f"{_rel(root, unit['old_path'])} → "
                f"{_rel(root, unit['new_path'])} "
                f"(материалов: {len(unit['materials'])}, "
                f"причина: {unit['reason']})")
    if atype == "close_task":
        return (f"close_task {unit['task']}: "
                f"{_rel(root, unit['old_path'])} → "
                f"{_rel(root, unit['new_path'])} ({unit['outcome']})")
    if atype == "reopen":
        blocked = (f"; заблокированы зависимые: {', '.join(unit['blocked'])}"
                   if unit["blocked"] else "")
        return (f"reopen {unit['task']}: "
                f"{_rel(root, unit['old_path'])} → "
                f"{_rel(root, unit['new_path'])} "
                f"({unit['status']}){blocked}")
    return (f"promote_change {unit['document']}: ревизия "
            f"{unit['revision_from']} → {unit['revision_to']}, "
            f"изменение '{unit['change']}' принято")


def _companion_body(unit, plan_id, at, norm, root):
    """Сопровождающий JSON-доказательство действия."""
    files = [{"from": _rel(root, old), "to": _rel(root, new),
              "sha256": file_operations.scoped_operations.file_digest(old),
              "bytes": old.stat().st_size}
             for old, new in sorted(unit["file_moves"].items())]
    body = {"schema_version": 1, "plan_id": plan_id, "at": at,
            "actor": norm["actor"], "basis": norm["basis"],
            "action": unit["atype"],
            "files": files,
            "moves": [{"from": _rel(root, old), "to": _rel(root, new)}
                      for old, new in unit["top_moves"]],
            "materials": unit["materials"],
            "external": unit["external"]}
    atype = unit["atype"]
    if atype == "archive_document":
        body.update(type="lifecycle-archive", document=unit["document"],
                    reason=unit["reason"], successor=unit["successor"])
    elif atype == "repair_task_materials":
        body.update(type="lifecycle-material-repair", task=unit["task"],
                    reason=unit["reason"])
    elif atype == "close_task":
        task_meta = unit["task_meta"] or {}
        body.update(type="lifecycle-close", task=unit["task"],
                    outcome=unit["outcome"],
                    previous_status=unit["record"]["status"],
                    reason=unit["reason"],
                    acceptance_ref=unit["acceptance"],
                    policy="managed" if unit["meta"] is not None
                    else "legacy",
                    handoff=task_meta.get("handoff"),
                    acceptance=task_meta.get("acceptance"))
    elif atype == "reopen":
        body.update(type="lifecycle-reopen", task=unit["task"],
                    status=unit["status"], reason=unit["reason"],
                    previous_status=unit["record"]["status"],
                    previous_closed=unit["record"]["closed"],
                    policy="managed" if unit["meta"] is not None
                    else "legacy",
                    blocked=unit["blocked"])
    else:  # promote_change
        body.update(type="lifecycle-promote", document=unit["document"],
                    change=unit["change"],
                    revision_from=unit["revision_from"],
                    revision_to=unit["revision_to"],
                    acceptance=unit["acceptance"],
                    acceptance_record=unit["acceptance_record"],
                    payload_sha256=file_operations.sha256(
                        unit["payload"].encode("utf-8")))
    return body


def _write_close(root, unit, moves, inputs, plan_id, at, projected_records,
                 safe_path, require):
    """Запись закрываемой карточки: статус, Closed, метаданные, ссылки."""
    old_path, new_path = unit["old_path"], unit["new_path"]
    old_rel, new_rel = _rel(root, old_path), _rel(root, new_path)
    companion_rel = _rel(root, unit["evidence_dir"]
                         / f"close-{plan_id}.json")
    _claim_target(inputs, companion_rel, root, safe_path, require)
    text = _close_text(documents.read_text(old_path),
                       unit["outcome"], at, unit["task"], require)
    if unit["meta"] is not None:
        meta = dict(unit["meta"])
        meta["moves"] = list(meta.get("moves") or []) + [
            {"from": _rel(root, old), "to": _rel(root, new)}
            for old, new in unit["top_moves"]]
        meta["materials"] = _remap_meta_materials(meta, moves, companion_rel,
                                                  plan_id, root, safe_path)
        errors = []
        _validate_moved_task_meta(root, meta, new_rel, errors, safe_path)
        require(not errors, "\n".join(errors))
        merged = _splice_meta(text, meta)
        require(merged is not None, f"{old_rel}: блок метаданных не найден")
        text = merged
    rewritten = document_links.rewrite(text, old_path.resolve(),
                                       new_path.resolve(), moves)
    text = rewritten["text"]
    if unit["meta"] is not None:
        meta_updated = False
        parsed_closed = None
        if meta.get("criterion_evidence"):
            parsed_closed = task_cards.parse(text, new_rel)
            entries_by_id = {e["id"]: e for e in parsed_closed["entries"]}
            updated_evidence = dict(meta["criterion_evidence"])
            for cid, b in updated_evidence.items():
                if cid in entries_by_id:
                    updated_evidence[cid] = dict(b, check_sha256=entries_by_id[cid]["sha256"])
            meta["criterion_evidence"] = updated_evidence
            meta_updated = True
        if meta.get("handoff") and "entry_id" in meta["handoff"]:
            if parsed_closed is None:
                parsed_closed = task_cards.parse(text, new_rel)
            entries_by_id = {e["id"]: e for e in parsed_closed["entries"]}
            hid = meta["handoff"]["entry_id"]
            if hid in entries_by_id:
                meta["handoff"] = dict(meta["handoff"], journal_sha256=entries_by_id[hid]["journal_sha256"])
                meta_updated = True
        if meta.get("acceptance") and "audit_id" in meta["acceptance"]:
            if parsed_closed is None:
                parsed_closed = task_cards.parse(text, new_rel)
            entries_by_id = {e["id"]: e for e in parsed_closed["entries"]}
            aid = meta["acceptance"]["audit_id"]
            if aid in entries_by_id:
                meta["acceptance"] = dict(meta["acceptance"], audit_sha256=entries_by_id[aid]["sha256"])
                meta_updated = True
        if meta_updated:
            errors = []
            _validate_moved_task_meta(root, meta, new_rel, errors, safe_path)
            require(not errors, "\n".join(errors))
            merged = _splice_meta(text, meta)
            require(merged is not None, f"{new_rel}: блок метаданных не найден")
            text = merged
    writes_value = text.encode("utf-8")
    record = projected_records[unit["task"]]
    record.update(status=unit["outcome"], closed=at,
                  closed_ts=dt.datetime.fromisoformat(at),
                  path=new_path, archived=True)
    return new_rel, writes_value, companion_rel


def _write_reopen(root, unit, moves, inputs, plan_id, at, norm, records,
                  projected_records, writes, update_by_resolved, handled,
                  safe_path, require):
    """Возврат карточки и блокировка её активных зависимых."""
    old_path, new_path = unit["old_path"], unit["new_path"]
    old_rel, new_rel = _rel(root, old_path), _rel(root, new_path)
    companion_rel = _rel(root, unit["evidence_dir"]
                         / f"reopen-{plan_id}.json")
    _claim_target(inputs, companion_rel, root, safe_path, require)
    text = _reopen_text(documents.read_text(old_path),
                        unit["record"], unit["status"], unit["reason"],
                        at, norm["actor"], require)
    if unit["meta"] is not None:
        meta = dict(unit["meta"])
        # Переданное состояние приёмки сбрасывается: история остаётся
        # в архивных материалах и сопровождающих JSON.
        meta.pop("handoff", None)
        meta.pop("acceptance", None)
        meta.pop("criterion_evidence", None)
        if task_cards.version(text) == 2:
            text = task_cards.render_checkboxes(text, {key: {"checked": False} for key in task_cards.parse(text)["criteria"]})
        meta["moves"] = list(meta.get("moves") or []) + [
            {"from": _rel(root, old), "to": _rel(root, new)}
            for old, new in unit["top_moves"]]
        meta["materials"] = _remap_meta_materials(meta, moves, companion_rel,
                                                  plan_id, root, safe_path)
        errors = []
        _validate_moved_task_meta(root, meta, new_rel, errors, safe_path)
        require(not errors, "\n".join(errors))
        merged = _splice_meta(text, meta)
        require(merged is not None, f"{old_rel}: блок метаданных не найден")
        text = merged
    rewritten = document_links.rewrite(text, old_path.resolve(),
                                       new_path.resolve(), moves)
    writes[new_rel] = rewritten["text"].encode("utf-8")
    record = projected_records[unit["task"]]
    record.update(status=unit["status"], closed="-", closed_ts=None,
                  path=new_path, archived=False)
    for dep_id in unit["blocked"]:
        dep_rec = records[dep_id]
        dep_path = dep_rec["path"]
        dep_text = _blocked_text(documents.read_text(dep_path),
                                 dep_id, unit["task"], at, norm["actor"],
                                 require)
        dep_text = document_links.rewrite(
            dep_text, dep_path.resolve(), dep_path.resolve(),
            moves)["text"]
        writes[_rel(root, dep_path)] = dep_text.encode("utf-8")
        handled.add(dep_path.resolve())
        projected_records[dep_id]["status"] = "blocked"
    return companion_rel


def _write_promote(root, unit, inputs, plan_id, at, projected_docs,
                   safe_path, require):
    """Promotion: новое тело документа + снимок прежней редакции."""
    old_path = unit["old_path"]
    old_rel = _rel(root, old_path)
    revision_rel = _rel(root, unit["evidence_dir"]
                        / f"revision-{unit['revision_from']}.md")
    companion_rel = _rel(root, unit["evidence_dir"]
                         / f"promote-{plan_id}.json")
    _claim_target(inputs, revision_rel, root, safe_path, require)
    _claim_target(inputs, companion_rel, root, safe_path, require)
    text = documents.read_text(old_path)
    positions = documents._meta_positions(text.splitlines())
    require(isinstance(positions, tuple),
            f"{old_rel}: нет блока метаданных")
    _, end = positions
    lines = text.split("\n")
    body_old = "\n".join(lines[end + 1:]).strip("\n")
    new_text = "\n".join(lines[:end + 1]) + "\n\n" \
        + unit["payload"].rstrip("\n") + "\n"
    meta = dict(unit["meta"])
    meta["current_revision"] = unit["revision_to"]
    meta["updated_at"] = at
    changes = []
    for change in meta.get("changes") or []:
        item = dict(change)
        if item.get("id") == unit["change"]:
            item["state"] = "accepted"
            item["acceptance"] = companion_rel
        changes.append(item)
    meta["changes"] = changes
    material_ids = {item.get("id")
                    for item in meta.get("materials") or []}
    for ident in (f"revision-{unit['revision_from']}",
                  f"lifecycle-{plan_id}"):
        require(ident not in material_ids,
                f"{unit['document']}: материал '{ident}' уже "
                "зарегистрирован")
    meta["materials"] = list(meta.get("materials") or []) + [
        {"id": f"revision-{unit['revision_from']}", "role": "other",
         "source": revision_rel, "storage": "local",
         "required": True, "immutable": True, "used_by": []},
        {"id": f"lifecycle-{plan_id}", "role": "report",
         "source": companion_rel, "storage": "local",
         "required": True, "immutable": True, "used_by": []}]
    errors = []
    _caps = documents.capabilities_of(json.loads(
        (root / 'ai-dev-playbook/project.json').read_text(encoding='utf-8')))
    documents._validate_document(meta, old_path, unit["entry"]["kind"],
                                 False, errors, capabilities=_caps)
    require(not errors, "\n".join(errors))
    merged = _splice_meta(new_text, meta)
    require(merged is not None, f"{old_rel}: блок метаданных не найден")
    # Снимок прежней редакции без блока метаданных (файл вне коллекции);
    # ссылки пересчитаны под расположение в evidence-каталоге.
    revision_path = safe_path(root, revision_rel)
    title = documents._doc_title(text) or old_path.stem
    snapshot = (f"# {title} — редакция {unit['revision_from']}\n\n"
                f"Снимок прежней редакции {unit['document']} "
                f"(зафиксирована при promotion {at}).\n\n"
                + body_old + "\n")
    snapshot = document_links.rewrite(
        snapshot, old_path.resolve(), revision_path.resolve(), {})["text"]
    record = dict(unit["entry"])
    record["revision"], record["updated_at"] = unit["revision_to"], at
    record["changes"] = changes
    projected_docs[unit["entry"]["id"]] = record
    return {old_rel: merged.encode("utf-8"),
            revision_rel: snapshot.encode("utf-8")}, companion_rel


def build_plan(root, request, at, ctx, safe_path, require):
    """(plan, writes) — детерминированный план по запросу и текущему дереву.

    writes — {rel: bytes}; в plan записываются только sha256 содержимого.
    Повторный вызов при неизменных входах и том же ``at`` даёт идентичный
    результат — именно это проверяет apply.
    """
    physical = file_operations.scoped_operations.physical_root(root)
    root = file_operations.scoped_operations.project_view.ProjectView(physical).path
    data, paths = ctx["config"](root)
    records = ctx["read_tasks"](root, paths)
    norm = validate_request(request, require)
    actions = norm["actions"]
    repair_tasks = {item["task"] for item in actions
                    if item["action"] == "repair_task_materials"}
    catalog = documents.load_catalog(root, data, paths, records,
                                     safe_path, require, repair_tasks=repair_tasks)
    doc_targets = {item["document"] for item in actions
                   if "document" in item}
    for item in actions:
        if item["action"] == "archive_document":
            require(item["successor"] not in doc_targets,
                    f"{item['document']}: преемник '{item['successor']}' сам "
                    "архивируется этой операцией")
    units = []
    for item in actions:
        kind = item["action"]
        if kind == "archive_document":
            require(catalog is not None,
                    "Раздел documents не настроен в project.json")
            units.append(_plan_archive(root, catalog, paths, item, at,
                                       safe_path, require))
        elif kind == "promote_change":
            require(catalog is not None,
                    "Раздел documents не настроен в project.json")
            units.append(_plan_promote(root, catalog, paths, item, at,
                                       safe_path, require))
        elif kind == "repair_task_materials":
            units.append(_plan_repair(root, catalog, paths, records, item,
                                      safe_path, require))
        elif kind == "close_task":
            units.append(_plan_close(root, catalog, paths, records, item,
                                     at, ctx, safe_path, require))
        else:
            units.append(_plan_reopen(root, catalog, paths, records, item,
                                      at, ctx, safe_path, require))
    # Целевой граф статусов: действия + автоблокировка зависимых
    # возвращённых карточек; недопустимый результат отклоняется.
    status_map = {ident: rec["status"] for ident, rec in records.items()}
    targeted = set()
    for unit in units:
        if unit["atype"] == "close_task":
            status_map[unit["task"]] = unit["outcome"]
            targeted.add(unit["task"])
        elif unit["atype"] == "reopen":
            status_map[unit["task"]] = unit["status"]
            targeted.add(unit["task"])
    for unit in (item for item in units if item["atype"] == "reopen"):
        for other in records.values():
            if unit["task"] not in other["deps"] or other["archived"] \
                    or other["id"] in targeted:
                continue
            if status_map[other["id"]] in BLOCKABLE_STATES:
                status_map[other["id"]] = "blocked"
                unit["blocked"].append(other["id"])
        unit["blocked"].sort()
    bad = [(ident, dep) for ident, rec in records.items()
           if status_map[ident] in {"ready", "in_progress", "in_review",
                                    "done"}
           for dep in rec["deps"] if status_map[dep] != "done"]
    require(not bad,
            "Операция оставила бы недопустимый граф статусов: " + "; ".join(
                f"{ident} требует завершённый {dep}"
                for ident, dep in sorted(bad)))
    for unit in units:
        if unit["atype"] == "archive_document" and unit["entry"]["kind"] == "plan":
            open_tasks = [task for task in unit["entry"]["tasks"]
                          if status_map.get(task) not in FINAL_TASK_STATES]
            require(not open_tasks,
                    f"{unit['document']}: план с незавершёнными задачами {open_tasks}")
    # Sidecars are part of the same move/input/remove sets as their card.
    for unit in units:
        old = unit.get('old_path')
        new = unit.get('new_path')
        if old is not None and new is not None and unit['atype'] in {'close_task', 'reopen'}:
            if documents.external_card(documents.read_text(old)):
                source, target = documents.meta_path(old).resolve(), documents.meta_path(new).resolve()
                safe_path(root, _rel(root, source))
                safe_path(root, _rel(root, target))
                unit['moves'][source] = target
                unit['file_moves'][source] = target
                unit['top_moves'].append((source, target))
    # Единая карта переносов: конфликт назначений (разные источники в один
    # путь) — отказ; один источник в moves и dir_moves дублируется законно.
    moves, file_moves, destinations = {}, {}, {}
    for unit in units:
        for old, new in unit["moves"].items():
            require(old not in moves or moves[old] == new,
                    f"Перенос {_rel(root, old)} конфликтует между действиями")
            moves[old] = new
        file_moves.update(unit["file_moves"])
        for old, new in unit["dir_moves"].items():
            moves.setdefault(old, new)
        for old, new in list(unit["moves"].items()) + \
                list(unit["dir_moves"].items()):
            previous = destinations.setdefault(new, old)
            require(previous == old,
                    f"Коллизия назначений: {_rel(root, new)} от "
                    f"{_rel(root, previous)} и {_rel(root, old)}")
    destinations = sorted(destinations)
    # Записи: документы/карточки на новых местах, материалы, затронутые
    # ссылки, индексы, сопровождающие JSON. Удаления: исходные пути.
    handled = {unit["old_path"].resolve() for unit in units
               if "old_path" in unit}
    removes = sorted(_rel(root, old) for old in file_moves)
    remove_dirs = []
    for unit in units:
        if "old_path" not in unit:
            continue
        doc_rel = _rel(root, unit["old_path"].parent)
        remove_dirs += _emptied_dirs(root, doc_rel, set(removes), require)
        remove_dirs += [_rel(root, item) for item in unit["dir_moves"]]
    remove_dirs = sorted(set(remove_dirs))
    # Пустые переносимые каталоги пересоздаются в архиве явно.
    create_dirs = sorted({
        _rel(root, dst_dir)
        for unit in units for src_dir, dst_dir in unit["dir_moves"].items()
        if not any(item.is_file() for item in src_dir.rglob("*"))})
    # Сканируемый набор ссылок: весь проектный scan-set + переносимые .md
    # материалы вне него (например, в work/verification).
    scan = [item for item in ctx["scan_files"](root, paths)
            if item.is_file()
            and item.resolve() != paths["task_index"].resolve()]
    known = {item.resolve() for item in scan}
    scan += [old for old in sorted(file_moves)
             if old.suffix == ".md" and old not in known]
    managed = [safe_path(root, ctx["core_prefix"]).resolve()]
    frozen = list(catalog["immutable"]) if catalog is not None else []
    frozen += [old for unit in units if unit["atype"] == "repair_task_materials"
               for old, _ in unit["top_moves"]]
    result = document_links.plan_rewrites(
        scan, moves, root.resolve(), managed=managed, frozen=frozen)
    require(not result["conflicts"],
            "Ссылки требуют ручного разбора:\n" + "\n".join(
                f"  {item['path']}:{item['line']} {item['kind']} "
                f"{item['detail']}" for item in result["conflicts"]))
    updates = result["updates"]
    index_paths = ({cols["index"].resolve()
                    for cols in catalog["collections"].values()}
                   if catalog is not None else set())
    # Входы: весь scan-set ссылок, карточки, конфигурация, переносимое и
    # удаляемое содержимое, отсутствие будущих путей и пустых каталогов.
    input_rels = {_rel(root, item) for item in scan}
    input_rels.add(_rel(root, paths["task_index"]))
    input_rels.update(_rel(root, rec["path"]) for rec in records.values())
    input_rels.update(_rel(root, documents.meta_path(rec['path']))
                      for rec in records.values()
                      if documents.meta_path(rec['path']).is_file())
    input_rels.add(ctx["core_prefix"] + "/project.json")
    input_rels.add(ctx["core_prefix"] + "/VERSION")
    input_rels.update(removes)
    input_rels.update(remove_dirs)
    input_rels.update(_rel(root, dst) for dst in destinations)
    input_rels.update(create_dirs)
    input_rels.update(unit["acceptance"] for unit in units
                      if unit["atype"] == "promote_change")
    if catalog is not None:
        input_rels.update(_rel(root, col["index"])
                          for col in catalog["collections"].values())
    inputs = file_operations.snapshot_paths(root, sorted(input_rels),
                                            safe_path, require)
    for dst in destinations:
        require(inputs[_rel(root, dst)] == "missing",
                f"Назначение занято: {_rel(root, dst)}")
    # plan_id привязан к запросу и пред-образу входов, но не к байтам
    # записей: operation_id и материалы-доказательства содержат plan_id.
    plan_id = "lc-" + file_operations.sha256(_canonical(
        {"request": norm, "created_at": at, "inputs": inputs,
         "moves": {_rel(root, old): _rel(root, new)
                   for old, new in sorted(moves.items())},
         "removes": removes, "remove_dirs": remove_dirs,
         "create_dirs": create_dirs}).encode("utf-8"))[7:23]
    writes = {}
    update_by_resolved = {path.resolve(): info
                          for path, info in updates.items()}
    projected_docs = dict(catalog["documents"]) if catalog is not None else {}
    projected_records = {ident: dict(rec)
                         for ident, rec in records.items()}
    companions = []
    for unit in units:
        atype = unit["atype"]
        if atype == "archive_document":
            entry, old_path, new_path = \
                unit["entry"], unit["old_path"], unit["new_path"]
            old_rel = _rel(root, old_path)
            companion_rel = _rel(root, unit["evidence_dir"]
                                 / f"archive-{plan_id}.json")
            _claim_target(inputs, companion_rel, root, safe_path, require)
            meta_errors = []
            meta = documents.parse_meta(
                documents.read_text(old_path), old_rel, meta_errors)
            require(not meta_errors and meta is not None,
                    f"{old_rel}: метаданные не разбираются")
            meta = dict(meta)
            meta["archive"] = {"at": at, "reason": unit["reason"],
                               "successor": unit["successor"],
                               "operation_id": plan_id,
                               "original_path": old_rel}
            meta["updated_at"] = at
            meta["moves"] = list(meta.get("moves") or []) + [
                {"from": _rel(root, old), "to": _rel(root, new)}
                for old, new in unit["top_moves"]]
            meta["materials"] = _remap_meta_materials(
                meta, moves, companion_rel, plan_id, root, safe_path)
            errors = []
            _caps = documents.capabilities_of(json.loads(
                (root / 'ai-dev-playbook/project.json')
                .read_text(encoding='utf-8')))
            documents._validate_document(meta, new_path, entry["kind"], True,
                                         errors, capabilities=_caps)
            require(not errors, "\n".join(errors))
            merged = _splice_meta(documents.read_text(old_path),
                                  meta)
            require(merged is not None,
                    f"{old_rel}: блок метаданных не найден")
            rewritten = document_links.rewrite(
                merged, old_path.resolve(), new_path.resolve(), moves)
            writes[_rel(root, new_path)] = \
                rewritten["text"].encode("utf-8")
            record = dict(entry)
            record["path"], record["archived"] = new_path, True
            record["archive"], record["updated_at"] = meta["archive"], at
            projected_docs[entry["id"]] = record
            companions.append((companion_rel, unit))
        elif atype == "repair_task_materials":
            old_path = unit["old_path"]
            companion_rel = _rel(root, unit["evidence_dir"] / f"repair-{plan_id}.json")
            _claim_target(inputs, companion_rel, root, safe_path, require)
            meta = {"schema_version": 1, "relations": [],
                    "materials": [{"id": "recovered-evidence", "role": "report",
                                   "source": _rel(root, unit["evidence_dir"]),
                                   "storage": "local", "required": True,
                                   "immutable": True, "used_by": []}],
                    "moves": [{"from": _rel(root, old), "to": _rel(root, new)}
                              for old, new in unit["top_moves"]]}
            text = documents.read_text(old_path)
            rewritten = document_links.rewrite(text, old_path.resolve(),
                                               old_path.resolve(), moves)["text"]
            raw = json.dumps(meta, ensure_ascii=False, indent=2)
            writes[_rel(root, old_path)] = (rewritten.rstrip("\n") + "\n\n"
                + documents.META_START + "\n```json\n" + raw + "\n```\n"
                + documents.META_END + "\n").encode("utf-8")
            companions.append((companion_rel, unit))
        elif atype == "close_task":
            new_rel, content, companion_rel = _write_close(
                root, unit, moves, inputs, plan_id, at,
                projected_records, safe_path, require)
            writes[new_rel] = content
            companions.append((companion_rel, unit))
        elif atype == "reopen":
            companion_rel = _write_reopen(
                root, unit, moves, inputs, plan_id, at, norm, records,
                projected_records, writes, update_by_resolved, handled,
                safe_path, require)
            companions.append((companion_rel, unit))
        else:  # promote_change
            produced, companion_rel = _write_promote(
                root, unit, inputs, plan_id, at, projected_docs,
                safe_path, require)
            writes.update(produced)
            companions.append((companion_rel, unit))
    # Материалы и прочие переносимые файлы — байты на новых путях, плюс
    # переписанные непереносимые файлы с затронутыми ссылками.
    for old, new in sorted(file_moves.items()):
        if old in handled:
            continue
        info = update_by_resolved.get(old)
        if info is not None:
            writes[_rel(root, new)] = info["updated"].encode("utf-8")
        else:
            writes[_rel(root, new)] = file_operations.scoped_operations.Payload(old)
    for path, info in updates.items():
        resolved = path.resolve()
        if resolved in file_moves or resolved in index_paths \
                or resolved in handled:
            continue
        writes[_rel(root, resolved)] = info["updated"].encode("utf-8")
    # Индексы затронутых коллекций — перегенерация области + правка ссылок
    # ручной части.
    if catalog is not None:
        projected = {**catalog, "documents": projected_docs,
                     "records": projected_records}
        document_kinds = {unit["entry"]["kind"] for unit in units
                          if "entry" in unit}
        for kind in sorted(catalog["collections"]):
            col = catalog["collections"][kind]
            index = col["index"]
            if not index.exists():
                if kind not in document_kinds:
                    continue
                index_write = ctx["collection_index_write"](root, projected,
                                                            kind)
                if index_write is None:
                    continue
                rel, content = index_write
                text = document_links.rewrite(content.decode("utf-8"),
                                              index.resolve(), index.resolve(),
                                              moves)["text"]
                writes[rel] = text.encode("utf-8")
                continue
            index_write = ctx["collection_index_write"](root, projected, kind)
            if index_write is not None:
                rel, content = index_write
                base_text = content.decode("utf-8")
            else:
                rel = _rel(root, index)
                base_text = index.read_text(encoding="utf-8")
            rewritten = document_links.rewrite(base_text,
                                               index.resolve(), index.resolve(),
                                               moves)["text"]
            if rewritten != index.read_text(encoding="utf-8"):
                writes[rel] = rewritten.encode("utf-8")
    # Индекс задач — при любых действиях над карточками.
    if any(unit["atype"] in TASK_ACTIONS for unit in units):
        index = paths["task_index"]
        if index.exists():
            require(index.read_text(encoding="utf-8")
                    .startswith(ctx["task_index_header"]),
                    "Конфликт: индекс указывает на чужой документ; "
                    "запись отменена")
        writes[_rel(root, index)] = ctx["render_task_index"](
            projected_records, index.parent).encode("utf-8")
    # Сопровождающие JSON-доказательства — последним шагом, когда известен
    # полный состав переносов.
    for companion_rel, unit in companions:
        body = _companion_body(unit, plan_id, at, norm, root)
        writes[companion_rel] = \
            (json.dumps(body, ensure_ascii=False, indent=2) + "\n") \
            .encode("utf-8")
    writes = documents.split_card_writes(writes)
    summary = [_unit_summary(root, unit) for unit in units]
    plan_obj = {"schema_version": PLAN_SCHEMA, "kind": "lifecycle-plan",
                "plan_id": plan_id, "created_at": at, "root": str(root),
                "tool": {"name": "playbook.py",
                         "version": ctx["version"](root)},
                "request": norm, "summary": summary,
                "moves": {_rel(root, old): _rel(root, new)
                          for old, new in sorted(moves.items())},
                "inputs": inputs,
                "writes": {rel: file_operations.sha256(content)
                           for rel, content in sorted(writes.items())},
                "removes": removes, "remove_dirs": remove_dirs,
                "create_dirs": create_dirs, "read_set": json.loads(json.dumps(root.view.read_set.snapshot()))}
    return plan_obj, writes


def _actor_label(actor):
    label = f"{actor.get('name')} ({actor.get('role')}"
    if actor.get("model"):
        label += f", {actor['model']}"
    return label + ")"


def _preview(root, plan, writes):
    lines = [f"План {plan['plan_id']} (lifecycle):",
             f"Основание: {plan['request']['basis']}",
             f"Исполнитель: {_actor_label(plan['request']['actor'])}",
             "Действия:"]
    lines += [f"  {line}" for line in plan["summary"]]
    lines.append("Переносы:")
    lines += [f"  {old} → {new}"
              for old, new in sorted(plan["moves"].items())]
    if plan["create_dirs"]:
        lines.append("Создаются пустые каталоги:")
        lines += [f"  {rel}" for rel in plan["create_dirs"]]
    if plan["remove_dirs"]:
        lines.append("Удаляются каталоги:")
        lines += [f"  {rel}" for rel in plan["remove_dirs"]]
    lines.append(f"Записей: {len(plan['writes'])}, "
                 f"удалений файлов: {len(plan['removes'])}")
    lines.append("Diff записей (.md):")
    for rel in sorted(plan["writes"]):
        if isinstance(writes[rel], file_operations.scoped_operations.Payload):
            lines.append(f"  {rel}: prepared stream {writes[rel].sha256}")
            continue
        if not rel.endswith(".md"):
            continue
        path = root / rel
        old = path.read_text(encoding="utf-8") if path.is_file() else ""
        diff = difflib.unified_diff(old.splitlines(keepends=True),
                                    writes[rel].decode("utf-8")
                                    .splitlines(keepends=True),
                                    fromfile=f"a/{rel}" if old else "/dev/null",
                                    tofile=f"b/{rel}")
        lines.append("".join(diff).rstrip("\n"))
    return "\n".join(lines)


def _read_request(root, rel, safe_path, require):
    path = safe_path(root, rel)
    require(path.is_file() and not path.is_symlink(),
            f"Нет файла запроса: {rel}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"),
                          object_pairs_hook=documents._unique_pairs)
    except ValueError as error:
        require(False, f"{rel}: {error}")
    return data


def _save_plan(root, plan, safe_path, require):
    """Атомарная запись плана в work/lifecycle/plans/<plan_id>.json."""
    root = file_operations.scoped_operations.physical_root(root)
    directory = safe_path(root, PLANS_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{plan['plan_id']}.json"
    temp = directory / f".{plan['plan_id']}.{os.getpid()}.tmp"
    temp.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    os.replace(temp, target)
    return target


def plan_command(root, request_rel, save, ctx, safe_path, require):
    request = _read_request(root, request_rel, safe_path, require)
    plan, writes = build_plan(root, request, file_operations.now_iso(),
                              ctx, safe_path, require)
    print(_preview(root, plan, writes))
    if not save:
        print("\nПредпросмотр, без записи; сохранение плана: --save")
        return
    target = _save_plan(root, plan, safe_path, require)
    print(f"\nПлан записан: {_rel(root, target)}")
    print("Применение: playbook.py lifecycle apply "
          f"{_rel(root, target)}")


def _load_plan(root, rel, safe_path, require):
    path = safe_path(root, rel)
    require(path.is_file() and not path.is_symlink(),
            f"Нет файла плана: {rel}")
    try:
        plan = json.loads(path.read_text(encoding="utf-8"),
                          object_pairs_hook=documents._unique_pairs)
    except ValueError as error:
        require(False, f"{rel}: {error}")
    require(isinstance(plan, dict)
            and plan.get("kind") == "lifecycle-plan"
            and plan.get("schema_version") == PLAN_SCHEMA,
            f"{rel}: не план lifecycle-операции")
    for field in ("plan_id", "created_at", "root"):
        require(isinstance(plan.get(field), str) and plan[field].strip(),
                f"{rel}: нет поля '{field}'")
    require(isinstance(plan.get("request"), dict),
            f"{rel}: нет поля 'request'")
    for field in ("moves", "inputs", "writes"):
        require(isinstance(plan.get(field), dict),
                f"{rel}: поле '{field}' — объект")
    for field in ("removes", "remove_dirs", "create_dirs", "summary"):
        require(isinstance(plan.get(field), list),
                f"{rel}: поле '{field}' — список")
    require(plan["root"] == str(root),
            f"{rel}: план построен для другого корня проекта")
    return plan


def _consistency(ctx, kinds, has_tasks, label, safe_path, require):
    """Проверка каталога, индексов затронутых коллекций, индекса задач
    и ссылок дерева в проверяемом корне."""
    def check(staged_root):
        data, paths = ctx["config"](staged_root)
        records = ctx["read_tasks"](staged_root, paths)
        catalog = documents.load_catalog(staged_root, data, paths, records,
                                         safe_path, require)
        if kinds:
            require(catalog is not None,
                    f"{label}: каталог документов недоступен")
        errors = []
        if catalog is not None:
            for kind in kinds:
                index = catalog["collections"][kind]["index"]
                rel = index.relative_to(staged_root).as_posix()
                if not index.is_file():
                    errors.append(f"{rel}: нет файла индекса")
                    continue
                text = index.read_text(encoding="utf-8")
                expected = documents.splice_index(
                    text, documents.index_region(catalog, kind))
                if expected is None or expected != text:
                    errors.append(f"{rel}: индекс не соответствует каталогу")
        if has_tasks:
            index = paths["task_index"]
            if not index.is_file() \
                    or index.read_text(encoding="utf-8") \
                    != ctx["render_task_index"](records, index.parent):
                errors.append("индекс задач не соответствует карточкам")
        require(not errors, f"{label}: " + "; ".join(errors))
        ctx["check_links"](staged_root, paths, catalog)
        if catalog is not None:
            portable_materials(staged_root, catalog, paths, ctx, safe_path, require,
                               existing_links_checked=True)
    return check


def operation_spec(root, plan, writes, ctx, safe_path, require):
    """Спецификация file_operations для готового (plan, writes) и запуск."""
    actions = plan["request"]["actions"]
    kinds = {action["document"].split(":", 1)[0]
             for action in actions if "document" in action}
    has_tasks = any(action["action"] in TASK_ACTIONS for action in actions)
    data, paths = ctx["config"](root)
    cols = data.get("documents", {}).get("collections", {})
    touched_kinds = {kind for kind, col in cols.items()
                     if col.get("index") in plan["writes"]}
    kinds = kinds | touched_kinds
    checks = [_consistency(ctx, kinds, has_tasks, "staged", safe_path,
                           require),
              _consistency(ctx, kinds, has_tasks, "final", safe_path,
                           require)]
    spec = {"operation": "lifecycle",
            "plan_id": plan["plan_id"],
            "actor": _actor_label(plan["request"]["actor"]),
            "basis": plan["request"]["basis"],
            "writes": writes, "removes": plan["removes"],
            "remove_dirs": plan["remove_dirs"],
            "create_dirs": plan["create_dirs"],
            "inputs": plan["inputs"], "read_set": plan.get("read_set"),
            "staged_checks": [checks[0]], "final_checks": [checks[1]],
            "moves": plan["moves"],
            "receipt": {"actions": plan["summary"],
                        "plan": f"{PLANS_DIR}/{plan['plan_id']}.json"}}
    return spec


def run_plan(root, plan, writes, ctx, safe_path, require):
    return ctx['run_operation'](root, operation_spec(root, plan, writes, ctx, safe_path, require))


def preview_plan(root, plan, writes, ctx, safe_path, require):
    file_operations.preview_operation(root, operation_spec(root, plan, writes, ctx, safe_path, require), safe_path, require)


def apply_command(root, plan_rel, ctx, safe_path, require):
    """Применить сохранённый план: пересборка, сверка и запуск engine."""
    plan = _load_plan(root, plan_rel, safe_path, require)
    # Повторное применение уже завершённого плана — короткий ответ до
    # пересборки: документ давно в архиве, и повторный plan был бы отказом.
    # Решение принимается под удерживаемым командным gate и специализирован-
    # ным lock операции — как find_completed внутри run_operation.
    lock = file_operations._operation_lock(root, safe_path)
    if lock is not None:
        try:
            prior = file_operations.find_completed(root, plan["plan_id"]) \
                if file_operations.pending_info(root) is None else None
        finally:
            lock.close()
        if prior is not None:
            print(f"План {plan['plan_id']} уже применён операцией {prior} — "
                  "повторное применение пропущено.")
            return {"status": "already_completed",
                    "plan_id": plan["plan_id"], "op_id": prior}
    rebuilt, writes = build_plan(root, plan["request"], plan["created_at"],
                                 ctx, safe_path, require)
    stale = [field for field in ("plan_id", "inputs", "writes", "removes",
                                 "remove_dirs", "create_dirs", "moves", "read_set")
             if rebuilt.get(field) != plan.get(field)]
    require(not stale,
            f"План устарел или подменён ({', '.join(stale)}): входы "
            "изменились — постройте план заново командой lifecycle plan")
    result = run_plan(root, rebuilt, writes, ctx, safe_path, require)
    if result["status"] == "already_completed":
        print(f"План {plan['plan_id']} уже применён операцией "
              f"{result['op_id']} — повторное применение пропущено.")
    else:
        print(f"План {plan['plan_id']} применён. "
              f"Операция: {result['op_dir']}")
    return result


def status(root, safe_path, require):
    pending = file_operations.pending_info(root)
    if pending is not None:
        print(f"Незавершённая операция документов: {pending.get('op_id')} "
              f"({pending.get('operation')}, {pending.get('started_at')})")
        op_dir = pending.get("op_dir")
        probe = file_operations._lock_stream(root, safe_path,
                                             file_operations.LOCK_FILE)
        if probe is None:
            print("Состояние: выполняется другим процессом — восстановление "
                  "запрещено до завершения.")
        else:
            probe.close()
            print(f"Состояние: прервана. Восстановление: "
                  f"python3 {op_dir}/restore.py либо playbook.py "
                  "lifecycle restore")
    else:
        print("Незавершённых операций документов нет.")
    directory = safe_path(root, PLANS_DIR)
    saved = sorted(directory.glob("*.json")) if directory.is_dir() else []
    if saved:
        print(f"Сохранённые планы ({len(saved)}):")
        for path in saved:
            try:
                plan = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                plan = {}
            count = len((plan.get("request") or {}).get("actions") or [])
            print(f"  {path.name}: {plan.get('created_at', '?')}, "
                  f"действий: {count}")
    else:
        print("Сохранённых планов нет.")


def restore(root, safe_path, require):
    result = file_operations.restore_operation(root, safe_path, require)
    if result["status"] == "aborted_before_write":
        print(f"Операция прервана до записи: изменений не было, маркер снят. "
              f"Каталог: {result['op_dir']}")
    else:
        conflicts = (f" Конфликтующее содержимое: "
                     f"{result['op_dir']}/conflicts/ "
                     f"({', '.join(result['conflicts'])})"
                     if result["conflicts"] else "")
        print(f"Восстановлено из резервной копии: {result['restored']} файлов, "
              f"удалено {result['removed']}.{conflicts} "
              f"Каталог: {result['op_dir']}")
    return result


def _managed_card(root, records, task_id, safe_path, require):
    """(rec, rel, text, meta) активной карточки с блоком метаданных."""
    rec = records.get(task_id)
    require(rec is not None, f"Нет задачи: {task_id}")
    require(not rec["archived"],
            f"{task_id}: карточка в архиве — записи передачи/приёмки "
            "добавляются до закрытия")
    rel = _rel(root, rec["path"])
    text, meta = _task_meta(rec["path"], rel, require)
    require(meta is not None,
            f"{task_id}: карточка без метаданных — handoff/accept требуют "
            "раздел documents в project.json и блок метаданных карточки")
    return rec, rel, text, meta


def _verify_runs(root, task_id, safe_path):
    """(каталог прогона, report.json) для всех запусков verify задачи."""
    base = safe_path(root, f"work/verification/{task_id}")
    if not base.is_dir():
        return []
    return [(child, child / "report.json")
            for child in sorted(base.iterdir())
            if child.is_dir() and not child.is_symlink()]


def _write_card_meta(root, rel, text, meta, label, actor, basis, ctx,
                     safe_path, require, extra_writes=None, read_inputs=()):
    """Splice + валидация + запись карточки одной операцией engine."""
    errors = []
    data, paths = ctx['config'](root)
    tw = data.get('task_workflow')
    documents._validate_task_meta(meta, rel, errors,
        registry={r['id']: r for r in tw['registry']} if tw else None,
        task_workflow_enabled=tw is not None, root=root, safe_path=safe_path,
        block_enabled=block_workflow.CAPABILITY in documents.capabilities_of(data))
    require(not errors, "\n".join(errors))
    merged = _splice_meta(text, meta)
    require(merged is not None, f"{rel}: блок метаданных не найден")
    writes = documents.split_card_writes({rel: merged.encode("utf-8")})
    writes.update(extra_writes or {})

    def check(staged_root):
        staged = staged_root / rel
        staged_errors = []
        parsed = documents.parse_meta(
            documents.read_text(staged), rel, staged_errors)
        require(documents.read_text(staged) == merged,
                f"{label}: записанный текст не совпадает с подготовленным")
        require(not staged_errors and parsed == meta,
                f"{label}: карточка изменилась после подготовки записи")

    if label in {"sync-criteria", "handoff", "accept"} and task_cards.version(text) == 2:
        def check(staged_root, base_check=check):
            base_check(staged_root)
            _, mismatches = criterion_view(staged_root, Path(rel).stem, rel, merged, meta, safe_path, require)
            require(not mismatches, f"{label}: чекбоксы не соответствуют доказательствам {mismatches}")
    if meta.get('schema_version') == 2:
        index_rel = _rel(root, paths['task_index'])
        records = ctx['read_tasks'](root, paths)
        header = task_cards.HEADER.match(merged)
        records[Path(rel).stem] = dict(records[Path(rel).stem],
            status=header.group(3), executor=header.group(6), model=header.group(7),
            route=meta['workflow']['route'])
        writes[index_rel] = ctx['render_task_index'](records, paths['task_index'].parent).encode('utf-8')
        def check(staged_root, base_check=check):
            base_check(staged_root)
            _consistency(ctx, set(), True, label, safe_path, require)(staged_root)
    inputs = file_operations.snapshot_paths(root, sorted(set(writes) | set(read_inputs)), safe_path, require)
    plan_id = "lc-" + file_operations.sha256(_canonical(
        {"op": label, "path": rel,
         "writes": {key: file_operations.sha256(value) for key, value in sorted(writes.items())},
         "inputs": inputs}).encode("utf-8"))[7:23]
    spec = {"operation": f"lifecycle {label}", "plan_id": plan_id,
            "actor": actor, "basis": basis, "writes": writes,
            "inputs": inputs,
            "read_set": root.view.read_set.snapshot() if hasattr(root, 'view') else None,
            "staged_checks": [check],
            "final_checks": [check]}
    result = ctx["run_operation"](root, spec)
    return result


def prepare_audit_command(root, task_id, audit_id, snapshot_rel, apply, actor,
                          ctx, safe_path, require):
    """Create a portable audit container and register it in one recoverable write."""
    _, paths = ctx['config'](root)
    records = ctx['read_tasks'](root, paths)
    _, rel, text, meta = _managed_card(root, records, task_id, safe_path, require)
    require(file_operations.ID_RE.fullmatch(audit_id) is not None, 'Некорректный ID аудита')
    source = safe_path(root, snapshot_rel or rel)
    require(source.is_file(), 'Snapshot должен быть обычным файлом')
    folder = f'work/verification/{task_id}/{audit_id}'
    destination = safe_path(root, folder)
    source_paths = {'snapshot.txt': source}
    if source.suffix == '.md' and documents.external_card(source.read_text(encoding='utf-8')):
        sidecar = documents.meta_path(source)
        safe_path(root, _rel(root, sidecar))
        source_paths['snapshot.meta.json'] = sidecar
    contents = {name: path.read_bytes() for name, path in source_paths.items()}
    provenance = {'schema_version': 1, 'task_id': task_id, 'audit_id': audit_id,
                  'at': file_operations.now_iso(), 'actor': actor,
                  'files': {name: {'source': _rel(root, path),
                                   'sha256': file_operations.sha256(contents[name])}
                            for name, path in source_paths.items()}}
    material = {'id': audit_id, 'role': 'report', 'source': folder,
                'storage': 'local', 'immutable': True, 'required': True,
                'used_by': [task_id]}
    existing = next((m for m in meta['materials'] if m['id'] == audit_id), None)
    if existing is not None:
        require(existing == material and destination.is_dir(), 'ID аудита уже занят')
        provenance_path = safe_path(root, f'{folder}/provenance.json')
        digest_path = safe_path(root, f'{folder}/provenance.sha256')
        saved_bytes = provenance_path.read_bytes()
        require(meta.get('audit_packages', {}).get(audit_id) == file_operations.sha256(saved_bytes)
                and digest_path.read_text(encoding='ascii').strip() == file_operations.sha256(saved_bytes),
                'provenance аудита повреждён; исходные сведения не перезаписываются')
        saved = json.loads(saved_bytes)
        require(isinstance(saved, dict) and set(saved) == set(provenance)
                and all(saved.get(k) == provenance[k] for k in ('schema_version', 'task_id', 'audit_id', 'actor'))
                and isinstance(saved.get('at'), str) and documents._iso(saved['at']),
                'provenance аудита не соответствует владельцу, автору или формату')
        require(isinstance(saved.get('files'), dict) and set(saved['files']) == set(contents),
                'Состав снимка аудита изменился')
        for name, record in saved['files'].items():
            target = safe_path(root, f'{folder}/{name}')
            require(isinstance(record, dict) and set(record) == {'source', 'sha256'}
                    and record.get('source') == provenance['files'][name]['source']
                    and target.is_file()
                    and file_operations.sha256(target.read_bytes()) == record.get('sha256'),
                    'Снимок аудита изменился; используйте новый ID')
        if _rel(root, source) == rel:
            raw = (destination / 'snapshot.txt').read_text(encoding='utf-8')
            if documents.external_card(raw):
                raw = documents.hydrate_card(raw, json.loads((destination / 'snapshot.meta.json').read_text()))
            require(card_projection(raw) == card_projection(text), 'Содержимое карточки изменилось; используйте новый ID')
        else:
            require(all((destination / name).read_bytes() == content for name, content in contents.items()),
                    'Исходный snapshot изменился; используйте новый ID')
        print(f'{task_id}: аудит {audit_id} уже подготовлен; байты не менялись')
        return
    require(not destination.exists(), f'Каталог аудита занят: {folder}')
    for m in meta['materials']:
        if m.get('storage') == 'local':
            other = safe_path(root, m['source'])
            require(not (destination == other or destination.is_relative_to(other)
                         or other.is_relative_to(destination)), 'Пересечение материалов аудита')
    updated = {**meta, 'materials': meta['materials'] + [material]}
    projected = meta['materials'] + [dict(material, resolved=destination)]
    _check_destinations(root, task_id, projected, paths, safe_path, require)
    writes = {f'{folder}/{name}': content for name, content in contents.items()}
    writes[f'{folder}/provenance.json'] = (json.dumps(provenance, ensure_ascii=False, indent=2) + '\n').encode()
    writes[f'{folder}/provenance.sha256'] = (file_operations.sha256(writes[f'{folder}/provenance.json']) + '\n').encode('ascii')
    updated['audit_packages'] = {**meta.get('audit_packages', {}),
                                 audit_id: file_operations.sha256(writes[f'{folder}/provenance.json'])}
    wrapped = dict(ctx)
    def run(operation_root, spec):
        def check(staged):
            _task_material_catalog(staged, task_id, updated, ctx, safe_path, require)
        spec = dict(spec)
        spec['staged_checks'] = list(spec.get('staged_checks', [])) + [check]
        spec['final_checks'] = list(spec.get('final_checks', [])) + [check]
        if apply:
            return ctx['run_operation'](operation_root, spec)
        return file_operations.preview_operation(operation_root, spec, safe_path, require)
    wrapped['run_operation'] = run
    _write_card_meta(root, rel, text, updated, 'prepare-audit', actor,
                     'Подготовка переносимых материалов аудита', wrapped, safe_path,
                     require, extra_writes=writes,
                     read_inputs=[folder] + [_rel(root, p) for p in source_paths.values()])
    print(f"{'Подготовлен' if apply else 'Предпросмотр, без записи'}: {folder}; snapshot.txt/provenance.json")


def portable_materials(root, catalog, paths, ctx, safe_path, require, existing_links_checked=False):
    """Validate references against the exact exclusions of physical staging."""
    moves = catalog['moves'] if catalog else {}
    frozen = catalog['immutable'] if catalog else []
    files = set(ctx['scan_files'](root, paths))
    checked_files = set(files) if existing_links_checked else set()
    for owner, materials in (catalog['owners'].items() if catalog else []):
        for material in materials:
            if material.get('storage') != 'local':
                continue
            source = material['resolved']
            digest = (catalog['task_meta'].get(owner, {}).get('audit_packages', {}).get(material['id']))
            if digest is not None:
                provenance_path = safe_path(root, _rel(root, source / 'provenance.json'))
                require(file_operations.sha256(provenance_path.read_bytes()) == digest,
                        f'{owner}: provenance аудита {material["id"]} повреждён')
                package = json.loads(provenance_path.read_bytes())
                require(isinstance(package, dict) and isinstance(package.get('files'), dict),
                        f'{owner}: неверный provenance аудита')
                for name, info in package['files'].items():
                    require(name in {'snapshot.txt', 'snapshot.meta.json'} and isinstance(info, dict),
                            f'{owner}: неверный состав audit snapshot')
                    snapshot = safe_path(root, _rel(root, source / name))
                    require(file_operations.sha256(snapshot.read_bytes()) == info.get('sha256'),
                            f'{owner}: audit snapshot {name} повреждён')
            members = documents._walk_files(source)[0] if source.is_dir() else [source]
            for path in [source] + members:
                rel = _rel(root, path)
                if path != source and not file_operations.staged_path_available(rel):
                    # Incidental caches inside a registered directory are not
                    # separately required evidence. Explicit claims/references
                    # to excluded files are still rejected below.
                    continue
                require(file_operations.staged_path_available(rel),
                        f"{owner}: материал {rel} исключён из staged; перенесите доказательства "
                        "в поддерживаемый каталог; служебные файлы не регистрируйте")
                if path.suffix == '.md' and path.is_file():
                    files.add(path)
    for path in sorted(files):
        if not path.is_file():
            continue
        rel = _rel(root, path)
        safe_path(root, rel)
        origin = (document_links.origin_path(path.resolve(), moves) or path.resolve()) \
            if any(path.resolve() == p or p in path.resolve().parents for p in frozen) else path.resolve()
        content = path.read_text(encoding='utf-8')
        if path not in checked_files:
            errors = document_links.check_text(content, rel, origin.parent, root.resolve(), moves)
            require(not errors, '\n'.join(errors))
        for link in document_links.scan(content):
            if link['kind'] not in {'inline', 'image', 'refdef'}:
                continue
            parts = document_links._split(link['target'])
            if parts is None:
                continue
            target = (origin.parent / parts[1]).resolve()
            for _ in range(len(moves)):
                if target.exists():
                    break
                mapped = document_links.map_path(target, moves)
                if mapped == target:
                    break
                target = mapped
            if target.is_relative_to(root.resolve()):
                target_rel = target.relative_to(root.resolve()).as_posix()
                require(file_operations.staged_path_available(target_rel),
                        f"{rel}: ссылка {link['target']} ведёт в исключённый из staged путь "
                        f"{target_rel}; сохраните материал в owner-evidence или work/verification/<ID>/")


def audit_preflight(root, task_id, ctx, safe_path, require):
    """Check a handed-off result before spending time on independent review."""
    data, paths = ctx['config'](root)
    records = ctx['read_tasks'](root, paths)
    rec, rel, text, meta = _managed_card(root, records, task_id, safe_path, require)
    require(rec['status'] == 'in_review', f'{task_id}: аудит требует in_review')
    catalog = documents.load_catalog(root, data, paths, records, safe_path, require)
    meta = _task_meta_entry(catalog, task_id, meta, require)
    task_cards.receipt(text, meta)
    handoff = _handoff_evidence(root, task_id, rel, text, meta, safe_path, require)
    require(not data.get('verification') or handoff.get('reports'),
            f'{task_id}: передача не содержит отчётов verify')
    if task_cards.version(text) == 2:
        states, mismatches = criterion_view(root, task_id, rel, text, meta, safe_path, require)
        require(not mismatches and all(s['checked'] for s in states.values()),
                f'{task_id}: критерии не подтверждены свежими проверками; обновите CHECK/handoff')


def verification_preflight(root, task_id, ctx, safe_path, require):
    data, paths = ctx['config'](root)
    records = ctx['read_tasks'](root, paths)
    catalog = documents.load_catalog(root, data, paths, records, safe_path, require)
    if catalog is None:
        return
    _consistency(ctx, set(catalog['collections']), True, 'preflight', safe_path, require)(root)
    candidates = []
    for item in catalog['candidates']:
        if item.get('owner') != task_id:
            continue
        path = safe_path(root, item['path'])
        # Runner directories (including failed historical runs) are accounted for
        # automatically by handoff. Arbitrary files still need an explicit owner.
        report = path / 'report.json' if path.is_dir() else None
        if report is not None and report.is_file():
            safe_path(root, _rel(root, report))
            if verification.runner_directory(path):
                continue  # A damaged historical runner report must not prevent its replacement.
            try:
                value = verification.load_report(report, require)
                if value.get('task_id') == task_id and value.get('run_id') == path.name:
                    continue
            except (ValueError, OSError):
                pass
        candidates.append(item)
    require(not candidates, f"{task_id}: неучтённые материалы до verify: "
            + ', '.join(item['path'] for item in candidates)
            + '; выполните lifecycle register-material')
    _check_destinations(root, task_id, catalog['owners'].get(task_id, []),
                        paths, safe_path, require)


def _task_material_catalog(root, task_id, meta, ctx, safe_path, require):
    """Validate projected metadata with the normal catalog, without writing."""
    data, paths = ctx["config"](root)
    records = ctx["read_tasks"](root, paths)
    catalog = documents.load_catalog(root, data, paths, records,
                                     safe_path, require,
                                     task_overrides={task_id: meta})
    require(catalog is not None, "Раздел documents не настроен")
    protected = _protected_files(paths, records, catalog)
    evidence_dirs = {
        (paths["tasks"] / f"{task_id}-evidence").resolve(),
        (paths["archive"] / f"{task_id}-evidence").resolve(),
    }
    local = []
    for material in catalog["owners"].get(task_id, []):
        if material.get("storage") != "local":
            continue
        src = material["resolved"]
        require(src.exists() and (src.is_file() or src.is_dir()),
                f"{task_id}: материал отсутствует или не обычный файл/каталог: {material['source']}")
        _movable_material(_rel(root, src), material, task_id, require)
        require(not any(src == path or path.is_relative_to(src)
                        for path in protected),
                f"{task_id}: материал накрывает защищённый файл: {material['source']}")
        for other in local:
            require(src != other,
                    f"{task_id}: повтор пути материала: {material['source']}")
            if src.is_relative_to(other) or other.is_relative_to(src):
                require(other in evidence_dirs or src in evidence_dirs,
                        f"{task_id}: пересечение материалов: {material['source']}")
        local.append(src)
    _check_destinations(root, task_id, catalog["owners"].get(task_id, []),
                        paths, safe_path, require)
    portable_materials(root, catalog, paths, ctx, safe_path, require)
    return catalog


def register_material_command(root, task_id, source, material_id, role,
                              immutable, required, apply, actor, ctx,
                              safe_path, require):
    """Explicit local task material registration; preview is read-only."""
    _, paths = ctx["config"](root)
    records = ctx["read_tasks"](root, paths)
    _, rel, text, meta = _managed_card(root, records, task_id,
                                      safe_path, require)
    src = safe_path(root, source)
    require(src.exists() and (src.is_file() or src.is_dir()),
            f"Материал отсутствует или не обычный файл/каталог: {source}")
    resolved_src = src.resolve()
    for m in meta.get("materials") or []:
        if m.get("storage") != "local" or m.get("id") == material_id:
            continue
        other_path = safe_path(root, m["source"]).resolve()
        require(resolved_src != other_path and not resolved_src.is_relative_to(other_path)
                and not other_path.is_relative_to(resolved_src),
                f"{task_id}: пересечение материалов: {source}")
    material = {"id": material_id, "role": role,
                "source": _rel(root, src), "storage": "local",
                "required": required, "immutable": immutable,
                "used_by": [task_id]}
    materials = list(meta.get("materials") or [])
    existing = next((m for m in materials if m.get("id") == material_id), None)
    require(existing is None or existing == material,
            f"{task_id}: id материала '{material_id}' уже занят; перезапись запрещена")
    if existing is None:
        materials.append(material)
    meta = {**meta, "materials": materials}
    _task_material_catalog(root, task_id, meta, ctx, safe_path, require)
    if existing is not None:
        print(f"{task_id}: материал '{material_id}' уже зарегистрирован.")
        return
    if not apply:
        print(f"Предпросмотр: {task_id} → {json.dumps(material, ensure_ascii=False)}; запись только с --apply")
        return
    checked_ctx = dict(ctx)
    original = ctx["run_operation"]

    def run_checked(operation_root, spec):
        def check_materials(staged_root):
            _task_material_catalog(staged_root, task_id, meta, ctx,
                                   safe_path, require)
        spec = dict(spec)
        for key in ("staged_checks", "final_checks"):
            spec[key] = list(spec.get(key, [])) + [check_materials]
        return original(operation_root, spec)

    checked_ctx["run_operation"] = run_checked
    _write_card_meta(root, rel, text, meta, "register-material", actor,
                     "Явная регистрация материала", checked_ctx,
                     safe_path, require)
    physical = file_operations.scoped_operations.physical_root(root)
    _, saved = _task_meta(safe_path(physical, rel), rel, require)
    require(saved == meta,
            f"{task_id}: операция не записала регистрацию; проверьте lifecycle status")
    print(f"{task_id}: материал '{material_id}' зарегистрирован.")


def relocate_material_command(root, task_id, material_id, target, reason,
                              apply, actor_name, actor_model, ctx,
                              safe_path, require):
    """Явный перенос одного зарегистрированного локального файла (PB-072).

    Preview по умолчанию; --apply переносит файл в свободный путь внутри
    owner-evidence той же восстанавливаемой операцией, что прочие записи
    карточки. Байты, material ID, флаги и исторические факты сохраняются;
    обновляются source, живая цель ссылок и служебная карта moves.
    Старые journal/check/audit отпечатки не пересчитываются.
    """
    data, paths = ctx['config'](root)
    records = ctx['read_tasks'](root, paths)
    rec, rel, text, meta = _managed_card(root, records, task_id, safe_path,
                                         require)
    require(rec['status'] not in FINAL_TASK_STATES,
            f"{task_id}: карточка в статусе {rec['status']}")
    require(actor_name and actor_name != 'cli' and actor_model
            and ctx['model_unconfirmed'] not in actor_model,
            'relocate-material требует точные --actor и --model')
    require(reason and reason.strip(), 'Требуется непустая причина (--reason)')
    catalog = documents.load_catalog(root, data, paths, records, safe_path,
                                     require)
    require(catalog is not None, 'Раздел documents не настроен')
    material = next((item for item in meta.get('materials') or []
                     if item.get('id') == material_id), None)
    require(material is not None,
            f"{task_id}: нет материала '{material_id}'")
    require(material.get('storage') == 'local',
            f"{task_id}: материал '{material_id}' — {material.get('storage')}; "
            "переносится только локальный файл")
    require(set(material.get('used_by') or []) <= {task_id},
            f"{task_id}: материал '{material_id}' используется "
            f"{sorted(set(material.get('used_by') or []) - {task_id})}; "
            "общее и чужое владение не переносится")
    dst = safe_path(root, target)
    dst_rel = _rel(root, dst)
    evidence = paths['tasks'] / f'{task_id}-evidence'
    require(dst.resolve() != evidence.resolve()
            and dst.resolve().is_relative_to(evidence.resolve()),
            f'Назначение обязано быть файлом внутри '
            f'{_rel(root, evidence)}/: {dst_rel}')
    src_rel = material['source']
    if src_rel == dst_rel:
        require(dst.is_file() and not dst.is_symlink(),
                f"{task_id}: материал '{material_id}' уже указывает на "
                f"{dst_rel}, но файл отсутствует")
        print(f"{task_id}: материал '{material_id}' уже размещён в "
              f"{dst_rel}; повторный перенос не создаёт копию.")
        return
    src = safe_path(root, src_rel)
    require(src.is_file() and not src.is_symlink(),
            f"{task_id}: материал '{material_id}' ({src_rel}) — переносится "
            "один существующий обычный файл, не каталог и не symlink")
    require(not dst.exists() and not dst.is_symlink(),
            f"Назначение занято: {dst_rel}")
    if dst.parent.is_dir():
        folded = unicodedata.normalize('NFC', dst.name).casefold()
        for child in dst.parent.iterdir():
            require(unicodedata.normalize('NFC', child.name).casefold()
                    != folded, f"Коллизия имени: {_rel(root, child)}")
    require(file_operations.staged_path_available(dst_rel),
            f'{dst_rel}: путь исключён из staged; выберите другое '
            'назначение внутри owner-evidence')
    protected = _protected_files(paths, records, catalog)
    require(not any(dst.resolve() == path or dst.resolve().is_relative_to(path)
                    for path in protected),
            f'{task_id}: назначение накрывает служебный файл: {dst_rel}')
    for owner, mats in catalog['owners'].items():
        for other in mats:
            if other.get('storage') != 'local' \
                    or other.get('id') == material_id:
                continue
            other_src = other.get('resolved')
            if other_src is None:
                continue
            if not (dst.resolve() == other_src.resolve()
                    or dst.resolve().is_relative_to(other_src.resolve())):
                continue
            # Законное повторное представление каталога: назначение
            # внутри собственного материала-корня owner-evidence. Чужое
            # и общее владение по-прежнему отклоняется.
            require(owner == task_id
                    and set(other.get('used_by') or []) <= {task_id}
                    and other_src.resolve() == evidence.resolve(),
                    f'{task_id}: назначение пересекается с материалом '
                    f"'{other.get('id')}' владельца {owner}: "
                    f"{other.get('source')}")
    src_resolved = src.resolve()
    moves = {src_resolved: dst.resolve()}
    # Ссылки на переносимое внутри чужих immutable-материалов — отказ:
    # их исторические адреса остаются чужими фактами. Собственные frozen
    # байты сохраняются и разрешаются через meta.moves.
    foreign_hits = []
    for owner, mats in catalog['owners'].items():
        if owner == task_id:
            continue
        for item in mats:
            if item.get('storage') != 'local' or not item.get('immutable') \
                    or item.get('resolved') is None:
                continue
            base = item['resolved'].resolve()
            files = (documents._walk_files(base)[0] if base.is_dir()
                     else [base])
            for path in files:
                if path.suffix != '.md' or not path.is_file():
                    continue
                for link in document_links.scan(
                        path.read_text(encoding='utf-8')):
                    parts = document_links._split(link['target'])
                    if parts is None:
                        continue
                    if (path.parent / parts[1]).resolve() == src_resolved:
                        foreign_hits.append(
                            f'{_rel(root, path)}:{link["line"]}')
    require(not foreign_hits,
            f"{task_id}: ссылки на '{material_id}' внутри чужих "
            f"immutable-материалов ({', '.join(sorted(foreign_hits))}) — "
            "требуется ручной разбор, перенос отклонён")
    if src.suffix == '.md' and not material.get('immutable'):
        own = document_links.rewrite(src.read_text(encoding='utf-8'),
                                     src_resolved, dst.resolve(), moves)
        require(not own['changes'],
                f"{task_id}: материал '{material_id}' содержит относительные "
                "ссылки; перенос изменил бы его байты — выберите файл без "
                "ссылок либо разберите их вручную")
    frozen = [item for item in catalog['immutable']]
    scan = [item for item in ctx['scan_files'](root, paths)
            if item.is_file()
            and item.resolve() != paths['task_index'].resolve()]
    managed = [safe_path(root, ctx['core_prefix']).resolve()]
    result = document_links.plan_rewrites(scan, moves, root.resolve(),
                                          managed=managed, frozen=frozen)
    require(not result['conflicts'],
            'Ссылки требуют ручного разбора:\n' + '\n'.join(
                f"  {item['path']}:{item['line']} {item['kind']} "
                f"{item['detail']}" for item in result['conflicts']))
    updates = dict(result['updates'])
    card_path = safe_path(root, rel).resolve()
    card_update = next((info for path, info in updates.items()
                        if path.resolve() == card_path), None)
    updates = {path: info for path, info in updates.items()
               if path.resolve() != card_path}
    at = file_operations.now_iso()
    digest = file_operations.scoped_operations.file_digest(src)
    # Первый проход входов определяет plan_id; companion получает его имя.
    removes = [src_rel]
    remove_dirs = _emptied_dirs(root, _rel(root, evidence),
                                set(removes), require)
    probe = sorted({rel, src_rel, dst_rel}
                   | {_rel(root, documents.meta_path(safe_path(root, rel)))}
                   | set(removes) | set(remove_dirs)
                   | {_rel(root, path) for path in scan})
    inputs = file_operations.snapshot_paths(root, probe, safe_path, require)
    plan_id = 'lc-' + file_operations.sha256(_canonical(
        {'op': 'relocate-material', 'path': rel, 'material': material_id,
         'from': src_rel, 'to': dst_rel,
         'inputs': inputs}).encode('utf-8'))[7:23]
    companion_rel = f'{_rel(root, evidence)}/relocate-{plan_id}.json'
    require(not safe_path(root, companion_rel).exists()
            and not safe_path(root, companion_rel).is_symlink(),
            f'Назначение занято: {companion_rel}')
    inputs[companion_rel] = 'missing'
    companion = {'id': f'relocate-{plan_id}', 'role': 'report',
                 'source': companion_rel, 'storage': 'local',
                 'required': True, 'immutable': True, 'used_by': []}
    new_materials = [dict(item, source=dst_rel)
                     if item.get('id') == material_id else item
                     for item in meta.get('materials') or []]
    new_materials.append(companion)
    new_meta = copy.deepcopy(meta)
    new_meta['materials'] = new_materials
    new_meta['moves'] = list(meta.get('moves') or []) + [
        {'from': src_rel, 'to': dst_rel}]
    if new_meta.get('workflow', {}).get('baseline', {}).get('source') == src_rel:
        new_meta['workflow']['baseline']['source'] = dst_rel
    errors = []
    _validate_moved_task_meta(root, new_meta, rel, errors, safe_path)
    require(not errors, '\n'.join(errors))
    merged = _splice_meta(text, new_meta)
    require(merged is not None, f'{rel}: блок метаданных не найден')
    merged = document_links.rewrite(merged, card_path, card_path,
                                    moves)['text']
    link_changes = []
    if card_update is not None:
        link_changes.append({'path': rel,
                             'changes': card_update['changes']})
    link_changes += [{'path': _rel(root, path), 'changes': info['changes']}
                     for path, info in sorted(updates.items(),
                                              key=lambda kv: str(kv[0]))]
    body = {'schema_version': 1, 'type': 'lifecycle-relocate-material',
            'plan_id': plan_id, 'task': task_id, 'material': material_id,
            'at': at,
            'actor': {'name': actor_name, 'model': actor_model},
            'reason': reason,
            'files': [{'from': src_rel, 'to': dst_rel, 'sha256': digest,
                       'bytes': src.stat().st_size}],
            'links': link_changes,
            'receipts': {
                'handoff': ({'at': meta['handoff'].get('at'),
                             'summary': meta['handoff'].get('summary')}
                            if isinstance(meta.get('handoff'), dict) else None),
                'acceptance': ({'at': meta['acceptance'].get('at'),
                                'result': meta['acceptance'].get('result'),
                                'handoff_at': meta['acceptance'].get('handoff_at')}
                               if isinstance(meta.get('acceptance'), dict)
                               else None)}}
    writes = documents.split_card_writes({rel: merged.encode('utf-8')})
    for path, info in updates.items():
        writes[_rel(root, path.resolve())] = info['updated'].encode('utf-8')
    writes[companion_rel] = (json.dumps(body, ensure_ascii=False, indent=2)
                             + '\n').encode('utf-8')
    writes[dst_rel] = file_operations.scoped_operations.Payload(src)
    for rel_path in writes:
        inputs.setdefault(rel_path, 'missing')
    # Проектируемая раскладка после переноса — унаследованная коллизия
    # исходного состояния не блокирует корректное исправление. Исходный
    # путь исключён вместе с опустевшими от переноса каталогами:
    # материалы-каталоги разворачиваются по итоговому дереву.
    projected = []
    for item in new_materials:
        projected_item = dict(item)
        if item.get('storage') == 'local':
            projected_item['resolved'] = dst.resolve() \
                if item.get('id') == material_id else safe_path(
                    root, item['source'])
        projected.append(projected_item)
    exclude = {src_resolved} | {safe_path(root, empty).resolve()
                                for empty in remove_dirs}
    _check_destinations(root, task_id, projected, paths, safe_path, require,
                        exclude=exclude)

    def check(staged_root):
        staged_card = documents.read_text(staged_root / rel)
        staged_errors = []
        parsed = documents.parse_meta(staged_card, rel, staged_errors)
        require(staged_card == merged,
                'relocate-material: записанный текст не совпадает '
                'с подготовленным')
        require(not staged_errors and parsed == new_meta,
                'relocate-material: карточка изменилась после подготовки '
                'записи')
        staged_dst = staged_root / dst_rel
        require(staged_dst.is_file()
                and file_operations.scoped_operations.file_digest(staged_dst)
                == digest,
                f'relocate-material: байты {dst_rel} не совпадают с исходными')
        require(not (staged_root / src_rel).exists(),
                f'relocate-material: источник {src_rel} остался на месте')
        require((staged_root / companion_rel).is_file(),
                'relocate-material: companion не записан')
        _task_material_catalog(staged_root, task_id, new_meta, ctx,
                               safe_path, require)
        _consistency(ctx, set(), True, 'relocate-material', safe_path,
                     require)(staged_root)

    spec = {'operation': 'lifecycle relocate-material', 'plan_id': plan_id,
            'actor': _actor_label({'name': actor_name, 'role': 'executor',
                                   'model': actor_model}),
            'basis': reason, 'writes': writes, 'removes': removes,
            'remove_dirs': remove_dirs, 'inputs': inputs,
            'read_set': root.view.read_set.snapshot()
            if hasattr(root, 'view') else None,
            'staged_checks': [check], 'final_checks': [check],
            'receipt': {'type': 'relocate-material', 'task': task_id,
                        'material': material_id,
                        'from': src_rel, 'to': dst_rel}}
    if not apply:
        file_operations.preview_operation(root, spec, safe_path, require)
        print(f'Предпросмотр {task_id}: материал '
              f"'{material_id}' {src_rel} → {dst_rel} (sha256 {digest}); "
              f'companion {companion_rel}; ссылок переписано: '
              f'{sum(len(item["changes"]) for item in link_changes)}; '
              'запись только с --apply')
        return
    ctx['run_operation'](root, spec)
    print(f"{task_id}: материал '{material_id}' перенесён "
          f"{src_rel} → {dst_rel}; доказательство {companion_rel}. "
          'Исторические hashes и приёмка не пересчитывались.')


def handoff_command(root, task_id, summary, actor_name, actor_model,
                    reports, ctx, safe_path, require):
    """Запись передачи на приёмку: прогоны verify → materials, handoff.

    Отчёты регистрируются как immutable-материалы; в handoff.reports
    попадают только успешные прогоны с неизменными входами. Свежесть
    входов повторно проверяет close_task — передача фиксирует состав
    доказательств, а не подменяет приёмку.
    """
    data, paths = ctx["config"](root)
    records = ctx["read_tasks"](root, paths)
    rec, rel, text, meta = _managed_card(root, records, task_id, safe_path,
                                         require)
    if meta.get('schema_version') == 2:
        return handoff_v2_command(root, task_id, summary, actor_name, actor_model,
                                  reports, ctx, safe_path, require)
    materials = [dict(item) for item in meta.get("materials") or []]
    material_ids = {item.get("id") for item in materials}
    candidates = []
    if reports:
        for rel_path in reports:
            run_dir = safe_path(root, rel_path)
            candidates.append(
                (run_dir if run_dir.is_dir() else run_dir.parent,
                 run_dir / "report.json" if run_dir.is_dir() else run_dir))
    else:
        # Registered audit logs may live under work/verification too. They
        # are materials, not runner reports; explicit --report remains strict.
        registered = [safe_path(root, m["source"])
                      for m in materials if m.get("storage") == "local"]
        audit_sources = [safe_path(root, m['source']) for m in materials
                         if m.get('storage') == 'local' and m.get('id') in meta.get('audit_packages', {})]
        candidates = [(run_dir, report) for run_dir, report
                      in _verify_runs(root, task_id, safe_path)
                      if run_dir not in audit_sources
                      and (report.is_file() or not any(
                          run_dir == src or run_dir.is_relative_to(src)
                          for src in registered))]
    report_refs = []
    for run_dir, report_path in candidates:
        require(report_path.is_file(),
                f"{task_id}: нет отчёта проверки "
                f"{_rel(root, report_path)}")
        safe_path(root, _rel(root, report_path))
        automatic = not reports and verification.runner_directory(run_dir)
        try:
            report = verification.load_report(report_path, require)
            if automatic and (report.get('task_id') != task_id or report.get('run_id') != run_dir.name):
                report = {}
        except (ValueError, OSError, TypeError, KeyError):
            if not automatic:
                raise
            report = {}
        require(not report or report.get("task_id") == task_id,
                f"{task_id}: отчёт {_rel(root, report_path)} принадлежит "
                f"задаче {report.get('task_id')}")
        run_id = run_dir.name if automatic else (report.get("run_id") or run_dir.name)
        material_id = f"verify-{run_id}"
        run_rel = _rel(root, run_dir)
        existing = next((item for item in materials
                         if item.get("id") == material_id), None)
        if existing is None:
            materials.append({"id": material_id, "role": "report",
                              "source": run_rel, "storage": "local",
                              "required": True, "immutable": True,
                              "used_by": [task_id]})
            material_ids.add(material_id)
        else:
            require(existing.get("source") == run_rel,
                    f"{task_id}: материал '{material_id}' указывает на "
                    f"{existing.get('source')}, ожидался {run_rel}")
        if report.get("status") == "passed" \
                and report.get("inputs_unchanged"):
            if task_cards.version(text) == 2:
                if report.get("card_projection") != card_projection(text):
                    continue
                target_exec_id, target_exec_sha = task_cards.execution_state(text, rel)
                if target_exec_id is not None:
                    if report.get("execution_id") != target_exec_id or report.get("execution_sha256") != target_exec_sha:
                        continue
                elif report.get("execution_id") is not None:
                    continue
            report_refs.append(material_id)
    projected_meta = {**meta, "materials": materials}
    catalog = _task_material_catalog(root, task_id, projected_meta, ctx,
                                     safe_path, require)
    stray = sorted(item["path"] for item in catalog["candidates"]
                   if item.get("owner") == task_id)
    require(not stray,
            f"{task_id}: неучтённые материалы {stray} — передача отменена. "
            "Зарегистрируйте каждый материал: lifecycle register-material "
            f"{task_id} <source> --id <id> --role report "
            "--immutable --required --apply; затем повторите verify и handoff")
    checks_verdict = _checks_gate(root, task_id, rel, text, meta,
                                  safe_path, require)
    require(report_refs or checks_verdict is not None,
            f"{task_id}: нет успешного прогона verify с неизменными "
            "входами — выполните playbook.py verify до передачи")
    binding = {}
    if task_cards.version(text) == 2:
        journal = task_cards.parse(text)
        last = journal["entries"][-1] if journal["entries"] else None
        require(last and last["type"] == "handoff", "Добавьте последнюю HANDOFF-запись перед CLI handoff")
        require(last["fields"]["Исполнитель"] == actor_name and last["fields"]["Модель"] == (actor_model or "—"),
                "Атрибуция handoff не совпадает с журналом")
        binding = {"entry_id": last["id"], "journal_sha256": last["journal_sha256"]}
        summary = last["id"]
    at = file_operations.now_iso()
    meta = dict(meta)
    meta["materials"] = materials
    if task_cards.version(text) == 2:
        text, meta = _prepare_criteria(root, task_id, rel, text, meta, safe_path, require)
    meta["handoff"] = {"at": at,
                       "actor": {"name": actor_name, "model": actor_model},
                       "summary": summary,
                       "reports": sorted(set(report_refs)),
                       "card_projection": card_projection(text), **binding}
    result = _write_card_meta(root, rel, text, meta, "handoff",
                              f"{actor_name} (executor)", "lifecycle handoff",
                              ctx, safe_path, require)
    if result["status"] == "already_completed":
        print(f"{task_id}: такая запись передачи уже сделана — "
              "повтор пропущен.")
    else:
        print(f"{task_id}: передача на приёмку записана; отчётов: "
              f"{len(report_refs)}. Независимая приёмка: lifecycle accept.")
    return result


def accept_command(root, task_id, result, kind, role, actor_name,
                   actor_model, note, ctx, safe_path, require):
    """Запись приёмки либо диагностического протокола аудита.

    accepted требует свежей записи передачи: проекция карточки обязана
    совпадать с переданной — иначе приёмка относилась бы к другому
    состоянию требований. diagnostic протокол не является приёмкой и
    не открывает close (кроме карточек аудита без зависимостей).
    """
    data, paths = ctx["config"](root)
    records = ctx["read_tasks"](root, paths)
    rec, rel, text, meta = _managed_card(root, records, task_id, safe_path,
                                         require)
    if meta.get('schema_version') == 2:
        root = _tracking_root(root)
        data, paths = ctx['config'](root)
        records = ctx['read_tasks'](root, paths)
        rec, rel, text, meta = _managed_card(root, records, task_id, safe_path, require)
        if result == 'accepted':
            _dependencies_done(task_id, records, require)
            require(rec['status'] == 'in_review', 'Приёмка требует in_review')
            _handoff_v2_evidence(root, task_id, rel, text, meta, safe_path, require)
            require(role == 'engineer' or meta['workflow']['route'] != 'quick',
                    'Быстрый маршрут требует приёмку инженером')
            require(kind == 'acceptance', 'V2 требует kind=acceptance')
            if role == 'engineer':
                require(note and note.strip() and actor_name != 'cli',
                        'Приёмка инженера требует автора и --note с явным основанием')
    handoff = meta.get("handoff")
    if result == "accepted":
        require(isinstance(handoff, dict),
                f"{task_id}: приёмка без записи передачи — сначала "
                "lifecycle handoff")
        require(card_projection(text) == handoff.get("card_projection"),
                f"{task_id}: карточка изменилась после передачи — "
                "приёмка относилась бы к другому состоянию требований; "
                "повторите handoff и аудит")
        if meta.get('schema_version') != 2:
            _checks_gate(root, task_id, rel, text, meta, safe_path,
                         require)
    binding = {}
    if result == "accepted" and task_cards.version(text) == 2:
        task_cards.receipt(text, meta)
        journal = task_cards.parse(text)
        audit = journal["entries"][-1]
        require(audit["type"] == "audit" and audit["fields"]["Результат"] == "accepted",
                "Нужен последний accepted-аудит журнала")
        require(audit["fields"]["Исполнитель"] == actor_name and
                audit["fields"]["Модель"] == (actor_model or "—"), "Атрибуция accept не совпадает с журналом")
        binding = {"audit_id": audit["id"], "audit_sha256": audit["sha256"]}
        note = None
    previous = meta.get('acceptance')
    if (meta.get('schema_version') == 2 and previous and previous.get('result') == result
            and previous.get('kind') == kind and previous.get('role') == role
            and previous.get('actor') == {'name': actor_name, 'model': actor_model}
            and previous.get('handoff_at') == (handoff or {}).get('at')
            and previous.get('detail') == note
            and all(previous.get(k) == v for k, v in binding.items())):
        print(f'{task_id}: приёмка уже записана.')
        return {'status': 'already_completed'}
    at = file_operations.now_iso()
    meta = dict(meta)
    meta["acceptance"] = {"at": at,
                          "actor": {"name": actor_name,
                                    "model": actor_model},
                          "role": role, "kind": kind, "result": result,
                          "handoff_at": handoff.get("at")
                          if isinstance(handoff, dict) else None,
                          "session": None, "detail": note, **binding}
    if meta.get('schema_version') == 2 and result == 'accepted':
        meta['acceptance']['result_hash'] = handoff['result_hash']
    if task_cards.version(text) == 2:
        text, meta = _prepare_criteria(root, task_id, rel, text, meta, safe_path, require)
    applied = _write_card_meta(root, rel, text, meta, "accept",
                               f"{actor_name} (auditor)", "lifecycle accept",
                               ctx, safe_path, require)
    if applied["status"] == "already_completed":
        print(f"{task_id}: такая запись приёмки уже сделана — "
              "повтор пропущен.")
    else:
        print(f"{task_id}: записана приёмка result={result} kind={kind}.")
    return applied


def criterion_view(root, task_id, rel, text, meta, safe_path, require,
                   historical=False):
    """Read-only criterion projection; malformed/missing evidence never confirms it."""
    parsed = task_cards.parse(text, rel)
    cache = {}
    materials = {m['id']: m for m in meta.get('materials', [])}

    def fresh(binding):
        key = (binding.get('report_id'), binding.get('report_sha256'))
        if key not in cache:
            try:
                material = materials.get(key[0], {})
                require(material.get('storage') == 'local', 'Нет локального отчёта')
                source = safe_path(root, material.get('source'))
                path = source / 'report.json' if source.is_dir() else source
                require(file_operations.sha256(path.read_bytes()) == key[1], 'Отчёт изменён')
                report = verification.load_report(path, require)
                if report.get('schema_version') == 2:
                    valid = _criterion_v2_report(root, task_id, rel, text, meta, report,
                                                 safe_path, require, historical)
                    cache[key] = valid
                    return cache[key]
                valid = (report.get('task_id') == task_id and report.get('status') == 'passed'
                         and report.get('inputs_unchanged') is True)
                if not historical and report.get('card_projection') is not None and report.get('card_projection') != card_projection(text):
                    valid = False
                if not historical:
                    target_exec_id, target_exec_sha = task_cards.execution_state(text, rel)
                    if target_exec_id is not None:
                        if report.get('execution_id') != target_exec_id or report.get('execution_sha256') != target_exec_sha:
                            valid = False
                    elif report.get('execution_id') is not None:
                        valid = False
                cache[key] = valid and (historical or verification.inputs_fresh(
                    root, report, {rel}, safe_path, require)[0])
            except (OSError, ValueError, TypeError):
                cache[key] = False
        return cache[key]

    return task_cards.criterion_states(parsed, meta, card_projection(text), fresh, historical, root=root)


def _prepare_criteria(root, task_id, rel, text, meta, safe_path, require, bind=True):
    """Bind new CHECK entries once, then render one canonical criterion view."""
    parsed = task_cards.parse(text, rel)
    require(parsed['status'] == 'supported', 'card sync поддерживает только формат 2')
    had_bindings = 'criterion_evidence' in meta
    meta = dict(meta)
    meta['materials'] = list(meta.get('materials', []))
    bindings = dict(meta.get('criterion_evidence', {}))
    meta['criterion_evidence'] = bindings
    if not any(v is not None for v in parsed['criterion_markers'].values()):
        legacy = [finding['id'] for entry in parsed['entries']
                  for finding in entry['findings'] if finding['criteria_inferred']]
        if legacy or 'checkbox_legacy_findings' in meta:
            meta['checkbox_legacy_findings'] = legacy
    entries = parsed['entries']
    last_exec = max((i for i, entry in enumerate(entries) if entry['type'] == 'execution'), default=-1)
    unbound = [entry for i, entry in enumerate(entries) if i > last_exec
               and entry['type'] == 'verification' and entry['fields']['Результат'] == 'passed'
               and entry['id'] not in bindings]
    if bind and unbound:
        candidates = {}
        audit_sources = {safe_path(root, m['source']) for m in meta['materials']
                         if m.get('storage') == 'local' and m.get('id') in meta.get('audit_packages', {})}
        for material in meta['materials']:
            if (material.get('storage') == 'local' and material['id'].startswith('verify-')
                    and material['id'] not in meta.get('audit_packages', {})):
                source = safe_path(root, material['source'])
                path = source / 'report.json' if source.is_dir() else source
                candidates[material['id']] = (source, path)
        for directory, report_path in _verify_runs(root, task_id, safe_path):
            if report_path.is_file() and directory not in audit_sources:
                candidates.setdefault('verify-' + directory.name, (directory, report_path))
        target_exec_id, target_exec_sha = task_cards.execution_state(text, rel)
        current_proj = card_projection(text)
        eligible = []
        for ident, (source, path) in candidates.items():
            try:
                report = verification.load_report(path, require)
                if report.get('schema_version') == 2:
                    if _criterion_v2_report(root, task_id, rel, text, meta, report, safe_path, require):
                        eligible.append((report['started_at'], ident, source, path))
                    continue
                if (report.get('task_id') == task_id and report.get('status') == 'passed'
                        and report.get('inputs_unchanged') is True
                        and verification.inputs_fresh(root, report, {rel}, safe_path, require)[0]):
                    if report.get('card_projection') != current_proj:
                        continue
                    if target_exec_id is not None:
                        if report.get('execution_id') != target_exec_id or report.get('execution_sha256') != target_exec_sha:
                            continue
                    elif report.get('execution_id') is not None:
                        continue
                    require(verification._iso_dt(report.get('started_at')) is not None, 'Нет времени прогона')
                    eligible.append((report['started_at'], ident, source, path))
            except (OSError, ValueError, TypeError, KeyError):
                continue
        if eligible:
            _, ident, source, path = max(eligible)
            if not any(m['id'] == ident for m in meta['materials']):
                meta['materials'].append({'id': ident, 'role': 'report', 'source': _rel(root, source),
                                         'storage': 'local', 'required': True, 'immutable': True,
                                         'used_by': [task_id]})
            for entry in unbound:
                bindings[entry['id']] = {'check_sha256': entry['sha256'],
                                         'card_projection': current_proj,
                                         'report_id': ident,
                                         'report_sha256': file_operations.sha256(path.read_bytes())}
    if not bindings and not had_bindings:
        meta.pop('criterion_evidence', None)
    states, _ = criterion_view(root, task_id, rel, text, meta, safe_path, require)
    return task_cards.render_checkboxes(text, states), meta


def sync_criteria_command(root, task_id, apply, ctx, safe_path, require, bind=True):
    _, paths = ctx['config'](root)
    records = ctx['read_tasks'](root, paths)
    _, rel, text, meta = _managed_card(root, records, task_id, safe_path, require)
    require(task_cards.version(text) == 2, 'card sync поддерживает только формат 2')
    updated, updated_meta = _prepare_criteria(root, task_id, rel, text, meta, safe_path, require, bind=bind)
    if updated == text and updated_meta == meta:
        print(f'{task_id}: чекбоксы уже актуальны.')
        return
    if not apply:
        states, _ = criterion_view(root, task_id, rel, updated, updated_meta, safe_path, require)
        print(f'Предпросмотр {task_id}: ' + ', '.join(
            f"{ident}=[{'x' if state['checked'] else ' '}]" for ident, state in states.items()))
        print('Запись только с --apply; история журнала не изменяется.')
        return
    _task_material_catalog(root, task_id, updated_meta, ctx, safe_path, require)
    _write_card_meta(root, rel, updated, updated_meta, 'sync-criteria',
                     'cli', 'Пересчёт чекбоксов по журналу', ctx, safe_path, require)
    physical = file_operations.scoped_operations.physical_root(root)
    require(documents.read_text(safe_path(physical, rel)) == _splice_meta(updated, updated_meta),
            'Пересчёт не записан; проверьте lifecycle status')
    print(f'{task_id}: чекбоксы синхронизированы.')


# Workflow v2 uses the same operation engine, gate and recovery as legacy cards.
def _tracking_root(root):
    if hasattr(root, 'view'):
        return root
    return file_operations.scoped_operations.project_view.ProjectView(root).path


def _v2_context(root, task_id, ctx, safe_path, require):
    root = _tracking_root(root)
    data, paths = ctx['config'](root)
    records = ctx['read_tasks'](root, paths)
    rec, rel, text, meta = _managed_card(root, records, task_id, safe_path, require)
    require(meta.get('schema_version') == 2 and data.get('task_workflow'),
            'Переход требует явных metadata/task_workflow v2; legacy не мигрируется')
    documents.load_catalog(root, data, paths, records, safe_path, require)
    return root, data, paths, records, rec, rel, text, meta


def _dependencies_done(task_id, records, require):
    require(all(records.get(dep, {}).get('status') == 'done' for dep in records[task_id]['deps']),
            f'{task_id}: зависимости не завершены')


def transition_command(root, task_id, action, actor, model, reason, to_route,
                       approval, ctx, safe_path, require):
    root, data, paths, records, rec, rel, text, meta = _v2_context(
        root, task_id, ctx, safe_path, require)
    require(actor and actor != 'cli' and model and ctx['model_unconfirmed'] not in model,
            'Переход требует точные --actor и --model')
    require(reason and reason.strip(), 'Требуется непустая причина')
    _dependencies_done(task_id, records, require)
    meta = copy.deepcopy(meta)
    wf = meta['workflow']
    writes = {}
    at = file_operations.now_iso()
    if action == 'start':
        if rec['status'] == 'in_progress' and wf.get('baseline'):
            verification_policy.baseline_files(root, meta)
            print(f'{task_id}: работа уже начата, baseline сохранён.')
            return {'status': 'already_completed'}
        require(rec['status'] in {'draft', 'ready'}, 'start требует draft/ready')
        require(wf.get('reason'), 'Маршрут требует reason')
        # Both supported Markdown formats must describe a real goal and AC.
        goal = re.search(r'^#{2,3} Цель и границы\s*\n(.*?)(?=^#{2,3} |\Z)', text, re.M | re.S)
        criteria = re.findall(r'^- \[ \] (.+)$', text, re.M)
        require(goal and goal.group(1).strip() and 'TODO' not in goal.group(1)
                and criteria and all(c.strip() and 'TODO' not in c for c in criteria),
                'Заполните цель и проверяемую приёмку до start')
        require(meta['verification_plan']['checks'], 'План проверок пуст')
        text = re.sub(r'^Executor:.*$', f'Executor: {actor}', text, flags=re.M)
        text = re.sub(r'^Model:.*$', f'Model: {model}', text, flags=re.M)
        source = _rel(root, paths['tasks'] / f'{task_id}-evidence/baseline.json')
        require(not safe_path(root, source).exists(), 'Baseline уже существует; автоматическая замена запрещена')
        files = verification_policy.applicability_files(root, rel, text, meta, _rel(root, paths['task_index']))
        required = verification_policy.required_checks(data['task_workflow'], meta['verification_plan'],
            baseline=files, current=files)
        require(required['ready'], '\n'.join(required['errors']))
        baseline = {'schema_version': 1, 'task_id': task_id, 'at': at, 'files': files}
        payload = (_canonical(baseline) + '\n').encode('utf-8')
        wf['baseline'] = {'source': source, 'sha256': file_operations.sha256(payload)}
        writes[source] = payload
        meta.setdefault('materials', []).append({'id': 'workflow-baseline', 'role': 'report',
            'source': source, 'storage': 'local', 'required': True, 'immutable': True, 'used_by': [task_id]})
        wf.setdefault('transitions', []).append({'at': at, 'action': action, 'reason': reason, 'actor': actor})
        text = re.sub(r'^Status:.*$', 'Status: in_progress', text, flags=re.M)
    elif action == 'route':
        require(rec['status'] in {'ready', 'in_progress'}, 'route требует ready/in_progress; после передачи сначала revise')
        old = wf['route']
        require(to_route in documents.ROUTES, 'Неизвестный маршрут')
        if old == to_route and wf.get('reason') == reason:
            return {'status': 'already_completed'}
        order = ['quick', 'small', 'medium', 'large']
        if order.index(to_route) < order.index(old) and (wf.get('baseline') or rec['status'] == 'in_progress'):
            require(approval and approval.strip(), 'Понижение после старта требует --engineer-approval с автором/основанием')
        event = {'at': at, 'from_route': old, 'to_route': to_route, 'reason': reason, 'actor': actor}
        if approval:
            event['engineer_approval'] = approval
        wf.setdefault('history', []).append(event)
        wf['route'], wf['reason'] = to_route, reason
    elif action == 'revise':
        if rec['status'] == 'in_progress' and wf.get('transitions', []) and wf['transitions'][-1]['action'] == 'revise' and wf['transitions'][-1]['reason'] == reason:
            return {'status': 'already_completed'}
        require(rec['status'] == 'in_review', 'revise требует in_review')
        wf.setdefault('transitions', []).append({'at': at, 'action': action, 'reason': reason,
            'actor': actor, 'handoff': meta.pop('handoff', None), 'acceptance': meta.pop('acceptance', None)})
        text = re.sub(r'^Status:.*$', 'Status: in_progress', text, flags=re.M)
    result = _write_card_meta(root, rel, text, meta, action, actor, reason, ctx,
                             safe_path, require, extra_writes=writes)
    print(f'{task_id}: {action} записан; карточка и индекс согласованы.')
    return result


def _v2_coverage(root, task_id, rel, text, meta, safe_path, require):
    # Shared runner evaluation; no commands and no writes.
    data = json.loads((root / 'ai-dev-playbook/project.json').read_text(encoding='utf-8'))
    pre = verification.preflight_v2(root,
        {'id': task_id, 'path': root / rel, 'status': 'in_review'},
        data['task_workflow'], None, safe_path, require, print_output=False)
    require(pre['ready'], '\n'.join(pre['errors']))
    cov = verification_policy.coverage(pre['required_checks'], pre['reusable_map'])
    require(cov['satisfied'], f'{task_id}: неполное или устаревшее coverage: {cov["checks"]}')
    return cov, pre


def _result_hash(root, rel, text, meta):
    data = json.loads((root / 'ai-dev-playbook/project.json').read_text(encoding='utf-8'))
    excluded = {data['paths']['task_index'], rel, str(documents.meta_path(rel))}
    evidence_root = str(Path(rel).parent / (Path(rel).stem + '-evidence'))
    excluded.update(m['source'] for m in meta.get('materials', [])
                    if m.get('storage') == 'local' and m.get('immutable')
                    and (m['source'] == evidence_root or m['source'].startswith(evidence_root + '/')))
    files = verification_policy.project_files(root, exclude=excluded)
    return verification_policy.fingerprint({'files': files,
        'task': verification_policy.task_projection(text, task_cards.metadata(text))})



def _evidence_hash(path):
    if path.is_symlink():
        raise ValueError('Symlink в материале: ' + str(path))
    if path.is_file():
        return verification_policy.fingerprint({'kind': 'file', 'sha256': file_operations.sha256(path.read_bytes())})
    if not path.is_dir():
        raise ValueError('Непереносимый материал: ' + str(path))
    return verification_policy.fingerprint({'kind': 'directory', 'entries': {
        child.name: _evidence_hash(child) for child in sorted(path.iterdir())}})

def _handoff_v2_evidence(root, task_id, rel, text, meta, safe_path, require):
    handoff = meta.get('handoff')
    require(isinstance(handoff, dict) and handoff.get('result_hash'), f'{task_id}: нет v2 передачи')
    require(card_projection(text) == handoff.get('card_projection'), 'Требования изменились после передачи')
    hashes = handoff.get('evidence_hashes', {})
    require(set(handoff.get('reports', [])) <= set(hashes), 'Передача не связывает байты всех отчётов')
    materials = {m['id']: m for m in meta.get('materials', [])}
    for ident, material in materials.items():
        if (material.get('storage') != 'local' or not material.get('immutable')
                or ident in hashes or ident in meta.get('audit_packages', {})):
            continue
        source = material['source']
        known_run = (source.startswith(f'work/verification/{task_id}/')
                     and verification.runner_directory(safe_path(root, source)))
        require(known_run, 'Новый материал требует новой передачи: ' + ident)
    for ident, expected in hashes.items():
        material = materials.get(ident, {})
        require(material.get('storage') == 'local' and material.get('immutable'), 'Нет immutable отчёта')
        path = safe_path(root, material['source'])
        report = path / 'report.json' if path.is_dir() else path
        require(_evidence_hash(path) == expected, 'Байты отчёта изменились после передачи')
        if ident in handoff['reports']:
            verification.load_report(report, require)
    cov, _ = _v2_coverage(root, task_id, rel, text, meta, safe_path, require)
    require(cov == handoff.get('coverage'), 'Покрытие изменилось после передачи')
    require(_result_hash(root, rel, text, meta) == handoff['result_hash'],
            'Результат изменился после передачи; повторите затронутые проверки и handoff')
    _checks_gate(root, task_id, rel, text, meta, safe_path, require)
    return handoff


def handoff_v2_command(root, task_id, summary, actor_name, actor_model,
                       reports, ctx, safe_path, require):
    root, data, paths, records, rec, rel, text, meta = _v2_context(
        root, task_id, ctx, safe_path, require)
    require(rec['status'] in {'in_progress', 'in_review'}, 'handoff требует in_progress/in_review')
    require(actor_name and actor_model and ctx['model_unconfirmed'] not in actor_model, 'Требуется точная атрибуция')
    _dependencies_done(task_id, records, require)
    _checks_gate(root, task_id, rel, text, meta, safe_path, require)
    cov, _ = _v2_coverage(root, task_id, rel, text, meta, safe_path, require)
    meta = copy.deepcopy(meta)
    audit_sources = {m['source'] for m in meta.get('materials', []) if m['id'] in meta.get('audit_packages', {})}
    registered = {m['source'] for m in meta.get('materials', []) if m.get('storage') == 'local'}
    candidates = [(d, p) for d, p in _verify_runs(root, task_id, safe_path)
                  if verification.runner_directory(d) or _rel(root, d) not in registered]
    # --report adds explicit evidence; it cannot hide newer failed history.
    for report_rel in reports:
        p = safe_path(root, report_rel)
        rp = p / 'report.json' if p.is_dir() else p
        value = verification.load_report(rp, require)
        require(value.get('schema_version') == 2 and value.get('task_id') == task_id, 'Нужен v2 отчёт этой задачи')
        pair = (rp.parent, rp)
        if pair not in candidates:
            candidates.append(pair)
    hashes = {}
    refs = []
    for directory, path in candidates:
        source = _rel(root, directory)
        if source in audit_sources:
            continue
        require(path.is_file(), f'Нет report.json: {source}')
        try:
            report = verification.load_report(path, require)
            require(report.get('schema_version') == 2 and report.get('task_id') == task_id, 'Чужой отчёт')
        except (ValueError, OSError):
            require(verification.runner_directory(directory), f'Повреждённый материал {source}')
            report = None  # preserved history, never evidence
        ident = 'verify-' + directory.name
        existing = next((m for m in meta['materials'] if m['id'] == ident), None)
        if existing:
            require(existing['source'] == source and existing.get('immutable'), 'Конфликт материала отчёта')
        else:
            meta['materials'].append({'id': ident, 'role': 'report', 'source': source,
                'storage': 'local', 'required': True, 'immutable': True, 'used_by': [task_id]})
        if report is not None:
            refs.append(ident)
            hashes[ident] = _evidence_hash(directory)
    catalog = _task_material_catalog(root, task_id, meta, ctx, safe_path, require)
    stray = [i['path'] for i in catalog['candidates'] if i.get('owner') == task_id]
    require(not stray, f'Неучтённые материалы: {stray}; выполните register-material')
    binding = {}
    if task_cards.version(text) == 2:
        entries = task_cards.parse(text)['entries']
        last = entries[-1] if entries else None
        require(last and last['type'] == 'handoff', 'Добавьте последнюю HANDOFF-запись')
        require(last['fields']['Исполнитель'] == actor_name and last['fields']['Модель'] == actor_model, 'Атрибуция handoff не совпадает с журналом')
        binding = {'entry_id': last['id'], 'journal_sha256': last['journal_sha256']}
        text, meta = _prepare_criteria(root, task_id, rel, text, meta, safe_path, require)
    for material in meta['materials']:
        if (material.get('storage') == 'local' and material.get('immutable')
                and material['id'] not in meta.get('audit_packages', {})):
            hashes[material['id']] = _evidence_hash(safe_path(root, material['source']))
    result_hash = _result_hash(root, rel, text, meta)
    previous = meta.get('handoff')
    if (rec['status'] == 'in_review' and previous and previous.get('result_hash') == result_hash
            and previous.get('evidence_hashes') == hashes and previous.get('coverage') == cov
            and previous.get('actor') == {'name': actor_name, 'model': actor_model}
            and all(previous.get(k) == v for k, v in binding.items())):
        print(f'{task_id}: передача уже актуальна.')
        return {'status': 'already_completed'}
    if previous:
        meta['workflow'].setdefault('transitions', []).append({
            'at': file_operations.now_iso(), 'action': 'revise', 'actor': actor_name,
            'reason': 'Новая передача заменила прежнюю',
            'handoff': previous, 'acceptance': meta.get('acceptance')})
    meta['handoff'] = {'at': file_operations.now_iso(), 'actor': {'name': actor_name, 'model': actor_model},
        'summary': summary, 'reports': sorted(refs), 'card_projection': card_projection(text),
        'result_hash': result_hash, 'coverage': cov, 'evidence_hashes': hashes, **binding}
    meta.pop('acceptance', None)
    text = re.sub(r'^Status:.*$', 'Status: in_review', text, flags=re.M)
    result = _write_card_meta(root, rel, text, meta, 'handoff', actor_name, summary,
                             ctx, safe_path, require)
    print(f'{task_id}: v2 передача записана, coverage полное; статус и индекс in_review.')
    return result


def _criterion_v2_report(root, task_id, rel, text, meta, report, safe_path, require, historical=False):
    if report.get('task_id') != task_id or report.get('status') != 'passed':
        return False
    if historical:
        return report.get('coverage', {}).get('satisfied') is True
    raw = task_cards.metadata(text)
    if report.get('plan_hash') != verification_policy.fingerprint(raw['verification_plan']):
        return False
    cov, _ = _v2_coverage(root, task_id, rel, text, meta, safe_path, require)
    return cov['satisfied']
