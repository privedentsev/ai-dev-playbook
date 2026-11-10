"""Чистый выбор проверок и объяснимый план ``checks plan`` (PB-059).

Селектор объединяет фактические изменения (diff снимка входов PB-058),
provenance-граф (PB-059) и обязательства задачи: объявленная
применимость и содержательные входы могут добавлять проверки к
вычисленному набору, но не сужать его — семантика
``verification_policy.required_checks`` сохраняется, registry
applicability legacy/v2 не ослабляется.

План read-only: ни discovery, ни найденный код не исполняются, запись
не производится. Оценка стоимости сравнивается с одобренным бюджетом
``fallback.limits`` до любого запуска: превышение или недоказанная
оценка оставляют обязательства ``blocked``, не ``passed`` (R-10).
Неизвестные связи расширяют набор только до доказанной границы
``unknown_scope``; за её пределами — ``blocked`` и решение инженера.

Постоянное состояние контура (пишется последующими картами, читается
здесь): ``work/checks/snapshot.json`` — последний снимок состава
входов (baseline), ``work/checks/graph.json`` — сохранённый граф,
``work/checks/results/**/*.json`` — результаты проверок. Утрата или
несовместимость baseline/графа/результатов — консервативный выбор,
никогда ложная независимость.
"""
import json
import sys
from pathlib import Path

import checks_contract
import checks_graph
import checks_inventory
import project_view
import verification_policy


STATE_DIR = "work/checks"
SNAPSHOT_FILENAME = "snapshot.json"
RESULTS_DIR = "results"
RUNNER = {"id": "playbook-runner", "version": "1"}

STATUS_ORDER = ("run", "reused", "blocked", "not_applicable")


class BudgetExceeded(Exception):
    """Оценка чтения входов превысила одобренный лимит байтов."""


def _raise(condition, message):
    if not condition:
        raise ValueError(message)


def universe_inputs(policy, inventory=None):
    """Входы снимка состава: области модулей, объявленные и неизвестные.

    Снимок universe хеширует байты значимых файлов; исключённые и
    служебные пути не читаются слоем PB-058. Неприписанные файлы из
    ``inventory.unknown_paths`` хешируются поштучно — неизвестный файл
    не теряется и отслеживается по содержимому (R-07, design §4).
    """
    inputs, seen = [], set()
    for module in policy["modules"]:
        for pattern in module["paths"]:
            head = checks_contract._literal_head(pattern)
            if head == pattern:
                key = ("path", pattern)
                if key not in seen:
                    seen.add(key)
                    inputs.append(pattern)
            else:
                key = ("dir", head or ".", pattern)
                if key not in seen:
                    seen.add(key)
                    inputs.append({"type": "directory",
                                   "path": head or ".",
                                   "patterns": [pattern]})
    for check in policy["checks"]:
        for entry in check.get("inputs") or []:
            if isinstance(entry, str):
                key = ("path", entry)
                if key not in seen:
                    seen.add(key)
                    inputs.append(entry)
            elif isinstance(entry, dict) and entry.get("type") in \
                    {"file", "directory", "path"}:
                key = ("spec", json.dumps(entry, sort_keys=True))
                if key not in seen:
                    seen.add(key)
                    inputs.append(dict(entry))
    for rel in (inventory or {}).get("unknown_paths") or []:
        key = ("file", rel)
        if key not in seen:
            seen.add(key)
            inputs.append({"type": "file", "path": rel})
    return inputs


def _counting_hasher(state, limit, hash_fn=None):
    """Обёртка хеширования: считает байты и рубит при превышении лимита."""
    real = hash_fn or project_view.digest_file

    def wrapped(path):
        size = Path(path).lstat().st_size
        if limit is not None and state["bytes"] + size > limit:
            raise BudgetExceeded(
                f"чтение входов превысило лимит max_bytes={limit}")
        state["bytes"] += size
        state["files"] += 1
        return real(path)

    return wrapped


