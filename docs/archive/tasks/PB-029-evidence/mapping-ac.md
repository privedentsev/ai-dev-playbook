# Матрица трассировки AC-01–AC-12 → Реализация, Тесты и Доказательства (PB-029)

Документ подготовлен для независимого приёмочного аудита блока (AUDIT-006) и задачи PB-029 после устранения замечаний аудита Round 2, Round 3 и Round 4 (PB-029-A1, PB-029-A2, PB-029-A3).

## 1. Сводная таблица критериев приёмки (AC-01 – AC-12)

| Критерий | Требование спецификации | Механизм реализации в комплекте | Постоянные тесты (существующие модули) | Автономный пилот (`e2e_pilot.py` и полигон) | Статус |
|---|---|---|---|---|---|
| **AC-01** | Согласованность 8 режимов, ролей и разрешений | `ai-dev-playbook/rules/workflow.md`, `project.json`, CLI `playbook.py` | `tests/test_pb028.py`, `tests/test_close_lifecycle.py`, `tests/test_pb029.py::test_small_task_route_e2e` | Этап 1: настройка 5 коллекций документов, проверка каталога и индексов | Подтверждено |
| **AC-02** | Обсуждения: без файлов по умолчанию; сохранение создаёт discussion + index; перенос в spec без обязательной заметки | `playbook.py document create discussion`, `documents.py`, `rules/workflow.md` | `tests/test_document_cli.py::test_create_discussion_initial_state_open`, `tests/test_pb029.py::test_ac02_discussion_lifecycle_and_transition_to_spec` | CLI-создание обсуждения с регистрацией в индексе и прямой переход к спецификации подтверждены тестами; процессное правило (обсуждения ведутся без файлов по умолчанию до явной фиксации) закреплено в `rules/workflow.md` | Подтверждено |
| **AC-03** | Маленькая задача (баг): 1 карточка, verify -> handoff -> in_review -> аудит в той же карточке -> close. Без автосоздания аудитора | `playbook.py verify`, `playbook.py lifecycle handoff`, `playbook.py lifecycle accept`, `playbook.py close` | `tests/test_pb029.py::test_small_task_route_e2e`, `tests/test_close_lifecycle.py::test_handoff_without_verify_refused` | Этап 2: реальная программная фикстура (`src/validator.py` с функцией `parse_age`, `tests/test_validator.py`). **Автоматический CLI-зонд** (`Test-Harness`, `automated-cli-probe`): воспроизведение дефекта `parse_age('') -> ValueError`, диагностическое замечание, отказ в закрытии (rc=1), возврат в `in_progress`, исправление функции и добавление теста `test_empty_age_returns_none`, повторные verify (4 теста unittest PASS) / handoff, проверка `parse_age('') is None` (код 0), приёмка и закрытие в архив. Снимок передачи в `pilot-workspace-handoff.tar.gz` (3 теста OK), итоговый — в `pilot-workspace.tar.gz` (5 тестов OK). **Ограничение**: зонд доказывает CLI-механику, но **не является** реальным процессным пилотом с независимым участником; для полного подтверждения AC-03 требуется фактическое участие инженера или отдельной агентской сессии | Подтверждено реальным пилотом: [протокол](independent-pilot/report.md) |
| **AC-04** | Средняя задача (feature): отдельная спецификация, действующее поведение не подменяется до приёмки, promotion обновляет ревизию | `playbook.py lifecycle plan/apply` (`action: promote_change`), `documents.py` | `tests/test_pb029.py::test_medium_task_route_e2e` | Этап 3: DEV-201, spec `payment-retry` остаётся активной в `docs/specs/`, ревизия 1 -> 2 после приёмки, снимок исходной ревизии сохраняется в companion evidence | Подтверждено |
| **AC-05** | Большой блок задач: подзадачи принимаются отдельно, общие документы ждут итогового аудита блока | `playbook.py lifecycle accept`, `playbook.py close`, `playbook.py lifecycle plan` | `tests/test_pb029.py::test_large_block_route_e2e` | Этап 4: закрытие SUB-1 оставляет общий план `search-engine.md` и спецификацию активными; только итоговый AUDIT-1 позволяет архивировать план | Подтверждено |
| **AC-06** | Замечание аудита: возвращает задачу в `in_progress`, старый verify инвалидируется, требуется повторный verify и handoff | `playbook.py lifecycle accept --kind diagnostic`, контроль статусов и хэшей входов | `tests/test_pb029.py::test_large_block_route_e2e`, `tests/test_close_lifecycle.py::test_diagnostic_acceptance_not_enough` | Этап 4: диагностическое замечание по SUB-2 отклоняет close (rc=1); возврат в `in_progress`, исправление в коде, повторные verify/handoff/accept разрешают close | Подтверждено |
| **AC-07** | Актуализация требований: промоушен ревизий, история изменений в companion evidence, активная спецификация остаётся в `docs/specs/`, вывод устаревшей возможности (SMS) и архивация ADR и design | `documents.py`, `lifecycle.py` (`promote_change`, `archive_document`) | `tests/test_pb029.py::test_medium_task_route_e2e`, `tests/test_pb029.py::test_ac07_feature_deprecation_and_adr_archival_e2e` | Этап 8: созданы `spec:auth-methods`, `design:auth-sms`, `adr:0010-sms-auth`; частичный вывод SMS (DEV-701) обновляет spec и design до ревизии 2, ADR остаётся активным; полный вывод (DEV-702) повышает ревизию spec до 3 и одновременно архивирует `design:auth-sms` и `adr:0010-sms-auth` в `superseded` с проверкой значений `current_revision` | Подтверждено |
| **AC-08** | Завершённый план архивируется, спецификация/код/тесты остаются; отказ в архивировании плана при открытых задачах | `lifecycle plan` (валидация незавершённых задач плана) | `tests/test_pb029.py::test_large_block_route_e2e`, `tests/test_pb029.py::test_link_and_archive_immutability_e2e` | Этап 4: отказ архивирования `plan:search-engine` при открытой AUDIT-1; успешная архивация после закрытия AUDIT-1; спецификация остаётся активной | Подтверждено |
| **AC-09** | Перемещение сохраняет целостность ссылок, обновляет ссылки в документах проекта; evidence перемещается неизменным (побайтовый SHA256) | `engine.py` (`_rewrite_links`), `file_operations.py`, `lifecycle.py` | `tests/test_pb029.py::test_link_and_archive_immutability_e2e` | Этап 5: бинарный файл `TASK-301-evidence/log.bin` перемещён в `docs/archive/tasks/TASK-301-evidence/log.bin`, хэш SHA-256 побайтно совпал | Подтверждено |
| **AC-10** | Атомарность, восстановление, изоляция через staging, защита от гонок | `file_operations.py` (`_prepare_staged`, staging lock, atomic rename, backup/rollback) | `tests/test_file_operations.py`, `tests/test_pb028.py` (`test_real_sigkill_interrupted_update_locks_lifecycle_and_cli_restores`) | Все этапы: все мутирующие операции выполняются через staging с откатом при сбоях и защитой общим мьютексом | Подтверждено |
| **AC-11** | Контроль жизненного цикла: отказ в закрытии без acceptance, проверка обязательных материалов, обработка отмены задачи, reopen и postpone | `lifecycle.py`, `verify.py`, `tasks.py` | `tests/test_close_lifecycle.py`, `tests/test_pb029.py::test_ac10_ac11_lifecycle_controls_and_missing_evidence` | Этап 9: потеря обязательного evidence блокирует close (rc=1); cancel архивирует со статусом `cancelled`; reopen через `lifecycle apply` возвращает карточку в `in_progress`, Closed: -; postpone переводит в `blocked` с записью причины | Подтверждено |
| **AC-12** | Самоприменение новой схемы на копии репозитория `ai-dev-playbook` со всеми незакоммиченными изменениями | `playbook.py check --ready`, `playbook.py document list`, `playbook.py verify`, `playbook.py lifecycle handoff/accept/close` | `tests/test_pb029.py::test_self_project_copy_pilot` | Этап 7: копия репозитория разворачивается на полигоне, проходит `check --ready`, полный цикл новой задачи `PILOT-001` (verify->handoff->accept->close), побайтовый контроль SHA-256 ВСЕХ 35 карточек (активные и архивные), всех скриптов, правил, шаблонов, конфигураций и документов коллекций | Подтверждено |

