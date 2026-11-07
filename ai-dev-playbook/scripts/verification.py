"""Explicit local checks; results are evidence, not task acceptance."""

import datetime as dt
import hashlib
import json
import math
import os
import platform
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import task_cards
import documents
import copy
import verification_policy


def readiness(root, settings, safe_path, require, archive=None):
    """Cheap, read-only checks before any configured command is started."""
    errors = []
    if Path(tempfile.gettempdir()).resolve().is_relative_to(root.resolve()):
        errors.append('TMPDIR должен находиться вне проекта: временная копия внутри проекта может копировать саму себя')
    for item in settings['checks']:
        executable = item['argv'][0]
        if os.path.dirname(executable):
            path = executable if os.path.isabs(executable) else str(root / executable)
            available = os.path.isfile(path) and os.access(path, os.X_OK)
        else:
            available = shutil.which(executable) is not None
        if not available:
            errors.append(f"{item['id']}: исполняемый файл недоступен: {executable}")

    # Use only successful local runner measurements with the same argv. Audit
    # JSON and damaged history are not runner evidence and must not block repair.
    measurements = {}
    reports = list((root / 'work/verification').glob('*/*/report.json'))
    reports += list((archive or root / 'docs/archive/tasks').glob('*-evidence/**/report.json'))
    for path in reports:
        safe_path(root, path.relative_to(root).as_posix())
        try:
            report = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if (not isinstance(report, dict) or report.get('schema_version') != 1
                or report.get('cwd') != str(root) or report.get('status') != 'passed'
                or report.get('inputs_unchanged') is not True
                or not isinstance(report.get('checks'), list)):
            continue
        for check in report['checks']:
            if not isinstance(check, dict) or check.get('status') != 'passed':
                continue
            argv, duration = check.get('argv'), check.get('duration_seconds')
            if (not isinstance(argv, list) or not all(isinstance(a, str) for a in argv)
                    or type(duration) not in (int, float) or not math.isfinite(duration)
                    or duration <= 0):
                continue
            key = tuple(argv)
            if duration > measurements.get(key, (0, None))[0]:
                measurements[key] = duration, path.relative_to(root).as_posix()
    for item in settings['checks']:
        duration, source = measurements.get(tuple(item['argv']), (0, None))
        if item['timeout_seconds'] <= duration:
            errors.append(f"{item['id']}: timeout_seconds={item['timeout_seconds']} "
                          f"не превышает прежний успешный прогон {duration}s ({source}); "
                          f"увеличьте лимит с запасом (например, {math.ceil(duration * 1.25)}s)")
    require(not errors, 'preflight: ' + '\n'.join(errors))


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
                   "default_inputs", "applicability", "required_environment"}
REQUIRED_REPORT_V2_FIELDS = {"schema_version", "run_id", "task_id", "route", "plan_revision",
                             "plan_hash", "policy_hash", "at", "status", "results", "coverage"}
REPORT_V2_FIELDS = REQUIRED_REPORT_V2_FIELDS | {"started_at", "finished_at", "cwd",
                                                "card_projection", "execution_id", "execution_sha256"}
