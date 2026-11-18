# AUDIT-009 — повторный независимый аудит audit-06

Вердикт: **changes_requested**. A-08 устранена. Новая **A-09 (P1)**: unittest-адаптер игнорирует Python одобренной команды и выдаёт passed в другой среде. AUDIT-009 остаётся in_progress; приёмка, close и promotion не выполняются. Исправление — [PB-081](../../../../docs/tasks/PB-081.md).

## Повторный аудит 2026-11-18T05:13:24+03:00; исполнитель: Codex; модель: GPT-6.

Кандидат b6f20ea; точные SHA-256 кода/тестов, среда и ветка — [candidate.json](candidate.json). Независимая сессия относительно реализации PB-057–064 и исправлений. Уточнение инженера: текущая и прежние аудиторские сессии — GPT-6.1 Sol (High); это сообщение инженера, а не проверенная runtime-атрибуция. Среда текущего агента сообщает GPT-6. Прежняя история/атрибуция не переписана.

## A-09 — одобренный Python подменяется интерпретатором родителя

Серьёзность: **P1**. Критерии карточки: AC-01, AC-02, AC-03. Требования: R-11/R-13/R-15/R-19, спецификация AC-05/AC-07.

Воспроизведение: `python3 work/verification/AUDIT-009/audit-06/probe_interpreter.py .`.

[Программа](probe_interpreter.py) создаёт настоящий временный no-Git проект и stdlib venv без pip. Политика содержит абсолютный `approved-venv/bin/python3 -m unittest discover -s tests -p test_payments.py`; CLI setup apply принимает её. Тест записывает executable/prefix/base_prefix и проверяет равенство prefix/base_prefix. История/отчёты не редактируются, mock нет.

[Факты](interpreter.json):

1. Прямое выполнение ровно одобренного argv — rc=1, assertion failed; sys.executable указывает venv, prefix отличается от base_prefix.
2. `checks run DEV-01 --scope project --mode force` — rc=0, настоящий test ID и outcome=passed, adapter version=2. Наблюдаемая среда — системный Python родителя, prefix=base_prefix.
3. Project gate.ok=true, проверка payments-tests покрыта. Это ложный passed относительно одобренной команды, а не reuse или подделка отчёта.

Причина: checks_unittest.py:65–68 проверяет только имя python*, а _collect (282–284) заменяет argv[0] на sys.executable. Запись definition_digest связывает результат с одобренным argv, хотя фактически исполняется иной runtime. Виртуальные окружения могут иметь тот же бинарник через symlink при другом sys.prefix/site-packages; проверка одного realpath не решает проблему.

Влияние: тесты проходят в другой среде/с другими зависимостями, чем назначено политикой; локальный gate считает их доказательством. Protected kind=test остаётся запрещённым; обход реальной OS/CI границы не заявляется. Формальные handoff/accept на этой фикстуре не запускались.

Критерий исправления: исполнять объявленный Python с корректной bytecode-изоляцией либо явно отклонять несовместимую форму; не выдавать passed для другой среды. Проверить absolute venv/PATH/missing interpreter, привязку runtime, миграцию unsafe passed и законный reuse. [PB-081](../../../../docs/tasks/PB-081.md) не зависит от незавершённой AUDIT-009.

## Независимые проверки

- `python3 -m unittest discover -s tests -p 'test_checks*.py' -v`: **368 tests OK, 307.262 с**, [чистый лог](checks-tests.log). Выполнен вне внешней песочницы после завершения всех собственных lifecycle-транзакций; вложенный sandbox-exec работает. Набор содержит настоящие CLI/subprocess counters и отрицательные сценарии, не только структурную проверку.
- A-08: [normal](bytecode-normal.json) и [dont-write](bytecode-dont-write.json) наблюдают **[1,2,2]**, stale/force rc=1, gate=false; естественные пользовательские caches сохранены.
- A-04/A-05: [corrupt](old-corrupt.json), [delete](old-delete.json), [legacy](old-legacy.json) сохраняют закрытый gate; stale повторяет исполнение, attempts=3.
- A-06: [rollback](rollback.json) сохраняет task/project gate=false; stale реально повторяет исполнение, attempts=3.
- A-07: [чистый verifier](verifier-gate-unsandboxed.json) реально исполнил passed→failed; подписи валидны, task/project/reference gate=false, CLI gate rc=1. Первый [песочничный запуск](verifier-gate.json) получил средовые rc=71 и не считается продуктовым доказательством passed→failed.
- A-09 воспроизведена [повторно](interpreter-repeat.json). Дополнительный контроль запускает сам playbook CLI через одобренный venv Python: rc=1, наблюдаемый prefix=venv, gate=false. Политика и тест не меняются; смена Python родительского CLI меняет исполнение вопреки закреплённому argv.
- [Plan-only self-pilot](pilot-plan.log): rc=0, PLAN-ONLY OK; обнаружены 1137 экземпляров/1131 уникальный test ID (6 shared load_tests IDs), получена настоящая command-квитанция. capability=local; protected kind=test и protected release-ready отклонены. [Команды](pilot-plan/commands.json). Это проверка воспроизводимости подготовки, **не** новый полный пилот и не новое измерение экономии.
- [Сверка сохранности](preservation.json): все **3121** архивных tracked файла и **167** файлов кода/тестов совпадают с исходными SHA-256; исходные изменения четырёх общих индексов/документов побайтно сохранены по git diff.