## 2. Устранение замечаний аудита Round 2, Round 3, Round 4 и Round 5

1. **PB-029-A1 (Автоматический CLI-зонд вместо процессного пилота — устранение ложных утверждений):**
   - В `e2e_pilot.py` все приёмочные действия TASK-101 переобозначены с актора `Engineer`/`human` на `Test-Harness`/`automated-cli-probe`. Добавлен блок комментариев, явно обозначающий автоматическую природу зонда и его отличие от реального процессного пилота.
   - CLI-зонд доказывает:
     - Работоспособность `parse_age` с реальной фикстурой (3 теста до исправления, 5 после);
     - CLI-механику `verify -> handoff -> accept --kind diagnostic -> close отказ -> возврат -> исправление -> verify -> handoff -> accept --kind acceptance -> close`;
     - Корректность архивации и неизменяемость evidence.
   - CLI-зонд **не доказывает** реальное участие независимого приёмщика. Утверждения process-log приведены в соответствие фактам.
   - В архивах полигона `pilot-workspace-handoff.tar.gz` (исходный снимок) и `pilot-workspace.tar.gz` (итоговый) содержатся реальные исполняемые фикстуры, воспроизводимые `python3 -m unittest discover -s tests -v`.
   - **AC-03 помечен как «Частично подтверждено (CLI-механика)»**: для полного подтверждения требуется фактическое участие инженера или отдельной агентской сессии.