def compute_change(root, policy, baseline, state, *, hash_fn=None,
                   now=None):
    """Фактические изменения состава входов против baseline-снимка.

    ``changed=None`` — доказательства свежести нет (baseline утрачен,
    повреждён, от чужой политики либо diff ушёл за бюджет байтов):
    потребитель обязан выбрать консервативный набор.
    """
    hasher = _counting_hasher(state, policy["fallback"]["limits"]
                            .get("max_bytes"), hash_fn)
    inventory = checks_inventory.build_inventory(root, policy)
    try:
        current = checks_inventory.snapshot(
            root, policy, universe_inputs(policy, inventory),
            hash_fn=hasher, now=now)
    except BudgetExceeded as error:
        return {"changed": None, "snapshot": None,
                "reason": str(error), "bytes_exceeded": True}
    if baseline is None:
        return {"changed": None, "snapshot": current,
                "reason": "baseline отсутствует",
                "bytes_exceeded": False}
    diff = checks_inventory.diff_snapshots(baseline, current)
    if not diff["complete"]:
        return {"changed": None, "snapshot": current,
                "reason": f"baseline непригоден: {diff['reason']}",
                "bytes_exceeded": False}
    raw = diff["added"] + diff["removed"] + diff["changed"]
    changed = sorted({p[5:] if p.startswith("path:") else p
                      for p in raw})
    return {"changed": changed, "snapshot": current,
            "reason": "", "bytes_exceeded": False}


def _check_applicability(rule, changed, checkpoint):
    """Обязательность по применимости; неизвестный diff — требуется."""
    app = {"kind": rule} if isinstance(rule, str) else rule
    kind = app.get("kind")
    if kind == "always":
        return True, "применимость always"
    if kind == "checkpoint":
        if checkpoint is not None and \
                app.get("checkpoint") in {None, checkpoint}:
            return True, f"контрольная точка {checkpoint}"
        return False, "вне запрошенной контрольной точки"
    if kind != "when_paths":
        raise ValueError(f"неизвестная применимость {kind!r}")
    if changed is None:
        return True, "изменения не доказаны — when_paths требуется"
    for path in changed:
        if any(checks_contract._match(path, pattern)
               for pattern in app["paths"]):
            return True, f"when_paths: изменён {path}"
    return False, "when_paths не пересекается с изменениями"


def _task_bound(check):
    """Содержательные входы задачи/политики — обязательство плана."""
    bound = []
    for entry in check.get("inputs") or []:
        if isinstance(entry, dict):
            if entry.get("type") == "task_projection":
                bound.append("обязательство задачи (task_projection)")
            elif entry.get("type") == "policy_projection":
                bound.append("обязательство политики (policy_projection)")
    return bound


