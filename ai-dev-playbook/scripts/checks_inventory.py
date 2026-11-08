"""Inventory и снимки входов проверок без Git (PB-058).

Файловый слой выбора проверок по зависимостям: обход проекта без ``.git``,
классификация путей по модулям одобренной политики и проверенные снимки
объявленных/вычисленных входов. Контракты политики — ``checks_contract``
(PB-057); чистый расчёт coverage/reuse остаётся в ``verification_policy``,
runner и выбор — PB-059/060.

``checks-inventory`` (schema_version 1): ``{project_id, policy_digest,
created_at, entries, excluded, unknown_paths}``. ``entries`` отображает
каждый наблюдаемый относительный путь в ``{type: file|dir, modules: [...]}``
без чтения байтов. ``excluded`` фиксирует наблюдаемые пути, снятые
одобренными исключениями и служебными каталогами (``.git``, ``work``,
``__pycache__``, ``.pytest_cache``, ``.DS_Store``) с причиной.
``unknown_paths`` — файлы вне любого модуля: классификация или расширение
области — решение потребителя (PB-059).

``checks-input-snapshot`` (schema_version 1): ``{project_id,
policy_digest, created_at, entries}``. ``entries`` отображает ключ входа
в ``file``/``directory``/``absent``/``projection``/``value``. Ключи —
``path:<rel>``, ``task_projection``, ``policy_projection`` и
``environment:<name>``; каталог хранит полный состав потомков, файлы —
SHA-256. Новый файл внутри области входа заметен по составу каталога, не
только по прежнему списку. Снимок входов — отдельный артефакт и не
смешивается с lifecycle snapshot операций (``work/``): у них разные
форматы, владельцы и гарантии.

Правила слоя (design §4, spec R-05–07,15):

- байты читаются только для объявленных и вычисленных входов; хеш —
  потоковый SHA-256, один путь один раз на снимок; mtime/size ключом
  кеша не являются (только контроль гонок);
- отсутствующий вход фиксируется ``absent``; ошибка чтения,
  изменяющееся дерево и появление наблюдавшегося входа или его предка —
  контролируемый отказ, не частичный результат;
- символические ссылки, ссылки за пределы корня, циклы и специальные
  файлы отклоняются; служебные каталоги не читаются массово;
- исключение с обнаруженным входом проверки — ошибка (R-07): одобренный
  селективный glob допустим, но столкновение с выбранным входом
  (явный ``check.inputs`` без фильтра либо файл/поддерево, совпадающее
  с образцом модуля) отклоняется при обнаружении; исключённый файл вне
  выбранных входов пропускается;
- снимок связан с ``policy_digest``: смена политики и утраченный или
  повреждённый baseline не дают ``reused`` — diff возвращает
  ``complete: false``, а не ложную свежесть.
"""
import datetime
import json
import stat
from pathlib import Path

import checks_contract
import project_view
import verification_policy


INVENTORY_SCHEMA_VERSION = 1
SNAPSHOT_SCHEMA_VERSION = 1
INVENTORY_KIND = "checks-inventory"
SNAPSHOT_KIND = "checks-input-snapshot"

SERVICE_DIRS = {".git", "__pycache__", ".pytest_cache"}
SERVICE_FILES = {".DS_Store"}
RUNTIME_DIR = "work"

INVENTORY_FIELDS = {"schema_version", "kind", "project_id",
                    "policy_digest", "created_at", "entries", "excluded",
                    "unknown_paths"}
INVENTORY_ENTRY_FIELDS = {"type", "modules"}
EXCLUDED_FIELDS = {"path", "reason", "origin"}
EXCLUDED_ORIGINS = checks_contract.EXCLUDE_ORIGINS | {"service"}

SNAPSHOT_FIELDS = {"schema_version", "kind", "project_id",
                   "policy_digest", "created_at", "entries"}
ENTRY_FIELDS = {"type", "sha256", "members", "digest"}
ENTRY_TYPES = {"file", "directory", "absent", "projection", "value"}
MEMBER_FIELDS = {"type", "sha256"}
MEMBER_TYPES = {"file", "dir"}