2. **PB-029-A2 (Полнота покрытия AC-02, AC-07, AC-10/11 и точность ссылок):**
   - Устранено в раунде 3 и подтверждено в аудите Round 4:
     - Ссылки AC-02 скорректированы на `tests/test_document_cli.py::test_create_discussion_initial_state_open` и постоянный тест `tests/test_pb029.py::test_ac02_discussion_lifecycle_and_transition_to_spec`;
     - В AC-07 добавлены проверки фактических ревизий `current_revision` (spec=2/design=2, spec=3);
     - В AC-10/11 добавлен сценарий `postpone` в постоянный тест;
     - Контроль неизменности на копии репозитория (AC-12) охватывает все 35 карточек задач и все файлы ядра.
3. **PB-029-A3 (Свежесть верификации):**
   - Устранено в раунде 3 и подтверждено в аудите Round 4 (`inputs_fresh=True`, хэши совпадают, регрессии нет).
   - Порядок фиксации выдерживается строго: все сопутствующие материалы evidence и индекс задач фиксируются ДО запуска `verify PB-029`, сразу после чего вызывается `lifecycle handoff PB-029` без последующих изменений `docs/`.

## 3. Окружение и платформа

- **Операционная система:** macOS (Darwin 24.6.0).
- **Интерпретатор:** Python 3.14.
- **Платформенные особенности:** Модуль `verify.py` опирается на вызовы POSIX групп процессов (`os.setpgid`, `os.killpg`, сигналы `signal.SIGKILL` / `signal.SIGTERM`). Все проверки и тесты валидированы в среде macOS (Darwin POSIX). Локально в средах Windows и Linux прогон не проводился.
- **Git:** В соответствии с правилами комплекта, все изменения выполняются исключительно в рабочей копии; операции `git commit`, `push`, `merge`, `release` не выполняются без явного указания инженера.

## 4. Ссылки на артефакты

- **Модуль постоянных тестов:** [`tests/test_pb029.py`](file:///Users/privedentsev1/Dev/ai-dev-playbook/tests/test_pb029.py) (8 тестов, результат `Ran 8 tests ... OK`).
- **Скрипт автономного пилота:** [`docs/tasks/PB-029-evidence/e2e_pilot.py`](file:///Users/privedentsev1/Dev/ai-dev-playbook/docs/tasks/PB-029-evidence/e2e_pilot.py).
- **Снимок полигона на момент первой передачи (TASK-101 в in_review):** [`docs/tasks/PB-029-evidence/pilot-workspace-handoff.tar.gz`](file:///Users/privedentsev1/Dev/ai-dev-playbook/docs/tasks/PB-029-evidence/pilot-workspace-handoff.tar.gz).
- **Итоговый архив полигона (TASK-101 проверена и закрыта через Test-Harness):** [`docs/tasks/PB-029-evidence/pilot-workspace.tar.gz`](file:///Users/privedentsev1/Dev/ai-dev-playbook/docs/tasks/PB-029-evidence/pilot-workspace.tar.gz).
- **Билет передачи TASK-101:** [`docs/tasks/PB-029-evidence/handoff-ticket.json`](file:///Users/privedentsev1/Dev/ai-dev-playbook/docs/tasks/PB-029-evidence/handoff-ticket.json).
- **Журнал процесса взаимодействия ролей:** [`docs/tasks/PB-029-evidence/process-log.md`](file:///Users/privedentsev1/Dev/ai-dev-playbook/docs/tasks/PB-029-evidence/process-log.md).
- **Снимок входных файлов:** [`docs/tasks/PB-029-evidence/inputs.json`](file:///Users/privedentsev1/Dev/ai-dev-playbook/docs/tasks/PB-029-evidence/inputs.json).
- **Отчёт верификации:** [`docs/tasks/PB-029-evidence/report.json`](file:///Users/privedentsev1/Dev/ai-dev-playbook/docs/tasks/PB-029-evidence/report.json).

## Актуализация 2026-10-22T12:39:02+03:00

PB-029-A1 устранено. Описанное выше ограничение автоматического зонда снято отдельным реальным процессным пилотом: аудитор /root → возврат → исполнитель /root/pilot_executor → повторная передача → независимая приёмка и close TASK-101. [Протокол](independent-pilot/report.md). Исторические результаты раундов сохранены.
