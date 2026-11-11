"""Runner обязательств ``checks run`` и покрытие ``checks status`` (PB-060).

Runner исполняет набор, вычисленный ``checks_select.plan``, через
адаптер ``python-unittest`` и команды argv, пишет append-only
результаты схемы 1 с полной привязкой (политика, определение, замыкание
графа, входы, среда, требования задачи) и обновляет baseline/граф
контура в ``work/checks/``. Reused — способ покрытия, не новый запуск:
указатель на исходный результат записывается отдельной строкой истории
(R-12). Локальный режим сверяет входы до и после каждой команды и честно
сообщает более слабую гарантию: изменившиеся во время запуска входы
дают ``blocked``, не ``passed`` (R-15); изолированный кандидат и
квитанции — доверенный контур PB-061.

Режимы ортогональны контракту PB-057: ``--scope task`` — фактические
изменения задачи, ``--scope project`` — весь обязательный набор;
``--mode stale`` исполняет только обязательства без пригодного успеха,
``--mode force`` — весь выбранный набор заново. Внешние свежести:
``always_run`` исключает reuse, ``ttl`` ограничивает возраст результата,
``version`` участвует в привязке среды. Ручные обязательства runner не
исполняет — ``not_run`` с причиной, подтверждение остаётся инженером.
"""
import datetime as dt
import subprocess
import time
import uuid
from pathlib import Path

import checks_contract
import checks_graph
import checks_inventory
import checks_results
import checks_select
import checks_unittest
import verification
import verification_policy


RUNS_DIR = "runs"
COMMAND_ADAPTER = {"id": "argv-command", "version": "1"}

TRUST_NOTE_LOCAL = ("trust_mode=local: результаты диагностические — "
                    "локальная история не защищена от владельца "
                    "проекта; доверенное выполнение требует контура "
                    "PB-061")
TRUST_NOTE_PROTECTED = ("trust_mode=protected: локальные результаты "
                        "не являются доверенными квитанциями; "
                        "подтверждение — внешний verifier (PB-061)")


def _raise(condition, message):
    if not condition:
        raise ValueError(message)


def _clock(now):
    return now or verification.timestamp()


def _iso_dt(value):
    return dt.datetime.fromisoformat(value)


def _load_state(root):
    """Baseline-снимок и прежний граф контура с заметками о порче."""
    notes = []
    baseline = None
    snap_path = Path(root) / checks_select.STATE_DIR / \
        checks_select.SNAPSHOT_FILENAME
    if snap_path.is_file():
        try:
            baseline = checks_inventory.load_snapshot(snap_path)
        except (ValueError, OSError) as error:
            notes.append(f"baseline-снимок непригоден: {error}")
    previous = None
    graph_path = Path(root) / checks_select.STATE_DIR / \
        checks_graph.GRAPH_FILENAME
    if graph_path.is_file():
        try:
            previous = checks_graph.load_graph(graph_path)
        except (ValueError, OSError) as error:
            notes.append(f"прежний граф непригоден: {error}")
    return baseline, previous, notes


def _persist_state(root, snapshot_doc, graph_doc):
    """Записать baseline-снимок и объединённый граф атомарно."""
    state = Path(root) / checks_select.STATE_DIR
    state.mkdir(parents=True, exist_ok=True)
    if snapshot_doc is not None:
        checks_results._atomic_write(
            state / checks_select.SNAPSHOT_FILENAME, snapshot_doc)
    if graph_doc is not None:
        checks_results._atomic_write(
            state / checks_graph.GRAPH_FILENAME, graph_doc)


def _projections(policy, task_content, task_meta):
    return {"task_projection":
            verification_policy.task_projection(task_content, task_meta),
            "policy_projection": policy}