| AC спецификации | Независимый результат |
|---|---|
| AC-01 | ShopGraphE2ETest: no-Git, ручные правки payments/discounts, транзитивные checkout/cart, реальные counters — passed. |
| AC-02 | Add/delete/rename/import/cycle/dynamic/missing baseline/same-size-mtime — E2E passed; отдельная bytecode-проба PB-080 — passed. |
| AC-03 | InputsExclusionsFallbackBudgetE2ETest: log/card projection/PNG/archive/unknown — passed. |
| AC-04 | Eight reused/two run, subsystem/project fallback и budget stop — E2E passed. |
| AC-05 | Штатные release/cross-task/stale/force/TTL/command/environment/late-failed — passed; новый реальный venv-сценарий **выявил A-09**, неверно исполняется одобренная команда. |
| AC-06 | Zero/missing/duplicate/skip/corrupt-report/description/reused-time — passed. A-09 имеет настоящий правильный ID и исход другого runtime. |
| AC-07 | Штатные race/drift/arena/receipt tamper — passed; A-09 нарушает связь одобренного исполнения и доказательства среды. |
| AC-08 | Forgery/replay/revocation/policy weakening/local-protected — passed на локальных стендах; отдельная OS/CI identity не проверялась. |
| AC-09 | Manual/task-specific/unified gate/provenance/terminal report — E2E passed. |
| AC-10 | Legacy/opt-in preview/update/unsupported/recovery/corrupt history — passed. |
| AC-11 | Свежий plan-only воспроизводим; прежний полный пилот audit-05 остаётся историческим измерением. Новый полный пилот не выполнялся при A-09; обещание ускорения не добавлялось. |

Штатные 368 успешных тестов не покрывают новый контрпример и не снимают A-09.

## Процедура и ограничения

Начальный `verify AUDIT-009 --audit-ready` честно отказал: карточка in_progress, актуальной передачи нет. Фиктивная положительная HANDOFF не создаётся. Существующий audit-06 подтверждён через prepare-audit --actor Codex; первый вызов без actor отказал из-за отличающегося автора по умолчанию и ничего не переписал.

Первый тестовый прогон во внешней песочнице остановлен аудитором для чистой проверки вложенного sandbox-exec. Второй пересёкся с собственной CLI-транзакцией создания PB-081 и получил 30 процессных отказов; он исключён из продуктовых доказательств и сохранён отдельно. Первый plan-only с выходным путём внутри work отклонён ограничением неизменённого harness; это ошибка подготовки аудитора, не новая находка.

Продуктовый код, постоянные тесты и архив не исправляются. Полный legacy verify и полный дорогой self-pilot при доказанной A-09 не запускаются; прежние результаты audit-05/PB-080 не объявляются новыми. Реальная отдельная OS/CI identity не подтверждена. Следующий шаг — отдельная реализация и независимая приёмка PB-081, затем новый AUDIT-009.

## Фиксация результата

Результат сохранён в CHECK-06 карточки и зарегистрирован через `lifecycle accept AUDIT-009 --result cancelled --kind diagnostic --actor Codex --model GPT-6`: это отрицательный протокол, не отмена AUDIT-009 и не приёмка. Карточка остаётся in_progress. Спецификация не promoted, общий план не архивирован. Индексы и check --ready проверяются отдельно как структура; они не заменяют поведение CLI. Комплект не изменён, adopt не требуется.

Структурные итоги: `index` — индексы актуальны; [check --ready](check-ready.log) — rc=0, 92 карточки/31 документ/2 legacy; [verify --preflight](preflight.log) — rc=0, ранняя проверка материалов/переносимости пройдена, команды тестов не запускались. `git diff --check` — чисто. Эти проверки не подменяют 368 настоящих тестов и отдельный CLI-контрпример.
