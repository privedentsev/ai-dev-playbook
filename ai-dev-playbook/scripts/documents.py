"""Каталог документов: метаданные, коллекции, связи и владельцы материалов.

Read-only часть жизненного цикла документов (design §2–5): разбор блока
метаданных, проверка коллекций из project.json, каталог с графом отношений
и единственным владельцем каждого материала. Запись и архивирование —
отдельные команды, здесь не реализованы.

Контракт метаданных документа — ровно один блок, ограниченный маркерами
``<!-- playbook-meta:start/end -->`` вне fenced-контекста, с одним
fenced-блоком JSON внутри; примеры в других fenced-блоках не регистрируются:

- ``schema_version``: 1; неизвестная версия — ошибка, не legacy.
- ``document_id``: ``<kind>:<slug>`` (``spec:notifications``, ``adr:0001``),
  префикс равен ``kind``, неизменяем, уникален с учётом регистра.
- ``kind``: ``discussion | spec | design | adr | plan``; ``state`` —
  из набора соответствующего kind; архивный файл требует финальный state.
- ``created_at``/``updated_at``: ISO 8601 с часовым поясом.
- ``current_revision``: целое >= 1; планируемые правки идут в ``changes``
  и действующей ревизии не заменяют.
- ``relations``: ``{relation, target}``, relation —
  ``changes | replaces | cancels | based_on``, target — существующий ID
  документа; цепочки ``replaces`` ацикличны.
- ``tasks``/``changes[].tasks``: ID существующих карточек.
- ``changes``: ``{id, state, section, basis, tasks, acceptance}``; state —
  ``draft | approved | implemented | accepted | cancelled``; basis —
  ``{type: engineer_approval | authorized_scope, who, at, detail, source}``.
- ``materials``: ``{id, role, source, storage, required, immutable,
  used_by}``; role — ``report | log | screenshot | probe | other``;
  storage — ``local | external``; у external обязателен ``access``.
  Локальный материал — путь внутри корня без symlink, не внутри
  кода/тестов компонентов; один владелец, каталоги владельцев
  не пересекаются; обязательный материал существует.
- ``archive``: null либо ``{at, reason, successor, operation_id,
  original_path}``; расположение файла обязано совпадать с archive.
- ``moves`` (необязательно): служебная карта переносов владельца —
  ``{from, to}`` относительными путями; file и dir записи. Карты всех
  владельцев собираются в общую ``catalog["moves"]``; один ``from``
  не может указывать на разные ``to``.

Дополнительный блок карточки задачи: ``schema_version``, ``relations``,
``materials``, ``handoff``, ``acceptance``, ``moves`` — поля шапки (ID,
Status, даты, исполнитель) в метаданных не дублируются. ``handoff`` —
запись передачи ``{at, actor{name,model}, summary, reports,
card_projection}``: состав проверяемого изменения, id материалов-отчётов
verify и отпечаток проекции карточки. ``acceptance`` — протокол приёмки
``{at, actor, role, kind, result, handoff_at, session, detail}``:
``kind`` — ``diagnostic|acceptance``, ``result`` — ``accepted|cancelled``,
``handoff_at`` привязывает приёмку к конкретной передаче.

Индексы коллекций генерируются из каталога в область между маркерами
``<!-- playbook-index:start/end -->`` файла ``index`` каждой коллекции:
актуальные сверху, архив ниже; у планов выводятся связанные задачи без
их статусов. Ручное содержимое вне маркеров сохраняется; файл без
выделенной области не перезаписывается и отмечается проверкой.

Коллекции в ``project.json → documents``: ``schema_version: 1``,
``collections.<kind> = {active, archive, index}`` внутри ``paths.docs``,
без пересечений с задачами и друг с другом; index — Markdown внутри
``active`` и исключён из поиска документов. Файлы коллекций без блока —
``legacy``; вложенные файлы (материалы) документами не считаются.
Ошибки формата содержат путь документа и имя поля.
"""

import datetime as dt
import json
import math
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import document_links
import block_workflow

META_START = "<!-- playbook-meta:start -->"
META_END = "<!-- playbook-meta:end -->"
EXTERNAL_META = "<!-- playbook-meta:external -->"


def external_meta_position(text):
    """Return the actual header marker line; examples and inline mentions are text."""
    fence = None
    markers = []
    headings = []
    for number, line in enumerate(text.splitlines()):
        if fence:
            if document_links.closes_fence(line, fence):
                fence = None
            continue
        match = document_links.FENCE.match(line)
        if match:
            fence = match.group(1)
        elif line == EXTERNAL_META:
            markers.append(number)
        elif line.startswith('## '):
            headings.append(number)
    if not markers:
        return None
    if len(markers) != 1 or not TASK_ID.fullmatch(_card_id(text) or ''):
        raise ValueError('external metadata: неверный маркер/ID карточки')
    if any(number < markers[0] for number in headings):
        raise ValueError('external metadata: маркер должен находиться в шапке')
    return markers[0]


def external_card(text):
    return external_meta_position(text) is not None


def hydrate_card(text, meta):
    """Insert at the validated structural position, preserving every other line."""
    position = external_meta_position(text)
    if position is None:
        raise ValueError('external metadata: нет структурного маркера')
    raw = json.dumps(meta, ensure_ascii=False, indent=2)
    lines = text.split('\n')
    lines[position + 1:position + 1] = [META_START, '```json', *raw.splitlines(), '```', META_END]
    return '\n'.join(lines)


def _card_id(text):
    match = re.search(r'(?m)^ID: ([^\n]+)$', text)
    return match[1] if match else None


def meta_path(path):
    return path.with_suffix('.meta.json') if hasattr(path, 'view') else Path(path).with_suffix('.meta.json')


def read_text(path):
    """Load a card pair into the existing in-memory metadata representation.

    Other files and inline cards retain their original representation.
    Never writes, follows sidecar symlinks, or silently falls back on lost state.
    """
    if not hasattr(path, 'view'):
        path = Path(path)
    text = path.read_text(encoding='utf-8')
    if path.suffix != '.md':
        return text
    external = external_card(text)
    sidecar = meta_path(path)
    exists = sidecar.exists() or sidecar.is_symlink()
    if not external:
        if exists:
            raise ValueError(f'{path}: sidecar без внешнего маркера (два источника или orphan)')
        return text
    if _meta_positions(text.splitlines()) is not None:
        raise ValueError(f'{path}: два источника метаданных')
    if not sidecar.is_file() or sidecar.is_symlink():
        raise ValueError(f'{sidecar}: обязательный обычный файл метаданных отсутствует или symlink')
    if path.stem != _card_id(text):
        raise ValueError(f'{path}: ID не совпадает с именем карточки')
    try:
        meta = json.loads(sidecar.read_text(encoding='utf-8'), object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError) as error:
        raise ValueError(f'{sidecar}: повреждённый JSON: {error}') from error
    if not isinstance(meta, dict):
        raise ValueError(f'{sidecar}: метаданные должны быть объектом')
    if type(meta.get('card_format_version')) is not int or meta['card_format_version'] <= 0:
        raise ValueError(f'{sidecar}: отсутствует корректный card_format_version')
    return hydrate_card(text, meta)


