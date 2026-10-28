"""Explicit local checks; results are evidence, not task acceptance."""

import datetime as dt
import hashlib
import json
import math
import os
import platform
import signal
import subprocess
import sys
import time
import uuid

import task_cards
import documents


def validate(root, settings, safe_path, require):
    require(isinstance(settings, dict), "Нет verification в project.json")
    inputs = settings.get("inputs")
    require(isinstance(inputs, list) and inputs, "verification.inputs должен быть непустым списком")
    for entry in inputs:
        path = safe_path(root, entry)
        require(path != root and not path.is_relative_to(root / "work"),
                "Входы проверки не должны включать корень проекта или work")
        require(path.exists(), f"Нет входа проверки: {entry}")
    checks = settings.get("checks")
    require(isinstance(checks, list) and checks, "verification.checks должен быть непустым списком")
    ids = set()
    for item in checks:
        require(isinstance(item, dict), "Проверка должна быть объектом")
        ident = item.get("id")
        require(isinstance(ident, str) and ident.isascii()
                and ident.replace("-", "").replace("_", "").isalnum(), "Некорректный ID проверки")
        require(ident not in ids, f"Повторный ID проверки: {ident}")
        ids.add(ident)
        argv = item.get("argv")
        require(isinstance(argv, list) and argv
                and all(isinstance(arg, str) and "\0" not in arg for arg in argv)
                and argv[0].strip(), f"Некорректный argv: {ident}")
        timeout = item.get("timeout_seconds")
        require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
                f"Нужен положительный конечный timeout_seconds: {ident}")
    return settings


def snapshot(root, inputs, safe_path, require):
    """Hash declared files; caches are excluded, symlinks are rejected."""
    files = {}
    for entry in inputs:
        path = safe_path(root, entry)
        require(path.exists(), f"Нет входа проверки: {entry}")
        candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
        for candidate in candidates:
            relative = candidate.relative_to(root)
            if "__pycache__" in relative.parts or candidate.name == ".DS_Store" or candidate.suffix == ".pyc":
                continue
            safe_path(root, str(relative))
            if candidate.is_dir():
                continue
            require(candidate.is_file(), f"Вход не является обычным файлом: {relative}")
            content = candidate.read_bytes()
            if candidate.suffix == '.md' and (b'card_format_version' in content or documents.EXTERNAL_META.encode() in content):
                try:
                    content = task_cards.normalize_checkboxes(content.decode('utf-8')).encode('utf-8')
                except (ValueError, UnicodeError):
                    pass  # Invalid formats still get a raw fingerprint; check diagnoses them.
            files[relative.as_posix()] = hashlib.sha256(content).hexdigest()
            if (candidate.suffix == '.md' and documents.EXTERNAL_META.encode() in content
                    and documents._card_id(content.decode('utf-8')) == candidate.stem
                    and documents.external_card(content.decode('utf-8'))):
                documents.read_text(candidate)  # Validate the pair even for an explicit Markdown input.
                sidecar = documents.meta_path(candidate)
                safe_path(root, str(sidecar.relative_to(root)))
                files[sidecar.relative_to(root).as_posix()] = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    payload = json.dumps(files, sort_keys=True).encode()
    return {"sha256": hashlib.sha256(payload).hexdigest(), "files": files}


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _iso_dt(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _validate_snapshot(snap, label, require):
    require(isinstance(snap, dict) and not isinstance(snap, bool),
            f"{label} должен быть объектом")
    require("files" in snap or "sha256" in snap,
            f"{label}: должен содержать 'files' или 'sha256'")
    unknown = sorted(set(snap) - {"files", "sha256"})
    require(not unknown, f"{label}: неизвестные поля {unknown}")
    if "files" in snap:
        files = snap["files"]
        require(isinstance(files, dict) and not isinstance(files, bool),
                f"{label}.files должен быть объектом")
        require(all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()),
                f"{label}.files: пути и хеши должны быть строками")
    if "sha256" in snap:
        require(isinstance(snap["sha256"], str) and bool(snap["sha256"].strip()),
                f"{label}.sha256 должен быть непустой строкой")


TASK_WORKFLOW_FIELDS = {"schema_version", "registry", "checkpoints"}
REGISTRY_FIELDS = {"id", "kind", "purpose", "argv", "scenario", "timeout_seconds",
                   "default_inputs", "applicability"}