def _raise(condition, message):
    if not condition:
        raise ValueError(message)


def _now(now):
    if now is not None:
        checks_contract._iso(now, "created_at", _raise)
        return now
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _require_root(root):
    if root.is_symlink():
        raise ValueError("checks: корень проекта — символическая ссылка")
    if not root.is_dir():
        raise ValueError("checks: корень проекта не является каталогом")


def _join(base, rel):
    if base in {"", "."}:
        return rel
    return base + "/" + rel


def _service_reason(rel, name, is_dir):
    if is_dir and (name in SERVICE_DIRS or rel == RUNTIME_DIR):
        return "служебный runtime-каталог"
    if not is_dir and name in SERVICE_FILES:
        return "служебный файл среды"
    return None


def _matched_exclude(rel, excludes):
    for pattern, reason, origin in excludes:
        if checks_contract._match(rel, pattern):
            return {"pattern": pattern, "reason": reason, "origin": origin}
    return None


def _declared_input_areas(policy):
    """Литеральные входы проверок; область покрывает путь и его потомков."""
    areas = []
    for check in policy.get("checks", []):
        for entry in check.get("inputs") or []:
            if isinstance(entry, str):
                areas.append(entry)
            elif (isinstance(entry, dict)
                  and entry.get("type") in {"file", "directory", "path"}):
                areas.append(entry["path"])
    return areas


def _inside_areas(rel, areas):
    return any(rel == area or rel.startswith(area + "/") for area in areas)


def build_inventory(root, policy, *, now=None):
    """Обойти проект без Git и классифицировать пути по модулям политики.

    Байты файлов не читаются: inventory — карта путей, типов и владельцев.
    Symlink, специальный файл, цикл каталогов, ошибка чтения и коллизия
    исключения с объявленным входом — ошибка, а не частичный результат.
    """
    root = Path(root)
    _require_root(root)
    modules = [(m["id"], list(m["paths"])) for m in policy["modules"]]
    excludes = [(e["pattern"], e["reason"], e["origin"])
                for e in policy["excludes"]]
    input_areas = _declared_input_areas(policy)
    entries, excluded, unknown = {}, [], []
    visited = set()

    def assign(rel):
        return sorted(mid for mid, globs in modules
                      if any(checks_contract._match(rel, glob)
                             for glob in globs))

    def descend(abs_dir, rel):
        info = abs_dir.stat()
        marker = (info.st_dev, info.st_ino)
        if marker in visited:
            raise ValueError(f"checks-inventory: циклический каталог "
                             f"{rel or '.'}")
        visited.add(marker)
        for child in sorted(abs_dir.iterdir(), key=lambda p: p.name):
            child_rel = _join(rel, child.name)
            st = child.lstat()
            if stat.S_ISLNK(st.st_mode):
                raise ValueError(f"checks-inventory: символическая ссылка "
                                 f"{child_rel} не допускается")
            is_dir = stat.S_ISDIR(st.st_mode)
            service = _service_reason(child_rel, child.name, is_dir)
            if service is not None:
                excluded.append({"path": child_rel, "reason": service,
                                 "origin": "service"})
                continue
            matched = _matched_exclude(child_rel, excludes)
            if matched is not None:
                if _inside_areas(child_rel, input_areas):
                    raise ValueError(
                        f"checks-inventory: исключение "
                        f"{matched['pattern']!r} конфликтует с "
                        f"обнаруженным входом {child_rel!r}")
                excluded.append({"path": child_rel,
                                 "reason": matched["reason"],
                                 "origin": matched["origin"]})
                continue
            if is_dir:
                entries[child_rel] = {"type": "dir",
                                      "modules": assign(child_rel)}
                descend(child, child_rel)
            elif stat.S_ISREG(st.st_mode):
                owners = assign(child_rel)
                entries[child_rel] = {"type": "file", "modules": owners}
                if not owners:
                    unknown.append(child_rel)
            else:
                raise ValueError(f"checks-inventory: специальный файл "
                                 f"{child_rel} не допускается")

    descend(root, "")
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "kind": INVENTORY_KIND,
        "project_id": policy["project_id"],
        "policy_digest": verification_policy.fingerprint(policy),
        "created_at": _now(now),
        "entries": entries,
        "excluded": excluded,
        "unknown_paths": sorted(unknown),
    }


