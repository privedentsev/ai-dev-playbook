# PB-080 — реализация и проверки

Среда: macOS, Python 3.14.6, локальные временные проекты без Git. Реальная отдельная OS/CI identity не проверялась.

Исправление: каждый collect создаёт приватный пустой pycache-prefix до старта Python, запрещает запись через -B и env; обычные дочерние Python наследуют контекст. Кэши пользователя не удаляются. Adapter identity/version 2 инвалидирует reuse и gate для старых результатов version 1; JSON-канал protocol 1 сохранён.

## Доказательства

- [До исправления](before.json): реальный исходный CLI-контрпример AUDIT-009, наблюдения [1,1,2], stale rc=0 и gate.ok=true. Исходный probe сохранён в work/verification/AUDIT-009/audit-05.
- [После](after.json), [dont-write](after-dont-write.json), [probe](probe_bytecode_cache_cli.py): естественный cache предварительно создаётся обычным unittest, затем новый адаптер выполняет [1,2,2], stale и force rc=1, gate.ok=false. Контроль также сохраняет cache. Не редактируются pyc, expected/actual IDs и исходы.
- [Полный checks-набор](checks-tests-final.log): 368 tests, OK, 300.291 s. Команда: `python3 -m unittest discover -s tests -p 'test_checks*.py'`. Включает unittest/run/gate/select/e2e/trust/verifier-history и три новых регрессии.
- tests/test_checks_bytecode.py: 12 комбинаций source/test × direct/package × normal/dont-write/inherited-prefix. Cache естественный, редактирование сохраняет size/mtime_ns, реальные subprocess исполняют новый код, cache побайтно сохранён. Также обычное изменение size/mtime; CLI родитель с -B; stale/force failure, old-identity gate, новый фактический run и законный reuse/cross-task/release. Миграционная регрессия меняет только identity первого реального collect через mock; результаты/исходы не подделываются.
- [Первый широкий прогон](checks-tests.log) отрицательный: незавершённые assertions новых тестов, запрет вложенного sandbox-exec внешней песочницей и совпадение с root index-транзакцией. После исправления тестов повтор вне внешнего sandbox, без параллельных root-транзакций — 368/368 passed. Сохранён без выдачи за продуктовую регрессию.
- [Архив](archive-sha256.json): 3085 tracked файлов; git diff HEAD -- docs/archive пуст. При завершении сверить текущие хеши с этим снимком. Архивные карточки и evidence не переписывались.

## Границы

Исправление касается обычного файлового source-backed импорта. Sourceless pyc, native extensions, zip/custom loaders, явное чтение bytecode, child с переопределённым env/argv остаются ограничениями; значимые внешние зависимости требуют policy binding и консервативного отказа при неизвестной связи. Это не защита от враждебного тестового кода; protected kind=test по-прежнему заблокирован. Новая независимая приёмка PB-080 и затем повтор AUDIT-009 ещё необходимы.

Штатный полный legacy verify фиксируется отдельно в work/verification/PB-080/<run_id>/report.json; этот отчёт его не подменяет.
