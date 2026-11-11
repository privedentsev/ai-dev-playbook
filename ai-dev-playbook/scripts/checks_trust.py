"""Граница доверия: дом verifier, ключи, одобрение политики, probe (PB-061).

Доверенные данные живут в ``verifier home`` — каталоге, который по
deploy-контракту принадлежит отдельной OS identity либо CI-сервису и не
доступен агенту и исполняемым тестам на запись. Локальные файлы проекта не
аутентифицируют владельца: поле ``approved`` в редактируемом JSON не
является авторизацией, а дом, указанный в ``project.json``, доверия не
даёт — путь передаётся оператором через ``--home``/``PLAYBOOK_VERIFIER_HOME``.

Состав дома::

    verifier.json     engine identity и привязка к project_id
    keys/<id>.key     секреты HMAC; keys/active — активный key_id
    approved.json     одобренная политика целиком + digest/revision/basis
    revocations.json  отозванные key_id, policy_digest и receipt_id
    journal.jsonl     доверенный append-only журнал событий
    results/<run>/    хранилище результатов, вычисленных verifier'ом
    receipts/         выданные квитанции
    arena/            материализованные изолированные снимки кандидата

Подпись — ``hmac-sha256`` (контракт допускает ed25519 внешними
реализациями). Криптография проверяется только внутри дома: клиентский
итог никогда не подписывается как собственный запуск сервиса (R-17).

Изоляция недоверенного подпроцесса проверки — очищенное окружение
``scrub_env`` и, при наличии, ``sandbox-exec`` (macOS): профиль запрещает
чтение и запись дома и любую запись вне arena/системного temp. Probe
честно различает фактические возможности: без отдельной OS identity
граница от владельца проекта не доказана, capability остаётся ``local``
и защищённая эксплуатация не заявляется (R-20, AC-03).
"""
import datetime as dt
import errno
import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import uuid
from pathlib import Path

import checks_contract
import checks_results
import verification_policy


ENGINE = {"id": "playbook-verifier", "version": "1"}
HOME_SCHEMA_VERSION = 1
SIGNATURE_ALGORITHM = "hmac-sha256"
HOME_ENV = "PLAYBOOK_VERIFIER_HOME"

VERIFIER_DOC = "verifier.json"
KEYS_DIR = "keys"
ACTIVE_KEY_FILE = "active"
APPROVED_DOC = "approved.json"
REVOCATIONS_DOC = "revocations.json"
JOURNAL_FILE = "journal.jsonl"
RESULTS_DIR = "results"
RECEIPTS_DIR = "receipts"
ARENA_DIR = "arena"

# Публикованная в проекте копия квитанций — читаемая, не доверенная.
PROJECT_RECEIPTS_DIR = "work/checks/receipts"

SANDBOX_EXEC = "sandbox-exec"


def _raise(condition, message):
    if not condition:
        raise ValueError(message)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode()


def _now(now):
    if now is not None:
        checks_contract._iso(now, "at", _raise)
        return now
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _basis(who, at, source, detail):
    return {"who": who, "at": at, "source": source, "detail": detail}


def _write_json(path, doc):
    checks_results._atomic_write(path, doc)


def _read_json(path, label):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"),
                          object_pairs_hook=checks_results._unique_pairs)
    except (OSError, ValueError) as error:
        raise ValueError(f"{label}: нечитаемый JSON {path}: {error}")


def _journal(store, event, **fields):
    entry = {"at": _now(None), "event": event}
    entry.update(fields)
    path = store["home"] / JOURNAL_FILE
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(entry, ensure_ascii=False,
                             sort_keys=True) + "\n")


def journal_entries(store):
    """Прочитать события доверенного журнала; повреждённая строка — отказ."""
    path = store["home"] / JOURNAL_FILE
    entries = []
    if not path.is_file():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entries.append(json.loads(line))
    return entries