def resolved_inputs(policy, check):
    """Объявленные и вычисленные (модульные) входы проверки.

    Объявленные ``check.inputs`` валидируются контрактом; каждый glob
    ``paths`` связанного модуля становится входом-каталогом по литеральной
    области с фильтром ``patterns``: байты хешируются только у файлов,
    совпадающих с образцом модуля, а состав каталога фиксируется полностью.
    """
    declared = check.get("inputs") or []
    checks_contract._validate_inputs(declared, "check.inputs", _raise)
    inputs = list(declared)
    modules_by_id = {m["id"]: m for m in policy["modules"]}
    for module_id in check.get("modules") or []:
        module = modules_by_id[module_id]
        for pattern in module["paths"]:
            head = checks_contract._literal_head(pattern)
            if head == pattern:
                inputs.append({"type": "path", "path": pattern})
            else:
                inputs.append({"type": "directory", "path": head or ".",
                               "patterns": [pattern]})
    return inputs


def _normalize_input(spec, label):
    if isinstance(spec, str):
        checks_contract._path(spec, label, _raise)
        return {"type": "path", "path": spec, "patterns": None}
    _raise(isinstance(spec, dict), f"{label}: некорректный вход")
    extra = sorted(set(spec) - {"type", "path", "names", "patterns"})
    _raise(not extra, f"{label}: неизвестные поля {extra}")
    kind = spec.get("type")
    if kind in {"file", "directory", "path"}:
        if spec.get("path") != ".":
            checks_contract._path(spec.get("path"), f"{label}.path",
                                  _raise)
        elif kind != "directory":
            raise ValueError(f"{label}.path: '.' — только каталог")
        patterns = spec.get("patterns")
        if patterns is not None:
            _raise(isinstance(patterns, list) and patterns
                   and all(isinstance(p, str) and p.strip()
                           for p in patterns),
                   f"{label}.patterns должен быть непустым списком")
            for pattern in patterns:
                checks_contract._path(pattern, f"{label}.patterns",
                                      _raise, pattern=True)
        return {"type": kind, "path": spec["path"], "patterns": patterns}
    if kind == "environment":
        names = spec.get("names")
        _raise(isinstance(names, list) and names
               and all(isinstance(n, str) and n.strip() for n in names),
               f"{label}.names должен быть непустым списком строк")
        return {"type": "environment", "names": names}
    if kind in {"task_projection", "policy_projection"}:
        return {"type": kind}
    raise ValueError(f"{label}: неизвестный тип входа {kind!r}")


def _group_inputs(inputs):
    """Слить входы одного пути; file против directory — противоречие."""
    files, dirs, anys, other = set(), {}, set(), []
    for idx, spec in enumerate(inputs):
        item = _normalize_input(spec, f"inputs[{idx}]")
        kind, rel = item["type"], item.get("path")
        if kind == "environment" or kind in {"task_projection",
                                             "policy_projection"}:
            other.append(item)
        elif kind == "file":
            files.add(rel)
        elif kind == "directory":
            prev = dirs.setdefault(rel, [])
            if item["patterns"] is None:
                dirs[rel] = None
            elif prev is not None:
                prev.extend(item["patterns"])
        else:
            anys.add(rel)
    for rel in sorted(files):
        if rel in dirs:
            raise ValueError(f"checks-snapshot: вход {rel!r} объявлен и "
                             f"файлом, и каталогом")
    # Объявленный path-вход покрывает область целиком: объединение со
    # связанным модулем не сужает его до фильтра образца модуля.
    for rel in sorted(anys - files):
        dirs[rel] = None
    return files, dirs, anys, other


