"""Managed-kit update mechanics: manifest, adopt, plan, apply, restore.

Pure standard library. Commands in playbook.py delegate here; previews
never write to the project. Contract: ai-dev-playbook/rules/update.md.
"""

import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

CORE = "ai-dev-playbook"
MANIFEST_SCHEMA = 1
PLAN_SCHEMA = 1
RELEASE_SCHEMA = 1
UPDATES_SCHEMA = 2
MANIFEST_PATH = CORE + "/manifest.json"
UPDATES_PATH = CORE + "/updates.json"
RELEASE_FILE = "release.json"
SETTINGS = (CORE + "/project.json", CORE + "/resources.json")
RESERVED_PATHS = frozenset(SETTINGS) | {MANIFEST_PATH}
# Обязательные файлы ядра по контракту rules/update.md.
CORE_REQUIRED = (CORE + "/VERSION", CORE + "/README.md", UPDATES_PATH)
WORK_DIR = "work/update"
PLAN_FILE = WORK_DIR + "/plan.json"
PENDING_FILE = WORK_DIR + "/pending.json"
LOCK_FILE = WORK_DIR + "/lock"
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
STAGED_IGNORE = {".git", "work", "__pycache__", ".DS_Store", ".venv", "node_modules"}
STAGED_ROOT_IGNORE = {".git", "work", ".venv", "node_modules"}
STAGED_ANY_IGNORE = {"__pycache__", ".DS_Store"}
# Декларативные операции миграций, объявляемые релизом в updates_from.
MIGRATION_OPS = {"json_set", "json_delete", "json_rename", "note"}


def sha256(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def fingerprint(hashes):
    return sha256(json.dumps(hashes, sort_keys=True).encode("utf-8"))


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def parse_version(text, require):
    require(isinstance(text, str) and VERSION_PATTERN.fullmatch(text.strip()) is not None,
            f"Некорректный номер версии: {text!r}")
    return tuple(int(part) for part in text.strip().split("."))


def load_json(path, require, what):
    data = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(data, dict), f"{what}: ожидается JSON-объект")
    return data


def managed_files(kit_root, safe_path, require):
    """{relposix: bytes} управляемого ядра комплекта внутри kit_root."""
    base = kit_root / CORE
    require(base.is_dir(), f"Нет каталога комплекта {CORE}/ в {kit_root}")
    candidates = [base / "VERSION", base / "README.md", base / "updates.json"]
    for subdir, pattern in (("scripts", "*.py"), ("rules", "*.md"), ("templates", "*")):
        directory = base / subdir
        if not directory.is_dir():
            continue
        candidates += sorted(directory.rglob(pattern))
    files = {}
    for path in candidates:
        rel = path.relative_to(kit_root).as_posix()
        require(not path.is_symlink(), f"Символическая ссылка в составе комплекта: {rel}")
        if not path.is_file() or any(part.startswith(".") for part in Path(rel).parts):
            continue
        safe_path(kit_root, rel)
        files[rel] = path.read_bytes()
    require(CORE + "/VERSION" in files, f"В комплекте нет {CORE}/VERSION")
    return files


def card_spec(template_text):
    """Отпечаток формата карточки: упорядоченные поля заголовка шаблона задачи."""
    keys = []
    for line in template_text.splitlines():
        if line.startswith("## "):
            break
        match = re.match(r"([A-Za-z-]+):", line)
        if match:
            keys.append(match.group(1))
    return sha256("\n".join(keys).encode("utf-8"))


def load_artifact(directory, safe_path, require):
    """Каталог-артефакт релиза: ai-dev-playbook/ + необязательный release.json."""
    root = Path(directory)
    require(not root.is_symlink(), f"Артефакт — символическая ссылка: {directory}")
    require(root.is_dir(), f"Артефакт должен быть каталогом: {directory}")
    root = root.resolve()
    files = managed_files(root, safe_path, require)
    hashes = {rel: sha256(content) for rel, content in files.items()}
    version_text = files[CORE + "/VERSION"].decode("utf-8").strip()
    version = parse_version(version_text, require)
    data_format = {}
    for rel, key in ((CORE + "/templates/project.json", "project_schema"),
                     (CORE + "/templates/resources.json", "resources_schema")):
        if rel in files:
            data = json.loads(files[rel].decode("utf-8"))
            require(isinstance(data, dict) and isinstance(data.get("schema_version"), int),
                    f"{rel}: нет числовой schema_version")
            data_format[key] = data["schema_version"]
    task_template = CORE + "/templates/task.md"
    if task_template in files:
        data_format["card_format"] = card_spec(files[task_template].decode("utf-8"))
    updates_from = {}
    if UPDATES_PATH in files:
        data = json.loads(files[UPDATES_PATH].decode("utf-8"))
        require(isinstance(data, dict) and data.get("schema_version") in {1, UPDATES_SCHEMA},
                f"{UPDATES_PATH}: неподдерживаемая схема")
        if data["schema_version"] == 2:
            minimum = parse_version(data.get("minimum_cli_version"), require)
            cli_version = parse_version((Path(__file__).resolve().parents[1] / "VERSION")
                                        .read_text(encoding="utf-8").strip(), require)
            require(cli_version >= minimum,
                    f"Артефакт требует CLI >= {data['minimum_cli_version']}; используйте CLI целевого артефакта")
            schemas = data.get("supported_project_schemas", [data_format.get("project_schema")])
            require(isinstance(schemas, list) and schemas and all(type(v) is int and v > 0 for v in schemas),
                    "updates.json: supported_project_schemas должен содержать номера схем")
            data_format["project_schemas"] = schemas
        declared = data.get("updates_from") or {}
        require(isinstance(declared, dict), f"{UPDATES_PATH}: updates_from должен быть объектом")
        updates_from = declared
    provenance = "checkout"
    release_meta = root / RELEASE_FILE
    if release_meta.exists():
        require(release_meta.is_file() and not release_meta.is_symlink(),
                "release.json должен быть обычным файлом")
        meta = load_json(release_meta, require, "release.json")
        require(meta.get("schema_version") == RELEASE_SCHEMA,
                "release.json: неподдерживаемая схема")
        require(meta.get("version") == version_text,
                "release.json: версия не совпадает с VERSION")
        require(meta.get("files") == hashes,
                "release.json: состав или хеши не совпадают с содержимым артефакта")
        require(meta.get("release_id") == fingerprint(hashes),
                "release.json: release_id не совпадает с содержимым")
        declared = meta.get("updates_from") or {}
        require(isinstance(declared, dict) and declared == updates_from,
                "release.json: updates_from не совпадает с updates.json")
        provenance = "release"
    return {"root": root, "files": files, "hashes": hashes,
            "version": version_text, "version_tuple": version,
            "release_id": fingerprint(hashes), "data_format": data_format,
            "updates_from": updates_from, "provenance": provenance}