REQUIRED_REPORT_V2_FIELDS = {"schema_version", "run_id", "task_id", "route", "plan_revision",
                             "plan_hash", "policy_hash", "at", "status", "results", "coverage"}
REPORT_V2_FIELDS = REQUIRED_REPORT_V2_FIELDS | {"started_at", "finished_at", "cwd",
                                                "card_projection", "execution_id", "execution_sha256"}
RESULT_FIELDS = {"check_id", "definition_hash", "kind", "status", "actor",
                 "environment", "input_snapshot", "started_at", "finished_at",
                 "duration_seconds", "exit_code", "stdout_log", "stderr_log",
                 "snapshot_before", "snapshot_after", "scenario_hash", "observation",
                 "candidate_snapshot", "engineer_confirmation", "materials",
                 "limitations", "reason"}
COVERAGE_FIELDS = {"total_checks", "passed", "failed", "not_applicable", "not_run",
                   "satisfied"}


def validate_task_workflow(root, settings, safe_path, require):
    require(isinstance(settings, dict), "task_workflow должен быть объектом")
    unknown = sorted(set(settings) - TASK_WORKFLOW_FIELDS)
    require(not unknown, f"Неизвестные поля в task_workflow: {unknown}")
    require(settings.get("schema_version") == 2,
            f"task_workflow: неподдерживаемая schema_version {settings.get('schema_version')!r}, требуется 2")
    registry = settings.get("registry")
    require(isinstance(registry, list) and registry,
            "task_workflow.registry должен быть непустым списком")
    registry_ids = set()
    for idx, item in enumerate(registry):
        require(isinstance(item, dict), f"task_workflow.registry[{idx}] должен быть объектом")
        item_unknown = sorted(set(item) - REGISTRY_FIELDS)
        require(not item_unknown, f"Неизвестные поля в registry[{idx}]: {item_unknown}")
        ident = item.get("id")
        require(isinstance(ident, str) and ident.isascii()
                and ident.replace("-", "").replace("_", "").isalnum() and ident,
                f"Некорректный ID проверки в реестре: {ident!r}")
        require(ident not in registry_ids, f"Повторный ID проверки в реестре: {ident}")
        registry_ids.add(ident)
        kind = item.get("kind")
        require(kind in {"command", "manual"},
                f"{ident}: kind должен быть 'command' или 'manual', получено {kind!r}")
        if item.get("purpose") is not None:
            require(isinstance(item["purpose"], str) and item["purpose"].strip(),
                    f"{ident}: purpose должен быть непустой строкой")
        if kind == "command":
            argv = item.get("argv")
            require(isinstance(argv, list) and argv
                    and all(isinstance(arg, str) and "\0" not in arg for arg in argv)
                    and argv[0].strip(), f"Некорректный argv: {ident}")
            timeout = item.get("timeout_seconds")
            require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
                    f"Нужен положительный конечный timeout_seconds: {ident}")
            require("scenario" not in item,
                    f"{ident}: scenario не допускается для command-проверки")
        elif kind == "manual":
            scenario = item.get("scenario")
            require(isinstance(scenario, str) and scenario.strip(),
                    f"{ident}: scenario обязателен для manual-проверки")
            require("argv" not in item,
                    f"{ident}: argv не допускается для manual-проверки")
        if "default_inputs" in item:
            inputs = item["default_inputs"]
            require(isinstance(inputs, list), f"{ident}: default_inputs должен быть списком")
            for inp_idx, inp in enumerate(inputs):
                if isinstance(inp, str):
                    path = safe_path(root, inp)
                    require(path != root and not path.is_relative_to(root / "work"),
                            f"{ident}: вход не должен указывать на корень проекта или work: {inp}")
                    require(not path.is_symlink(), f"{ident}: вход является символической ссылкой: {inp}")
                elif isinstance(inp, dict):
                    inp_type = inp.get("type")
                    require(inp_type in {"file", "directory", "task_projection", "policy_projection", "environment"},
                            f"{ident}: некорректный тип входа: {inp_type!r}")
                    if inp_type in {"file", "directory"}:
                        unknown_inp = sorted(set(inp) - {"type", "path"})
                        require(not unknown_inp, f"{ident}: неизвестные поля во входе '{inp_type}': {unknown_inp}")
                        p = inp.get("path")
                        require(isinstance(p, str) and p.strip() and not p.startswith("/") and ".." not in p,
                                f"{ident}: вход '{inp_type}' требует непустой относительный path")
                        path = safe_path(root, p)
                        require(path != root and not path.is_relative_to(root / "work"),
                                f"{ident}: вход не должен указывать на корень или work")
                        require(not path.is_symlink(), f"{ident}: вход является символической ссылкой: {p}")
                    elif inp_type == "environment":
                        unknown_inp = sorted(set(inp) - {"type", "names"})
                        require(not unknown_inp, f"{ident}: неизвестные поля во входе 'environment': {unknown_inp}")
                        names = inp.get("names")
                        require(isinstance(names, list) and names and all(isinstance(n, str) and n.strip() for n in names),
                                f"{ident}: names должен быть непустым списком строк")
                    elif inp_type in {"task_projection", "policy_projection"}:
                        unknown_inp = sorted(set(inp) - {"type"})
                        require(not unknown_inp, f"{ident}: неизвестные поля во входе '{inp_type}': {unknown_inp}")
                else:
                    require(False, f"{ident}: некорректный элемент default_inputs[{inp_idx}]")
        if "applicability" in item:
            app = item["applicability"]
            if isinstance(app, str):
                require(app in {"always", "checkpoint"},
                        f"{ident}: строковая применимость должна быть 'always' или 'checkpoint'")
            elif isinstance(app, dict):
                app_unknown = sorted(set(app) - {"kind", "paths", "checkpoint"})
                require(not app_unknown, f"{ident}: неизвестные поля в applicability: {app_unknown}")
                app_kind = app.get("kind")
                require(app_kind in {"always", "when_paths", "checkpoint"},
                        f"{ident}: неизвестный kind применимости: {app_kind!r}")
                if app_kind == "when_paths":
                    paths = app.get("paths")
                    require(isinstance(paths, list) and paths
                            and all(isinstance(p, str) and p.strip() and not p.startswith("/") and ".." not in p for p in paths),
                            f"{ident}: when_paths требует непустой список корректных относительных путей")
                elif app_kind == "checkpoint":
                    cp = app.get("checkpoint")
                    require(isinstance(cp, str) and cp.strip(),
                            f"{ident}: checkpoint требует непустую строку checkpoint")
            else:
                require(False, f"{ident}: applicability должен быть строкой или объектом")
    if "checkpoints" in settings:
        cps = settings["checkpoints"]
        require(isinstance(cps, dict), "task_workflow.checkpoints должен быть объектом")
        for cp_name, cp_checks in cps.items():
            require(isinstance(cp_name, str) and cp_name.strip(),
                    "Имя контрольной точки должно быть непустой строкой")
            require(isinstance(cp_checks, list) and all(isinstance(c, str) and c in registry_ids for c in cp_checks),
                    f"Контрольная точка '{cp_name}' ссылается на неизвестную проверку или не является списком")
    return settings