RESULT_FIELDS = {"check_id", "definition_hash", "kind", "status", "actor",
                 "environment", "input_snapshot", "started_at", "finished_at",
                 "duration_seconds", "exit_code", "stdout_log", "stderr_log",
                 "snapshot_before", "snapshot_after", "scenario_hash", "observation",
                 "candidate_snapshot", "engineer_confirmation", "materials",
                 "limitations", "reason", "policy_binding"}
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
        if "required_environment" in item:
            require(kind == "manual" and isinstance(item["required_environment"], list)
                    and all(isinstance(x, str) and x.strip() for x in item["required_environment"]),
                    f"{ident}: required_environment допустим только как список фактов manual-среды")
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
        if "policy_binding" in res:
            require(isinstance(res["policy_binding"], str) and res["policy_binding"].startswith("sha256:"),
                    f"results[{idx}]: некорректный policy_binding")
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
    if report.get("schema_version") == 2:
        changed_all = []
        exclude = set(exclude)
        for rel in list(exclude):
            card = root / rel
            if card.suffix == '.md' and card.is_file() and documents.external_card(card.read_text(encoding='utf-8')):
                exclude.add(str(documents.meta_path(rel)))
        for res in report.get("results", []):
            if res.get("status") not in {"passed", "not_applicable"}:
                continue
            inp_snap = res.get("input_snapshot")
            if not isinstance(inp_snap, dict) or not isinstance(inp_snap.get("files"), dict):
                continue
            for fpath, fhash in inp_snap["files"].items():
                if fpath.startswith("path:"):
                    rel_p = fpath[5:]
                    if rel_p in exclude:
                        continue
                    real_p = root / rel_p
                    if not real_p.exists():
                        val = {'type': 'absent'}
                    elif real_p.is_file():
                        val = {'type': 'file', 'sha256': 'sha256:' + hashlib.sha256(real_p.read_bytes()).hexdigest()}
                    elif real_p.is_dir():
                        val = {'type': 'directory', 'entries': {p.name: {'type': 'file'} for p in sorted(real_p.iterdir())}}
                    else:
                        val = {'type': 'special'}
                    cur_fp = verification_policy.fingerprint(val)
                    if cur_fp != fhash:
                        changed_all.append(rel_p)
        return not changed_all, changed_all

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


def is_v2_task(task_content, task_meta=None):
    if task_meta is None:
        errors = []
        task_meta = documents.parse_meta(task_content, 'card', errors) or {}
    return (task_meta.get("schema_version") == 2 or
            isinstance(task_meta.get("workflow"), dict) or
            isinstance(task_meta.get("verification_plan"), dict))


def runtime_environment(environment=None):
    return {
        "os": os.name,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "architecture": platform.machine(),
        "executable": sys.executable,
        "environment_sha256": verification_policy.fingerprint({
            k: v for k, v in (os.environ if environment is None else environment).items()
            if k != "PLAYBOOK_GATE"}),
    }


