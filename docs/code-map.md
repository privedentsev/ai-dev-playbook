# Карта кода

PB-037: `tests/test_workflow_e2e.py` объединяет ручные три итерации с устаревшим candidate, независимые счётчики и add/delete входов, small/medium/large с архивом и отдельную release checkpoint. Воспроизводимый self-project pilot — `docs/tasks/PB-037-evidence/self_pilot.py`; opt-in и миграция выполняются только на временной копии с сохранением обязательных команд.

PB-036: `playbook.py` содержит workflow_settings, workflow_enable, workflow_migrate_card и init --workflow-v2; `templates/verification.json` задаёт стартовую структурную проверку. `updating.load_artifact` читает updates schemas 1/2 с minimum_cli_version. `tests/test_workflow_install.py` проверяет реальные локальные переходы, отказ старого CLI, opt-in, перевод карточек с историей, custom paths, конфликты, прерывания и автономный restore.

| Область | Путь | Назначение |
|---|---|---|
| CLI | `ai-dev-playbook/scripts/playbook.py` | Установка, проверка, индексы задач и документов, verify, close, adopt, update, document create/register/impact/materials/list |
| Политика проверок v2 | `ai-dev-playbook/scripts/verification_policy.py` | Чистые функции обязательности, semantic projections, явных snapshots, freshness/reuse и coverage; подключены к runner v2 (PB-032, PB-033) |
| Контракт совместной приёмки | `ai-dev-playbook/scripts/block_workflow.py` | Версии схем политики/ссылок/review package/findings/block receipt, валидация графа блока, общий evaluator readiness и `block status`; `policy preview\|apply` в playbook.py (PB-065) |
| Контракт выбора проверок | `ai-dev-playbook/scripts/checks_contract.py` | Версии схем policy/graph/result/receipt, контракт адаптера и классификация правок политики; `checks plan\|run\|status`, `setup preview\|apply` и ссылка `checks_policy` в playbook.py (PB-057) |
| Файловый слой проверок | `ai-dev-playbook/scripts/checks_inventory.py` | Inventory без Git и снимки входов: классификация модулей, исключения с причиной, потоковое хеширование раз на снимок, контроль гонок и консервативный diff; fixtures `tests/fixtures/checks_inventory/` (PB-058) |
| Граф и выбор проверок | `ai-dev-playbook/scripts/checks_graph.py`, `ai-dev-playbook/scripts/checks_select.py` | Python AST-анализатор и `checks-graph.json` с union прежнего снимка; обратное замыкание, расширение до доказанной границы, explain и бюджет; read-only `checks plan` в playbook.py (PB-059) |
| Runner и результаты проверок | `ai-dev-playbook/scripts/checks_runner.py`, `checks_unittest.py`, `checks_results.py` | Адаптер `python-unittest` с JSON-отчётом и сверкой каталога; append-only результаты `work/checks/results/` и производный индекс; `checks run\|status` со scope/mode, внешней свежестью и локальным контролем входов до/после (PB-060) |
| Исполнение проверок | `ai-dev-playbook/scripts/verification.py` | Legacy и v2 runner, read-only preflight, доверенные команды, ручные candidate и выборочное reuse (PB-033) |
| Обновление | `ai-dev-playbook/scripts/updating.py` | Manifest, adopt, план/применение/восстановление обновления |
| Представление проекта | `ai-dev-playbook/scripts/project_view.py` | Read-only ProjectView/FutureView, отслеживание прочитанных байтов/отсутствия/состава коллекций, streaming-подготовка отдельных payload; рабочие команды пока используют прежний staged |
| Файловые операции | `ai-dev-playbook/scripts/file_operations.py` | Общий gate изменяющих команд, staged-валидация, backup-index, журнал, pending и восстановление операций документов |
| Формат карточек | `ai-dev-playbook/scripts/task_cards.py` | Чтение и валидация Markdown v2, ID/связи/находки, хеши истории, общий расчёт чекбоксов и нормализация; `card show --json`, fallback на исходный Markdown |
| Каталог документов | `ai-dev-playbook/scripts/documents.py` | Метаданные, коллекции, связи и владельцы материалов; карта moves, immutable, рендер/проверка индексов; read-only |
| Ссылки документов | `ai-dev-playbook/scripts/document_links.py` | Разбор Markdown-ссылок, check/rewrite по карте переносов, конфликты, origin_path для immutable |
| Жизненный цикл | `ai-dev-playbook/scripts/lifecycle.py` | `lifecycle plan|apply|status|restore|start|route|revise|handoff|accept|register-material|relocate-material`; действия `archive_document`, `close_task`, `reopen`, `repair_task_materials`, `promote_change`: переносы документов/карточек и материалов, записи передачи/приёмки, проверка свежести входов verify, граф статусов — по детерминированному плану через `file_operations` |
| Переходы версий | `ai-dev-playbook/updates.json` | Объявленные `updates_from` с шагами миграций |
| Manifest установки | `ai-dev-playbook/manifest.json` | Управляемые файлы, эталонные хеши, kept; пишется CLI |
| Правила | `ai-dev-playbook/rules/` | Переносимый процесс; контракт обновления — `update.md` |
| Шаблоны | `ai-dev-playbook/templates/` | Начальные документы и конфиги; `templates/documents/<kind>.md` — тела для `document create` |
| Настройки этого проекта | `ai-dev-playbook/project.json`, `resources.json` | Пути, команды, проверки verify, ресурсы и Git |
| Отчёты проверок | `work/verification/` | Логи и `report.json` запусков `verify`; игнорируется Git |
| Операции обновления | `work/update/` | Сохранённый план, журналы, staged-копии, резервные копии |
| Операции документов | `work/lifecycle/` | Сохранённые планы `plans/`, журналы, staged-копии, backup-индексы, `restore.py`, маркер pending; игнорируется Git |
| Блокировка команд | `work/gate.lock` | Общий неблокирующий lock изменяющих команд; токен вложенности `PLAYBOOK_GATE` |
| Тесты | `tests/` | unittest: логика (`test_playbook.py`), сценарии CLI (`test_cli.py`), каталог документов (`test_documents.py`), ссылки и индексы (`test_document_links.py`), восстановление и gate (`test_file_operations.py`), CLI документов (`test_document_cli.py`), архивный lifecycle (`test_lifecycle.py`), коллизии назначений и перенос материала (`test_material_relocation.py`), переходы карточек, передача/приёмка и promotion (`test_close_lifecycle.py`), регистрация материалов и ранний handoff (`test_material_registration.py`), исторические остатки и восстановление (`test_material_repair.py`) |
| CI | `.github/workflows/checks.yml` | unittest и `check --ready` на push и pull request |