def snapshot(root, policy, inputs, *, environment=None, projections=None,
             hash_fn=None, now=None):
    """Проверенный снимок объявленных и вычисленных входов.

    Один файл хешируется потоково ровно один раз на снимок. После снятия
    все наблюдавшиеся файлы и состав каталогов сверяются повторно:
    изменившееся дерево — ошибка, а не частичный снимок (R-15).
    ``hash_fn`` — точка измерения стоимости; по умолчанию потоковый
    ``project_view.digest_file``.
    """
    root = Path(root)
    _require_root(root)
    digest = hash_fn or project_view.digest_file
    excludes = [(e["pattern"], e["reason"], e["origin"])
                for e in policy["excludes"]]
    file_digests, watch_files, watch_dirs = {}, {}, {}
    watch_states = {}
    entries = {}

    def state_of(path):
        try:
            st = path.lstat()
        except OSError:
            return "absent"
        if stat.S_ISLNK(st.st_mode):
            return "symlink"
        if stat.S_ISDIR(st.st_mode):
            return "dir"
        if stat.S_ISREG(st.st_mode):
            return "file"
        return "other"

    def resolve(rel):
        path = root
        if rel in {"", "."}:
            watch_states["."] = "dir"
            return path
        current = ""
        for part in rel.split("/"):
            if path.exists() and not path.is_dir():
                raise ValueError(f"checks-snapshot: предок входа {rel!r} "
                                 f"не является каталогом")
            path = path / part
            current = _join(current, part)
            if path.is_symlink():
                raise ValueError(f"checks-snapshot: символическая ссылка "
                                 f"в пути входа {rel!r}")
            kind = state_of(path)
            if current in watch_states:
                if watch_states[current] != kind:
                    raise ValueError(
                        f"checks-snapshot: вход {current} изменился во "
                        f"время снимка")
            else:
                watch_states[current] = kind
        return path

    def hash_file(rel, abs_path):
        if rel not in file_digests:
            st = abs_path.lstat()
            watch_files[rel] = (st.st_size, st.st_mtime_ns)
            file_digests[rel] = digest(abs_path)
        return file_digests[rel]

    def check_excluded_entry(child, full_rel, is_dir, patterns):
        """Исключённый член внутри входа: совпадение со входом — отказ."""
        if patterns is None:
            return True
        if is_dir:
            return excluded_holds_input(child, full_rel, patterns)
        return any(checks_contract._match(full_rel, p) for p in patterns)

    def excluded_holds_input(abs_dir, base_rel, patterns):
        """Есть ли в исключённом поддереве файл, совпадающий со входом."""
        stack = [(abs_dir, "")]
        while stack:
            current_abs, current_rel = stack.pop()
            for child in current_abs.iterdir():
                st = child.lstat()
                if stat.S_ISLNK(st.st_mode):
                    raise ValueError(f"checks-snapshot: символическая "
                                     f"ссылка в исключённом поддереве")
                member_rel = _join(current_rel, child.name)
                full_rel = _join(base_rel, member_rel)
                if stat.S_ISDIR(st.st_mode):
                    stack.append((child, member_rel))
                elif not stat.S_ISREG(st.st_mode):
                    raise ValueError(f"checks-snapshot: специальный файл "
                                     f"{full_rel} не допускается")
                elif any(checks_contract._match(full_rel, p)
                         for p in patterns):
                    return True
        return False

    def descend_input(abs_dir, base_rel, rel, patterns, members):
        listing = []
        for child in sorted(abs_dir.iterdir(), key=lambda p: p.name):
            st = child.lstat()
            if stat.S_ISLNK(st.st_mode):
                raise ValueError(f"checks-snapshot: символическая ссылка "
                                 f"{_join(base_rel, _join(rel, child.name))} "
                                 f"не допускается")
            is_dir = stat.S_ISDIR(st.st_mode)
            kind = "dir" if is_dir else \
                "file" if stat.S_ISREG(st.st_mode) else None
            member_rel = _join(rel, child.name)
            full_rel = _join(base_rel, member_rel)
            if kind is None:
                raise ValueError(f"checks-snapshot: специальный файл "
                                 f"{full_rel} не допускается")
            if _service_reason(full_rel, child.name, is_dir) is not None:
                continue
            if _matched_exclude(full_rel, excludes) is not None:
                # Селективное исключение вне выбранных входов допустимо;
                # совпадение с фильтром образца — коллизия (R-07).
                if check_excluded_entry(child, full_rel, is_dir,
                                        patterns):
                    raise ValueError(
                        f"checks-snapshot: исключение конфликтует с "
                        f"обнаруженным входом {full_rel!r}")
                continue
            listing.append((child.name, kind))
            if is_dir:
                members[member_rel] = {"type": "dir"}
                descend_input(child, base_rel, member_rel, patterns,
                              members)
            elif patterns is None or any(
                    checks_contract._match(full_rel, p) for p in patterns):
                members[member_rel] = {"type": "file",
                                       "sha256": hash_file(full_rel, child)}
            else:
                members[member_rel] = {"type": "file"}
        watch_dirs[_join(base_rel, rel)] = (tuple(listing), patterns)

    def directory_entry(rel, abs_path, patterns):
        members = {}
        descend_input(abs_path, rel, "", patterns, members)
        return {"type": "directory", "members": members}

    def check_service(rel):
        if rel not in {"", "."} \
                and (any(seg in SERVICE_DIRS for seg in rel.split("/"))
                     or rel == RUNTIME_DIR
                     or rel.startswith(RUNTIME_DIR + "/")):
            raise ValueError(f"checks-snapshot: вход {rel!r} в служебной "
                             f"области")

    files, dirs, anys, other = _group_inputs(inputs)
    for rel in sorted(dirs):
        check_service(rel)
        if _matched_exclude(rel, excludes) is not None:
            raise ValueError(f"checks-snapshot: вход {rel!r} исключён "
                             f"одобренной политикой")
        path = resolve(rel)
        if not path.exists():
            entries["path:" + rel] = {"type": "absent"}
        elif not path.is_dir():
            if rel in anys:
                entries["path:" + rel] = {"type": "file",
                                          "sha256": hash_file(rel, path)}
            else:
                raise ValueError(f"checks-snapshot: вход {rel!r} не "
                                 f"является каталогом")
        else:
            entries["path:" + rel] = directory_entry(rel, path,
                                                     dirs[rel])
    for rel in sorted(files):
        check_service(rel)
        if _matched_exclude(rel, excludes) is not None:
            raise ValueError(f"checks-snapshot: вход {rel!r} исключён "
                             f"одобренной политикой")
        path = resolve(rel)
        if not path.exists():
            entries["path:" + rel] = {"type": "absent"}
        elif not path.is_file() or path.is_symlink():
            raise ValueError(f"checks-snapshot: вход {rel!r} не является "
                             f"обычным файлом")
        else:
            entries["path:" + rel] = {"type": "file",
                                      "sha256": hash_file(rel, path)}
    for item in other:
        kind = item["type"]
        if kind == "environment":
            for name in item["names"]:
                if environment is None or name not in environment:
                    raise ValueError(f"checks-snapshot: неизвестный факт "
                                     f"среды {name!r}")
                entries["environment:" + name] = {
                    "type": "value",
                    "digest": verification_policy.fingerprint(
                        environment[name])}
        else:
            if projections is None or kind not in projections:
                raise ValueError(f"checks-snapshot: отсутствует проекция "
                                 f"{kind}")
            entries[kind] = {
                "type": "projection",
                "digest": verification_policy.fingerprint(
                    projections[kind])}

    for rel, expected in sorted(watch_files.items()):
        try:
            st = (root / rel).lstat()
        except OSError as error:
            raise ValueError(f"checks-snapshot: вход {rel!r} изменился "
                             f"во время снимка") from error
        if not stat.S_ISREG(st.st_mode) \
                or (st.st_size, st.st_mtime_ns) != expected:
            raise ValueError(f"checks-snapshot: вход {rel!r} изменился "
                             f"во время снимка")
    for rel, watched in sorted(watch_dirs.items()):
        expected, patterns = watched
        abs_dir = resolve(rel)
        try:
            actual = []
            for child in sorted(abs_dir.iterdir(), key=lambda p: p.name):
                st = child.lstat()
                if stat.S_ISLNK(st.st_mode):
                    raise ValueError(f"checks-snapshot: вход {rel} "
                                     f"изменился во время снимка")
                is_dir = stat.S_ISDIR(st.st_mode)
                full_rel = _join(rel, child.name)
                if _service_reason(full_rel, child.name,
                                   is_dir) is not None:
                    continue
                if _matched_exclude(full_rel, excludes) is not None:
                    # Исключённый член вне входов не входит в состав;
                    # появившееся совпадение со входом — изменение.
                    if check_excluded_entry(child, full_rel, is_dir,
                                            patterns):
                        raise ValueError(
                            f"checks-snapshot: вход {rel} изменился во "
                            f"время снимка")
                    continue
                actual.append((child.name, "dir" if is_dir else "file"))
        except OSError as error:
            raise ValueError(f"checks-snapshot: вход {rel} изменился во "
                             f"время снимка") from error
        if tuple(actual) != expected:
            raise ValueError(f"checks-snapshot: вход {rel} изменился во "
                             f"время снимка")
    for rel, expected in sorted(watch_states.items()):
        try:
            st = (root if rel == "." else
                  root.joinpath(*rel.split("/"))).lstat()
        except OSError:
            kind = "absent"
        else:
            if stat.S_ISLNK(st.st_mode):
                raise ValueError(f"checks-snapshot: вход {rel} "
                                 f"изменился во время снимка")
            kind = "dir" if stat.S_ISDIR(st.st_mode) else \
                "file" if stat.S_ISREG(st.st_mode) else "other"
        if kind != expected:
            raise ValueError(f"checks-snapshot: вход {rel} изменился во "
                             f"время снимка")

    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "kind": SNAPSHOT_KIND,
        "project_id": policy["project_id"],
        "policy_digest": verification_policy.fingerprint(policy),
        "created_at": _now(now),
        "entries": entries,
    }


