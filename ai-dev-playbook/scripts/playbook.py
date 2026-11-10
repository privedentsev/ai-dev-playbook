#!/usr/bin/env python3
"""Portable playbook tools. Python 3.10+, standard library only."""

import argparse
import datetime as dt
import difflib
import json
import os
import re
import sys
import unicodedata
import uuid
from pathlib import Path


# Support both direct CLI execution and importlib-based tests.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import document_links
import documents
import file_operations
import lifecycle
import updating
import verification
import task_cards
import project_view
import block_workflow
import checks_contract
import checks_graph
import checks_inventory
import checks_runner
import checks_select


CORE = "ai-dev-playbook"
STATUSES = {"draft", "ready", "in_progress", "in_review", "done", "blocked", "cancelled"}
AVAILABILITY = {"reported", "confirmed", "unavailable", "unknown"}
ID_PATTERN = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-[0-9]+\Z")
META = task_cards.HEADER
STAMP_UNKNOWN = "unknown"
STAMP_UNSET = "-"
EPOCH = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
MODEL_UNCONFIRMED = "(не подтверждено)"
FENCE = document_links.FENCE
INLINE_CODE = document_links.INLINE_CODE
DOC_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
INDEX_HEADER = ("# Индекс задач\n\nСгенерировано playbook.py; "
                "статусы изменять в карточках.\n")
INITIAL_STATE = {"discussion": "open"}


def timestamp(value, ident, field):
    if value in {STAMP_UNSET, STAMP_UNKNOWN}:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    require(parsed is not None and parsed.tzinfo is not None,
            f"{ident}: {field} должен быть ISO-временем с часовым поясом, "
            f"'{STAMP_UNSET}' или '{STAMP_UNKNOWN}'")
    return parsed


def require(condition, message):
    if not condition:
        raise ValueError(message)


def safe_path(root, value):
    require(isinstance(value, str) and value.strip(), "Путь должен быть непустой строкой")
    path = Path(value)
    require(not path.is_absolute() and ".." not in path.parts and path.parts,
            f"Небезопасный относительный путь: {value}")
    candidate = root / path
    current = root
    for part in path.parts:
        current = current / part
        require(not current.is_symlink(), f"Символическая ссылка в управляемом пути: {value}")
    require(candidate.resolve().is_relative_to(root.resolve()), f"Выход за корень: {value}")
    return candidate


def read_json(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(data, dict), f"Ожидается JSON-объект: {path}")
    return data


def config(root):
    data = read_json(safe_path(root, f"{CORE}/project.json"))
    require(data.get("schema_version") in {1, 2}, "Неподдерживаемая схема project.json")
    if data["schema_version"] == 2:
        require(isinstance(data.get("task_workflow"), dict)
                and data["task_workflow"].get("schema_version") == 2,
                "project.json schema 2 требует task_workflow v2")
    require(isinstance(data.get("name"), str) and data["name"].strip(), "Нет имени проекта")
    require(data.get("setup_status") in {"needs_review", "configured"}, "Некорректный setup_status")
    require(data.get("playbook_version") == safe_path(root, f"{CORE}/VERSION").read_text().strip(),
            "Версия внедрения отличается от VERSION; завершите ручное обновление")
    manifest = updating.read_manifest(root, safe_path, require)
    require(manifest["playbook_version"] == data["playbook_version"],
            "manifest: playbook_version не совпадает с project.json")
    paths = data.get("paths")
    require(isinstance(paths, dict), "Нет paths")
    resolved = {key: safe_path(root, paths.get(key))
                for key in ("docs", "tasks", "archive", "task_index")}
    for key in ("tasks", "archive", "task_index"):
        require(resolved[key] != resolved["docs"] and resolved[key].is_relative_to(resolved["docs"]),
                f"{key} должен находиться внутри docs")
    require(not resolved["tasks"].is_relative_to(resolved["archive"])
            and not resolved["archive"].is_relative_to(resolved["tasks"]),
            "Каталоги активных задач и архива пересекаются")
    require(resolved["task_index"].parent == resolved["tasks"]
            and resolved["task_index"].suffix == ".md", "Индекс должен быть Markdown в каталоге задач")
    require(ID_PATTERN.fullmatch(resolved["task_index"].stem) is None,
            "Путь индекса конфликтует с ID карточки")
    for key in ("docs", "tasks", "archive"):
        require(not resolved[key].exists() or resolved[key].is_dir(), f"{key} должен быть каталогом")
    git = data.get("git", {})
    require(isinstance(git, dict), "Некорректные разрешения Git")
    for action in ("commit", "push", "merge", "release"):
        allowed = {"explicit_request", "after_checks"} if action == "commit" else {"explicit_request"}
        require(git.get(action) in allowed, f"Некорректное разрешение Git: {action}")
    require(isinstance(data.get("components"), list), "components должен быть списком")
    names = set()
    for component in data["components"]:
        require(isinstance(component, dict), "Компонент должен быть объектом")
        name = component.get("name")
        require(isinstance(name, str) and name and name not in names, "Нет имени компонента или оно повторяется")
        names.add(name)
        safe_path(root, component.get("code"))
        require(isinstance(component.get("tests"), list), "tests должен быть списком путей")
        for path in component["tests"]:
            safe_path(root, path)
        if component.get("migrations") is not None:
            safe_path(root, component["migrations"])
    commands = data.get("commands")
    require(isinstance(commands, dict), "Нет команд проекта")
    require(all(value is None or isinstance(value, str) for value in commands.values()),
            "Команда должна быть строкой или null")
    if "verification" in data:
        verification.validate(root, data["verification"], safe_path, require)
    if checks_contract.PROJECT_LINK_FIELD in data:
        link = data[checks_contract.PROJECT_LINK_FIELD]
        require(isinstance(link, str),
                f"{checks_contract.PROJECT_LINK_FIELD} должен быть "
                f"относительным путём к документу политики")
        policy_path = safe_path(root, link)
        require(policy_path.is_file(),
                f"{checks_contract.PROJECT_LINK_FIELD}: нет файла {link}")
        require(not policy_path.is_symlink(),
                f"{checks_contract.PROJECT_LINK_FIELD}: символическая ссылка")
        policy_data = json.loads(policy_path.read_text(encoding="utf-8"),
                                 object_pairs_hook=documents._unique_pairs)
        checks_contract.validate_policy(policy_data, require)
    if "task_workflow" in data:
        verification.validate_task_workflow(root, data["task_workflow"], safe_path, require)
    if "documents" in data:
        documents.collections_config(root, data, resolved, safe_path, require)
    return data, resolved


def resources(root):
    data = read_json(safe_path(root, f"{CORE}/resources.json"))
    require(data.get("schema_version") == 1, "Неподдерживаемая схема ресурсов")
    require(type(data.get("policy_revision")) is int and data["policy_revision"] > 0,
            "policy_revision должен быть положительным целым числом")
    if data.get("updated_at") is not None:
        dt.date.fromisoformat(data["updated_at"])
    require(data.get("on_unavailable") == "ask_engineer", "Неизвестная политика недоступности")
    require(isinstance(data.get("tools"), list), "tools должен быть списком")
    tools = set()
    for tool in data["tools"]:
        require(isinstance(tool, dict), "Инструмент должен быть объектом")
        ident = tool.get("id")
        require(isinstance(ident, str) and ident and ident not in tools, "ID инструментов должны быть уникальны")
        tools.add(ident)
        require(tool.get("availability") in AVAILABILITY, f"Неизвестная доступность: {ident}")
        require(isinstance(tool.get("models"), list), "models должен быть списком")
        models = set()
        for model in tool["models"]:
            require(isinstance(model, dict), "Модель должна быть объектом")
            model_id = model.get("id")
            require(isinstance(model_id, str) and model_id and model_id not in models,
                    "ID моделей должны быть уникальны внутри инструмента")
            models.add(model_id)
            require(model.get("availability") in AVAILABILITY, f"Неизвестная доступность модели {model_id}")
            for field in ("roles", "fallbacks"):
                require(isinstance(model.get(field), list)
                        and all(isinstance(v, str) and v for v in model[field]), f"Некорректный {field}")
        for model in tool["models"]:
            require(all(value in models and value != model["id"] for value in model["fallbacks"]),
                    f"Неизвестная или собственная модель в fallbacks: {model['id']}")
    budgets = data.get("budgets")
    require(isinstance(budgets, dict), "Нет budgets")
    for key in ("per_task", "verification_reserve"):
        require(key in budgets, f"Нет budgets.{key}")
        budget = budgets[key]
        if budget is not None:
            require(isinstance(budget, dict), f"Некорректный бюджет {key}")
            amount = budget.get("amount")
            require(type(amount) in (int, float) and amount >= 0, f"Некорректная сумма {key}")
            require(budget.get("unit") in {"tokens", "USD", "minutes"}, "Неизвестная единица бюджета")
            require(isinstance(budget.get("scope"), str) and budget["scope"], "Нет scope бюджета")
    require(isinstance(data.get("quota_snapshots"), list), "quota_snapshots должен быть списком")
    for quota in data["quota_snapshots"]:
        require(isinstance(quota, dict) and quota.get("tool") in tools, "Неизвестный инструмент квоты")
        require(type(quota.get("remaining")) in (int, float) and quota["remaining"] >= 0,
                "Некорректный остаток квоты")
        for field in ("unit", "source"):
            require(isinstance(quota.get(field), str) and quota[field], f"Нет {field} квоты")
        times = []
        for field in ("checked_at", "expires_at"):
            value = quota.get(field)
            require(isinstance(value, str), f"Нет {field} квоты")
            timestamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            require(timestamp.tzinfo is not None, "Время квоты требует часовой пояс")
            times.append(timestamp)
        require(times[1] >= times[0], "Квота истекает раньше проверки")
    return data


def read_tasks(root, paths):
    records = {}
    for key in ("tasks", "archive"):
        for path in sorted(paths[key].glob("*.md")):
            if path == paths["task_index"]:
                continue
            safe_path(root, str(path.relative_to(root)))
            content = documents.read_text(path)
            match = META.match(content)
            require(match is not None, f"Некорректная шапка карточки: {path.name}")
            title, ident, status, priority, dependencies, executor, model, created, closed = \
                match.groups()
            require(ID_PATTERN.fullmatch(ident) is not None and path.stem == ident,
                    f"ID должен совпадать с именем файла: {path.name}")
            require(ident not in records, f"Повторный ID задачи: {ident}")
            require(status in STATUSES, f"Неизвестный статус: {ident}: {status}")
            require(priority in {"P0", "P1", "P2", "P3"}, f"Неизвестный приоритет: {ident}")
            require(key != "archive" or status in {"done", "cancelled"}, f"Незакрытая задача в архиве: {ident}")
            require(key != "tasks" or status != "done", f"Завершённая задача вне архива: {ident}")
            deps = [] if dependencies == "-" else [item.strip() for item in dependencies.split(",")]
            require(len(deps) == len(set(deps)), f"Повторная зависимость: {ident}")
            normalize = lambda value: value.strip() if value and value.strip() != "-" else None
            created_v, closed_v = created.strip(), closed.strip()
            require(created_v != STAMP_UNSET,
                    f"{ident}: Created обязателен (ISO-время или '{STAMP_UNKNOWN}')")
            created_ts = timestamp(created_v, ident, "Created")
            closed_ts = timestamp(closed_v, ident, "Closed")
            if status in {"done", "cancelled"}:
                require(closed_v != STAMP_UNSET, f"{ident}: закрытая карточка без Closed")
            else:
                require(closed_v == STAMP_UNSET,
                        f"{ident}: активная карточка с Closed: {closed_v}")
            require(created_ts is None or closed_ts is None or closed_ts >= created_ts,
                    f"{ident}: Closed раньше Created")
            route = None
            meta_errors = []
            meta = documents.parse_meta(content, str(path.relative_to(root)), meta_errors)
            if meta is not None:
                workflow = meta.get("workflow")
                if workflow is not None:
                    require(isinstance(workflow, dict), f"{ident}: workflow должен быть объектом")
                    route = workflow.get("route")
                    require(route in documents.ROUTES, f"{ident}: неизвестный route {route!r}")
            if meta is not None and "card_format_version" in meta and task_cards.version(content) == 2:
                task_cards.parse(content, str(path.relative_to(root)))
            records[ident] = dict(id=ident, title=title, status=status, priority=priority,
                                  deps=deps, executor=normalize(executor), model=normalize(model),
                                  created=created_v, closed=closed_v,
                                  created_ts=created_ts, closed_ts=closed_ts,
                                  path=path, archived=key == "archive",
                                  route=route)
    for ident, task in records.items():
        for dependency in task["deps"]:
            require(dependency in records, f"{ident}: отсутствует зависимость {dependency}")
    # Iterative DFS keeps validation usable when the task history grows.
    visited = set()
    for first in records:
        pending = [(first, False)]
        active = set()
        while pending:
            ident, finished = pending.pop()
            if finished:
                active.remove(ident)
                visited.add(ident)
            elif ident not in visited:
                require(ident not in active, f"Цикл зависимостей: {ident}")
                active.add(ident)
                pending.append((ident, True))
                pending.extend((dep, False) for dep in reversed(records[ident]["deps"]))
    for ident, task in records.items():
        if task["status"] in {"ready", "in_progress", "in_review", "done"}:
            require(all(records[dep]["status"] == "done" for dep in task["deps"]),
                    f"{ident}: зависимость ещё не завершена")
    return records


