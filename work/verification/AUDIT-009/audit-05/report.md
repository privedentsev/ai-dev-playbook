# AUDIT-009 — повторный независимый аудит audit-05

Вердикт: **changes_requested**. Прежняя A-07 устранена. Новая **A-08 (P1)**: встроенный unittest-адаптер выдаёт passed и готовность для изменённого исходника, фактически исполняя старый Python bytecode. Приёмка/закрытие AUDIT-009 не выполняются; исправление — PB-080.

## Повторный аудит 2026-11-17T20:41:37+03:00; исполнитель: Codex; модель: GPT-6.

Кандидат b11e60b, среда/SHA-256: [candidate.json](candidate.json). Изучены актуальные передачи AUDIT-009/PB-079, общий checks_coverage, защищённая история, runner/adapter и lifecycle. Код и постоянные тесты в этой сессии не исправлялись, архивные задачи не переоткрывались. Снимок карточки/provenance подготовлен штатным prepare-audit.

## A-08 — Реальный повтор исполняет старый .pyc вместо изменённого исходника

Серьёзность: **P1**. Критерии карточки AC-01, AC-02, AC-03; спецификация R-04/R-06/R-07/R-11/R-15, AC-02/AC-07.

Воспроизведение из корня репозитория:

```bash
python3 work/verification/AUDIT-009/audit-05/probe_bytecode_cache_cli.py .
python3 work/verification/AUDIT-009/audit-05/probe_bytecode_cache_cli.py . dont-write
```

Временный проект без Git, реальный setup/apply и subprocess CLI, валидная локальная политика. Mock, подмены отчётов, ручной генерации или редактирования bytecode нет. Первый настоящий run создаёт стандартный Python cache. В тесте неизменный assert(value == 1); тест только записывает наблюдаемое value во внешний файл, не читает его; наблюдения собирает родитель. Системные часы не менялись: сохраняются только mtime конкретного временного исходника, как в сценарии AC-02.

Наблюдения ([обычный env](bytecode-cache-cli.json), [PYTHONDONTWRITEBYTECODE=1](bytecode-cache-dont-write.json)):

1. `src/payments/pay.py`: `value = 1`, первый force — passed; Python сам создаёт `src/payments/__pycache__/pay.cpython-314.pyc`.
2. Исходник меняется на `value = 2` при **одинаковых size/mtime_ns**; SHA-256 различается. Cache не изменяется аудитором.
3. `checks plan` видит `~ src/payments/pay.py`, выбирает **run**, объясняет «входы изменились»; reused=0.
4. Настоящий `checks run --scope task --mode stale` опять **passed/rc=0**, тест наблюдает **1**, `checks status` показывает покрытие, project gate.ok=**true**. Это повторное исполнение, а не reuse результата.
5. Контроль удаляет только cache-файл внутри нашего временного проекта; исходник, тест, политика и прочие условия сохраняются. Следующий force наблюдает **2**, тест **failed/rc=1**. Последовательность наблюдений **[1,1,2]**.
6. `binding.input_digest` второго passed и контрольного failed **одинаков**; изменённый источник уже привязан к новому отпечатку, но факт исполнения старого bytecode этим отпечатком не отражён. Исходный passed имеет другой input_digest.
7. В варианте dont-write первый run создаёт обычный cache, последующие выполняются с PYTHONDONTWRITEBYTECODE=1; false passed и контрольный failed сохраняются. Запрет записи байткода не исключает чтение существующего cache.

[Дополнительная API-проба](bytecode-cache.json) независимо показывает ту же последовательность и закрытие gate после реального failed. Основное CLI-доказательство не использует чтение счётчика самим тестом и сохраняет результаты/binding каждого исполнения.

Причина: `checks_inventory.py:62` исключает __pycache__ как service, но `checks_unittest.collect` (270–299) создаёт обычный Python-процесс и добавляет PYTHONPATH без изоляции cache. `_subprocess_main` (219–225) загружает suite обычным import. Timestamp-valid старый .pyc становится неучтённым фактическим входом исполнения. Проверки стабильности исходников до/после проходят, поскольку новые байты стабильны весь запуск; тесту они не исполняются. Наличие правильных IDs/count/outcomes само по себе не связывает исполненный код с текущим исходником.

