"""Воспроизводимый self-pilot PB-064 на изолированных временных копиях без изменения исходного проекта.

Использование:
    python3 docs/tasks/PB-064-evidence/self_pilot.py <SOURCE_DIR> <OUTPUT_DIR> [--plan-only]

Сравнивает одинаковый трёхшаговый сценарий на двух независимых копиях репозитория
вне дерева проекта (без .git и work/):
  - Copy A (legacy): штатный `playbook.py verify PB-064` / `verify PB-064 --reuse`;
  - Copy B (opt-in): `setup preview` -> одобренная `checks-policy.json` (с сохранением
    всех обязательных проверок и 100% из 1082 unittest ID) -> `setup apply` ->
    `checks run` (initial / localized edit / repeat+release-ready) ->
    `lifecycle handoff` -> `verify --audit-ready` -> `close --dry-run`;
  - Отдельно проверяет границу `local` vs `protected` через `verifier init/approve/probe/run/verify/gate`.
"""
import argparse
import fnmatch
import hashlib
from collections import Counter
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


LOCALIZED_TARGET = "tests/test_checks_setup.py"
LOCALIZED_APPEND = "\n# PB-064 self-pilot localized scenario marker\nPILOT_SCENARIO_MARKER = 1\n"
TRANSITIVE_TARGET = "ai-dev-playbook/scripts/checks_setup.py"
TRANSITIVE_APPEND = "\n# PB-064 self-pilot transitive AST scenario marker\nPILOT_AST_MARKER = 1\n"


def tree_fingerprint(root: Path) -> dict:
    """SHA-256 всех отслеживаемых файлов дерева (кроме .git, work, __pycache__, .DS_Store)."""
    digest_map = {}
    for name in ("AGENTS.md", "README.md", "CHANGELOG.md", "ai-dev-playbook", "docs", "tests"):
        base = root / name
        if not base.exists():
            continue
        items = [base] if base.is_file() else sorted(base.rglob("*"))
        for path in items:
            if not path.is_file() or path.is_symlink():
                continue
            if "__pycache__" in path.parts or ".pytest_cache" in path.parts:
                continue
            if path.name == ".DS_Store" or path.name.endswith(".pyc"):
                continue
            rel = path.relative_to(root).as_posix()
            digest_map[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest_map


def copy_clean_repo(source: Path, dest: Path) -> None:
    """Скопировать репозиторий без .git, истории операций work/lifecycle и кешей среды."""
    src_str = str(source.resolve())

    def ignore(cur_src, names):
        rel_dir = Path(cur_src).resolve().relative_to(src_str).as_posix() if Path(cur_src).resolve() != Path(src_str) else ""
        skipped = set()
        for item in names:
            if item in {".git", "__pycache__", ".pytest_cache", ".DS_Store", " templates"}:
                skipped.add(item)
            elif item.endswith(".pyc"):
                skipped.add(item)
            elif rel_dir == "work" and item in {"lifecycle", "update", "checks", "gate.lock"}:
                skipped.add(item)
            elif rel_dir == "work/verification" and item == "PB-064":
                skipped.add(item)
        return skipped

    shutil.copytree(source, dest, ignore=ignore)


def prepare_pb064_card(copy_root: Path, register_self_pilot: bool) -> None:
    """Подготовить карточку PB-064 на копии для воспроизводимого прогона."""
    card = copy_root / "docs" / "tasks" / "PB-064.md"
    text = card.read_text(encoding="utf-8")
    if "### EXEC-01 — Выполнение" not in text:
        text = (
            text.rstrip()
            + "\n\n### EXEC-01 — Выполнение\n"
            "Время: 2026-11-14T15:00:00+03:00\n"
            "Исполнитель: Devin\n"
            "Модель: SWE-2 Flash High\n"
            "Изменения: подготовка изолированной копии self-pilot PB-064.\n"
            "Файлы:\n- docs/tasks/PB-064-evidence/self_pilot.py\n"
        )
        card.write_text(text, encoding="utf-8")
    evidence_dir = copy_root / "docs" / "tasks" / "PB-064-evidence"
    evidence_rel = "docs/tasks/PB-064-evidence"
    if register_self_pilot and evidence_dir.is_dir():
        meta = json.loads((copy_root / "docs" / "tasks" / "PB-064.meta.json").read_text(encoding="utf-8"))
        covered = any(
            m.get("source") == evidence_rel
            or str(m.get("source", "")).startswith(evidence_rel + "/")
            for m in meta.get("materials", [])
        )
        if not covered:
            subprocess.run(
                [sys.executable, "ai-dev-playbook/scripts/playbook.py",
                 "lifecycle", "register-material", "PB-064", evidence_rel,
                 "--id", "evidence", "--role", "report",
                 "--immutable", "--required", "--apply"],
                cwd=copy_root, check=True, capture_output=True, text=True,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            )
    subprocess.run(
        [sys.executable, "ai-dev-playbook/scripts/playbook.py", "index"],
        cwd=copy_root, check=True, capture_output=True, text=True,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
    )


def _purge_foreign_modules():
    """Убрать из sys.modules модули, импортированные из прежних копий.

    unittest discovery и прямые импорты checks_* кешируются по имени:
    вторая копия с теми же именами модулей иначе вызывает ImportError
    или молча использует чужой код.
    """
    stale = []
    for name, mod in list(sys.modules.items()):
        if name.startswith("test"):
            stale.append(name)
            continue
        origin = getattr(mod, "__file__", None)
        if not origin:
            continue
        parts = set(Path(origin).parts)
        if "ai-dev-playbook" in parts and "scripts" in parts:
            stale.append(name)
    for name in stale:
        sys.modules.pop(name, None)


def discover_ids(tests_dir: Path, pattern: str) -> list:
    """Собрать стабильные unittest IDs для заданного шаблона."""
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(tests_dir), pattern=pattern, top_level_dir=str(tests_dir))
    found = []

    def flatten(item):
        for sub in item:
            if isinstance(sub, unittest.TestSuite):
                flatten(sub)
            else:
                found.append(sub.id())

    flatten(suite)
    return found