def _find_v2_reusable_results(root, task_id, plan, tw_settings, task_content, task_meta, safe_path, require):
    directory = safe_path(root, f"work/verification/{task_id}")
    if not directory.is_dir():
        return {}, set(), {}
    registry = {item["id"]: item for item in tw_settings["registry"]}
    env_facts = runtime_environment()
    task_proj = verification_policy.task_projection(task_content, task_meta)

    candidates = []
    for child in directory.iterdir():
        p = child / "report.json"
        if not child.is_dir() or child.is_symlink() or not p.is_file():
            continue
        try:
            safe_path(root, p.relative_to(root).as_posix())
            val = load_report(p, require)
            if (val.get("schema_version") == 2 and val.get("task_id") == task_id
                    and val.get("cwd") == str(root)
                    and val.get("run_id") == p.parent.name):
                started = _iso_dt(val.get("started_at")) or _iso_dt(val.get("at"))
                ts = started.timestamp() if started else p.stat().st_mtime
                candidates.append((ts, p, val))
            elif val.get('schema_version') == 2 and runner_directory(child):
                candidates.append((p.stat().st_mtime, p, {'_invalid': True}))
        except (ValueError, OSError, TypeError, KeyError):
            if runner_directory(child):
                candidates.append((p.stat().st_mtime, p, {'_invalid': True}))
    candidates.sort(key=lambda x: (x[0], str(x[1])), reverse=True)

    reusable_map = {}
    failed_history = set()
    reasons = {}

    for _, rep_path, rep in candidates:
        for res_item in rep.get("results", []):
            if res_item.get("status") in {"failed", "timeout", "error"}:
                cid = res_item.get("check_id")
                if cid:
                    failed_history.add(cid)

    for check in plan.get("checks", []):
        cid = check["id"]
        reg_id = check.get("registry_id")

        try:
            def_obj = verification_policy.definition(check, registry)
            def_hash = verification_policy.fingerprint(def_obj)
        except Exception as e:
            reasons[cid] = f"ошибка определения проверки: {e}"
            continue

        check_policy_proj = verification_policy.policy_projection(tw_settings, [reg_id] if reg_id else None)
        try:
            cur_snap = verification_policy.snapshot(root, check.get("inputs", []),
                                                    task=task_proj, policy=check_policy_proj,
                                                    environment=env_facts)
        except Exception as e:
            reasons[cid] = f"входы проверки недоступны: {e}"
            continue

        found = False
        for _, rep_path, rep in candidates:
            if rep.get('_invalid'):
                reasons[cid] = 'повреждённый более поздний отчёт; требуется заменяющий прогон'
                found = True
                break
            res_item = next((r for r in rep.get("results", []) if r.get("check_id") == cid), None)
            if res_item is None:
                continue

            if res_item.get("status") in {"failed", "timeout", "error"}:
                failed_history.add(cid)
                reasons[cid] = f"предыдущий запуск {rep.get('run_id')} завершился с ошибкой ({res_item.get('status')})"
                found = True
                break

            if res_item.get('kind') == 'command' and res_item.get('status') == 'passed':
                try:
                    for field in ('stdout_log', 'stderr_log'):
                        require(safe_path(root, res_item[field]).is_file(), 'Отсутствует лог результата')
                except (ValueError, OSError, KeyError, TypeError):
                    reasons[cid] = 'отсутствуют или повреждены логи результата'
                    found = True
                    break
            eval_res = dict(res_item, policy_hash=res_item.get("policy_binding"))
            relevant_hash = verification_policy.fingerprint(check_policy_proj)
            expected_env = res_item.get("environment") if res_item.get("kind") == "manual" else env_facts
            if verification_policy.reusable(eval_res, definition_hash=def_hash,
                                            input_snapshot=cur_snap,
                                            environment=expected_env,
                                            policy_hash=relevant_hash):
                reusable_map[cid] = (res_item, rep.get("run_id"))
                found = True
                break
            else:
                diff = verification_policy.compare_snapshots(res_item.get("input_snapshot"), cur_snap)
                if not diff["fresh"]:
                    clean_changed = [c[5:] if c.startswith("path:") else c for c in diff["changed"]]
                    reasons[cid] = f"изменились входы: {', '.join(clean_changed[:5])}"
                elif res_item.get("definition_hash") != def_hash:
                    reasons[cid] = "изменилось определение команды/проверки"
                elif res_item.get("policy_binding") != relevant_hash:
                    reasons[cid] = "изменилась политика проверок"
                elif res_item.get("status") != "passed":
                    reasons[cid] = f"статус в {rep.get('run_id')} был {res_item.get('status')}"
                else:
                    reasons[cid] = "не совпадают снимки выполнения или среда"
                found = True
                break
        if not found and cid not in reasons:
            reasons[cid] = "проверка ранее не выполнялась"

    return reusable_map, failed_history, reasons