def snapshot_digest(doc):
    """Отпечаток содержимого снимка для binding.input_digest."""
    return verification_policy.fingerprint(doc["entries"])


def snapshot_compatible(doc, policy):
    """Может ли снимок служить входом при текущей одобренной политике."""
    try:
        validate_snapshot(doc, _raise)
    except ValueError as error:
        return {"compatible": False, "reason": str(error)}
    if doc["project_id"] != policy["project_id"]:
        return {"compatible": False, "reason": "другой project_id"}
    if doc["policy_digest"] != verification_policy.fingerprint(policy):
        return {"compatible": False,
                "reason": "снимок привязан к другой ревизии политики"}
    return {"compatible": True, "reason": "policy_digest совпадает"}


def _entry_paths(key, entry):
    if entry["type"] == "directory":
        base = key[5:] if key.startswith("path:") else key
        return [_join(base, m) if base not in {"", "."} else m
                for m in entry["members"]] or [key]
    return [key]


def _all_paths(doc):
    paths = []
    for key, entry in doc["entries"].items():
        paths.extend(_entry_paths(key, entry))
    return sorted(paths)


def diff_snapshots(baseline, current):
    """Сравнить два снимка входов; потерянный/повреждённый baseline —
    ``complete: false``, никогда ложная свежесть.

    Переименование видно как delete/add; одинаковые mtime/size при других
    байтах различаются по SHA-256.
    """
    validate_snapshot(current, _raise)
    if baseline is None:
        return {"complete": False, "reason": "baseline отсутствует",
                "fresh": False, "added": _all_paths(current),
                "removed": [], "changed": []}
    try:
        validate_snapshot(baseline, _raise)
    except ValueError as error:
        return {"complete": False, "reason": f"baseline повреждён: {error}",
                "fresh": False, "added": _all_paths(current),
                "removed": [], "changed": []}
    if (baseline["project_id"] != current["project_id"]
            or baseline["policy_digest"] != current["policy_digest"]):
        return {"complete": False,
                "reason": "baseline относится к другой политике или "
                          "проекту",
                "fresh": False, "added": _all_paths(current),
                "removed": [], "changed": []}
    def rel_key(key):
        return key[5:] if key.startswith("path:") else key

    added, removed, changed = [], [], []
    before, after = baseline["entries"], current["entries"]
    for key in sorted(set(before) | set(after)):
        if key not in before:
            added.extend(_entry_paths(key, after[key]))
            continue
        if key not in after:
            removed.extend(_entry_paths(key, before[key]))
            continue
        old, new = before[key], after[key]
        if old["type"] != new["type"]:
            changed.append(rel_key(key))
            continue
        if new["type"] == "directory":
            old_members, new_members = old["members"], new["members"]
            base = key[5:]
            for member in sorted(set(old_members) | set(new_members)):
                rel = _join(base, member)
                prev, nxt = old_members.get(member), new_members.get(member)
                if prev is None:
                    added.append(rel)
                elif nxt is None:
                    removed.append(rel)
                elif prev != nxt:
                    changed.append(rel)
        elif new["type"] in {"projection", "value"}:
            if old["digest"] != new["digest"]:
                changed.append(key)
        elif new["type"] == "file" and old["sha256"] != new["sha256"]:
            changed.append(rel_key(key))
    fresh = not (added or removed or changed)
    return {"complete": True, "reason": "", "fresh": fresh,
            "added": sorted(added), "removed": sorted(removed),
            "changed": sorted(changed)}