def _external_freshness(check, policy, entry, result, now):
    """Внешняя свежесть результата: ttl/always_run режут reuse (R-11).

    ``version`` уже учтена привязкой среды; здесь — временная ось.
    Возвращает None, если reuse допустим, иначе причину запуска.
    """
    externals = {item["id"]: item
                 for item in policy["environment"].get("external") or []}
    for name in check.get("required_environment") or []:
        external = externals.get(name)
        if external is None:
            continue
        freshness = external["freshness"]
        if freshness["kind"] == "always_run":
            return f"внешний {name}: политика always_run"
        if freshness["kind"] == "ttl":
            try:
                # A-01: TTL от фактического запуска — цепочка reuse
                # сохраняет исходное время в reused.issued_at.
                finished = _iso_dt(result.get("reused", {}).get(
                    "issued_at") or result["finished_at"])
                current = _iso_dt(now) if now else \
                    dt.datetime.now().astimezone()
            except (ValueError, TypeError):
                return f"внешний {name}: возраст результата не доказан"
            if (current - finished).total_seconds() > \
                    freshness["ttl_seconds"]:
                return (f"внешний {name}: ttl "
                        f"{freshness['ttl_seconds']}с истёк")
    return None


def _apply_external_freshness(checks, results, policy, now):
    """stale-режим: внешняя свежесть ttl/always_run режет reuse."""
    latest = checks_results.latest(results)
    for check in policy["checks"]:
        entry = checks[check["id"]]
        if entry["status"] != "reused":
            continue
        result = latest.get(check["id"])
        reason = _external_freshness(check, policy, entry, result, now)
        if reason is not None:
            entry["status"] = "run"
            entry["explain"] = entry["explain"] + [f"запуск: {reason}"]


def _budget_recheck(checks, policy):
    """Повторная оценка бюджета после смены набора (force/external)."""
    limits = policy["fallback"]["limits"]
    estimate = sum(check["timeout_seconds"]
                   for check in policy["checks"]
                   if checks[check["id"]]["status"] == "run"
                   and check["kind"] in {"test", "command"})
    if estimate > limits["max_seconds"]:
        reason = (f"оценка запуска {estimate}с превышает "
                  f"max_seconds={limits['max_seconds']} — "
                  f"требуется решение инженера")
        for check in policy["checks"]:
            entry = checks[check["id"]]
            if entry["status"] == "run" and \
                    check["kind"] in {"test", "command"}:
                entry["status"] = "blocked"
                entry["explain"] = entry["explain"] + [reason]
        return reason
    return None


def _binding(policy, check, edges, input_digest, task_digest, env):
    binding = {
        "policy_digest": verification_policy.fingerprint(policy),
        "definition_digest": checks_select.definition_digest(check),
        "adapter": (dict(checks_unittest.ADAPTER)
                    if check["kind"] == "test" else
                    dict(COMMAND_ADAPTER)),
        "runner": dict(checks_select.RUNNER),
        "input_digest": input_digest,
        "dependency_closure_digest":
            checks_select.closure_digest(edges, check),
        "environment": env,
    }
    if any(isinstance(e, dict) and e.get("type") == "task_projection"
           for e in check.get("inputs") or []):
        binding["task_requirements_digest"] = task_digest
    return binding


def _snapshot_inputs(root, policy, check, projections, env, now):
    inputs = checks_inventory.resolved_inputs(policy, check)
    return checks_inventory.snapshot(
        root, policy, inputs, environment=env,
        projections=projections, now=now)


def _execute_test(root, check, policy, run_dir, now, *, env=None,
                  wrap=None):
    """Запуск через адаптер и сверка с каталогом."""
    isolation = {"wrap": wrap} if wrap else {}
    collected = checks_unittest.collect(
        root, check, run_dir,
        timeout=check["timeout_seconds"], env=env, **isolation)
    outcome = checks_unittest.verdict(check, policy, collected)
    evidence = {"stdout_log": collected["stdout_log"],
                "stderr_log": collected["stderr_log"],
                "adapter_report": collected["report_path"]}
    return outcome["status"], outcome["reasons"], evidence, \
        {"tests": outcome["tests"]}