def preflight_v2(root, task, tw_settings, policy_revision, safe_path, require,
                 checkpoint=None, print_output=True, task_index_path=None, expected_index=None):
    errors = []
    task_content = documents.read_text(task["path"])
    task_meta = task_cards.metadata(task_content) or {}

    if task["status"] not in {"ready", "in_progress", "in_review"}:
        errors.append(f"{task['id']}: статус '{task['status']}' не готов к проверке (требуется ready/in_progress/in_review)")

    if task_index_path is not None and expected_index is not None:
        if not task_index_path.is_file() or task_index_path.read_text(encoding="utf-8") != expected_index:
            errors.append("Индекс задач устарел. Способ исправления: выполните 'python3 ai-dev-playbook/scripts/playbook.py index'")

    plan = task_meta.get("verification_plan")
    if not isinstance(plan, dict):
        errors.append(f"{task['id']}: отсутствует verification_plan в метаданных задачи")
        return {"ready": False, "errors": errors, "checks": {}, "plan": {}}

    registry = {item["id"]: item for item in tw_settings["registry"]}

    for check in plan.get("checks", []):
        reg_id = check.get("registry_id")
        if reg_id and reg_id in registry:
            item = registry[reg_id]
            if item.get("kind") == "command":
                exe = item["argv"][0]
                if os.path.dirname(exe):
                    p = exe if os.path.isabs(exe) else str(root / exe)
                    avail = os.path.isfile(p) and os.access(p, os.X_OK)
                else:
                    avail = shutil.which(exe) is not None
                if not avail:
                    errors.append(f"{check['id']}: исполняемый файл недоступен: {exe}. Способ исправления: установите исполняемый файл или настройте окружение")

    reusable_map, failed_history, reasons = _find_v2_reusable_results(
        root, task["id"], plan, tw_settings, task_content, task_meta, safe_path, require
    )

    baseline = verification_policy.baseline_files(root, task_meta)
    project_paths = json.loads((root / 'ai-dev-playbook/project.json').read_text(encoding='utf-8'))['paths']
    current = verification_policy.applicability_files(root, task['path'].relative_to(root).as_posix(),
        task_content, task_meta, project_paths['task_index']) if baseline is not None else None

    req_eval = verification_policy.required_checks(
        tw_settings, plan, baseline=baseline, current=current, checkpoint=checkpoint,
        failed_history=failed_history
    )

    for err in req_eval.get("errors", []):
        if "Missing required registry check" in err:
            cid = err.split(":")[-1].strip()
            errors.append(f"Отсутствует обязательная проверка из реестра: {cid}. Способ исправления: добавьте проверку с registry_id '{cid}' в verification_plan.checks карточки")
        elif "Unapproved exclusion of failed check" in err:
            cid = err.split(":")[-1].strip()
            errors.append(f"Попытка исключить ранее проваленную проверку '{cid}' без решения инженера. Способ исправления: верните проверку в verification_plan или добавьте решение с engineer_approval в verification_plan.decisions")
        else:
            errors.append(f"{err}. Способ исправления: скорректируйте verification_plan карточки")

    check_details = {}
    for check in plan.get("checks", []):
        cid = check["id"]
        app = req_eval["checks"].get(cid, {"required": True, "reason": "default"})
        is_req = app["required"]
        app_reason = app["reason"]

        if not is_req:
            freshness = f"неприменима ({app_reason})"
        elif cid in reusable_map:
            res_item, run_source = reusable_map[cid]
            freshness = f"свежий (reused из {run_source})"
        else:
            freshness = reasons.get(cid, "not_run (требуется запуск)")

        check_details[cid] = {
            "kind": check.get("kind", "command"),
            "required": is_req,
            "applicability_reason": app_reason,
            "freshness": freshness,
            "reusable": cid in reusable_map,
        }

    ready = not errors
    if print_output:
        workflow = task_meta.get("workflow", {})
        print(f"Preflight v2 для задачи {task['id']}:")
        print(f"Маршрут: {workflow.get('route')}, ревизия плана: {plan.get('revision')}")
        print("Проверки:")
        for cid, d in sorted(check_details.items()):
            req_str = "обязательна" if d["required"] else "неприменима"
            print(f"- {cid} ({d['kind']}): {req_str} ({d['applicability_reason']}); актуальность: {d['freshness']}")
        if errors:
            print("\nОшибки preflight:")
            for e in errors:
                print(f"- {e}")
        else:
            print("\npreflight: структура, материалы, блокировки, команды и лимиты проверены; поведение CLI и приёмка не подтверждены.")

    return {
        "ready": ready,
        "errors": errors,
        "checks": check_details,
        "plan": plan,
        "reusable_map": {cid: r[0] for cid, r in reusable_map.items()},
        "required_checks": req_eval["checks"],
        "reasons": reasons,
    }