Влияние: реальный неуспешный исходник получает свежий passed и положительное покрытие локального gate. Это надёжность local kind=test, **не** заявленный обход реальной protected OS/CI identity. Формальные handoff/accept на cache-фикстуре не запускались. Protected kind=test блокируется отдельным контрактом.

Критерий исправления: исполнять текущий исходник в доказанном контексте либо безопасно блокировать такую проверку; не удалять пользовательские кэши и не маскировать дефект одним -B. Проверить cache продуктовых модулей и самих тестов, сохранить законный reuse. Старые unsafe passed не должны автоматически стать пригодными для нового контракта без фактического повторения; идентичность adapter/контекста и миграция должны это учитывать. Постановка — PB-080 (без зависимости от незавершённой AUDIT-009).

## Независимая матрица и исходные замечания

| AC спецификации | Проверка и фактический результат |
|---|---|
| AC-01 | ShopGraphE2ETest: no-Git, payments/checkout/discounts, настоящие subprocess-счётчики — passed. |
| AC-02 | Add/delete/rename, content при равных mtime/size, циклы/import/dynamic/fallback — штатные E2E passed. Дополнительная cache-проба выявила A-08: источник выбран заново, но исполняется старый код. |
| AC-03 | Logs/card projection/PNG/archive/unknown — штатные InputsExclusionsFallbackBudgetE2ETest passed. A-08 показывает, что игнорируемый service cache может оказаться фактическим входом. |
| AC-04 | Восемь независимых/two stale, subsystem/project fallback и budget stop — E2E passed. В реальном self-project транзитивный план выбрал все 7 проверок по зависимостям; независимость block-набора не заявляется. |
| AC-05 | Release/cross-task/stale/force, command/environment/fixture/TTL, late failed — штатный набор passed. |
| AC-06 | Zero/missing/duplicate/skip/corrupt report и IDs/descriptions — passed. Cache-сценарий имеет настоящий правильный test ID и реальный passed старого кода, не поддельный отчёт. |
| AC-07 | Race/drift/arena/receipt tamper — штатные E2E passed; связь с актуальными байтами нарушена A-08. |
| AC-08 | Forgery/replay/revocation/policy weakening и protected/local граница — passed на локальных стендах. Реальная отдельная OS identity не проверялась. |
| AC-09 | Manual/task criteria/handoff/archive provenance — passed; на временной копии реальные handoff/audit-ready и отказ close без приёмки выполнены. |
| AC-10 | Legacy/opt-in/read-only/unsupported/update/restore — passed. Старая corrupt/delete/legacy-история блокирует готовность. |
| AC-11 | Полный новый self-pilot завершён rc=0, все его сценарии/команды выполнены. Измерения ниже; это не снимает A-08, которой в его сценариях нет. |

- **365 tests/checks, OK, 260.783 с** ([лог](checks-tests.log)); включает 11 регрессий PB-079, e2e, trust, gate и lifecycle/legacy.
- Полный штатный **verify AUDIT-009 passed**, **1134 теста OK, 1993.169 с**; self-check/preflight-regressions/unit-tests passed, inputs_unchanged=true ([оригинал отчёта](../84693ca6b4d149da9bad2908c0b2e8b5/report.json), [unit log](../84693ca6b4d149da9bad2908c0b2e8b5/unit-tests.stderr.log)). Прогон выполнен до оформления A-08/PB-080; это историческое успешное выполнение настроенного набора, не положительная передача текущего отрицательного аудита.
- A-07: обе подписи валидны, кандидат общий, task/project/verifier gate дают uncovered, CLI rc=1 ([JSON](verifier-gate.json)). Реальное перекрытие с задержанной выдачей раннего passed также блокируется, новый passed восстанавливает покрытие ([JSON](protected-overlap.json)).
- A-06: gate=false и stale действительно повторяет исполнение, attempts=3 ([JSON](rollback.json)). Утрата nf, rebuild, исцеление и последующий rollback сохраняют блокирующее свидетельство ([loss](loss.json), [healed/rebuild](healed-rebuild.json)). Старые A-04/A-05 не воспроизводятся ([corrupt](old-corrupt.json), [delete](old-delete.json), [legacy](old-legacy.json)).

