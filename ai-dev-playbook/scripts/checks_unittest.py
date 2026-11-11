"""Адаптер ``python-unittest`` (protocol 1) для runner PB-060.

Адаптер исполняет одобренный ``argv`` проверки kind=test как выбор
unittest и выдаёт структурированный отчёт: ожидаемый набор стабильных
test ID (discovery по тому же argv) и фактические исходы через
``TestResult`` — passed/failed/error/skipped/expected_failure/
unexpected_success, включая subtests. Текстовый разбор ``-v`` не
используется: события результата — единственный источник факта.

Подпроцесс запускается ``sys.executable checks_unittest.py --suite
--argv JSON`` в корне проекта: загружает suite без исполнения
discovery вне одобренной команды, исполняет и выдаёт отчёт одной
строкой с маркером ``REPORT_SENTINEL`` в stdout. Путь файла отчёта
тестовому процессу не передаётся: отчёт, записанный тестовым кодом
в run_dir, доверием не является — родитель разбирает канал stdout
после завершения и сам сохраняет ``*.adapter.json`` (A-01, PB-061).
Завершение без строки отчёта, усечённый JSON и нулевой набор — не
``passed`` (R-13). Дочерний процесс завершается ``os._exit``, чтобы
обработчики ``atexit`` тестового кода не печатали строки после
отчёта. Родитель сверяет набор с одобренным ``test_catalog``: тест
вне каталога, пропавший ID группы проверки, дубликат, незавершённый
ID и skip/expected_failure вне допусков отклоняются.

Остаточное ограничение, заявленное честно: события результата
вычисляются в том же процессе, что и тестовый код, — вредоносный
тест может подменить состояние адаптера изнутри процесса либо
оставить отделённый процесс, пишущий в stdout после отчёта. Такой
канал принципиально не доказывает происхождение self-reported
исходов; защищённое подтверждение опирается на границу verifier,
а не на неподделываемость заявлений тестов.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

import checks_contract

ADAPTER = {"id": checks_contract.BUILTIN_ADAPTER,
           "version": str(checks_contract.ADAPTER_PROTOCOL_VERSION)}
REPORT_PROTOCOL = 1
REPORT_SENTINEL = "CHECKS-REPORT-V1 "

_PASSED = "passed"
_FAIL_OUTCOMES = {"failed", "error"}


def parse_argv(argv):
    """Разобрать одобренный argv проверки в выбор unittest.

    Поддерживается ``python* -m unittest [discover -s S -p P -t T]``
    либо ``python* -m unittest <имя>...``. Иное — ошибка: адаптер не
    исполняет команды, которые не может отобразить на unittest-набор.
    ``-k``-фильтрация пока не поддерживается (отказ, не угадывание).
    """
    if not isinstance(argv, list) or len(argv) < 3:
        raise ValueError("checks-unittest: argv должен содержать "
                         "python -m unittest")
    exe = Path(str(argv[0])).name
    if not exe.startswith("python"):
        raise ValueError(f"checks-unittest: интерпретатор {argv[0]!r} "
                         "не является python*")
    if argv[1] != "-m" or argv[2] != "unittest":
        raise ValueError("checks-unittest: поддерживается только "
                         "'python -m unittest'")
    tail = list(argv[3:])
    flags = {"-v", "--verbose", "-q", "--quiet", "-f", "--failfast",
             "-b", "--buffer", "-c", "--catch", "--locals", "-d",
             "--durations"}
    if any(a in {"-k", "--k"} or a.startswith("-k=")
           or a.startswith("--durations=") for a in tail):
        raise ValueError("checks-unittest: фильтрация -k не "
                         "поддерживается адаптером")
    if tail and tail[0] == "discover":
        selection = {"kind": "discover", "start": ".",
                     "pattern": "test*.py", "top": None}
        idx = 1
        while idx < len(tail):
            arg = tail[idx]
            if arg in {"-s", "--start-directory"}:
                idx += 1
                if idx >= len(tail):
                    raise ValueError("checks-unittest: -s без значения")
                selection["start"] = tail[idx]
            elif arg.startswith("--start-directory="):
                selection["start"] = arg.split("=", 1)[1]
            elif arg in {"-p", "--pattern"}:
                idx += 1
                if idx >= len(tail):
                    raise ValueError("checks-unittest: -p без значения")
                selection["pattern"] = tail[idx]
            elif arg.startswith("--pattern="):
                selection["pattern"] = arg.split("=", 1)[1]
            elif arg in {"-t", "--top-level-directory"}:
                idx += 1
                if idx >= len(tail):
                    raise ValueError("checks-unittest: -t без значения")
                selection["top"] = tail[idx]
            elif arg.startswith("--top-level-directory="):
                selection["top"] = arg.split("=", 1)[1]
            elif arg in flags:
                pass
            else:
                raise ValueError(f"checks-unittest: неизвестный "
                                 f"аргумент discover {arg!r}")
            idx += 1
        return selection
    names = [a for a in tail if a not in flags]
    return {"kind": "names", "names": names}


def _flatten(suite):
    """Список стабильных test ID загруженного suite (без исполнения)."""
    ids = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            ids.extend(_flatten(item))
        else:
            ids.append(item.id())
    return ids


class JsonResult(unittest.TestResult):
    """События TestResult → фактические исходы по стабильным ID.

    Провальный subtest делает родительский тест failed; поздняя
    ошибка tearDown перекрывает ранний success (семантика unittest).
    Дубликаты не скрываются: два события одного ID видны родителю.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events = []
        self._order = {}

    def _record(self, test, outcome, reason=None):
        test_id = test.id()
        entry = {"id": test_id, "outcome": outcome}
        if reason:
            entry["reason"] = reason
        if test_id in self._order:
            # error/failed после раннего события перекрывает его;
            # повторный запуск того же ID добавляет вторую запись —
            # сверка на стороне runner отклонит дубликат.
            prev = self.events[self._order[test_id]]
            if prev["outcome"] in _FAIL_OUTCOMES:
                return
            if outcome in _FAIL_OUTCOMES:
                self.events[self._order[test_id]] = entry
                return
            self.events.append(entry)
            return
        self._order[test_id] = len(self.events)
        self.events.append(entry)

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, _PASSED)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "failed")

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, "error")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, "skipped", str(reason))

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._record(test, "expected_failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._record(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._record(test, "failed")


def _load_suite(selection):
    loader = unittest.TestLoader()
    if selection["kind"] == "discover":
        top = selection["top"] or selection["start"]
        return loader.discover(start_dir=selection["start"],
                               pattern=selection["pattern"],
                               top_level_dir=top)
    if not selection["names"]:
        return loader.discover(start_dir=".", pattern="test*.py",
                               top_level_dir=".")
    return loader.loadTestsFromNames(selection["names"])


def _subprocess_main(argv=None):
    """Режим suite: исполнить набор, выдать отчёт в stdout.

    Отчёт — последняя строка с ``REPORT_SENTINEL``; процесс
    завершается ``os._exit``, чтобы ``atexit``-код тестов не мог
    добавить строки после отчёта. Файл отчёта не создаётся: путь
    недоступен тестовому процессу, подделка файла не принимается.
    """
    parser = argparse.ArgumentParser(prog="checks_unittest")
    parser.add_argument("--suite", action="store_true", required=True)
    parser.add_argument("--argv", required=True)
    args = parser.parse_args(argv)
    report = {"protocol": REPORT_PROTOCOL, "expected": [],
              "actual": [], "exit_code": 2}
    try:
        selection = parse_argv(json.loads(args.argv))
        suite = _load_suite(selection)
        report["expected"] = _flatten(suite)
        result = unittest.TextTestRunner(
            resultclass=JsonResult, stream=sys.stderr,
            verbosity=1).run(suite)
        report["actual"] = result.events
        report["exit_code"] = 0 if result.wasSuccessful() else 1
    except Exception as error:  # отказ до/во время загрузки — crash
        report["error"] = f"{type(error).__name__}: {error}"
    sys.stdout.write(REPORT_SENTINEL
                     + json.dumps(report, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    # Ack-протокол A-01: доверенный хвост ждёт подтверждения родителя.
    # Строка отчёта, напечатанная тестовым кодом до выхода процесса,
    # не может быть подтверждена живым процессом и отклоняется.
    try:
        sys.stdin.buffer.read(1)
    except (OSError, ValueError, AttributeError):
        pass
    os._exit(report["exit_code"])


def _write_report(out_path, report):
    """Атомарно сохранить проверенный отчёт; пишет только родитель."""
    temporary = Path(str(out_path) + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    temporary.replace(out_path)


def collect(root, check, run_dir, *, timeout, env=None, wrap=None):
    """Исполнить проверку в подпроцессе; вернуть сырой сбор адаптера.

    ``run_dir`` — каталог прогона внутри work/checks; stdout/stderr
    уходят в файлы рядом с отчётом. Timeout останавливает группу
    процессов; отсутствующий/повреждённый отчёт — crash, не passed.
    ``wrap`` — необязательный префикс argv механизма изоляции
    (например, sandbox-exec доверенного контура PB-061).

    Канал отчёта (A-01): строка ``REPORT_SENTINEL`` в stdout
    подтверждается ack-байтом по stdin только если процесс ещё жив —
    отчёт, напечатанный тестовым кодом и за которым процесс завершился
    (``os._exit``, crash), отклоняется как posthumous; вторая строка
    отчёта (подделка поверх настоящей) — crash. ``adapter.json``
    перезаписывается родителем только проверенным содержимым.
    """
    import verification
    root = Path(root)
    script = Path(__file__).resolve()
    out_path = run_dir / (check["id"] + ".adapter.json")
    argv = [sys.executable, str(script), "--suite", "--argv",
            json.dumps(check["argv"], ensure_ascii=False)]
    if wrap:
        argv = list(wrap) + argv
    stdout = run_dir / (check["id"] + ".stdout.log")
    stderr = run_dir / (check["id"] + ".stderr.log")
    collected = {"status": None, "exit_code": None, "report": None,
                 "stdout_log": str(stdout.relative_to(root)),
                 "stderr_log": str(stderr.relative_to(root)),
                 "report_path": str(out_path.relative_to(root))}
    # A-05: `python -m unittest` из корня видит корень в sys.path;
    # файл-скрипт ставит scripts/ — корень добавляем через PYTHONPATH,
    # иначе импорты модулей проекта не воспроизводят одобренную команду.
    env = dict(os.environ if env is None else env)
    root_abs = str(root.resolve())
    if env.get("PYTHONPATH"):
        env["PYTHONPATH"] = root_abs + os.pathsep + env["PYTHONPATH"]
    else:
        env["PYTHONPATH"] = root_abs
    process = None
    reports = []
    reports_lock = threading.Lock()
    acked = False
    posthumous = False
    try:
        with stdout.open("xb") as out_file, \
                stderr.open("xb") as err:
            process = subprocess.Popen(
                argv, cwd=root, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=err, env=env,
                start_new_session=True)

            def _pump():
                sentinel = REPORT_SENTINEL.encode("utf-8")
                try:
                    for line in process.stdout:
                        out_file.write(line)
                        out_file.flush()
                        if line.startswith(sentinel):
                            with reports_lock:
                                reports.append(line)
                except (OSError, ValueError):
                    pass
                finally:
                    try:
                        out_file.close()
                    except OSError:
                        pass

            pump = threading.Thread(target=_pump, daemon=True)
            pump.start()
            deadline = time.monotonic() + timeout
            while True:
                with reports_lock:
                    pending = bool(reports)
                if pending and not acked:
                    if process.poll() is None:
                        try:
                            process.stdin.write(b"\x00")
                            process.stdin.flush()
                            acked = True
                        except (OSError, ValueError):
                            posthumous = True
                    else:
                        posthumous = True
                if process.poll() is not None:
                    collected["exit_code"] = process.returncode
                    break
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(argv, timeout)
                time.sleep(0.005)
            pump.join(timeout=5)
    except subprocess.TimeoutExpired:
        collected["status"] = "timeout"
    except OSError as error:
        collected.update(status="spawn_error", error=str(error))
    finally:
        if process is not None:
            verification.stop_group(process)
            collected.setdefault("exit_code", process.returncode)
            for stream in (process.stdin, process.stdout):
                try:
                    stream.close()
                except (OSError, ValueError, AttributeError):
                    pass
    if collected["status"] is not None:
        return collected
    with reports_lock:
        lines = list(reports)
    if len(lines) != 1 or not acked or posthumous:
        collected["status"] = "crash"
        collected["error"] = ("канал отчёта адаптера не подтверждён: "
                              f"строк={len(lines)}, ack={acked}, "
                              f"posthumous={posthumous}, "
                              f"exit {collected['exit_code']}")
        return collected
    payload = lines[0][len(REPORT_SENTINEL):]
    try:
        report = json.loads(payload.decode("utf-8").strip(),
                            object_pairs_hook=_unique_pairs)
        _validate_report(report)
        _write_report(out_path, report)
        collected["report"] = report
    except (ValueError, OSError, UnicodeDecodeError) as error:
        collected["status"] = "crash"
        collected["error"] = f"отчёт адаптера повреждён: {error}"
    return collected


def _unique_pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"повторное поле {key!r}")
        obj[key] = value
    return obj


def _validate_report(report):
    require = checks_contract_validate
    if not isinstance(report, dict) or report.get("protocol") != 1:
        raise ValueError("protocol != 1")
    expected = report.get("expected")
    actual = report.get("actual")
    if not isinstance(expected, list) or not all(
            isinstance(t, str) and t.strip() for t in expected):
        raise ValueError("expected не список test id")
    if not isinstance(actual, list):
        raise ValueError("actual не список")
    for item in actual:
        if not isinstance(item, dict) or \
                not isinstance(item.get("id"), str) or \
                item.get("outcome") not in checks_contract.TEST_OUTCOMES:
            raise ValueError("некорректная запись actual")
    if report.get("exit_code") not in {0, 1, 2}:
        raise ValueError("некорректный exit_code")


def checks_contract_validate(condition, message):
    if not condition:
        raise ValueError(message)


def claimed_catalog(check, policy):
    """Записи одобренного каталога, закреплённые за проверкой.

    ``group`` записи — ``id`` проверки либо связанного модуля; записи
    без группы общие: они проверяются при discovery любого набора.
    """
    catalog = {e["id"]: e
               for e in policy["test_catalog"]["entries"]}
    owned = {cid for cid in {check["id"], *(check.get("modules") or [])}}
    claimed = {ident: e for ident, e in catalog.items()
               if e.get("group") is not None and e["group"] in owned}
    return catalog, claimed


def verdict(check, policy, collected):
    """Сверить сбор адаптера с ожиданиями и каталогом (R-13/R-14).

    Возвращает ``{status, tests, reasons}``: exit 0, нулевой набор,
    пропавший/лишний/повторный ID, незавершённый тест, skip или
    expected_failure вне допусков, unexpected_success, crash и
    несогласованный отчёт не дают ``passed``.
    """
    reasons = []
    tests = {"expected": [], "actual": []}
    if collected["status"] == "timeout":
        return {"status": "timeout", "tests": tests,
                "reasons": ["превышен timeout проверки"]}
    if collected["status"] == "spawn_error":
        return {"status": "blocked", "tests": tests,
                "reasons": [f"запуск адаптера невозможен: "
                            f"{collected['error']}"]}
    if collected["status"] == "crash" or collected["report"] is None:
        return {"status": "failed", "tests": tests,
                "reasons": [f"crash: {collected.get('error', 'отчёт '
                            'адаптера отсутствует')}"]}
    report = collected["report"]
    # Валидная запись результата требует уникальных ID: первое событие
    # каждого ID сохраняется, факт дубликата фиксируется в reasons.
    seen_actual, kept_actual = set(), []
    for item in report["actual"]:
        if item["id"] not in seen_actual:
            seen_actual.add(item["id"])
            kept_actual.append(item)
    tests = {"expected": sorted(dict.fromkeys(report["expected"])),
             "actual": kept_actual}
    if report.get("error"):
        reasons.append(f"загрузка suite: {report['error']}")
    catalog, claimed = claimed_catalog(check, policy)
    expected, actual = report["expected"], report["actual"]
    if not expected:
        reasons.append("нулевой набор тестов: discovery пуст")
    if len(set(expected)) != len(expected):
        dupes = sorted({t for t in expected if expected.count(t) > 1})
        reasons.append(f"повторный test id при discovery: {dupes}")
    for ident in sorted(set(expected)):
        if ident not in catalog:
            reasons.append(f"тест вне одобренного каталога: {ident}")
    for ident in sorted(set(claimed) - set(expected)):
        reasons.append(f"пропавший test ID каталога: {ident} — "
                       f"{claimed[ident]['description']}")
    actual_ids = [item["id"] for item in actual]
    if len(set(actual_ids)) != len(actual_ids):
        dupes = sorted({t for t in actual_ids
                        if actual_ids.count(t) > 1})
        reasons.append(f"повторный test id в запуске: {dupes}")
    outcomes = {item["id"]: item for item in actual}
    for ident in sorted(set(actual_ids) - set(expected)):
        reasons.append(f"неожиданный test id в запуске: {ident}")
    for ident in sorted(set(expected) - set(actual_ids)):
        reasons.append(f"тест не завершён (нет исхода): {ident}")
    for item in actual:
        entry = catalog.get(item["id"])
        outcome = item["outcome"]
        if outcome in _FAIL_OUTCOMES:
            descr = f" — {entry['description']}" if entry else ""
            reasons.append(f"тест {outcome}: {item['id']}{descr}")
        elif outcome == "unexpected_success":
            reasons.append(f"unexpected_success недопустим: "
                           f"{item['id']}")
        elif outcome == "skipped" and not (
                entry and entry.get("skip", {}).get("allowed")):
            reasons.append(f"неожиданный skip: {item['id']} "
                           f"({item.get('reason', '')})")
        elif outcome == "expected_failure" and not (
                entry and entry.get("expected_failure", {})
                .get("allowed")):
            reasons.append(f"expected_failure вне допуска: "
                           f"{item['id']}")
    if not reasons and report["exit_code"] != 0:
        reasons.append(f"несогласованный отчёт: exit_code="
                       f"{report['exit_code']} при зелёных исходах")
    if not reasons and collected["exit_code"] not in {0, report["exit_code"]}:
        reasons.append(f"процесс адаптера завершился кодом "
                       f"{collected['exit_code']}")
    status = "passed" if not reasons else "failed"
    return {"status": status, "tests": tests, "reasons": reasons}


if __name__ == "__main__":
    sys.exit(_subprocess_main())