Миграций в репозитории нет. Структурная проверка: `python3 ai-dev-playbook/scripts/playbook.py check --ready`. Команда тестов указана в `project.json → commands.test`.

- Контекст карточек: `documents.py` — чтение пары/сериализация; `task_cards.py` — компактный указатель/выбор записей; `playbook.py` — card create/show; `lifecycle.py` — парные операции; `tests/test_compact_context.py` — поведенческие проверки внешнего хранения и чтения.

- Передача одним вызовом: `task_cards.py` — handoff_view/render_handoff, `playbook.py` — card show --handoff; `tests/test_handoff_context.py` — выбор исходных записей, read-only, fallback/архив и один verify с подготовленными статусом/индексом.

PB-054: file_operations.preview_operation использует те же staged-checks без записи проекта; lifecycle.operation_spec/preview_plan разделяют построение операции и применение. lifecycle.prepare_audit_command/portable_materials/verification_preflight отвечают за контейнер и раннюю проверку. verification.reusable_run/runtime_identity — явный reuse полного legacy-прогона. documents.audit_packages хранит независимую привязку provenance. Поведенческие регрессии: tests/test_audit_material_safety.py.

PB-056: verification.readiness проверяет доступность argv[0], TMPDIR и лимиты по успешной истории; file_operations.read_only_gate_check проверяет существующие gate/pending без записи. lifecycle.audit_preflight проверяет актуальную передачу перед аудитом (`verify --audit-ready`). Регрессии отказа до команд и read-only: tests/test_verification_readiness.py; ранний набор прежних ошибок версий/ссылок: tests/test_verification_smoke.py.

PB-034: lifecycle.transition_command фиксирует baseline/историю и обновляет карточку/индекс через file_operations; handoff_v2_command и _handoff_v2_evidence используют общую coverage runner и result_hash. verification_policy.project_files/applicability_files/baseline_files дают Git-независимые карты для применимости. tests/test_task_workflow_lifecycle.py проверяет CLI, роли, byte preservation, счётчик команд, SIGKILL/restore и конфликт read-set.

PB-035: `playbook.py card create --template quick` выбирает сокращённую пару `templates/task-quick.*` только для явно включённого workflow v2, принимает reason и verification_plan без миграции legacy. `tests/test_quick_route.py` проверяет создание, три итерации с одним ID и повышение маршрута.