def record_manual_v2(root, task, tw_settings, manual_path, safe_path, require):
    p = safe_path(root, manual_path)
    require(p.is_file(), f"Нет файла ручной проверки: {manual_path}")
    try:
        statement = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        require(False, f"Ошибка чтения JSON из {manual_path}: {e}")
    require(isinstance(statement, dict), f"{manual_path}: ожидается JSON-объект")

    check_id = statement.get("check_id")
    require(isinstance(check_id, str) and check_id.strip(), "В заявлении отсутствует check_id")

    task_content = documents.read_text(task["path"])
    task_meta = task_cards.metadata(task_content) or {}
    plan = task_meta.get("verification_plan", {})
    checks = plan.get("checks", [])
    target_check = next((c for c in checks if c["id"] == check_id), None)
    require(target_check is not None, f"{task['id']}: проверка '{check_id}' не найдена в verification_plan")
    require(target_check.get("kind") == "manual", f"{check_id}: проверка не является ручной (kind != 'manual')")

    status = statement.get("status", "not_run")
    require(status in {"passed", "failed", "not_run"}, f"Некорректный status в заявлении: {status}")

    obs = statement.get("observation")
    require(isinstance(obs, str) and obs.strip(), "observation обязателен в заявлении")

    registry = {item["id"]: item for item in tw_settings["registry"]}
    reg_id = target_check.get("registry_id")
    scenario = target_check.get("scenario") or (registry.get(reg_id, {}).get("scenario") if reg_id else None)
    require(scenario, f"{check_id}: не задан сценарий ручной проверки")
    scenario_hash = verification_policy.fingerprint(scenario)

    env_facts = runtime_environment()
    if isinstance(statement.get("environment"), dict):
        env_facts.update(statement["environment"])

    task_proj = verification_policy.task_projection(task_content, task_meta)
    global_policy_proj = verification_policy.policy_projection(tw_settings)
    policy_hash = verification_policy.fingerprint(global_policy_proj)
    check_policy_proj = verification_policy.policy_projection(tw_settings, [reg_id] if reg_id else None)

    cur_snap = verification_policy.snapshot(root, target_check.get("inputs", []),
                                            task=task_proj, policy=check_policy_proj,
                                            environment=env_facts)

    def_hash = verification_policy.fingerprint(verification_policy.definition(target_check, registry))
    presentation = {"task_id": task["id"], "check_id": check_id,
                    "definition_hash": def_hash, "scenario_hash": scenario_hash,
                    "policy_hash": policy_hash}
    cand_snap = statement.get("candidate_snapshot")
    if status == "passed":
        require(isinstance(cand_snap, dict) and "files" in cand_snap,
                "Заявление со статусом passed требует candidate_snapshot, зафиксированный при предъявлении")
        presented_candidates = []
        ver_dir = root / f"work/verification/{task['id']}"
        if ver_dir.is_dir():
            for rep_file in ver_dir.glob("*/report.json"):
                try:
                    rep_data = json.loads(rep_file.read_text(encoding="utf-8"))
                    validate_report(rep_data, require)
                    if rep_data.get("task_id") != task["id"] or rep_data.get("policy_hash") != policy_hash:
                        continue
                    for r in rep_data.get("results", []):
                        if (r.get("check_id") == check_id and r.get("kind") == "manual"
                                and r.get("definition_hash") == def_hash
                                and r.get("scenario_hash") == scenario_hash
                                and isinstance(r.get("candidate_snapshot"), dict)
                                and r["candidate_snapshot"].get("sha256") == verification_policy.fingerprint(presentation)):
                            presented_candidates.append(r["candidate_snapshot"])
                except Exception:
                    pass
        require(bool(presented_candidates),
                f"{check_id}: ручная проверка ещё не предъявлялась через verify; отсутствует исходный candidate_snapshot")
        matched_any = any(
            cand_snap == pc
            for pc in presented_candidates
        )
        require(matched_any, "candidate_snapshot не совпадает ни с одним ранее предъявленным снимком проверки")
        diff = verification_policy.compare_snapshots(cand_snap, cur_snap)
        require(diff["fresh"], "Файлы изменились после фиксации candidate_snapshot; требуется новый просмотр")
    else:
        if cand_snap is not None:
            require(isinstance(cand_snap, dict) and "files" in cand_snap, "candidate_snapshot должен содержать files")
        else:
            cand_snap = cur_snap

    if status == "passed":
        required_environment = registry.get(reg_id, {}).get("required_environment", ["browser"])
        supplied_environment = statement.get("environment", {})
        has_browser = isinstance(supplied_environment, dict) and all(
            bool(supplied_environment.get(fact)) for fact in required_environment)
        has_confirmation = isinstance(statement.get("engineer_confirmation"), dict) and bool(statement["engineer_confirmation"].get("who"))
        if not (has_browser and has_confirmation):
            status = "not_run"
            print("Нет браузера или подтверждения инженера: статус зафиксирован как 'not_run', а не 'passed'.")

    actor = statement.get("actor") or task.get("executor") or "Инженер"
    def_hash = verification_policy.fingerprint(verification_policy.definition(target_check, registry))

    res = {
        "check_id": check_id,
        "definition_hash": def_hash,
        "kind": "manual",
        "scenario_hash": scenario_hash,
        "status": status,
        "actor": actor,
        "environment": env_facts,
        "policy_binding": verification_policy.fingerprint(check_policy_proj),
        "input_snapshot": cur_snap,
        "candidate_snapshot": cand_snap,
        "observation": obs,
        "started_at": timestamp(),
        "finished_at": timestamp(),
        "duration_seconds": 0.0,
    }
    if statement.get("engineer_confirmation"):
        res["engineer_confirmation"] = statement["engineer_confirmation"]

    run_id = uuid.uuid4().hex
    directory = safe_path(root, f"work/verification/{task['id']}/{run_id}")
    directory.mkdir(parents=True, exist_ok=False)

    workflow = task_meta.get("workflow", {})
    plan_hash = verification_policy.fingerprint(plan)

    report = {
        "schema_version": 2,
        "run_id": run_id,
        "task_id": task["id"],
        "route": workflow.get("route", "quick"),
        "plan_revision": plan.get("revision", 1),
        "plan_hash": plan_hash,
        "policy_hash": policy_hash,
        "at": timestamp(),
        "started_at": timestamp(),
        "finished_at": timestamp(),
        "cwd": str(root),
        "status": "passed" if status == "passed" else "incomplete",
        "results": [res],
        "coverage": {
            "total_checks": 1,
            "passed": [check_id] if status == "passed" else [],
            "failed": [check_id] if status == "failed" else [],
            "not_applicable": [],
            "not_run": [check_id] if status == "not_run" else [],
            "satisfied": status == "passed",
        }
    }

    validate_report(report, require)
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Ручная проверка {check_id} зарегистрирована со статусом {status}: {directory / 'report.json'}")
    return 0 if status == "passed" else 1