def split_card_writes(writes):
    """Serialize hydrated cards before planning a single recoverable operation."""
    result = dict(writes)
    for rel, content in writes.items():
        if not rel.endswith('.md') or not isinstance(content, (bytes, bytearray)):
            continue
        text = content.decode('utf-8')
        if not external_card(text):
            continue
        errors = []
        meta = parse_meta(text, rel, errors)
        if meta is None and not errors:
            continue  # A plain link rewrite changes only Markdown, not its sidecar.
        if errors:
            raise ValueError(f'{rel}: внешний JSON не подготовлен: {errors}')
        positions = _meta_positions(text.splitlines())
        start, end = positions
        lines = text.split('\n')
        result[rel] = '\n'.join(lines[:start] + lines[end + 1:]).encode('utf-8')
        result[str(meta_path(rel))] = (json.dumps(meta, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    return result


FENCE = document_links.FENCE
DOC_ID = re.compile(r"[a-z][a-z0-9_-]*:[a-z0-9][a-z0-9_-]*\Z")
TASK_ID = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-[0-9]+\Z")

KINDS = ("discussion", "spec", "design", "adr", "plan")
STATES = {
    "discussion": {"open", "resolved", "deferred", "cancelled"},
    "spec": {"draft", "accepted", "superseded", "cancelled"},
    "design": {"draft", "accepted", "superseded", "cancelled"},
    "adr": {"draft", "accepted", "superseded", "cancelled"},
    "plan": {"draft", "accepted", "completed", "cancelled"},
}
ARCHIVED_STATES = {"resolved", "deferred", "cancelled", "superseded", "completed"}
RELATIONS = {"changes", "replaces", "cancels", "based_on"}
CHANGE_STATES = {"draft", "approved", "implemented", "accepted", "cancelled"}
BASIS_TYPES = {"engineer_approval", "authorized_scope"}
ROLES = {"report", "log", "screenshot", "probe", "other"}
STORAGE = {"local", "external"}

ROUTES = {"quick", "small", "medium", "large"}

DOC_FIELDS = {"schema_version", "document_id", "kind", "state", "created_at",
              "updated_at", "current_revision", "relations", "tasks", "changes",
              "materials", "archive", "moves", "acceptance_policy"}
# У карточки шапка — единственный источник ID/Status/дат/исполнителя;
# дополнительный блок содержит только связи, материалы и сведения передачи.
TASK_FIELDS_V1 = {"schema_version", "relations", "materials", "handoff",
                  "acceptance", "moves", "card_format_version", "criterion_evidence", "checkbox_legacy_findings", "audit_packages", "block"}
TASK_FIELDS = TASK_FIELDS_V1
TASK_FIELDS_V2 = {"schema_version", "workflow", "verification_plan",
                  "relations", "materials", "handoff", "acceptance", "moves", "card_format_version", "criterion_evidence", "checkbox_legacy_findings", "audit_packages", "block"}
WORKFLOW_FIELDS = {"route", "reason", "history", "baseline", "transitions"}
ROUTE_HISTORY_FIELDS = {"at", "from_route", "to_route", "reason", "actor", "engineer_approval"}
PLAN_FIELDS = {"revision", "rationale", "scope", "checks", "decisions"}
CHECK_FIELDS = {"id", "kind", "purpose", "registry_id", "scenario", "inputs",
                "applicability", "timeout_seconds"}
DECISION_FIELDS = {"check_id", "at", "actor", "decision", "reason",
                   "engineer_approval"}
APPLICABILITY_KINDS = {"always", "when_paths", "checkpoint"}
ARCHIVE_FIELDS = {"at", "reason", "successor", "operation_id", "original_path"}
MOVE_FIELDS = {"from", "to"}
RELATION_FIELDS = {"relation", "target"}
CHANGE_FIELDS = {"id", "state", "section", "basis", "tasks", "acceptance"}
BASIS_FIELDS = {"type", "who", "at", "detail", "source"}
MATERIAL_FIELDS = {"id", "role", "source", "storage", "required", "immutable",
                   "used_by", "access"}


def _err(errors, label, message, field=None):
    if field is not None:
        errors.append(f"{label}: поле '{field}': {message}")
    else:
        errors.append(f"{label}: {message}")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"повторный JSON-ключ '{key}'")
        result[key] = value
    return result


def _iso(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _meta_positions(lines):
    """(start, end) — индексы строк маркеров вне fenced-блоков, либо None."""
    fence, start, end = None, None, None
    for index, line in enumerate(lines):
        if fence is not None:
            if document_links.closes_fence(line, fence):
                fence = None
            continue
        marker = FENCE.match(line)
        if marker:
            fence = marker.group(1)
            continue
        stripped = line.strip()
        if stripped == META_START:
            if start is not None:
                return "duplicate-start"
            start = index
        elif stripped == META_END:
            if start is None:
                return "end-without-start"
            if end is not None:
                return "duplicate-end"
            end = index
    if start is None:
        return "end-without-start" if end is not None else None
    return (start, end)


def parse_meta(text, label, errors):
    """Метаданные документа: dict либо None при отсутствии блока.

    Примеры внутри fenced-блоков (например, вложенный образец формата)
    метаданными не считаются. Проблемы разбора добавляются в errors.
    """
    positions = _meta_positions(text.splitlines())
    if positions is None:
        return None
    if positions == "duplicate-start":
        errors.append(f"{label}: повторный маркер {META_START}")
        return None
    if positions == "end-without-start":
        errors.append(f"{label}: {META_END} без {META_START}")
        return None
    if positions == "duplicate-end":
        errors.append(f"{label}: повторный маркер {META_END}")
        return None
    start, end = positions
    if end is None:
        errors.append(f"{label}: нет закрывающего {META_END}")
        return None
    body = text.splitlines()[start + 1:end]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    if not body:
        errors.append(f"{label}: пустой блок метаданных")
        return None
    opening = FENCE.match(body[0])
    if opening is None:
        errors.append(f"{label}: метаданные должны быть fenced-блоком JSON")
        return None
    sequence = opening.group(1)
    info = body[0].strip()[len(sequence):].strip().lower()
    if info not in {"", "json"}:
        errors.append(f"{label}: fenced-блок метаданных должен быть JSON")
        return None
    closing = FENCE.match(body[-1])
    if closing is None or closing.group(1)[0] != sequence[0] \
            or len(closing.group(1)) < len(sequence) \
            or closing.group(1).strip() != body[-1].strip():
        errors.append(f"{label}: fenced-блок метаданных не закрыт перед {META_END}")
        return None
    try:
        data = json.loads("\n".join(body[1:-1]), object_pairs_hook=_unique_pairs)
    except ValueError as error:
        errors.append(f"{label}: метаданные: {error}")
        return None
    if not isinstance(data, dict):
        errors.append(f"{label}: метаданные должны быть JSON-объектом")
        return None
    return data


def _fields(meta, allowed, required, label, errors):
    unknown = sorted(set(meta) - allowed)
    if unknown:
        _err(errors, label, f"неизвестные поля {unknown}")
    missing = sorted(required - set(meta))
    if missing:
        _err(errors, label, f"нет обязательных полей {missing}")


def _str(value):
    return isinstance(value, str) and bool(value.strip())


def _relations(value, label, errors):
    if not isinstance(value, list):
        _err(errors, label, "ожидается список", field="relations")
        return []
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            _err(errors, label, "отношение должно быть объектом", field="relations")
            continue
        _fields(item, RELATION_FIELDS, RELATION_FIELDS, label, errors)
        relation, target = item.get("relation"), item.get("target")
        ok = True
        if not isinstance(relation, str) or relation not in RELATIONS:
            _err(errors, label, f"неизвестное отношение {relation!r}", field="relations")
            ok = False
        if not _str(target):
            _err(errors, label, "target должен быть непустой строкой", field="relations")
            ok = False
        elif not DOC_ID.fullmatch(target):
            _err(errors, label, f"target '{target}' — не ID документа", field="relations")
            ok = False
        if not ok:
            continue
        pair = (relation, target)
        if pair in seen:
            _err(errors, label, f"повторное отношение {relation} → {target}",
                 field="relations")
            continue
        seen.add(pair)
        result.append({"relation": relation, "target": target})
    return result


def _task_refs(value, label, errors, field="tasks"):
    if not isinstance(value, list) or not all(_str(item) for item in value):
        _err(errors, label, "ожидается список ID карточек", field=field)
        return []
    result = []
    for item in value:
        if not TASK_ID.fullmatch(item):
            _err(errors, label, f"'{item}' — не ID карточки", field=field)
        result.append(item)
    if len(result) != len(set(result)):
        _err(errors, label, "повторный ID карточки", field=field)
    return result


def _changes(value, label, errors):
    if not isinstance(value, list):
        _err(errors, label, "ожидается список", field="changes")
        return []
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            _err(errors, label, "изменение должно быть объектом", field="changes")
            continue
        _fields(item, CHANGE_FIELDS, CHANGE_FIELDS, label, errors)
        ident = item.get("id")
        if not _str(ident):
            _err(errors, label, "id изменения должен быть непустой строкой",
                 field="changes")
        elif ident in seen:
            _err(errors, label, f"повторный id изменения '{ident}'", field="changes")
        else:
            seen.add(ident)
        if not isinstance(item.get("state"), str) \
                or item.get("state") not in CHANGE_STATES:
            _err(errors, label, f"неизвестное состояние {item.get('state')!r}",
                 field="changes")
        if not _str(item.get("section")):
            _err(errors, label, "section должен быть непустой строкой", field="changes")
        basis = item.get("basis")
        if not isinstance(basis, dict):
            _err(errors, label, "basis должен быть объектом", field="changes")
        else:
            _fields(basis, BASIS_FIELDS, BASIS_FIELDS, label, errors)
            if not isinstance(basis.get("type"), str) \
                    or basis.get("type") not in BASIS_TYPES:
                _err(errors, label, f"basis.type {basis.get('type')!r} не из "
                     f"{sorted(BASIS_TYPES)}", field="changes")
            for key in ("who", "detail", "source"):
                if not _str(basis.get(key)):
                    _err(errors, label, f"basis.{key} должен быть непустой строкой",
                         field="changes")
            if _iso(basis.get("at")) is None:
                _err(errors, label, "basis.at должен быть ISO-временем с часовым поясом",
                     field="changes")
        tasks = item.get("tasks")
        if not isinstance(tasks, list) \
                or not all(_str(t) and TASK_ID.fullmatch(t) for t in tasks):
            _err(errors, label, "tasks изменения — список ID карточек", field="changes")
            tasks = []
        acceptance = item.get("acceptance")
        if acceptance is not None and not _str(acceptance):
            _err(errors, label, "acceptance — строка со ссылкой либо null",
                 field="changes")
        result.append({"id": ident, "state": item.get("state"),
                       "section": item.get("section"), "basis": basis,
                       "tasks": tasks, "acceptance": acceptance})
    return result


def _materials(value, label, errors):
    if not isinstance(value, list):
        _err(errors, label, "ожидается список", field="materials")
        return []
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            _err(errors, label, "материал должен быть объектом", field="materials")
            continue
        _fields(item, MATERIAL_FIELDS, MATERIAL_FIELDS - {"access"}, label, errors)
        ident = item.get("id")
        if not _str(ident):
            _err(errors, label, "id материала должен быть непустой строкой",
                 field="materials")
        elif ident in seen:
            _err(errors, label, f"повторный id материала '{ident}'", field="materials")
        else:
            seen.add(ident)
        if not isinstance(item.get("role"), str) or item.get("role") not in ROLES:
            _err(errors, label, f"role {item.get('role')!r} не из {sorted(ROLES)}",
                 field="materials")
        storage = item.get("storage")
        if not isinstance(storage, str) or storage not in STORAGE:
            _err(errors, label, f"storage {storage!r} не из {sorted(STORAGE)}",
                 field="materials")
        source = item.get("source")
        if not _str(source):
            _err(errors, label, "source должен быть непустой строкой",
                 field="materials")
        elif storage == "external":
            try:
                link = urlsplit(source)
            except ValueError:
                link = None
            if link is None or not link.scheme \
                    or (link.scheme in {"http", "https"} and not link.netloc):
                _err(errors, label, f"external source '{source}' — не URI",
                     field="materials")
            if not _str(item.get("access")):
                _err(errors, label, "external требует описание доступа 'access'",
                     field="materials")
        elif storage == "local" and "access" in item:
            _err(errors, label, "access допустим только для external",
                 field="materials")
        for key in ("required", "immutable"):
            if type(item.get(key)) is not bool:
                _err(errors, label, f"{key} должен быть true/false", field="materials")
        used_by = item.get("used_by")
        if not isinstance(used_by, list) or not all(_str(v) for v in used_by):
            _err(errors, label, "used_by — список ID потребителей", field="materials")
            used_by = []
        result.append({**item, "used_by": used_by})
    return result


def _moves(value, label, errors):
    if not isinstance(value, list):
        _err(errors, label, "ожидается список", field="moves")
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            _err(errors, label, "перенос должен быть объектом", field="moves")
            continue
        _fields(item, MOVE_FIELDS, MOVE_FIELDS, label, errors)
        for key in ("from", "to"):
            if not _str(item.get(key)):
                _err(errors, label, f"{key} должен быть непустой строкой",
                     field="moves")
        result.append({"from": item.get("from"), "to": item.get("to")})
    return result


def _archive(value, label, errors):
    if value is None:
        return None
    if not isinstance(value, dict):
        _err(errors, label, "archive — объект либо null", field="archive")
        return None
    _fields(value, ARCHIVE_FIELDS, ARCHIVE_FIELDS, label, errors)
    if _iso(value.get("at")) is None:
        _err(errors, label, "at должно быть ISO-временем с часовым поясом",
             field="archive")
    if not _str(value.get("reason")):
        _err(errors, label, "reason должен быть непустой строкой", field="archive")
    successor = value.get("successor")
    if successor is not None and (not _str(successor) or not DOC_ID.fullmatch(successor)):
        _err(errors, label, f"successor '{successor}' — не ID документа или null",
             field="archive")
    if not _str(value.get("operation_id")):
        _err(errors, label, "operation_id должен быть непустой строкой",
             field="archive")
    if not _str(value.get("original_path")):
        _err(errors, label, "original_path должен быть непустой строкой",
             field="archive")
    return value


def _walk_files(base):
    """Все файлы под каталогом без разыменования symlink.

    Возвращает (files, links): каталоги-symlink не обходятся и попадают
    в links вместе с файлами-symlink — ни один адресат не читается.
    """
    files, links = [], []
    pending = [base]
    while pending:
        current = pending.pop()
        if not current.exists():
            continue
        for entry in current.iterdir():
            if entry.is_symlink():
                links.append(entry)
            elif entry.is_dir():
                pending.append(entry)
            elif entry.is_file():
                files.append(entry)
    return files, links


def _walk_links(base):
    return _walk_files(base)[1]


def _doc_title(text):
    """Первый заголовок `# …` вне fenced-блоков и блока метаданных."""
    fence, in_meta = None, False
    for line in text.splitlines():
        if fence is not None:
            if document_links.closes_fence(line, fence):
                fence = None
            continue
        marker = FENCE.match(line)
        if marker:
            fence = marker.group(1)
            continue
        stripped = line.strip()
        if stripped == META_START:
            in_meta = True
        elif stripped == META_END:
            in_meta = False
        elif not in_meta:
            heading = re.match(r"^ {0,3}#\s+(\S.*?)\s*#*\s*$", line)
            if heading:
                return heading.group(1).strip()
    return None


def _validate_document(meta, path, kind, in_archive, errors,
                       capabilities=frozenset()):
    label = str(path)
    _fields(meta, DOC_FIELDS,
            DOC_FIELDS - {"moves", "acceptance_policy"}, label, errors)
    if meta.get("schema_version") != 1:
        _err(errors, label, f"неизвестная версия {meta.get('schema_version')!r}",
             field="schema_version")
    kind_meta = meta.get("kind")
    if kind_meta not in KINDS:
        _err(errors, label, f"неизвестный kind {kind_meta!r}", field="kind")
    elif kind_meta != kind:
        _err(errors, label, f"kind '{kind_meta}' не совпадает с коллекцией '{kind}'",
             field="kind")
    ident = meta.get("document_id")
    if not isinstance(ident, str) or not DOC_ID.fullmatch(ident):
        _err(errors, label, f"некорректный document_id {ident!r}",
             field="document_id")
        ident = None
    elif kind_meta in KINDS and ident.split(":", 1)[0] != kind_meta:
        _err(errors, label, f"document_id '{ident}' не совпадает с kind "
             f"'{kind_meta}'", field="document_id")
    state = meta.get("state")
    if kind_meta in KINDS and (not isinstance(state, str)
                               or state not in STATES[kind_meta]):
        _err(errors, label, f"state {state!r} недопустим для kind '{kind_meta}'",
             field="state")
    created, updated = _iso(meta.get("created_at")), _iso(meta.get("updated_at"))
    if created is None:
        _err(errors, label, "ISO-время с часовым поясом", field="created_at")
    if updated is None:
        _err(errors, label, "ISO-время с часовым поясом", field="updated_at")
    elif created is not None and updated < created:
        _err(errors, label, "updated_at раньше created_at", field="updated_at")
    if type(meta.get("current_revision")) is not int or meta.get("current_revision", 0) < 1:
        _err(errors, label, "current_revision — положительное целое",
             field="current_revision")
    relations = _relations(meta.get("relations"), label, errors)
    tasks = _task_refs(meta.get("tasks"), label, errors)
    changes = _changes(meta.get("changes"), label, errors)
    materials = _materials(meta.get("materials"), label, errors)
    archive = _archive(meta.get("archive"), label, errors)
    moves = _moves(meta.get("moves", []), label, errors)
    acceptance_policy = None
    if "acceptance_policy" in meta:
        if block_workflow.CAPABILITY not in capabilities:
            _err(errors, label, "поле требует явной capability "
                 f"'{block_workflow.CAPABILITY}' в project.json → "
                 "documents.capabilities", field="acceptance_policy")
        elif kind_meta != "plan":
            _err(errors, label, "acceptance_policy допускается только "
                 "в документе kind 'plan'", field="acceptance_policy")
        else:
            acceptance_policy = block_workflow.validate_policy(
                meta["acceptance_policy"], label, errors)
    if in_archive and archive is None:
        errors.append(f"{label}: файл в архивном каталоге, но archive равен null")
    if not in_archive and archive is not None:
        errors.append(f"{label}: archive задан, но файл в активном каталоге")
    final = STATES.get(kind_meta, set()) - ARCHIVED_STATES \
        if isinstance(kind_meta, str) else set()
    if archive is not None and isinstance(state, str) and state in final:
        errors.append(f"{label}: архивный документ с нефинальным state '{state}'")
    return {"id": ident, "kind": kind, "state": state, "path": path,
            "archived": in_archive, "revision": meta.get("current_revision"),
            "updated_at": meta.get("updated_at"), "relations": relations,
            "tasks": tasks, "changes": changes, "materials": materials,
            "archive": archive, "moves": moves,
            "acceptance_policy": acceptance_policy,
            "has_policy": "acceptance_policy" in meta, "title": None}


ACTOR_FIELDS = {"name", "model"}
HANDOFF_FIELDS = {"at", "actor", "summary", "reports", "card_projection",
                  "entry_id", "journal_sha256", "result_hash", "coverage", "evidence_hashes"}
ACCEPTANCE_FIELDS = {"at", "actor", "role", "kind", "result", "handoff_at",
                     "session", "detail", "audit_id", "audit_sha256", "result_hash"}
ACCEPTANCE_ROLES = {"auditor", "engineer"}
ACCEPTANCE_KINDS = {"diagnostic", "acceptance"}
ACCEPTANCE_RESULTS = {"accepted", "cancelled"}
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _actor(value, label, errors, field):
    """Запись участника {name, model|null}; model — фактическая либо null."""
    if not isinstance(value, dict):
        _err(errors, label, "ожидается объект", field=field)
        return
    _fields(value, ACTOR_FIELDS, {"name", "model"}, label, errors)
    if not _str(value.get("name")):
        _err(errors, label, "name должен быть непустой строкой", field=field)
    if value.get("model") is not None and not _str(value.get("model")):
        _err(errors, label, "model — непустая строка либо null", field=field)


def _handoff(value, label, errors, material_ids):
    """Запись передачи: состав проверяемого изменения и отпечатки входов.

    ``reports`` — id материалов этой же карточки (прогоны verify);
    ``card_projection`` — SHA-256 проекции карточки без разделов аудита,
    блока acceptance и служебных полей Status/Closed.
    """
    if not isinstance(value, dict):
        _err(errors, label, "ожидается объект", field="handoff")
        return
    _fields(value, HANDOFF_FIELDS, HANDOFF_FIELDS - {"entry_id", "journal_sha256", "result_hash", "coverage", "evidence_hashes"}, label, errors)
    if _iso(value.get("at")) is None:
        _err(errors, label, "at должно быть ISO-временем с часовым поясом",
             field="handoff")
    _actor(value.get("actor"), label, errors, "handoff")
    if not _str(value.get("summary")):
        _err(errors, label, "summary должно быть непустой строкой",
             field="handoff")
    reports = value.get("reports")
    if not isinstance(reports, list) \
            or not all(isinstance(item, str) for item in reports):
        _err(errors, label, "reports — список id материалов", field="handoff")
    else:
        for report in reports:
            if report not in material_ids:
                _err(errors, label, f"reports ссылается на неизвестный "
                     f"материал '{report}'", field="handoff")
    digest = value.get("card_projection")
    if not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
        _err(errors, label, "card_projection ожидается sha256:<hex>",
             field="handoff")


    for key, pattern in (("entry_id", r"HANDOFF-[0-9]{2,}"),
                         ("journal_sha256", r"sha256:[0-9a-f]{64}")):
        if key in value and (not isinstance(value[key], str) or re.fullmatch(pattern, value[key]) is None):
            _err(errors, label, f"некорректный {key}", field="handoff")
    if ("entry_id" in value) != ("journal_sha256" in value):
        _err(errors, label, "entry_id и journal_sha256 задаются вместе", field="handoff")

    if "result_hash" in value and not DIGEST.fullmatch(str(value["result_hash"])):
        _err(errors, label, "некорректный result_hash", field="handoff")
    if "coverage" in value:
        cov = value["coverage"]
        if (not isinstance(cov, dict) or set(cov) != {"checks", "satisfied"}
                or type(cov.get("satisfied")) is not bool or not isinstance(cov.get("checks"), dict)
                or any(v not in {"passed", "not_applicable", "not_run", "failed", "timeout"}
                       for v in cov.get("checks", {}).values())):
            _err(errors, label, "некорректное coverage", field="handoff")
    if "evidence_hashes" in value:
        hashes = value["evidence_hashes"]
        if (not isinstance(hashes, dict) or not all(k in material_ids
                and DIGEST.fullmatch(str(v)) for k, v in hashes.items())):
            _err(errors, label, "некорректные evidence_hashes", field="handoff")


def _acceptance(value, label, errors):
    """Протокол приёмки: кто, когда и что принял либо отменил.

    ``kind`` различает ``diagnostic`` (принят протокол аудита, находки —
    отдельные карточки) и ``acceptance`` (принят результат/блок);
    ``result`` — ``accepted | cancelled``; ``handoff_at`` привязывает
    приёмку к конкретной передаче — смена передачи обесценивает запись.
    """
    if not isinstance(value, dict):
        _err(errors, label, "ожидается объект", field="acceptance")
        return
    _fields(value, ACCEPTANCE_FIELDS, {"at", "actor", "role", "kind",
                                     "result", "handoff_at"}, label, errors)
    if _iso(value.get("at")) is None:
        _err(errors, label, "at должно быть ISO-временем с часовым поясом",
             field="acceptance")
    _actor(value.get("actor"), label, errors, "acceptance")
    if value.get("role") not in ACCEPTANCE_ROLES:
        _err(errors, label, "role ожидается auditor|engineer",
             field="acceptance")
    if value.get("kind") not in ACCEPTANCE_KINDS:
        _err(errors, label, "kind ожидается diagnostic|acceptance",
             field="acceptance")
    if value.get("result") not in ACCEPTANCE_RESULTS:
        _err(errors, label, "result ожидается accepted|cancelled",
             field="acceptance")
    if "result_hash" in value and not DIGEST.fullmatch(str(value["result_hash"])):
        _err(errors, label, "некорректный result_hash", field="acceptance")
    handoff_at = value.get("handoff_at")
    if handoff_at is not None and _iso(handoff_at) is None:
        _err(errors, label, "handoff_at — ISO-время либо null",
             field="acceptance")
    if value.get("session") is not None and not _str(value.get("session")):
        _err(errors, label, "session — непустая строка либо null",
             field="acceptance")
    if value.get("detail") is not None and not _str(value.get("detail")):
        _err(errors, label, "detail — непустая строка либо null",
             field="acceptance")


    for key, pattern in (("audit_id", r"AUDIT-[0-9]{2,}"),
                         ("audit_sha256", r"sha256:[0-9a-f]{64}")):
        if key in value and (not isinstance(value[key], str) or re.fullmatch(pattern, value[key]) is None):
            _err(errors, label, f"некорректный {key}", field="acceptance")
    if ("audit_id" in value) != ("audit_sha256" in value):
        _err(errors, label, "audit_id и audit_sha256 задаются вместе", field="acceptance")


def _validate_path_str(path_str, label, errors, field):
    if not isinstance(path_str, str) or not path_str.strip():
        _err(errors, label, "путь должен быть непустой строкой", field=field)
        return False
    if "\0" in path_str:
        _err(errors, label, "путь содержит нулевой байт", field=field)
        return False
    if "\\" in path_str:
        _err(errors, label, f"путь '{path_str}' должен использовать прямые слеши (POSIX)", field=field)
        return False
    if path_str.startswith("/") or path_str.startswith("./"):
        _err(errors, label, f"путь '{path_str}' не должен быть абсолютным или начинаться с './'", field=field)
        return False
    parts = path_str.split("/")
    if ".." in parts:
        _err(errors, label, f"путь '{path_str}' содержит '..' (выход за корень)", field=field)
        return False
    if path_str == "." or path_str == "":
        _err(errors, label, "путь не должен указывать на корень проекта", field=field)
        return False
    if parts[0] == "work":
        _err(errors, label, f"путь '{path_str}' не должен находиться внутри каталога work", field=field)
        return False
    return True


def _check_fs_path(root, path_str, label, errors, field, safe_path=None):
    if root is None:
        return True
    if safe_path is not None:
        try:
            safe_path(root, path_str)
        except (ValueError, TypeError) as exc:
            _err(errors, label, str(exc), field=field)
            return False
        return True
    cur = root if hasattr(root, "view") else Path(root)
    for part in Path(path_str).parts:
        cur = cur / part
        if cur.is_symlink():
            _err(errors, label, f"символическая ссылка в управляемом пути: '{path_str}'", field=field)
            return False
    return True


def _workflow(value, label, errors):
    if not isinstance(value, dict):
        _err(errors, label, "workflow должен быть объектом", field="workflow")
        return None
    _fields(value, WORKFLOW_FIELDS, {"route"}, label, errors)
    route = value.get("route")
    if not isinstance(route, str) or route not in ROUTES:
        _err(errors, label, f"неизвестный route {route!r}", field="workflow.route")
    reason = value.get("reason")
    if reason is not None and not _str(reason):
        _err(errors, label, "reason — непустая строка", field="workflow.reason")
    history = value.get("history")
    norm_history = []
    if history is not None:
        if not isinstance(history, list):
            _err(errors, label, "history должен быть списком", field="workflow.history")
        else:
            for idx, item in enumerate(history):
                item_label = f"{label}.workflow.history[{idx}]"
                if not isinstance(item, dict):
                    _err(errors, label, "элемент history должен быть объектом",
                         field="workflow.history")
                    continue
                _fields(item, ROUTE_HISTORY_FIELDS, {"at", "to_route", "reason"},
                        item_label, errors)
                at = item.get("at")
                if not _iso(at):
                    _err(errors, item_label, f"некорректное время at: {at!r}", field="at")
                to_route = item.get("to_route")
                if to_route not in ROUTES:
                    _err(errors, item_label, f"неизвестный to_route: {to_route!r}", field="to_route")
                from_route = item.get("from_route")
                if from_route is not None and from_route not in ROUTES:
                    _err(errors, item_label, f"неизвестный from_route: {from_route!r}", field="from_route")
                if not _str(item.get("reason")):
                    _err(errors, item_label, "reason должен быть непустой строкой", field="reason")
                if "engineer_approval" in item and not _str(item["engineer_approval"]):
                    _err(errors, item_label, "engineer_approval — непустое основание инженера", field="engineer_approval")
                if item.get("actor") is not None and not _str(item.get("actor")):
                    _err(errors, item_label, "actor должен быть непустой строкой либо null", field="actor")
                norm_history.append(item)
    baseline = value.get("baseline")
    if baseline is not None:
        if (not isinstance(baseline, dict) or set(baseline) != {"source", "sha256"}
                or not _str(baseline.get("source")) or not DIGEST.fullmatch(str(baseline.get("sha256")))):
            _err(errors, label, "baseline требует source и sha256", field="workflow.baseline")
    transitions = value.get("transitions", [])
    if not isinstance(transitions, list):
        _err(errors, label, "transitions — список", field="workflow.transitions")
    else:
        for event in transitions:
            if (not isinstance(event, dict) or set(event) - {"at", "action", "reason", "actor", "handoff", "acceptance"}
                    or not _iso(event.get("at")) or event.get("action") not in {"start", "revise"}
                    or not _str(event.get("reason")) or not _str(event.get("actor"))):
                _err(errors, label, "некорректное событие transitions", field="workflow.transitions")
    result = {"route": route, "reason": reason, "history": norm_history}
    for key in ("baseline", "transitions"):
        if key in value:
            result[key] = value[key]
    return result


def _verification_plan(value, label, errors, registry=None, root=None, safe_path=None):
    if not isinstance(value, dict):
        _err(errors, label, "verification_plan должен быть объектом", field="verification_plan")
        return None
    _fields(value, PLAN_FIELDS, {"revision", "rationale", "scope", "checks"}, label, errors)
    revision = value.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        _err(errors, label, f"revision должен быть целым числом >= 1: {revision!r}",
             field="verification_plan.revision")
    rationale = value.get("rationale")
    if not _str(rationale):
        _err(errors, label, "rationale должен быть непустой строкой",
             field="verification_plan.rationale")
    scope = value.get("scope")
    norm_scope = []
    if not isinstance(scope, list) or not scope:
        _err(errors, label, "scope должен быть непустым списком путей",
             field="verification_plan.scope")
    else:
        seen_scope = set()
        for idx, entry in enumerate(scope):
            fld = f"verification_plan.scope[{idx}]"
            if _validate_path_str(entry, label, errors, fld):
                if _check_fs_path(root, entry, label, errors, fld, safe_path):
                    if entry in seen_scope:
                        _err(errors, label, f"повторный путь в scope: '{entry}'", field=fld)
                    seen_scope.add(entry)
                    norm_scope.append(entry)
    checks = value.get("checks")
    norm_checks = []
    if not isinstance(checks, list) or not checks:
        _err(errors, label, "checks должен быть непустым списком",
             field="verification_plan.checks")
    else:
        seen_check_ids = set()
        for idx, item in enumerate(checks):
            item_label = f"{label}.verification_plan.checks[{idx}]"
            if not isinstance(item, dict):
                _err(errors, label, "элемент checks должен быть объектом",
                     field="verification_plan.checks")
                continue
            _fields(item, CHECK_FIELDS, {"id", "kind", "purpose", "inputs"}, item_label, errors)
            ident = item.get("id")
            if not isinstance(ident, str) or not ident.isascii() \
                    or not ident.replace("-", "").replace("_", "").isalnum() or not ident:
                _err(errors, item_label, f"некорректный ID проверки: {ident!r}", field="id")
                ident = None
            elif ident in seen_check_ids:
                _err(errors, item_label, f"повторный ID проверки '{ident}'", field="id")
            else:
                seen_check_ids.add(ident)
            kind = item.get("kind")
            if kind not in {"command", "manual"}:
                _err(errors, item_label, f"kind проверки должен быть 'command' или 'manual': {kind!r}", field="kind")
            if not _str(item.get("purpose")):
                _err(errors, item_label, "purpose проверки должен быть непустой строкой", field="purpose")
            if "argv" in item:
                _err(errors, item_label, "произвольный argv запрещён; используйте registry_id", field="argv")
            reg_id = item.get("registry_id")
            scenario = item.get("scenario")
            if kind == "command":
                if not _str(reg_id):
                    _err(errors, item_label, "для command-проверки обязателен registry_id", field="registry_id")
                elif registry is None:
                    _err(errors, item_label, f"неизвестная команда реестра '{reg_id}': task_workflow реестр не настроен", field="registry_id")
                elif reg_id not in registry:
                    _err(errors, item_label, f"неизвестная команда реестра '{reg_id}'", field="registry_id")
                else:
                    reg_item = registry[reg_id] if isinstance(registry, dict) else None
                    if isinstance(reg_item, dict):
                        reg_kind = reg_item.get("kind")
                        if reg_kind and reg_kind != kind:
                            _err(errors, item_label, f"несовместимый kind проверки '{ident}': в карточке '{kind}', в реестре '{reg_kind}'", field="kind")
                if scenario is not None:
                    _err(errors, item_label, "scenario не допускается для command-проверки", field="scenario")
            elif kind == "manual":
                if not _str(scenario) and not _str(reg_id):
                    _err(errors, item_label, "для manual-проверки обязателен scenario или registry_id", field="scenario")
                elif _str(reg_id):
                    if registry is None:
                        _err(errors, item_label, f"неизвестная проверка реестра '{reg_id}': task_workflow реестр не настроен", field="registry_id")
                    elif reg_id not in registry:
                        _err(errors, item_label, f"неизвестная проверка реестра '{reg_id}'", field="registry_id")
                    else:
                        reg_item = registry[reg_id] if isinstance(registry, dict) else None
                        if isinstance(reg_item, dict):
                            reg_kind = reg_item.get("kind")
                            if reg_kind and reg_kind != kind:
                                _err(errors, item_label, f"несовместимый kind проверки '{ident}': в карточке '{kind}', в реестре '{reg_kind}'", field="kind")
            timeout = item.get("timeout_seconds")
            if timeout is not None:
                if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
                    _err(errors, item_label, f"timeout_seconds должен быть положительным числом: {timeout!r}", field="timeout_seconds")
            inputs = item.get("inputs")
            norm_inputs = []
            if inputs is None or not isinstance(inputs, list):
                _err(errors, item_label, "inputs должен быть списком", field="inputs")
            else:
                seen_inputs = set()
                for inp_idx, inp in enumerate(inputs):
                    inp_fld = f"inputs[{inp_idx}]"
                    if isinstance(inp, str):
                        if _validate_path_str(inp, item_label, errors, inp_fld):
                            if _check_fs_path(root, inp, item_label, errors, inp_fld, safe_path):
                                if inp in seen_inputs:
                                    _err(errors, item_label, f"повторный вход: '{inp}'", field=inp_fld)
                                seen_inputs.add(inp)
                                norm_inputs.append(inp)
                    elif isinstance(inp, dict):
                        inp_type = inp.get("type")
                        if inp_type in {"file", "directory"}:
                            _fields(inp, {"type", "path"}, {"type", "path"}, f"{item_label}.{inp_fld}", errors)
                            p = inp.get("path")
                            if _str(p):
                                if _validate_path_str(p, item_label, errors, f"{inp_fld}.path"):
                                    if _check_fs_path(root, p, item_label, errors, f"{inp_fld}.path", safe_path):
                                        if p in seen_inputs:
                                            _err(errors, item_label, f"повторный вход: '{p}'", field=f"{inp_fld}.path")
                                        seen_inputs.add(p)
                                        norm_inputs.append(inp)
                        elif inp_type == "environment":
                            _fields(inp, {"type", "names"}, {"type", "names"}, f"{item_label}.{inp_fld}", errors)
                            names = inp.get("names")
                            if not isinstance(names, list) or not names or not all(isinstance(n, str) and n.strip() for n in names):
                                _err(errors, item_label, "names должен быть непустым списком строк", field=f"{inp_fld}.names")
                            else:
                                norm_inputs.append(inp)
                        elif inp_type in {"task_projection", "policy_projection"}:
                            _fields(inp, {"type"}, {"type"}, f"{item_label}.{inp_fld}", errors)
                            norm_inputs.append(inp)
                        else:
                            _err(errors, item_label, f"неизвестный тип входа: {inp_type!r}", field=f"{inp_fld}.type")
                    else:
                        _err(errors, item_label, "вход должен быть строкой (путь) или объектом", field=inp_fld)
            app = item.get("applicability")
            norm_app = None
            if app is not None:
                if isinstance(app, str):
                    if app not in {"always", "checkpoint"}:
                        _err(errors, item_label, f"строковая применимость должна быть 'always' или 'checkpoint', для when_paths требуется объект с paths: {app!r}", field="applicability")
                    norm_app = {"kind": app}
                elif isinstance(app, dict):
                    _fields(app, {"kind", "paths", "checkpoint"}, {"kind"}, f"{item_label}.applicability", errors)
                    app_kind = app.get("kind")
                    if app_kind not in APPLICABILITY_KINDS:
                        _err(errors, item_label, f"неизвестный kind применимости: {app_kind!r}", field="applicability.kind")
                    if app_kind == "when_paths":
                        paths = app.get("paths")
                        if not isinstance(paths, list) or not paths:
                            _err(errors, item_label, "when_paths требует непустой список paths", field="applicability.paths")
                        else:
                            for p_idx, p in enumerate(paths):
                                if not isinstance(p, str) or not p.strip() or p.startswith("/") or ".." in p:
                                    _err(errors, item_label, f"некорректный glob в paths[{p_idx}]: {p!r}", field="applicability.paths")
                    elif app_kind == "checkpoint":
                        cp = app.get("checkpoint")
                        if not _str(cp):
                            _err(errors, item_label, "checkpoint требует непустую строку checkpoint", field="applicability.checkpoint")
                    norm_app = app
                else:
                    _err(errors, item_label, "applicability должен быть строкой или объектом", field="applicability")
            norm_checks.append({
                "id": ident,
                "kind": kind,
                "purpose": item.get("purpose"),
                "registry_id": reg_id,
                "scenario": scenario,
                "inputs": norm_inputs,
                "applicability": norm_app,
                "timeout_seconds": timeout,
            })
    decisions = value.get("decisions", [])
    norm_decisions = []
    if not isinstance(decisions, list):
        _err(errors, label, "decisions должен быть списком", field="verification_plan.decisions")
    else:
        for idx, item in enumerate(decisions):
            item_label = f"{label}.verification_plan.decisions[{idx}]"
            if not isinstance(item, dict):
                _err(errors, label, "элемент decisions должен быть объектом",
                     field="verification_plan.decisions")
                continue
            _fields(item, DECISION_FIELDS, {"check_id", "at", "actor", "decision", "reason"},
                    item_label, errors)
            chk_id = item.get("check_id")
            if not _str(chk_id):
                _err(errors, item_label, "check_id должен быть непустой строкой", field="check_id")
            at = item.get("at")
            if not _iso(at):
                _err(errors, item_label, f"некорректное время at: {at!r}", field="at")
            if not _str(item.get("actor")):
                _err(errors, item_label, "actor должен быть непустой строкой", field="actor")
            if not _str(item.get("decision")):
                _err(errors, item_label, "decision должен быть непустой строкой", field="decision")
            if not _str(item.get("reason")):
                _err(errors, item_label, "reason должен быть непустой строкой", field="reason")
            norm_decisions.append(item)
    return {
        "revision": revision,
        "rationale": rationale,
        "scope": norm_scope,
        "checks": norm_checks,
        "decisions": norm_decisions,
    }


def _validate_task_meta(meta, label, errors, registry=None, task_workflow_enabled=None, root=None, safe_path=None, block_enabled=False):
    card_version = meta.get("card_format_version", 1)
    if type(card_version) is not int or card_version < 1:
        _err(errors, label, "ожидается положительное целое", field="card_format_version")
    version = meta.get("schema_version")
    if version == 1:
        _fields(meta, TASK_FIELDS_V1, {"schema_version"}, label, errors)
        workflow = None
        verification_plan = None
    elif version == 2:
        _fields(meta, TASK_FIELDS_V2, {"schema_version", "workflow", "verification_plan"}, label, errors)
        if task_workflow_enabled is False or (task_workflow_enabled is None and registry is None):
            _err(errors, label, "карточка использует schema_version 2, но task_workflow v2 не включён в project.json", field="schema_version")
        workflow = _workflow(meta.get("workflow"), label, errors)
        raw_plan = meta.get("verification_plan")
        verification_plan = _verification_plan(
            raw_plan, label, errors, registry=registry, root=root, safe_path=safe_path
        )
    else:
        _err(errors, label, f"неизвестная версия {version!r}", field="schema_version")
        return None
    relations = _relations(meta.get("relations", []), label, errors)
    materials = _materials(meta.get("materials", []), label, errors)
    moves = _moves(meta.get("moves", []), label, errors)
    material_ids = {item.get("id") for item in materials
                    if isinstance(item.get("id"), str)}
    packages = meta.get('audit_packages', {})
    if not isinstance(packages, dict):
        _err(errors, label, 'ожидается объект', field='audit_packages')
        packages = {}
    for ident, digest in packages.items():
        material = next((m for m in materials if m.get('id') == ident), {})
        if (ident not in material_ids or material.get('storage') != 'local'
                or material.get('immutable') is not True
                or not isinstance(digest, str) or DIGEST.fullmatch(digest) is None):
            _err(errors, label, 'ожидается ID локального immutable материала и SHA-256 provenance', field='audit_packages')
    handoff = meta.get("handoff")
    if handoff is not None:
        _handoff(handoff, label, errors, material_ids)
    acceptance = meta.get("acceptance")
    if acceptance is not None:
        _acceptance(acceptance, label, errors)
    bindings = meta.get("criterion_evidence", {})
    if not isinstance(bindings, dict):
        _err(errors, label, "ожидается объект", field="criterion_evidence")
        bindings = {}
    for ident, binding in bindings.items():
        if not isinstance(ident, str) or re.fullmatch(r"CHECK-[0-9]{2,}", ident) is None or not isinstance(binding, dict):
            _err(errors, label, "некорректная привязка CHECK", field="criterion_evidence")
            continue
        _fields(binding, {"check_sha256", "card_projection", "report_id", "report_sha256"},
                {"check_sha256", "card_projection", "report_id", "report_sha256"}, label, errors)
        for key in ("check_sha256", "card_projection", "report_sha256"):
            if not isinstance(binding.get(key), str) or DIGEST.fullmatch(binding[key]) is None:
                _err(errors, label, f"некорректный {ident}.{key}", field="criterion_evidence")
        if not isinstance(binding.get("report_id"), str) or binding["report_id"] not in material_ids:
            _err(errors, label, f"неизвестный report_id у {ident}", field="criterion_evidence")
    legacy_findings = meta.get("checkbox_legacy_findings", [])
    if (not isinstance(legacy_findings, list) or not all(isinstance(item, str) and re.fullmatch(r"A-[0-9]{2,}", item) for item in legacy_findings)
            or len(legacy_findings) != len(set(legacy_findings))):
        _err(errors, label, "ожидаются уникальные ID прежних находок", field="checkbox_legacy_findings")
    block = None
    if "block" in meta:
        if not block_enabled:
            _err(errors, label, "поле требует явной capability "
                 f"'{block_workflow.CAPABILITY}' в project.json → "
                 "documents.capabilities", field="block")
        else:
            block = block_workflow.validate_card_block(meta["block"],
                                                       label, errors)
    res = {"schema_version": version, "relations": relations, "materials": materials,
           "moves": moves, "handoff": handoff, "acceptance": acceptance,
           "block": block, "has_block": "block" in meta}
    if "checkbox_legacy_findings" in meta:
        res["checkbox_legacy_findings"] = legacy_findings
    if "criterion_evidence" in meta:
        res["criterion_evidence"] = bindings
    if 'audit_packages' in meta:
        res['audit_packages'] = packages
    if "card_format_version" in meta:
        res["card_format_version"] = card_version
    if version == 2:
        res["workflow"] = workflow
        res["verification_plan"] = verification_plan
    return res


def collections_config(root, data, paths, safe_path, require):
    """Раздел documents project.json → {kind: {active, archive, index}} или None."""
    raw = data.get("documents")
    if raw is None:
        return None
    label = "project.json documents"
    require(isinstance(raw, dict), f"{label}: ожидается объект")
    unknown = sorted(set(raw) - {"schema_version", "collections",
                               "capabilities"})
    require(not unknown, f"{label}: неизвестные поля {unknown}")
    require(raw.get("schema_version") == 1,
            f"{label}: неподдерживаемая schema_version "
            f"{raw.get('schema_version')!r}")
    capabilities = raw.get("capabilities")
    if capabilities is not None:
        require(isinstance(capabilities, list)
                and all(isinstance(c, str) for c in capabilities),
                f"{label}.capabilities: ожидается список строк")
        require(len(capabilities) == len(set(capabilities)),
                f"{label}.capabilities: повторное значение")
        unknown_caps = sorted(set(capabilities)
                              - block_workflow.KNOWN_CAPABILITIES)
        require(not unknown_caps,
                f"{label}.capabilities: неизвестные значения "
                f"{unknown_caps}; известные: "
                f"{sorted(block_workflow.KNOWN_CAPABILITIES)}")
    raw_cols = raw.get("collections")
    require(isinstance(raw_cols, dict) and raw_cols,
            f"{label}: collections должен быть непустым объектом")
    cols = {}
    for kind in sorted(raw_cols):
        item_label = f"documents.collections.{kind}"
        require(kind in KINDS, f"{item_label}: неизвестный kind")
        entry = raw_cols[kind]
        require(isinstance(entry, dict), f"{item_label}: ожидается объект")
        unknown = sorted(set(entry) - {"active", "archive", "index"})
        require(not unknown, f"{item_label}: неизвестные поля {unknown}")
        resolved = {field: safe_path(root, entry.get(field))
                    for field in ("active", "archive", "index")}
        for field in ("active", "archive", "index"):
            require(resolved[field].is_relative_to(paths["docs"]),
                    f"{item_label}.{field}: должен быть внутри docs")
        require(resolved["index"].suffix == ".md"
                and resolved["index"].parent == resolved["active"],
                f"{item_label}.index: Markdown-файл в каталоге active")
        cols[kind] = resolved
    placed = [("tasks", paths["tasks"]), ("archive.tasks", paths["archive"])]
    for kind, entry in cols.items():
        placed += [(f"{kind}.active", entry["active"]),
                   (f"{kind}.archive", entry["archive"])]
    for first, (name, path) in enumerate(placed):
        for other_name, other in placed[first + 1:]:
            require(not path.is_relative_to(other) and not other.is_relative_to(path),
                    f"documents: пересечение каталогов {name} и {other_name}")
    indexes = {entry["index"]: kind for kind, entry in cols.items()}
    require(len(indexes) == len(cols), "documents: общий index у нескольких коллекций")
    for kind, entry in cols.items():
        require(entry["index"] != paths["task_index"],
                f"{kind}.index: конфликт с индексом задач")
        for other, other_entry in cols.items():
            if other == kind:
                continue
            require(not entry["index"].is_relative_to(other_entry["active"])
                    and not entry["index"].is_relative_to(other_entry["archive"]),
                    f"{kind}.index: внутри коллекции {other}")
    return cols


def capabilities_of(data):
    """Явно включённые documents.capabilities → frozenset имён."""
    raw = data.get("documents") if isinstance(data, dict) else None
    caps = raw.get("capabilities") if isinstance(raw, dict) else None
    return frozenset(caps) if isinstance(caps, list) else frozenset()


def _cross_check(root, documents, task_meta, records, component_roots,
                 safe_path, errors):
    doc_ids = set(documents)
    for ident, entry in documents.items():
        label = str(entry["path"].relative_to(root))
        for relation in entry["relations"]:
            target = relation["target"]
            if target == ident:
                _err(errors, label, f"{relation['relation']} → сам документ",
                     field="relations")
            elif target not in doc_ids:
                _err(errors, label, f"{relation['relation']} → '{target}': "
                     "нет такого документа", field="relations")
        for task in entry["tasks"]:
            if task not in records:
                _err(errors, label, f"нет карточки '{task}'", field="tasks")
        for change in entry["changes"]:
            for task in change.get("tasks") or []:
                if task not in records:
                    _err(errors, label, f"изменение '{change.get('id')}': "
                         f"нет карточки '{task}'", field="changes")
        archive = entry["archive"]
        if archive is not None:
            successor = archive.get("successor")
            if isinstance(successor, str) and successor not in doc_ids:
                _err(errors, label, f"successor '{successor}': нет такого документа",
                     field="archive")
            if _str(archive.get("original_path")):
                try:
                    safe_path(root, archive["original_path"])
                except ValueError as error:
                    _err(errors, label, str(error), field="archive")
    for ident, meta in task_meta.items():
        label = str(records[ident]["path"].relative_to(root))
        for relation in meta["relations"]:
            if relation["target"] not in doc_ids:
                _err(errors, label, f"{relation['relation']} → "
                     f"'{relation['target']}': нет такого документа",
                     field="relations")
    edges = {ident: [rel["target"] for rel in entry["relations"]
                     if rel["relation"] == "replaces" and rel["target"] in doc_ids]
             for ident, entry in documents.items()}
    visited = set()
    for first in edges:
        pending, active = [(first, False)], set()
        while pending:
            ident, finished = pending.pop()
            if finished:
                active.discard(ident)
                visited.add(ident)
            elif ident not in visited:
                if ident in active:
                    errors.append(f"цикл replaces: {ident}")
                    visited.update(active)
                    break
                active.add(ident)
                pending.append((ident, True))
                pending.extend((dep, False) for dep in reversed(edges[ident]))
    owners = []
    for ident, entry in documents.items():
        label = str(entry["path"].relative_to(root))
        owners += [(ident, label, m) for m in entry["materials"]]
    for ident, meta in task_meta.items():
        label = str(records[ident]["path"].relative_to(root))
        owners += [(ident, label, m) for m in meta["materials"]]
    local, external = {}, {}
    for owner, label, material in owners:
        for consumer in material.get("used_by") or []:
            if consumer not in doc_ids and consumer not in records:
                _err(errors, label, f"материал '{material.get('id')}': "
                     f"used_by '{consumer}' — нет такого владельца/карточки",
                     field="materials")
        if material.get("storage") == "external":
            source = material.get("source")
            if _str(source):
                previous = external.get(source)
                if previous is not None and previous != owner:
                    errors.append(f"{label}: материал '{material.get('id')}': "
                                  f"{source} уже принадлежит {previous}")
                external.setdefault(source, owner)
            continue
        if material.get("storage") != "local":
            continue
        try:
            resolved = safe_path(root, material.get("source"))
        except ValueError as error:
            _err(errors, label, f"материал '{material.get('id')}': {error}",
                 field="materials")
            continue
        material["resolved"] = resolved
        previous = local.get(resolved)
        if previous is not None and previous != owner:
            errors.append(f"{label}: материал '{material.get('id')}': "
                          f"{material['source']} уже принадлежит {previous}")
        local.setdefault(resolved, owner)
        for comp in component_roots:
            if resolved == comp or resolved.is_relative_to(comp) \
                    or comp.is_relative_to(resolved):
                _err(errors, label, f"материал '{material.get('id')}': "
                     f"{material['source']} — код/тесты компонента, не evidence",
                     field="materials")
                break
        if resolved.is_dir():
            for link in _walk_links(resolved):
                _err(errors, label, f"материал '{material.get('id')}': "
                     f"символическая ссылка {link.relative_to(root)}",
                     field="materials")
        if material.get("required") and not resolved.exists():
            _err(errors, label, f"обязательный материал '{material.get('id')}' "
                 f"отсутствует: {material['source']}", field="materials")
    placed = [(owner, label, m, m["resolved"])
              for owner, label, m in owners
              if m.get("storage") == "local" and "resolved" in m]
    for first, (owner, label, material, path) in enumerate(placed):
        for other_owner, _, other_material, other in placed[first + 1:]:
            if other_owner == owner or path == other:
                continue
            if path.is_relative_to(other) or other.is_relative_to(path):
                errors.append(f"{label}: материал '{material.get('id')}' и "
                              f"'{other_material.get('id')}' ({other_owner}): "
                              "пересечение каталогов владельцев")
    moves = {}
    for owner, label, entry_moves in [
            (ident, str(entry["path"].relative_to(root)), entry["moves"])
            for ident, entry in documents.items()] + [
            (ident, str(records[ident]["path"].relative_to(root)), meta["moves"])
            for ident, meta in task_meta.items()]:
        for item in entry_moves:
            try:
                old = safe_path(root, item["from"]).resolve()
                new = safe_path(root, item["to"]).resolve()
            except (ValueError, TypeError) as error:
                _err(errors, label, str(error), field="moves")
                continue
            if old == new:
                _err(errors, label, f"перенос '{item['from']}' в себя",
                     field="moves")
                continue
            if old in moves and moves[old] != new:
                errors.append(f"{label}: перенос '{item['from']}' конфликтует "
                              f"с картой владельца {owner}")
                continue
            moves[old] = new
    policies = {ident: {"policy": entry["acceptance_policy"],
                        "path": str(entry["path"].relative_to(root)),
                        "archived": entry["archived"]}
                for ident, entry in documents.items()
                if entry["has_policy"]}
    blocks = {ident: meta["block"] for ident, meta in task_meta.items()
              if meta is not None and meta.get("has_block")}
    block_workflow.validate_block_graph(policies, blocks, records, errors)
    return moves


def _check_evidence(hint, owner, materials, root, errors, candidates):
    """Полный состав evidence-каталога владельца → ошибки и кандидаты.

    Каталог без зарегистрированных материалов — один кандидат на каталог.
    При частичном учёте каждый неучтённый файл — отдельный кандидат.
    """
    if hint.is_symlink():
        errors.append(f"{hint.relative_to(root)}: символическая ссылка "
                      "вместо каталога evidence")
        return
    if not hint.is_dir():
        return
    owned = [m["resolved"] for m in materials if m.get("resolved") is not None]
    files, links = _walk_files(hint)
    for link in links:
        errors.append(f"{link.relative_to(root)}: символическая ссылка "
                      "в evidence-каталоге")
    touched = any(path == hint or path.is_relative_to(hint)
                  or hint.is_relative_to(path) for path in owned)
    if not touched:
        candidates.append({"path": str(hint.relative_to(root)), "owner": owner,
                           "reason": "каталог evidence без учёта в материалах"})
        return
    for path in files:
        if not any(path == base or path.is_relative_to(base) for base in owned):
            candidates.append({"path": str(path.relative_to(root)),
                               "owner": owner,
                               "reason": "файл evidence без учёта в материалах"})


def load_catalog(root, data, paths, records, safe_path, require,
                 task_overrides=None, repair_tasks=()):
    """Build catalog; task_overrides validates projected metadata without writes.

    Returns None when documents is not configured. Overrides go through the
    same schema, ownership and path checks as saved task metadata.
    repair_tasks exempts only the misplaced-evidence diagnostic for explicit
    lifecycle repair targets; schema, ownership and symlink checks still apply.
    """
    cols = collections_config(root, data, paths, safe_path, require)
    if cols is None:
        return None
    capabilities = capabilities_of(data)
    block_enabled = block_workflow.CAPABILITY in capabilities
    component_roots = []
    for component in data.get("components") or []:
        component_roots.append(safe_path(root, component["code"]))
        component_roots += [safe_path(root, test) for test in component.get("tests", [])]
        if component.get("migrations"):
            component_roots.append(safe_path(root, component["migrations"]))
    errors, legacy, candidates, nested = [], [], [], []
    link_errors = set()
    resolved_root = root.resolve()
    documents, task_meta = {}, {}
    claimed = {entry["index"].resolve() for entry in cols.values()}
    claimed.add(paths["task_index"].resolve())
    task_paths = {rec["path"].resolve() for rec in records.values()}
    for kind in sorted(cols):
        for place in ("active", "archive"):
            base = cols[kind][place]
            if not base.exists():
                continue
            require(base.is_dir(), f"{kind}.{place}: {base.relative_to(root)} "
                                   "не является каталогом")
            files, links = _walk_files(base)
            for link in links:
                rel = str(link.relative_to(root))
                link_errors.add(rel)
                errors.append(f"{rel}: символическая ссылка в коллекции {kind}")
            for path in sorted(files):
                resolved = path.resolve()
                rel = str(path.relative_to(root))
                if not resolved.is_relative_to(resolved_root):
                    errors.append(f"{rel}: выходит за корень проекта")
                    continue
                if resolved in claimed:
                    continue
                claimed.add(resolved)
                if path.parent != base or path.suffix != ".md":
                    # материал или посторонний файл — не документ; разбор ниже
                    nested.append({"path": rel, "resolved": resolved,
                                   "reason": f"вложенный файл в коллекции {kind}"})
                    continue
                text = path.read_text(encoding="utf-8")
                title = _doc_title(text)
                meta = parse_meta(text, rel, errors)
                if meta is None:
                    legacy.append({"kind": kind, "path": path,
                                   "archived": place == "archive",
                                   "title": title})
                    continue
                entry = _validate_document(meta, path, kind, place == "archive",
                                           errors, capabilities=capabilities)
                entry["title"] = title
                ident = entry["id"]
                if ident is None:
                    continue
                if ident in documents:
                    errors.append(f"{rel}: повторный document_id '{ident}'")
                    continue
                folded = ident.casefold()
                twin = next((key for key in documents if key.casefold() == folded), None)
                if twin is not None:
                    errors.append(f"{rel}: document_id '{ident}' совпадает с "
                                  f"'{twin}' без учёта регистра")
                    continue
                documents[ident] = entry
    tw = data.get("task_workflow") if isinstance(data, dict) else None
    registry_map = None
    if isinstance(tw, dict) and isinstance(tw.get("registry"), list):
        registry_map = {item["id"]: item for item in tw["registry"]
                        if isinstance(item, dict) and isinstance(item.get("id"), str)}

    for base in (paths['tasks'], paths['archive']):
        for sidecar in base.glob('*.meta.json'):
            card = sidecar.with_name(sidecar.name[:-10] + '.md')
            if not card.is_file() or card.resolve() not in task_paths:
                errors.append(f'{sidecar.relative_to(root)}: orphan метаданные без карточки')
    for ident, rec in records.items():
        label = str(rec["path"].relative_to(root))
        meta = parse_meta(read_text(rec["path"]), label, errors)
        if task_overrides and ident in task_overrides:
            meta = task_overrides[ident]
        if meta is not None:
            task_meta[ident] = _validate_task_meta(
                meta, label, errors,
                registry=registry_map,
                task_workflow_enabled=(tw is not None),
                root=root,
                safe_path=safe_path,
                block_enabled=block_enabled
            )
    stray_files, stray_links = _walk_files(paths["docs"])
    for link in stray_links:
        rel = str(link.relative_to(root))
        if rel not in link_errors:
            link_errors.add(rel)
            errors.append(f"{rel}: символическая ссылка")
    for path in sorted(p for p in stray_files if p.suffix == ".md"):
        resolved = path.resolve()
        if not resolved.is_relative_to(resolved_root):
            errors.append(f"{path.relative_to(root)}: выходит за корень проекта")
            continue
        if resolved in claimed or resolved in task_paths:
            continue
        if any(part.endswith("-evidence") for part in path.parts):
            continue
        rel = str(path.relative_to(root))
        stray = []
        meta = parse_meta(path.read_text(encoding="utf-8"), rel, stray)
        if meta is not None or stray:
            errors.append(f"{rel}: блок метаданных вне зарегистрированной "
                          "коллекции или карточки")
    moves = _cross_check(root, documents, task_meta, records, component_roots,
                         safe_path, errors)
    owners = {ident: entry["materials"] for ident, entry in documents.items()}
    owners.update({ident: meta["materials"] for ident, meta in task_meta.items()})
    evidence_dirs = set()
    for ident, rec in records.items():
        for base in (paths["tasks"], paths["archive"]):
            hint = base / f"{ident}-evidence"
            evidence_dirs.add(hint)
            _check_evidence(hint, ident, owners.get(ident, []), root,
                            errors, candidates)
            if (base != rec["path"].parent
                    and (hint.exists() or hint.is_symlink())
                    and ident not in repair_tasks):
                errors.append(f"{ident}: материалы на противоположной стороне "
                              f"архива: {hint.relative_to(root)}; "
                              "требуется явное восстановление материалов")
    for ident, entry in documents.items():
        hint = entry["path"].parent / f"{entry['path'].stem}-evidence"
        evidence_dirs.add(hint)
        _check_evidence(hint, ident, owners.get(ident, []), root,
                        errors, candidates)
    require(not errors, "\n".join(errors))
    registered = set()
    for entry in documents.values():
        registered.update(m["resolved"] for m in entry["materials"]
                          if m.get("resolved") is not None)
    for meta in task_meta.values():
        registered.update(m["resolved"] for m in meta["materials"]
                          if m.get("resolved") is not None)
    evidence_resolved = {hint.resolve() for hint in evidence_dirs}
    for item in nested:
        covered = any(item["resolved"] == path
                      or item["resolved"].is_relative_to(path)
                      for path in registered)
        inside_evidence = any(item["resolved"].is_relative_to(hint)
                              for hint in evidence_resolved)
        if not covered and not inside_evidence:
            candidates.append({"path": item["path"], "owner": None,
                               "reason": item["reason"]})
    incoming = {ident: [] for ident in documents}
    for ident, entry in documents.items():
        for relation in entry["relations"]:
            incoming[relation["target"]].append(
                {"relation": relation["relation"], "source": ident})
    for ident, meta in task_meta.items():
        for relation in meta["relations"]:
            if relation["target"] in incoming:
                incoming[relation["target"]].append(
                    {"relation": relation["relation"], "source": ident})
    for ident, rec in records.items():
        staged = root / "work" / "verification" / ident
        if staged.is_dir():
            owned = owners.get(ident, [])
            covered = [m["resolved"] for m in owned
                       if m.get("resolved") is not None]
            for child in sorted(staged.iterdir()):
                if any(child.resolve() == base
                       or child.resolve().is_relative_to(base)
                       for base in covered):
                    continue
                candidates.append({"path": str(child.relative_to(root)),
                                   "owner": ident,
                                   "reason": "прогон verify без регистрации "
                                             "в материалах"})
    immutable = sorted({material["resolved"]
                        for owner_materials in owners.values()
                        for material in owner_materials
                        if material.get("storage") == "local"
                        and material.get("immutable")
                        and material.get("resolved") is not None})
    return {"documents": documents, "legacy": legacy, "task_meta": task_meta,
            "owners": owners, "incoming": incoming, "candidates": candidates,
            "collections": cols, "records": records, "root": root,
            "moves": moves, "immutable": immutable,
            "capabilities": capabilities}


INDEX_KIND_TITLES = {"discussion": "обсуждений", "spec": "спецификаций",
                     "design": "design-документов", "adr": "ADR",
                     "plan": "планов"}
_EPOCH = dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def _escape_cell(value):
    return str(value).replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")


def _index_link(root, index_dir, path):
    relative = Path(os.path.relpath(str(path), str(index_dir))).as_posix()
    return document_links.format_dest(relative, "", "",
                                      any(char.isspace() for char in relative))


def _index_date(value):
    parsed = _iso(value)
    return parsed.strftime("%Y-%m-%d") if parsed is not None else "—"


def index_region(catalog, kind):
    """Генерируемая область индекса коллекции (без маркеров playbook-index)."""
    root, records = catalog["root"], catalog.get("records") or {}
    index_dir = catalog["collections"][kind]["index"].parent
    entries = [entry for entry in catalog["documents"].values()
               if entry["kind"] == kind]
    active = sorted((e for e in entries if not e["archived"]),
                    key=lambda e: e["id"])
    archived = sorted((e for e in entries if e["archived"]),
                      key=lambda e: e["id"])
    archived.sort(key=lambda e: _iso((e["archive"] or {}).get("at")) or _EPOCH,
                  reverse=True)
    legacy_active = sorted((item for item in catalog["legacy"]
                            if item["kind"] == kind and not item["archived"]),
                           key=lambda item: str(item["path"]))
    legacy_archived = sorted((item for item in catalog["legacy"]
                              if item["kind"] == kind and item["archived"]),
                             key=lambda item: str(item["path"]))
    is_plan = kind == "plan"

    def task_cells(entry):
        names = []
        for task in entry["tasks"]:
            record = records.get(task)
            names.append(task if record is None else
                         f"[{task}]({_index_link(root, index_dir, record['path'])})")
        return ", ".join(names) or "—"

    lines = ["## Актуальные", ""]
    header = "| Документ | Состояние | Обновлён |"
    divider = "|---|---|---|"
    if is_plan:
        header += " Задачи |"
        divider += "---|"
    rows = []
    for entry in active:
        cells = [entry["state"] or "—", _index_date(entry["updated_at"])]
        if is_plan:
            cells.append(task_cells(entry))
        title = _escape_cell(entry["title"] or entry["path"].stem)
        rows.append(f"| [{title}]({_index_link(root, index_dir, entry['path'])}) "
                    f"| {' | '.join(cells)} |")
    for item in legacy_active:
        cells = ["legacy", "—"] + (["—"] if is_plan else [])
        title = _escape_cell(item["title"] or item["path"].stem)
        rows.append(f"| [{title}]({_index_link(root, index_dir, item['path'])}) "
                    f"| {' | '.join(cells)} |")
    lines += [header, divider, *rows] if rows else \
        ["Актуальных документов пока нет."]
    lines += ["", "## Архив", ""]
    header = "| Документ | Состояние | Архивирован | Причина | Преемник |"
    divider = "|---|---|---|---|---|"
    if is_plan:
        header += " Задачи |"
        divider += "---|"
    rows = []
    for entry in archived:
        archive = entry["archive"] or {}
        cells = [entry["state"] or "—", _index_date(archive.get("at")),
                 _escape_cell(archive.get("reason") or "—"),
                 archive.get("successor") or "—"]
        if is_plan:
            cells.append(task_cells(entry))
        title = _escape_cell(entry["title"] or entry["path"].stem)
        rows.append(f"| [{title}]({_index_link(root, index_dir, entry['path'])}) "
                    f"| {' | '.join(cells)} |")
    for item in legacy_archived:
        cells = ["legacy", "—", "—", "—"] + (["—"] if is_plan else [])
        title = _escape_cell(item["title"] or item["path"].stem)
        rows.append(f"| [{title}]({_index_link(root, index_dir, item['path'])}) "
                    f"| {' | '.join(cells)} |")
    lines += [header, divider, *rows] if rows else \
        ["Архивных документов пока нет."]
    return "\n".join(lines)


def splice_index(text, region):
    """Подставить область генерации между маркерами; ручное сохраняется.

    Возвращает новый текст или None, если выделенной области нет либо
    маркеры повреждены.
    """
    positions = document_links.marker_span(text, document_links.INDEX_START,
                                           document_links.INDEX_END)
    if not isinstance(positions, tuple):
        return None
    start, end = positions
    lines = text.split("\n")
    return "\n".join(lines[:start + 1] + region.split("\n") + lines[end:])


def render_index_file(catalog, kind):
    """Полный текст нового файла индекса коллекции."""
    title = INDEX_KIND_TITLES.get(kind, kind)
    return (f"# Индекс {title}\n\n{document_links.INDEX_START}\n"
            f"{index_region(catalog, kind)}\n{document_links.INDEX_END}\n")


def check_indexes(catalog):
    """Сверка файлов индексов коллекций с ожидаемой генерацией → ошибки."""
    errors = []
    root = catalog["root"]
    for kind, entry in sorted(catalog["collections"].items()):
        index = entry["index"]
        label = str(index.relative_to(root))
        if not index.is_file():
            errors.append(f"{label}: нет файла индекса коллекции {kind}")
            continue
        text = index.read_text(encoding="utf-8")
        expected = splice_index(text, index_region(catalog, kind))
        if expected is None:
            errors.append(f"{label}: нет выделенной области "
                          f"{document_links.INDEX_START} / "
                          f"{document_links.INDEX_END}")
        elif expected != text:
            errors.append(f"{label}: индекс устарел")
    return errors


def _rel(root, path):
    return str(path.relative_to(root))


def list_text(catalog):
    lines = ["# Каталог документов"]
    documents, legacy = catalog["documents"], catalog["legacy"]
    for kind in KINDS:
        group = [d for d in documents.values() if d["kind"] == kind]
        old = [item for item in legacy if item["kind"] == kind]
        if not group and not old and kind not in catalog["collections"]:
            continue
        lines.append(f"\n## {kind}")
        for doc in sorted(group, key=lambda d: (d["archived"], d["id"])):
            place = "архив" if doc["archived"] else "активен"
            lines.append(f"- {doc['id']} [{doc['state']}, {place}] "
                         f"{_rel(catalog['root'], doc['path'])}")
        for item in sorted(old, key=lambda i: str(i["path"])):
            place = "архив" if item["archived"] else "активен"
            lines.append(f"- (legacy, {place}) {_rel(catalog['root'], item['path'])}")
    if catalog["candidates"]:
        lines.append("\n## Кандидаты на учёт")
        for item in catalog["candidates"]:
            owner = item["owner"] or "—"
            lines.append(f"- {item['path']} (владелец: {owner}; {item['reason']})")
    return "\n".join(lines)


def impact_text(catalog, ident, require):
    documents = catalog["documents"]
    require(ident in documents, f"Нет документа: {ident}")
    entry = documents[ident]
    root = catalog["root"]
    lines = [f"{ident} — {entry['kind']}, состояние {entry['state']}, "
             f"ревизия {entry['revision']}",
             f"Путь: {_rel(root, entry['path'])}"
             f" ({'архив' if entry['archived'] else 'активен'})",
             f"Обновлён: {entry['updated_at']}"]
    if entry["archive"] is not None:
        archive = entry["archive"]
        successor = archive.get("successor") or "—"
        lines.append(f"Архивирован: {archive.get('at')}; причина: "
                     f"{archive.get('reason')}; преемник: {successor}")
    lines.append("Исходящие связи:")
    if entry["relations"]:
        for relation in entry["relations"]:
            target = relation["target"]
            note = ""
            if target in documents:
                other = documents[target]
                note = f" [{other['state']}, {_rel(root, other['path'])}]"
            lines.append(f"  {relation['relation']} → {target}{note}")
    else:
        lines.append("  —")
    lines.append("Обратные связи:")
    incoming = catalog["incoming"].get(ident, [])
    if incoming:
        for relation in sorted(incoming, key=lambda r: (r["source"], r["relation"])):
            lines.append(f"  {relation['source']} → {relation['relation']}")
    else:
        lines.append("  —")
    lines.append("Связанные задачи: "
                 + (", ".join(entry["tasks"]) if entry["tasks"] else "—"))
    lines.append(f"Действующая ревизия: {entry['revision']}; "
                 f"запланированные изменения: {len(entry['changes'])}")
    for change in entry["changes"]:
        acceptance = change.get("acceptance") or "—"
        lines.append(f"  {change.get('id')} [{change.get('state')}] "
                     f"раздел «{change.get('section')}», приёмка: {acceptance}")
    materials = catalog["owners"].get(ident, [])
    lines.append(f"Материалы: {len(materials)}")
    for material in materials:
        lines.append("  " + _material_line(root, material))
    affected = sorted({r["source"] for r in incoming})
    lines.append("Потенциально затронутые при изменении: "
                 + (", ".join(affected) if affected else "—"))
    return "\n".join(lines)


def _material_line(root, material):
    flags = [material.get("role"), material.get("storage"),
             "required" if material.get("required") else "optional",
             "immutable" if material.get("immutable") else "mutable"]
    line = f"{material.get('id')} [{', '.join(str(f) for f in flags)}] {material.get('source')}"
    if material.get("storage") == "local":
        resolved = material.get("resolved")
        if resolved is not None:
            line += " — на месте" if resolved.exists() else " — ОТСУТСТВУЕТ"
    consumers = material.get("used_by") or []
    if consumers:
        line += f"; потребители: {', '.join(consumers)}"
    return line


def materials_text(catalog, owner, require):
    documents, records = catalog["documents"], catalog["records"]
    require(owner in documents or owner in records,
            f"Нет владельца (документ или карточка): {owner}")
    root = catalog["root"]
    if owner in documents:
        entry = documents[owner]
        title = f"документ {owner} [{entry['state']}]"
    else:
        title = f"карточка {owner} [{records[owner]['status']}]"
    lines = [f"Владелец: {title}"]
    materials = catalog["owners"].get(owner, [])
    if not materials:
        lines.append("Материалы не зарегистрированы.")
    for material in materials:
        lines.append("- " + _material_line(root, material))
        if material.get("storage") == "local" and "resolved" in material:
            lines.append(f"  путь: {_rel(root, material['resolved'])}")
        if material.get("storage") == "external":
            lines.append(f"  доступ: {material.get('access')}")
    found = [item for item in catalog["candidates"] if item["owner"] == owner]
    if found:
        lines.append("Кандидаты на учёт:")
        for item in found:
            lines.append(f"- {item['path']} ({item['reason']})")
    return "\n".join(lines)