def select_checks(policy, *, changed, edges, unknown, checkpoint=None):
    """Вычислить требуемый набор по изменениям, графу и обязательствам.

    ``changed`` — список путей либо ``None`` (baseline недоказуем →
    весь набор). Расширение идёт module → subsystem → project только
    до доказанной границы: неприписанный путь требует
    ``fallback.unknown_scope == 'project'``, иначе обязательства —
    blocked до решения инженера. Возвращает ``{checks, explain,
    changed, unattributed}``.
    """
    modules_by_id = {m["id"]: m for m in policy["modules"]}
    subsystem_modules = {}
    for module in policy["modules"]:
        subsystem_modules.setdefault(module["subsystem_id"], set()) \
            .add(module["id"])
    unknown_scope = policy["fallback"]["unknown_scope"]
    affected = set()
    reasons = {}
    unattributed = []

    def mark(module_id, reason):
        if module_id not in affected:
            affected.add(module_id)
            reasons.setdefault(module_id, []).append(reason)

    # Неизвестные области: анализ не доказал независимость — модуль,
    # подсистема или проект затрагиваются любым изменением (R-04/R-09).
    universal = set()
    for item in unknown:
        scope, reason = item["scope"], item["reason"]
        if scope["kind"] == "project":
            universal.update(modules_by_id)
        elif scope["kind"] == "subsystem":
            universal.update(subsystem_modules.get(scope["id"], set()))
        else:
            universal.add(scope["id"])
    for module_id in sorted(universal & set(modules_by_id)):
        mark(module_id, "область unknown — независимость не доказана")

    if changed is None:
        for module_id in modules_by_id:
            mark(module_id, "изменения не доказаны — консервативный "
                            "набор")
    else:
        for path in changed:
            owners = checks_graph.owners_of(path, policy)
            if not owners:
                unattributed.append(path)
            for module_id in owners:
                mark(module_id, f"изменён {path}")

    # Обратное замыкание по объединённому графу (design §5). Ребро
    # consumer→dependency: при изменении dependency требуется consumer.
    chains = {f"module:{m}": [] for m in sorted(affected)}
    queue = list(chains)
    reached = set(chains)
    for module_id in affected:
        sub = f"subsystem:{modules_by_id[module_id]['subsystem_id']}"
        if sub not in reached:
            reached.add(sub)
            chains[sub] = []
            queue.append(sub)
    reverse = {}
    for edge in edges:
        reverse.setdefault(edge["dependency"], []).append(edge)

    def push(ref):
        if ref not in reached:
            reached.add(ref)
            queue.append(ref)

    while queue:
        ref = queue.pop(0)
        for edge in reverse.get(ref, []):
            consumer = edge["consumer"]
            if consumer in reached or consumer == ref:
                continue
            chains[consumer] = chains.get(ref, []) + [edge]
            kind, _, ident = consumer.partition(":")
            if kind == "module" and ident in modules_by_id:
                mark(ident, f"зависит от {edge['dependency']} "
                            f"({edge['kind']}, {edge['source']})")
                push(f"subsystem:"
                     f"{modules_by_id[ident]['subsystem_id']}")
            elif kind == "subsystem":
                for member in sorted(
                        subsystem_modules.get(ident, set())):
                    mark(member, f"подсистема {ident} затронута "
                                 f"через {edge['dependency']}")
                    chains.setdefault(f"module:{member}",
                                      chains[consumer])
                    push(f"module:{member}")
            push(consumer)

    required = {}
    for check in policy["checks"]:
        explain = []
        bound = _task_bound(check)
        if bound:
            explain.extend(bound)
        hit = [m for m in check.get("modules") or [] if m in affected]
        for module_id in hit:
            explain.extend(reasons.get(module_id, []))
        ref = f"check:{check['id']}"
        if ref in chains:
            for hop in chains[ref]:
                explain.append(f"ребро {hop['consumer']} → "
                               f"{hop['dependency']} ({hop['source']})")
        if "applicability" in check:
            applicable, why = _check_applicability(
                check["applicability"], changed, checkpoint)
            if applicable:
                explain.append(why)
        if explain:
            required[check["id"]] = sorted(set(explain))
    checks = {}
    for check in policy["checks"]:
        if check["id"] in required:
            checks[check["id"]] = {"status": "run",
                                   "explain": required[check["id"]]}
        else:
            checks[check["id"]] = {"status": "not_applicable",
                                   "explain": ["обязательства "
                                               "не затронуты"]}

    # Недоказанная граница: неприписанные пути при недостаточном
    # одобренном unknown_scope блокируют набор до решения инженера.
    blocked_reason = None
    if unattributed:
        if unknown_scope == "project":
            affected.update(modules_by_id)
            for check in policy["checks"]:
                entry = checks[check["id"]]
                entry["explain"] = sorted(
                    set(entry["explain"]) |
                    {"неприписанные пути — расширение до проекта"})
                if entry["status"] == "not_applicable":
                    entry["status"] = "run"
        else:
            blocked_reason = (
                f"неприписанные пути {unattributed}: "
                f"unknown_scope={unknown_scope} не разрешает "
                f"проектное расширение — требуется решение инженера")
            for check in policy["checks"]:
                entry = checks[check["id"]]
                entry["status"] = "blocked"
                entry["explain"] = entry["explain"] + [blocked_reason]
    return {"checks": checks, "affected": affected,
            "unattributed": unattributed, "blocked": blocked_reason,
            "chains": {ref: chain for ref, chain in chains.items()}}


def definition_digest(check):
    """Отпечаток определения проверки для binding.definition_digest."""
    return verification_policy.fingerprint(check)


def closure_digest(edges, check):
    """Отпечаток dependency-замыкания проверки в текущем графе.

    Семена — ``check:<id>`` и связанные ``module:<m>``; вперёд по
    рёбрам consumer→dependency. Смена карты расширяет замыкание до
    решения о reuse (design §6).
    """
    forward = {}
    for edge in edges:
        forward.setdefault(edge["consumer"], []).append(edge)
    seeds = [f"check:{check['id']}"] + \
            [f"module:{m}" for m in check.get("modules") or []]
    seen, queue = set(seeds), list(seeds)
    used = []
    while queue:
        ref = queue.pop(0)
        for edge in forward.get(ref, []):
            used.append(_key(edge))
            target = edge["dependency"]
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return verification_policy.fingerprint(
        {"seeds": sorted(seeds), "edges": sorted(set(used))})


def _key(edge):
    return (edge["consumer"], edge["dependency"], edge["kind"])


def environment_identity(policy):
    """Наблюдаемая идентичность среды для сравнения binding.

    Read-only: версия интерпретатора и платформа читаются из runtime;
    неизвестный toolchain — ``unknown``: такой факт не совпадёт с
    честным результатом, и reuse не будет доказан.
    """
    import platform
    env = {"platform": sys.platform}
    for tool in policy["environment"].get("toolchain") or []:
        env[tool["id"]] = (platform.python_version()
                           if tool["id"] == "python" else "unknown")
    return env