def render_index(records, index_dir=None):
    def link(task):
        if index_dir is None:
            return task["path"].name
        return Path(os.path.relpath(str(task["path"]), str(index_dir))).as_posix()

    def cell(raw, parsed):
        if parsed is not None:
            return parsed.strftime("%Y-%m-%d %H:%M")
        return "—" if raw == STAMP_UNSET else raw

    def row(task):
        escape = lambda value: value.replace("|", "\\|")
        title = escape(task["title"]).replace("[", "\\[").replace("]", "\\]")
        deps = ", ".join(task["deps"]) or "—"
        who = "—"
        if task["executor"]:
            who = escape(task["executor"])
            if task["model"]:
                who += f" / {escape(task['model'])}"
        return (f"| {task['id']} | [{title}]({link(task)}) | {task['status']} "
                f"| {task['priority']} | {who} | {cell(task['created'], task['created_ts'])} "
                f"| {cell(task['closed'], task['closed_ts'])} | {deps} |")

    header = ["| ID | Задача | Статус | Приоритет | Исполнитель | Создана | Закрыта | Зависимости |",
              "|---|---|---|---|---|---|---|---|"]
    lines = ["# Индекс задач", "", "Сгенерировано playbook.py; статусы изменять в карточках.", "",
             "## Активные", "", *header]
    active = sorted((t for t in records.values() if not t["archived"]),
                    key=lambda t: (t["priority"], t["id"]))
    lines += [row(task) for task in active] or ["", "Активных карточек пока нет."]
    # Архивные — свежие по Closed первыми; unknown в конце по ID убыванию внутри префикса.
    archived = sorted((t for t in records.values() if t["archived"]),
                      key=lambda t: (t["id"].rsplit("-", 1)[0],
                                     -int(t["id"].rsplit("-", 1)[1])))
    archived.sort(key=lambda t: t["closed_ts"] or EPOCH, reverse=True)
    lines += ["", "## Архив", "", *header]
    lines += [row(task) for task in archived] or ["", "Архивных карточек пока нет."]
    return "\n".join(lines) + "\n"


def write_index(root, paths):
    records = read_tasks(root, paths)
    index = paths["task_index"]
    if index.exists():
        require(index.read_text(encoding="utf-8").startswith(INDEX_HEADER),
                "Конфликт: индекс указывает на чужой документ; запись отменена")
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(render_index(records, index.parent), encoding="utf-8")


def relocate_links(text, old_path, new_path):
    """Пересчитать локальные ссылки текста при переносе документа old_path → new_path."""
    old_resolved, new_resolved = old_path.resolve(), new_path.resolve()
    return document_links.rewrite(text, old_resolved, new_resolved,
                                  {old_resolved: new_resolved})["text"]


def codeless_lines(text):
    """(номер, строка без инлайн-кода) для строк вне fenced-блоков."""
    result, fence = [], None
    for number, line in enumerate(text.splitlines(), 1):
        if fence is not None:
            if document_links.closes_fence(line, fence):
                fence = None
            continue
        match = FENCE.match(line)
        if match:
            fence = match.group(1)
            continue
        result.append((number, INLINE_CODE.sub("", line)))
    return result


def unconfirmed_lines(text):
    """Строки с маркером неподтверждённой модели вне код-разметки."""
    return [number for number, line in codeless_lines(text) if MODEL_UNCONFIRMED in line]


def retarget_links(root, paths, old_path, new_path):
    """Входящие ссылки на old_path переписать на new_path. Возвращает (исходники, замены)."""
    docs = list(paths["docs"].rglob("*.md"))
    docs += list((root / CORE / "rules").glob("*.md"))
    docs += [root / "AGENTS.md", root / CORE / "README.md"]
    if (root / "README.md").exists():
        docs.append(root / "README.md")
    moves = {old_path.resolve(): new_path.resolve()}
    originals, updates = {}, {}
    for doc in docs:
        if doc in {old_path, new_path, paths["task_index"]}:
            continue
        text = doc.read_text(encoding="utf-8")
        rewritten = document_links.rewrite(text, doc.resolve(), doc.resolve(),
                                           moves)["text"]
        if rewritten != text:
            originals[doc], updates[doc] = text, rewritten
    return originals, updates


def _rel(root, path):
    base = root if hasattr(path, "view") else file_operations.scoped_operations.physical_root(root)
    return path.relative_to(base).as_posix()


def _actor():
    user = os.environ.get("USER") or os.environ.get("USERNAME")
    return f"cli:{user}" if user else "cli"


def _templates_dir():
    return Path(__file__).resolve().parents[1] / "templates" / "documents"


def _document_text(title, meta, body):
    raw = json.dumps(meta, ensure_ascii=False, indent=2)
    return (f"# {title}\n\n{documents.META_START}\n```json\n{raw}\n```\n"
            f"{documents.META_END}\n\n{body}")


def _insert_meta(text, meta):
    """Блок метаданных после первого заголовка либо в начало; тело не меняется."""
    raw = json.dumps(meta, ensure_ascii=False, indent=2)
    block = [documents.META_START, "```json", *raw.splitlines(), "```",
             documents.META_END]
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.strip() and re.match(r"^ {0,3}#\s", line):
            lines[index + 1:index + 1] = ["", *block]
            break
    else:
        lines = [*block, "", *lines]
    return "\n".join(lines)


def _metadata_arg(root, value):
    """--metadata: инлайн-JSON либо относительный путь к JSON-файлу."""
    raw = value.strip()
    if not raw.startswith("{"):
        path = safe_path(root, raw)
        require(path.is_file() and not path.is_symlink(),
                f"Нет файла метаданных: {value}")
        raw = path.read_text(encoding="utf-8")
    try:
        meta = json.loads(raw, object_pairs_hook=documents._unique_pairs)
    except ValueError as error:
        require(False, f"--metadata: {error}")
    require(isinstance(meta, dict), "--metadata: ожидается JSON-объект")
    return meta


def _collection_index_write(root, catalog, kind):
    """(rel, bytes) обновления индекса коллекции; None — уже актуален.

    Существующий файл обновляется только внутри области playbook-index;
    файл без маркеров не перезаписывается, отсутствующий — создаётся.
    """
    index = catalog["collections"][kind]["index"]
    rel = _rel(root, index)
    region = documents.index_region(catalog, kind)
    if index.exists() or index.is_symlink():
        require(index.is_file() and not index.is_symlink(),
                f"{rel}: не обычный файл — индекс не перезаписывается")
        text = index.read_text(encoding="utf-8")
        merged = documents.splice_index(text, region)
        require(merged is not None,
                f"{rel}: нет области {document_links.INDEX_START} / "
                f"{document_links.INDEX_END} — ручной файл не изменяется; "
                "добавьте маркеры вручную")
        if merged == text:
            return None
        return rel, merged.encode("utf-8")
    return rel, documents.render_index_file(catalog, kind).encode("utf-8")


def _catalog_consistency(staged_root, label, kinds):
    """Каталог валиден, а записанные индексы kinds соответствуют генерации.

    Затронутые операцией индексы сверяются заново вычисленными областями —
    так ловится любое изменение входов коллекции после построения плана.
    Индексы прочих коллекций не проверяются: их состояние — не дело этой
    операции (их рассогласование остаётся предметом check).
    """
    data, paths = config(staged_root)
    records = read_tasks(staged_root, paths)
    catalog = documents.load_catalog(staged_root, data, paths, records,
                                     safe_path, require)
    require(catalog is not None, f"{label}: каталог документов недоступен")
    errors = []
    for kind in kinds:
        index = catalog["collections"][kind]["index"]
        rel = _rel(staged_root, index)
        if not index.is_file():
            errors.append(f"{rel}: нет файла индекса")
            continue
        text = index.read_text(encoding="utf-8")
        expected = documents.splice_index(text, documents.index_region(catalog, kind))
        if expected is None or expected != text:
            errors.append(f"{rel}: индекс не соответствует каталогу")
    require(not errors, f"{label}: " + "; ".join(errors))


def _task_index_fresh(staged_root, label):
    """Индекс задач в проверяемом корне соответствует карточкам."""
    _, paths = config(staged_root)
    records = read_tasks(staged_root, paths)
    index = paths["task_index"]
    require(index.is_file()
            and index.read_text(encoding="utf-8")
            == render_index(records, index.parent),
            f"{label}: индекс задач не соответствует карточкам")


def _write_spec(root, operation, seed, writes, checks):
    """Спецификация file_operations: входы, детерминированный plan_id."""
    inputs = file_operations.snapshot_paths(root, sorted(writes),
                                            safe_path, require)
    digest = file_operations.sha256(json.dumps(
        {rel: file_operations.sha256(content)
         for rel, content in sorted(writes.items())},
        sort_keys=True).encode("utf-8")).split(":", 1)[1][:12]
    plan_id = f"{seed}-{digest}"
    require(file_operations.ID_RE.fullmatch(plan_id) is not None,
            f"Некорректный plan_id: {plan_id}")
    return {"operation": operation, "plan_id": plan_id, "actor": _actor(),
            "basis": f"playbook.py {operation}", "writes": writes,
            "inputs": inputs, "staged_checks": list(checks),
            "final_checks": list(checks)}


def _run_operation(root, spec, gate):
    """run_operation под уже удерживаемым общим gate — вложенный токен."""
    return _nested_gate(gate, lambda: file_operations.run_operation(
        root, spec, safe_path, require))


def _nested_gate(gate, call):
    """Вызов внутренней операции под уже удерживаемым общим gate."""
    previous = os.environ.get(file_operations.GATE_ENV)
    if gate is not None:
        os.environ[file_operations.GATE_ENV] = gate.token
    try:
        return call()
    finally:
        if gate is not None:
            if previous is None:
                os.environ.pop(file_operations.GATE_ENV, None)
            else:
                os.environ[file_operations.GATE_ENV] = previous


def _scan_markdown(root, paths):
    """Полный набор файлов, чьи ссылки проверяет check_links."""
    docs = list(paths["docs"].rglob("*.md"))
    docs += list((root / CORE / "rules").glob("*.md"))
    docs += [root / "AGENTS.md", root / CORE / "README.md"]
    if (root / "README.md").exists():
        docs.append(root / "README.md")
    return docs


def _lifecycle_ctx(gate):
    """Связки lifecycle с playbook: конфиг, задачи, scan-set, проверки."""
    return {"config": config, "read_tasks": read_tasks,
            "scan_files": _scan_markdown, "core_prefix": CORE,
            "collection_index_write": _collection_index_write,
            "check_links": check_links,
            "unconfirmed": unconfirmed_lines,
            "model_unconfirmed": MODEL_UNCONFIRMED,
            "render_task_index": render_index,
            "task_index_header": INDEX_HEADER,
            "version": lambda root: safe_path(root, CORE + "/VERSION")
            .read_text(encoding="utf-8").strip(),
            "run_operation": lambda root, spec: _run_operation(root, spec,
                                                               gate)}


def _preview(root, writes):
    """Детерминированный предпросмотр write-set: unified diff каждого файла."""
    lines = ["Предпросмотр, без записи:"]
    for rel in sorted(writes):
        path = root / rel
        old = path.read_text(encoding="utf-8") if path.is_file() else ""
        new = writes[rel].decode("utf-8")
        diff = difflib.unified_diff(old.splitlines(keepends=True),
                                    new.splitlines(keepends=True),
                                    fromfile=f"a/{rel}" if old else "/dev/null",
                                    tofile=f"b/{rel}")
        lines.append("".join(diff).rstrip("\n"))
    return "\n".join(lines)


def _unique_document(catalog, doc_id):
    twin = next((key for key in catalog["documents"]
                 if key.casefold() == doc_id.casefold()), None)
    require(twin is None,
            f"document_id '{doc_id}' уже занят документом '{twin}'")


def _apply_or_preview(root, operation, seed, writes, apply, gate, kinds):
    if not apply:
        print(_preview(root, writes))
        return
    if not writes:
        print("Изменений нет: файлы уже соответствуют желаемому состоянию.")
        return
    checks = [lambda staged: _catalog_consistency(staged, "staged", kinds)]
    spec = _write_spec(root, operation, seed, writes, checks)
    result = _run_operation(root, spec, gate)
    if result["status"] == "already_completed":
        print(f"{operation}: операция {result['op_id']} уже завершена — "
              "повторное применение пропущено.")
    else:
        print(f"{operation}: записано. Операция: {result['op_dir']}")


