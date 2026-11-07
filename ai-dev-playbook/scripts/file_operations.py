"""Recoverable document operations and the shared command gate.

Generic write-engine for the lifecycle commands designed in
docs/designs/workflow-and-document-lifecycle.md §7 (ADR-0002). It is used
directly by tests today; the lifecycle CLI of PB-024+ builds on it.

Protocol per operation (spec -> staged -> backup -> recheck -> write ->
final check -> receipt):

1. The caller prepares a spec: writes (final bytes), removes, remove_dirs,
   inputs (expected pre-image digests for every touched and read path) and
   optional staged/final checks. Unknown fields and unsafe paths are
   rejected before anything runs.
2. The shared gate (work/gate.lock) is taken first, then the operation
   lock (work/lifecycle/lock) — a fixed order shared with updating.py,
   which keeps its own lock/backup format unchanged. Pending markers of
   both subsystems are refused.
3. work/lifecycle/<op>/ is created with a pending marker, a journal and
   the standalone restore.py; the changed files are prepared in a staged
   copy of the project and the staged checks must pass before any write.
4. backup/ copies the original bytes of every touched path; backup_index
   records backed/added/missing files, sha256 of copies, permission modes,
   planned final hashes and created/removed directories. Inputs are
   re-verified after backup and before the first project write.
5. Writes go through a temp file + replace; removals happen only after the
   new content is verified. On error every touched path is restored, only
   files/dirs created by the operation are removed, and the pre-image is
   verified. If recovery is impossible the marker stays and the command
   fails — a mixed state is never reported as success.
6. Restore (restore_operation or the standalone restore.py) validates the
   marker owner, index consistency, copy hashes and path safety. Foreign
   content that matches neither the pre-image nor the planned result is
   preserved under conflicts/ instead of being destroyed.

The gate is a non-blocking exclusive file lock (flock/msvcrt) held by an
open descriptor — the OS releases it on any process exit. Commands spawned
inside a gated run (e.g. configured verification checks) inherit the gate
token via PLAYBOOK_GATE and continue nested; a stale token never bypasses
a live foreign holder.
"""

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import scoped_operations
import stat
import tempfile
import uuid
from pathlib import Path, PurePosixPath

WORK_DIR = "work/lifecycle"
PENDING_FILE = WORK_DIR + "/pending.json"
LOCK_FILE = WORK_DIR + "/lock"
BACKUP_INDEX = "backup_index.json"
RESTORE_FILE = "restore.py"
GATE_FILE = "work/gate.lock"
GATE_ENV = "PLAYBOOK_GATE"
# Маркер механизма обновления (владелец — updating.py): gate проверяет оба.
UPDATE_PENDING_FILE = "work/update/pending.json"
BACKUP_SCHEMA = 1
HASH_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
STAGED_IGNORE = {".git", "work", "__pycache__", ".DS_Store", ".venv", "node_modules"}
STAGED_ROOT_IGNORE = {".git", "work", ".venv", "node_modules"}
STAGED_ANY_IGNORE = {"__pycache__", ".DS_Store"}


def staged_path_available(rel):
    """Scoped validation reads unchanged paths in place; reject cache/service evidence."""
    parts = PurePosixPath(rel).parts
    if not parts or any(p in STAGED_ANY_IGNORE or p.endswith('.pyc') for p in parts):
        return False
    if parts[0] == '.git':
        return False
    return rel not in {GATE_FILE, PENDING_FILE, UPDATE_PENDING_FILE,
                       'work', WORK_DIR, 'work/update',
                       WORK_DIR + '/lock', 'work/update/lock'}


def preview_operation(root, spec, safe_path, require):
    """Run the real staged checks outside the project, without backup/pending."""
    root, spec = scoped_operations.bind_planning_reads(root, spec)
    plan = _validate_spec(root, spec, safe_path, require)
    _require_inputs(root, plan['inputs'], safe_path, require)
    require(not Path(tempfile.gettempdir()).resolve().is_relative_to(root.resolve()),
            'Для read-only preview временный каталог должен находиться вне проекта (TMPDIR)')
    with tempfile.TemporaryDirectory(prefix='playbook-preview-') as directory:
        _prepare_staged(root, Path(directory).resolve(), plan, safe_path, require)
    _require_inputs(root, plan['inputs'], safe_path, require)


SPEC_KEYS = {"operation", "plan_id", "actor", "basis", "writes", "removes",
             "remove_dirs", "create_dirs", "inputs", "read_set", "staged_checks",
             "final_checks", "moves", "receipt"}


def sha256(data):
    if isinstance(data, scoped_operations.Payload):
        return data.sha256
    return "sha256:" + hashlib.sha256(data).hexdigest()


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _sync_dir(directory):
    if os.name == "posix":
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _journal(op_dir, event, **fields):
    line = json.dumps({"ts": now_iso(), "event": event, **fields}, ensure_ascii=False)
    with (op_dir / "journal.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _flock(stream):
    """Неблокирующая эксклюзивная блокировка; OSError = занято."""
    stream.seek(0)
    if os.name == "posix":
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)