def build_self_policy(copy_root: Path) -> tuple:
    """Построить одобренную checks-policy.json из survey с сохранением всех 1082 тестов."""
    _purge_foreign_modules()
    scripts_dir = copy_root / "ai-dev-playbook" / "scripts"
    sys.path.insert(0, str(scripts_dir))
    import checks_contract
    import checks_setup
    import verification_policy

    survey = checks_setup.survey(copy_root)
    tests_dir = copy_root / "tests"
    all_ids = discover_ids(tests_dir, "test*.py")
    suites = [
        ("block-unit-tests", "test_block_*.py", ["block-code", "block-tests"], 300,
         "Модульные и сценарные проверки совместной приёмки блока"),
        ("checks-unit-tests", "test_checks_*.py", ["checks-code", "checks-tests"], 600,
         "Проверки контракта, снимков, графа, runner, доверия и сквозных сценариев PB-057–PB-064"),
        ("cli-unit-tests", "test_c[!h]*.py", ["core-code", "cli-tests"], 900,
         "Проверки основного CLI, закрытия карточек, компактного контекста и чекбоксов"),
        ("core-unit-tests", "test_[ad-uw-z]*.py", ["core-code", "core-tests"], 1800,
         "Проверки файловых операций, документов, ссылок, обновлений и workflow v2"),
        ("verification-unit-tests", "test_verification_*.py", ["core-code", "core-tests"], 900,
         "Verification-policy/preflight/readiness/v2 и smoke-набор load_tests"),
    ]
    static_desc = {
        entry["id"]: entry["description"]
        for entry in survey["catalog_entries"]
    }
    catalog_entries = []
    partition_ids = []
    seen_ids = set()
    for check_id, pat, _mods, _timeout, _purpose in suites:
        group_ids = discover_ids(tests_dir, pat)
        partition_ids.extend(group_ids)
        for tid in group_ids:
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            desc = static_desc.get(tid) or f"Ранний регрессионный сценарий {tid}."
            catalog_entries.append({
                "id": tid,
                "group": check_id,
                "description": desc,
            })

    assert set(partition_ids) == set(all_ids), "Разбиение потеряло или добавило unittest ID"
    id_counts = Counter(partition_ids)
    borrowed_ids = sorted(t for t, n in id_counts.items() if n > 1)

    basis = {
        "who": "Инженер (изолированный self-pilot PB-064)",
        "at": "2026-11-14T15:00:00+03:00",
        "source": "PB-064 AC-02 disposable copy opt-in",
        "detail": "Явный opt-in только на временной копии для измерения стоимости против legacy verify",
    }
    policy = {
        "schema_version": 1,
        "policy_id": "ai-dev-playbook-self-pilot",
        "revision": 1,
        "project_id": "ai-dev-playbook",
        "trust_mode": "local",
        "approved_basis": basis,
        "subsystems": [
            {"id": "core", "title": "Ядро комплекта и CLI"},
            {"id": "checks", "title": "Выбор проверок по зависимостям"},
            {"id": "block", "title": "Приёмка блоков"},
            {"id": "docs", "title": "Проектная документация"},
        ],
        "modules": [
            {
                "id": "checks-code",
                "title": "Модули выбора проверок",
                "subsystem_id": "checks",
                "shared": True,
                "paths": ["ai-dev-playbook/scripts/checks_*.py"],
            },
            {
                "id": "block-code",
                "title": "Модуль приёмки блоков",
                "subsystem_id": "block",
                "shared": True,
                "paths": ["ai-dev-playbook/scripts/block_workflow.py"],
            },
            {
                "id": "core-code",
                "title": "Ядро, правила и шаблоны комплекта",
                "subsystem_id": "core",
                "shared": True,
                "paths": [
                    "ai-dev-playbook/scripts/[!cb]*.py",
                    "ai-dev-playbook/rules/**",
                    "ai-dev-playbook/templates/**",
                    "ai-dev-playbook/VERSION",
                    "ai-dev-playbook/README.md",
                    "ai-dev-playbook/manifest.json",
                    "ai-dev-playbook/updates.json",
                    "ai-dev-playbook/project.json",
                    "ai-dev-playbook/resources.json",
                    "AGENTS.md",
                    "README.md",
                    "CHANGELOG.md",
                    ".gitignore",
                    "checks-policy.json",
                ],
            },
            {
                "id": "checks-tests",
                "title": "Тесты выбора проверок",
                "subsystem_id": "checks",
                "shared": True,
                "paths": [
                    "tests/test_checks_*.py",
                    "tests/fixtures/checks_contract/**",
                    "tests/fixtures/checks_inventory/**",
                ],
            },
            {
                "id": "block-tests",
                "title": "Тесты приёмки блоков",
                "subsystem_id": "block",
                "shared": True,
                "paths": ["tests/test_block_*.py"],
            },
            {
                "id": "cli-tests",
                "title": "Тесты CLI и карточек",
                "subsystem_id": "core",
                "shared": True,
                "paths": ["tests/test_c[!h]*.py"],
            },
            {
                "id": "core-tests",
                "title": "Тесты ядра, документов и workflow",
                "subsystem_id": "core",
                "shared": True,
                "paths": [
                    "tests/test_[ad-z]*.py",
                    "tests/fixtures/release-0.0.2/**",
                    "tests/fixtures/task_workflow_v2/**",
                ],
            },
            {
                "id": "docs-authored",
                "title": "Спецификации, дизайны, ADR, планы и руководство",
                "subsystem_id": "docs",
                "shared": True,
                "paths": [
                    "docs/specs/**",
                    "docs/designs/**",
                    "docs/adr/**",
                    "docs/plans/**",
                    "docs/discussions/**",
                    "docs/user-guide/**",
                    "docs/architecture.md",
                    "docs/code-map.md",
                    "docs/index.md",
                    "docs/product.md",
                    "docs/roadmap.md",
                    "docs/update-plan.md",
                ],
            },
            {
                "id": "docs-tasks",
                "title": "Карточки задач и архив доказательств",
                "subsystem_id": "docs",
                "shared": True,
                "paths": ["docs/tasks/**", "docs/archive/**"],
            },
        ],
        "checks": [
            {
                "id": "self-check",
                "title": "Структурная проверка проекта",
                "purpose": "Сохранение обязательной проверки check --ready",
                "kind": "command",
                "modules": ["core-code", "checks-code", "block-code", "docs-authored"],
                "inputs": [{"type": "task_projection"}, {"type": "policy_projection"}],
                "argv": [sys.executable, "ai-dev-playbook/scripts/playbook.py", "check", "--ready"],
                "timeout_seconds": 60,
            },
            {
                "id": "preflight-regressions",
                "title": "Быстрые регрессии версий и ссылок",
                "purpose": "Сохранение обязательного набора test_verification_smoke.py",
                "kind": "command",
                "modules": ["core-code"],
                "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests",
                         "-p", "test_verification_smoke.py", "-v"],
                "timeout_seconds": 120,
            },
        ] + [
            {
                "id": check_id,
                "title": f"Набор {check_id}",
                "purpose": purpose,
                "kind": "test",
                "modules": mods,
                "adapter": "python-unittest",
                "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests",
                         "-p", pat, "-v"],
                "timeout_seconds": timeout,
            }
            for check_id, pat, mods, timeout, purpose in suites
        ],
        "dependencies": [],
        "excludes": [
            {
                "pattern": ".github/**",
                "reason": "Конфигурация GitHub Actions обследована отдельно (origin=survey)",
                "origin": "survey",
            },
        ],
        "environment": {
            "toolchain": [{"id": "python", "version": platform.python_version()}],
            "external": [],
        },
        "fallback": {
            "expansion_order": ["module", "subsystem", "project"],
            "approved_commands": [
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            ],
            "limits": {"max_seconds": 7200, "max_bytes": 250_000_000},
            "unknown_scope": "project",
        },
        "test_catalog": {
            "adapter": "python-unittest",
            "fingerprint": verification_policy.fingerprint(catalog_entries),
            "entries": catalog_entries,
        },
    }
    checks_contract.validate_policy(
        policy,
        lambda cond, msg: (_ for _ in ()).throw(ValueError(msg)) if not cond else None,
    )
    return policy, {
        "total_unittest_ids": len(all_ids),
        "unique_test_ids": len(set(all_ids)),
        "load_tests_shared_ids": borrowed_ids,
        "suites": {cid: len(discover_ids(tests_dir, pat)) for cid, pat, *_ in suites},
    }