def binding_environment(policy, check):
    """Среда привязки результата конкретной проверки (R-11).

    К наблюдаемой ``environment_identity`` добавляются внешние факты,
    объявленные проверкой в ``required_environment``: политика
    фиксирует ожидаемую ``version`` либо маркер ``ttl``/``always_run``
    для каждого внешнего ``environment.external``. Смена объявленной
    версии или вида свежести меняет привязку и обесценивает результат;
    сам TTL/always_run оценивает runner отдельно от identity.
    """
    env = environment_identity(policy)
    externals = {item["id"]: item for item in
                 policy["environment"].get("external") or []}
    for name in check.get("required_environment") or []:
        external = externals.get(name)
        if external is None:
            env[f"required:{name}"] = "unknown"
            continue
        freshness = external["freshness"]
        if freshness["kind"] == "version":
            env[f"external:{name}"] = freshness["version"]
        else:
            env[f"external:{name}"] = {"freshness": freshness["kind"]}
    return env


def _latest_results(results):
    """Последний результат на check_id; более поздний failed важнее."""
    latest = {}
    for result in results:
        key = result["check_id"]
        prev = latest.get(key)
        if prev is None or result["finished_at"] > prev["finished_at"]:
            latest[key] = result
    return latest


def evaluate_reuse(check, result, *, root, policy, edges, state,
                   hash_fn=None, task_digest=None, now=None,
                   projections=None, environment=None):
    """Решение reuse для одной обязательной проверки (R-11).

    ``reused`` требует успешного валидного результата с совпадающими
    значимыми входами, определением, замыканием графа, политикой,
    адаптером/runner и средой. Неизвестная среда reuse исключает.
    ``projections`` и ``environment`` — те же значения, что runner
    передавал снимку при записи результата (иначе входы
    projection/environment не снимаются и свежесть недоказуема).
    """
    if result is None:
        return {"status": "run", "reason": "результатов нет"}
    if result["status"] != "passed":
        return {"status": "run",
                "reason": f"последний результат {result['status']}"}
    if result["project_id"] != policy["project_id"]:
        return {"status": "run", "reason": "результат другого проекта"}
    binding = result["binding"]
    if binding["policy_digest"] != \
            verification_policy.fingerprint(policy):
        return {"status": "run",
                "reason": "результат привязан к другой политике"}
    if binding["definition_digest"] != definition_digest(check):
        return {"status": "run",
                "reason": "изменилось определение проверки"}
    if binding["dependency_closure_digest"] != \
            closure_digest(edges, check):
        return {"status": "run",
                "reason": "изменилось замыкание графа"}
    if check["kind"] == "test" and binding["adapter"] != {
            "id": checks_contract.BUILTIN_ADAPTER,
            "version": str(checks_contract.ADAPTER_PROTOCOL_VERSION)}:
        return {"status": "run", "reason": "другой адаптер"}
    if binding["runner"] != RUNNER:
        return {"status": "run", "reason": "другой runner"}
    environment = (environment if environment is not None
                   else binding_environment(policy, check))
    if binding["environment"] != environment:
        return {"status": "run",
                "reason": "среда отличается или неизвестна"}
    has_task_input = any(isinstance(e, dict) and
                         e.get("type") == "task_projection"
                         for e in check.get("inputs") or [])
    if has_task_input:
        if binding.get("task_requirements_digest") != task_digest:
            return {"status": "run",
                    "reason": "требования задачи изменились"}
    try:
        snap = checks_inventory.snapshot(
            root, policy, checks_inventory.resolved_inputs(
                policy, check), environment=environment,
            projections=projections,
            hash_fn=_counting_hasher(
                state, policy["fallback"]["limits"]
                .get("max_bytes"), hash_fn), now=now)
    except BudgetExceeded as error:
        return {"status": "run", "reason": str(error)}
    if binding["input_digest"] != \
            checks_inventory.snapshot_digest(snap):
        return {"status": "run", "reason": "входы изменились"}
    return {"status": "reused",
            "reason": f"reused {result['result_id']} "
                      f"от {result['finished_at']}",
            "result": result}


def _counting_reader(root, state, limit, read_file):
    """Обёртка чтения исходников: считает байты и рубит по max_bytes."""
    def wrapped(rel):
        size = (root / rel).lstat().st_size
        if limit is not None and state["bytes"] + size > limit:
            raise BudgetExceeded(
                f"лимит чтения {limit} байт исчерпан")
        state["bytes"] += size
        state["files"] += 1
        if read_file is not None:
            return read_file(rel)
        return (root / rel).read_bytes().decode("utf-8")
    return wrapped