def init_home(home, *, project_id, actor, basis_detail, now=None):
    """Создать дом verifier; существующее содержимое не затирается."""
    home = Path(home)
    _raise(not (home / VERIFIER_DOC).exists(),
           f"verifier home уже инициализирован: {home}")
    _raise(not home.exists() or not any(home.iterdir())
           or all(p.name in {ARENA_DIR, RESULTS_DIR, RECEIPTS_DIR}
                  for p in home.iterdir()),
           f"verifier home не пуст: {home}")
    home.mkdir(parents=True, exist_ok=True)
    key_id = "k-" + uuid.uuid4().hex[:16]
    keys = home / KEYS_DIR
    keys.mkdir(exist_ok=True)
    (keys / f"{key_id}.key").write_text(secrets.token_hex(32) + "\n",
                                       encoding="utf-8")
    (keys / ACTIVE_KEY_FILE).write_text(key_id + "\n", encoding="utf-8")
    at = _now(now)
    doc = {"schema_version": HOME_SCHEMA_VERSION,
           "kind": "checks-verifier-home",
           "engine": dict(ENGINE),
           "project_id": project_id,
           "created_at": at,
           "basis": _basis(actor, at, "init", basis_detail)}
    _write_json(home / VERIFIER_DOC, doc)
    _write_json(home / REVOCATIONS_DOC,
                {"schema_version": 1, "keys": [], "policy_digests": [],
                 "receipts": []})
    for name in (RESULTS_DIR, RECEIPTS_DIR, ARENA_DIR):
        (home / name).mkdir(exist_ok=True)
    store = {"home": home, "doc": doc}
    _journal(store, "init", project_id=project_id, actor=actor)
    return doc


def _load_revocations(home):
    doc = _read_json(home / REVOCATIONS_DOC, "revocations")
    _raise(type(doc.get("schema_version")) is int
           and doc["schema_version"] == 1, "revocations: schema_version != 1")
    for field in ("keys", "policy_digests", "receipts"):
        _raise(isinstance(doc.get(field), list),
               f"revocations.{field} должен быть списком")
    return doc


def load_home(home, *, project_id=None):
    """Загрузить дом и проверить привязку; неполный дом — ошибка."""
    home = Path(home)
    doc = _read_json(home / VERIFIER_DOC, "verifier")
    _raise(type(doc.get("schema_version")) is int
           and doc["schema_version"] == HOME_SCHEMA_VERSION,
           "verifier.json: неподдерживаемая schema_version")
    _raise(doc.get("kind") == "checks-verifier-home",
           f"{home}: не является checks-verifier-home")
    for field in ("engine", "project_id", "created_at", "basis"):
        _raise(field in doc, f"verifier.json: отсутствует '{field}'")
    checks_contract._unknown(doc["engine"], {"id", "version"},
                             "verifier.engine", _raise)
    _raise(doc["engine"].get("id") == ENGINE["id"]
           and doc["engine"].get("version") == ENGINE["version"],
           f"verifier: неподдерживаемый engine "
           f"{doc['engine'].get('id')!r}/{doc['engine'].get('version')!r}")
    if project_id is not None:
        _raise(doc["project_id"] == project_id,
               f"verifier home привязан к {doc['project_id']!r}, "
               f"ожидался {project_id!r}")
    active_path = home / KEYS_DIR / ACTIVE_KEY_FILE
    _raise(active_path.is_file(), "verifier: нет активного ключа")
    active = active_path.read_text(encoding="utf-8").strip()
    key_path = home / KEYS_DIR / f"{active}.key"
    _raise(active and key_path.is_file(),
           f"verifier: ключ {active!r} отсутствует")
    approved = None
    approved_path = home / APPROVED_DOC
    if approved_path.is_file():
        approved = _read_json(approved_path, "approved")
        for field in ("policy", "policy_digest", "approved_at", "basis"):
            _raise(field in approved, f"approved: отсутствует '{field}'")
    return {"home": home, "doc": doc, "active_key": active,
            "approved": approved,
            "revocations": _load_revocations(home)}


def _key_secret(store, key_id):
    path = store["home"] / KEYS_DIR / f"{key_id}.key"
    _raise(key_id not in store["revocations"]["keys"],
           f"verifier: ключ {key_id!r} отозван")
    _raise(path.is_file() and not path.is_symlink(),
           f"verifier: ключ {key_id!r} неизвестен")
    return bytes.fromhex(path.read_text(encoding="utf-8").strip())