def _lock_stream(root, safe_path, rel):
    """Открыть lock-файл и взять эксклюзивную блокировку; None — занято."""
    root = scoped_operations.physical_root(root)
    path = safe_path(root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        _flock(stream)
    except OSError:
        stream.close()
        return None
    return stream


class Gate:
    """Держатель общего gate. nested=True — токен получен от родительского
    процесса (настроенные проверки внутри verify), блокировка не наша."""

    def __init__(self, stream=None, token=None, nested=False):
        self._stream = stream
        self.token = token
        self.nested = nested

    def close(self):
        if self._stream is not None:
            try:
                # Убрать запись держателя: устаревший токен не должен
                # подтверждать вложенность после освобождения блокировки.
                self._stream.seek(0)
                self._stream.truncate()
            finally:
                self._stream.close()
                self._stream = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _gate_holder(root):
    root = scoped_operations.physical_root(root)
    try:
        data = json.loads((root / GATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def pending_info(root):
    """Маркер незавершённой операции документов либо None."""
    root = scoped_operations.physical_root(root)
    path = root / PENDING_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"op_id": "unknown"}
    if not isinstance(data, dict):
        data = {"op_id": "unknown"}
    data["path"] = path
    return data


def pending_hint(root, rel):
    """Текст отказа по маркеру незавершённой операции."""
    if rel == UPDATE_PENDING_FILE:
        return ("Незавершённое обновление комплекта: обычные команды заблокированы. "
                "Выполните playbook.py update restore или playbook.py update status")
    try:
        marker = json.loads((root / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        marker = {}
    if not isinstance(marker, dict):
        marker = {}
    op = marker.get("op_dir") or WORK_DIR + "/<операция>"
    return (f"Незавершённая операция документов ({marker.get('op_id', '?')}): "
            f"обычные команды заблокированы. Восстановление: "
            f"python3 {op}/{RESTORE_FILE}")


def read_only_gate_check(root, safe_path, require, token=None):
    """Probe existing state without creating/truncating a lock or pending file."""
    for rel in (UPDATE_PENDING_FILE, PENDING_FILE):
        require(not safe_path(root, rel).exists(), pending_hint(root, rel))
    path = safe_path(root, GATE_FILE)
    if not path.exists():
        return
    with path.open('rb') as stream:
        try:
            _flock(stream)
        except OSError:
            holder = _gate_holder(root)
            nested = token or os.environ.get(GATE_ENV)
            require(nested and holder and holder.get('token') == nested,
                    'Другая команда удерживает блокировку записи — дождитесь её завершения')


def acquire_gate(root, safe_path, require, owner, allowed_pending=()):
    """Общая блокировка изменяющих команд проекта.

    Порядок: сначала этот gate, затем специализированный lock операции
    (update/lifecycle) — обратный порядок не используется нигде. Пока gate
    занят, другая изменяющая команда получает отказ. Процессы, запущенные
    внутри удерживающей команды (env PLAYBOOK_GATE == записанному токену),
    проходят как вложенные — так настроенные проверки verify не блокируют
    сами себя. Проверка pending-маркеров выполняется под блокировкой.
    """
    path = safe_path(root, GATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        _flock(stream)
    except OSError:
        # Блокировка занята: вложенность признаём только по живому держателю —
        # токен из окружения обязан совпадать с записью в lock-файле.
        holder = _gate_holder(root)
        token = os.environ.get(GATE_ENV)
        if token and holder and holder.get("token") == token:
            stream.close()
            return Gate(token=token, nested=True)
        stream.close()
        who = holder.get("owner") if isinstance(holder, dict) else None
        require(False,
                "Другая команда удерживает блокировку записи"
                + (f" ({who})" if who else "")
                + " — операция выполняется: дождитесь её завершения")
    try:
        for rel in (UPDATE_PENDING_FILE, PENDING_FILE):
            if rel not in allowed_pending and (root / rel).is_file():
                raise ValueError(pending_hint(root, rel))
        token = uuid.uuid4().hex
        stream.seek(0)
        stream.truncate()
        holder = {"token": token, "owner": owner, "pid": os.getpid(),
                  "at": now_iso()}
        stream.write(json.dumps(holder, ensure_ascii=False).encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    except BaseException:
        stream.close()
        raise
    return Gate(stream=stream, token=token)


def _operation_lock(root, safe_path):
    """Специализированная блокировка операций документов (после gate)."""
    return _lock_stream(root, safe_path, LOCK_FILE)


def _release_pending(root, op_id):
    """Снять маркер, только если он принадлежит этой операции."""
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
    _sync_dir(path.parent)
    return True


def snapshot_paths(root, rels, safe_path, require):
    """{rel: 'sha256:..' | 'dir' | 'missing'} — ожидаемый пред-образ входов."""
    result = {}
    for rel in rels:
        path = safe_path(root, rel)
        if path.is_file():
            result[rel] = scoped_operations.file_digest(path)
        elif path.is_dir():
            result[rel] = "dir"
        elif not path.exists() and not path.is_symlink():
            result[rel] = "missing"
        else:
            require(False, f"Вход не является файлом или каталогом: {rel}")
    return result


def _require_inputs(root, inputs, safe_path, require):
    """Сверка текущего состояния с пред-образом из спецификации."""
    changed = []
    for rel, expected in inputs.items():
        current = snapshot_paths(root, [rel], safe_path, require)[rel]
        if current != expected:
            changed.append(rel)
    require(not changed,
            "Входы изменились после построения плана: "
            + ", ".join(sorted(changed))
            + " — операция отменена до записи")


def _check_rel(root, rel, safe_path, require, write):
    require(isinstance(rel, str) and rel, "Путь должен быть непустой строкой")
    require(PurePosixPath(rel).as_posix() == rel and not rel.endswith("/"),
            f"Неканонический относительный путь: {rel!r}")
    safe_path(root, rel)
    if write:
        protected = (WORK_DIR, "work/update", GATE_FILE)
        require(not rel.startswith(tuple(item + "/" for item in protected))
                and rel not in protected,
                f"Служебный путь операций недопустим в write-set: {rel}")


def _validate_spec(root, spec, safe_path, require):
    """Проверка spec до всякой записи; возвращает нормализованный план."""
    require(isinstance(spec, dict), "Спецификация операции должна быть объектом")
    unknown = sorted(set(spec) - SPEC_KEYS)
    require(not unknown, f"Неизвестные поля операции: {', '.join(unknown)}")
    operation, plan_id = spec.get("operation"), spec.get("plan_id")
    require(isinstance(operation, str) and operation.strip(),
            "operation должен быть непустой строкой")
    require(isinstance(plan_id, str) and ID_RE.fullmatch(plan_id) is not None,
            "plan_id обязателен: [A-Za-z0-9_.-]")
    actor = spec.get("actor")
    require(actor is None or isinstance(actor, str), "actor должен быть строкой")
    writes = spec.get("writes") or {}
    removes = spec.get("removes") or []
    remove_dirs = spec.get("remove_dirs") or []
    create_dirs = spec.get("create_dirs") or []
    require(isinstance(writes, dict), "writes должен быть объектом путь→bytes")
    require(isinstance(removes, list) and isinstance(remove_dirs, list)
            and isinstance(create_dirs, list),
            "removes/remove_dirs/create_dirs должны быть списками")
    for rel, content in writes.items():
        _check_rel(root, rel, safe_path, require, write=True)
        require(isinstance(content, (bytes, bytearray, scoped_operations.Payload)),
                f"writes[{rel}]: содержимое должно быть bytes")
    for rel in removes + remove_dirs + create_dirs:
        _check_rel(root, rel, safe_path, require, write=True)
    file_set, dir_set, create_set = \
        set(writes) | set(removes), set(remove_dirs), set(create_dirs)
    require(not set(writes) & set(removes), "Пересечение writes/removes")
    require(not file_set & dir_set, "Путь одновременно файл и каталог в write-set")
    require(not file_set & create_set,
            "Путь одновременно файл и каталог в write-set")
    require(not dir_set & create_set,
            "Каталог одновременно в remove_dirs и create_dirs")
    for directory in dir_set:
        require(not any(rel.startswith(directory + "/") for rel in writes),
                f"remove_dirs {directory} содержит записываемый путь")
        require(not any(directory.startswith(rel + "/") for rel in file_set),
                f"remove_dirs {directory} вложен в файловый путь")
        require(not any(directory == rel or directory.startswith(rel + "/")
                        for rel in create_set),
                f"remove_dirs {directory} пересекается с create_dirs")
    for directory in create_set:
        path = safe_path(root, directory)
        require(not path.exists() and not path.is_symlink(),
                f"create_dirs содержит существующий путь: {directory}")
        require(not any(directory.startswith(rel + "/") for rel in file_set),
                f"create_dirs {directory} вложен в файловый путь")
    # Удаление — от самых глубоких каталогов, создание — от верхних.
    remove_dirs = sorted(dir_set,
                         key=lambda p: (-len(PurePosixPath(p).parts), p))
    create_dirs = sorted(create_set,
                         key=lambda p: (len(PurePosixPath(p).parts), p))
    touched = file_set | dir_set | create_set
    require(touched, "Пустой write-set: нечего применять")
    for rel in removes:
        path = safe_path(root, rel)
        require(not path.exists() or path.is_file(),
                f"removes содержит не файл: {rel}")
    for rel in remove_dirs:
        path = safe_path(root, rel)
        require(not path.exists() or path.is_dir(),
                f"remove_dirs содержит не каталог: {rel}")
    inputs = spec.get("inputs")
    require(isinstance(inputs, dict),
            "inputs обязателен: снимок входов для повторной сверки")
    for rel, expected in inputs.items():
        _check_rel(root, rel, safe_path, require, write=False)
        require(expected in ("missing", "dir")
                or (isinstance(expected, str) and HASH_RE.fullmatch(expected)),
                f"inputs[{rel}]: ожидается sha256, 'missing' или 'dir'")
    require(touched <= set(inputs),
            "inputs должен покрывать все затрагиваемые пути: "
            + ", ".join(sorted(touched - set(inputs))))
    for field in ("staged_checks", "final_checks"):
        checks = spec.get(field) or []
        require(isinstance(checks, list) and all(callable(item) for item in checks),
                f"{field} должен быть списком вызываемых проверок")
    moves = spec.get("moves")
    require(moves is None or isinstance(moves, dict), "moves должен быть объектом")
    expected = {rel: sha256(content) for rel, content in writes.items()}
    for rel in removes + remove_dirs:
        expected[rel] = "absent"
    for rel in create_dirs:
        expected[rel] = "dir"
    return {"operation": operation.strip(), "plan_id": plan_id,
            "actor": actor or "unknown", "basis": spec.get("basis"),
            "writes": {rel: bytes(content) if not isinstance(content, scoped_operations.Payload) else content
                       for rel, content in writes.items()},
            "removes": sorted(set(removes)), "remove_dirs": remove_dirs,
            "create_dirs": create_dirs,
            "inputs": dict(inputs), "read_set": spec.get("read_set"),
            "staged_checks": spec.get("staged_checks") or [],
            "final_checks": spec.get("final_checks") or [],
            "moves": moves or {}, "receipt": spec.get("receipt") or {},
            "expected": expected, "touched": touched}


def find_completed(root, plan_id):
    """op_id успешно завершённой операции с этим plan_id либо None."""
    root = scoped_operations.physical_root(root)
    base = root / WORK_DIR
    if not base.is_dir():
        return None
    for op in sorted(base.iterdir()):
        receipt = op / "receipt.json"
        try:
            data = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("plan_id") == plan_id \
                and data.get("result") == "completed":
            return op.name
    return None


def _verify_expected(root, expected, safe_path, require):
    """Финальная сверка состава: записанные хеши и отсутствие удалённых."""
    problems = []
    for rel, want in sorted(expected.items()):
        path = safe_path(root, rel)
        if want == "absent":
            if path.exists() or path.is_symlink():
                problems.append(f"{rel} (должен отсутствовать)")
        elif want == "dir":
            if not path.is_dir():
                problems.append(f"{rel} (нет каталога)")
        elif not path.is_file():
            problems.append(f"{rel} (нет файла)")
        elif scoped_operations.file_digest(path) != want:
            problems.append(f"{rel} (содержимое не совпадает)")
    require(not problems,
            "Итоговое состояние не совпадает с планом: " + "; ".join(problems))


def _verify_post_modes(root, index, safe_path, require):
    for rel, mode in {**index["planned_modes"], **index["planned_dir_modes"]}.items():
        target = safe_path(root, rel)
        require(target.exists() and stat.S_IMODE(target.stat().st_mode) == mode,
                f"Итоговые права не совпадают с планом: {rel}")


def _atomic_write(target, content, op_id):
    """Временный файл с эксклюзивным созданием рядом с целью + replace.

    Имя уникально и содержит op_id: существующий файл или ссылка с таким
    именем не перезаписываются (O_EXCL), а оставшийся после прерывания
    файл доказуемо принадлежит операции и удаляется восстановлением.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    name = f".{target.name}.fop-{op_id}-{uuid.uuid4().hex[:8]}.tmp"
    temporary = target.parent / name
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if isinstance(content, scoped_operations.Payload):
                content.copy_to(stream)
            else:
                stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if isinstance(content, scoped_operations.Payload):
            os.chmod(temporary, content.mode)
        elif target.exists():
            os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
        temporary.replace(target)
        _sync_dir(target.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _own_temp_pattern(op_id):
    return re.compile(r"\..*\.fop-" + re.escape(op_id) + r"-[0-9a-f]{8}\.tmp\Z")


def _remove_own_temps(root, op_dir, index, safe_path):
    """Удалить временные файлы этой операции, оставшиеся после сбоя."""
    pattern = _own_temp_pattern(op_dir.name)
    parents = {PurePosixPath(rel).parent
               for rel in index["backed"] + index["added"] + index["missing"]}
    for parent in parents:
        directory = root if str(parent) == "." else safe_path(root, str(parent))
        if not directory.is_dir():
            continue
        for stray in directory.iterdir():
            if (stray.is_file() or stray.is_symlink()) \
                    and pattern.fullmatch(stray.name):
                stray.unlink()


def _prepare_staged(root, op_dir, plan, safe_path, require):
    """Prepare only payload files, then validate a read-only future tree."""
    return scoped_operations.prepare(root, op_dir, plan, safe_path, require,
                                     _journal, _verify_expected)


def _write_backup(root, op_dir, plan, safe_path, require):
    """Резервная копия и индекс всех затрагиваемых путей до записи."""
    backup = op_dir / "backup"
    backed, added, missing, hashes, modes = [], [], [], {}, {}
    for rel in sorted(set(plan["writes"]) | set(plan["removes"])):
        source = safe_path(root, rel)
        require(not source.is_symlink(), f"Символическая ссылка в write-set: {rel}")
        if source.is_file():
            copy = backup / rel
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, copy)
            with copy.open("rb") as backup_stream:
                os.fsync(backup_stream.fileno())
            backed.append(rel)
            hashes[rel] = scoped_operations.file_digest(copy)
            modes[rel] = stat.S_IMODE(source.stat().st_mode)
        elif source.exists():
            require(False, f"Затрагиваемый путь не является файлом: {rel}")
        elif rel in plan["writes"]:
            added.append(rel)
        else:
            missing.append(rel)
    removed_dirs = []
    dir_modes = {}
    for rel in plan["remove_dirs"]:
        path = safe_path(root, rel)
        if path.exists():
            require(path.is_dir(), f"remove_dirs содержит не каталог: {rel}")
            removed_dirs.append(rel)
            dir_modes[rel] = stat.S_IMODE(path.stat().st_mode)
    created = set()
    for rel in plan["writes"]:
        parent = PurePosixPath(rel).parent
        while str(parent) != ".":
            if not (root / str(parent)).exists():
                created.add(str(parent))
            parent = parent.parent
    for rel in plan["create_dirs"]:
        current = PurePosixPath(rel)
        while str(current) != ".":
            if not (root / str(current)).exists():
                created.add(str(current))
            current = current.parent
    created_dirs = sorted(created, key=lambda p: -len(PurePosixPath(p).parts))
    index = {"schema_version": BACKUP_SCHEMA, "op_id": op_dir.name,
             "created_at": now_iso(), "backed": backed, "added": added,
             "missing": missing, "hashes": hashes, "modes": modes,
             "planned": {rel: plan["expected"][rel]
                         for rel in backed + added + missing},
             "created_dirs": created_dirs, "removed_dirs": removed_dirs,
             "dir_modes": dir_modes,
             "planned_modes": {rel: modes.get(rel, plan["_staged_modes"][rel])
                               for rel in plan["writes"]},
             "planned_dir_modes": {rel: stat.S_IMODE((op_dir / "staged" / rel).stat().st_mode)
                                   for rel in created_dirs}}
    # Индекс пишется атомарно и сбрасывается на диск: его наличие — граница,
    # после которой операция могла начать запись в проект.
    index_tmp = (op_dir / BACKUP_INDEX).with_suffix(".tmp")
    with index_tmp.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    index_tmp.replace(op_dir / BACKUP_INDEX)
    _sync_dir(op_dir)
    _journal(op_dir, "backup_complete", backed=len(backed), added=len(added))
    return index


def _read_backup_index(root, op_dir, safe_path, require):
    """Полная проверка backup_index до восстановления: отказ без изменений.

    Состав должен быть самосогласованным: backed/added/missing не
    пересекаются и без повторов, hashes/modes покрывают ровно backed,
    planned — ровно затрагиваемые файлы, набор копий на диске равен backed,
    каждая копия проходит sha256. Выпавший из индекса файл отклоняет
    восстановление, а не пропускается молча.
    """
    index_path = op_dir / BACKUP_INDEX
    require(index_path.is_file() and not index_path.is_symlink(),
            f"Нет {BACKUP_INDEX} в {op_dir.relative_to(root)}: пред-образ "
            f"недоступен, восстановление невозможно — маркер сохранён")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    require(isinstance(index, dict), f"{BACKUP_INDEX}: ожидается JSON-объект")
    require(index.get("schema_version") == BACKUP_SCHEMA,
            f"{BACKUP_INDEX}: неподдерживаемая схема — маркер сохранён")
    require(index.get("op_id") == op_dir.name,
            f"{BACKUP_INDEX}: индекс принадлежит другой операции — маркер сохранён")
    problems = []
    lists = {}
    for key in ("backed", "added", "missing", "created_dirs", "removed_dirs"):
        value = index.get(key)
        if not (isinstance(value, list)
                and all(isinstance(rel, str) for rel in value)):
            problems.append(f"{key} не является списком строк")
            value = []
        elif len(set(value)) != len(value):
            problems.append(f"повторы в {key}")
        lists[key] = value
    backed = lists["backed"]
    touched = lists["backed"] + lists["added"] + lists["missing"]
    for pair in (("backed", "added"), ("backed", "missing"), ("added", "missing")):
        overlap = sorted(set(lists[pair[0]]) & set(lists[pair[1]]))
        if overlap:
            problems.append("пересечение " + "/".join(pair) + ": " + ", ".join(overlap))
    overlap = sorted(set(lists["created_dirs"]) & set(lists["removed_dirs"]))
    if overlap:
        problems.append("пересечение каталогов: " + ", ".join(overlap))
    dir_overlap = sorted(
        (set(lists["created_dirs"]) | set(lists["removed_dirs"])) & set(touched))
    if dir_overlap:
        problems.append("каталоги пересекаются с файловыми путями: "
                        + ", ".join(dir_overlap))
    hashes, modes, planned = index.get("hashes"), index.get("modes"), index.get("planned")
    if not (isinstance(hashes, dict) and set(hashes) == set(backed)
            and all(isinstance(digest, str) and HASH_RE.fullmatch(digest)
                    for digest in hashes.values())):
        problems.append("hashes не соответствует backed")
    if not (isinstance(modes, dict) and set(modes) == set(backed)
            and all(type(mode) is int and 0 <= mode <= 0o7777
                    for mode in modes.values())):
        problems.append("modes не соответствует backed")
    dir_modes = index.get("dir_modes")
    if not (isinstance(dir_modes, dict)
            and set(dir_modes) == set(lists["removed_dirs"])
            and all(type(mode) is int and 0 <= mode <= 0o7777
                    for mode in dir_modes.values())):
        problems.append("dir_modes не соответствует removed_dirs")
    if not (isinstance(planned, dict) and set(planned) == set(touched)
            and all(value == "absent"
                    or (isinstance(value, str) and HASH_RE.fullmatch(value))
                    for value in planned.values())):
        problems.append("planned не соответствует затрагиваемым путям")
    for field, wanted in (("planned_modes", {rel for rel, value in (planned or {}).items() if value != "absent"}),
                          ("planned_dir_modes", set(lists["created_dirs"]))):
        extra = index.get(field)
        if extra is not None and not (isinstance(extra, dict) and set(extra) == wanted
                and all(type(mode) is int and 0 <= mode <= 0o7777 for mode in extra.values())):
            problems.append(field + " не соответствует планируемым путям")
    for rel in touched + lists["created_dirs"] + lists["removed_dirs"]:
        try:
            _check_rel(root, rel, safe_path, require, write=True)
        except ValueError as error:
            problems.append(str(error))
    backup_dir = op_dir / "backup"
    on_disk = set()
    if backup_dir.is_dir():
        for path in backup_dir.rglob("*"):
            if path.is_file():
                rel = path.relative_to(backup_dir).as_posix()
                on_disk.add(rel)
    unlisted = sorted(on_disk - set(backed))
    if unlisted:
        problems.append("копии без записи в backed: " + ", ".join(unlisted))
    if isinstance(hashes, dict):
        for rel in backed:
            source = backup_dir / rel
            if not source.is_file() or source.is_symlink() or source.stat().st_nlink != 1:
                problems.append(f"{rel} (нет независимой обычной копии)")
            elif isinstance(hashes.get(rel), str) \
                    and scoped_operations.file_digest(source) != hashes[rel]:
                problems.append(f"{rel} (копия повреждена)")
    require(not problems,
            "Резервная копия неполна или повреждена: " + "; ".join(problems)
            + " — восстановление отклонено, маркер сохранён")
    return index


def _keep_conflict(op_dir, root, target, rel):
    """Сохранить чужое содержимое в conflicts/ вместо молчаливого удаления."""
    destination = op_dir / "conflicts" / rel
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        stem = destination.name
        counter = 1
        while destination.exists() or destination.is_symlink():
            destination = destination.with_name(stem + f".conflict-{counter:02d}")
            counter += 1
    shutil.move(str(target), str(destination))


def _restore_from_backup(root, op_dir, safe_path, require):
    """Восстановление по проверенному backup_index.

    Детерминированно: added удаляются, backed возвращаются с режимами
    доступа, missing остаются отсутствующими, removed_dirs создаются,
    created_dirs убираются если пусты. Содержимое, совпадающее ни с
    пред-образом, ни с запланированным результатом, — поздняя внешняя
    правка — сохраняется в conflicts/ и сообщается, а не уничтожается.
    """
    index = _read_backup_index(root, op_dir, safe_path, require)
    backup_dir = op_dir / "backup"
    planned = index["planned"]
    conflicts = []
    _remove_own_temps(root, op_dir, index, safe_path)

    def state_of(path):
        if path.is_symlink():
            return ("other", None)
        if path.is_file():
            return (scoped_operations.file_digest(path), stat.S_IMODE(path.stat().st_mode))
        if path.exists():
            return ("other", stat.S_IMODE(path.stat().st_mode))
        return ("absent", None)

    def matches_planned(rel, state):
        expected_mode = index.get("planned_modes", {}).get(rel, index["modes"].get(rel))
        return expected_mode is not None and state == (planned[rel], expected_mode)

    def directory_matches_preimage(directory):
        for path in [directory] + list(directory.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                return False
            if path.is_dir():
                if rel not in index["dir_modes"] or stat.S_IMODE(path.stat().st_mode) != index["dir_modes"][rel]:
                    return False
            elif rel not in index["hashes"] or state_of(path) != (index["hashes"][rel], index["modes"][rel]):
                return False
        return True

    # Restore directory permissions before files; preserve foreign trees first.
    for rel in sorted(index["removed_dirs"], key=lambda p: len(PurePosixPath(p).parts)):
        target = safe_path(root, rel)
        if target.exists() and (not target.is_dir() or not directory_matches_preimage(target)):
            _keep_conflict(op_dir, root, target, rel)
            conflicts.append(rel)
        target.mkdir(parents=True, exist_ok=True)
        os.chmod(target, index["dir_modes"][rel] | 0o700)

    removed = 0
    for rel in index["added"] + index["missing"]:
        target = safe_path(root, rel)
        current = state_of(target)
        if current[0] == "absent":
            continue
        if rel in index["added"] and matches_planned(rel, current):
            target.unlink()
            removed += 1
        else:
            _keep_conflict(op_dir, root, target, rel)
            conflicts.append(rel)
            removed += 1
    restored = 0
    for rel in index["backed"]:
        target = safe_path(root, rel)
        current = state_of(target)
        if current == (index["hashes"][rel], index["modes"][rel]):
            pass
        else:
            if current[0] != "absent" and not matches_planned(rel, current):
                _keep_conflict(op_dir, root, target, rel)
                conflicts.append(rel)
            copy = backup_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, scoped_operations.Payload(copy, index["hashes"][rel], mode=index["modes"][rel]), op_dir.name)
        os.chmod(target, index["modes"][rel])
        with target.open("rb") as restored_file:
            os.fsync(restored_file.fileno())
        require(scoped_operations.file_digest(target) == index["hashes"][rel],
                f"Восстановленный файл не совпадает с резервной копией: {rel}")
        restored += 1
    # Родители раньше потомков: mkdir родителя может создать вложенный
    # каталог по умолчанию — его режим всё равно восстановится chmod'ом.
    for rel in sorted(index["removed_dirs"],
                      key=lambda p: len(PurePosixPath(p).parts)):
        target = safe_path(root, rel)
        if not target.is_dir():
            if target.exists() or target.is_symlink():
                _keep_conflict(op_dir, root, target, rel)
                conflicts.append(rel)
            target.mkdir(parents=True)
        os.chmod(target, index["dir_modes"][rel])
        require(stat.S_IMODE(target.stat().st_mode) == index["dir_modes"][rel],
                f"Режим восстановленного каталога не совпадает: {rel}")
    for rel in index["created_dirs"]:
        target = safe_path(root, rel)
        if not target.exists():
            continue
        expected_mode = index.get("planned_dir_modes", {}).get(rel)
        if not target.is_dir() or expected_mode is None or stat.S_IMODE(target.stat().st_mode) != expected_mode:
            _keep_conflict(op_dir, root, target, rel)
            conflicts.append(rel)
            continue
        try:
            target.rmdir()
        except OSError:
            _keep_conflict(op_dir, root, target, rel)
            conflicts.append(rel)
    return restored, removed, conflicts


def run_operation(root, spec, safe_path, require):
    """Применить операцию документов с backup, журналом и восстановлением.

    spec — объект с полями operation, plan_id, actor, writes {rel: bytes},
    removes [rel], remove_dirs [rel], create_dirs [rel] (каталоги,
    создаваемые операцией — например пустые перенесённые), inputs
    {rel: sha256|'dir'|'missing'} (обязан покрывать все затрагиваемые
    пути), staged_checks/final_checks (callable(root)), moves/receipt
    (метаданные квитанции). Повторный запуск успешно завершённого plan_id
    сообщает already_completed.
    """
    root, spec = scoped_operations.bind_planning_reads(root, spec)
    plan = _validate_spec(root, spec, safe_path, require)
    # Ответ already_completed принимается только под удерживаемым gate после
    # проверки pending (в _apply_operation): квитанция completed может
    # принадлежать операции, чьё восстановление ещё не завершено или идёт
    # прямо сейчас — без блокировки чтение квитанции и состояния маркера
    # было бы гонкой с конкурирующим restore.
    gate = acquire_gate(root, safe_path, require,
                        owner=f"{plan['operation']}:{plan['plan_id']}")
    try:
        lock = _operation_lock(root, safe_path)
        require(lock is not None,
                "Операция документов выполняется другим процессом: "
                "дождитесь завершения; при прерывании выполните "
                f"{WORK_DIR}/<операция>/{RESTORE_FILE}")
        try:
            return _apply_operation(root, plan, safe_path, require)
        finally:
            lock.close()
    finally:
        gate.close()


def _apply_operation(root, plan, safe_path, require):
    pending = pending_info(root)
    require(pending is None, pending_hint(root, PENDING_FILE))
    prior = find_completed(root, plan["plan_id"])
    if prior is not None:
        return {"status": "already_completed", "plan_id": plan["plan_id"],
                "op_id": prior}
    op_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    op_dir = safe_path(root, WORK_DIR) / op_id
    op_dir.mkdir(parents=True, exist_ok=False)
    with (op_dir / RESTORE_FILE).open("x", encoding="utf-8") as restore_file:
        restore_file.write(RESTORE_SCRIPT)
        restore_file.flush()
        os.fsync(restore_file.fileno())
    _sync_dir(op_dir)
    _sync_dir(op_dir.parent)
    _journal(op_dir, "start", operation=plan["operation"],
             plan_id=plan["plan_id"], actor=plan["actor"])
    marker = root / PENDING_FILE
    try:
        with marker.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(
                {"op_id": op_id, "operation": plan["operation"],
                 "actor": plan["actor"], "started_at": now_iso(),
                 "op_dir": op_dir.relative_to(root).as_posix()},
                ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        _journal(op_dir, "abort", reason="pending marker appeared concurrently")
        require(False, pending_hint(root, PENDING_FILE))
    # Автономный restore доступен с любой точки прерывания: пишется до staged.
    try:
        _require_inputs(root, plan["inputs"], safe_path, require)
        _prepare_staged(root, op_dir, plan, safe_path, require)
        _require_inputs(root, plan["inputs"], safe_path, require)
        scoped_operations.recheck(root, plan, safe_path, require, _verify_expected)
        _write_backup(root, op_dir, plan, safe_path, require)
        backup_index = _read_backup_index(root, op_dir, safe_path, require)
        scoped_operations.validate_backup(backup_index, plan, require)
        _require_inputs(root, plan["inputs"], safe_path, require)
        scoped_operations.recheck(root, plan, safe_path, require, _verify_expected)
        _journal(op_dir, "rechecked")
    except Exception as error:
        # Запись в проект ещё не начиналась: отмена и снятие маркера безопасны.
        _journal(op_dir, "abort", reason="pre-write failure", error=str(error))
        released = _release_pending(root, op_id)
        if released:
            scoped_operations.cleanup_staged(op_dir)
            if not (op_dir / BACKUP_INDEX).exists():
                shutil.rmtree(op_dir)
        raise
    failed = None
    try:
        _journal(op_dir, "write_started")
        for rel in plan["create_dirs"]:
            safe_path(root, rel).mkdir(parents=True, exist_ok=False)
            _journal(op_dir, "create_dir", path=rel)
        for rel, content in plan["writes"].items():
            payload = scoped_operations.Payload(op_dir / "staged" / rel,
                                                plan["expected"][rel],
                                                mode=backup_index["planned_modes"][rel])
            _atomic_write(safe_path(root, rel), payload, op_id)
            _journal(op_dir, "write", path=rel)
        for rel in plan["removes"]:
            safe_path(root, rel).unlink(missing_ok=True)
            _journal(op_dir, "remove", path=rel)
        for rel in plan["remove_dirs"]:
            target = safe_path(root, rel)
            if target.exists():
                target.rmdir()
            _journal(op_dir, "remove_dir", path=rel)
        _verify_expected(root, plan["expected"], safe_path, require)
        _verify_post_modes(root, backup_index, safe_path, require)
        for check in plan["final_checks"]:
            check(root)
        _verify_expected(root, plan["expected"], safe_path, require)
        _verify_post_modes(root, backup_index, safe_path, require)
        _journal(op_dir, "verified")
    except Exception as error:
        failed = error
        _journal(op_dir, "failed", error=str(error))
    if failed is not None:
        try:
            restored, removed, conflicts = _restore_from_backup(
                root, op_dir, safe_path, require)
            _journal(op_dir, "restored", restored=restored, removed=removed,
                     conflicts=conflicts)
        except Exception as restore_error:
            _journal(op_dir, "restore_failed", error=str(restore_error))
            require(False,
                    f"Ошибка записи: {failed}. Восстановление неполное: "
                    f"{restore_error}. Маркер сохранён: после устранения "
                    f"причины выполните {op_dir.relative_to(root)}/{RESTORE_FILE}. "
                    f"Журнал: {op_dir.relative_to(root)}")
        _release_pending(root, op_id)
        scoped_operations.cleanup_staged(op_dir)
        note = (" Конфликтующее содержимое сохранено в "
                f"{op_dir.relative_to(root)}/conflicts/: {', '.join(conflicts)}"
                if conflicts else "")
        require(False,
                f"Ошибка записи: {failed}. Состояние восстановлено из резервной "
                f"копии ({restored} файлов).{note} "
                f"Журнал: {op_dir.relative_to(root)}")
    _write_receipt(op_dir, plan)
    _journal(op_dir, "done")
    _release_pending(root, op_id)
    warning = scoped_operations.cleanup_staged(op_dir)
    return {"warnings": [warning] if warning else [], "status": "completed", "op_id": op_id, "plan_id": plan["plan_id"],
            "op_dir": op_dir.relative_to(root).as_posix()}


def _require_pre_write(op_dir, require):
    """Доказать по журналу, что операция не дошла до резервного копирования.

    backup_index создаётся строго до первой записи в проект, а событие
    'backup_complete' журналируется после его записи: пока журнал содержит
    только 'start'/'staged', индекса не было и файлы проекта не трогались.
    Любое более позднее событие (или отсутствие журнала) при отсутствующем
    индексе означает утрату пред-образа — отказ, маркер сохраняется.
    """
    journal = op_dir / "journal.jsonl"
    events = []
    if journal.is_file():
        for line in journal.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line).get("event"))
            except ValueError:
                events.append("?")
    require(events and all(event in {"start", "staged"} for event in events),
            f"Нет {BACKUP_INDEX}: пред-образ утрачен — восстановление "
            f"невозможно, маркер сохранён")


def _neutralize_receipt(op_dir):
    """Квитанция completed прерванной операции после отката — не применение."""
    receipt = op_dir / "receipt.json"
    if not receipt.is_file():
        return
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict) and data.get("result") == "completed":
        data["result"] = "restored"
        data["restored_at"] = now_iso()
        receipt.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")


def _write_receipt(op_dir, plan):
    receipt = {"schema_version": 1, "op_id": op_dir.name,
               "plan_id": plan["plan_id"], "operation": plan["operation"],
               "actor": plan["actor"], "basis": plan["basis"],
               "result": "completed", "finished_at": now_iso(),
               "expected": plan["expected"], "moves": plan["moves"],
               "details": plan["receipt"]}
    target = op_dir / "receipt.json"
    temporary = op_dir / ".receipt.tmp"
    with temporary.open("x", encoding="utf-8") as output:
        output.write(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(target)
    _sync_dir(op_dir)


def restore_operation(root, safe_path, require):
    """Восстановить незавершённую операцию по маркеру и backup_index.

    Отказывает без изменений, если операция жива (удерживает блокировки),
    маркер чужой/повреждён или пред-образ неполон; маркер при этом
    сохраняется. После подтверждённого возврата всех путей маркер снимается;
    повторный вызов безопасно сообщает об отсутствии операции.
    """
    pending = pending_info(root)
    require(pending is not None, "Нет незавершённой операции документов")
    gate = acquire_gate(root, safe_path, require, owner="lifecycle-restore",
                        allowed_pending={PENDING_FILE})
    try:
        lock = _operation_lock(root, safe_path)
        require(lock is not None,
                "Операция документов выполняется другим процессом: "
                "восстановление запрещено до её завершения")
        try:
            base = safe_path(root, WORK_DIR).resolve()
            rel_dir = pending.get("op_dir")
            op_dir = (root / rel_dir).resolve() \
                if isinstance(rel_dir, str) and rel_dir else None
            require(op_dir is not None and op_dir.parent == base
                    and op_dir.name == pending.get("op_id"),
                    f"Маркер повреждён или ссылается на чужой каталог "
                    f"({rel_dir!r}): восстановление отклонено — маркер сохранён")
            require(op_dir.is_dir(),
                    f"Нет каталога операции {rel_dir}: пред-образ утрачен, "
                    f"восстановление невозможно — маркер сохранён")
            if not (op_dir / BACKUP_INDEX).is_file():
                # Индекс создаётся строго до первой записи в проект. Его
                # отсутствие доказывает «записи не было» только при журнале
                # без событий после staged; иначе пред-образ утрачен и
                # восстановление отклоняется с сохранением маркера.
                _require_pre_write(op_dir, require)
                _journal(op_dir, "pre_write_abort", op_id=pending["op_id"])
                _release_pending(root, pending["op_id"])
                scoped_operations.cleanup_staged(op_dir)
                return {"status": "aborted_before_write",
                        "restored": 0, "removed": 0, "conflicts": [],
                        "op_dir": op_dir.relative_to(root).as_posix()}
            restored, removed, conflicts = _restore_from_backup(
                root, op_dir, safe_path, require)
            _neutralize_receipt(op_dir)
            scoped_operations.cleanup_staged(op_dir)
            _journal(op_dir, "restored_by_command", restored=restored,
                     removed=removed, conflicts=conflicts)
            if not _release_pending(root, pending["op_id"]):
                _journal(op_dir, "pending_foreign", op_id=pending["op_id"])
            return {"status": "restored", "restored": restored,
                    "removed": removed, "conflicts": conflicts,
                    "op_dir": op_dir.relative_to(root).as_posix()}
        finally:
            lock.close()
    finally:
        gate.close()


RESTORE_SCRIPT = '''#!/usr/bin/env python3
"""Автономное восстановление прерванной операции документов playbook.

Запуск: python3 restore.py — из каталога операции work/lifecycle/<op>/.
Не зависит от установленного ai-dev-playbook/scripts/: импортирует только
стандартную библиотеку. Отказ ничего не меняет и не снимает маркер.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
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


# Живая операция держит общий gate и блокировку операции; восстановление
# разрешено только после реального прерывания, когда ОС их уже сняла.
gate_stream = (root / "work" / "gate.lock").open("a+b")
try:
    lock(gate_stream)
except OSError:
    print("Другая команда удерживает блокировку записи: восстановление "
          "запрещено, дождитесь завершения.", file=sys.stderr)
    sys.exit(1)
lock_stream = (root / "work" / "lifecycle" / "lock").open("a+b")
try:
    lock(lock_stream)
except OSError:
    print("Операция документов выполняется: восстановление запрещено, "
          "дождитесь завершения.", file=sys.stderr)
    sys.exit(1)

update_pending = root / "work" / "update" / "pending.json"
if update_pending.is_file():
    print("Обнаружен маркер незавершённого обновления: восстановление "
          "операции документов запрещено до разрешения обновления. "
          "Файлы проекта и маркер не изменены.", file=sys.stderr)
    sys.exit(1)


def fail(message):
    print(message + " Маркер незавершённой операции сохранён.", file=sys.stderr)
    sys.exit(1)


def refuse(message):
    print(message + " Файлы проекта и маркер не изменены.", file=sys.stderr)
    sys.exit(1)


def digest(path):
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        fail("Symlink in recovery data: " + str(path))
    checksum = hashlib.sha256()
    with path.open("rb") as incoming:
        for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
            checksum.update(chunk)
    return "sha256:" + checksum.hexdigest()


def state_of(path):
    if path.is_symlink():
        return ("other", None)
    if path.is_file():
        return (digest(path), path.stat().st_mode & 0o7777)
    if path.exists():
        return ("other", path.stat().st_mode & 0o7777)
    return ("absent", None)


def matches_planned(rel, state):
    expected_mode = index.get("planned_modes", {}).get(rel, modes.get(rel))
    return expected_mode is not None and state == (planned[rel], expected_mode)


def restore_file(source, target, expected, mode):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / ("." + target.name + ".fop-" + op_dir.name
                                + "-" + uuid.uuid4().hex[:8] + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        checksum = hashlib.sha256()
        with os.fdopen(descriptor, "wb") as outgoing, source.open("rb") as incoming:
            for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                checksum.update(chunk)
                outgoing.write(chunk)
            outgoing.flush()
            if "sha256:" + checksum.hexdigest() != expected:
                fail("Backup changed during restore: " + str(source))
            os.chmod(temporary, mode)
            os.fsync(outgoing.fileno())
        temporary.replace(target)
        if os.name == "posix":
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def cleanup_staged():
    staged = op_dir / "staged"
    try:
        if staged.is_symlink():
            raise ValueError("staged became a symlink")
        if staged.exists():
            shutil.rmtree(staged)
    except (OSError, ValueError) as error:
        print("Restore completed; staged cleanup failed: " + str(error), file=sys.stderr)


def keep_conflict(target, rel):
    destination = op_dir / "conflicts" / rel
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        stem = destination.name
        counter = 1
        while destination.exists() or destination.is_symlink():
            destination = destination.with_name(stem + f".conflict-{counter:02d}")
            counter += 1
    shutil.move(str(target), str(destination))


# Восстановление разрешено только собственной операции: маркер должен
# существовать и ссылаться на этот каталог. Чужой, отсутствующий или
# повреждённый маркер — отказ без каких-либо изменений.
pending = root / "work" / "lifecycle" / "pending.json"
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
if index_path.is_symlink():
    fail("Backup index is a symlink.")
if not index_path.is_file():
    # Индекс создаётся строго до первой записи в проект, а событие
    # 'backup_complete' журналируется после его записи: журнал из одних
    # 'start'/'staged' доказывает, что файлы проекта не трогались. Любое
    # более позднее событие при отсутствующем индексе — утрата пред-образа.
    journal = op_dir / "journal.jsonl"
    events = []
    if journal.is_file():
        for line in journal.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line).get("event"))
            except ValueError:
                events.append("?")
    if not events or any(event not in {"start", "staged"} for event in events):
        fail("Нет backup_index.json: пред-образ утрачен, восстановление "
             "невозможно.")
    pending.unlink()
    cleanup_staged()
    print("Операция прервана до записи в проект: восстановление не "
          "требуется, маркер снят.")
    sys.exit(0)
try:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert isinstance(index, dict)
    assert index.get("schema_version") == 1
    assert index.get("op_id") == op_dir.name
    backed, added, missing = index["backed"], index["added"], index["missing"]
    created_dirs = index["created_dirs"]
    removed_dirs = index["removed_dirs"]
    hashes, modes, planned = index["hashes"], index["modes"], index["planned"]
    dir_modes = index["dir_modes"]
    for field, wanted in (("planned_modes", {rel for rel, value in planned.items() if value != "absent"}),
                          ("planned_dir_modes", set(created_dirs))):
        extra = index.get(field)
        if extra is not None:
            assert isinstance(extra, dict) and set(extra) == wanted
            assert all(type(mode) is int and 0 <= mode <= 0o7777 for mode in extra.values())
    for value in (backed, added, missing, created_dirs, removed_dirs):
        assert isinstance(value, list)
        assert all(isinstance(rel, str) for rel in value)
        assert len(set(value)) == len(value)
    assert not set(backed) & set(added)
    assert not set(backed) & set(missing)
    assert not set(added) & set(missing)
    assert not set(created_dirs) & set(removed_dirs)
    assert isinstance(hashes, dict) and set(hashes) == set(backed)
    assert isinstance(modes, dict) and set(modes) == set(backed)
    assert all(type(mode) is int and 0 <= mode <= 0o7777
               for mode in modes.values())
    assert isinstance(dir_modes, dict) and set(dir_modes) == set(removed_dirs)
    assert all(type(mode) is int and 0 <= mode <= 0o7777
               for mode in dir_modes.values())
    assert isinstance(planned, dict)
    assert set(planned) == set(backed) | set(added) | set(missing)
    assert all(value == "absent" or (isinstance(value, str)
               and value.startswith("sha256:") and len(value) == 71)
               for value in planned.values())
    assert all(isinstance(digest_, str) and digest_.startswith("sha256:")
               and len(digest_) == 71 for digest_ in hashes.values())
except (ValueError, KeyError, AssertionError):
    fail("backup_index.json повреждён или неполон: восстановление невозможно.")
for rel in backed + added + missing + created_dirs + removed_dirs:
    candidate = Path(rel)
    resolved = (root / candidate).resolve()
    if candidate.is_absolute() or ".." in candidate.parts \\
            or resolved == root or root not in resolved.parents:
        fail(f"backup_index.json содержит путь вне проекта: {rel}.")
backup_dir = op_dir / "backup"
on_disk = set()
if backup_dir.is_dir():
    for path in backup_dir.rglob("*"):
        if path.is_file():
            rel = path.relative_to(backup_dir).as_posix()
            on_disk.add(rel)
unlisted = sorted(on_disk - set(backed))
if unlisted:
    fail("backup_index.json неполон: копии без записи в backed: "
         + ", ".join(unlisted) + ".")
problems = []
for rel in backed:
    source = backup_dir / rel
    if not source.is_file() or source.is_symlink() or source.stat().st_nlink != 1:
        problems.append(rel + " (нет независимой обычной копии)")
    elif digest(source) != hashes[rel]:
        problems.append(rel + " (копия повреждена)")
if problems:
    fail("Резервная копия неполна или повреждена: " + ", ".join(problems) + ".")

# Временные файлы этой операции убираются, чтобы восстановленный состав
# совпадал с пред-образом. Принадлежность доказывается строгим шаблоном
# '.<имя>.fop-<op_id>-<8 hex>.tmp', как у engine: похожие посторонние
# файлы (без полного hex-суффикса) не трогаются.
temp_pattern = re.compile(r"\\..*\\.fop-" + re.escape(op_dir.name)
                          + r"-[0-9a-f]{8}\\.tmp\\Z")
for rel in backed + added + missing:
    parent = (root / rel).parent
    if not parent.is_dir():
        continue
    for stray in parent.iterdir():
        if not temp_pattern.match(stray.name):
            continue
        if stray.is_file() or stray.is_symlink():
            stray.unlink()

def directory_matches_preimage(directory):
    for path in [directory] + list(directory.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            return False
        if path.is_dir():
            if rel not in dir_modes or path.stat().st_mode & 0o7777 != dir_modes[rel]:
                return False
        elif rel not in hashes or state_of(path) != (hashes[rel], modes[rel]):
            return False
    return True


restored = removed = 0
conflicts = []
for rel in sorted(removed_dirs, key=lambda p: len(Path(p).parts)):
    target = root / rel
    if target.exists() and (not target.is_dir() or not directory_matches_preimage(target)):
        keep_conflict(target, rel)
        conflicts.append(rel)
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, dir_modes[rel] | 0o700)
for rel in added + missing:
    target = root / rel
    current = state_of(target)
    if current[0] == "absent":
        continue
    if rel in added and matches_planned(rel, current):
        target.unlink()
        removed += 1
    else:
        keep_conflict(target, rel)
        conflicts.append(rel)
        removed += 1
for rel in backed:
    target = root / rel
    current = state_of(target)
    if current != (hashes[rel], modes[rel]):
        if current[0] != "absent" and not matches_planned(rel, current):
            keep_conflict(target, rel)
            conflicts.append(rel)
        source = backup_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        restore_file(source, target, hashes[rel], modes[rel])
    os.chmod(target, modes[rel])
    if digest(target) != hashes[rel]:
        fail(f"Восстановленный файл не совпадает с резервной копией: {rel}.")
    restored += 1
for rel in sorted(removed_dirs, key=lambda p: len(Path(p).parts)):
    target = root / rel
    if not target.is_dir():
        if target.exists() or target.is_symlink():
            keep_conflict(target, rel)
            conflicts.append(rel)
        target.mkdir(parents=True)
    os.chmod(target, dir_modes[rel])
    if target.stat().st_mode & 0o7777 != dir_modes[rel]:
        fail(f"Режим восстановленного каталога не совпадает: {rel}.")
for rel in created_dirs:
    target = root / rel
    if not target.exists():
        continue
    expected_mode = index.get("planned_dir_modes", {}).get(rel)
    if not target.is_dir() or expected_mode is None or target.stat().st_mode & 0o7777 != expected_mode:
        keep_conflict(target, rel)
        conflicts.append(rel)
        continue
    try:
        target.rmdir()
    except OSError:
        keep_conflict(target, rel)
        conflicts.append(rel)
# Квитанция completed после отката ложно подтверждала бы применение плана:
# переводится в restored, чтобы повторный запуск плана применил его заново.
receipt = op_dir / "receipt.json"
if receipt.is_file():
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and data.get("result") == "completed":
        data["result"] = "restored"
        receipt.write_text(json.dumps(data, ensure_ascii=False, indent=2)
                           + "\\n", encoding="utf-8")
# Маркер снимается, только если он по-прежнему принадлежит этой операции.
try:
    current = json.loads(pending.read_text(encoding="utf-8"))
except (OSError, ValueError):
    current = None
removed_marker = isinstance(current, dict) \\
    and current.get("op_id") == op_dir.name
if removed_marker:
    pending.unlink()
    cleanup_staged()
note = ""
if conflicts:
    note = (" Конфликтующее содержимое сохранено в "
            + str(op_dir / "conflicts") + ": " + ", ".join(conflicts) + ".")
if removed_marker:
    print(f"Восстановлено файлов: {restored}; удалено добавленных: {removed}; "
          f"маркер снят." + note)
else:
    print(f"Восстановлено файлов: {restored}; удалено добавленных: {removed}; "
          "маркер сменил владельца и не снят." + note, file=sys.stderr)
sys.exit(0)
'''