def legacy_report_metrics(copy_root: Path, report_path: Path) -> dict:
    """Подсчитать команды и реально прохешированные файлы/байты отчёта legacy verify."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    before_files = report.get("snapshot_before", {}).get("files", {})
    after_files = report.get("snapshot_after", {}).get("files", {})
    before_bytes = sum((copy_root / rel).stat().st_size for rel in before_files if (copy_root / rel).is_file())
    after_bytes = sum((copy_root / rel).stat().st_size for rel in after_files if (copy_root / rel).is_file())
    executed = [c for c in report.get("checks", []) if c.get("status") in {"passed", "failed", "timeout"}]
    return {
        "run_id": report["run_id"],
        "status": report["status"],
        "commands_executed": len(executed),
        "command_ids": [c["id"] for c in executed],
        "unique_input_files": len(before_files),
        "hashed_files_total": len(before_files) + len(after_files),
        "hashed_bytes_total": before_bytes + after_bytes,
        "check_durations_seconds": {c["id"]: c.get("duration_seconds") for c in executed},
    }


PLAN_COST_RE = re.compile(r"прочитано (\d+) Б / (\d+) файлов")


def parse_plan_cost(stdout: str) -> dict:
    match = PLAN_COST_RE.search(stdout)
    if not match:
        return {"plan_hashed_bytes": None, "plan_hashed_files": None}
    return {
        "plan_hashed_bytes": int(match.group(1)),
        "plan_hashed_files": int(match.group(2)),
    }


def optin_snapshot_cost(copy_root: Path, policy: dict, executed_ids: list, reused_ids: list) -> dict:
    """Точный подсчёт файлов и байтов, прочитанных runner при pre/post снимках."""
    scripts_dir = copy_root / "ai-dev-playbook" / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import checks_graph
    import checks_inventory

    graph = checks_graph.build_graph(copy_root, policy)
    by_id = {c["id"]: c for c in policy["checks"]}
    files_hashed = 0
    bytes_hashed = 0

    def count_once(cid):
        nonlocal files_hashed, bytes_hashed
        chk = by_id[cid]
        if chk["kind"] == "manual":
            return
        state = {"files": 0, "bytes": 0}

        def counting_hash(path):
            p = Path(path)
            state["files"] += 1
            state["bytes"] += p.lstat().st_size
            return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()

        inputs = checks_inventory.resolved_inputs(
            policy, chk, edges=graph["edges"], unknown=graph["unknown"]
        )
        checks_inventory.snapshot(
            copy_root, policy, inputs,
            projections={"task_projection": {}, "policy_projection": policy},
            hash_fn=counting_hash,
        )
        return state["files"], state["bytes"]

    for cid in executed_ids:
        f_cnt, b_cnt = count_once(cid)
        # Исполняемая проверка делает pre и post снимки в локальном режиме.
        files_hashed += 2 * f_cnt
        bytes_hashed += 2 * b_cnt
    for cid in reused_ids:
        f_cnt, b_cnt = count_once(cid)
        # Reused проверка делает 1 снимок подтверждения неизменности при записи reuse.
        files_hashed += f_cnt
        bytes_hashed += b_cnt
    return {"runner_snapshot_files": files_hashed, "runner_snapshot_bytes": bytes_hashed}


def check_protected_boundary(source: Path, tmp_base: Path, log_dir: Path, run_cmd) -> dict:
    """Отдельная проверка local vs protected границ (AC-03 и AC-11).

    Подмножество rev2 содержит только arena-совместимые command-проверки:
    self-check исключён, т.к. check --ready требует work/-материалы
    активных карточек, а снимок кандидата service-область work/ не
    материализует.
    """
    probe_copy = tmp_base / "protected_probe_copy"
    copy_clean_repo(source, probe_copy)
    prepare_pb064_card(probe_copy, register_self_pilot=True)

    # 1. Политика с kind=test отвергается verifier run (self-reported исходы).
    test_policy, _ = build_self_policy(probe_copy)
    test_policy["trust_mode"] = "protected"
    (probe_copy / "checks-policy.json").write_text(
        json.dumps(test_policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    run_cmd(probe_copy, "prot-01-apply", ["setup", "apply"], expected=0)
    vhome_test = tmp_base / "verifier_home_test_kind"
    run_cmd(probe_copy, "prot-02-init-test",
            ["verifier", "init", "--home", str(vhome_test), "--project-id", "ai-dev-playbook"], expected=0)
    run_cmd(probe_copy, "prot-03-approve-test",
            ["verifier", "approve", "--home", str(vhome_test)], expected=0)
    reject_proc, _ = run_cmd(probe_copy, "prot-04-reject-kind-test",
                             ["verifier", "run", "--home", str(vhome_test), "--scope", "project"], expected=1)

    # 2. Политика с наблюдаемой command-проверкой preflight-regressions
    # исполняется в arena с sandbox-exec, выпускает валидную
    # HMAC-квитанцию, но probe и release-ready честно сообщают
    # capability=local без отдельной OS/CI identity (st_uid == euid).
    # self-check (playbook.py check --ready) в arena не входит: снимок
    # кандидата исключает service-область work/, а активные карточки
    # ссылаются на обязательные материалы work/verification/* — проверка
    # структуры проекта вне арены непредставима. Это ограничение
    # защищённого контура задокументировано в matrix.md.
    cmd_policy = json.loads(json.dumps(test_policy))
    cmd_policy["revision"] = 2
    cmd_policy["checks"] = [
        c for c in cmd_policy["checks"]
        if c["kind"] == "command" and c["id"] != "self-check"
    ]
    (probe_copy / "checks-policy.json").write_text(
        json.dumps(cmd_policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    vhome_cmd = tmp_base / "verifier_home_cmd"
    run_cmd(probe_copy, "prot-05-init-cmd",
            ["verifier", "init", "--home", str(vhome_cmd), "--project-id", "ai-dev-playbook"], expected=0)
    run_cmd(probe_copy, "prot-06-approve-cmd",
            ["verifier", "approve", "--home", str(vhome_cmd)], expected=0)
    probe_proc, _ = run_cmd(probe_copy, "prot-07-probe",
                            ["verifier", "probe", "--home", str(vhome_cmd)], expected=0)
    run_cmd(probe_copy, "prot-08-verifier-run",
            ["verifier", "run", "--home", str(vhome_cmd), "--scope", "project"], expected=0)
    receipts = sorted((vhome_cmd / "receipts").glob("*.json"))
    assert len(receipts) == 1, "Ожидалась ровно одна выпущенная квитанция"
    run_cmd(probe_copy, "prot-09-verifier-verify",
            ["verifier", "verify", "--home", str(vhome_cmd), "--receipt", str(receipts[0])], expected=0)
    gate_proc, _ = run_cmd(probe_copy, "prot-10-verifier-gate",
                           ["verifier", "gate", "--home", str(vhome_cmd)], expected=0)
    rr_proc, _ = run_cmd(
        probe_copy, "prot-11-release-ready-blocked",
        ["checks", "release-ready", "PB-064"], expected=1,
        extra_env={"PLAYBOOK_VERIFIER_HOME": str(vhome_cmd)},
    )
    return {
        "kind_test_rejected_by_verifier": "self-reported" in (reject_proc.stderr + reject_proc.stdout),
        "command_isolated_receipt_verified": True,
        "receipt_id": json.loads(receipts[0].read_text(encoding="utf-8"))["receipt_id"],
        "probe_capability": "local" if "capability=local" in probe_proc.stdout else "unknown",
        "sandbox_exec_present": shutil.which("sandbox-exec") is not None,
        # verifier gate — диагностический consumer: ok отражает покрытие
        # квитанциями, а не защищённость; protected_confirmed требует
        # доказанной границы OS identity (см. checks_verifier.gate).
        "verifier_gate_coverage_ok_capability_local":
            gate_proc.returncode == 0 and "capability=local" in gate_proc.stdout,
        "release_ready_refused_without_separate_os_identity": rr_proc.returncode == 1,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--plan-only", action="store_true",
                        help="Проверить обследование, политику, граф и границы без долгого запуска всех тестов")
    args = parser.parse_args()

    source = args.source.resolve()
    output = args.output.resolve()
    if output.is_relative_to(source) and not str(output).startswith(str(source / "docs/tasks/PB-064-evidence")):
        raise RuntimeError("Выходной каталог внутри репозитория допускается только в PB-064-evidence")
    output.mkdir(parents=True, exist_ok=True)
    logs_dir = output / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    before_source = tree_fingerprint(source)
    before_project_bytes = (source / "ai-dev-playbook" / "project.json").read_bytes()

    commands_log = []

    def run_cmd(cwd: Path, label: str, cli_args: list, *, expected: int = 0, extra_env: dict = None):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        if extra_env:
            env.update(extra_env)
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "ai-dev-playbook/scripts/playbook.py", *cli_args],
            cwd=cwd, text=True, capture_output=True, env=env,
        )
        elapsed = round(time.monotonic() - started, 3)
        (logs_dir / f"{label}.stdout.log").write_text(proc.stdout, encoding="utf-8")
        (logs_dir / f"{label}.stderr.log").write_text(proc.stderr, encoding="utf-8")
        record = {
            "label": label,
            "argv": cli_args,
            "returncode": proc.returncode,
            "expected": expected,
            "seconds": elapsed,
        }
        commands_log.append(record)
        (output / "commands.json").write_text(
            json.dumps(commands_log, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if proc.returncode != expected:
            raise RuntimeError(
                f"[{label}] {cli_args} -> {proc.returncode} (expected {expected})\n"
                f"STDOUT:\n{proc.stdout[-2000:]}\nSTDERR:\n{proc.stderr[-2000:]}"
            )
        print(f"[{label}] {' '.join(cli_args)} -> rc={proc.returncode} ({elapsed}s)", flush=True)
        return proc, elapsed

    with tempfile.TemporaryDirectory(prefix="pb064-self-pilot-") as tmp:
        tmp_base = Path(tmp).resolve()
        legacy_root = tmp_base / "copy_a_legacy"
        optin_root = tmp_base / "copy_b_optin"
        copy_clean_repo(source, legacy_root)
        copy_clean_repo(source, optin_root)

        prepare_pb064_card(legacy_root, register_self_pilot=True)
        prepare_pb064_card(optin_root, register_self_pilot=True)

        # --- Шаг 0: Opt-in на Copy B ---
        optin_before_preview = tree_fingerprint(optin_root)
        _, preview_sec = run_cmd(optin_root, "00-optin-setup-preview", ["setup", "preview"], expected=0)
        assert tree_fingerprint(optin_root) == optin_before_preview, "setup preview изменил дерево!"

        policy, catalog_stats = build_self_policy(optin_root)
        policy_json = json.dumps(policy, ensure_ascii=False, indent=2) + "\n"
        (optin_root / "checks-policy.json").write_text(policy_json, encoding="utf-8")
        (output / "policy-applied.json").write_text(policy_json, encoding="utf-8")
        _, apply_sec = run_cmd(optin_root, "01-optin-setup-apply", ["setup", "apply"], expected=0)

        plan0_proc, plan0_sec = run_cmd(optin_root, "02-optin-initial-plan", ["checks", "plan", "PB-064"], expected=0)
        assert "unknown-областей: 0" in plan0_proc.stdout, f"Неожиданные unknown в графе:\n{plan0_proc.stdout}"
        assert "Вне модулей:" not in plan0_proc.stdout, f"Неприписанные файлы:\n{plan0_proc.stdout}"

        if args.plan_only:
            boundary = check_protected_boundary(source, tmp_base, logs_dir, run_cmd)
            assert tree_fingerprint(source) == before_source, "Исходное дерево изменилось!"
            assert (source / "ai-dev-playbook" / "project.json").read_bytes() == before_project_bytes
            print("PLAN-ONLY OK:", json.dumps({"catalog": catalog_stats, "boundary": boundary}, ensure_ascii=False))
            return 0

        # --- Шаг 1: Первичный полный прогон на идентичном чистом состоянии ---
        run_cmd(legacy_root, "10-legacy-preflight", ["verify", "PB-064", "--preflight"], expected=0)
        _, legacy_step1_sec = run_cmd(legacy_root, "11-legacy-initial-verify", ["verify", "PB-064"], expected=0)
        legacy_reports_1 = sorted((legacy_root / "work/verification/PB-064").glob("*/report.json"),
                                  key=lambda p: p.stat().st_mtime_ns)
        legacy_step1_metrics = legacy_report_metrics(legacy_root, legacy_reports_1[-1])
        legacy_step1_metrics["wall_seconds"] = legacy_step1_sec

        _, optin_step1_sec = run_cmd(
            optin_root, "12-optin-initial-force",
            ["checks", "run", "PB-064", "--scope", "project", "--mode", "force"], expected=0,
        )
        optin_step1_cost = parse_plan_cost(plan0_proc.stdout)
        optin_step1_snap = optin_snapshot_cost(
            optin_root, policy,
            executed_ids=[c["id"] for c in policy["checks"]],
            reused_ids=[],
        )
        optin_step1_metrics = {
            "commands_executed": len(policy["checks"]),
            "command_ids": [c["id"] for c in policy["checks"]],
            **optin_step1_cost,
            **optin_step1_snap,
            "hashed_files_total": optin_step1_cost["plan_hashed_files"] + optin_step1_snap["runner_snapshot_files"],
            "hashed_bytes_total": optin_step1_cost["plan_hashed_bytes"] + optin_step1_snap["runner_snapshot_bytes"],
            "wall_seconds": optin_step1_sec,
        }

        # --- Шаг 2: Одинаковая локальная правка в tests/test_checks_setup.py ---
        for root_dir in (legacy_root, optin_root):
            target = root_dir / LOCALIZED_TARGET
            target.write_text(target.read_text(encoding="utf-8") + LOCALIZED_APPEND, encoding="utf-8")

        _, legacy_step2_sec = run_cmd(
            legacy_root, "20-legacy-localized-verify-reuse",
            ["verify", "PB-064", "--reuse"], expected=0,
        )
        legacy_reports_2 = sorted((legacy_root / "work/verification/PB-064").glob("*/report.json"),
                                  key=lambda p: p.stat().st_mtime_ns)
        assert len(legacy_reports_2) == 2, "Legacy --reuse обязан создать новый прогон при изменении входа"
        legacy_step2_metrics = legacy_report_metrics(legacy_root, legacy_reports_2[-1])
        legacy_step2_metrics["wall_seconds"] = legacy_step2_sec

        plan2_proc, plan2_sec = run_cmd(
            optin_root, "21-optin-localized-plan", ["checks", "plan", "PB-064"], expected=0,
        )
        _, optin_step2_sec = run_cmd(
            optin_root, "22-optin-localized-stale-run",
            ["checks", "run", "PB-064", "--scope", "task", "--mode", "stale"], expected=0,
        )
        # Разбираем последний каталог результатов run на Copy B
        latest_run_dir = max((optin_root / "work/checks/results").iterdir(), key=lambda p: p.stat().st_mtime_ns)
        step2_docs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(latest_run_dir.glob("*.json"))]
        step2_exec = [d["check_id"] for d in step2_docs if d["status"] == "passed" and not d.get("reused")]
        step2_reused = [d["check_id"] for d in step2_docs if d.get("reused")]
        optin_step2_cost = parse_plan_cost(plan2_proc.stdout)
        optin_step2_snap = optin_snapshot_cost(optin_root, policy, step2_exec, step2_reused)
        optin_step2_metrics = {
            "commands_executed": len(step2_exec),
            "executed_check_ids": step2_exec,
            "reused_check_ids": step2_reused,
            "plan_seconds": plan2_sec,
            **optin_step2_cost,
            **optin_step2_snap,
            "hashed_files_total": optin_step2_cost["plan_hashed_files"] + optin_step2_snap["runner_snapshot_files"],
            "hashed_bytes_total": optin_step2_cost["plan_hashed_bytes"] + optin_step2_snap["runner_snapshot_bytes"],
            "wall_seconds": round(plan2_sec + optin_step2_sec, 3),
            "run_only_wall_seconds": optin_step2_sec,
        }

        # --- Шаг 2b: Read-only проверка транзитивного замыкания AST при правке checks_setup.py ---
        trans_file = optin_root / TRANSITIVE_TARGET
        orig_trans = trans_file.read_text(encoding="utf-8")
        trans_file.write_text(orig_trans + TRANSITIVE_APPEND, encoding="utf-8")
        trans_plan_proc, trans_plan_sec = run_cmd(
            optin_root, "23-optin-transitive-ast-plan", ["checks", "plan", "PB-064"], expected=0,
        )
        trans_file.write_text(orig_trans, encoding="utf-8")

        # --- Шаг 3: Повтор на неизменных входах + release-ready + handoff + audit-ready + close --dry-run ---
        _, legacy_step3_sec = run_cmd(
            legacy_root, "30-legacy-repeat-unchanged-reuse",
            ["verify", "PB-064", "--reuse"], expected=0,
        )
        legacy_reports_3 = sorted((legacy_root / "work/verification/PB-064").glob("*/report.json"),
                                  key=lambda p: p.stat().st_mtime_ns)
        assert len(legacy_reports_3) == 2, "При неизменных входах --reuse не должен создавать третий отчёт"
        legacy_once_bytes = sum(
            (legacy_root / rel).stat().st_size
            for rel in json.loads(legacy_reports_3[-1].read_text(encoding="utf-8"))["snapshot_before"]["files"]
            if (legacy_root / rel).is_file()
        )
        legacy_step3_metrics = {
            "commands_executed": 0,
            "hashed_files_total": legacy_step2_metrics["unique_input_files"],
            "hashed_bytes_total": legacy_once_bytes,
            "wall_seconds": legacy_step3_sec,
        }

        plan3_proc, plan3_sec = run_cmd(
            optin_root, "31-optin-repeat-plan", ["checks", "plan", "PB-064"], expected=0,
        )
        _, optin_step3_sec = run_cmd(
            optin_root, "32-optin-release-stale",
            ["checks", "run", "PB-064", "--scope", "project", "--checkpoint", "release", "--mode", "stale"],
            expected=0,
        )
        run_cmd(optin_root, "33-optin-status", ["checks", "status", "PB-064"], expected=0)
        run_cmd(optin_root, "34-optin-release-ready", ["checks", "release-ready", "PB-064"], expected=0)

        latest_run_dir3 = max((optin_root / "work/checks/results").iterdir(), key=lambda p: p.stat().st_mtime_ns)
        step3_docs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(latest_run_dir3.glob("*.json"))]
        step3_exec = [d["check_id"] for d in step3_docs if d["status"] == "passed" and not d.get("reused")]
        step3_reused = [d["check_id"] for d in step3_docs if d.get("reused")]
        assert len(step3_exec) == 0 and len(step3_reused) == len(policy["checks"]), "Ожидался полный reuse"
        optin_step3_cost = parse_plan_cost(plan3_proc.stdout)
        optin_step3_snap = optin_snapshot_cost(optin_root, policy, step3_exec, step3_reused)
        optin_step3_metrics = {
            "commands_executed": 0,
            "reused_check_ids": step3_reused,
            **optin_step3_cost,
            **optin_step3_snap,
            "hashed_files_total": optin_step3_cost["plan_hashed_files"] + optin_step3_snap["runner_snapshot_files"],
            "hashed_bytes_total": optin_step3_cost["plan_hashed_bytes"] + optin_step3_snap["runner_snapshot_bytes"],
            "wall_seconds": optin_step3_sec,
        }

        # Жизненный цикл карточки на Copy B без legacy verify
        optin_card = optin_root / "docs/tasks/PB-064.md"
        optin_card.write_text(
            optin_card.read_text(encoding="utf-8")
            .replace("Status: in_progress", "Status: in_review", 1)
            + "\n### CHECK-01 — Проверка\n"
            "Время: 2026-11-14T15:30:00+03:00\n"
            "Исполнитель: Devin\n"
            "Модель: SWE-2 Flash High\n"
            "Результат: passed\n"
            "Критерии: AC-01, AC-02, AC-03, AC-04\n"
            "Команда: `python3 ai-dev-playbook/scripts/playbook.py checks run PB-064`\n"
            "Среда: временная копия self-pilot без .git.\n"
            "Факт: обязательства покрыты.\n"
            "Доказательства: результаты checks.\n"
            "Ограничения: изолированная копия, trust_mode=local.\n\n"
            "### HANDOFF-01 — Передача\n"
            "Время: 2026-11-14T15:31:00+03:00\n"
            "Исполнитель: Devin\n"
            "Модель: SWE-2 Flash High\n"
            "Выполнение: EXEC-01\n"
            "Проверки: CHECK-01\n"
            "Состояние: in_review на временной копии пилота.\n"
            "Фокус аудита: демонстрация связки checks + handoff + audit-ready.\n",
            encoding="utf-8",
        )
        run_cmd(optin_root, "35-optin-index", ["index"], expected=0)
        run_cmd(optin_root, "36-optin-handoff-1",
                ["lifecycle", "handoff", "PB-064", "--summary", "HANDOFF-01",
                 "--actor", "Devin", "--model", "SWE-2 Flash High"], expected=0)
        handoff2_proc, _ = run_cmd(
            optin_root, "37-optin-handoff-idempotent",
            ["lifecycle", "handoff", "PB-064", "--summary", "HANDOFF-01",
             "--actor", "Devin", "--model", "SWE-2 Flash High"], expected=0,
        )
        run_cmd(optin_root, "38-optin-audit-ready", ["verify", "PB-064", "--audit-ready"], expected=0)
        run_cmd(optin_root, "39-optin-close-refused-without-accept",
                ["close", "PB-064", "--dry-run"], expected=1)

        # --- Отдельная проверка границ local vs protected ---
        boundary = check_protected_boundary(source, tmp_base, logs_dir, run_cmd)

    assert tree_fingerprint(source) == before_source, "Исходный проект был изменён!"
    assert (source / "ai-dev-playbook" / "project.json").read_bytes() == before_project_bytes
    assert "checks_policy" not in json.loads(before_project_bytes.decode("utf-8"))

    summary = {
        "schema_version": 1,
        "task_id": "PB-064",
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "sandbox_exec_available": shutil.which("sandbox-exec") is not None,
            "separate_os_identity_available": False,
        },
        "source_repository_unchanged": True,
        "source_checks_policy_enabled": False,
        "catalog_bijection": catalog_stats,
        "optin_setup_seconds": {"preview": preview_sec, "apply": apply_sec},
        "scenarios": {
            "step1_initial_full_baseline": {
                "description": "Первичный полный прогон на идентичной чистой копии без .git и work/",
                "legacy_verify": legacy_step1_metrics,
                "optin_checks": optin_step1_metrics,
            },
            "step2_localized_edit_tests_checks_setup": {
                "description": f"Одинаковая локальная правка в {LOCALIZED_TARGET}",
                "legacy_verify_reuse": legacy_step2_metrics,
                "optin_checks_task_stale": optin_step2_metrics,
            },
            "step2b_transitive_ast_plan_checks_setup_script": {
                "description": f"Read-only checks plan при правке {TRANSITIVE_TARGET} (транзитивное ребро в playbook.py)",
                "plan_seconds": trans_plan_sec,
                "block_unit_tests_reused": "block-unit-tests: reused" in trans_plan_proc.stdout
                                           or "block-unit-tests: not_applicable" in trans_plan_proc.stdout,
                "core_and_cli_selected": ("core-unit-tests: run" in trans_plan_proc.stdout
                                          and "cli-unit-tests: run" in trans_plan_proc.stdout),
            },
            "step3_repeat_unchanged_and_release_gate": {
                "description": "Повтор на неизменных входах, release-ready, handoff и audit-ready",
                "legacy_verify_reuse": legacy_step3_metrics,
                "optin_checks_project_release_stale": optin_step3_metrics,
                "handoff_idempotent": "уже сделана" in handoff2_proc.stdout,
                "audit_ready_passed": True,
                "close_without_acceptance_refused": True,
            },
        },
        "trust_boundaries": boundary,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("SELF-PILOT COMPLETED SUCCESSFULLY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