def _execute_command(root, check, run_dir, now, *, env=None, wrap=None):
    """Команда с постусловием по exit-коду; выдуманного числа тестов нет."""
    stdout = run_dir / (check["id"] + ".stdout.log")
    stderr = run_dir / (check["id"] + ".stderr.log")
    command = {"exit_code": None, "stdout_log": str(
        stdout.relative_to(root)), "stderr_log": str(
        stderr.relative_to(root))}
    argv = (list(wrap) + list(check["argv"])) if wrap \
        else check["argv"]
    process = None
    try:
        with stdout.open("xb") as out, stderr.open("xb") as err:
            process = subprocess.Popen(
                argv, cwd=root, stdin=subprocess.DEVNULL,
                stdout=out, stderr=err, env=env,
                start_new_session=True)
            command["exit_code"] = process.wait(
                timeout=check["timeout_seconds"])
    except subprocess.TimeoutExpired:
        return "timeout", ["превышен timeout проверки"], \
            {"stdout_log": command["stdout_log"],
             "stderr_log": command["stderr_log"]}, {"command": command}
    except OSError as error:
        return "blocked", [f"запуск невозможен: {error}"], \
            {"stdout_log": command["stdout_log"],
             "stderr_log": command["stderr_log"]}, {"command": command}
    finally:
        if process is not None:
            verification.stop_group(process)
            command.setdefault("exit_code", process.returncode)
    status = "passed" if command["exit_code"] == 0 else "failed"
    reasons = [] if status == "passed" else \
        [f"команда завершилась кодом {command['exit_code']}"]
    return status, reasons, {"stdout_log": command["stdout_log"],
                             "stderr_log": command["stderr_log"]}, \
        {"command": command}


def _reused_result(policy, check, source, edges, task_digest, env,
                   input_digest, run_id, now):
    """Строка истории reuse: ссылка на исходный успех, не новый прогон."""
    body = {key: source[key] for key in ("tests", "command", "manual")
            if key in source}
    result = {
        "schema_version": 1,
        "result_id": f"{run_id}-{check['id']}-reused",
        "project_id": policy["project_id"], "check_id": check["id"],
        "kind": check["kind"],
        "binding": _binding(policy, check, edges, input_digest,
                            task_digest, env),
        "status": "passed",
        "reused": {"result_id": source["result_id"],
                   "issued_at": source.get("reused", {}).get(
                       "issued_at") or source["finished_at"],
                   "input_digest": source["binding"]["input_digest"]},
        "started_at": _clock(now), "finished_at": _clock(now),
        "evidence": {"run_id": run_id, "source": "history"},
        **body,
    }
    source_run = source.get("evidence", {}).get("run_id")
    if source_run:
        result["reused"]["run_id"] = source_run
    checks_contract.validate_result(result, _raise)
    return result


