"""Append-only история результатов проверок и производный индекс (PB-060).

Хранилище контура: ``work/checks/results/<run_id>/<check_id>.json`` —
по одной записи результата схемы 1 на проверку в прогоне; файлы не
перезаписываются, новая запись получает новый run_id. Производный
``work/checks/results-index.json`` ускоряет выбор актуального
результата, но не является источником истины: расхождение, утрата или
повреждение индекса восстанавливается пересбором из проверенных записей
(design §6). Повреждённая запись отклоняется валидатором, а не
угадывается — ложного reuse нет.

Последний результат на check_id выбирается состоянием порядка
``checks_select``: seq-запись ``passed`` — покрытие только когда
её ``run_seq`` строго позже ``run_seq`` любой непокрывающей
seq-записи **и** её ``finished_at`` не противоречит (не раньше)
меткам всех непокрывающих исходов; ``run_seq`` — порядок
резервации исполнений и сам по себе не доказывает порядок
завершения перекрывающихся прогонов (A-01 аудита PB-078), а
``finished_at`` не доказывает его при откате wall clock
(A-06 PB-078) — метка непокрывающего исхода позже метки passed
есть доказанное противоречие, консервативно важнее
непокрывающий исход; равные метки не противоречат, решает
``run_seq``. При равном ``run_seq`` порядок недоказан —
непокрывающий исход важнее метки; legacy-записи без ``run_seq``
упорядочены ``finished_at`` как единственным свидетельством
(A-02 PB-073, A-05 PB-074).
Записи разных проектов не смешиваются: результат хранит
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


ORDER_STATE_VERSION = 1


def _record_mini(record, check_id):
    """Минимальная запись порядка для производного индекса.

    Содержит поля, достаточные для состояния порядка и фильтра
    проекта; полной записи результата в индексе нет — источник
    истины остаётся append-only файлами.
    """
    evidence = record.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    run_id = record.get("run_id") or evidence.get("run_id") or ""
    seq = evidence.get("run_seq") if "run_seq" in evidence \
        else record.get("run_seq")
    rel = record.get("file") or (
        f"{checks_select.STATE_DIR}/{checks_select.RESULTS_DIR}/"
        f"{run_id}/{check_id}.json" if run_id else "")
    return {"result_id": record.get("result_id") or "",
            "run_id": run_id,
            "project_id": record.get("project_id"),
            "finished_at": record.get("finished_at") or "",
            "run_seq": seq if type(seq) is int else None,
            "file": rel, "status": record.get("status")}


def _state_from_entry(entry, check_id):
    """Состояние порядка из записи индекса.

    Записи с ``order.v == 1`` несут компоненты состояния; старые
    записи (только победитель) дают состояние из одной записи —
    консервативно достаточное до пересборки.
    """
    state = checks_select._new_order_state()
    order = entry.get("order")
    if isinstance(order, dict) and \
            order.get("v") == ORDER_STATE_VERSION:
        state["has_seq"] = bool(order.get("has_seq"))
        for key in ("pw", "nw", "lw", "lnw"):
            mini = order.get(key)
            state[key] = dict(mini) if isinstance(mini, dict) \
                else None
        ns = order.get("ns")
        state["ns"] = ns if type(ns) is int else -1
        nf = order.get("nf")
        state["nf"] = nf if isinstance(nf, str) else ""
        return state
    pseudo = {key: entry.get(key) for key in
              ("result_id", "run_id", "project_id", "finished_at",
               "run_seq", "file", "status")}
    pseudo["check_id"] = check_id
    checks_select._merge_order_state(state, pseudo)
    return state


def _entry_from_state(state, check_id):
    """Запись индекса: победитель + состояние порядка для merge."""
    winner = checks_select._choose_order_state(state)
    if winner is None:
        return None

    def mini(record):
        return _record_mini(record, check_id) \
            if record is not None else None

    top = mini(winner)
    top["order"] = {"v": ORDER_STATE_VERSION,
                    "has_seq": state["has_seq"],
                    "pw": mini(state["pw"]),
                    "nw": mini(state["nw"]),
                    "ns": state["ns"], "nf": state["nf"],
                    "lw": mini(state["lw"]),
                    "lnw": mini(state["lnw"])}
    return top


def _accounted_evidence(results):
    """Свидетельства порядка, воспроизводимые файлами check_id.

    ``{check_id: {"ids", "has_seq", "ns", "nf"}}``: result_id всех
    записей и максимумы ``run_seq``/``finished_at`` среди seq-записей
    с непокрывающим исходом — те же величины, что состояние порядка
    хранит скалярами (A-02 аудита PB-078).
    """
    info = {}
    for record in results:
        slot = info.setdefault(record["check_id"],
                               {"ids": set(), "has_seq": False,
                                "ns": -1, "nf": ""})
        slot["ids"].add(record.get("result_id") or "")
        seq = checks_select._run_seq_of(record)
        if seq >= 0:
            slot["has_seq"] = True
            if record.get("status") != "passed":
                slot["ns"] = max(slot["ns"], seq)
                slot["nf"] = max(slot["nf"],
                                 record.get("finished_at") or "")
    return info


def _claim_unaccounted(check_id, entry, accounted):
    """Индекс заявляет свидетельство, утраченное на диске.

    ``True``, если состояние порядка, восстановленное из записи
    индекса (компоненты ``order.v=1`` и скалярные максимумы
    ``ns``/``nf`` непобедивших записей), не воспроизводимо
    сохранившимися файлами check_id: компонентной записи нет на
    диске либо скалярный максимум превышает файловый — источник
    ``nf`` мог не быть компонентом состояния (A-02 аудита
    PB-078). Индекс — производный и не бывает «новее» файлов,
    поэтому любое невоспроизводимое заявление есть утрата или
    подмена записи; проверка не зависит от совпадения
    победителей: иначе rebuild молча снимал бы блокирующий
    максимум ``nf``, не менявший исход, и поздний passed с
    противоречащей меткой получил бы покрытие. Устаревший, но не
    ложный индекс (заявленное воспроизводимо файлами) corrupt
    не создаёт — пересборка его исправляет.

    Заявленный победитель — покрывающий ``passed``, чья запись
    есть на диске, доказывает порядок над всем заявленным
    непокрывающим свидетельством (``_choose_order_state``
    возвращает его только при доминации по ``run_seq`` и
    ``finished_at``): утраченные максимумы им уже перекрыты и не
    могут изменить исход — флаг не нужен, иначе сохранённое
    свидетельство утраты было бы неисцелимым. При этом само
    состояние индекса максимум сохраняет: поздний passed с
    меткой раньше утраченного ``nf`` по-прежнему не получает
    покрытия, а следующий passed с меткой не раньше ``nf``
    восстанавливает его.
    """
    claimed_state = _state_from_entry(entry, check_id)
    acc = accounted.get(check_id) or {"ids": set(), "has_seq": False,
                                      "ns": -1, "nf": ""}
    unaccounted = claimed_state["has_seq"] and not acc["has_seq"]
    for key in ("pw", "nw", "lw", "lnw"):
        mini = claimed_state[key]
        if mini is not None and \
                (mini.get("result_id") or "") not in acc["ids"]:
            unaccounted = True
    unaccounted = unaccounted or claimed_state["ns"] > acc["ns"] or \
        claimed_state["nf"] > acc["nf"]
    if not unaccounted:
        return False
    winner = checks_select._choose_order_state(claimed_state)
    return not (winner is not None
                and winner.get("status") == "passed"
                and (winner.get("result_id") or "") in acc["ids"])


def _merge_claimed_blocking(state, claimed):
    """Поднять блокирующие свидетельства файла заявленными.

    Состояние прежней записи индекса может нести доказательства
    утраченных записей — скалярные максимумы ``ns``/``nf``,
    компоненты ``nw``/``lnw`` и ``has_seq``, — которые файловое
    состояние не воспроизводит. При пересборке они поднимают
    файловое состояние: для честного (устаревшего) индекса
    заявленное ⊆ файлового и merge — no-op; для перекрытых
    покрывающим passed утраченных максимумов — сохранение
    блокирующего свидетельства (A-03 аудита PB-078). Покрывающие
    компоненты ``pw``/``lw`` не переносятся: лучший passed на
    диске уже известен файлам, а утрата passed ложной готовности
    не создаёт.
    """
    state["has_seq"] = state["has_seq"] or claimed["has_seq"]
    state["ns"] = max(state["ns"], claimed["ns"])
    state["nf"] = max(state["nf"], claimed["nf"])
    for key in ("nw", "lnw"):
        current, old = state[key], claimed[key]
        if old is not None and (current is None or
                checks_select.result_order_key(old) >
                checks_select.result_order_key(current)):
            state[key] = old


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
    Запись check_id — состояние порядка ``checks_select``: новая
    запись сливается с сохранёнными компонентами, победитель
    вычисляется тем же правилом, что у selector (A-06/A-01 аудита
    PB-078).
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
    check_id = result["check_id"]
    current = index["entries"].get(check_id)
    state = _state_from_entry(current, check_id) \
        if isinstance(current, dict) else checks_select._new_order_state()
    checks_select._merge_order_state(state, result)
    entry = _entry_from_state(state, check_id)
    if entry is not None:
        index["entries"][check_id] = entry
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
            accounted = _accounted_evidence(found)
            for check_id, entry in entries.items():
                if not isinstance(entry, dict):
                    continue
                # Индекс заявляет свидетельство, которого нет на
                # диске — запись утрачена/подменена, старый passed
                # не разрешает reuse. Заявленное состояние порядка
                # сверяется с файловым по всем компонентам и
                # скалярным максимумам: невоспроизводимое
                # заявление — утрата (A-02, A-06 PB-078, A-01/A-02
                # аудита PB-078).
                if _claim_unaccounted(check_id, entry, accounted):
                    corrupt.add(check_id)
                    notes.append(
                        f"запись результата {check_id} по индексу "
                        f"утрачена или подменена — история "
                        f"недоказуема")
    return found, notes, corrupt


def latest(results):
    """Последний результат на check_id по выбору ``checks_select``."""
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
    states = {}
    for result in results:
        state = states.setdefault(result["check_id"],
                                  checks_select._new_order_state())
        checks_select._merge_order_state(state, result)
    accounted = _accounted_evidence(results)
    entries = {}
    for check_id, state in states.items():
        previous_entry = previous.get(check_id)
        if isinstance(previous_entry, dict):
            _merge_claimed_blocking(
                state, _state_from_entry(previous_entry, check_id))
        winner = checks_select._choose_order_state(state)
        if winner is None:
            continue
        if policy is not None and \
                winner.get("project_id") != policy["project_id"]:
            continue
        entry = _entry_from_state(state, check_id)
        if entry is not None:
            entries[check_id] = entry
    # A-07: свидетельство утраты переживает пересборку — если прежний
    # индекс заявляет состояние порядка check_id, которое не
    # воспроизводимо записями (компонент или скалярный максимум
    # ``ns``/``nf`` без источника на диске), его запись сохраняется:
    # история остаётся недоказанной до нового фактического результата,
    # старый passed не разрешает reuse (A-02/A-03 PB-074, A-06 PB-078,
    # A-01/A-02 аудита PB-078). Перекрытые покрывающим passed
    # максимумы corrupt не создают, но переносятся в состояние
    # выше — rebuild их не теряет (A-03 аудита PB-078).
    for check_id, entry in previous.items():
        if not isinstance(entry, dict):
            continue
        if _claim_unaccounted(check_id, entry, accounted):
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