def validate_report(report, require):
    require(isinstance(report, dict), "Отчёт должен быть объектом")
    version = report.get("schema_version")
    require(version in {1, 2}, f"Неподдерживаемая schema_version отчёта: {version!r}")
    if version == 1:
        require("task_id" in report and "status" in report, "v1 отчёт требует task_id и status")
        return report
    # schema_version == 2
    unknown = sorted(set(report) - REPORT_V2_FIELDS)
    require(not unknown, f"Неизвестные поля в report v2: {unknown}")
    for req_field in REQUIRED_REPORT_V2_FIELDS:
        require(req_field in report, f"report v2: отсутствует обязательное поле '{req_field}'")
    require(_iso_dt(report["at"]) is not None, "report v2: поле 'at' должно быть ISO-временем с часовым поясом")
    if "started_at" in report and report["started_at"] is not None:
        require(_iso_dt(report["started_at"]) is not None, "report v2: started_at должен быть ISO-временем")
    if "finished_at" in report and report["finished_at"] is not None:
        require(_iso_dt(report["finished_at"]) is not None, "report v2: finished_at должен быть ISO-временем")
    require(isinstance(report["run_id"], str) and report["run_id"].strip(), "Некорректный run_id")
    require(isinstance(report["task_id"], str) and report["task_id"].strip(), "Некорректный task_id")
    route = report["route"]
    require(route in {"quick", "small", "medium", "large"}, f"Неизвестный route в отчёте: {route!r}")
    require(isinstance(report["plan_revision"], int) and report["plan_revision"] >= 1
            and not isinstance(report["plan_revision"], bool),
            "plan_revision должен быть целым числом >= 1")
    require(isinstance(report["plan_hash"], str) and report["plan_hash"].strip(), "Некорректный plan_hash")
    require(isinstance(report["policy_hash"], str) and report["policy_hash"].strip(), "Некорректный policy_hash")
    status = report["status"]
    require(status in {"passed", "failed", "stale", "running", "incomplete", "interrupted"},
            f"Неизвестный статус отчёта: {status!r}")
    results = report["results"]
    require(isinstance(results, list) and results, "results должен быть непустым списком")
    for idx, res in enumerate(results):
        require(isinstance(res, dict), f"results[{idx}] должен быть объектом")
        res_unknown = sorted(set(res) - RESULT_FIELDS)
        require(not res_unknown, f"Неизвестные поля в results[{idx}]: {res_unknown}")
        for rf in ("check_id", "definition_hash", "kind", "status"):
            require(rf in res, f"results[{idx}]: отсутствует поле '{rf}'")
        require(isinstance(res["check_id"], str) and res["check_id"].strip(), f"results[{idx}]: некорректный check_id")
        require(isinstance(res["definition_hash"], str) and res["definition_hash"].strip(),
                f"results[{idx}]: некорректный definition_hash")
        res_kind = res["kind"]
        require(res_kind in {"command", "manual"}, f"results[{idx}]: некорректный kind: {res_kind!r}")
        res_status = res["status"]
        require(res_status in {"passed", "failed", "not_run", "not_applicable", "timeout", "error", "skipped"},
                f"results[{idx}]: некорректный status: {res_status!r}")
        if res_status in {"passed", "failed", "timeout"}:
            for prov_fld in ("actor", "environment", "input_snapshot", "started_at", "finished_at"):
                require(prov_fld in res, f"results[{idx}] ({res['check_id']}): отсутствует поле происхождения '{prov_fld}'")
            actor = res["actor"]
            if isinstance(actor, str):
                require(bool(actor.strip()), f"results[{idx}] ({res['check_id']}): actor должен быть непустой строкой")
            elif isinstance(actor, dict) and not isinstance(actor, bool):
                name = actor.get("name")
                require(isinstance(name, str) and bool(name.strip()),
                        f"results[{idx}] ({res['check_id']}): actor.name должен быть непустой строкой")
                if "model" in actor:
                    model = actor["model"]
                    require(isinstance(model, str) and bool(model.strip()),
                            f"results[{idx}] ({res['check_id']}): actor.model должен быть непустой строкой")
            else:
                require(False, f"results[{idx}] ({res['check_id']}): некорректный actor")
            require(isinstance(res["environment"], dict) and not isinstance(res["environment"], bool),
                    f"results[{idx}] ({res['check_id']}): environment должен быть объектом")
            _validate_snapshot(res["input_snapshot"], f"results[{idx}] ({res['check_id']}).input_snapshot", require)
            require(_iso_dt(res["started_at"]) is not None,
                    f"results[{idx}] ({res['check_id']}): started_at должен быть ISO-временем с часовым поясом")
            require(_iso_dt(res["finished_at"]) is not None,
                    f"results[{idx}] ({res['check_id']}): finished_at должен быть ISO-временем с часовым поясом")
            dur = res.get("duration_seconds")
            if dur is not None:
                require(type(dur) in (int, float) and math.isfinite(dur) and dur >= 0,
                        f"results[{idx}] ({res['check_id']}): duration_seconds должен быть неотрицательным числом")
            if res_kind == "command":
                require(isinstance(res.get("exit_code"), int) and not isinstance(res.get("exit_code"), bool),
                        f"results[{idx}] ({res['check_id']}): exit_code должен быть целым числом")
                require(isinstance(res.get("stdout_log"), str) and res["stdout_log"].strip(),
                        f"results[{idx}] ({res['check_id']}): stdout_log обязателен для command-результата")
                require(isinstance(res.get("stderr_log"), str) and res["stderr_log"].strip(),
                        f"results[{idx}] ({res['check_id']}): stderr_log обязателен для command-результата")
                if res_status in {"passed", "failed"}:
                    require("snapshot_before" in res,
                            f"results[{idx}] ({res['check_id']}): snapshot_before обязателен для command-результата")
                    _validate_snapshot(res["snapshot_before"], f"results[{idx}] ({res['check_id']}).snapshot_before", require)
                    require("snapshot_after" in res,
                            f"results[{idx}] ({res['check_id']}): snapshot_after обязателен для command-результата")
                    _validate_snapshot(res["snapshot_after"], f"results[{idx}] ({res['check_id']}).snapshot_after", require)
            elif res_kind == "manual":
                require(isinstance(res.get("scenario_hash"), str) and res["scenario_hash"].strip(),
                        f"results[{idx}] ({res['check_id']}): scenario_hash обязателен для manual-результата")
                require(isinstance(res.get("observation"), str) and res["observation"].strip(),
                        f"results[{idx}] ({res['check_id']}): observation обязателен для manual-результата")
                if res_status in {"passed", "failed"}:
                    require("candidate_snapshot" in res,
                            f"results[{idx}] ({res['check_id']}): candidate_snapshot обязателен для manual-результата")
                    _validate_snapshot(res["candidate_snapshot"], f"results[{idx}] ({res['check_id']}).candidate_snapshot", require)
        elif res_status == "not_applicable":
            require(isinstance(res.get("reason"), str) and res["reason"].strip(),
                    f"results[{idx}] ({res['check_id']}): для not_applicable обязательно непустое поле 'reason'")
    coverage = report["coverage"]
    require(isinstance(coverage, dict), "coverage должен быть объектом")
    cov_unknown = sorted(set(coverage) - COVERAGE_FIELDS)
    require(not cov_unknown, f"Неизвестные поля в coverage: {cov_unknown}")
    for cf in ("total_checks", "passed", "failed", "not_applicable", "not_run", "satisfied"):
        require(cf in coverage, f"coverage: отсутствует поле '{cf}'")
    require(isinstance(coverage["total_checks"], int) and coverage["total_checks"] >= 0
            and not isinstance(coverage["total_checks"], bool),
            "coverage.total_checks должен быть целым неотрицательным числом")
    for list_fld in ("passed", "failed", "not_applicable", "not_run"):
        val = coverage[list_fld]
        require(isinstance(val, list) and all(isinstance(x, str) for x in val),
                f"coverage.{list_fld} должен быть списком строк")
    require(isinstance(coverage["satisfied"], bool), "coverage.satisfied должен быть boolean")
    return report