def sign(store, payload):
    """Подписать канонические байты payload активным ключом дома."""
    secret = _key_secret(store, store["active_key"])
    value = hmac.new(secret, _canonical(payload),
                     hashlib.sha256).hexdigest()
    return {"algorithm": SIGNATURE_ALGORITHM,
            "key_id": store["active_key"], "value": value}


def check_signature(store, signature, payload):
    """Сверить подпись; отозванный/неизвестный ключ и подмена — отказ."""
    checks_contract._unknown(signature, {"algorithm", "key_id", "value"},
                             "signature", _raise)
    _raise(signature.get("algorithm") == SIGNATURE_ALGORITHM,
           f"signature: алгоритм {signature.get('algorithm')!r} "
           f"не обслуживается эталонным контуром")
    secret = _key_secret(store, signature.get("key_id"))
    expected = hmac.new(secret, _canonical(payload),
                        hashlib.sha256).hexdigest()
    _raise(hmac.compare_digest(expected, signature.get("value") or ""),
           "signature: подпись не совпадает (forgery)")
    return signature["key_id"]


def approve_policy(store, policy, *, actor, reapprove=False, now=None):
    """Одобрить политику через независимый канал (действие оператора).

    Политика сохраняется целиком: verifier работает с доверенной копией,
    а не с редактируемым файлом проекта. Повторное одобрение сравнивает с
    прежней: ``extension``/``unchanged`` проходит штатно, ``weakening``
    (включая новую исполняемую команду) требует явного ``reapprove`` —
    обычное расширение не разрешает новые недоверенные команды (R-18).
    """
    checks_contract.validate_policy(policy, _raise)
    policy_digest = verification_policy.fingerprint(policy)
    _raise(policy_digest not in store["revocations"]["policy_digests"],
           f"политика {policy_digest} отозвана — одобрение невозможно")
    previous = store.get("approved")
    classification = {"verdict": "initial", "changes": []}
    if previous is not None:
        classification = checks_contract.classify_policy_change(
            previous["policy"], policy)
        if classification["verdict"] == "weakening" and not reapprove:
            detail = "; ".join(f"{c['kind']}:{c['ref']} {c['detail']}"
                               for c in classification["changes"]
                               if c["classification"] == "weakening")
            raise ValueError(
                "одобрение ослабляет политику — требуется отдельное "
                f"решение инженера (--reapprove): {detail}")
    at = _now(now)
    doc = {"policy": policy, "policy_digest": policy_digest,
           "revision": policy["revision"], "approved_at": at,
           "basis": _basis(actor, at, "verifier approve",
                           f"verdict={classification['verdict']}"),
           "classification": {"verdict": classification["verdict"],
                              "changes": classification["changes"]}}
    _write_json(store["home"] / APPROVED_DOC, doc)
    store["approved"] = doc
    _journal(store, "approve", actor=actor,
             policy_digest=policy_digest,
             revision=policy["revision"],
             verdict=classification["verdict"], reapprove=bool(reapprove))
    return classification


def revoke(store, *, key_id=None, policy_digest=None, receipt_id=None,
           actor, now=None):
    """Отозвать ключ, дайджест политики или квитанцию (действие оператора)."""
    targets = {"keys": key_id, "policy_digests": policy_digest,
               "receipts": receipt_id}
    _raise(any(v for v in targets.values()),
           "revoke: укажите --key, --policy-digest или --receipt")
    revocations = store["revocations"]
    for field, value in targets.items():
        if value and value not in revocations[field]:
            revocations[field].append(value)
    _write_json(store["home"] / REVOCATIONS_DOC, revocations)
    _journal(store, "revoke", actor=actor,
             **{k: v for k, v in targets.items() if v})
    return revocations


def require_approved(store):
    """Одобренная политика дома; отсутствие — контролируемый отказ."""
    approved = store.get("approved")
    _raise(approved is not None,
           "verifier: политика не одобрена — выполните 'verifier approve' "
           "через независимый канал")
    _raise(approved["policy_digest"] not in
           store["revocations"]["policy_digests"],
           "verifier: одобренная политика отозвана")
    return approved