def execute(root, policy, *, task_id, scope, mode, checkpoint,
            task_content, task_meta, now=None, emit=print, env=None,
            argv_prefix=None):
    """Выполнить обязательства области и вернуть итог прогона.

    Пишет результаты и обновляет baseline/граф контура. Возвращает
    ``{"ok": bool, ..., "results": [result...]}``; ``ok``
    истинно только когда все обязательства области покрыты passed или
    reused — ручные и блокированные остаются непокрытыми. ``env`` и
    ``argv_prefix`` задают окружение и механизм изоляции подпроцессов
    (доверенный контур PB-061); по умолчанию — наследование среды.
    """
    root = Path(root)
    task_digest = verification_policy.fingerprint(
        verification_policy.task_projection(task_content, task_meta))
    projections = _projections(policy, task_content, task_meta)
    baseline, previous, notes = _load_state(root)
    results, load_notes, corrupt_ids = checks_results.load_results(root)
    notes.extend(load_notes)
    # A-03: повреждённая запись делает историю проверки недоказанной —
    # результаты затронутых check_id не участвуют в reuse, обязательство
    # консервативно перезапускается.
    if corrupt_ids:
        results = [r for r in results
                   if r["check_id"] not in corrupt_ids]
        notes.append("повреждённая история check_id "
                     f"{sorted(corrupt_ids)} — результаты не "
                     f"переиспользуются")

    planned = checks_select.plan(
        root, policy, baseline=baseline if scope == "task" else None,
        previous=previous, results=results, task_digest=task_digest,
        checkpoint=checkpoint, now=now, projections=projections)
    checks = {cid: dict(entry)
              for cid, entry in planned["checks"].items()}
    graph = planned["graph"]
    edges = graph["edges"]
    if scope == "project":
        for check in policy["checks"]:
            entry = checks[check["id"]]
            if entry["status"] in {"run", "reused"}:
                entry["explain"] = entry["explain"] + \
                    ["область project — весь обязательный набор"]
    if mode == "stale":
        _apply_external_freshness(checks, results, policy, now)
    else:
        for check in policy["checks"]:
            entry = checks[check["id"]]
            if entry["status"] == "reused":
                entry["status"] = "run"
                entry["explain"] = entry["explain"] + \
                    ["режим force — выбранный набор заново"]
    budget_reason = _budget_recheck(checks, policy)

    run_id = uuid.uuid4().hex
    run_dir = Path(root) / checks_select.STATE_DIR / RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    emit(f"checks run {task_id}: политика {policy['policy_id']} "
         f"rev {policy['revision']}, scope={scope} mode={mode}"
         + (f" checkpoint={checkpoint}" if checkpoint else ""))
    emit("Ограничение: " + (TRUST_NOTE_LOCAL
                            if policy["trust_mode"] == "local"
                            else TRUST_NOTE_PROTECTED))

    stored, outcomes = [], {}
    latest = checks_results.latest(results)
    for check in policy["checks"]:
        entry = checks[check["id"]]
        status = entry["status"]
        # A-03/A-05 аудита PB-061: окружение процесса (env) и идентичность
        # среды привязки (bind_env) — разные величины; binding_environment
        # не является окружением подпроцесса.
        bind_env = checks_select.binding_environment(policy, check)
        if status == "reused":
            source = latest.get(check["id"])
            try:
                snap = _snapshot_inputs(root, policy, check,
                                        projections, bind_env, now)
                input_digest = \
                    checks_inventory.snapshot_digest(snap)
                # A-02: запись reuse только на байтах исходного успеха;
                # расхождение снимков — гонка, обязательство идёт на
                # запуск, старый успех не перепривязывается.
                if input_digest != source["binding"]["input_digest"]:
                    status = "run"
                    emit(f"  {check['id']}: входы изменились после "
                         f"оценки reuse — запуск")
                else:
                    result = _reused_result(
                        policy, check, source, edges, task_digest,
                        bind_env, input_digest, run_id, now)
            except (ValueError, OSError) as error:
                result = {"schema_version": 1,
                          "result_id": f"{run_id}-{check['id']}",
                          "project_id": policy["project_id"],
                          "check_id": check["id"],
                          "kind": check["kind"],
                          "binding": _binding(
                              policy, check, edges,
                              "sha256:" + "0" * 64, task_digest,
                              bind_env),
                          "status": "blocked",
                          "reason": f"входы результата не доказаны "
                                    f"при записи reuse: {error}",
                          "started_at": _clock(now),
                          "finished_at": _clock(now),
                          "evidence": {"run_id": run_id}}
                checks_contract.validate_result(result, _raise)
                status = "blocked"
            if status == "reused":
                checks_results.store_result(root, run_id, result)
                checks_results.update_index(root, run_id, result)
                stored.append(result)
                outcomes[check["id"]] = status
                emit(f"  {check['id']}: reused — исходный результат "
                     f"{source['result_id']} от "
                     f"{source['finished_at']}")
                continue
            if status == "blocked":
                checks_results.store_result(root, run_id, result)
                checks_results.update_index(root, run_id, result)
                stored.append(result)
                outcomes[check["id"]] = status
                emit(f"  {check['id']}: blocked — {result['reason']}")
                continue
        if status == "blocked":
            reason = entry["explain"][-1] if entry["explain"] else \
                "обязательство заблокировано"
            result = {"schema_version": 1,
                      "result_id": f"{run_id}-{check['id']}",
                      "project_id": policy["project_id"],
                      "check_id": check["id"], "kind": check["kind"],
                      "binding": _binding(
                          policy, check, edges,
                          "sha256:" + "0" * 64, task_digest, bind_env),
                      "status": "blocked", "reason": reason,
                      "started_at": _clock(now),
                      "finished_at": _clock(now),
                      "evidence": {"run_id": run_id}}
            checks_contract.validate_result(result, _raise)
            checks_results.store_result(root, run_id, result)
            checks_results.update_index(root, run_id, result)
            stored.append(result)
            outcomes[check["id"]] = "blocked"
            emit(f"  {check['id']}: blocked — {reason}")
            continue
        if status != "run":
            outcomes[check["id"]] = status
            emit(f"  {check['id']}: not_applicable")
            continue
        if check["kind"] == "manual":
            result = {"schema_version": 1,
                      "result_id": f"{run_id}-{check['id']}",
                      "project_id": policy["project_id"],
                      "check_id": check["id"], "kind": "manual",
                      "binding": _binding(
                          policy, check, edges,
                          "sha256:" + "0" * 64, task_digest, bind_env),
                      "status": "not_run",
                      "reason": "ручная проверка: runner не исполняет, "
                                "подтверждение — инженер",
                      "started_at": _clock(now),
                      "finished_at": _clock(now),
                      "evidence": {"run_id": run_id}}
            checks_contract.validate_result(result, _raise)
            checks_results.store_result(root, run_id, result)
            checks_results.update_index(root, run_id, result)
            stored.append(result)
            outcomes[check["id"]] = "not_run"
            emit(f"  {check['id']}: not_run — ручная проверка "
                 f"({check['scenario']})")
            continue
        # Исполняемая проверка: снимок входов до и после команды.
        started_at = _clock(now)
        started = time.monotonic()
        try:
            pre = _snapshot_inputs(root, policy, check, projections,
                                   bind_env, now)
        except (ValueError, OSError) as error:
            result = {"schema_version": 1,
                      "result_id": f"{run_id}-{check['id']}",
                      "project_id": policy["project_id"],
                      "check_id": check["id"], "kind": check["kind"],
                      "binding": _binding(
                          policy, check, edges,
                          "sha256:" + "0" * 64, task_digest, bind_env),
                      "status": "blocked",
                      "reason": f"снимок входов не построен: {error}",
                      "started_at": started_at,
                      "finished_at": _clock(now),
                      "evidence": {"run_id": run_id}}
            checks_contract.validate_result(result, _raise)
            checks_results.store_result(root, run_id, result)
            checks_results.update_index(root, run_id, result)
            stored.append(result)
            outcomes[check["id"]] = "blocked"
            emit(f"  {check['id']}: blocked — {result['reason']}")
            continue
        emit(f"  {check['id']}: RUN {check['kind']}")
        if check["kind"] == "test":
            if check.get("adapter") != checks_contract.BUILTIN_ADAPTER:
                status_r, reasons, evidence, body = "blocked", [
                    f"неподдерживаемый адаптер "
                    f"{check.get('adapter')!r}"], {"run_id": run_id}, \
                    {"tests": {"expected": [], "actual": []}}
            else:
                status_r, reasons, evidence, body = _execute_test(
                    root, check, policy, run_dir, now, env=env,
                    wrap=argv_prefix)
        else:
            status_r, reasons, evidence, body = _execute_command(
                root, check, run_dir, now, env=env, wrap=argv_prefix)
        evidence["run_id"] = run_id
        mutation = None
        if status_r in {"passed", "failed"}:
            try:
                post = _snapshot_inputs(root, policy, check,
                                        projections, bind_env, now)
                if checks_inventory.snapshot_digest(post) != \
                        checks_inventory.snapshot_digest(pre):
                    mutation = ("входы изменились во время запуска — "
                                "локальный режим не изолирует кандидат")
            except (ValueError, OSError) as error:
                mutation = f"контроль входов после запуска: {error}"
        if mutation is not None:
            status_r = "blocked"
            reasons = [mutation]
        result = {
            "schema_version": 1,
            "result_id": f"{run_id}-{check['id']}",
            "project_id": policy["project_id"], "check_id": check["id"],
            "kind": check["kind"],
            "binding": _binding(
                policy, check, edges,
                checks_inventory.snapshot_digest(pre), task_digest,
                bind_env),
            "status": status_r,
            "started_at": started_at,
            "finished_at": _clock(now),
            "duration_seconds": round(time.monotonic() - started, 3),
            "evidence": evidence,
            **body,
        }
        if reasons:
            result["reason"] = "; ".join(reasons)
        checks_contract.validate_result(result, _raise)
        checks_results.store_result(root, run_id, result)
        checks_results.update_index(root, run_id, result)
        stored.append(result)
        outcomes[check["id"]] = status_r
        if status_r == "passed":
            emit(f"  {check['id']}: passed "
                 f"({result['duration_seconds']}с)")
        else:
            emit(f"  {check['id']}: {status_r} — {result['reason']}")

    _persist_state(root, planned["snapshot"], graph)
    checks_results.rebuild_index(root, policy)
    summary = {"passed": 0, "reused": 0, "failed": 0, "timeout": 0,
               "blocked": 0, "not_run": 0, "not_applicable": 0}
    for value in outcomes.values():
        summary[value] = summary.get(value, 0) + 1
    uncovered = [check["id"] for check in policy["checks"]
                 if outcomes.get(check["id"]) not in
                 {"passed", "reused", "not_applicable"}]
    ok = not uncovered
    emit("Итог: " + ", ".join(f"{k}={v}" for k, v in summary.items()
                              if v) + f"; непокрытых обязательств: "
         f"{len(uncovered)}")
    for note in notes:
        emit(f"Примечание: {note}")
    return {"ok": ok, "summary": summary, "uncovered": uncovered,
            "outcomes": outcomes, "run_id": run_id, "results": stored}


