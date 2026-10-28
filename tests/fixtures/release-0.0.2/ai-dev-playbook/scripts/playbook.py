#!/usr/bin/env python3
"""Portable playbook tools. Python 3.10+, standard library only."""

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

# Support both direct CLI execution and importlib-based tests.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import updating
import verification


CORE = "ai-dev-playbook"
STATUSES = {"draft", "ready", "in_progress", "in_review", "done", "blocked", "cancelled"}
AVAILABILITY = {"reported", "confirmed", "unavailable", "unknown"}
ID_PATTERN = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-[0-9]+\Z")
META = re.compile(
    r"\A# ([^\n]+)\n\s*\nID: ([^\n]+)\nStatus: ([^\n]+)\n"
    r"Priority: ([^\n]+)\nDepends-On: ([^\n]+)"
    r"(?:\nExecutor: ([^\n]+))?(?:\nModel: ([^\n]+))?"
    r"\nCreated: ([^\n]+)\nClosed: ([^\n]+)(?:\n|$)"
)
STAMP_UNKNOWN = "unknown"
STAMP_UNSET = "-"
EPOCH = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
MODEL_UNCONFIRMED = "(не подтверждено)"
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
INLINE_CODE = re.compile(r"(`+)(?!`)[^\n]*?(?<!`)\1(?!`)")


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
    require(data.get("schema_version") == 1, "Неподдерживаемая схема project.json")
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
            match = META.match(path.read_text(encoding="utf-8"))
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
            records[ident] = dict(id=ident, title=title, status=status, priority=priority,
                                  deps=deps, executor=normalize(executor), model=normalize(model),
                                  created=created_v, closed=closed_v,
                                  created_ts=created_ts, closed_ts=closed_ts,
                                  path=path, archived=key == "archive")
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
        return Path(os.path.relpath(task["path"], index_dir)).as_posix()

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
        require(index.read_text(encoding="utf-8").startswith(
            "# Индекс задач\n\nСгенерировано playbook.py; статусы изменять в карточках.\n"),
            "Конфликт: индекс указывает на чужой документ; запись отменена")
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(render_index(records, index.parent), encoding="utf-8")


LINK_INLINE = re.compile(r"(?<!!)(\[[^\n]*?\]\()([^\s)]+)(\))")
LINK_DEF = re.compile(r"^ {0,3}(\[[^\]\n]+\]:[ \t]*)(<[^>\n]+>|[^\s]+)", re.M)


def rewrite_links(text, fix):
    text = LINK_INLINE.sub(lambda m: m.group(1) + fix(m.group(2)) + m.group(3), text)
    return LINK_DEF.sub(lambda m: m.group(1) + fix(m.group(2)), text)


def relocate_links(text, old_path, new_path):
    """Пересчитать локальные ссылки текста при переносе документа old_path → new_path."""
    old_dir, new_dir = old_path.parent, new_path.parent
    old_resolved, new_resolved = old_path.resolve(), new_path.resolve()

    def fix(target):
        link = urlsplit(target.strip("<>"))
        if link.scheme or link.netloc or not link.path:
            return target
        dest = (old_dir / unquote(link.path)).resolve()
        if dest == old_resolved:
            dest = new_resolved
        rel = Path(os.path.relpath(dest, new_dir)).as_posix()
        rel += f"#{link.fragment}" if link.fragment else ""
        return f"<{rel}>" if target.startswith("<") else rel
    return rewrite_links(text, fix)


def codeless_lines(text):
    """(номер, строка без инлайн-кода) для строк вне fenced-блоков."""
    result, fence = [], None
    for number, line in enumerate(text.splitlines(), 1):
        match = FENCE.match(line)
        if fence is not None:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
            continue
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
    old_resolved, new_resolved = old_path.resolve(), new_path.resolve()
    originals, updates = {}, {}
    for doc in docs:
        if doc in {old_path, new_path, paths["task_index"]}:
            continue

        def fix(target):
            link = urlsplit(target.strip("<>"))
            if link.scheme or link.netloc or not link.path:
                return target
            if (doc.parent / unquote(link.path)).resolve() != old_resolved:
                return target
            rel = Path(os.path.relpath(new_resolved, doc.parent)).as_posix()
            rel += f"#{link.fragment}" if link.fragment else ""
            return f"<{rel}>" if target.startswith("<") else rel

        text = doc.read_text(encoding="utf-8")
        rewritten = rewrite_links(text, fix)
        if rewritten != text:
            originals[doc], updates[doc] = text, rewritten
    return originals, updates


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


def check_links(root, paths):
    docs = list(paths["docs"].rglob("*.md"))
    docs += list((root / CORE / "rules").glob("*.md"))
    docs += [root / "AGENTS.md", root / CORE / "README.md"]
    if (root / "README.md").exists():
        docs.append(root / "README.md")
    errors = []
    for path in docs:
        safe_path(root, str(path.relative_to(root)))
        text = path.read_text(encoding="utf-8")
        text = "\n".join(line for _, line in codeless_lines(text))
        text = re.sub(r"(`+)(?!`).*?(?<!`)\1(?!`)", "", text, flags=re.S)
        definitions = re.findall(r'^ {0,3}\[([^\]\n]+)\]:[ \t]*(<[^>\n]+>|[^\s]+)', text, re.M)
        normalize = lambda label: " ".join(label.split()).casefold()
        labels = {normalize(label) for label, _ in definitions}
        for label, reference in re.findall(r'(?<!!)\[([^\]\n]+)\]\[([^\]\n]*)\]', text):
            if normalize(reference or label) not in labels:
                errors.append(f"{path.relative_to(root)}: нет определения ссылки {reference or label}")
        targets = re.findall(r"(?<!!)\[[^\n]*?\]\(([^\s)]+)\)", text)
        targets.extend(target for _, target in definitions)
        for target in targets:
            link = urlsplit(target.strip("<>"))
            if link.scheme or link.netloc or not link.path:
                continue
            destination = (path.parent / unquote(link.path)).resolve()
            if not destination.is_relative_to(root.resolve()) or not destination.exists():
                errors.append(f"{path.relative_to(root)}: битая или внешняя локальная ссылка {target}")
    require(not errors, "\n".join(errors))