def scrub_env(arena):
    """Минимальное окружение недоверенного подпроцесса проверки.

    Пути дома и переменные доверенной стороны не передаются; HOME/TMPDIR
    указывают внутрь arena. Наследуются только нейтральные переменные.
    """
    arena = Path(arena)
    keep = {}
    for name in ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name)
        if value:
            keep[name] = value
    (arena / "tmp").mkdir(parents=True, exist_ok=True)
    (arena / "home").mkdir(parents=True, exist_ok=True)
    keep["HOME"] = str(arena / "home")
    keep["TMPDIR"] = str(arena / "tmp")
    keep["PYTHONPATH"] = str(arena.resolve())
    return keep


def sandbox_prefix(root, home, arena):
    """Префикс argv для sandbox-exec либо None при отсутствии механизма.

    Профиль запрещает подпроцессу чтение и запись дома verifier и любую
    запись вне arena/системного temp. Пути канонизируются: Seatbelt
    сравнивает resolved-пути (``/tmp`` — ссылка на ``/private/tmp``).
    """
    if shutil.which(SANDBOX_EXEC) is None:
        return None
    home_r = Path(home).resolve()
    arena_r = Path(arena).resolve()
    writable = [arena_r]
    for extra in (Path("/dev"), Path("/private/var/folders"),
                  Path("/tmp").resolve()):
        writable.append(extra)
    allow = " ".join(f'(subpath "{path}")' for path in writable)
    profile = ("(version 1)(allow default)"
               f'(deny file-read* file-write* (subpath "{home_r}"))'
               f'(allow file-read* (subpath "{arena_r}"))'
               "(deny file-write*)"
               f"(allow file-write* {allow})")
    return [SANDBOX_EXEC, "-p", profile]


def _writable(path, *, append=False, directory=False):
    """Доступна ли запись в ``path`` текущему процессу.

    ``append`` — открытие существующего файла без изменения
    содержимого; ``directory`` — создание уникального marker внутри
    каталога (A-05: ``O_EXCL`` на самом каталоге даёт EEXIST и ничего
    не проверяет). ``True`` — запись подтверждена, ``False`` — отказ
    по правам (EACCES/EPERM), ``None`` — ошибка не связана с правами:
    граница не доказана и не опровергнута.
    """
    try:
        if append:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND)
            os.close(fd)
            return True
        if directory:
            marker = Path(path) / f".probe-{uuid.uuid4().hex}"
            fd = os.open(marker,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            marker.unlink(missing_ok=True)
            return True
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        Path(path).unlink(missing_ok=True)
        return True
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EPERM):
            return False
        return None


def _home_writability(home_r, store):
    """Проверить запись по чувствительным точкам дома, не только корню.

    ``chmod`` корня не защищает ключи и историю; подмена возможна и
    через запись в родительский каталог (переименование/замена).
    """
    targets = [
        ("дом", home_r, {"directory": True}),
        ("родительский каталог", home_r.parent, {"directory": True}),
        ("ключи", home_r / KEYS_DIR, {"directory": True}),
    ]
    # Существующие доверенные файлы проверяются на запись открытием
    # без изменения содержимого; отсутствие файла покрывается записью
    # в его каталог.
    for name in (APPROVED_DOC, REVOCATIONS_DOC, JOURNAL_FILE):
        path = home_r / name
        if path.exists() or path.is_symlink():
            targets.append((name, path, {"append": True}))
    if store["active_key"]:
        targets.append((f"ключ {store['active_key']}",
                        home_r / KEYS_DIR /
                        f"{store['active_key']}.key", {"append": True}))
    results = {}
    for label, path, kwargs in targets:
        results[label] = _writable(path, **kwargs)
    return results