def document_create(root, catalog, kind, slug, title, apply, gate):
    cols = catalog["collections"]
    require(kind in cols,
            f"Коллекция '{kind}' не настроена в documents project.json")
    require(DOC_SLUG.fullmatch(slug) is not None,
            f"Некорректный slug '{slug}': нужен [a-z0-9][a-z0-9_-]*")
    doc_id = f"{kind}:{slug}"
    _unique_document(catalog, doc_id)
    if title is not None:
        require(title.strip() and "\n" not in title,
                "Заголовок должен быть одной непустой строкой")
    title = title or slug
    active = cols[kind]["active"]
    target = active / f"{slug}.md"
    require(target != cols[kind]["index"],
            f"slug '{slug}' занят файлом индекса коллекции")
    require(not target.exists() and not target.is_symlink(),
            f"Файл уже существует: {_rel(root, target)}")
    if active.is_dir():
        folded = unicodedata.normalize("NFC", target.name).casefold()
        for child in active.iterdir():
            other = unicodedata.normalize("NFC", child.name).casefold()
            require(other != folded,
                    f"Коллизия имени: {_rel(root, child)}")
    template = _templates_dir() / f"{kind}.md"
    require(template.is_file(),
            f"Нет шаблона документа: {template.relative_to(_templates_dir().parent)}")
    stamp = file_operations.now_iso()
    state = INITIAL_STATE.get(kind, "draft")
    meta = {"schema_version": 1, "document_id": doc_id, "kind": kind,
            "state": state, "created_at": stamp, "updated_at": stamp,
            "current_revision": 1, "relations": [], "tasks": [],
            "changes": [], "materials": [], "archive": None}
    text = _document_text(title, meta, template.read_text(encoding="utf-8"))
    entry = {"id": doc_id, "kind": kind, "state": state, "path": target,
             "archived": False, "revision": 1, "updated_at": stamp,
             "relations": [], "tasks": [], "changes": [], "materials": [],
             "archive": None, "moves": [], "title": title}
    projected = {**catalog, "documents": {**catalog["documents"], doc_id: entry}}
    writes = {_rel(root, target): text.encode("utf-8")}
    index_write = _collection_index_write(root, projected, kind)
    if index_write is not None:
        writes[index_write[0]] = index_write[1]
    _apply_or_preview(root, f"document create {doc_id}",
                      f"create-{kind}-{slug}", writes, apply, gate, {kind})


def document_register(root, catalog, target_rel, metadata_arg, apply, gate):
    target = safe_path(root, target_rel)
    rel = _rel(root, target)
    require(target.is_file() and not target.is_symlink(),
            f"Не файл документа: {rel}")
    require(target.suffix == ".md", f"Не Markdown-документ: {rel}")
    location = None
    for kind, entry in sorted(catalog["collections"].items()):
        if target.parent == entry["active"]:
            location = (kind, False, entry)
        elif target.parent == entry["archive"]:
            location = (kind, True, entry)
    require(location is not None,
            f"{rel}: файл не находится в каталоге коллекции (active/archive)")
    kind, in_archive, collection = location
    require(target != collection["index"],
            f"{rel}: файл индекса коллекции не регистрируется как документ")
    text = target.read_text(encoding="utf-8")
    errors = []
    existing = documents.parse_meta(text, rel, errors)
    require(not errors and existing is None,
            f"{rel}: документ уже содержит метаданные"
            + (": " + "; ".join(errors) if errors else ""))
    meta = _metadata_arg(root, metadata_arg)
    errors = []
    entry = documents._validate_document(meta, target, kind, in_archive,
                                         errors,
                                         capabilities=catalog["capabilities"])
    require(not errors, "\n".join(errors))
    doc_id = entry["id"]
    _unique_document(catalog, doc_id)
    new_text = _insert_meta(text, meta)
    errors = []
    reparsed = documents.parse_meta(new_text, rel, errors)
    require(not errors and reparsed == meta,
            f"{rel}: вставленный блок метаданных не разбирается обратно")
    entry["title"] = documents._doc_title(new_text)
    # Файл покидает список legacy: проекция каталога без него.
    projected = {**catalog,
                 "documents": {**catalog["documents"], doc_id: entry},
                 "legacy": [item for item in catalog["legacy"]
                            if item["path"].resolve() != target.resolve()]}
    writes = {rel: new_text.encode("utf-8")}
    index_write = _collection_index_write(root, projected, kind)
    if index_write is not None:
        writes[index_write[0]] = index_write[1]
    _apply_or_preview(root, f"document register {doc_id}",
                      "register-" + re.sub(r"[^A-Za-z0-9_.-]", "_", target.stem),
                      writes, apply, gate, {kind})


def index_command(root, args, gate):
    data, paths = config(root)
    records = read_tasks(root, paths)
    catalog = None
    if args.documents or args.all:
        catalog = documents.load_catalog(root, data, paths, records,
                                         safe_path, require)
        require(catalog is not None,
                "Раздел documents не настроен в project.json")
    writes, checks, touched_kinds = {}, [], []
    if not args.documents:
        index = paths["task_index"]
        if index.exists():
            require(index.read_text(encoding="utf-8").startswith(INDEX_HEADER),
                    "Конфликт: индекс указывает на чужой документ; запись отменена")
        content = render_index(records, index.parent)
        if not index.is_file() or index.read_text(encoding="utf-8") != content:
            writes[_rel(root, index)] = content.encode("utf-8")
        checks.append(lambda staged: _task_index_fresh(staged, "staged"))
    if catalog is not None:
        for kind in sorted(catalog["collections"]):
            index_write = _collection_index_write(root, catalog, kind)
            if index_write is not None:
                writes[index_write[0]] = index_write[1]
            touched_kinds.append(kind)
        checks.append(lambda staged: _catalog_consistency(staged, "staged",
                                                        touched_kinds))
    if not writes:
        print("Индексы уже актуальны.")
        return
    spec = _write_spec(root, "index", "index", writes, checks)
    # index is a fresh request, not a replay of a saved operation. A previous
    # successful render may have the same bytes while today's index is stale.
    spec["plan_id"] = f"index-{uuid.uuid4().hex}"
    result = _run_operation(root, spec, gate)
    if result["status"] == "already_completed":
        print("Индексы уже актуальны.")
    else:
        print("Индекс обновлён.")


def close_task(root, task_id, status):
    _, paths = config(root)
    records = read_tasks(root, paths)
    require(task_id in records, f"Нет задачи: {task_id}")
    task = records[task_id]
    require(not task["archived"], f"{task_id}: карточка уже в архиве")
    require(task["status"] not in {"done", "cancelled"},
            f"{task_id}: уже {task['status']} — перенесите в архив вручную")
    if status == "done":
        require(task["status"] == "in_review",
                f"{task_id}: done только из in_review (после аудита), сейчас {task['status']}")
    path = task["path"]
    target = paths["archive"] / path.name
    require(not target.exists(), f"В архиве уже есть карточка: {target.name}")
    # Валидация и новое состояние — в памяти, до любой записи.
    stamp_dt = dt.datetime.now().astimezone()
    require(task["created_ts"] is None or stamp_dt >= task["created_ts"],
            f"{task_id}: Closed раньше Created")
    stamp = stamp_dt.isoformat(timespec="seconds")
    original = path.read_text(encoding="utf-8")
    if status == "done":
        lines = unconfirmed_lines(original)
        require(not lines,
                f"{task_id}: модель не подтверждена — маркер '{MODEL_UNCONFIRMED}' "
                f"в карточке (строки {', '.join(map(str, lines))}). Инженер вписывает "
                f"точное название или 'неизвестна' вместо маркера, затем повторяет close")
    new_text, count = re.subn(r"(?m)^Status: [^\n]+$", f"Status: {status}", original, count=1)
    require(count == 1, f"{task_id}: не найдена строка Status")
    new_text, count = re.subn(r"(?m)^Closed: -$", f"Closed: {stamp}", new_text, count=1)
    require(count == 1, f"{task_id}: не найдена строка Closed")
    new_text = relocate_links(new_text, path, target)
    originals, updates = retarget_links(root, paths, path, target)
    moved, written = False, []
    try:
        path.write_text(new_text, encoding="utf-8")
        paths["archive"].mkdir(parents=True, exist_ok=True)
        path.replace(target)
        moved = True
        for doc, content in updates.items():
            doc.write_text(content, encoding="utf-8")
            written.append(doc)
        write_index(root, paths)
    except (OSError, ValueError, TypeError, KeyError):
        if moved:
            target.replace(path)
        path.write_text(original, encoding="utf-8")
        for doc in written:
            doc.write_text(originals[doc], encoding="utf-8")
        raise
    linked = f", ссылки обновлены: {len(updates)}" if updates else ""
    print(f"{task_id}: {status}, Closed {stamp}. Карточка в архиве, индекс обновлён{linked}.")


def close_command(root, args, gate):
    """Закрытие задачи: через lifecycle engine при разделе documents,
    иначе — прежний пошаговый путь."""
    data, paths = config(root)
    records = read_tasks(root, paths)
    catalog = documents.load_catalog(root, data, paths, records,
                                     safe_path, require)
    if catalog is None:
        require(not args.dry_run,
                "--dry-run доступен при настроенном разделе documents "
                "project.json")
        return close_task(file_operations.scoped_operations.physical_root(root), args.task_id, args.status)
    action = {"action": "close_task", "task": args.task_id,
              "outcome": args.status}
    if args.status == "cancelled":
        require(args.reason,
                "--reason обязателен при --status cancelled в проекте "
                "с разделом documents — причина сохраняется в истории")
        action["reason"] = args.reason
    elif args.reason:
        action["reason"] = args.reason
    request = {"schema_version": 1,
               "actor": {"name": _actor(), "role": "cli", "model": None},
               "basis": f"playbook.py close {args.task_id}",
               "scope": {"tasks": [args.task_id]},
               "actions": [action]}
    ctx = _lifecycle_ctx(gate)
    plan, writes = lifecycle.build_plan(root, request,
                                        file_operations.now_iso(), ctx,
                                        safe_path, require)
    if args.dry_run:
        lifecycle.preview_plan(root, plan, writes, ctx, safe_path, require)
        print(lifecycle._preview(root, plan, writes))
        return
    target = lifecycle._save_plan(root, plan, safe_path, require)
    result = lifecycle.run_plan(root, plan, writes, ctx, safe_path, require)
    if result["status"] == "already_completed":
        print(f"Операция уже завершена: {result['op_id']} — повтор "
              "пропущен.")
    else:
        print(f"{args.task_id}: {args.status}. Карточка в архиве, индексы "
              f"и ссылки обновлены. План: {_rel(root, target)}. "
              f"Операция: {result['op_dir']}")


def check_links(root, paths, catalog=None):
    docs = list(paths["docs"].rglob("*.md"))
    docs += list((root / CORE / "rules").glob("*.md"))
    docs += [root / "AGENTS.md", root / CORE / "README.md"]
    if (root / "README.md").exists():
        docs.append(root / "README.md")
    moves = catalog["moves"] if catalog else {}
    immutable = catalog["immutable"] if catalog else []
    resolved_root = root.resolve()
    errors = []
    for path in docs:
        safe_path(root, str(path.relative_to(root)))
        text = path.read_text(encoding="utf-8")
        resolved = path.resolve()
        label = str(path.relative_to(root))
        frozen = any(resolved == item or item in resolved.parents
                     for item in immutable)
        if frozen:
            # Immutable-доказательство: ссылки разрешаются по исходному
            # расположению файла, адресат — через карту переносов.
            origin = document_links.origin_path(resolved, moves) or resolved
            errors += document_links.check_text(text, label, origin.parent,
                                                resolved_root, moves)
        else:
            errors += document_links.check_text(text, label, path.parent,
                                                resolved_root)
    require(not errors, "\n".join(errors))


