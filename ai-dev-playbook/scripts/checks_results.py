"""Append-only история результатов проверок и производный индекс (PB-060).

Хранилище контура: ``work/checks/results/<run_id>/<check_id>.json`` —
по одной записи результата схемы 1 на проверку в прогоне; файлы не
перезаписываются, новая запись получает новый run_id. Производный
``work/checks/results-index.json`` ускоряет выбор актуального
результата, но не является источником истины: расхождение, утрата или
повреждение индекса восстанавливается пересбором из проверенных записей
(design §6). Повреждённая запись отклоняется валидатором, а не
угадывается — ложного reuse нет.

Последний результат на check_id по устойчивому порядку
``(finished_at, run_seq, result_id)`` решает: более поздний
failed/timeout/blocked/not_run не скрывается старым passed, а при
равном ``finished_at`` — более позднее исходное исполнение (R-11,
A-02 PB-073). Записи разных проектов не смешиваются: результат хранит
``project_id`` и потребитель сверяет его с политикой.
"""
import json
from pathlib import Path

import checks_contract
import checks_select
import verification_policy


INDEX_FILENAME = "results-index.json"
INDEX_KIND = "checks-results-index"


def _raise(condition, message):
    if not condition:
        raise ValueError(message)


def _unique_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"повторное поле {key!r}")
        obj[key] = value
    return obj


def _atomic_write(path, doc):
    temporary = Path(str(path) + ".tmp")
    temporary.write_text(json.dumps(doc, ensure_ascii=False,
                                    indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def results_dir(root):
    return Path(root) / checks_select.STATE_DIR / \
        checks_select.RESULTS_DIR


def index_entries(root):
    """Записи производного индекса результатов.

    ``{}`` при отсутствии или порче индекса — он не источник истины;
    используется для резерва ``run_seq`` и диагностики (PB-074).
    """
    path = Path(root) / checks_select.STATE_DIR / INDEX_FILENAME
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        entries = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_pairs).get("entries")
    except (ValueError, OSError):
        return {}
    return entries if isinstance(entries, dict) else {}


def store_result(root, run_id, result):
    """Записать проверенный результат append-only; вернуть путь файла.

    Имя файла — ``<run_id>/<check_id>.json``: повторная запись того же
    результата невозможна (exist_ok=False), история не переписывается.
    """
    checks_contract.validate_result(result, _raise)
    directory = results_dir(root) / run_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (result["check_id"] + ".json")
    _raise(not path.exists(), f"результат {path} уже существует — "
                              f"история append-only")
    _atomic_write(path, result)
    return path


def update_index(root, run_id, result):
    """Обновить производный индекс последними записями прогона.

    Индекс читается целиком; повреждённый игнорируется (пересборка).
    Победитель — запись с максимальным ключом порядка
    ``(finished_at, run_seq, result_id)`` среди индекса и новой
    записи (A-02 PB-073).
    """
    index_path = Path(root) / checks_select.STATE_DIR / INDEX_FILENAME
    index = {"schema_version": 1, "kind": INDEX_KIND,
             "project_id": result["project_id"], "entries": {}}
    if index_path.is_file():
        try:
            previous = json.loads(index_path.read_text(encoding="utf-8"),
                                  object_pairs_hook=_unique_pairs)
            if previous.get("entries") and \
                    previous.get("project_id") == result["project_id"]:
                index["entries"] = previous["entries"]
        except (ValueError, OSError):
            pass
    rel = str((results_dir(root) / run_id /
               (result["check_id"] + ".json")).relative_to(root))
    current = index["entries"].get(result["check_id"])
    # A-03 PB-074 (аудит-02): то же консервативное сравнение, что у
    # selector — при равном (finished_at, run_seq) непокрывающий исход
    # важнее result_id, иначе индекс хранил бы passed поверх failed.
    if current is None or checks_select._latest_better(result, current):
        seq = (result.get("evidence") or {}).get("run_seq") \
            if isinstance(result.get("evidence"), dict) else None
        index["entries"][result["check_id"]] = {
            "result_id": result["result_id"], "run_id": run_id,
            "finished_at": result["finished_at"],
            "run_seq": seq if type(seq) is int else None,
            "file": rel, "status": result["status"]}
    _atomic_write(index_path, index)


def load_results(root, *, validate=None):
    """Прочитать валидные результаты истории.

    Возвращает ``(results, notes, corrupt_ids)``: каждый ``*.json``
    внутри ``work/checks/results`` проверяется схемой результата;
    символическая ссылка, нечитаемый и повреждённый файл пропускаются с
    отметкой в notes — утраченное доказательство не даёт ложного reuse.
    ``corrupt_ids`` — check_id повреждённых записей: их история
    недоказуема, потребитель обязан перезапустить обязательство, а не
    переиспользовать оставшийся старый успех (A-03).
    """
    validate = validate or _raise
    found, notes, corrupt = [], [], set()
    directory = results_dir(root)
    # A-01 PB-074 (аудит-01): отсутствие каталога results не отменяет
    # свидетельства индекса — проверка ниже выполняется всегда, иначе
    # утрата всей истории снимала бы известные обязательства.
    if directory.is_dir():
        for path in sorted(directory.rglob("*.json")):
            try:
                validate(not path.is_symlink(),
                         f"{path.name}: символическая ссылка")
                doc = json.loads(path.read_text(encoding="utf-8"),
                                 object_pairs_hook=_unique_pairs)
                checks_contract.validate_result(doc, validate)
                found.append(doc)
            except (ValueError, OSError) as error:
                corrupt.add(path.stem)
                notes.append(f"пропущена повреждённая запись "
                             f"{path.relative_to(root)}: {error}")
    # A-06: свидетельства индекса о последних результатах сверяются с
    # файлами — потерянная или подменённая запись делает историю
    # check_id недоказанной, старый passed не разрешает reuse.
    index_path = Path(root) / checks_select.STATE_DIR / INDEX_FILENAME
    if index_path.is_file() and not index_path.is_symlink():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"),
                               object_pairs_hook=_unique_pairs)
            entries = index.get("entries")
            validate(isinstance(entries, dict),
                     "index.entries должен быть объектом")
        except (ValueError, OSError) as error:
            entries = None
            notes.append(f"индекс результатов нечитаем: {error}")
        if isinstance(entries, dict):
            actual_latest = latest(found)
            for check_id, entry in entries.items():
                if not isinstance(entry, dict):
                    continue
                actual = actual_latest.get(check_id)
                # Индекс знает о более позднем результате, которого нет
                # на диске — запись утрачена/подменена, старый passed
                # не разрешает reuse. «Более поздний» — по тому же
                # устойчивому ключу порядка, что у selector (A-02).
                claim = {"finished_at": entry.get("finished_at") or "",
                         "result_id": entry.get("result_id") or "",
                         "run_seq": entry.get("run_seq"),
                         "status": entry.get("status")}
                # A-03 PB-074 (аудит-02): «позднее» — по тому же
                # консервативному ключу, что у selector: при равном
                # (finished_at, run_seq) непокрывающий исход важнее
                # result_id, поэтому утраченный legacy-failed
                # обнаруживается при любом UUID-порядке.
                if actual is None or checks_select._latest_better(
                        claim, actual):
                    corrupt.add(check_id)
                    notes.append(
                        f"запись результата {check_id} по индексу "
                        f"утрачена или подменена — история "
                        f"недоказуема")
    return found, notes, corrupt