def load_report(path, require):
    """Прочитать report.json прогона verify; проверка схемы v1/v2."""
    require(path.is_file(), f"Нет отчёта прогона: {path}")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        require(False, f"Отчёт {path} не читается: {error}")
        return {}
    return validate_report(report, require)


def inputs_fresh(root, report, exclude, safe_path, require):
    """True, если текущие файлы совпадают со snapshot_after отчёта.

    ``exclude`` — относительные пути, исключаемые из сравнения (карточка
    задачи отдельно защищена проекцией). Сравнение по составу и хешам
    файлов: появление, исчезновение и изменение входов делают снимок
    устаревшим. Отсутствующий вход отчёта — тоже несвежесть.
    """
    inputs = report.get("inputs")
    snapshot_after = report.get('snapshot_after')
    after = snapshot_after.get('files') if isinstance(snapshot_after, dict) else None
    if not isinstance(inputs, list) or not isinstance(after, dict):
        return False, ["у отчёта нет snapshot_after/inputs"]
    try:
        current = snapshot(root, inputs, safe_path, require)["files"]
    except (ValueError, OSError, TypeError) as error:
        return False, [f"снимок входов не построен: {error}"]
    exclude = set(exclude)
    for rel in list(exclude):
        card = root / rel
        if card.suffix == '.md' and card.is_file() and documents.external_card(card.read_text(encoding='utf-8')):
            exclude.add(str(documents.meta_path(rel)))
    for rel in exclude:
        current.pop(rel, None)
        after = {key: value for key, value in after.items() if key != rel}
    diff = sorted(set(current) ^ set(after))
    diff += sorted(rel for rel in current
                   if rel in after and current[rel] != after[rel])
    return not diff, diff