def check(root, ready, skip_criteria=()):
    data, paths = config(root)
    resources(root)
    records = read_tasks(root, paths)
    require(paths["task_index"].is_file(), "Нет индекса; выполните index")
    require(paths["task_index"].read_text(encoding="utf-8") == render_index(records, paths["task_index"].parent),
            "Индекс устарел; выполните index")
    catalog = documents.load_catalog(root, data, paths, records, safe_path, require)
    if catalog is not None:
        index_errors = documents.check_indexes(catalog)
        require(not index_errors, "\n".join(index_errors))
    check_links(root, paths, catalog)
    for ident, rec in records.items():
        text = documents.read_text(rec["path"])
        if ident not in skip_criteria and task_cards.version(text) == 2:
            parsed = task_cards.parse(text)
            if any(v is not None for v in parsed["criterion_markers"].values()):
                _, mismatches = lifecycle.criterion_view(root, ident, str(rec["path"].relative_to(root)),
                    text, task_cards.metadata(text), safe_path, require, historical=rec["archived"])
                require(not mismatches, f"{ident}: чекбоксы не соответствуют журналу/свежести {mismatches}; выполните card sync {ident} --apply")
    if ready:
        require(data["setup_status"] == "configured", "Первичная настройка не завершена")
        require("TODO:" not in data["name"], "Не заполнено имя проекта")
        require(isinstance(data["commands"].get("test"), str) and data["commands"]["test"].strip(),
                "Не указана команда проверок проекта")
        for filename in ("index.md", "product.md", "architecture.md", "code-map.md", "roadmap.md"):
            require("TODO:" not in (paths["docs"] / filename).read_text(encoding="utf-8"),
                    f"Не заполнен базовый документ: {filename}")
        for component in data["components"]:
            registered = [component["code"], *component["tests"]]
            if component.get("migrations") is not None:
                registered.append(component["migrations"])
            for path in registered:
                require(safe_path(root, path).is_dir(), f"Нет каталога компонента: {path}")
    for task in records.values():
        if task["archived"]:
            continue
        lines = unconfirmed_lines(task["path"].read_text(encoding="utf-8"))
        if lines:
            print(f"ВНИМАНИЕ: {task['id']}: маркер '{MODEL_UNCONFIRMED}' в карточке "
                  f"(строки {', '.join(map(str, lines))}) — уточните точную модель "
                  f"у инженера; close в done отклонит карточку с маркером")
    found = (f"; каталог документов: {len(catalog['documents'])} документов, "
             f"{len(catalog['legacy'])} legacy") if catalog else ""
    print(f"OK: структура, ссылки и {len(records)} карточки{found}. "
          "Поведение продукта не проверялось.")


def workflow_settings(root, data, validate_inputs=True):
    """Conservative explicit opt-in; legacy settings remain the source of truth."""
    require("task_workflow" not in data, "task_workflow уже настроен; автоматическая перезапись запрещена")
    require("verification" in data,
            "Сначала настройте verification.inputs/checks; команды commands автоматически не мигрируются")
    legacy = (verification.validate(root, data["verification"], safe_path, require)
              if validate_inputs else data["verification"])
    registry = [{"id": item["id"], "kind": "command",
                 "purpose": "Обязательная проверка из legacy verification",
                 "argv": list(item["argv"]), "timeout_seconds": item["timeout_seconds"],
                 "default_inputs": list(legacy["inputs"]), "applicability": "always"}
                for item in legacy["checks"]]
    settings = {"schema_version": 2, "registry": registry,
                "checkpoints": {"merge": [item["id"] for item in registry],
                                "release": [item["id"] for item in registry]}}
    verification.validate_task_workflow(root, settings, safe_path, require)
    return settings