def diff_inventories(baseline, current):
    """Сравнить два inventory; утрата baseline — консервативный выбор."""
    validate_inventory(current, _raise)
    if baseline is None:
        return {"complete": False, "reason": "baseline отсутствует",
                "fresh": False, "added": sorted(current["entries"]),
                "removed": [], "changed": []}
    try:
        validate_inventory(baseline, _raise)
    except ValueError as error:
        return {"complete": False, "reason": f"baseline повреждён: {error}",
                "fresh": False, "added": sorted(current["entries"]),
                "removed": [], "changed": []}
    if (baseline["project_id"] != current["project_id"]
            or baseline["policy_digest"] != current["policy_digest"]):
        return {"complete": False,
                "reason": "baseline относится к другой политике или "
                          "проекту",
                "fresh": False, "added": sorted(current["entries"]),
                "removed": [], "changed": []}
    before, after = baseline["entries"], current["entries"]
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(rel for rel in set(before) & set(after)
                     if before[rel] != after[rel])
    return {"complete": True, "reason": "",
            "fresh": not (added or removed or changed),
            "added": added, "removed": removed, "changed": changed}


def _validate_inventory_entry(entry, label, require):
    checks_contract._unknown(entry, INVENTORY_ENTRY_FIELDS, label, require)
    require(entry.get("type") in {"file", "dir"},
            f"{label}.type должен быть file|dir")
    modules = entry.get("modules")
    require(isinstance(modules, list)
            and all(isinstance(m, str) for m in modules),
            f"{label}.modules должен быть списком модулей")