def stop_group(process):
    # Also reap descendants which outlive their parent inside this session.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def runtime_identity():
    # Only a digest is stored; environment values may contain secrets.
    environment = {k: v for k, v in os.environ.items() if k != 'PLAYBOOK_GATE'}
    return {'python': sys.executable, 'version': sys.version,
            'platform': platform.platform(),
            'environment_sha256': hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()}


def runner_directory(path):
    """The private UUID namespace generated by run(), never an audit label."""
    return len(path.name) == 32 and all(c in '0123456789abcdef' for c in path.name)


def reusable_run(root, task, settings, policy_revision, inputs, plan, safe_path, require):
    """Conservative opt-in reuse of one complete legacy run; never per-check skipping."""
    directory = safe_path(root, f"work/verification/{task['id']}")
    reasons = []
    content = documents.read_text(task['path'])
    meta = task_cards.metadata(content) or {}
    audit_sources = [safe_path(root, m['source']) for m in meta.get('materials', [])
                     if m.get('storage') == 'local' and m.get('source')
                     and m.get('id') in meta.get('audit_packages', {})]
    projection = task_cards.card_projection(content)
    exec_id, exec_sha = task_cards.execution_state(content, str(task['path'].relative_to(root))) \
        if task_cards.version(content) == 2 else (None, None)
    candidates = [p for p in directory.glob('*/report.json')
                  if not any(p.parent == folder for folder in audit_sources)]
    def run_order(path):
        try:
            safe_path(root, str(path.relative_to(root)))
            value = load_report(path, require)
            started = _iso_dt(value.get('started_at'))
            require(started is not None, 'не зафиксировано время запуска')
            return started.timestamp(), path.stat().st_mtime_ns
        except (ValueError, OSError, TypeError, KeyError):
            # Corrupt history rejects older evidence, but a new real run made
            # after that corruption can become the latest trustworthy result.
            try:
                safe_path(root, str(path.relative_to(root)))
                return path.stat().st_mtime, path.stat().st_mtime_ns
            except (ValueError, OSError):
                return float('inf'), 0
    for path in sorted(candidates, key=run_order, reverse=True):
        try:
            safe_path(root, str(path.relative_to(root)))
            report = load_report(path, require)
            require(type(report.get('schema_version')) is int and report['schema_version'] == 1
                    and report.get('task_id') == task['id']
                    and report.get('cwd') == str(root) and report.get('run_id') == path.parent.name,
                    'чужой или неподдерживаемый отчёт')
            require(report.get('status') == 'passed' and report.get('inputs_unchanged') is True,
                    'прогон не passed/неизменный')
            require(_iso_dt(report.get('started_at')) is not None, 'не зафиксировано время запуска')
            require(isinstance(report.get('snapshot_before'), dict)
                    and report['snapshot_before'] == report.get('snapshot_after'),
                    'повреждены или различаются снимки до/после проверки')
            require(report.get('policy_revision') == policy_revision and report.get('inputs') == inputs,
                    'изменились policy или набор входов')
            require(report.get('runtime') == runtime_identity(), 'среда не совпадает или не зафиксирована')
            checks = report.get('checks')
            require(isinstance(checks, list) and len(checks) == len(plan), 'неполный набор проверок')
            require(all(isinstance(c, dict) and c.get('status') == 'passed'
                        and type(c.get('exit_code')) is int and c['exit_code'] == 0
                        and {k: c.get(k) for k in ('id', 'argv', 'timeout_seconds')} == expected
                        for c, expected in zip(checks, plan)), 'изменились команды или их результаты')
            require(report.get('card_projection') == projection
                    and report.get('execution_id') == exec_id
                    and report.get('execution_sha256') == exec_sha, 'изменились требования или EXEC')
            fresh, changed = inputs_fresh(root, report, {str(task['path'].relative_to(root))}, safe_path, require)
            require(fresh, 'изменились входы: ' + ', '.join(changed[:8]))
            return path, []
        except (ValueError, OSError, TypeError, KeyError) as error:
            reasons.append(f'{path.parent.name}: {error}')
            break  # A later failed/corrupt run must not be hidden by an older pass.
    return None, reasons or ['нет подходящего полного отчёта']


