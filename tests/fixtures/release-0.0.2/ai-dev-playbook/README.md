# Рабочий комплект

Переносимый комплект правил, шаблонов и CLI для организации работы AI-ассистентов над проектом.

## Состав комплекта

- `rules/` — переносимые правила процесса:
  - [workflow.md](rules/workflow.md) — восемь режимов работы, маршруты и границы полномочий.
  - [context.md](rules/context.md) — коллекции документов, источники истины, управление изменениями и ссылки.
  - [tasks.md](rules/tasks.md) — жизненный цикл задач, статусы, передача (`handoff`), приёмка (`acceptance`), декомпозиция и закрытие.
  - [verification.md](rules/verification.md) — самопроверка исполнителя (`verify`), независимый аудит (`audit`), доказательства и отпечатки входов.
  - [update.md](rules/update.md) — контракт версий, владения файлами и безопасного обновления комплекта.
  - [git.md](rules/git.md) — регламент Git-операций и разрешений инженера.
  - [layout.md](rules/layout.md) — структура каталогов и правила размещения проектных материалов.
  - [resources.md](rules/resources.md) — модель учёта ресурсов, бюджетов и политик.
- `templates/` — шаблоны проектных документов:
  - `task.md` — карточка задачи.
  - `documents/` — шаблоны коллекций документов: `spec.md`, `design.md`, `discussion.md`, `plan.md`, `adr.md`.
  - `AGENTS.md`, `project.json`, `resources.json`, `CHANGELOG.md`.
- `scripts/` — исполняемый CLI:
  - `playbook.py` — единая точка входа: проверка структуры, управление документами, жизненный цикл задач и комплекта.
  - `documents.py` — каталог проектных документов, валидация метаданных и генерация индексов.
  - `document_links.py` — анализ, проверка и детерминированное переписывание Markdown-ссылок при переносах.
  - `lifecycle.py` — планирование и проверка операций жизненного цикла (`archive_document`, `close_task`, `reopen`, `promote_change`, `handoff`, `accept`).
  - `file_operations.py` — восстанавливаемый движок файловых операций: gate-блокировки, staged-проверки, резервные копии и автономный откат.
  - `verification.py` — исполнение проверок runner, JSON-отчёты и отпечатки входов.
  - `updating.py` — учёт состава установленного комплекта (`manifest.json`), план, применение и откат обновлений.
- `updates.json` — реестр поддерживаемых переходов версий комплекта.
- `VERSION` — номер текущей версии комплекта.

## Восемь режимов и три маршрута

Процесс регламентирует 8 согласованных режимов ([rules/workflow.md](rules/workflow.md)):
1. **Обсуждение** (`discussion`) — формулирование проблемы, запись в файл только по явной просьбе.
2. **Спецификация** (`spec`) — проверяемые требования и сценарии, разделение текущего и планируемого поведения (`changes`).
3. **Проектирование** (`design`) — архитектура, интерфейсы и архитектурные решения (ADR).
4. **Планирование** (`plan`) — этапы, зависимости и проверки блока задач.
5. **Постановка задач** (`tasks`) — формирование исполнимых карточек задач.
6. **Выполнение** (`implementation`) — реализация кода и тестов; исправление дефектов (`fix`) входит в этот режим.
7. **Проверка** (`verify`) — самопроверка исполнителем, запуск тестов, сохранение доказательств и передача (`handoff`).
8. **Аудит** (`audit`) — независимая приёмка в отдельной сессии или инженером; аудит блока (`block audit`) входит в этот режим.

Маршруты задач зависят от масштаба:
- **Маленькая задача**: карточка с требованиями, планом и приёмкой → выполнение → проверка (`verify` + `handoff`) → независимый аудит и закрытие.
- **Средняя задача**: обсуждение → спецификация → (при необходимости) design → планирование → задачи → выполнение/проверка → аудит.
- **Большая задача**: обсуждение → спецификация → design → план → подзадачи + AUDIT-карточка блока → выполнение/приёмка каждой подзадачи → итоговый аудит блока.

## Основные команды CLI

```sh
# Проверка структуры проекта и готовности карточек к работе
python3 ai-dev-playbook/scripts/playbook.py check --ready

# Обновление индексов задач и документов
python3 ai-dev-playbook/scripts/playbook.py index --all

# Создание и регистрация документов коллекций
python3 ai-dev-playbook/scripts/playbook.py document create <spec|design|discussion|plan|adr> <slug> [--apply]
python3 ai-dev-playbook/scripts/playbook.py document register <путь> --metadata '<json>' [--apply]
python3 ai-dev-playbook/scripts/playbook.py document impact <document-id>
python3 ai-dev-playbook/scripts/playbook.py document materials <owner-id>

# Самопроверка задачи через runner
python3 ai-dev-playbook/scripts/playbook.py verify <TASK-ID> [--dry-run]

# Передача задачи на аудит (исполнитель)
python3 ai-dev-playbook/scripts/playbook.py lifecycle handoff <TASK-ID> --summary "..." [--report <путь>]

# Регистрация приёмки аудитором
python3 ai-dev-playbook/scripts/playbook.py lifecycle accept <TASK-ID> --result accepted --kind acceptance

# Закрытие принятой задачи (аудитор или инженер)
python3 ai-dev-playbook/scripts/playbook.py close <TASK-ID> [--dry-run]

# Многодокументные операции жизненного цикла
python3 ai-dev-playbook/scripts/playbook.py lifecycle plan <request.json> [--save]
python3 ai-dev-playbook/scripts/playbook.py lifecycle apply <plan.json>
python3 ai-dev-playbook/scripts/playbook.py lifecycle status
python3 ai-dev-playbook/scripts/playbook.py lifecycle restore
```

## Обновление комплекта

Обновление установленного комплекта выполняется только по локальному релизному артефакту:
1. `python3 ai-dev-playbook/scripts/playbook.py update plan <каталог>` — предпросмотр без изменения файлов.
2. `--save` сохраняет план в `work/update/plans/`.
3. `python3 ai-dev-playbook/scripts/playbook.py update apply` — применение с созданием резервной копии и журнала.
4. `python3 ai-dev-playbook/scripts/playbook.py update restore` — откат после прерывания.
5. Установки без manifest принимаются под управление через `adopt`.