def _sandbox_denies_write(home_r):
    """Реально ли sandbox-профиль запрещает запись в дом.

    ``True`` — попытка записи отклонена, ``False`` — запись прошла,
    ``None`` — механизм недоступен или результат не доказан.
    """
    if shutil.which(SANDBOX_EXEC) is None:
        return None
    marker = home_r / f".probe-{uuid.uuid4().hex}"
    arena = home_r / ARENA_DIR
    arena.mkdir(exist_ok=True)
    prefix = sandbox_prefix(Path("."), home_r, arena)
    try:
        result = subprocess.run(
            prefix + ["/usr/bin/touch", str(marker)],
            capture_output=True, timeout=30)
        if marker.exists():
            marker.unlink(missing_ok=True)
            return False
        return result.returncode != 0
    except (OSError, subprocess.SubprocessError):
        return None


def probe(root, home=None):
    """Оценить фактическую границу прав; возвратить отчёт capability.

    ``boundary=separate`` и ``capability=protected`` требуют всех
    условий (A-02): дом валиден и вне проекта; владелец дома — иная
    OS identity (``st_uid != euid`` — владелец может вернуть права,
    поэтому совпадение uid границей не является); запись отклонена
    во всех чувствительных точках (ключи, approved, журнал, дом и
    родительский каталог); реальный прогон sandbox-профиля запрещает
    подпроцессу запись в дом. Любая ошибка, не связанная с правами,
    оставляет capability ``local`` — ограничение заявляется честно.
    """
    root_r = Path(root).resolve()
    report = {"home": None, "configured": False, "inside_project": None,
              "agent_can_write": None, "separate_identity": None,
              "isolation": "none", "isolation_probe": None,
              "boundary": "none", "capability": "local", "notes": []}
    source = home or os.environ.get(HOME_ENV)
    if not source:
        report["notes"].append(
            f"verifier home не задан (--home или {HOME_ENV}) — "
            "режим local")
        return report
    home_p = Path(source)
    report["home"] = str(home_p)
    try:
        home_r = home_p.resolve()
        store = load_home(home_p)
    except ValueError as error:
        report["notes"].append(f"verifier home непригоден: {error}")
        return report
    report["configured"] = True
    report["project_id"] = store["doc"]["project_id"]
    inside = home_r == root_r or root_r in home_r.parents
    report["inside_project"] = inside
    if inside:
        report["notes"].append(
            "verifier home внутри проекта — владелец проекта контролирует "
            "ключи; границы нет")
    try:
        report["separate_identity"] = \
            home_r.stat().st_uid != os.geteuid()
    except OSError:
        report["separate_identity"] = None
    if report["separate_identity"] is False:
        report["notes"].append(
            "дом принадлежит текущей OS identity — владелец может "
            "вернуть права записи; отдельной границы нет")
    writability = _home_writability(home_r, store)
    writable_points = [name for name, ok in writability.items() if ok]
    unknown_points = [name for name, ok in writability.items()
                      if ok is None]
    report["writable_points"] = sorted(writable_points)
    if writable_points:
        report["agent_can_write"] = True
        report["notes"].append(
            "запись доступна: " + ", ".join(sorted(writable_points)))
    elif unknown_points:
        report["agent_can_write"] = None
        report["notes"].append(
            "проверка записи не дала результата по правам: "
            + ", ".join(sorted(unknown_points)))
    else:
        report["agent_can_write"] = False
    if shutil.which(SANDBOX_EXEC) is not None:
        report["isolation"] = SANDBOX_EXEC
        denied = _sandbox_denies_write(home_r)
        report["isolation_probe"] = denied
        if denied is False:
            report["notes"].append(
                "sandbox-профиль не запретил запись в дом — "
                "изоляция подпроцесса не доказана")
        elif denied is None:
            report["notes"].append(
                "проверка sandbox-изоляции не дала результата")
    else:
        report["notes"].append(
            "sandbox-exec недоступен — изоляция подпроцесса "
            "ограничена очищенным окружением")
    if report["configured"] and not inside \
            and report["separate_identity"] is True \
            and report["agent_can_write"] is False \
            and report["isolation_probe"] is True:
        report["boundary"] = "separate"
        report["capability"] = "protected"
    elif report["configured"] and not inside \
            and report["separate_identity"] is not True:
        report["notes"].append(
            "отдельная OS/CI identity не доказана — защищённая "
            "эксплуатация не заявляется")
    return report


def require(condition, message):
    _raise(condition, message)