def workflow_enable(root, args, gate):
    data, _ = config(root)
    version = safe_path(root, CORE + "/VERSION").read_text(encoding="utf-8").strip()
    require(updating.parse_version(version, require) >= (0, 2, 0),
            "Сначала обновите установленный комплект до 0.2.0 или новее")
    data["task_workflow"] = workflow_settings(root, data)
    data["schema_version"] = 2  # Older CLI must refuse before interpreting v2.
    rel = CORE + "/project.json"
    writes = {rel: (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")}
    if not args.apply:
        print(_preview(root, writes))
        return
    spec = _write_spec(root, "workflow-enable-v2", "workflow-v2", writes,
                       [lambda staged: config(staged)])
    spec["basis"] = "Явное подключение workflow v2; legacy-команды сохранены как always"
    spec["inputs"].update(file_operations.snapshot_paths(
        root, [CORE + "/VERSION", CORE + "/resources.json", updating.MANIFEST_PATH],
        safe_path, require))
    result = _run_operation(root, spec, gate)
    print(f"Workflow v2 подключён; карточки и отчёты сохранены. Операция: {result['op_dir']}")


def workflow_migrate_card(root, args, gate):
    data, paths = config(root)
    require(data.get("task_workflow", {}).get("schema_version") == 2,
            "Сначала явно подключите task_workflow v2")
    require(args.actor.strip() and args.model.strip() and MODEL_UNCONFIRMED not in args.model
            and not any(c in args.actor + args.model for c in "\r\n")
            and args.reason.strip(), "Нужны точные actor/model и причина перехода")
    records = read_tasks(root, paths)
    rec, rel, text, meta = lifecycle._managed_card(root, records, args.task_id, safe_path, require)
    require(not rec.get("archived") and rec["status"] in {"draft", "ready", "in_progress", "in_review"},
            "Перевод допускается только для активной незакрытой карточки")
    require(meta.get("schema_version") == 1, "Перевод требует metadata v1; v2 не перезаписывается")
    plan_rel = _rel(root, safe_path(root, args.verification_plan))
    plan = read_json(safe_path(root, plan_rel))
    at = updating.now_iso()
    original = (json.dumps({"markdown": safe_path(root, rel).read_text(encoding="utf-8"),
                            "metadata": meta}, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    source = _rel(root, paths["tasks"] / (args.task_id + "-evidence/workflow-v1.json"))
    require(not safe_path(root, source).exists(), "История миграции уже существует")
    event = {"at": at, "action": "revise", "reason": args.reason,
             "actor": f"{args.actor} / {args.model}"}
    for key in ("handoff", "acceptance"):
        if key in meta:
            event[key] = meta.pop(key)
    meta.pop("criterion_evidence", None)
    meta["schema_version"] = 2
    meta["workflow"] = {"route": args.route, "reason": args.reason,
        "history": [{"at": at, "to_route": args.route, "reason": args.reason, "actor": event["actor"]}],
        "transitions": [event]}
    meta["verification_plan"] = plan
    meta.setdefault("materials", []).append({"id": "workflow-v1", "role": "report",
        "source": source, "storage": "local", "required": True, "immutable": True,
        "used_by": [args.task_id]})
    # The original bytes preserve status, protocol and metadata; new start is
    # required to capture a trustworthy baseline for this explicitly new plan.
    text = re.sub(r"^Status:.*$", "Status: ready", text, flags=re.M)
    if task_cards.version(text) == 2:
        text = task_cards.render_checkboxes(text, {
            ident: {"checked": False} for _, ident, _ in task_cards.criterion_lines(text)})
    else:
        text = re.sub(r"(^## Приёмка\s*\n)(.*?)(?=^## |\Z)",
                      lambda m: m[1] + re.sub(r"^- \[x\]", "- [ ]", m[2], flags=re.M),
                      text, flags=re.M | re.S)
    merged = lifecycle._splice_meta(text, meta)
    errors = []
    documents._validate_task_meta(meta, rel, errors,
        registry={r["id"]: r for r in data["task_workflow"]["registry"]},
        task_workflow_enabled=True, root=root, safe_path=safe_path)
    require(not errors, "\n".join(errors))
    required = verification.verification_policy.required_checks(
        data["task_workflow"], plan, baseline=None, current=None)
    require(required["ready"], "\n".join(required["errors"]))
    projected = dict(records)
    projected[args.task_id] = dict(rec, status="ready", route=args.route)
    index_rel = _rel(root, paths["task_index"])
    writes = documents.split_card_writes({rel: merged.encode("utf-8")})
    writes.update({source: original, index_rel: render_index(projected, paths["task_index"].parent).encode("utf-8")})
    if not args.apply:
        print(_preview(root, writes))
        return
    checks = [lambda staged: _catalog_consistency(staged, "workflow migrate-card", set()),
              lambda staged: _task_index_fresh(staged, "workflow migrate-card")]
    spec = _write_spec(root, "workflow-migrate-card", "migrate-" + args.task_id, writes, checks)
    spec["actor"] = event["actor"]
    spec["basis"] = args.reason
    spec["inputs"].update(file_operations.snapshot_paths(root, [plan_rel, CORE + "/project.json"], safe_path, require))
    _run_operation(root, spec, gate)
    print(f"{args.task_id}: metadata v2, ready; история сохранена. Выполните lifecycle start и новые проверки.")


def install(source, target, apply, workflow_v2=False):
    require(not target.is_symlink(), "Каталог назначения — символическая ссылка")
    target = target.resolve()
    cur = target
    while cur != cur.parent:
        require(not cur.is_symlink(), f"Символическая ссылка в пути назначения: {cur}")
        if cur.exists():
            require(cur.is_dir(), f"Путь назначения не является каталогом: {cur}")
            break
        cur = cur.parent
    require(not source.is_relative_to(target), "Нельзя устанавливать поверх исходного комплекта или его родителя")
    base = source / CORE
    managed = updating.managed_files(source, safe_path, require)
    version = updating.parse_version(managed[f"{CORE}/VERSION"].decode("utf-8").strip(), require)
    version_text = ".".join(str(part) for part in version)
    planned = dict(managed)
    project_template = json.loads((base / "templates" / "project.json").read_text(encoding="utf-8"))
    project_template["playbook_version"] = version_text
    if workflow_v2:
        require(version >= (0, 2, 0), "init --workflow-v2 требует комплект 0.2.0 или новее")
        defaults = json.loads((base / "templates" / "verification.json").read_text(encoding="utf-8"))
        project_template["verification"] = defaults
        # Bundled init inputs are created below; artifacts need not contain docs.
        project_template["task_workflow"] = workflow_settings(target, project_template, validate_inputs=False)
        project_template["schema_version"] = 2
    planned[f"{CORE}/project.json"] = \
        (json.dumps(project_template, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    resources_template = json.loads((base / "templates" / "resources.json").read_text(encoding="utf-8"))
    planned[f"{CORE}/resources.json"] = \
        (json.dumps(resources_template, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    for filename in ("AGENTS.md", "CHANGELOG.md"):
        planned[filename] = (base / "templates" / filename).read_bytes()
    for path in sorted((base / "templates/docs").rglob("*.md")):
        planned[f"docs/{path.relative_to(base / 'templates/docs')}"] = path.read_bytes()
    planned["docs/tasks/index.md"] = render_index({}).encode("utf-8")
    docs_conf = project_template.get("documents")
    if isinstance(docs_conf, dict) and isinstance(docs_conf.get("collections"), dict):
        empty_catalog = {
            "root": target,
            "collections": {
                kind: {
                    "active": safe_path(target, col["active"]),
                    "archive": safe_path(target, col["archive"]),
                    "index": safe_path(target, col["index"])
                }
                for kind, col in docs_conf["collections"].items()
            },
            "documents": {},
            "legacy": [],
            "records": {}
        }
        for kind, col in sorted(docs_conf["collections"].items()):
            index_rel = col["index"]
            planned[index_rel] = documents.render_index_file(empty_catalog, kind).encode("utf-8")
    managed_hashes = {rel: updating.sha256(content) for rel, content in managed.items()}
    manifest = {"schema_version": updating.MANIFEST_SCHEMA, "playbook_version": version_text,
                "release_id": updating.fingerprint(managed_hashes),
                "installed_at": updating.now_iso(), "origin": "init",
                "files": managed_hashes}
    planned[updating.MANIFEST_PATH] = \
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    destinations = []
    for relative, content in sorted(planned.items()):
        path = safe_path(target, relative)
        require(not path.is_symlink(), f"Конфликт: {relative} — символическая ссылка.")
        require(not path.exists(), f"Конфликт: {relative}. Запись не начата; требуется ручное объединение.")
        for parent in path.parents:
            if parent == target:
                break
            require(not parent.is_symlink(), f"Родитель является символической ссылкой: {parent}")
            require(not parent.exists() or parent.is_dir(), f"Родитель не является каталогом: {parent}")
        destinations.append((path, content))
    archive_dirs = []
    if isinstance(docs_conf, dict) and isinstance(docs_conf.get("collections"), dict):
        for col in docs_conf["collections"].values():
            archive_dirs.append(safe_path(target, col["archive"]))
    if "paths" in project_template and "archive" in project_template["paths"]:
        archive_dirs.append(safe_path(target, project_template["paths"]["archive"]))
    for adir in archive_dirs:
        rel = str(adir.relative_to(target))
        require(not adir.is_symlink(), f"Конфликт: {rel} — символическая ссылка.")
        require(not adir.exists() or adir.is_dir(), f"Конфликт: {rel}. Запись не начата; требуется ручное объединение.")
        for parent in adir.parents:
            if parent == target:
                break
            require(not parent.is_symlink(), f"Родитель является символической ссылкой: {parent}")
            require(not parent.exists() or parent.is_dir(), f"Родитель не является каталогом: {parent}")
    if not apply:
        print("Предпросмотр, без записи:\n" + "\n".join(str(p.relative_to(target)) for p, _ in destinations))
        return
    created_files = []
    created_dirs = []

    def make_dir(d):
        stack = []
        cur = d
        while not cur.exists():
            stack.append(cur)
            cur = cur.parent
        for p in reversed(stack):
            p.mkdir()
            created_dirs.append(p)

    try:
        for path, content in destinations:
            make_dir(path.parent)
            with path.open("xb") as stream:
                created_files.append(path)
                stream.write(content)
        for adir in archive_dirs:
            make_dir(adir)
    except OSError:
        for path in reversed(created_files):
            try:
                path.unlink()
            except OSError:
                pass
        for dir_path in reversed(created_dirs):
            try:
                dir_path.rmdir()
            except OSError:
                pass
        raise
    print(f"Создано {len(created_files)} файлов в {target}; manifest записан. "
          f"Заполните документы и project.json; setup_status=needs_review.")


def command_gate(root, args):
    """Общий gate изменяющих команд; None — команда только читает.

    Фиксированный порядок блокировок: сначала этот gate, внутри — специа-
    лизированные lock операций (update/lifecycle). init пишет в другой
    корень и gate не берёт; verify пробрасывает токен вложенным проверкам.
    """
    action = getattr(args, "action", None)
    mutating = (
        args.command == "index"
        or (args.command == "workflow" and args.apply)
        or (args.command == "card" and action in {"sync", "create"} and args.apply)
        or (args.command == "close" and not args.dry_run)
        or (args.command == "document"
            and action in {"create", "register"} and args.apply)
        or (args.command == "verify" and not args.dry_run and not args.preflight and not args.audit_ready)
        or (args.command == "adopt" and args.apply)
        or (args.command == "update"
            and (action in {"apply", "restore"}
                 or (action == "plan" and getattr(args, "save", False))))
        or (args.command == "lifecycle"
            and (action in {"apply", "restore", "handoff", "accept", "start", "route", "revise"}
                 or (action in {"register-material", "prepare-audit",
                                "relocate-material"} and args.apply)
                 or (action == "plan" and getattr(args, "save", False))))
        or (args.command == "policy" and action == "apply")
        or (args.command == "block"
            and action in {"create-fix", "accept", "close"}
            and getattr(args, "apply", False)))
    if not mutating:
        return None
    allowed = ()
    if args.command == "update" and action == "restore":
        allowed = (updating.PENDING_FILE,)
    if args.command == "lifecycle" and action == "restore":
        allowed = (file_operations.PENDING_FILE,)
    owner = args.command + (f" {action}" if action else "")
    return file_operations.acquire_gate(root, safe_path, require,
                                        owner=owner, allowed_pending=allowed)


def _catalog_policies(catalog):
    """{document_id: entry} планов с полем acceptance_policy."""
    return {ident: {"policy": entry["acceptance_policy"],
                    "path": str(entry["path"].relative_to(catalog["root"])),
                    "archived": entry["archived"], "entry": entry}
            for ident, entry in catalog["documents"].items()
            if entry.get("has_policy")}


def _policy_command(root, args, gate):
    """policy preview/apply: проверка и запись acceptance_policy плана.

    Политика — единственный источник порядка приёмки блока; команда лишь
    валидирует payload и записывает его в метаданные plan-документа
    штатной восстанавливаемой операцией. Никаких статусов задач она
    не копирует и выполнение не разрешает.
    """
    data, paths = config(root)
    records = read_tasks(root, paths)
    catalog = documents.load_catalog(root, data, paths, records,
                                     safe_path, require)
    require(catalog is not None, "Раздел documents не настроен в project.json")
    require(block_workflow.CAPABILITY in catalog["capabilities"],
            f"Политика блока требует явной capability "
            f"'{block_workflow.CAPABILITY}' в project.json → "
            "documents.capabilities")
    entry = catalog["documents"].get(args.document_id)
    require(entry is not None and entry["kind"] == "plan",
            f"{args.document_id}: политика записывается только "
            "в документ kind 'plan'")
    require(not entry["archived"],
            f"{args.document_id}: политика архивированного плана "
            "не изменяется")
    payload = _metadata_arg(root, args.policy)
    label = f"policy {args.document_id}"
    errors = []
    block_workflow.validate_policy(payload, label, errors)
    require(not errors, "\n".join(errors))
    rel = _rel(root, entry["path"])
    text = documents.read_text(entry["path"])
    meta = documents.parse_meta(text, rel, [])
    existing = meta.get("acceptance_policy")
    if existing is not None:
        errors = []
        block_workflow.validate_policy(existing, label, errors)
        require(not errors, "\n".join(errors))
        require(payload.get("policy_id") == existing.get("policy_id"),
                "policy_id неизменяем — новая политика оформляется "
                "отдельным документом")
        if payload == existing:
            print(f"{args.document_id}: такая политика уже записана.")
            return
        require(type(payload.get("revision")) is int
                and payload["revision"] > existing.get("revision", 0),
                "Изменение политики требует новой ревизии: "
                f"{existing.get('revision')} → {payload.get('revision')}")
    # Проекция ссылок: существующие refs получают новую revision, у новых
    # members/final_audit создаётся минимальная ссылка — одна операция
    # поддерживает инвариант «политика ↔ ссылки карточек». Ссылка на эту
    # политику вне её состава (кроме fix) отклоняется, не затирается.
    policies = _catalog_policies(catalog)
    projected = dict(policies)
    projected[args.document_id] = {"policy": payload, "path": rel,
                                   "archived": False}
    new_revision = payload["revision"]
    ref_writes, projected_blocks = {}, {}
    for ident, tmeta in catalog["task_meta"].items():
        if tmeta is None or not tmeta.get("has_block"):
            continue
        ref = tmeta["block"]
        if ref is not None and ref.get("document") == args.document_id:
            ref = {**ref, "policy_id": payload["policy_id"],
                   "revision": new_revision}
            ref_writes[ident] = ref
        projected_blocks[ident] = ref
    for ident in projected_blocks:
        ref = projected_blocks[ident]
        if ref is None or ref.get("document") != args.document_id \
                or ref.get("role") == "fix":
            continue
        require(ident in payload["members"]
                or ident == payload["final_audit"],
                f"{ident}: ссылается на политику, но не входит в её "
                "состав — уберите ссылку или добавьте задачу в политику")
    for ident in [*payload["members"],
                  *([payload["final_audit"]] if payload["final_audit"]
                    else [])]:
        if ident in projected_blocks:
            continue
        rec = records.get(ident)
        require(rec is not None and not rec["archived"],
                f"{ident}: политика ссылается на отсутствующую "
                "активную карточку")
        ref = {"schema_version": block_workflow.CARD_BLOCK_VERSION,
               "document": args.document_id,
               "policy_id": payload["policy_id"], "revision": new_revision}
        ref_writes[ident] = ref
        projected_blocks[ident] = ref
    graph_errors = []
    block_workflow.validate_block_graph(projected, projected_blocks,
                                        records, graph_errors)
    require(not graph_errors, "\n".join(graph_errors))
    meta = dict(meta)
    meta["acceptance_policy"] = payload
    meta["updated_at"] = file_operations.now_iso()
    merged = lifecycle._splice_meta(text, meta)
    require(merged is not None, f"{rel}: блок метаданных не найден")
    writes = {rel: merged.encode("utf-8")}
    for ident, ref in sorted(ref_writes.items()):
        rec = records[ident]
        card_rel = _rel(root, rec["path"])
        card_text = documents.read_text(rec["path"])
        card_meta = documents.parse_meta(card_text, card_rel, [])
        require(card_meta is not None,
                f"{card_rel}: карточка без метаданных — ссылка block "
                "не записывается")
        if card_meta.get("block") == ref:
            continue
        card_meta = dict(card_meta)
        card_meta["block"] = ref
        merged = lifecycle._splice_meta(card_text, card_meta)
        require(merged is not None,
                f"{card_rel}: блок метаданных не найден")
        writes.update(documents.split_card_writes(
            {card_rel: merged.encode("utf-8")}))
    # Индекс коллекции — по проецированному каталогу (updated_at новой
    # редакции уже вошёл в регион); файл без маркеров не трогаем.
    index = catalog["collections"]["plan"]["index"]
    if index.is_file() and not index.is_symlink():
        projected_catalog = dict(catalog)
        projected_docs = dict(catalog["documents"])
        updated_entry = dict(projected_docs[args.document_id])
        updated_entry["updated_at"] = meta["updated_at"]
        projected_docs[args.document_id] = updated_entry
        projected_catalog["documents"] = projected_docs
        index_text = index.read_text(encoding="utf-8")
        merged_index = documents.splice_index(
            index_text, documents.index_region(projected_catalog, "plan"))
        if merged_index is not None and merged_index != index_text:
            writes[_rel(root, index)] = merged_index.encode("utf-8")
    if args.action == "preview":
        print(_preview(root, writes))
        print("Политика проверена: граф согласован, противоречий нет.")
        return
    checks = [lambda staged: _catalog_consistency(staged, "staged", {"plan"})]
    seed = "policy-" + re.sub(r"[^A-Za-z0-9_-]", "-", args.document_id)
    spec = _write_spec(root, "policy apply", seed, writes, checks)
    result = _run_operation(root, spec, gate)
    print(f"{args.document_id}: политика записана "
          f"(revision {payload['revision']}). Операция: {result['op_dir']}")


def _handoff_evidence(catalog, records):
    """Факты актуальных передач для evaluator: v2 — журнальная привязка."""
    handoffs = {}
    for ident, meta in catalog["task_meta"].items():
        if meta is None:
            continue
        record = meta.get("handoff")
        if record is None:
            continue
        current = records[ident]["status"] == "in_review"
        if current:
            text = documents.read_text(records[ident]["path"])
            try:
                task_cards.receipt(text, meta)
            except ValueError:
                current = False
        handoffs[ident] = {"current": current,
                           "id": record.get("entry_id")
                           or record.get("at")}
    return handoffs


def _block_status_command(root, args):
    """block status: read-only диагностика общего evaluator."""
    data, paths = config(root)
    records = read_tasks(root, paths)
    catalog = documents.load_catalog(root, data, paths, records,
                                     safe_path, require)
    require(catalog is not None, "Раздел documents не настроен в project.json")
    policies = _catalog_policies(catalog)
    require(policies, "Ни один план не содержит acceptance_policy")
    target = args.id
    if target in policies:
        doc_id = target
    else:
        meta = (catalog["task_meta"].get(target) or {})
        ref = meta.get("block")
        require(meta and ref is not None,
                f"{target}: нет ни политики, ни карточки со ссылкой block")
        doc_id = ref.get("document")
        require(doc_id in policies, f"{target}: политика '{doc_id}' "
                "не найдена")
    entry = policies[doc_id]
    policy = entry["policy"]
    require(policy is not None,
            f"{doc_id}: политика не проходит структурную проверку — "
            "см. check")
    evidence = {"handoffs": _handoff_evidence(catalog, records)}
    blocks = {ident: meta["block"]
              for ident, meta in catalog["task_meta"].items()
              if meta is not None and meta.get("has_block")}
    tasks_view = {ident: {"status": rec["status"], "deps": rec["deps"],
                          "archived": rec["archived"]}
                  for ident, rec in records.items()}
    status = block_workflow.block_status(policy, tasks_view, evidence)
    members = [t for t in policy["members"]
               + ([policy["final_audit"]] if policy["final_audit"] else [])
               if t in records]
    fixes = sorted(t for t, ref in blocks.items()
                   if ref and ref.get("document") == doc_id
                   and ref.get("role") == "fix" and t in records)
    report = {"policy": {"document": doc_id,
                         "policy_id": policy["policy_id"],
                         "revision": policy["revision"],
                         "mode": policy["mode"],
                         "archived": entry["archived"]},
              "block": status, "tasks": {}}
    for ident in members + fixes:
        rec = records[ident]
        task = {"id": ident, "status": rec["status"], "deps": rec["deps"],
                "block": blocks.get(ident)}
        readiness = {}
        for operation in ("check", "audit_ready", "accept", "close"):
            verdict = block_workflow.evaluate(
                operation, task, policy, tasks_view, evidence)
            readiness[operation] = {
                "allowed": verdict["allowed"],
                "reason_codes": verdict["reason_codes"]}
        report["tasks"][ident] = {"status": rec["status"],
                                  "archived": rec["archived"],
                                  "readiness": readiness}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"{doc_id}: политика '{policy['policy_id']}' "
          f"revision {policy['revision']}, mode={policy['mode']}"
          + (" (архивирована)" if entry["archived"] else ""))
    print(f"Статус блока: {status['status']} — {status['next']}")
    if status["findings_open"]:
        print(f"Открытые находки: {', '.join(status['findings_open'])}")
    for ident in members + fixes:
        item = report["tasks"][ident]
        parts = []
        for operation, verdict in item["readiness"].items():
            mark = "+" if verdict["allowed"] else "-"
            reasons = (f" ({', '.join(verdict['reason_codes'])})"
                       if verdict["reason_codes"] else "")
            parts.append(f"{operation}{mark}{reasons}")
        print(f"  {ident} [{item['status']}]: " + "; ".join(parts))


def _block_deferred(action, owner):
    require(False, f"Команда 'block {action}' зафиксирована контрактом "
            f"PB-065; операция реализуется {owner}. "
            "Синтаксис и форматы уже действуют.")


def _checks_deferred(command, action, owner):
    require(False, f"Команда '{command} {action}' зафиксирована контрактом "
            f"PB-057; операция реализуется {owner}. "
            "Синтаксис и форматы уже действуют.")


def _load_policy(root, data):
    """Загрузить одобренную checks-политику по ссылке project.json."""
    require(checks_contract.PROJECT_LINK_FIELD in data,
            "checks_policy не настроен в project.json — политика "
            "подключается явной миграцией (PB-063)")
    policy_path = safe_path(root, data[checks_contract.PROJECT_LINK_FIELD])
    policy = json.loads(policy_path.read_text(encoding="utf-8"),
                        object_pairs_hook=documents._unique_pairs)
    return checks_contract.validate_policy(policy, require)


def checks_plan(root, args):
    """checks plan <TASK>: read-only объяснение набора и стоимости.

    Не пишет файлы и не исполняет discovery/код проекта: inventory,
    AST-анализ и снимки входов только читают дерево. Хранимое состояние
    контура (work/checks/) производится позднейшими операциями; его
    утрата расширяет выбор, а не сужает его.
    """
    data, paths = config(root)
    policy = _load_policy(root, data)
    records = read_tasks(root, paths)
    task = records.get(args.task_id)
    require(task is not None and not task["archived"],
            f"{args.task_id}: активная карточка не найдена")
    task_content = documents.read_text(task["path"])
    meta_errors = []
    task_meta = documents.parse_meta(
        task_content, str(task["path"].relative_to(root)),
        meta_errors) or {}
    task_digest = verification.verification_policy.fingerprint(
        verification.verification_policy.task_projection(
            task_content, task_meta))

    notes = []
    state_dir = checks_select.STATE_DIR
    baseline = None
    snap_path = safe_path(
        root, f"{state_dir}/{checks_select.SNAPSHOT_FILENAME}")
    if snap_path.is_file():
        try:
            baseline = checks_inventory.load_snapshot(snap_path)
        except (ValueError, OSError) as error:
            notes.append(f"baseline-снимок непригоден: {error}")
    previous = None
    graph_path = safe_path(
        root, f"{state_dir}/{checks_graph.GRAPH_FILENAME}")
    if graph_path.is_file():
        try:
            previous = checks_graph.load_graph(graph_path)
        except (ValueError, OSError) as error:
            notes.append(f"прежний граф непригоден: {error}")
    results, skipped = [], 0
    results_dir = safe_path(
        root, f"{state_dir}/{checks_select.RESULTS_DIR}")
    if results_dir.is_dir():
        for item in sorted(results_dir.rglob("*.json")):
            try:
                require(not item.is_symlink(), "символическая ссылка")
                doc = json.loads(item.read_text(encoding="utf-8"),
                                 object_pairs_hook=documents._unique_pairs)
                checks_contract.validate_result(doc, require)
                results.append(doc)
            except (ValueError, OSError):
                skipped += 1
    if skipped:
        notes.append(f"пропущено повреждённых результатов: {skipped}")
    # Граф строится внутри плана: AST-чтение идёт под общим
    # бюджетом max_bytes (аудит A-04).
    plan = checks_select.plan(root, policy, baseline=baseline,
                              previous=previous, results=results,
                              task_digest=task_digest)

    print(f"checks plan {args.task_id}: политика "
          f"{policy['policy_id']} rev {policy['revision']}, "
          f"trust={policy['trust_mode']}")
    if plan["changed"] is None:
        print(f"Изменения: не доказаны ({plan['change_reason']}) — "
              f"консервативный набор")
    else:
        print(f"Изменения: {len(plan['changed'])} путей")
        for path in plan["changed"]:
            print(f"  ~ {path}")
    if plan["unattributed"]:
        print(f"Вне модулей: {', '.join(plan['unattributed'])}")
    print(f"Граф: {plan['graph_edges']} рёбер, "
          f"unknown-областей: {plan['graph_unknown']}")
    max_bytes = plan["limits"].get("max_bytes")
    byte_limit = f" (лимит {max_bytes} Б)" if max_bytes else ""
    print(f"Оценка: {plan['estimate_seconds']}с лимит "
          f"{plan['limits']['max_seconds']}с; прочитано "
          f"{plan['hashed_bytes']} Б / {plan['hashed_files']} "
          f"файлов{byte_limit}")
    for check in policy["checks"]:
        entry = plan["checks"][check["id"]]
        print(f"  {check['id']}: {entry['status']}")
        for line in entry["explain"]:
            print(f"      {line}")
    for reason in plan["budget_reasons"]:
        print(f"BLOCKED: {reason}")
    for note in notes:
        print(f"Примечание: {note}")
    print("Итог: " + ", ".join(f"{name}={count}" for name, count
                               in plan["summary"].items()
                               if count or name != "not_applicable"))


def _checks_task(root, args):
    """Политика и активная карточка задачи для checks run/status."""
    data, paths = config(root)
    policy = _load_policy(root, data)
    records = read_tasks(root, paths)
    task = records.get(args.task_id)
    require(task is not None and not task["archived"],
            f"{args.task_id}: активная карточка не найдена")
    task_content = documents.read_text(task["path"])
    meta_errors = []
    task_meta = documents.parse_meta(
        task_content, str(task["path"].relative_to(root)),
        meta_errors) or {}
    return policy, task_content, task_meta


def checks_run(root, args):
    """checks run <TASK>: исполнить обязательства области (PB-060).

    Runner пишет append-only результаты в work/checks/ и обновляет
    baseline/граф контура; локальный режим честно помечает вывод как
    диагностический. Код выхода 0 — только когда все обязательства
    области покрыты passed либо переиспользованным успехом.
    """
    policy, task_content, task_meta = _checks_task(root, args)
    outcome = checks_runner.execute(
        root, policy, task_id=args.task_id, scope=args.scope,
        mode=args.mode, checkpoint=args.checkpoint,
        task_content=task_content, task_meta=task_meta)
    return 0 if outcome["ok"] else 1


def checks_status(root, args):
    """checks status <TASK>: покрытие и ограничения доверия (PB-060)."""
    policy, task_content, task_meta = _checks_task(root, args)
    checks_runner.status(
        root, policy, task_id=args.task_id, task_content=task_content,
        task_meta=task_meta)
    return 0


def card_create(root, args, gate):
    """Create only new cards; never migrate an existing card implicitly."""
    data, paths = config(root)
    records = read_tasks(root, paths)
    require(ID_PATTERN.fullmatch(args.task_id) is not None, 'Некорректный ID')
    require(args.task_id not in records, 'ID уже занят')
    require(args.title.strip() and not any(c in args.title for c in '\r\n'), 'Название должно быть однострочным')
    require(data.get('documents') is not None, 'card create требует documents в project.json')
    path = paths['tasks'] / (args.task_id + '.md')
    sidecar = documents.meta_path(path)
    for target in (path, sidecar):
        safe_path(root, str(target.relative_to(root)))
        require(not target.exists(), f'Назначение занято: {target}')
    quick = args.template == 'quick'
    require(quick or (args.reason is None and args.verification_plan is None),
            '--reason и --verification-plan применяются только к --template quick')
    if quick:
        require(data.get('task_workflow', {}).get('schema_version') == 2,
                'quick требует явно включённый task_workflow.schema_version: 2; legacy не мигрируется')
        require(args.reason and args.reason.strip(), 'quick требует --reason с обоснованием низкого риска')
        require(args.verification_plan, 'quick требует --verification-plan <JSON> с конкретными проверками')
    template = Path(__file__).parents[1] / ('templates/task-quick.md' if quick else 'templates/task.md')
    text = template.read_text(encoding="utf-8")
    text = text.replace('# TASK-001: Краткое название', f'# {args.task_id}: {args.title}', 1)
    text = text.replace('ID: TASK-001', f'ID: {args.task_id}', 1)
    text = text.replace('Created: -', 'Created: ' + dt.datetime.now().astimezone().isoformat(timespec='seconds'), 1)
    raw_meta = documents.meta_path(template).read_text(encoding='utf-8')
    meta = json.loads(raw_meta, object_pairs_hook=documents._unique_pairs)
    if quick:
        plan_path = safe_path(root, args.verification_plan)
        require(plan_path.is_file(), 'Файл verification-plan не найден')
        meta['workflow']['reason'] = args.reason.strip()
        meta['verification_plan'] = json.loads(plan_path.read_text(encoding='utf-8'),
                                               object_pairs_hook=documents._unique_pairs)
    text = documents.hydrate_card(text, meta)
    rel = str(path.relative_to(root))
    task_cards.parse(text, rel)
    writes = documents.split_card_writes({rel: text.encode('utf-8')})
    projected = dict(records)
    projected[args.task_id] = dict(id=args.task_id, title=f'{args.task_id}: {args.title}', status='draft',
        priority='P2', deps=[], executor=None, model=None, created=re.search(r'(?m)^Created: (.+)$', text)[1],
        closed='-', created_ts=dt.datetime.fromisoformat(re.search(r'(?m)^Created: (.+)$', text)[1]),
        closed_ts=None, path=path, archived=False, route='quick' if quick else None)
    writes[str(paths['task_index'].relative_to(root))] = render_index(projected, paths['task_index'].parent).encode('utf-8')
    if not args.apply:
        print(_preview(root, writes))
        return
    checks = [lambda staged: _catalog_consistency(staged, 'card create', set()),
              lambda staged: _task_index_fresh(staged, 'card create')]
    spec = _write_spec(root, 'card create', 'card-' + args.task_id, writes, checks)
    _run_operation(root, spec, gate)
    print(f'{args.task_id}: создана карточка с внешними метаданными (draft).')


def dispatch(root, args, gate):
    tracked = (args.command in {"index", "close"}
               or args.command == "workflow"
               or (args.command == "card" and args.action in {"create", "sync"})
               or (args.command == "document" and args.action in {"create", "register"})
               or args.command == "policy"
               or (args.command == "block" and args.action != "status")
               or (args.command == "lifecycle" and args.action not in {"status", "restore"}))
    if tracked:
        root = project_view.ProjectView(root).path
    """Выполнить команду; gate — удерживаемая общая блокировка либо None."""
    if args.command == "card" and args.action == "create":
        card_create(root, args, gate)
        return 0
    if args.command == "card" and args.action == "sync":
        lifecycle.sync_criteria_command(root, args.task_id, args.apply, _lifecycle_ctx(gate), safe_path, require)
        return 0
    if args.command == "init":
        install(Path(__file__).resolve().parents[2], args.target, args.apply, args.workflow_v2)
    elif args.command == "workflow":
        if args.action == "enable-v2":
            workflow_enable(root, args, gate)
        else:
            workflow_migrate_card(root, args, gate)
    elif args.command == "check":
        check(root, args.ready)
    elif args.command == "card":
        data, paths = config(root)
        require(ID_PATTERN.fullmatch(args.task_id) is not None, "Некорректный ID")
        found = [safe_path(root, str((paths[key] / (args.task_id + ".md")).relative_to(root)))
                 for key in ("tasks", "archive")]
        found = [path for path in found if path.is_file()]
        require(len(found) == 1, "Карточка не найдена либо ID неоднозначен")
        path = found[0]
        text = documents.read_text(path)
        parsed = task_cards.parse(text, str(path.relative_to(root)))
        if parsed["status"] == "supported":
            meta = task_cards.metadata(text)
            states, mismatches = lifecycle.criterion_view(root, args.task_id, str(path.relative_to(root)), text,
                meta, safe_path, require, historical=path.parent == paths["archive"])
            parsed["criterion_states"] = states
            parsed["checkbox_mismatches"] = mismatches
            parsed["rendered_markdown"] = task_cards.public_markdown(task_cards.render_checkboxes(text, states))
            parsed["handoff_state"] = "not_current"
            parsed["acceptance_state"] = "not_current"
            if path.parent == paths["archive"]:
                parsed["handoff_state"] = "historical"
                parsed["acceptance_state"] = "historical"
            else:
                try:
                    records = read_tasks(root, paths)
                    catalog = documents.load_catalog(root, data, paths, records, safe_path, require)
                    require(catalog is not None, "Каталог документов не настроен")
                    meta = catalog["task_meta"][args.task_id]
                    rec = records[args.task_id]
                    try:
                        task_cards.receipt(text, meta)
                        lifecycle._handoff_evidence(root, args.task_id, str(path.relative_to(root)), text, meta,
                                                    safe_path, require)
                        parsed['handoff_state'] = 'current'
                    except ValueError as error:
                        parsed['handoff_reason'] = str(error)
                    lifecycle._close_evidence(root, args.task_id, rec, text, "done", meta,
                                              safe_path, require)
                    parsed["acceptance_state"] = "current"
                except ValueError as error:
                    parsed["acceptance_reason"] = str(error)
        if args.entry:
            require(parsed['status'] == 'supported', 'Выбор записей не поддержан: откройте Markdown')
            selected = task_cards.select_entry(parsed, args.entry)
            if args.json:
                print(json.dumps(selected, ensure_ascii=False, indent=2))
            elif 'raw' in selected:
                print(selected['raw'])
            else:
                print('#### ' + selected['id'] + ' — Замечание')
                print('\n'.join(k + ': ' + v for k,v in selected['fields'].items()))
        elif args.handoff and parsed['status'] == 'supported':
            print(json.dumps(task_cards.handoff_view(parsed), ensure_ascii=False, indent=2)
                  if args.json else task_cards.render_handoff(parsed))
            if not args.json and parsed['checkbox_mismatches']:
                print(f"Файл требует card sync {args.task_id} --apply: {parsed['checkbox_mismatches']}")
        elif args.json:
            print(json.dumps(parsed, ensure_ascii=False, indent=2))
        elif parsed["status"] == "unsupported":
            print(f"{parsed['message']}\n{parsed['markdown_path']}")
        else:
            print(parsed["rendered_markdown"] if args.full else task_cards.render_context(parsed))
            if parsed["checkbox_mismatches"]:
                print(f"Файл требует card sync {args.task_id} --apply: {parsed['checkbox_mismatches']}")
            if args.full:
                print(f"Приёмка CLI: {parsed['acceptance_state']}")
    elif args.command == "verify":
        data, paths = config(root)
        policy = resources(root)
        require(data["setup_status"] == "configured", "Первичная настройка не завершена")
        records = read_tasks(root, paths)
        require(args.task_id in records, f"Нет задачи: {args.task_id}")
        task = records[args.task_id]
        require(not task["archived"] and task["status"] in {"ready", "in_progress", "in_review"},
                "verify требует активную задачу ready/in_progress/in_review")

        task_content = documents.read_text(task["path"])
        task_meta = task_cards.metadata(task_content) or {}
        is_v2 = verification.is_v2_task(task_content, task_meta)

        task_index_path = paths["task_index"]
        expected_index = render_index(records, task_index_path.parent)

        # Preflight and dry-run are read-only: never modify task index.
        # Regular mutating verify for v2 tasks synchronizes task index before taking test snapshot.
        if is_v2 and not args.dry_run and not args.preflight and not args.audit_ready:
            if not task_index_path.is_file() or task_index_path.read_text(encoding="utf-8") != expected_index:
                try:
                    write_index(root, paths)
                    print("Индекс задач синхронизирован перед проверкой.")
                except Exception as e:
                    require(False, f"Ошибка синхронизации индекса задач: {e}")

        manual_arg = getattr(args, "record_manual", None)
        if manual_arg:
            require(is_v2, "--record-manual применим только к карточкам v2")
            require("task_workflow" in data, "Раздел task_workflow отсутствует в project.json")
            tw_settings = verification.validate_task_workflow(root, data["task_workflow"], safe_path, require)
            return verification.record_manual_v2(root, task, tw_settings, manual_arg, safe_path, require)

        if is_v2:
            require("task_workflow" in data, "Карточка v2 требует раздел task_workflow в project.json")
            tw_settings = verification.validate_task_workflow(root, data["task_workflow"], safe_path, require)
            checkpoint = getattr(args, "checkpoint", None)
            if args.preflight or args.audit_ready or not args.dry_run:
                file_operations.read_only_gate_check(root, safe_path, require,
                                                      token=gate.token if gate else None)
                try:
                    lifecycle.verification_preflight(root, args.task_id, _lifecycle_ctx(gate), safe_path, require)
                except ValueError as err:
                    if args.preflight and "индекс задач" in str(err):
                        require(False, f"Индекс задач устарел. Способ исправления: выполните 'python3 ai-dev-playbook/scripts/playbook.py index'")
                    raise
                if not args.preflight and not args.audit_ready:
                    check(root, True, skip_criteria={args.task_id})
            if args.preflight or args.audit_ready:
                res = verification.preflight_v2(root, task, tw_settings, policy["policy_revision"],
                                                safe_path, require, checkpoint=checkpoint,
                                                task_index_path=task_index_path, expected_index=expected_index)
                if args.audit_ready:
                    lifecycle.audit_preflight(root, args.task_id, _lifecycle_ctx(gate), safe_path, require)
                    print('audit-ready: свежая передача и проверки подтверждены; независимый аудит не выполнен')
                return 0 if res["ready"] else 1
            if not args.dry_run and task_cards.version(documents.read_text(task['path'])) == 2:
                lifecycle.sync_criteria_command(project_view.ProjectView(root).path, args.task_id, True, _lifecycle_ctx(gate), safe_path, require, bind=False)
            env = {file_operations.GATE_ENV: gate.token} if gate is not None else None
            return verification.run_v2(root, task, tw_settings, policy["policy_revision"], safe_path,
                                      require, dry_run=args.dry_run, extra_env=env,
                                      reuse=not getattr(args, "no_reuse", False),
                                      checkpoint=checkpoint)

        # Legacy v1 path
        settings = verification.validate(root, data.get("verification"), safe_path, require)
        if args.preflight or args.audit_ready or not args.dry_run:
            file_operations.read_only_gate_check(root, safe_path, require,
                                                  token=gate.token if gate else None)
            lifecycle.verification_preflight(root, args.task_id, _lifecycle_ctx(gate), safe_path, require)
            # The target's stale criterion markers are synchronized by verify
            # below; preflight must remain read-only and accept that transition.
            if args.preflight:
                if not task_index_path.is_file() or task_index_path.read_text(encoding="utf-8") != expected_index:
                    require(False, "Индекс устарел; выполните index")
            check(root, True, skip_criteria={args.task_id})
            verification.readiness(root, settings, safe_path, require, archive=paths['archive'])
        if args.audit_ready:
            lifecycle.audit_preflight(root, args.task_id, _lifecycle_ctx(gate), safe_path, require)
            print('audit-ready: свежая передача и проверки подтверждены; независимый аудит не выполнен')
        if args.preflight or args.audit_ready:
            print('preflight: структура, материалы, блокировки, команды и лимиты проверены; поведение CLI и приёмка не подтверждены')
            return verification.run(root, task, settings, policy['policy_revision'], safe_path,
                                    require, dry_run=True)
        if not args.dry_run and task_cards.version(documents.read_text(task['path'])) == 2:
            lifecycle.sync_criteria_command(project_view.ProjectView(root).path, args.task_id, True, _lifecycle_ctx(gate), safe_path, require, bind=False)
        env = {file_operations.GATE_ENV: gate.token} if gate is not None else None
        return verification.run(root, task, settings, policy["policy_revision"], safe_path,
                                require, dry_run=args.dry_run, extra_env=env, reuse=args.reuse)
    elif args.command == "close":
        close_command(root, args, gate)
    elif args.command == "document":
        data, paths = config(root)
        records = read_tasks(root, paths)
        catalog = documents.load_catalog(root, data, paths, records,
                                         safe_path, require)
        require(catalog is not None,
                "Раздел documents не настроен в project.json")
        if args.action == "impact":
            print(documents.impact_text(catalog, args.document_id, require))
        elif args.action == "materials":
            print(documents.materials_text(catalog, args.owner_id, require))
        elif args.action == "create":
            document_create(root, catalog, args.kind, args.slug, args.title,
                            args.apply, gate)
        elif args.action == "register":
            document_register(root, catalog, args.path, args.metadata,
                              args.apply, gate)
        else:
            print(documents.list_text(catalog))
    elif args.command == "policy":
        _policy_command(root, args, gate)
    elif args.command == "block":
        if args.action == "status":
            _block_status_command(root, args)
        elif args.action in {"prepare-review", "revalidate"}:
            _block_deferred(args.action, "PB-067")
        elif args.action == "create-fix":
            _block_deferred(args.action, "PB-068")
        else:
            _block_deferred(args.action, "PB-069")
    elif args.command == "checks":
        if args.action == "plan":
            checks_plan(root, args)
        elif args.action == "run":
            return checks_run(root, args)
        else:
            return checks_status(root, args)
    elif args.command == "setup":
        _checks_deferred("setup", args.action, "PB-063")
    elif args.command == "adopt":
        updating.adopt(root, args.release_dir,
                       updating.parse_resolves(args.resolve, require),
                       args.apply, args.force, safe_path, require)
    elif args.command == "update":
        if args.action == "plan":
            _, paths = config(root)
            updating.update_plan(root, paths, args.release_dir,
                                 updating.parse_resolves(args.resolve, require),
                                 args.save, safe_path, require)
        elif args.action == "apply":
            _, paths = config(root)
            updating.update_apply(root, paths, safe_path, require)
        elif args.action == "restore":
            updating.update_restore(root, safe_path, require)
        else:
            updating.update_status(root, safe_path, require)
    elif args.command == "lifecycle":
        if args.action == "status":
            lifecycle.status(root, safe_path, require)
        elif args.action == "restore":
            _nested_gate(gate, lambda: lifecycle.restore(root, safe_path,
                                                       require))
        elif args.action == "plan":
            lifecycle.plan_command(root, args.request, args.save,
                                   _lifecycle_ctx(gate), safe_path, require)
        elif args.action == "prepare-audit":
            lifecycle.prepare_audit_command(root, args.task_id, args.audit_id,
                args.snapshot, args.apply, args.actor or _actor(),
                _lifecycle_ctx(gate), safe_path, require)
        elif args.action == "register-material":
            lifecycle.register_material_command(
                root, args.task_id, args.source, args.material_id, args.role,
                args.immutable, args.required, args.apply,
                args.actor or _actor(), _lifecycle_ctx(gate), safe_path, require)
        elif args.action == "relocate-material":
            lifecycle.relocate_material_command(
                root, args.task_id, args.material_id, args.to, args.reason,
                args.apply, args.actor, args.model,
                _lifecycle_ctx(gate), safe_path, require)
        elif args.action in {"start", "route", "revise"}:
            lifecycle.transition_command(root, args.task_id, args.action,
                args.actor or _actor(), args.model, args.reason,
                getattr(args, 'to_route', None), getattr(args, 'engineer_approval', None),
                _lifecycle_ctx(gate), safe_path, require)
        elif args.action == "handoff":
            lifecycle.handoff_command(
                root, args.task_id, args.summary, args.actor or _actor(),
                args.model, args.report, _lifecycle_ctx(gate), safe_path,
                require)
        elif args.action == "accept":
            lifecycle.accept_command(
                root, args.task_id, args.result, args.kind, args.role,
                args.actor or _actor(), args.model, args.note,
                _lifecycle_ctx(gate), safe_path, require)
        else:
            lifecycle.apply_command(root, args.plan, _lifecycle_ctx(gate),
                                    safe_path, require)
    else:
        index_command(root, args, gate)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Корень проверяемого проекта")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("init", help="Предпросмотр установки; запись только с --apply")
    command.add_argument("target", type=Path)
    command.add_argument("--apply", action="store_true")
    command.add_argument("--workflow-v2", action="store_true", help="Явно подключить workflow v2")
    command = sub.add_parser("workflow", help="Явное подключение workflow v2")
    actions = command.add_subparsers(dest="action", required=True)
    action = actions.add_parser("enable-v2", help="Diff настроек; запись только с --apply")
    action.add_argument("--apply", action="store_true")
    action = actions.add_parser("migrate-card", help="Явный перевод metadata v1 с сохранением истории")
    action.add_argument("task_id")
    action.add_argument("--route", choices=("quick", "small", "medium", "large"), required=True)
    action.add_argument("--verification-plan", required=True)
    action.add_argument("--actor", required=True)
    action.add_argument("--model", required=True)
    action.add_argument("--reason", required=True)
    action.add_argument("--apply", action="store_true")
    command = sub.add_parser("check", help="Проверить структуру и ссылки")
    command.add_argument("--ready", action="store_true")
    command = sub.add_parser("card", help="Просмотр версионированной карточки")
    actions = command.add_subparsers(dest="action", required=True)
    action = actions.add_parser("show", help="Markdown либо JSON для приложения")
    action.add_argument("task_id")
    action.add_argument("--json", action="store_true")
    view = action.add_mutually_exclusive_group()
    view.add_argument("--entry", help="ID записи или замечания")
    view.add_argument("--full", action="store_true", help="Весь Markdown без служебного JSON")
    view.add_argument("--handoff", action="store_true", help="Контекст и исходные EXEC/CHECK последней передачи")
    action = actions.add_parser("create", help="Новая карточка и JSON; запись только с --apply")
    action.add_argument("task_id")
    action.add_argument("--title", required=True)
    action.add_argument("--template", choices=("standard", "quick"), default="standard",
                        help="Сокращённый quick требует явно включённый workflow v2")
    action.add_argument("--reason", help="Обоснование quick: локальная цель и низкий риск")
    action.add_argument("--verification-plan", help="Путь в проекте к JSON verification_plan для quick")
    action.add_argument("--apply", action="store_true")
    action = actions.add_parser("sync", help="Пересчитать чекбоксы; запись только с --apply")
    action.add_argument("task_id")
    action.add_argument("--apply", action="store_true")
    command = sub.add_parser("verify", help="Запустить настроенные проверки задачи")
    command.add_argument("task_id")
    preview = command.add_mutually_exclusive_group()
    preview.add_argument("--dry-run", action="store_true", help="Показать план без запуска и записи")
    preview.add_argument("--preflight", action="store_true", help="Проверить материалы и показать план без записи и команд")
    preview.add_argument("--audit-ready", action="store_true", help="Без записи и команд проверить готовность и свежую передачу до аудита")
    command.add_argument("--reuse", action="store_true", help="Повторно использовать только свежий полный passed-отчёт; иначе новый прогон")
    command.add_argument("--no-reuse", action="store_true", help="Отключить reuse для v2 (полный перезапуск всех проверок)")
    command.add_argument("--record-manual", metavar="FILE", help="Зарегистрировать заявление о ручной проверке из JSON-файла")
    command.add_argument("--checkpoint", help="Контрольная точка обязательности (например, merge или release)")
    command = sub.add_parser("close", help="Закрыть задачу: статус, Closed, перенос в архив, индекс")
    command.add_argument("task_id")
    command.add_argument("--status", choices=["done", "cancelled"], default="done")
    command.add_argument("--reason",
                         help="Причина (для cancelled в managed-проекте)")
    command.add_argument("--dry-run", action="store_true",
                         help="Показать план lifecycle-операции без записи")
    command = sub.add_parser("adopt", help="Принять существующую установку под управление")
    command.add_argument("release_dir", type=Path, help="Каталог эталонного артефакта релиза")
    command.add_argument("--resolve", action="append", default=[],
                         metavar="ПУТЬ=release|keep", help="Решение по расходящемуся файлу")
    command.add_argument("--apply", action="store_true", help="Записать manifest и восстановления")
    command.add_argument("--force", action="store_true", help="Перезаписать существующий manifest")
    command = sub.add_parser("update", help="Обновление комплекта по локальному артефакту")
    actions = command.add_subparsers(dest="action", required=True)
    action = actions.add_parser("plan", help="Предпросмотр обновления; запись только с --save")
    action.add_argument("release_dir", type=Path)
    action.add_argument("--resolve", action="append", default=[],
                        metavar="ПУТЬ=release|keep", help="Решение по конфликтному файлу")
    action.add_argument("--save", action="store_true", help="Сохранить план в work/update/plan.json")
    actions.add_parser("apply", help="Применить сохранённый план с резервной копией")
    actions.add_parser("restore", help="Восстановить состояние после прерванного обновления")
    actions.add_parser("status", help="Показать незавершённую операцию обновления")
    command = sub.add_parser("lifecycle",
                             help="Операции жизненного цикла: архивирование, "
                                  "закрытие/возврат карточек, promotion, "
                                  "передача и приёмка")
    actions = command.add_subparsers(dest="action", required=True)
    action = actions.add_parser("plan",
                                help="Построить план по запросу; "
                                     "запись только с --save")
    action.add_argument("request", help="JSON-файл запроса в проекте")
    action.add_argument("--save", action="store_true",
                        help="Сохранить план в work/lifecycle/plans/")
    action = actions.add_parser("apply",
                                help="Применить сохранённый план через "
                                     "восстанавливаемый engine")
    action.add_argument("plan", help="JSON-файл плана в проекте")
    actions.add_parser("status", help="Показать незавершённую операцию и планы")
    actions.add_parser("restore", help="Восстановить прерванную операцию")
    for transition in ('start', 'route', 'revise'):
        action = actions.add_parser(transition, help='Переход workflow v2 одной восстанавливаемой операцией')
        action.add_argument('task_id')
        action.add_argument('--actor', required=True)
        action.add_argument('--model', required=True)
        action.add_argument('--reason', required=True)
        if transition == 'route':
            action.add_argument('--to', dest='to_route', choices=sorted(documents.ROUTES), required=True)
            action.add_argument('--engineer-approval', help='Автор и явное основание согласования понижения')
    action = actions.add_parser('prepare-audit', help='Подготовить переносимый сырой snapshot и зарегистрировать материалы аудита')
    action.add_argument('task_id')
    action.add_argument('--id', dest='audit_id', required=True)
    action.add_argument('--snapshot', help='Исходный текстовый файл; по умолчанию карточка задачи')
    action.add_argument('--actor')
    action.add_argument('--apply', action='store_true')
    action = actions.add_parser("register-material",
                                help="Зарегистрировать локальный материал задачи; preview по умолчанию")
    action.add_argument("task_id")
    action.add_argument("source", help="Путь относительно корня проекта")
    action.add_argument("--id", dest="material_id", required=True)
    action.add_argument("--role", choices=sorted(documents.ROLES), required=True)
    flags = action.add_mutually_exclusive_group(required=True)
    flags.add_argument("--immutable", dest="immutable", action="store_true")
    flags.add_argument("--mutable", dest="immutable", action="store_false")
    flags = action.add_mutually_exclusive_group(required=True)
    flags.add_argument("--required", dest="required", action="store_true")
    flags.add_argument("--optional", dest="required", action="store_false")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--actor")
    action = actions.add_parser("relocate-material",
                                help="Перенести один локальный файл-материал "
                                     "в свободный путь внутри owner-evidence; "
                                     "preview по умолчанию")
    action.add_argument("task_id")
    action.add_argument("--id", dest="material_id", required=True)
    action.add_argument("--to", required=True,
                        help="Новый путь файла внутри docs/tasks/<ID>-evidence/")
    action.add_argument("--reason", required=True)
    action.add_argument("--actor", required=True)
    action.add_argument("--model", required=True)
    action.add_argument("--apply", action="store_true")
    action = actions.add_parser("handoff",
                                help="Записать передачу карточки на приёмку "
                                     "(отчёты verify → materials)")
    action.add_argument("task_id")
    action.add_argument("--summary", required=True,
                        help="Краткое описание переданного результата")
    action.add_argument("--report", action="append", default=[],
                        metavar="ПУТЬ", help="Каталог прогона verify либо "
                        "report.json; по умолчанию — все прогоны задачи")
    action.add_argument("--actor", help="Имя исполнителя (по умолчанию cli)")
    action.add_argument("--model", help="Модель исполнителя")
    action = actions.add_parser("accept",
                                help="Записать приёмку либо "
                                     "диагностический протокол аудита")
    action.add_argument("task_id")
    action.add_argument("--result", required=True,
                        choices=["accepted", "cancelled"],
                        help="Итог приёмки")
    action.add_argument("--kind", default="acceptance",
                        choices=["acceptance", "diagnostic"],
                        help="diagnostic — протокол без статуса приёмки")
    action.add_argument("--role", default="auditor",
                        choices=["auditor", "engineer"],
                        help="роль автора записи")
    action.add_argument("--note", help="Комментарий аудитора")
    action.add_argument("--actor", help="Имя аудитора (по умолчанию cli)")
    action.add_argument("--model", help="Модель аудитора")
    command = sub.add_parser("document",
                             help="Документы: каталог, создание и регистрация")
    actions = command.add_subparsers(dest="action", required=True)
    action = actions.add_parser("create",
                                help="Новый документ по шаблону; запись только с --apply")
    action.add_argument("kind", choices=list(documents.KINDS))
    action.add_argument("slug", help="Имя файла и document_id: [a-z0-9][a-z0-9_-]*")
    action.add_argument("--title", help="Заголовок документа (по умолчанию slug)")
    action.add_argument("--apply", action="store_true",
                        help="Записать документ и индекс через безопасную операцию")
    action = actions.add_parser("register",
                                help="Добавить метаданные в legacy-документ; "
                                     "запись только с --apply")
    action.add_argument("path", help="Markdown-файл в каталоге коллекции")
    action.add_argument("--metadata", required=True,
                        help="JSON-объект метаданных или путь к .json в проекте")
    action.add_argument("--apply", action="store_true",
                        help="Записать метаданные и индекс через безопасную операцию")
    action = actions.add_parser("impact", help="Связи, задачи, изменения и материалы документа")
    action.add_argument("document_id")
    action = actions.add_parser("materials", help="Материалы владельца (документ или карточка)")
    action.add_argument("owner_id")
    actions.add_parser("list", help="Зарегистрированные документы, legacy и кандидаты")
    command = sub.add_parser("policy",
                             help="Политика приёмки блока; запись только с apply")
    actions = command.add_subparsers(dest="action", required=True)
    for verb in ("preview", "apply"):
        action = actions.add_parser(
            verb, help="Проверить и " + ("записать" if verb == "apply"
                                         else "показать diff")
            + " acceptance_policy плана")
        action.add_argument("document_id",
                            help="ID plan-документа (plan:<slug>)")
        action.add_argument("--policy", required=True,
                            help="JSON-объект acceptance_policy либо путь "
                                 "к .json в проекте")
    command = sub.add_parser("block",
                             help="Граф совместной приёмки блока задач")
    actions = command.add_subparsers(dest="action", required=True)
    action = actions.add_parser("status",
                                help="Вычисляемый статус блока; read-only")
    action.add_argument("id", help="plan:<slug> либо ID карточки блока")
    action.add_argument("--json", action="store_true")
    for deferred, owner in (("prepare-review", "PB-067"),
                            ("revalidate", "PB-067"),
                            ("create-fix", "PB-068"),
                            ("accept", "PB-069"),
                            ("close", "PB-069")):
        action = actions.add_parser(
            deferred,
            help=f"Контракт PB-065; операция реализуется {owner}")
        action.add_argument("id", help="ID карточки итогового аудита")
        if deferred == "create-fix":
            action.add_argument("--report", required=True,
                                help="JSON-протокол находок в проекте")
            action.add_argument("--task", required=True,
                                help="ID новой карточки исправлений")
        if deferred in {"create-fix", "accept", "close"}:
            action.add_argument("--apply", action="store_true",
                                help="Записать; без apply — предпросмотр")
        if deferred == "close":
            action.add_argument("--dry-run", action="store_true",
                                help="Проверить конечный граф без записи")
    command = sub.add_parser("checks",
                             help="Выбор и запуск проверок по зависимостям; "
                                  "контракт PB-057")
    actions = command.add_subparsers(dest="action", required=True)
    action = actions.add_parser(
        "plan", help="Read-only объяснение required/reused/run/blocked "
                     "и стоимости (PB-059)")
    action.add_argument("task_id")
    action = actions.add_parser(
        "run", help="Выполнить обязательства области; операция "
                    "реализуется PB-060")
    action.add_argument("task_id")
    action.add_argument("--scope", choices=("task", "project"),
                        default="task",
                        help="область: фактические изменения задачи "
                             "(task) либо весь проект (project)")
    action.add_argument("--mode", choices=("stale", "force"),
                        default="stale",
                        help="stale — только обязательства без пригодного "
                             "результата; force — весь набор области заново")
    action.add_argument("--checkpoint",
                        help="контрольная точка обязательности "
                             "(например, release); потребление — PB-062")
    action = actions.add_parser(
        "status", help="Покрытие, фактические проверки и ограничения "
                       "доверия; операция реализуется PB-060")
    action.add_argument("task_id")
    command = sub.add_parser("setup",
                             help="Обследование проекта и одобрение "
                                  "политики проверок; контракт PB-057")
    actions = command.add_subparsers(dest="action", required=True)
    actions.add_parser(
        "preview", help="Обследование, неизвестные пути и diff политики "
                        "без записи; операция реализуется PB-063")
    actions.add_parser(
        "apply", help="Запись одобренной политики; операция "
                      "реализуется PB-063")
    command = sub.add_parser("index", help="Обновить индекс задач")
    command.add_argument("--documents", action="store_true",
                         help="Обновить только индексы коллекций документов")
    command.add_argument("--all", action="store_true",
                         help="Обновить индексы задач и документов")
    args = parser.parse_args(argv)
    try:
        require(not args.root.is_symlink(), "Корень проекта — символическая ссылка")
        root = args.root.resolve()
        pending = updating.pending_info(root)
        lifecycle_pending = file_operations.pending_info(root)
        allowed = (args.command in {"update", "lifecycle"}
                   and args.action in {"restore", "status"})
        require(pending is None or allowed,
                "Незавершённое обновление комплекта: обычные команды заблокированы. "
                "Выполните playbook.py update restore или playbook.py update status")
        require(lifecycle_pending is None or allowed,
                file_operations.pending_hint(root, file_operations.PENDING_FILE))
        gate = command_gate(root, args)
        try:
            return dispatch(root, args, gate)
        finally:
            if gate is not None:
                gate.close()
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
