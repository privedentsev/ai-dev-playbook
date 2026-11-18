# Независимый аудит PB-080

Дата: 2026-11-17. Исполнитель: Codex. Модель среды: GPT-6. Запрошенная инженером GPT-6.1 Sol (Medium) в этой сессии не переключалась. Режим: аудит, отдельная от реализации сессия. Результат: accepted, замечаний нет.

## Проверено независимо

- До проверки `verify PB-080 --audit-ready` подтвердил свежую HANDOFF-01 и исходный полный verify. Прочитаны требования AC-01–04, EXEC/CHECK/HANDOFF, diff адаптера, контракта и тестов, ограничения в verification/design. Полный verify реализации: passed, inputs_unchanged=true, 1137 тестов; заново весь набор не запускался.
- [Bytecode-регрессии](bytecode-tests.log): 3/3 OK, 28.388 s. Матрица из 12 сочетаний source/test, direct/package, normal/dont-write/inherited-prefix; текущий код исполняется при прежних size/mtime, пользовательские caches сохраняются, обычные изменения проходят. Миграционный тест действительно запускает проверку с прежней identity (через mock только identity), gate отклоняет её; новая identity требует исполнения, затем reuse работает.
- [Адресные регрессии](targeted-tests-unsandboxed.log): 170/170 OK, 28.780 s; AdapterTest и остальные test_checks_run, test_checks_gate, test_checks_select, test_checks_contract. Повтор вне внешней песочницы нужен для штатного sandbox-exec verifier.
- Исходный реальный CLI-контрпример повторён в вариантах [normal](cli-normal.json) и [dont-write](cli-dont-write.json). Естественный timestamp-valid pyc не редактируется/не удаляется; SHA исходника меняется при прежних size/mtime. Обе пробы наблюдают [1,2,2], stale/force rc=1, gate.ok=false; результаты не reused. Положительный CLI-контроль cross-task/release находится в BytecodeCliTest и выполнен независимо.
- Дополнительная [собственная проба](probe_child.py), [результат](independent-child.json): обычный дочерний Python импортирует source-backed зависимость из отдельного каталога PYTHONPATH при естественном старом pyc и прежних size/mtime. Наблюдается 2, assert(1) падает; пользовательский cache побайтно сохранён, приватный prefix удалён. Код адаптера задаёт -B/-X и обе переменные до старта Python; TemporaryDirectory охватывает возврат из _collect, включая ошибки/timeout.
- Identity 2 проверяется общей оценкой reuse в checks_select; gate использует её же. JSON-протокол остаётся 1. Доказательства версии 1 не покрывают новый контракт, protected kind=test остаётся запрещённым.
- [Архив](archive-check.json): все 3085 исходных tracked файлов совпали с сохранёнными SHA-256. [Self-manifest](manifest-check.json): все 58 путей совпали с полными sha256. git diff --check чист.

## Воспроизведение

Из корня проекта:

```sh
python3 -B -m unittest discover -s tests -p test_checks_bytecode.py -v
PYTHONPATH=tests:ai-dev-playbook/scripts python3 -B -m unittest test_checks_run test_checks_gate test_checks_select test_checks_contract -v
python3 -B docs/tasks/PB-080-evidence/probe_bytecode_cache_cli.py .
python3 -B docs/tasks/PB-080-evidence/probe_bytecode_cache_cli.py . dont-write
python3 -B work/verification/PB-080/audit-01/probe_child.py .
```

После close пути материалов перемещаются в архив; исходные команды приведены для аудируемого состояния, исходный probe реализации и собственный probe сохраняются с материалами.

## Отрицательные запуски и ограничения

[Первый запуск](initial-blocked-tests.log) совпал с моей активной prepare-audit транзакцией: CLI init отказал по pending marker, две API-регрессии прошли. После окончания транзакции весь bytecode-файл прошёл. [Первый расширенный запуск](targeted-tests.log) включал ошибочное имя несуществующего test_checks_unittest (адаптер проверяется в test_checks_run); остальные отказы protected-тестов возникли во внешней песочнице. Исправлен только список запуска, повтор вне неё дал 170/170 OK. Продуктовый код не менялся.

Проверено локальное source-backed исполнение; не удостоверяются sourceless/zip/custom/native loaders, явное переопределение контекста, враждебный тест и реальная отдельная OS/CI identity. Значимые внешние зависимости по-прежнему требуют policy binding. Совместный AUDIT-009 этим аудитом не заменяется и не принимается. Прежний архив не переписывается. Changelog записывается после успешного штатного close как итог приёмки; исходники комплекта не меняются.