def validate_inventory(doc, require):
    """Валидировать документ checks-inventory схемы 1; возвратить doc."""
    checks_contract._unknown(doc, INVENTORY_FIELDS, "inventory", require)
    require(type(doc.get("schema_version")) is int
            and doc["schema_version"] == INVENTORY_SCHEMA_VERSION,
            f"inventory: неподдерживаемая schema_version "
            f"{doc.get('schema_version')!r}")
    require(doc.get("kind") == INVENTORY_KIND,
            f"inventory: kind должен быть {INVENTORY_KIND!r}")
    checks_contract._str(doc.get("project_id"), "inventory.project_id",
                         require)
    checks_contract._digest(doc.get("policy_digest"),
                            "inventory.policy_digest", require)
    checks_contract._iso(doc.get("created_at"), "inventory.created_at",
                         require)
    entries = doc.get("entries")
    require(isinstance(entries, dict), "inventory.entries — объект")
    for rel, entry in sorted(entries.items()):
        checks_contract._path(rel, "inventory.entries key", require)
        _validate_inventory_entry(entry, f"inventory.entries[{rel}]",
                                  require)
    excluded = doc.get("excluded")
    require(isinstance(excluded, list), "inventory.excluded — список")
    for idx, item in enumerate(excluded):
        label = f"inventory.excluded[{idx}]"
        checks_contract._unknown(item, EXCLUDED_FIELDS, label, require)
        checks_contract._path(item.get("path"), f"{label}.path", require)
        checks_contract._str(item.get("reason"), f"{label}.reason",
                             require)
        require(item.get("origin") in EXCLUDED_ORIGINS,
                f"{label}: неизвестный origin {item.get('origin')!r}")
    unknown = doc.get("unknown_paths")
    require(isinstance(unknown, list)
            and all(isinstance(p, str) for p in unknown),
            "inventory.unknown_paths — список путей")
    for path in unknown:
        checks_contract._path(path, "inventory.unknown_paths", require)
    return doc