def check(root, ready):
    data, paths = config(root)
    resources(root)
    records = read_tasks(root, paths)
    require(paths["task_index"].is_file(), "Нет индекса; выполните index")
    require(paths["task_index"].read_text(encoding="utf-8") == render_index(records, paths["task_index"].parent),
            "Индекс устарел; выполните index")
    check_links(root, paths)
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
    print(f"OK: структура, ссылки и {len(records)} карточки. Поведение продукта не проверялось.")


def install(source, target, apply):
    require(not target.is_symlink(), "Каталог назначения — символическая ссылка")
    target = target.resolve()
    require(not target.exists() or target.is_dir(), "Назначение должно быть каталогом")
    require(not source.is_relative_to(target), "Нельзя устанавливать поверх исходного комплекта или его родителя")
    base = source / CORE
    managed = updating.managed_files(source, safe_path, require)
    version = updating.parse_version(managed[f"{CORE}/VERSION"].decode("utf-8").strip(), require)
    version_text = ".".join(str(part) for part in version)
    planned = dict(managed)
    for filename in ("project.json", "resources.json"):
        template = json.loads((base / "templates" / filename).read_text(encoding="utf-8"))
        if filename == "project.json":
            template["playbook_version"] = version_text
        planned[f"{CORE}/{filename}"] = \
            (json.dumps(template, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    for filename in ("AGENTS.md", "CHANGELOG.md"):
        planned[filename] = (base / "templates" / filename).read_bytes()
    for path in sorted((base / "templates/docs").rglob("*.md")):
        planned[f"docs/{path.relative_to(base / 'templates/docs')}"] = path.read_bytes()
    planned["docs/tasks/index.md"] = render_index({}).encode("utf-8")
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
        require(not path.exists(), f"Конфликт: {relative}. Запись не начата; требуется ручное объединение.")
        for parent in path.parents:
            if parent == target:
                break
            require(not parent.exists() or parent.is_dir(), f"Родитель не является каталогом: {parent}")
        destinations.append((path, content))
    if not apply:
        print("Предпросмотр, без записи:\n" + "\n".join(str(p.relative_to(target)) for p, _ in destinations))
        return
    created = []
    try:
        for path, content in destinations:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                created.append(path)
                stream.write(content)
    except OSError:
        for path in reversed(created):
            path.unlink()
        raise
    print(f"Создано {len(created)} файлов в {target}; manifest записан. "
          f"Заполните документы и project.json; setup_status=needs_review.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Корень проверяемого проекта")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("init", help="Предпросмотр установки; запись только с --apply")
    command.add_argument("target", type=Path)
    command.add_argument("--apply", action="store_true")
    command = sub.add_parser("check", help="Проверить структуру и ссылки")
    command.add_argument("--ready", action="store_true")
    command = sub.add_parser("verify", help="Запустить настроенные проверки задачи")
    command.add_argument("task_id")
    command.add_argument("--dry-run", action="store_true", help="Показать план без запуска и записи")
    command = sub.add_parser("close", help="Закрыть задачу: статус, Closed, перенос в архив, индекс")
    command.add_argument("task_id")
    command.add_argument("--status", choices=["done", "cancelled"], default="done")
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
    sub.add_parser("index", help="Обновить индекс задач")
    args = parser.parse_args(argv)
    try:
        require(not args.root.is_symlink(), "Корень проекта — символическая ссылка")
        root = args.root.resolve()
        pending = updating.pending_info(root)
        allowed = args.command == "update" and args.action in {"restore", "status"}
        require(pending is None or allowed,
                "Незавершённое обновление комплекта: обычные команды заблокированы. "
                "Выполните playbook.py update restore или playbook.py update status")
        if args.command == "init":
            install(Path(__file__).resolve().parents[2], args.target, args.apply)
        elif args.command == "check":
            check(root, args.ready)
        elif args.command == "verify":
            data, paths = config(root)
            policy = resources(root)
            require(data["setup_status"] == "configured", "Первичная настройка не завершена")
            records = read_tasks(root, paths)
            require(args.task_id in records, f"Нет задачи: {args.task_id}")
            task = records[args.task_id]
            require(not task["archived"] and task["status"] in {"ready", "in_progress", "in_review"},
                    "verify требует активную задачу ready/in_progress/in_review")
            settings = verification.validate(root, data.get("verification"), safe_path, require)
            return verification.run(root, task, settings, policy["policy_revision"], safe_path, require,
                                    dry_run=args.dry_run)
        elif args.command == "close":
            close_task(root, args.task_id, args.status)
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
        else:
            _, paths = config(root)
            write_index(root, paths)
            print("Индекс обновлён.")
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