## Полный self-pilot и стоимость

Неизменённый harness из архивной PB-073 запущен на предварительно побайтно сверенной временной копии кода b11e60b. [Сверка копии](source-snapshot-match.json), [журнал](pilot-full.log), [все команды](pilot-full/commands.json), [сводка](pilot-full/summary.json). Поле task_id=PB-073 в сводке — историческая метка harness; этот новый прогон принадлежит AUDIT-009, архив PB-073 не переоткрывался.

| Сценарий | Режим | Выполнено проверок | Учтено обработок файлов | Учтено байт | wall_seconds |
|---|---|---:|---:|---:|---:|
| Базис | legacy | 3 | 7208 | 276670772 | 1924.802 |
| Базис | opt-in | 7 | 5729 | 180008239 | 1875.462 |
| Локальная правка | legacy | 3 | 7208 | 276670918 | 1987.130 |
| Локальная правка | opt-in | 1 | 4554 | 157188488 | 258.100 |
| Неизменные входы | legacy | 0 | 3604 | 138335459 | 94.020 |
| Неизменные входы | opt-in | 0 | 4891 | 163624451 | 4.777 |

Локальная правка — marker в tests/test_checks_setup.py без изменения test IDs. Opt-in реально исполнил только checks-unit-tests (365 тестов); self-check reused, остальные проверки в task-scope не применимы. Неизменный project/release-повтор переиспользовал все 7 результатов без исполнения. Legacy после изменения повторил 3 настроенные команды, а на неизменном повторе не запускал команды.

Границы измерений: число проверок — число внешних check-команд/групп, **не** общее число OS-процессов. Учёт хеширования — before/after входы legacy-отчётов, фактические counters отдельного plan и контрольный пересчёт входных снимков runner; это не сплошной profiler всех операций I/O. Обработки файлов могут считать один путь несколько раз; объёмы основаны на физических размерах входов. wall_seconds приведён как в summary: для локального opt-in включает plan+run, для initial/unchanged — run; отдельные setup/plan затраты сохранены в сырых данных. Это единичный воспроизводимый сценарий, без обещания процента экономии для будущих проектов.

Discovery: 1134 экземпляра/1128 уникальных IDs (6 load_tests shared IDs). Подтверждены setup preview read-only, opt-in apply, полный force, локальный stale, транзитивный plan, release-ready, handoff, audit-ready, отказ close без acceptance. **handoff_idempotent=false**: обе команды повторной legacy-передачи действительно записали передачу; идемпотентность этим прогоном не подтверждается и не заявляется. Harness использует metadata schema_version=1/card_format_version=2; его legacy handoff обновляет at при повторе. Флаг block_unit_tests_reused=false также сохранён: транзитивный план выбрал block по связи с core, а не доказал его независимость. Ошибкой поведения это здесь не объявляется.

Источник harness-копии не изменён, source checks_policy не включён. Протокол тестового command-receipt проверен; capability=local, protected release-ready отказал без отдельной OS identity. Результаты полного пилота и широких тестов не покрывают A-08: обычная marker-правка отличается от same-size/same-mtime cache-сценария.

## Процедура и следующий шаг

Начальный audit-ready был отвергнут собственным параллельным prepare-audit (ошибка подготовки аудитора); повтор после завершения операции честно отказал по in_progress/отсутствию передачи. До полного verify подготовлены in_review/EXEC-05 и фактические статусы документации. После подтверждения A-08 карточка возвращена в in_progress; новые положительные CHECK/HANDOFF/acceptance не создаются. Итог сохраняется как failed CHECK и diagnostic lifecycle; cancelled/diagnostic означает отрицательный протокол, не отмену самой карточки.

Формальный root verify не повторяется ради уже доказанного false passed. Спецификация остаётся implemented, без promotion/приёмки блока; план не архивируется. Продуктовый код/постоянные тесты не менялись, прежний архив и исходные чужие изменения сохранены. Отдельная OS/CI identity не проверялась. Следующий шаг — отдельная реализация и независимая приёмка PB-080, затем повтор AUDIT-009.
