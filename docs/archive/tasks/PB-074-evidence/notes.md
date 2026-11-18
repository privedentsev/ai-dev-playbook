# PB-074 — доказательства

Исправление A-04 и A-05 повторного аудита AUDIT-009 audit-02. Кандидат воспроизведения — рабочее дерево на базе 6311aa9; среда: macOS, Python 3.14.

## Воспроизведение (до исправления)

- `python3 docs/tasks/AUDIT-009-evidence/audit-02/probe_corrupt_obligation.py . corrupt` — после `passed → failed` повреждение записи failed при актуальном baseline давало task gate `ok=true`, статус `not_applicable`, stale `rc=0` без нового прогона (attempts=2).
- Тот же вызов с `delete` — удаление записи failed давало тот же ложный успех.
- `python3 docs/tasks/AUDIT-009-evidence/audit-02/probe_legacy_equal.py .` — legacy-записи без `run_seq` с одинаковым `finished_at` и UUID в обратном лексикографическом порядке выбирали старый `passed`; project gate `ok=true`.

## Исправление

- `checks_results.load_results`/`index_entries`: check_id с повреждённой или утраченной записью (по индексу) передаётся в планирование как открытое обязательство (`corrupt_ids`).
- `checks_select.plan` принимает `corrupt_ids` и резервирует `run_seq` производного индекса: недоказуемая история требует нового прогона независимо от diff; новое доказательство всегда строго позже утраченного.
- `checks_select.result_order_key`: при равном `(finished_at, run_seq)` (оба legacy без `run_seq`) взаимный порядок недоказуем — консервативно выбирается непокрывающий исход вместо победы `result_id`.
- `checks_runner`, `checks_gate`, `playbook.py` прокидывают `corrupt_ids` и резерв seq во все пути plan/run/status/gate.

## Результаты после исправления (пробы аудитора)

- `probe-corrupt-fixed.json`: `corrupt` — gate_before_ok=false, gate_after_ok=false, payment `uncovered`, stale rc=1, attempts=3 (реальный повторный прогон).
- `probe-deleted-fixed.json`: `delete` — gate_after_ok=false, payment `uncovered`, stale rc=1, attempts=3, release-ready rc=1.
- `probe-legacy-equal-fixed.json`: legacy equal-timestamp — gate_before_legacy=false, выбран `failed`, gate_after_later_failure=false, corrupt не заявлен.

## Аудит-01 (changes_requested) — дополнительные контрпримеры

- `probe-audit01-directory-fixed.json`: удаление всего каталога `work/checks/results` при сохранённом baseline/индексе. `load_results` читал свидетельства индекса только при наличии каталога — после исправления индекс читается всегда: gate после удаления и rebuild остаётся `ok=false`, plan/status показывают `payment: run`, stale rc=1 с реальным третьим исполнением.
- `probe-audit01-delete-fixed.json`: удаление записи failed + rebuild. `checks plan` показывал `payment: reused` по старому passed — после исправления планировщик сам отсекает результаты `corrupt_ids` от reuse: plan `run=1, reused=0`, согласовано со status/gate/run.

## Аудит-02 (changes_requested) — остаточная находка

- A-03: при равном `(finished_at, run_seq)` selector выбирал непокрывающий исход, но `load_results`/`update_index`/`rebuild_index` сравнивали записи полным `result_order_key` с UUID — утраченный legacy-failed с меньшим UUID не обнаруживался, а старый passed-индекс воскресал при rebuild. Все три сравнения переведены на `_latest_better` — единый консервативный порядок selector; `claim` в сверке индекса получил `status`.
- `probe-audit02-legacy-loss-clean-fixed.json`: rebuild даёт корректный failed-индекс; после удаления failed-файла оба gate закрыты, stale реально повторяет (attempts=3), recovery passed работает.
- `probe-audit02-legacy-loss-oldindex-fixed.json`: старый passed-индекс не воскресает — rebuild хранит failed; далее то же закрытое поведение.
- `probe-audit02-legacy-loss-forward-fixed.json`: контроль прямого UUID-порядка — блокировка и повтор сохранены.

## Постоянные регрессии

- `tests/test_checks_run.py::Audit009PB74RegressionTest` — 10 unit-тестов: corrupt failed/timeout/manual, delete, equal-timestamp legacy, seq-порядок, однозначная legacy-история; аудит-01: утрата каталога results и запрет reuse в plan при corrupt_ids (с контрфактом законного reuse без флага); аудит-02: утрата legacy-failed при обоих UUID-порядках и обоих вариантах индекса (корректный rebuild и старый passed-индекс), task/project gate, stale и восстановление seq-passed.
- `tests/test_checks_e2e.py::Audit009BaselineFailureE2ETest` — 11 E2E/CLI-тестов на временных проектах без Git: failed/timeout/blocked/corrupt/delete/manual-until-confirm, rebuild индекса, видимость обязательства другой задачей, legacy equal timestamps, восстановление успешным прогоном; аудит-01: утрата каталога results и согласованность plan/status/run на утраченной истории; аудит-02: утрата legacy-failed со старым passed-индексом и худшим UUID-порядком через реальный CLI.

## Проверки

- `python3 -m unittest discover -s tests -p 'test_checks_*.py' -v` — 338 тестов, OK (~226 с).
- Остальные проверки — в журнале карточки (CHECK-записи).

## Ограничения

- Локальный режим: результаты диагностические (`trust_mode=local`), protected-граница не заявляется.
- Пробы аудитора печатают наблюдения; rc=0 процесса пробы не означает успех продукта.
