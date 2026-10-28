# Карта кода

| Область | Путь | Назначение |
|---|---|---|
| CLI | `ai-dev-playbook/scripts/playbook.py` | Установка, проверка, индексы задач и документов, verify, close, adopt, update, document create/register/impact/materials/list |
| Исполнение проверок | `ai-dev-playbook/scripts/verification.py` | Локальный runner и отчёты |
| Обновление | `ai-dev-playbook/scripts/updating.py` | Manifest, adopt, план/применение/восстановление обновления |
| Файловые операции | `ai-dev-playbook/scripts/file_operations.py` | Общий gate изменяющих команд, staged-валидация, backup-index, журнал, pending и восстановление операций документов |
| Формат карточек | `ai-dev-playbook/scripts/task_cards.py` | Чтение и валидация Markdown v2, ID/связи/находки, хеши истории, общий расчёт чекбоксов и нормализация; `card show --json`, fallback на исходный Markdown |
| Каталог документов | `ai-dev-playbook/scripts/documents.py` | Метаданные, коллекции, связи и владельцы материалов; карта moves, immutable, рендер/проверка индексов; read-only |
| Ссылки документов | `ai-dev-playbook/scripts/document_links.py` | Разбор Markdown-ссылок, check/rewrite по карте переносов, конфликты, origin_path для immutable |
| Жизненный цикл | `ai-dev-playbook/scripts/lifecycle.py` | `lifecycle plan|apply|status|restore|handoff|accept`; действия `archive_document`, `close_task`, `reopen`, `repair_task_materials`, `promote_change`: переносы документов/карточек и материалов, записи передачи/приёмки, проверка свежести входов verify, граф статусов — по детерминированному плану через `file_operations` |
| Переходы версий | `ai-dev-playbook/updates.json` | Объявленные `updates_from` с шагами миграций |
| Manifest установки | `ai-dev-playbook/manifest.json` | Управляемые файлы, эталонные хеши, kept; пишется CLI |
| Правила | `ai-dev-playbook/rules/` | Переносимый процесс; контракт обновления — `update.md` |
| Шаблоны | `ai-dev-playbook/templates/` | Начальные документы и конфиги; `templates/documents/<kind>.md` — тела для `document create` |
| Настройки этого проекта | `ai-dev-playbook/project.json`, `resources.json` | Пути, команды, проверки verify, ресурсы и Git |
| Отчёты проверок | `work/verification/` | Логи и `report.json` запусков `verify`; игнорируется Git |
| Операции обновления | `work/update/` | Сохранённый план, журналы, staged-копии, резервные копии |
| Операции документов | `work/lifecycle/` | Сохранённые планы `plans/`, журналы, staged-копии, backup-индексы, `restore.py`, маркер pending; игнорируется Git |
| Блокировка команд | `work/gate.lock` | Общий неблокирующий lock изменяющих команд; токен вложенности `PLAYBOOK_GATE` |
| Тесты | `tests/` | unittest: логика (`test_playbook.py`), сценарии CLI (`test_cli.py`), каталог документов (`test_documents.py`), ссылки и индексы (`test_document_links.py`), восстановление и gate (`test_file_operations.py`), CLI документов (`test_document_cli.py`), архивный lifecycle (`test_lifecycle.py`), переходы карточек, передача/приёмка и promotion (`test_close_lifecycle.py`), регистрация материалов и ранний handoff (`test_material_registration.py`), исторические остатки и восстановление (`test_material_repair.py`) |
| CI | `.github/workflows/checks.yml` | unittest и `check --ready` на push и pull request |

Миграций в репозитории нет. Структурная проверка: `python3 ai-dev-playbook/scripts/playbook.py check --ready`. Команда тестов указана в `project.json → commands.test`.

- Контекст карточек: `documents.py` — чтение пары/сериализация; `task_cards.py` — компактный указатель/выбор записей; `playbook.py` — card create/show; `lifecycle.py` — парные операции; `tests/test_compact_context.py` — поведенческие проверки внешнего хранения и чтения.

- Передача одним вызовом: `task_cards.py` — handoff_view/render_handoff, `playbook.py` — card show --handoff; `tests/test_handoff_context.py` — выбор исходных записей, read-only, fallback/архив и один verify с подготовленными статусом/индексом.

PB-054: file_operations.preview_operation использует те же staged-checks без записи проекта; lifecycle.operation_spec/preview_plan разделяют построение операции и применение. lifecycle.prepare_audit_command/portable_materials/verification_preflight отвечают за контейнер и раннюю проверку. verification.reusable_run/runtime_identity — явный reuse полного legacy-прогона. documents.audit_packages хранит независимую привязку provenance. Поведенческие регрессии: tests/test_audit_material_safety.py.