def status(root, policy, *, task_id, task_content, task_meta, now=None,
           emit=print):
    """Покрытие обязательств, фактические проверки и ограничения доверия."""
    root = Path(root)
    task_digest = verification_policy.fingerprint(
        verification_policy.task_projection(task_content, task_meta))
    projections = _projections(policy, task_content, task_meta)
    baseline, previous, notes = _load_state(root)
    results, load_notes, corrupt_ids = checks_results.load_results(root)
    notes.extend(load_notes)
    if corrupt_ids:
        results = [r for r in results
                   if r["check_id"] not in corrupt_ids]
        notes.append("повреждённая история check_id "
                     f"{sorted(corrupt_ids)} — результаты не "
                     f"переиспользуются")
    planned = checks_select.plan(
        root, policy, baseline=baseline, previous=previous,
        results=results, task_digest=task_digest, now=now,
        projections=projections)
    # A-04: status считает покрытие тем же расчётом свежести, что и
    # stale-run, включая внешние ttl/always_run.
    checks = {cid: dict(entry)
              for cid, entry in planned["checks"].items()}
    _apply_external_freshness(checks, results, policy, now)
    latest = checks_results.latest(results)
    emit(f"checks status {task_id}: политика {policy['policy_id']} "
         f"rev {policy['revision']}")
    emit("Ограничение: " + (TRUST_NOTE_LOCAL
                            if policy["trust_mode"] == "local"
                            else TRUST_NOTE_PROTECTED))
    catalog = {e["id"]: e for e in policy["test_catalog"]["entries"]}
    covered = 0
    obligated = 0
    for check in policy["checks"]:
        entry = checks[check["id"]]
        result = latest.get(check["id"])
        line = entry["status"]
        if result is not None:
            line += (f"; последний результат {result['status']} "
                     f"{result['result_id']} от {result['finished_at']}")
            if result.get("reused"):
                line += (f" (reused {result['reused']['result_id']} от "
                         f"{result['reused'].get('issued_at', '?')})")
        if check["kind"] == "test" and result is not None \
                and result.get("tests"):
            actual = result["tests"]["actual"]
            fails = [a for a in actual
                     if a["outcome"] != "passed"]
            line += f"; тестов: {len(actual)}"
            for item in fails[:5]:
                descr = catalog.get(item["id"], {}).get(
                    "description", "")
                line += (f"\n      {item['outcome']}: {item['id']}"
                         + (f" — {descr}" if descr else ""))
        emit(f"  {check['id']}: {line}")
        if entry["status"] in {"run", "reused", "blocked"}:
            obligated += 1
            # A-04: покрытие только по пригодному результату — план уже
            # учёл свежесть входов, привязку и внешнюю свежесть; старый
            # passed при обязательном run покрытия не даёт.
            if entry["status"] == "reused":
                covered += 1
    emit(f"Покрытие обязательств области задачи: {covered}/{obligated}")
    for note in notes:
        emit(f"Примечание: {note}")
    return {"covered": covered, "obligated": obligated}