def is_managed_path(rel):
    """True, если путь может входить в управляемое ядро по managed_files."""
    if not rel.startswith(CORE + "/"):
        return False
    parts = rel.split("/")[1:]
    if any(part.startswith(".") for part in parts):
        return False
    head, name = parts[0], parts[-1]
    if len(parts) == 1:
        return name in ("VERSION", "README.md", "updates.json")
    if head == "scripts":
        return name.endswith(".py")
    if head == "rules":
        return name.endswith(".md")
    return head == "templates"


def read_manifest(root, safe_path, require):
    path = safe_path(root, MANIFEST_PATH)
    require(path.is_file(),
            f"Нет {MANIFEST_PATH}: установка предшествует механизму обновления; "
            f"выполните adopt для принятия под управление")
    data = load_json(path, require, MANIFEST_PATH)
    require(data.get("schema_version") == MANIFEST_SCHEMA,
            "Неподдерживаемая схема manifest")
    version_text = data.get("playbook_version")
    version = parse_version(version_text, require)
    release_id = data.get("release_id")
    require(isinstance(release_id, str) and HASH_PATTERN.fullmatch(release_id) is not None,
            "manifest: некорректный release_id")
    files = data.get("files")
    require(isinstance(files, dict) and files, "manifest: files должен быть непустым объектом")
    reserved = sorted(set(files) & RESERVED_PATHS)
    require(not reserved,
            "manifest: зарезервированные пути не могут быть управляемыми: "
            + ", ".join(reserved))
    for rel, digest in files.items():
        require(is_managed_path(rel),
                f"manifest: путь вне канонического состава ядра: {rel}")
        safe_path(root, rel)
        require(isinstance(digest, str) and HASH_PATTERN.fullmatch(digest) is not None,
                f"manifest: некорректный хеш {rel}")
    for rel in CORE_REQUIRED:
        require(rel in files, f"manifest: нет обязательного файла ядра: {rel}")
    require(fingerprint(files) == release_id,
            "manifest: release_id не соответствует составу files — manifest повреждён; "
            "восстановите его командой adopt <эталон> --force")
    kept = data.get("kept") or []
    require(isinstance(kept, list) and all(item in files for item in kept),
            "manifest: kept должен ссылаться на управляемые файлы")
    return {"playbook_version": version_text, "version_tuple": version,
            "release_id": release_id, "files": files, "kept": sorted(kept),
            "installed_at": data.get("installed_at"), "origin": data.get("origin")}


def write_manifest(root, manifest):
    path = root / MANIFEST_PATH
    payload = {"schema_version": MANIFEST_SCHEMA,
               "playbook_version": manifest["playbook_version"],
               "release_id": manifest["release_id"],
               "installed_at": manifest.get("installed_at") or now_iso(),
               "origin": manifest.get("origin"),
               "files": dict(sorted(manifest["files"].items()))}
    if manifest.get("kept"):
        payload["kept"] = sorted(manifest["kept"])
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def current_hash(root, rel, safe_path, require):
    """sha256 текущего содержимого либо 'missing'."""
    path = safe_path(root, rel)
    if not path.exists():
        return "missing"
    require(path.is_file(), f"Ожидается обычный файл: {rel}")
    return sha256(path.read_bytes())


def state_fingerprint(root, paths, manifest, safe_path, require, extra=()):
    """Отпечаток входов плана: управляемые файлы, manifest, настройки, AGENTS.md, docs.

    extra — дополнительные затрагиваемые пути (целевые файлы релиза): их
    появление, изменение или исчезновение после предпросмотра тоже
    инвалидирует план.
    """
    parts = {}
    for rel in manifest["files"]:
        parts[rel] = current_hash(root, rel, safe_path, require)
    for rel in extra:
        if rel not in parts:
            parts[rel] = current_hash(root, rel, safe_path, require)
    for rel in (MANIFEST_PATH, *SETTINGS, "AGENTS.md"):
        parts[rel] = current_hash(root, rel, safe_path, require)
    for doc in sorted(paths["docs"].rglob("*.md")):
        rel = doc.relative_to(root).as_posix()
        parts[rel] = current_hash(root, rel, safe_path, require)
    return fingerprint(parts)


def unknown_kit_files(root, managed, safe_path):
    """Файлы внутри ai-dev-playbook/, не входящие ни в ядро, ни в настройки/manifest."""
    base = root / CORE
    known = set(managed) | set(SETTINGS) | {MANIFEST_PATH}
    result = []
    if base.is_dir():
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            if rel not in known and "__pycache__" not in path.parts:
                result.append(rel)
    return result


def parse_resolves(items, require):
    resolves = {}
    for item in items or []:
        path, sep, choice = item.rpartition("=")
        require(sep and choice in {"release", "keep"} and path.strip(),
                f"--resolve ожидает <путь>=release|keep: {item}")
        resolves[path.strip()] = choice
    return resolves


def pending_info(root):
    path = root / PENDING_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"op_id": "unknown"}
    data["path"] = path
    return data


def _require_no_lifecycle_pending(root, require):
    """Отказ при маркере операции документов: подсистемы не перемешиваются."""
    import file_operations
    pending = file_operations.pending_info(root)
    require(pending is None,
            file_operations.pending_hint(root, file_operations.PENDING_FILE))


