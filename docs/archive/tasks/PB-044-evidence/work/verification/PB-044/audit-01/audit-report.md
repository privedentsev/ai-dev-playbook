# PB-044 — независимый аудит HANDOFF-02

Результат: accepted. Блокирующих замечаний нет. Аудитор: Codex (независимый аудитор), GPT-6.1 Sol (High); отдельный субагент по поручению инженера.

- AC-01: независимо запущены 13 сценариев test_material_repair, 78 test_document_links и 38 test_close_lifecycle; все 129 прошли. Обнаружение evidence по обеим сторонам архива, обычные close/reopen и прежние gate воспроизведены.
- AC-02: 5 отдельно написанных CLI-проб подтверждают batch done/cancelled, сохранность произвольных бинарных байтов/путей с пробелами/пустых каталогов, Status/Closed и прежней атрибуции, обновление входящей ссылки и immutable-владение без новой приёмки. work/verification остаётся нетронутым; для отдельного reopen тестовый незарегистрированный work-артефакт удалён самой пробой.
- AC-03: воспроизведены read-only preview, повтор apply без изменения дерева, stale content, symlink корня источника, занятый файл назначения, невозможность скрыть residue невыбранной задачи. Постоянные регрессии дополнительно покрывают active/managed/foreign-owner, nested symlink и rollback полного дерева при final-check failure.
- AC-04: отдельное сравнение с baseline.json: 119/119 SHA-256 и размеров совпадают, исходные файлы и 6 каталогов отсутствуют, Status/Closed шести архивных карточек совпадают с baseline.

Свежесть: CLI HANDOFF-02 имеет состояние current. Полный verify e36bf53c778f4fb2abfdd7f5e5c1ab87 имеет status=passed, inputs_unchanged=true; unit-tests и self-check вернули 0. SHA-256 семи входов независимых проб повторно сверены после handoff и совпали. До AUDIT/accept close --dry-run корректно отказал: «нет последнего accepted-аудита».

В собственной первой фикстуре аудитора Closed был раньше Created; CLI правильно отклонил её до проверки реализации. Исходный скрипт и оба исходных лога сохранены. После исправления только Created все 5 независимых проб прошли. Это ошибка фикстуры аудитора, не замечание к PB-044.

Ограничения: macOS/Python 3.14; SIGKILL отдельно не воспроизводился, аварийное восстановление проверено rollback-пробой общего engine. Общий аудит AUDIT-008 не проводился. На момент содержательного аудита реальный close, commit, push и release не выполнялись. Последующее закрытие разрешено отдельной командой инженера после успешных accept и dry-run.

Воспроизведение из корня проекта:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_material_repair.py -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_document_links.py -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_close_lifecycle.py -v
PYTHONDONTWRITEBYTECODE=1 python3 work/verification/PB-044/audit-01/independent_probes.py
PYTHONDONTWRITEBYTECODE=1 python3 work/verification/PB-044/audit-01/migration_probe.py
```

Скрипты ссылаются на исходный локальный root; при переносе checkout замените константу REPO/root перед воспроизведением в отдельном временном каталоге, сохранив исходные файлы доказательств.