def plan(root, policy, *, baseline=None, graph=None, previous=None,
         results=(), task_digest=None, checkpoint=None, hash_fn=None,
         read_file=None, now=None, projections=None, environment=None):
    """Построить объяснимый план: required/reused/run/blocked + цена.

    Ничего не исполняет и не пишет. ``baseline`` — прошлый снимок
    universe (``load_snapshot`` либо None), ``graph`` — объединённый
    документ ``checks-graph.json`` (без него строится из ``previous``
    тем же бюджетом байтов, что и снимок), ``results`` — валидные
    результаты для оценки reuse, ``task_digest`` — отпечаток проекции
    карточки. Превышение max_bytes при анализе или снимке блокирует
    затронутые обязательства, а не расширяет запуск (R-10).
    """
    root = Path(root)
    state = {"bytes": 0, "files": 0}
    limit = policy["fallback"]["limits"].get("max_bytes")
    ast_reason = ""
    if graph is None:
        try:
            graph = checks_graph.build_graph(
                root, policy, previous=previous,
                read_file=_counting_reader(root, state, limit,
                                           read_file),
                now=now)
        except BudgetExceeded as error:
            ast_reason = str(error)
            graph = {"edges": [], "unknown": [
                {"scope": {"kind": "project"},
                 "reason": f"анализ зависимостей прерван бюджетом: "
                           f"{error}"}]}
    change = compute_change(root, policy, baseline, state,
                            hash_fn=hash_fn, now=now)
    changed = change["changed"]
    if ast_reason:
        changed = None
    selection = select_checks(
        policy, changed=changed, edges=graph["edges"],
        unknown=graph["unknown"], checkpoint=checkpoint)
    latest = _latest_results(results)
    for check in policy["checks"]:
        entry = selection["checks"][check["id"]]
        if entry["status"] != "run":
            continue
        verdict = evaluate_reuse(
            check, latest.get(check["id"]), root=root, policy=policy,
            edges=graph["edges"], state=state, hash_fn=hash_fn,
            task_digest=task_digest, now=now, projections=projections,
            environment=environment)
        if verdict["status"] == "reused":
            entry["status"] = "reused"
            entry["explain"] = entry["explain"] + [verdict["reason"]]
        else:
            entry["explain"] = entry["explain"] + \
                [f"запуск: {verdict['reason']}"]

    limits = policy["fallback"]["limits"]
    bytes_exceeded = bool(ast_reason) or change["bytes_exceeded"]
    estimate = sum(check["timeout_seconds"]
                   for check in policy["checks"]
                   if selection["checks"][check["id"]]["status"] == "run"
                   and check["kind"] in {"test", "command"})
    over_seconds = estimate > limits["max_seconds"]
    block_reasons = []
    if selection["blocked"]:
        block_reasons.append(selection["blocked"])
    if bytes_exceeded:
        block_reasons.append(
            f"чтение входов превысило max_bytes={limit} "
            f"({ast_reason or change['reason']}) — "
            f"требуется решение инженера")
        for check in policy["checks"]:
            entry = selection["checks"][check["id"]]
            if entry["status"] == "run":
                entry["status"] = "blocked"
                entry["explain"] = entry["explain"] + [block_reasons[-1]]
        estimate = 0
        over_seconds = False
    if over_seconds:
        block_reasons.append(
            f"оценка запуска {estimate}с превышает "
            f"max_seconds={limits['max_seconds']} — "
            f"требуется решение инженера")
        for check in policy["checks"]:
            entry = selection["checks"][check["id"]]
            if entry["status"] == "run" \
                    and check["kind"] in {"test", "command"}:
                entry["status"] = "blocked"
                entry["explain"] = entry["explain"] + [block_reasons[-1]]
    summary = {status: 0 for status in STATUS_ORDER}
    for entry in selection["checks"].values():
        summary[entry["status"]] += 1
    return {
        "checks": {check["id"]: selection["checks"][check["id"]]
                   for check in policy["checks"]},
        "changed": changed,
        "change_reason": change["reason"],
        "snapshot": change["snapshot"],
        "bytes_exceeded": bytes_exceeded,
        "affected": sorted(selection["affected"]),
        "unattributed": selection["unattributed"],
        "estimate_seconds": estimate,
        "limits": limits,
        "budget_blocked": bool(block_reasons),
        "budget_reasons": block_reasons,
        "hashed_bytes": state["bytes"],
        "hashed_files": state["files"],
        "summary": summary,
        "graph_edges": len(graph["edges"]),
        "graph_unknown": len(graph["unknown"]),
        "graph": graph,
    }