def _journal(op_dir, event, **fields):
    line = json.dumps({"ts": now_iso(), "event": event, **fields}, ensure_ascii=False)
    with (op_dir / "journal.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_file(source, target):
    """Атомарная запись одного файла: временный файл + replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".upd-tmp")
    shutil.copyfile(source, temporary)
    temporary.replace(target)


def _path_get(document, key):
    node = document
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _path_set(document, key, value, require):
    node = document
    parts = key.split(".")
    walked = []
    for part in parts[:-1]:
        walked.append(part)
        if part not in node:
            node[part] = {}
        child = node[part]
        require(isinstance(child, dict),
                f"коллизия миграции: {key}: путь {'.'.join(walked)} занят "
                f"не-объектом — запись отклонена, чтобы не потерять значение")
        node = child
    node[parts[-1]] = value


def _path_delete(document, key):
    node = document
    parts = key.split(".")
    for part in parts[:-1]:
        node = node.get(part)
        if not isinstance(node, dict):
            return
    if isinstance(node, dict):
        node.pop(parts[-1], None)


def validate_steps(steps, require):
    require(isinstance(steps, list), "steps должен быть списком")
    for index, step in enumerate(steps):
        require(isinstance(step, dict), f"steps[{index}]: шаг должен быть объектом")
        op = step.get("op")
        require(op in MIGRATION_OPS, f"steps[{index}]: неизвестная операция {op!r}")
        if op == "note":
            require(isinstance(step.get("text"), str) and step["text"].strip(),
                    f"steps[{index}]: note требует text")
            continue
        require(step.get("file") in SETTINGS,
                f"steps[{index}]: file должен быть одним из {SETTINGS}")
        require(isinstance(step.get("key"), str) and step["key"].strip(),
                f"steps[{index}]: требуется key")
        if op == "json_set":
            require("value" in step, f"steps[{index}]: json_set требует value")
        if op == "json_rename":
            require(isinstance(step.get("to"), str) and step["to"].strip(),
                    f"steps[{index}]: json_rename требует to")


def migrate_settings(content, steps, target_version, require):
    """Применить декларативные шаги к JSON-тексту настройки; неизвестные поля сохраняются."""
    data = json.loads(content)
    require(isinstance(data, dict), "Настройка должна быть JSON-объектом")
    for step in steps:
        op = step["op"]
        if op == "json_set":
            _path_set(data, step["key"], step["value"], require)
        elif op == "json_delete":
            _path_delete(data, step["key"])
        elif op == "json_rename":
            key, to = step["key"], step["to"]
            key_parts, to_parts = key.split("."), to.split(".")
            require(key_parts[:len(to_parts)] != to_parts
                    and to_parts[:len(key_parts)] != key_parts,
                    f"json_rename: пути {key} и {to} пересекаются — "
                    f"перенос неоднозначен и отклонён")
            found, value = _path_get(data, key)
            done, existing = _path_get(data, to)
            if found:
                require(not done or existing == value,
                        f"json_rename: {to} уже содержит другое значение — "
                        f"коллизия требует ручного разрешения, перенос отклонён")
                _path_delete(data, key)
                if not done:
                    _path_set(data, to, value, require)
            else:
                require(done, f"json_rename: нет ни {key}, ни {to}")
    return data


def _settings_target_schema(artifact, key):
    return artifact["data_format"].get(key)


def _settings_step_files(steps):
    result = {}
    for step in steps:
        if step["op"] != "note":
            result.setdefault(step["file"], []).append(step)
    return result


def plan_migrations(root, artifact, steps, safe_path, require):
    """Вычислить целевое содержимое настроек на основе установленных данных.

    Возвращает (staged_settings {rel: text}, summaries, manual_notes).
    """
    by_file = _settings_step_files(steps)
    staged, summaries, notes = {}, [], []
    schema_keys = {SETTINGS[0]: "project_schema", SETTINGS[1]: "resources_schema"}
    for rel in SETTINGS:
        path = safe_path(root, rel)
        if not path.is_file():
            continue
        before = path.read_text(encoding="utf-8")
        data = json.loads(before)
        require(isinstance(data, dict), f"{rel}: ожидается JSON-объект")
        from_schema = data.get("schema_version")
        file_steps = by_file.get(rel, [])
        migrated = migrate_settings(before, file_steps, artifact["version"], require)
        if rel == SETTINGS[0]:
            migrated["playbook_version"] = artifact["version"]
        after = json.dumps(migrated, ensure_ascii=False, indent=2) + "\n"
        target_schema = _settings_target_schema(artifact, schema_keys[rel])
        if target_schema is not None:
            supported = (artifact["data_format"].get("project_schemas", [target_schema])
                         if rel == SETTINGS[0] else [target_schema])
            require(migrated.get("schema_version") in supported,
                    f"{rel}: миграция не достигла схемы {target_schema} "
                    f"(получено {migrated.get('schema_version')})")
        if after != before:
            staged[rel] = after
            diff = "".join(difflib.unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile=f"a/{rel}", tofile=f"b/{rel}"))
            summaries.append({"file": rel, "from_schema": from_schema,
                              "to_schema": migrated.get("schema_version"),
                              "steps": len(file_steps), "diff": diff})
        elif file_steps:
            summaries.append({"file": rel, "from_schema": from_schema,
                              "to_schema": migrated.get("schema_version"),
                              "steps": len(file_steps), "diff": ""})
    notes = [step["text"] for step in steps if step["op"] == "note"]
    return staged, summaries, notes


def _build_plan(root, paths, artifact, manifest, resolves, safe_path, require):
    installed_version = manifest["playbook_version"]
    iv, tv = manifest["version_tuple"], artifact["version_tuple"]
    steps = []
    transition = {"from": installed_version, "to": artifact["version"], "steps": []}
    if tv < iv:
        require(False,
                f"Переход {installed_version} → {artifact['version']} является downgrade; "
                f"обновление downgrade не поддерживает — используйте adopt с эталонным артефактом")
    if tv > iv:
        entry = artifact["updates_from"].get(installed_version)
        require(entry is not None,
                f"Неподдерживаемый переход {installed_version} → {artifact['version']}: "
                f"артефакт не объявляет updates_from для {installed_version}")
        require(isinstance(entry, dict), "updates_from: запись перехода должна быть объектом")
        steps = entry.get("steps") or []
        validate_steps(steps, require)
        transition = {"from": installed_version, "to": artifact["version"], "steps": steps}
    elif artifact["release_id"] != manifest["release_id"]:
        require(False,
                f"Неоднозначный релиз: номер {installed_version} совпадает, "
                f"но содержимое отличается (release_id {artifact['release_id']}). "
                f"Переход по одному номеру не выполняется — проверьте происхождение "
                f"артефакта или используйте adopt")
    a_set, m_set = set(artifact["hashes"]), set(manifest["files"])
    kept = set(manifest["kept"])
    adds = sorted(a_set - m_set)
    removes = sorted(m_set - a_set)
    replaces = sorted(p for p in a_set & m_set
                      if artifact["hashes"][p] != manifest["files"][p])
    conflicts, unknown = [], unknown_kit_files(root, m_set | a_set, safe_path)
    for rel in adds + replaces:
        if rel in kept:
            continue
        current = current_hash(root, rel, safe_path, require)
        if rel not in m_set:
            if current != "missing":
                conflicts.append({"path": rel, "state": "collision",
                                  "resolution": resolves.get(rel)})
        elif current == "missing" or current != manifest["files"][rel]:
            state = "missing" if current == "missing" else "modified"
            conflicts.append({"path": rel, "state": state,
                              "resolution": resolves.get(rel)})
    for rel in removes:
        if rel in kept:
            continue
        current = current_hash(root, rel, safe_path, require)
        if current == "missing":
            continue
        if current != manifest["files"][rel]:
            conflicts.append({"path": rel, "state": "modified-remove",
                              "resolution": resolves.get(rel)})
    drift = []
    for rel in sorted(m_set & a_set - set(replaces)):
        if rel in kept:
            continue
        current = current_hash(root, rel, safe_path, require)
        if current == "missing":
            conflicts.append({"path": rel, "state": "missing",
                              "resolution": resolves.get(rel)})
        elif current != manifest["files"][rel]:
            drift.append(rel)
    extra = {path: choice for path, choice in resolves.items()
             if all(c["path"] != path for c in conflicts)}
    require(not extra,
            "Разрешения указаны для путей без конфликта: " + ", ".join(sorted(extra)))
    staged_settings, migrations, notes = plan_migrations(
        root, artifact, steps, safe_path, require)
    installed_task = root / CORE / "templates" / "task.md"
    artifact_spec = artifact["data_format"].get("card_format")
    if artifact_spec and installed_task.is_file():
        installed_spec = card_spec(installed_task.read_text(encoding="utf-8"))
        if installed_spec != artifact_spec:
            notes.append("Формат карточек изменился: сверьте карточки задач с новым шаблоном")
    agents_template = CORE + "/templates/AGENTS.md"
    agents_local = root / "AGENTS.md"
    if agents_template in artifact["files"] and agents_local.is_file():
        if artifact["files"][agents_template] != agents_local.read_bytes():
            notes.append("Шаблон AGENTS.md изменился: проектные инструкции сохраняются, "
                         "проверьте подключение правил вручную")
    changes = {"add": adds, "replace": replaces, "remove": removes}
    empty = not any(changes.values()) and not staged_settings and not conflicts
    keep_resolved = {c["path"] for c in conflicts if c["resolution"] == "keep"}
    unmanaged = sorted((kept | keep_resolved) & set(removes))
    before = state_fingerprint(root, paths, manifest, safe_path, require,
                               extra=set(artifact["hashes"]))
    return {"schema_version": PLAN_SCHEMA, "created_at": now_iso(), "root": str(root),
            "installed": {"version": installed_version, "release_id": manifest["release_id"]},
            "artifact": {"root": str(artifact["root"]), "version": artifact["version"],
                         "release_id": artifact["release_id"],
                         "provenance": artifact["provenance"]},
            "transition": transition, "changes": changes,
            "kept": sorted(kept & a_set), "unmanaged_kept": unmanaged,
            "drift": drift, "conflicts": conflicts,
            "migrations": [{"file": s["file"], "from_schema": s["from_schema"],
                            "to_schema": s["to_schema"], "steps": s["steps"]}
                           for s in migrations],
            "migration_diffs": {s["file"]: s["diff"] for s in migrations if s["diff"]},
            "staged_settings": staged_settings, "manual_notes": notes,
            "unknown_files": unknown, "empty": empty,
            "before_fingerprint": before}


def _print_plan(plan):
    art = plan["artifact"]
    print(f"Артефакт: {art['root']} ({art['provenance']}), "
          f"версия {art['version']}, release_id {art['release_id'][:19]}…")
    print(f"Установлено: {plan['installed']['version']}, "
          f"release_id {plan['installed']['release_id'][:19]}…")
    tr = plan["transition"]
    print(f"Переход: {tr['from']} → {tr['to']}, шагов миграции: {len(tr['steps'])}")
    changes = plan["changes"]
    marks = {"add": "+", "replace": "~", "remove": "-"}
    names = {"add": "Добавления", "replace": "Замены", "remove": "Удаления"}
    for key in ("add", "replace", "remove"):
        if changes[key]:
            print(f"{names[key]} ({len(changes[key])}):")
            for rel in changes[key]:
                print(f"  {marks[key]} {rel}")
    for conflict in plan["conflicts"]:
        decision = conflict["resolution"] or "не разрешён"
        print(f"  ! конфликт {conflict['path']} — {conflict['state']} (решение: {decision})")
    if plan["kept"]:
        print("Сохраняемые расхождения (kept):")
        for rel in plan["kept"]:
            print(f"  = {rel}")
    for rel in plan["unmanaged_kept"]:
        print(f"  = {rel} — выходит из управления, файл сохраняется")
    for rel in plan["drift"]:
        print(f"  ~ {rel} — изменён локально, обновлением не затрагивается")
    if plan["migrations"]:
        print("Миграции:")
        for item in plan["migrations"]:
            print(f"  {item['file']}: schema {item['from_schema']}→{item['to_schema']}, "
                  f"шагов: {item['steps']}")
            diff = plan["migration_diffs"].get(item["file"])
            if diff:
                print("    " + "    ".join(diff.splitlines(keepends=True)))
    if plan["manual_notes"]:
        print("Ручные шаги и предупреждения:")
        for note in plan["manual_notes"]:
            print(f"  * {note}")
    if plan["unknown_files"]:
        print("Неизвестные файлы комплекта (не изменяются):")
        for rel in plan["unknown_files"]:
            print(f"  ? {rel}")
    if plan["empty"]:
        print("План пуст: целевое состояние уже достигнуто.")


def update_plan(root, paths, release_dir, resolves, save, safe_path, require):
    require(pending_info(root) is None,
            "Незавершённое обновление: выполните update restore или update status")
    _require_no_lifecycle_pending(root, require)
    manifest = read_manifest(root, safe_path, require)
    artifact = load_artifact(release_dir, safe_path, require)
    plan = _build_plan(root, paths, artifact, manifest, resolves, safe_path, require)
    _print_plan(plan)
    if save:
        target = safe_path(root, PLAN_FILE)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        temporary.replace(target)
        print(f"План записан: {PLAN_FILE}")
    else:
        print("Предпросмотр без записи. Для сохранения плана: update plan <релиз> --save")


def _load_plan(root, safe_path, require):
    path = safe_path(root, PLAN_FILE)
    require(path.is_file(), f"Нет сохранённого плана: выполните update plan <релиз> --save")
    plan = load_json(path, require, PLAN_FILE)
    require(plan.get("schema_version") == PLAN_SCHEMA, "Неподдерживаемая схема плана")
    require(plan.get("root") == str(root), "План записан для другого корня проекта")
    return plan


def _stage_project(root, staged, safe_path, require):
    root_resolved = root.resolve()

    def ignore(directory, names):
        is_root = Path(directory).resolve() == root_resolved
        return [name for name in names
                if (is_root and name in STAGED_ROOT_IGNORE)
                or name in STAGED_ANY_IGNORE
                or name.endswith(".pyc")]

    shutil.copytree(root, staged, ignore=ignore, symlinks=True)


def _build_new_manifest(plan, artifact, manifest):
    """Итоговый manifest: эталонные хеши нового релиза + актуальные kept.

    kept ссылается только на файлы целого релиза: файл, сохранённый решением
    keep и отсутствующий в новом релизе, выходит из управления и остаётся
    проектным — в manifest он не попадает.
    """
    managed = set(artifact["hashes"])
    kept = set(manifest["kept"])
    conflicts_keep = {c["path"] for c in plan["conflicts"] if c["resolution"] == "keep"}
    return {"playbook_version": artifact["version"],
            "release_id": artifact["release_id"],
            "installed_at": now_iso(), "origin": "update",
            "files": dict(sorted(artifact["hashes"].items())),
            "kept": sorted((kept | conflicts_keep) & managed)}


def _pending_path(root):
    return root / PENDING_FILE


def _update_lock(root, safe_path):
    """Эксклюзивная блокировка операции обновления.

    Удерживается открытым дескриптором: ОС снимает её при любом завершении
    процесса (включая SIGKILL), поэтому прерванная операция не оставляет
    зависшей блокировки. Возвращает открытый поток либо None, когда
    блокировку держит другой процесс — то есть операция ещё жива.
    На платформах без flock/msvcrt блокировка деградирует до маркера pending.
    """
    path = safe_path(root, LOCK_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        stream.seek(0)
        if os.name == "posix":
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        stream.close()
        return None
    return stream


def _release_pending(root, op_id):
    """Снять маркер незавершённой операции, только если он принадлежит ей.

    Чужой или повреждённый маркер не удаляется — его разбирает update restore.
    """
    path = root / PENDING_FILE
    if not path.exists():
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("op_id") != op_id:
        return False
    path.unlink()
    return True


def _run_cli_compat(staged, journal_dir):
    """Совместимость целевого CLI с итоговым состоянием копии.

    Запускает в отдельном процессе config() и resources() playbook из
    подготовленной копии — то есть код артефакта проверяется на принятие
    мигрированных настроек и manifest до записи в проект. Произвольные
    команды из артефакта не исполняются: argv фиксирован нами.
    Возвращает (ok, detail).
    """
    snippet = ("import sys; from pathlib import Path; "
               "sys.path.insert(0, sys.argv[1]); import playbook; "
               "root = Path(sys.argv[2]).resolve(); "
               "playbook.config(root); playbook.resources(root)")
    try:
        result = subprocess.run(
            [sys.executable, "-B", "-c", snippet,
             str(staged / CORE / "scripts"), str(staged)],
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        _journal(journal_dir, "cli_compat", result="error", detail=str(error))
        return False, str(error)
    detail = (result.stderr or result.stdout).strip()
    _journal(journal_dir, "cli_compat",
             result="passed" if result.returncode == 0 else "failed",
             detail=detail[-500:])
    return result.returncode == 0, detail


def _run_staged_checks(root, staged, journal_dir, safe_path, require):
    """Проверки проекта из разрешённой (установленной) конфигурации в копии."""
    project = load_json(safe_path(root, SETTINGS[0]), require, SETTINGS[0])
    policy = load_json(safe_path(root, SETTINGS[1]), require, SETTINGS[1])
    settings = project.get("verification")
    if not isinstance(settings, dict) or not settings.get("checks"):
        _journal(journal_dir, "staged_checks", result="not_configured")
        return True
    import verification
    settings = verification.validate(staged, settings, safe_path, require)
    pseudo = {"id": "UPDATE", "path": staged / SETTINGS[0]}
    code = verification.run(staged, pseudo, settings,
                            policy.get("policy_revision", 1), safe_path, require)
    _journal(journal_dir, "staged_checks", result="passed" if code == 0 else "failed")
    return code == 0


def _read_backup_index(root, op_dir, safe_path, require):
    """Прочитать и полностью проверить backup_index.json до восстановления.

    Отсутствующий или повреждённый индекс и неполная резервная копия —
    отказ до изменения проекта: ложный успех недопустим. Проверяется
    согласованность полного состава: backed непуст и без повторов, backed и
    added не пересекаются, hashes (если записан) описывает ровно backed,
    а набор копий на диске совпадает с backed — выпавший из индекса путь
    не может остаться невосстановленным. Индекс старого формата без hashes
    допустим, но тогда проверяются только состав и наличие копий —
    целостность их содержимого такой записью не доказывается.
    """
    index_path = op_dir / "backup_index.json"
    require(index_path.is_file(),
            f"Нет backup_index.json в {op_dir.relative_to(root)}: пред-образ "
            f"недоступен, восстановление невозможно — маркер сохранён")
    index = load_json(index_path, require, "backup_index.json")
    backed, added = index.get("backed"), index.get("added")
    require(isinstance(backed, list) and isinstance(added, list)
            and all(isinstance(rel, str) for rel in backed + added),
            "backup_index.json повреждён: восстановление невозможно — маркер сохранён")
    hashes = index.get("hashes")
    if hashes is not None:
        require(isinstance(hashes, dict)
                and all(isinstance(digest, str)
                        and HASH_PATTERN.fullmatch(digest) is not None
                        for digest in hashes.values()),
                "backup_index.json: некорректный hashes — маркер сохранён")
    problems = []
    if not backed:
        problems.append("пустой backed")
    if len(set(backed)) != len(backed) or len(set(added)) != len(added):
        problems.append("повторы путей в списках")
    overlap = sorted(set(backed) & set(added))
    if overlap:
        problems.append("пересечение backed/added: " + ", ".join(overlap))
    if isinstance(hashes, dict) and set(hashes) != set(backed):
        problems.append("hashes не соответствует backed")
    for rel in backed + added:
        safe_path(root, rel)
    backup_dir = op_dir / "backup"
    on_disk = set()
    if backup_dir.is_dir():
        for path in backup_dir.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(backup_dir).as_posix()
            if not any(part.startswith(".") for part in Path(rel).parts):
                on_disk.add(rel)
    unlisted = sorted(on_disk - set(backed))
    if unlisted:
        problems.append("копии без записи в backed: " + ", ".join(unlisted))
    for rel in backed:
        source = backup_dir / rel
        if not source.is_file():
            problems.append(f"{rel} (нет копии)")
        elif isinstance(hashes, dict) and sha256(source.read_bytes()) != hashes[rel]:
            problems.append(f"{rel} (копия повреждена)")
    require(not problems,
            "Резервная копия неполна или повреждена: " + "; ".join(problems)
            + " — восстановление отклонено, маркер сохранён")
    return backed, added


def _restore_from_backup(root, op_dir, safe_path, require):
    """Детерминированное восстановление по backup_index.json и списку добавлений.

    Предварительно проверяет индекс и весь набор копий; при неполноте —
    отказ без изменений. После копирования сверяет результат с копией.
    """
    backed, added = _read_backup_index(root, op_dir, safe_path, require)
    restored, removed = 0, 0
    for rel in added:
        target = safe_path(root, rel)
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
            removed += 1
    for rel in backed:
        source = op_dir / "backup" / rel
        target = safe_path(root, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        require(target.read_bytes() == source.read_bytes(),
                f"Восстановленный файл не совпадает с резервной копией: {rel}")
        restored += 1
    return restored, removed


RESTORE_SCRIPT = '''#!/usr/bin/env python3
"""Автономное восстановление прерванного обновления playbook.

Запуск: python3 restore.py — из каталога операции work/update/<op>/.
Не зависит от установленного ai-dev-playbook/scripts/.
"""
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

op_dir = Path(__file__).resolve().parent
root = op_dir.parents[2]

def lock(stream):
    stream.seek(0)
    if os.name == "posix":
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

# Живая операция держит общий gate и блокировку обновления; восстановление
# разрешено только после реального прерывания, когда ОС их уже сняла.
(root / "work").mkdir(parents=True, exist_ok=True)
gate_file = root / "work" / "gate.lock"
gate_stream = gate_file.open("a+b")
try:
    lock(gate_stream)
except OSError:
    print("Другая команда удерживает блокировку записи: восстановление "
          "запрещено, дождитесь завершения.", file=sys.stderr)
    sys.exit(1)

lock_stream = (root / "work" / "update" / "lock").open("a+b")
try:
    lock(lock_stream)
except OSError:
    print("Операция обновления выполняется: восстановление запрещено, "
          "дождитесь завершения.", file=sys.stderr)
    sys.exit(1)

lifecycle_pending = root / "work" / "lifecycle" / "pending.json"
if lifecycle_pending.is_file():
    print("Обнаружен маркер незавершённой операции документов: восстановление "
          "обновления запрещено до разрешения операции документов. "
          "Файлы проекта и маркер не изменены.", file=sys.stderr)
    sys.exit(1)

def fail(message):
    print(message + " Маркер незавершённой операции сохранён.", file=sys.stderr)
    sys.exit(1)

def refuse(message):
    print(message + " Файлы проекта и маркер не изменены.", file=sys.stderr)
    sys.exit(1)

# Восстановление разрешено только собственной операции: маркер должен
# существовать и ссылаться на этот каталог. Чужой, отсутствующий или
# повреждённый маркер — отказ без каких-либо изменений.
pending = root / "work" / "update" / "pending.json"
if not pending.is_file():
    refuse("Нет маркера незавершённой операции: восстановление уже выполнено "
           "или не требуется.")
try:
    marker = json.loads(pending.read_text(encoding="utf-8"))
except (OSError, ValueError):
    marker = None
if not isinstance(marker, dict):
    refuse("Маркер незавершённой операции повреждён: владельца определить "
           "нельзя.")
op_ref = marker.get("op_dir")
own = (marker.get("op_id") == op_dir.name and isinstance(op_ref, str)
       and op_ref and (root / op_ref).resolve() == op_dir)
if not own:
    refuse(f"Маркер принадлежит другой операции ({marker.get('op_id')!r}): "
           f"этот пред-образ относится к операции {op_dir.name}.")

index_path = op_dir / "backup_index.json"
if not index_path.is_file():
    fail("Нет backup_index.json: пред-образ недоступен, восстановление невозможно.")
try:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    backed = index["backed"]
    added = index["added"]
    hashes = index.get("hashes")
    assert isinstance(backed, list) and isinstance(added, list)
    assert all(isinstance(rel, str) for rel in backed + added)
    assert backed
    assert len(set(backed)) == len(backed) and len(set(added)) == len(added)
    assert not set(backed) & set(added)
    if hashes is not None:
        assert isinstance(hashes, dict)
        assert all(isinstance(rel, str) and isinstance(digest, str)
                   and digest.startswith("sha256:") and len(digest) == 71
                   for rel, digest in hashes.items())
        assert set(hashes) == set(backed)
except (ValueError, KeyError, AssertionError):
    fail("backup_index.json повреждён или неполон: восстановление невозможно.")
for rel in backed + added:
    resolved = (root / rel).resolve()
    if resolved == root or root not in resolved.parents:
        fail(f"backup_index.json содержит путь вне проекта: {rel}.")
backup_dir = op_dir / "backup"
on_disk = set()
if backup_dir.is_dir():
    for path in backup_dir.rglob("*"):
        if path.is_file():
            rel = path.relative_to(backup_dir).as_posix()
            if not any(part.startswith(".") for part in Path(rel).parts):
                on_disk.add(rel)
unlisted = sorted(on_disk - set(backed))
if unlisted:
    fail("backup_index.json неполон: копии без записи в backed: "
         + ", ".join(unlisted) + ".")
problems = []
for rel in backed:
    source = backup_dir / rel
    if not source.is_file():
        problems.append(rel + " (нет копии)")
    elif isinstance(hashes, dict):
        digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
        if digest != hashes[rel]:
            problems.append(rel + " (копия повреждена)")
if problems:
    fail("Резервная копия неполна или повреждена: " + ", ".join(problems) + ".")
restored = removed = 0
for rel in added:
    target = root / rel
    if target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
        removed += 1
for rel in backed:
    source = backup_dir / rel
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if target.read_bytes() != source.read_bytes():
        fail(f"Восстановленный файл не совпадает с резервной копией: {rel}.")
    restored += 1
# Маркер снимается, только если он по-прежнему принадлежит этой операции.
try:
    current = json.loads(pending.read_text(encoding="utf-8"))
except (OSError, ValueError):
    current = None
if isinstance(current, dict) and current.get("op_id") == op_dir.name:
    pending.unlink()
    print(f"Восстановлено файлов: {restored}; удалено добавленных: {removed}; "
          "маркер снят.")
else:
    print(f"Восстановлено файлов: {restored}; удалено добавленных: {removed}; "
          "маркер сменил владельца и не снят.", file=sys.stderr)
sys.exit(0)
'''


def _require_plan_inputs(root, paths, plan, safe_path, require):
    """Сверка всех входов сохранённого плана с текущим состоянием.

    Возвращает (manifest, artifact); любое расхождение отклоняет применение.
    Вызывается до подготовки и повторно после staged-проверок перед записью:
    целевые пути релиза входят в отпечаток, поэтому появление, изменение или
    исчезновение затрагиваемого пути инвалидирует план.
    """
    manifest = read_manifest(root, safe_path, require)
    require(manifest["release_id"] == plan["installed"]["release_id"]
            and manifest["playbook_version"] == plan["installed"]["version"],
            "План устарел: установленное состояние изменилось; выполните update plan заново")
    artifact = load_artifact(plan["artifact"]["root"], safe_path, require)
    require(artifact["release_id"] == plan["artifact"]["release_id"]
            and artifact["version"] == plan["artifact"]["version"],
            "План устарел: артефакт изменился; выполните update plan заново")
    require(state_fingerprint(root, paths, manifest, safe_path, require,
                              extra=set(artifact["hashes"]))
            == plan["before_fingerprint"],
            "План устарел: файлы проекта изменились после предпросмотра; "
            "выполните update plan заново")
    return manifest, artifact


def update_apply(root, paths, safe_path, require):
    require(pending_info(root) is None,
            "Обнаружен маркер незавершённой операции: выполните update restore")
    _require_no_lifecycle_pending(root, require)
    plan = _load_plan(root, safe_path, require)
    if plan.get("empty"):
        print("План пуст: применять нечего.")
        return
    manifest, artifact = _require_plan_inputs(root, paths, plan, safe_path, require)
    unresolved = [c["path"] for c in plan["conflicts"] if not c["resolution"]]
    require(not unresolved,
            "Неразрешённые конфликты: " + ", ".join(unresolved)
            + ". Укажите --resolve <путь>=release|keep при update plan")
    keep_now = set(manifest["kept"]) | {c["path"] for c in plan["conflicts"]
                                        if c["resolution"] == "keep"}
    removes = [rel for rel in plan["changes"]["remove"] if rel not in keep_now]
    puts = [rel for rel in plan["changes"]["add"] + plan["changes"]["replace"]
            if rel not in keep_now]
    puts += [c["path"] for c in plan["conflicts"]
             if c["resolution"] == "release" and c["state"] == "missing"
             and c["path"] not in puts]
    lock = _update_lock(root, safe_path)
    require(lock is not None,
            "Незавершённая операция обновления выполняется другим процессом: "
            "дождитесь её завершения; если она прервана, выполните update restore")
    op_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + os.urandom(6).hex()
    op_dir = safe_path(root, WORK_DIR) / op_id
    op_dir.mkdir(parents=True, exist_ok=False)
    staged = op_dir / "staged"
    _journal(op_dir, "start", op_id=op_id, target=artifact["version"])
    pending = _pending_path(root)
    try:
        with pending.open("x", encoding="utf-8") as marker:
            marker.write(json.dumps({"op_id": op_id, "started_at": now_iso(),
                                     "op_dir": str(op_dir.relative_to(root))},
                                    ensure_ascii=False) + "\n")
    except FileExistsError:
        _journal(op_dir, "abort", reason="pending marker appeared concurrently")
        lock.close()
        require(False,
                "Обнаружен маркер незавершённой операции: выполните update restore")
    try:
        _stage_project(root, staged, safe_path, require)
        for rel in removes:
            target = staged / rel
            if target.exists():
                target.unlink()
        for rel in puts:
            (staged / rel).parent.mkdir(parents=True, exist_ok=True)
            (staged / rel).write_bytes(artifact["files"][rel])
        for rel, content in plan["staged_settings"].items():
            (staged / rel).write_text(content, encoding="utf-8")
        new_manifest = _build_new_manifest(plan, artifact, manifest)
        write_manifest(staged, new_manifest)
        _journal(op_dir, "staged", files=len(puts) + len(removes) + len(plan["staged_settings"]))
        compat, detail = _run_cli_compat(staged, op_dir)
        if not compat:
            _journal(op_dir, "abort", reason="target cli rejects staged state")
            _release_pending(root, op_id)
            require(False,
                    f"Целевой CLI не принимает итоговое состояние: {detail}. "
                    f"Проект не изменён; артефакт неконсистентен или требует "
                    f"иного перехода.")
        if not _run_staged_checks(root, staged, op_dir, safe_path, require):
            _journal(op_dir, "abort", reason="staged checks failed")
            _release_pending(root, op_id)
            require(False,
                    f"Проверки в подготовленной копии не прошли; проект не изменён. "
                    f"Отчёт: {op_dir.relative_to(root)}/staged/work/verification/")
        backup_dir = op_dir / "backup"
        backed, hashes = [], {}
        touched = sorted(set(puts + removes + list(plan["staged_settings"]) + [MANIFEST_PATH]))
        for rel in touched:
            source = safe_path(root, rel)
            if source.exists() and source.is_file():
                destination = backup_dir / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                backed.append(rel)
                hashes[rel] = sha256(destination.read_bytes())
        (op_dir / "backup_index.json").write_text(
            json.dumps({"backed": backed, "added": sorted(set(puts) - set(backed)),
                        "hashes": hashes},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (op_dir / "restore.py").write_text(RESTORE_SCRIPT, encoding="utf-8")
        _journal(op_dir, "backup_complete", backed=len(backed))
        try:
            _require_plan_inputs(root, paths, plan, safe_path, require)
        except (OSError, ValueError, TypeError, KeyError) as error:
            _journal(op_dir, "abort", reason="inputs changed during staging",
                     error=str(error))
            _release_pending(root, op_id)
            raise
        _journal(op_dir, "rechecked")
        failed = None
        try:
            for rel in removes:
                safe_path(root, rel).unlink(missing_ok=True)
                _journal(op_dir, "remove", path=rel)
            for rel in puts:
                _write_file(staged / rel, safe_path(root, rel))
                _journal(op_dir, "write", path=rel)
            for rel, content in plan["staged_settings"].items():
                _write_file(staged / rel, safe_path(root, rel))
                _journal(op_dir, "write", path=rel)
            _write_file(staged / MANIFEST_PATH, safe_path(root, MANIFEST_PATH))
            _journal(op_dir, "write", path=MANIFEST_PATH)
            _verify_final(root, artifact, new_manifest, safe_path, require,
                          preserved=plan["drift"])
            _journal(op_dir, "verified")
        except (OSError, ValueError, TypeError, KeyError) as error:
            failed = error
            _journal(op_dir, "failed", error=str(error))
        if failed is not None:
            try:
                restored, removed = _restore_from_backup(root, op_dir,
                                                       safe_path, require)
                _journal(op_dir, "restored", files=restored, removed=removed)
            except (OSError, ValueError, TypeError, KeyError) as restore_error:
                _journal(op_dir, "restore_failed", error=str(restore_error))
                require(False,
                        f"Ошибка записи: {failed}. Восстановление неполное: "
                        f"{restore_error}. Маркер сохранён: после устранения "
                        f"причины выполните update restore. "
                        f"Журнал: {op_dir.relative_to(root)}")
            _release_pending(root, op_id)
            require(False,
                    f"Ошибка записи: {failed}. Состояние восстановлено из резервной "
                    f"копии ({restored} файлов). Журнал: {op_dir.relative_to(root)}")
        if not _release_pending(root, op_id):
            _journal(op_dir, "pending_foreign", op_id=op_id)
        _journal(op_dir, "done")
    finally:
        lock.close()
    print(f"Обновление {plan['installed']['version']} → {artifact['version']} применено: "
          f"записано {len(puts)} + настроек {len(plan['staged_settings'])}, "
          f"удалено {len(removes)}. Журнал и резервная копия: {op_dir.relative_to(root)}")


def _verify_final(root, artifact, manifest, safe_path, require, preserved=()):
    """Итоговая согласованность: VERSION == playbook_version == manifest, хеши ядра.

    preserved — нетронутые обновлением drift-пути: план разрешает их локальное
    расхождение, поэтому итоговая сверка их пропускает так же, как kept.
    """
    version_file = safe_path(root, CORE + "/VERSION").read_text(encoding="utf-8").strip()
    require(version_file == manifest["playbook_version"],
            f"Итоговая VERSION {version_file} != {manifest['playbook_version']}")
    project = load_json(safe_path(root, SETTINGS[0]), require, SETTINGS[0])
    require(project.get("playbook_version") == manifest["playbook_version"],
            "Итоговый playbook_version != версии релиза")
    kept = set(manifest["kept"]) | set(preserved)
    for rel, digest in manifest["files"].items():
        if rel in kept:
            continue
        require(current_hash(root, rel, safe_path, require) == digest,
                f"Итоговое содержимое не совпадает с manifest: {rel}")
    final = read_manifest(root, safe_path, require)
    require(final["release_id"] == manifest["release_id"], "manifest записан неверно")


def update_restore(root, safe_path, require):
    pending = pending_info(root)
    require(pending is not None, "Нет незавершённой операции обновления")
    _require_no_lifecycle_pending(root, require)
    lock = _update_lock(root, safe_path)
    require(lock is not None,
            "Операция обновления выполняется другим процессом: восстановление "
            "запрещено до её завершения. Восстановление доступно только после "
            "реального прерывания операции")
    try:
        base = safe_path(root, WORK_DIR).resolve()
        rel_dir = pending.get("op_dir")
        op_dir = (root / rel_dir).resolve() if isinstance(rel_dir, str) and rel_dir else None
        require(op_dir is not None and op_dir.parent == base
                and op_dir.name == pending.get("op_id"),
                f"Маркер незавершённой операции повреждён или ссылается на "
                f"чужой каталог ({rel_dir!r}): восстановление отклонено — "
                f"маркер сохранён")
        require(op_dir.is_dir(),
                f"Нет каталога операции {rel_dir}: пред-образ утрачен, "
                f"восстановление невозможно — маркер сохранён")
        restored, removed = _restore_from_backup(root, op_dir, safe_path, require)
        _journal(op_dir, "restored_by_command", files=restored, removed=removed)
        pending["path"].unlink()
        print(f"Восстановление завершено: возвращено {restored} файлов, "
              f"удалено добавленных {removed}. Маркер незавершённой операции снят.")
    finally:
        lock.close()


def update_status(root, safe_path, require):
    import file_operations
    lifecycle = file_operations.pending_info(root)
    if lifecycle is not None:
        print(f"Незавершённая операция документов: {lifecycle.get('op_id')} — "
              f"восстановление: python3 {lifecycle.get('op_dir', '?')}/restore.py")
    pending = pending_info(root)
    if pending is None:
        plan_path = root / PLAN_FILE
        if plan_path.is_file():
            print(f"Нет активной операции. Сохранённый план: {PLAN_FILE}")
        else:
            print("Нет активной операции и сохранённого плана.")
        return
    print(f"Незавершённая операция: {pending.get('op_id')} "
          f"(с {pending.get('started_at', '?')})")
    op_dir = root / pending.get("op_dir", "")
    journal = op_dir / "journal.jsonl"
    if journal.is_file():
        lines = journal.read_text(encoding="utf-8").splitlines()[-10:]
        print("Последние события журнала:")
        for line in lines:
            print("  " + line)
    probe = _update_lock(root, safe_path)
    if probe is None:
        print("Операция выполняется другим процессом: восстановление запрещено "
              "до её завершения.")
    else:
        probe.close()
        print("Восстановление: playbook.py update restore "
              f"или python3 {pending.get('op_dir', '?')}/restore.py")


def adopt(root, release_dir, resolves, apply, force, safe_path, require):
    require(pending_info(root) is None,
            "Незавершённое обновление: выполните update restore")
    _require_no_lifecycle_pending(root, require)
    manifest_path = safe_path(root, MANIFEST_PATH)
    require(force or not manifest_path.exists(),
            f"{MANIFEST_PATH} уже существует: комплект под управлением; "
            f"для обновления используйте update (перезапись manifest — --force)")
    project_path = safe_path(root, SETTINGS[0])
    require(project_path.is_file(), f"Нет {SETTINGS[0]}: сначала выполните init")
    artifact = load_artifact(release_dir, safe_path, require)
    current = {rel: current_hash(root, rel, safe_path, require)
               for rel in artifact["hashes"]}
    match, modified, missing = [], [], []
    for rel in sorted(artifact["hashes"]):
        state = current[rel]
        if state == "missing":
            missing.append(rel)
        elif state == artifact["hashes"][rel]:
            match.append(rel)
        else:
            modified.append(rel)
    differences = set(modified) | set(missing)
    extra = sorted(set(resolves) - differences)
    require(not extra,
            "Разрешения указаны для файлов без расхождения: " + ", ".join(extra))
    unresolved = sorted(differences - set(resolves))
    kept = sorted(rel for rel, choice in resolves.items() if choice == "keep")
    restores = sorted(rel for rel, choice in resolves.items() if choice == "release")
    require(CORE + "/VERSION" not in kept,
            "VERSION нельзя сохранить расходящимся: оно определяет версию установки")
    unknown = unknown_kit_files(root, set(artifact["hashes"]), safe_path)
    print(f"Эталон: {release_dir} ({artifact['provenance']}), версия {artifact['version']}, "
          f"release_id {artifact['release_id'][:19]}…")
    print(f"Совпадают с эталоном: {len(match)}; изменены: {len(modified)}; "
          f"отсутствуют: {len(missing)}; неизвестные (сохраняются): {len(unknown)}")
    for rel in restores:
        print(f"  ~ {rel} — будет восстановлен из эталона")
    for rel in kept:
        print(f"  = {rel} — локальное расхождение сохраняется (kept)")
    for rel in unknown:
        print(f"  ? {rel}")
    if unresolved:
        print("Неразрешённые различия:")
        for rel in unresolved:
            state = "отсутствует" if rel in missing else "изменён"
            print(f"  ! {rel} — {state}")
        require(False,
                "Разрешите различия: --resolve <путь>=release|keep для каждого файла")
    project = load_json(project_path, require, SETTINGS[0])
    version_fix = project.get("playbook_version") != artifact["version"]
    if version_fix:
        print(f"project.json: playbook_version {project.get('playbook_version')!r} "
              f"→ {artifact['version']}")
    if not apply:
        print("Предпросмотр принятия под управление, без записи. Для записи: --apply")
        return
    for rel in restores:
        safe_path(root, rel).parent.mkdir(parents=True, exist_ok=True)
        safe_path(root, rel).write_bytes(artifact["files"][rel])
    if version_fix:
        project["playbook_version"] = artifact["version"]
        project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
    manifest = {"playbook_version": artifact["version"],
                "release_id": artifact["release_id"], "installed_at": now_iso(),
                "origin": f"adopt:{artifact['provenance']}",
                "files": artifact["hashes"], "kept": kept}
    write_manifest(root, manifest)
    print(f"Комплект принят под управление: {MANIFEST_PATH} записан "
          f"({len(artifact['hashes'])} управляемых файлов, kept: {len(kept)}).")
