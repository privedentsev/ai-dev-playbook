# PB-036: установка и обновление workflow v2

Инженер 2026-11-06 согласовал локальный артефакт 0.2.0 с прямыми переходами из 0.0.2, 0.0.3, 0.1.0. Публикация и теги не выполнялись. Рабочая ветка dev; исходные незакоммиченные изменения других задач сохранены.

## Изменения

- playbook.py: init --workflow-v2; workflow enable-v2 с preview/apply; workflow migrate-card с явными route, reason, actor/model и verification_plan. Изменения существующих установок используют общий gate, read-set, staged/final checks, backup и restore file_operations.
- Новая установка без флага остаётся legacy. V2-init использует templates/verification.json: только начальная структурная self-check; проверки поведения настраивает инженер при needs_review.
- Enable сохраняет legacy verification, неизвестные пользовательские поля и paths. Все прежние argv/timeout/inputs переносятся в registry как always, без сужения обязательности. Отсутствующий verification требует настройки, commands не преобразуется из shell в argv. Повторное включение не перезаписывает конфигурацию.
- Явный opt-in ставит project.json schema 2 с обязательным task_workflow v2: старый CLI отказывает при прямом запуске. Новый CLI читает project schemas 1/2; ранее подготовленные конфигурации schema 1 с task_workflow остаются читаемыми, но ручное добавление блока не заменяет безопасный adapter.
- updates.json schema 2 задаёт minimum_cli_version=0.2.0 и supported_project_schemas=[1,2]. Bootstrap выполняет CLI целевого артефакта с --root установленного проекта; старый updater отклоняет артефакт до записи. Новый loader сохраняет чтение updates schema 1. Последующие обновления сохраняют подключённый schema 2, если артефакт его объявляет; неподдерживаемую схему должен явно достигать migration.
- Migrate-card сохраняет исходный Markdown и metadata в зарегистрированном immutable workflow-v1.json; прежние handoff/acceptance — также в transitions. План проверяется на все обязательные registry ID. Metadata становится v2, ready требует нового start/baseline; прежнее coverage не наследуется. Сбрасываются текущие criterion_evidence и отметки приёмки; текст требований, журнал, отчёты и способ хранения inline/external сохраняются.
- Обновлены README, changelog Unreleased, правила update, архитектура/карта, руководство, design/план и документ версий. Self-project остаётся legacy; self-application и сквозной пилот — PB-037. Это не независимая приёмка PB-036/AUDIT-007.

## Матрица и воспроизведение

| Сценарии | Тесты/доказательства |
|---|---|
| Init legacy/v2, preview без записи, init из артефакта без проектных docs | test_workflow_install и прежние InitTest/PB-028 |
| Объявленные 0.0.2/0.0.3/0.1.0 → 0.2.0; сохранение legacy, paths и данных | test_workflow_install; версии 0.0.3/0.1.0 — генерируемые fixtures установленного manifest/config, не заявления о существовании исторических опубликованных релизов |
| Реальный исторический CLI/артефакт 0.0.2, bootstrap текущим CLI и custom paths/evidence | test_pb028.test_real_002_artifact_update_with_custom_paths_and_evidence; историческая fixture не изменялась |
| Старый CLI × новый артефакт / подключённый project schema 2 | test_workflow_install: отказ до plan, verify и close |
| Новый CLI × legacy/v2/неизвестные schema, minimum CLI | test_workflow_install и test_task_workflow_schema полного suite |
| Metadata v1 → v2, inline/external, сохранение передачи/журнала, обязательный новый план | test_workflow_install; v1-отчёт сохраняется побайтно, handoff после нового start до v2 verify отклоняется |
| Будущее обновление подключённого project schema 2 | Синтетический локальный артефакт 0.2.1, отдельная fixture; переход 0.2.0 → 0.2.1 не объявляется реальным артефактом 0.2.0 |
| Конфликт управляемого файла, чужой gate, pending | test_workflow_install и прежние update/file_operations регрессии |
| Прерывание enable/update после записи, corrupt backup, автономный restore | test_workflow_install: KeyboardInterrupt, побайтный предобраз, сохранение pending при corrupt backup |
| Реальный SIGKILL update/lifecycle и взаимная блокировка | test_pb028 и test_file_operations; общий engine не заменялся |
| Downgrade/необъявленный переход, unknown formats, coverage, checkpoints | Прежние test_cli, test_task_workflow_schema, test_verification_policy/v2 и test_task_workflow_lifecycle полного suite |

Команды: `PYTHONPATH=tests python3 -m unittest test_workflow_install test_pb028 -v`; два адресных test_cli.MigrationTest для совместимой/несовместимой новой схемы. Общая миграционная fixture переведена на синтетическую project schema 3, поскольку schema 2 теперь имеет собственный действующий контракт. PB-028 обновлён на bootstrap целевым CLI с обязательным отказом старого updater; assertions сохранности пользовательских данных сохранены.

Предварительные успешные логи — targeted-tests.log (25 сценариев до дополнительной защиты project schema), targeted-tests-02.log (27 сценариев). Итоговые целевые результаты — targeted-tests-final.log. Полный настроенный verify хранится отдельно, его результат не подменяется структурными проверками. Общие регрессии не означают внешнюю интеграцию блокировок merge/release.

## Фокус независимой приёмки

Проверить legacy defaults, отсутствие автоматического ослабления registry, minimum CLI и двойной отказ прежнего CLI (artifact/project), schema compatibility при следующем update, history/ready/baseline при migrate-card, неизменность v1-отчётов, late conflict/read-set/gate и автономное восстановление. Не выдавать временные fixtures или локальные checkpoints за опубликованные релизы/внешние интеграции.
