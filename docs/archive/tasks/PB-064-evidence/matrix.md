# PB-064: матрица AC-01–11 и воспроизведение

Сквозная проверка блока dependency-aware verification (PB-057–PB-063) на
временных проектах без Git и на изолированной копии этого репозитория.
Основной проект остаётся на legacy-контракте: `checks_policy` в
`ai-dev-playbook/project.json` отсутствует и пилотом не включается.

## Соответствие критериев проверкам

| Критерий | Сценарий | Проверка | Ожидаемый результат |
|---|---|---|---|
| AC-01 | Проект без `.git`: cart/payment/discounts, ручные правки, счётчики запусков | `tests/test_checks_e2e.py::ShopGraphE2ETest::test_ac01_no_git_manual_edits_payments_and_discounts_counters` | Правка payment запускает payment+checkout, cart reused; правка discounts затрагивает все три; счётчики subprocess подтверждают фактические запуски |
| AC-02 | Одинаковые mtime/size при других байтах, add/delete/rename, отсутствующий baseline, циклы и dynamic import | `test_ac02_same_mtime_size_add_delete_rename_and_missing_baseline`, `test_ac02_import_cycle_and_dynamic_import_expand_conservatively` | Изменённые байты обнаруживаются снимком; состав входов отслеживается; циклы/dynamic import дают консервативное расширение без ложного reuse |
| AC-03 | Логи/исключения, карточка и критерии приёмки, бинарные/archive входы, неизвестный файл | `InputsExclusionsFallbackBudgetE2ETest::test_ac03_excluded_logs_card_projection_binary_inputs_and_unknown_file`; карточная часть — `TrustBoundaryLifecycleAndMigrationE2ETest::test_ac08_and_card_ac03_bypass_attempts_and_local_vs_protected_boundary` | Изменение лога не запускает продуктовые проверки; task_projection/policy_projection привязаны; бинарные входы хешируются; неизвестный файл не теряется (fallback) |
| AC-04 | Границы подсистем, независимость результатов, бюджет | `test_ac04_subsystem_boundary_eight_reused_two_run_and_budget_stop` | Восемь актуальных результатов reused, два устаревших выполняются; недоказанная независимость запрещает reuse; превышение бюджета блокирует до запуска |
| AC-05 | Релиз cross-task, режимы stale/force, инвалидация среды/команды/fixture/TTL, поздний failed | `ReleaseModesFreshnessAndAdapterE2ETest::test_ac05_release_cross_task_stale_force_invalidation_and_late_failed` | `release-ready` собирает обязательства разных задач; force перезапускает выбранное; stale сохраняет пригодное; поздний failed не перекрывается passed |
| AC-06 | Негативные исходы адаптера: 0 тестов, пропавший/лишний ID, skip, дубликат, повреждённый отчёт; описания и reused-время | `test_ac06_adapter_negatives_descriptions_reused_time_and_label_rename` | Каждая подмена отклоняется вердиктом адаптера; rename label не выдаётся за новый тест |
| AC-07 | Гонка входов, дрейф после запуска, изоляция arena, подмена квитанции | `TrustBoundaryLifecycleAndMigrationE2ETest::test_ac07_input_race_post_run_drift_arena_isolation_and_receipt_tamper` | Изменение во время/после запуска не выдаёт passed; кандидат изолирован; подмена снимка/квитанции обнаруживается |
| AC-08 | Обходы доверия: editable approved, удаление проверки/связи, подмена ключа, поддельная квитанция, запись из тестового процесса; отзыв, другой project_id, replay | `test_ac08_and_card_ac03_bypass_attempts_and_local_vs_protected_boundary` | Все попытки обхода отклонены; local/protected границы разделены честно (capability=local без отдельной OS identity) |
| AC-09 | Ручные проверки, единый gate handoff/accept/release-ready, архивирование/provenance | `test_ac09_manual_and_task_criteria_unified_gate_and_archive_provenance` | Ручное обязательство остаётся ручным (`checks confirm`); handoff/accept/close используют один расчёт gate; архив не теряет provenance |
| AC-10 | Legacy неизменность, opt-in preview read-only, миграция, неподдержанная версия, прерывание/restore, повреждённая история | `test_ac10_legacy_intact_preview_readonly_unsupported_and_restore` | Без `checks_policy` действует прежний контракт; `setup preview` не пишет; restore и повреждённая история обрабатываются безопасно |
| AC-11 | Self-pilot: стоимость в запусках/файлах/байтах/времени, разделение local и protected | `docs/tasks/PB-064-evidence/self_pilot.py` → отчёт `pilot/` | Измерения без процента-обещания; local vs protected проверены отдельно; исходный проект не изменён |

## Воспроизведение

E2E-матрица (около 100 с на этом стенде):

```bash
python3 -m unittest tests.test_checks_e2e -v
# либо точечно:
python3 -m unittest tests.test_checks_e2e.ShopGraphE2ETest.test_ac01_no_git_manual_edits_payments_and_discounts_counters -v
```

Self-pilot (полный прогон, две копии репозитория + контур verifier;
десятки минут — legacy verify и opt-in force прогоняют весь набор):

```bash
python3 docs/tasks/PB-064-evidence/self_pilot.py . /tmp/pb064-pilot-out
# быстрая проверка без долгих прогонов (policy + plan + границы verifier):
python3 docs/tasks/PB-064-evidence/self_pilot.py . /tmp/pb064-pilot-plan --plan-only
```

Артефакты прогона: `commands.json` (все команды и коды), `logs/*.log`,
`policy-applied.json` (политика opt-in копии), `summary.json`
(измерения и границы), `pilot-report.md` (сводка для карточки).

Прогон 2026-11-14 завершён успешно (`SELF-PILOT COMPLETED SUCCESSFULLY`);
сводка измерений и границ — `pilot/pilot-report.md`.

## Ограничения пилота

- Стенд однопользовательский (`st_uid == euid`): `probe` честно сообщает
  `capability=local`; защищённый режим (`protected_confirmed`) здесь не
  достижим и не заявляется. `sandbox-exec` присутствует, но сам по себе
  границей OS identity не является.
- Verifier arena материализует кандидата без service-области `work/`:
  `self-check` (`playbook.py check --ready`) там невыполним, потому что
  активные карточки ссылаются на обязательные материалы
  `work/verification/*`. Поэтому protected-демонстрация rev2 использует
  подмножество command-проверок без `self-check`
  (`preflight-regressions`); полный набор проверок в arena —
  отдельное требование к проектам, перешедшим на protected-контур.
- Политика opt-in построена вручную в скрипте (survey даёт черновик);
  self-project её не принимает — применение только к временной копии.
- `fallback.limits.max_seconds=7200` в политике пилота превышает
  консервативную оценку полного обязательного набора (4680с — сумма
  объявленных таймаутов семи проверок): первый прогон без baseline
  обязан выполнить все проверки, и пилот не занижает лимит ради
  демонстрации. Блокировка по бюджету покрыта отдельно в AC-04
  (`test_ac04_subsystem_boundary_eight_reused_two_run_and_budget_stop`).
- Каталог `test_catalog` насчитывает 1076 уникальных unittest ID
  (1082 экземпляра discovery: `test_verification_smoke.py` через
  `load_tests` повторно исполняет 6 тестов других модулей — как и
  legacy discover; разбиение suite'ов это учитывает).
- Замеры сравнивают одинаковый трёхшаговый сценарий на двух копиях без
  `.git`/`work/`; проценты экономии не заявляются — только фактические
  счётчики команд, файлов, байтов и времени.