def run(root, task, settings, policy_revision, safe_path, require, dry_run=False,
        extra_env=None, reuse=False):
    inputs = sorted(set(settings["inputs"] + [
        "ai-dev-playbook/project.json", "ai-dev-playbook/resources.json", str(task["path"].relative_to(root))]))
    before = snapshot(root, inputs, safe_path, require)
    plan = [{"id": item["id"], "argv": item["argv"], "timeout_seconds": item["timeout_seconds"]}
            for item in settings["checks"]]
    if dry_run:
        print(json.dumps({"task_id": task["id"], "cwd": str(root), "inputs": inputs,
                          "checks": plan}, ensure_ascii=False, indent=2))
        return 0
    if reuse:
        previous, reasons = reusable_run(root, task, settings, policy_revision, inputs, plan, safe_path, require)
        if previous is not None:
            print(f'reused: {previous}; полный свежий результат, команды не запускались')
            return 0
        print('Новый прогон: ' + '; '.join(reasons[:3]))
    require(os.name == "posix", "verify пока поддерживает исполнение только на POSIX")
    run_id = uuid.uuid4().hex
    directory = safe_path(root, f"work/verification/{task['id']}/{run_id}")
    directory.mkdir(parents=True, exist_ok=False)
    task_content = documents.read_text(task["path"])
    card_proj = None
    exec_id = None
    exec_sha = None
    try:
        card_proj = task_cards.card_projection(task_content)
        exec_id, exec_sha = task_cards.execution_state(task_content, str(task["path"].relative_to(root)))
    except Exception:
        pass
    report = {"schema_version": 1, "run_id": run_id, "task_id": task["id"],
              "policy_revision": policy_revision, "cwd": str(root), "started_at": timestamp(),
              "status": "running", "inputs": inputs, "snapshot_before": before,
              "checks": [dict(item, status="pending") for item in plan],
              "card_projection": card_proj,
              "execution_id": exec_id,
              "execution_sha256": exec_sha,
              "runtime": runtime_identity()}

    def save():
        temporary = directory / "report.tmp"
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(directory / "report.json")

    save()
    failed = False
    interrupted = False
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    for result in report["checks"]:
        if failed:
            result["status"] = "skipped"
            continue
        result.update(status="running", started_at=timestamp())
        save()
        started = time.monotonic()
        process = None
        try:
            stdout = directory / (result["id"] + ".stdout.log")
            stderr = directory / (result["id"] + ".stderr.log")
            result.update(stdout=str(stdout.relative_to(root)), stderr=str(stderr.relative_to(root)))
            print(f"RUN: {result['id']}", flush=True)
            with stdout.open("xb") as out, stderr.open("xb") as err:
                process = subprocess.Popen(result["argv"], cwd=root, stdin=subprocess.DEVNULL,
                                           stdout=out, stderr=err, env=env,
                                           start_new_session=True)
                result["exit_code"] = process.wait(timeout=result["timeout_seconds"])
            result["status"] = "passed" if result["exit_code"] == 0 else "failed"
        except subprocess.TimeoutExpired:
            result["status"] = "timeout"
        except KeyboardInterrupt:
            result["status"] = "interrupted"
            interrupted = True
        except OSError as error:
            result.update(status="error", error=str(error))
        finally:
            if process is not None:
                stop_group(process)
                result.setdefault("exit_code", process.returncode)
            result["duration_seconds"] = round(time.monotonic() - started, 3)
            result["finished_at"] = timestamp()
        failed = result["status"] != "passed"
        save()
    try:
        after = snapshot(root, inputs, safe_path, require)
        report["snapshot_after"] = after
        report["inputs_unchanged"] = before == after
    except (OSError, ValueError) as error:
        report.update(inputs_unchanged=False, snapshot_error=str(error))
    report["status"] = ("interrupted" if interrupted else "failed" if failed else
                        "passed" if report["inputs_unchanged"] else "stale")
    report["finished_at"] = timestamp()
    save()
    print(f"{report['status']}: {directory / 'report.json'}")
    return 130 if interrupted else (0 if report["status"] == "passed" else 1)
