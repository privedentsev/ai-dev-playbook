# PB-073: материалы пробы исправлений AUDIT-009

- `probe_fixes.py` / `probe-fixes.json` — новый поведенческий прогон
  реального CLI во временном проекте без Git. Утверждает исправленное
  поведение A-01 (failed дважды → обязательство исполняется повторно,
  baseline не продвигается, gate закрыт; третья попытка проходит →
  покрытие восстановлено) и сохраняет cross-task reuse для DEV-02 без
  повторного исполнения. rc=0 только при успехе всех утверждений.
- `probe-a01-rerun.json` — повтор исходной пробы
  `AUDIT-009-evidence/probe_cli_obligations.py` на исправленном коде.
  Проба содержит старое ожидание дефекта (`returncode == 0` на втором
  stale); теперь она падает на этом утверждении: второй `checks run`
  возвращает 1 и исполняет команду повторно (`payment: failed`).
  Traceback в файле — ожидаемое свидетельство смены поведения, а не
  ошибка исправления.
- `probe-a02-rerun.json` — повтор исходной пробы
  `AUDIT-009-evidence/probe_equal_timestamps.py`: два force-прогона с
  управляемым одинаковым `finished_at` (passed → failed). Теперь
  `latest_selected.payment.status = failed` (поздний run `ffff…`),
  `gate_after_later_failure.ok = false`, `payment` в `uncovered` —
  старый passed не маскируется.
- Постоянные регрессии: `tests/test_checks_run.py::Audit009RegressionTest`
  (7 сценариев: failed/timeout/blocked, оба лексикографических порядка
  run_id, index/rebuild/load, legacy-записи без run_seq, end-to-end
  статус, восстановление успешным прогоном).
- `probe-audit01-rerun.json` — повтор пробы независимого аудита
  `work/verification/PB-073/audit-01/probe_baseline_failure.py` после
  исправления A-01 повторного аудита: passed project → failed project →
  task stale. Теперь `attempts=3` (обязательство исполнено повторно),
  `stale_rc=1`, gate `ok=false`, `payment` в `uncovered` — пустой diff
  к актуальному baseline не скрывает непокрытое обязательство.
- `pilot-planonly/` — повтор `self_pilot.py --plan-only` на коде после
  исправления A-01 повторного аудита: setup/catalog/graph/boundary
  зелёные; `plan-only-summary.json` — stdout-сводка (полный пилот не
  повторялся: его сценарий не содержит неуспешных проверок, ветки
  нового правила не затрагиваются).
- Постоянные CLI-регрессии повторного аудита:
  `tests/test_checks_e2e.py::Audit009BaselineFailureE2ETest` (5
  сценариев реального CLI без Git: failed/timeout/blocked после
  актуального baseline, manual до confirm, другая задача и законный
  cross-task reuse). Попутно исправлена запись timeout command-
  проверок: `command.exit_code` проставляется кодом убитого процесса —
  раньше `validate_result` отклонял результат и прогон падал без
  записи в историю.