def _validate_member(member, label, require):
    checks_contract._unknown(member, MEMBER_FIELDS, label, require)
    require(member.get("type") in MEMBER_TYPES,
            f"{label}.type должен быть file|dir")
    if member["type"] == "file" and "sha256" in member:
        checks_contract._digest(member["sha256"], f"{label}.sha256",
                                require)
    require(member["type"] != "dir" or "sha256" not in member,
            f"{label}: sha256 не допускается для dir")


def _validate_entry(entry, label, require):
    checks_contract._unknown(entry, ENTRY_FIELDS, label, require)
    kind = entry.get("type")
    require(kind in ENTRY_TYPES,
            f"{label}.type должен быть file|directory|absent|"
            f"projection|value")
    if kind == "file":
        checks_contract._digest(entry.get("sha256"), f"{label}.sha256",
                                require)
        require("members" not in entry and "digest" not in entry,
                f"{label}: members/digest не допускаются для file")
    elif kind == "directory":
        members = entry.get("members")
        require(isinstance(members, dict),
                f"{label}.members — объект")
        for rel, member in sorted(members.items()):
            checks_contract._path(rel, f"{label}.members key", require)
            _validate_member(member, f"{label}.members[{rel}]", require)
        require("sha256" not in entry and "digest" not in entry,
                f"{label}: sha256/digest не допускаются для directory")
    elif kind == "absent":
        require(set(entry) == {"type"},
                f"{label}: поля кроме type не допускаются для absent")
    else:
        checks_contract._digest(entry.get("digest"), f"{label}.digest",
                                require)
        require("sha256" not in entry and "members" not in entry,
                f"{label}: sha256/members не допускаются для {kind}")


def validate_snapshot(doc, require):
    """Валидировать снимок checks-input-snapshot схемы 1; возвратить doc."""
    checks_contract._unknown(doc, SNAPSHOT_FIELDS, "snapshot", require)
    require(type(doc.get("schema_version")) is int
            and doc["schema_version"] == SNAPSHOT_SCHEMA_VERSION,
            f"snapshot: неподдерживаемая schema_version "
            f"{doc.get('schema_version')!r}")
    require(doc.get("kind") == SNAPSHOT_KIND,
            f"snapshot: kind должен быть {SNAPSHOT_KIND!r}; снимок входов "
            f"не является lifecycle snapshot")
    checks_contract._str(doc.get("project_id"), "snapshot.project_id",
                         require)
    checks_contract._digest(doc.get("policy_digest"),
                            "snapshot.policy_digest", require)
    checks_contract._iso(doc.get("created_at"), "snapshot.created_at",
                         require)
    entries = doc.get("entries")
    require(isinstance(entries, dict), "snapshot.entries — объект")
    for key, entry in sorted(entries.items()):
        label = f"snapshot.entries[{key}]"
        if key.startswith("path:"):
            if key[5:] != ".":
                checks_contract._path(key[5:], f"{label} path", require)
        elif key.startswith("environment:"):
            checks_contract._str(key[12:], f"{label} name", require)
        else:
            require(key in {"task_projection", "policy_projection"},
                    f"{label}: неизвестный ключ входа {key!r}")
        _validate_entry(entry, label, require)
    return doc


def _load(path, validator, label):
    data = json.loads(Path(path).read_bytes())
    if not isinstance(data, dict):
        raise ValueError(f"{label}: ожидается JSON-объект")
    return validator(data, _raise)


def load_snapshot(path):
    """Загрузить снимок входов; отсутствие и порча — ошибка, не reuse."""
    return _load(path, validate_snapshot, "snapshot")


def load_inventory(path):
    """Загрузить inventory; отсутствие и порча — ошибка, не reuse."""
    return _load(path, validate_inventory, "inventory")
