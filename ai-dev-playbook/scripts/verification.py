"""Explicit local checks; results are evidence, not task acceptance."""

import datetime as dt
import hashlib
import json
import math
import os
import signal
import subprocess
import time
import uuid


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
            files[relative.as_posix()] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    payload = json.dumps(files, sort_keys=True).encode()
    return {"sha256": hashlib.sha256(payload).hexdigest(), "files": files}


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def stop_group(process):
    # Also reap descendants which outlive their parent inside this session.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run(root, task, settings, policy_revision, safe_path, require, dry_run=False):
    inputs = sorted(set(settings["inputs"] + [
        "ai-dev-playbook/project.json", "ai-dev-playbook/resources.json", str(task["path"].relative_to(root))]))
    before = snapshot(root, inputs, safe_path, require)
    plan = [{"id": item["id"], "argv": item["argv"], "timeout_seconds": item["timeout_seconds"]}
            for item in settings["checks"]]
    if dry_run:
        print(json.dumps({"task_id": task["id"], "cwd": str(root), "inputs": inputs,
                          "checks": plan}, ensure_ascii=False, indent=2))
        return 0
    require(os.name == "posix", "verify пока поддерживает исполнение только на POSIX")
    run_id = uuid.uuid4().hex
    directory = safe_path(root, f"work/verification/{task['id']}/{run_id}")
    directory.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 1, "run_id": run_id, "task_id": task["id"],
              "policy_revision": policy_revision, "cwd": str(root), "started_at": timestamp(),
              "status": "running", "inputs": inputs, "snapshot_before": before,
              "checks": [dict(item, status="pending") for item in plan]}

    def save():
        temporary = directory / "report.tmp"
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(directory / "report.json")

    save()
    failed = False
    interrupted = False
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
                                           stdout=out, stderr=err, start_new_session=True)
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