def latest(results):
    """Последний результат на check_id по времени завершения."""
    return checks_select._latest_results(results)


def rebuild_index(root, policy=None):
    """Пересобрать производный индекс из проверенных записей.

    Источник истины — append-only файлы; индекс с чужим project_id или
    ссылкой на отсутствующую/повреждённую запись пересобирается, не
    доверяется. Возвращает ``(index, notes)``.
    """
    index_path = Path(root) / checks_select.STATE_DIR / INDEX_FILENAME
    previous = {}
    if index_path.is_file() and not index_path.is_symlink():
        try:
            previous = json.loads(
                index_path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_pairs).get("entries") or {}
        except (ValueError, OSError):
            previous = {}
    results, notes, _corrupt = load_results(root)
    entries = {}
    for check_id, result in latest(results).items():
        if policy is not None and \
                result["project_id"] != policy["project_id"]:
            continue
        seq = (result.get("evidence") or {}).get("run_seq") \
            if isinstance(result.get("evidence"), dict) else None
        entries[check_id] = {
            "result_id": result["result_id"],
            "run_id": result.get("evidence", {}).get("run_id", ""),
            "finished_at": result["finished_at"],
            "run_seq": seq if type(seq) is int else None,
            "status": result["status"]}
    # A-07: свидетельство утраты переживает пересборку — если прежний
    # индекс знал о более позднем результате check_id, чем осталось на
    # диске, его запись сохраняется: история остаётся недоказанной до
    # нового фактического результата, старый passed не разрешает reuse.
    # «Более поздний» — консервативный ключ selector (finished_at,
    # run_seq, затем непокрывающий исход при неоднозначности), как у
    # A-02/A-03 PB-074: иначе старый passed-индекс воскрешал бы
    # скрытый failed при равном времени.
    for check_id, entry in previous.items():
        if not isinstance(entry, dict):
            continue
        recorded = entries.get(check_id)
        if recorded is None or checks_select._latest_better(
                entry, recorded):
            entries[check_id] = entry
            notes.append(f"индекс: запись {check_id} от "
                         f"{entry.get('finished_at')} утрачена — "
                         f"свидетельство сохранено")
    index = {"schema_version": 1, "kind": INDEX_KIND,
             "project_id": (policy or {}).get("project_id",
                                             "unknown"),
             "entries": entries}
    index_path = Path(root) / checks_select.STATE_DIR / INDEX_FILENAME
    index_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(index_path, index)
    return index, notes


def result_digest(result):
    """Отпечаток записи результата для ссылок и агрегатов."""
    return verification_policy.fingerprint(result)