def run_v2(root, task, tw_settings, policy_revision, safe_path, require,
           dry_run=False, extra_env=None, reuse=True, checkpoint=None):
    task_content = documents.read_text(task["path"])
    task_meta = task_cards.metadata(task_content) or {}
    plan = task_meta.get("verification_plan", {})
    registry = {item["id"]: item for item in tw_settings["registry"]}

    pre = preflight_v2(root, task, tw_settings, policy_revision, safe_path, require,
                       checkpoint=checkpoint, print_output=dry_run)
    require(pre["ready"], "Ошибки preflight перед запуском:\n" + "\n".join(pre["errors"]))
    if dry_run:
        return 0

    run_id = uuid.uuid4().hex
    directory = safe_path(root, f"work/verification/{task['id']}/{run_id}")
    directory.mkdir(parents=True, exist_ok=False)

    workflow = task_meta.get("workflow", {})
    route = workflow.get("route", "quick")
    plan_rev = plan.get("revision", 1)
    plan_hash = verification_policy.fingerprint(plan)
    policy_proj = verification_policy.policy_projection(tw_settings)
    policy_hash = verification_policy.fingerprint(policy_proj)

    env_facts = runtime_environment()
    task_proj = verification_policy.task_projection(task_content, task_meta)

    results = []
    reusable_map = pre.get("reusable_map", {}) if reuse else {}
    req_checks = pre.get("required_checks", {})
    reasons = pre.get("reasons", {})

    interrupted = False
    stale_run = False
    failed_any = False

    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    env_facts = runtime_environment(env)
    if env_facts != runtime_environment():
        reusable_map = {}

    actor_str = task.get("executor") or "cli"
    model_str = task.get("model")
    actor_val = {"name": actor_str, "model": model_str} if model_str and model_str != "-" else actor_str

    for check in plan.get("checks", []):
        cid = check["id"]
        reg_id = check.get("registry_id")
        reg_item = registry.get(reg_id) if reg_id else None
        def_hash = verification_policy.fingerprint(verification_policy.definition(check, registry))
        app = req_checks.get(cid, {"required": True, "reason": "always"})

        if not app["required"]:
            results.append({
                "check_id": cid,
                "definition_hash": def_hash,
                "kind": check.get("kind", "command"),
                "status": "not_applicable",
                "reason": app["reason"],
            })
            continue

        if reuse and cid in reusable_map:
            res_item = copy.deepcopy(reusable_map[cid])
            print(f"REUSE: {cid}: свежий результат переиспользован")
            results.append(res_item)
            continue

        reason_why = reasons.get(cid, "требуется новый запуск")
        print(f"RUN: {cid} ({reason_why})", flush=True)

        if check.get("kind") == "command":
            require(reg_item is not None, f"{cid}: отсутствует запись в реестре для команды")
            argv = reg_item["argv"]
            timeout = reg_item["timeout_seconds"]

            check_policy_proj = verification_policy.policy_projection(tw_settings, [reg_id])
            snap_before = verification_policy.snapshot(root, check.get("inputs", []),
                                                       task=task_proj, policy=check_policy_proj,
                                                       environment=env_facts)
            started_at = timestamp()
            started_mono = time.monotonic()

            stdout_path = directory / f"{cid}.stdout.log"
            stderr_path = directory / f"{cid}.stderr.log"

            process = None
            exit_code = None
            status = "failed"
            try:
                with stdout_path.open("xb") as out, stderr_path.open("xb") as err:
                    process = subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL,
                                               stdout=out, stderr=err, env=env,
                                               start_new_session=True)
                    exit_code = process.wait(timeout=timeout)
                status = "passed" if exit_code == 0 else "failed"
            except subprocess.TimeoutExpired:
                status = "timeout"
            except KeyboardInterrupt:
                status = "error"
                interrupted = True
            except OSError as error:
                status = "error"
            finally:
                if process is not None:
                    stop_group(process)
                    if exit_code is None:
                        exit_code = process.returncode if process.returncode is not None else -1
                duration = round(time.monotonic() - started_mono, 3)
                finished_at = timestamp()

            snap_after = verification_policy.snapshot(root, check.get("inputs", []),
                                                      task=task_proj, policy=check_policy_proj,
                                                      environment=env_facts)
            diff = verification_policy.compare_snapshots(snap_before, snap_after)
            if not diff["fresh"] or runtime_environment(env) != env_facts:
                stale_run = True
                status = "failed"
                print(f"ПРЕДУПРЕЖДЕНИЕ: входы {cid} изменились во время прогона: {', '.join(diff['changed'][:5])}")

            if status != "passed":
                failed_any = True

            res = {
                "check_id": cid,
                "definition_hash": def_hash,
                "kind": "command",
                "status": status,
                "actor": actor_val,
                "environment": env_facts,
                "policy_binding": verification_policy.fingerprint(check_policy_proj),
                "input_snapshot": snap_before,
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_seconds": duration,
                "exit_code": exit_code if exit_code is not None else 1,
                "stdout_log": str(stdout_path.relative_to(root)),
                "stderr_log": str(stderr_path.relative_to(root)),
                "snapshot_before": snap_before,
                "snapshot_after": snap_after,
            }
            results.append(res)

        elif check.get("kind") == "manual":
            scenario = check.get("scenario") or (reg_item.get("scenario") if reg_item else None)
            scenario_hash = verification_policy.fingerprint(scenario)
            check_policy_proj = verification_policy.policy_projection(tw_settings, [reg_id] if reg_id else None)
            cand_snap = verification_policy.snapshot(root, check.get("inputs", []),
                                                     task=task_proj, policy=check_policy_proj,
                                                     environment=env_facts)
            cand_snap["sha256"] = verification_policy.fingerprint({
                "task_id": task["id"], "check_id": cid,
                "definition_hash": def_hash, "scenario_hash": scenario_hash,
                "policy_hash": policy_hash,
            })
            print(f"MANUAL: {cid}: зафиксирован candidate_snapshot; статус: not_run (ожидается подтверждение)")
            res = {
                "check_id": cid,
                "definition_hash": def_hash,
                "kind": "manual",
                "scenario_hash": scenario_hash,
                "status": "not_run",
                "actor": actor_val,
                "environment": env_facts,
                "policy_binding": verification_policy.fingerprint(check_policy_proj),
                "input_snapshot": cand_snap,
                "candidate_snapshot": cand_snap,
                "observation": "Ожидается ручная проверка и подтверждение инженера",
                "started_at": timestamp(),
                "finished_at": timestamp(),
                "duration_seconds": 0.0,
            }
            results.append(res)

    cur_task_content = documents.read_text(task["path"])
    cur_task_meta = task_cards.metadata(cur_task_content) or {}
    cur_task_proj = verification_policy.task_projection(cur_task_content, cur_task_meta)

    for res in results:
        if res.get("status") == "passed":
            cid = res.get("check_id")
            chk = next((c for c in plan.get("checks", []) if c["id"] == cid), None)
            if chk is None:
                continue
            reg_id = chk.get("registry_id")
            chk_policy = verification_policy.policy_projection(tw_settings, [reg_id] if reg_id else None)
            try:
                cur_input_snap = verification_policy.snapshot(root, chk.get("inputs", []),
                                                              task=cur_task_proj, policy=chk_policy,
                                                              environment=env_facts)
                diff = verification_policy.compare_snapshots(res.get("input_snapshot"), cur_input_snap)
                fresh = diff["fresh"] and (res.get("kind") != "command" or res.get("environment") == runtime_environment(env))
                changed_desc = ', '.join([c[5:] if c.startswith("path:") else c for c in diff["changed"]][:5])
            except Exception as e:
                fresh = False
                changed_desc = str(e)
                cur_input_snap = None

            if not fresh:
                stale_run = True
                res["status"] = "failed"
                if res.get("kind") == "command" and cur_input_snap is not None:
                    res["snapshot_after"] = cur_input_snap
                print(f"ПРЕДУПРЕЖДЕНИЕ: входы проверки {cid} изменились во время прогона: {changed_desc}")

    cov = verification_policy.coverage(req_checks, {r["check_id"]: r for r in results})
    coverage_data = {
        "total_checks": len(plan.get("checks", [])),
        "passed": [cid for cid, s in cov["checks"].items() if s == "passed"],
        "failed": [cid for cid, s in cov["checks"].items() if s in {"failed", "timeout", "error"}],
        "not_applicable": [cid for cid, s in cov["checks"].items() if s == "not_applicable"],
        "not_run": [cid for cid, s in cov["checks"].items() if s == "not_run"],
        "satisfied": cov["satisfied"],
    }

    if interrupted:
        report_status = "interrupted"
    elif stale_run:
        report_status = "stale"
    elif failed_any or coverage_data["failed"]:
        report_status = "failed"
    elif coverage_data["satisfied"]:
        report_status = "passed"
    else:
        report_status = "incomplete"

    report = {
        "schema_version": 2,
        "run_id": run_id,
        "task_id": task["id"],
        "route": route,
        "plan_revision": plan_rev,
        "plan_hash": plan_hash,
        "policy_hash": policy_hash,
        "at": timestamp(),
        "started_at": timestamp(),
        "finished_at": timestamp(),
        "cwd": str(root),
        "status": report_status,
        "results": results,
        "coverage": coverage_data,
    }

    validate_report(report, require)
    report_file = directory / "report.json"
    temp_file = directory / "report.tmp"
    temp_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_file.replace(report_file)

    print(f"{report_status}: {report_file}")
    return 130 if interrupted else (0 if report_status == "passed" else 1)
