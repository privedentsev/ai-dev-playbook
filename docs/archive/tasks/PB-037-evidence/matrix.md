# PB-037: матрица сквозных сценариев

Исполнитель: Codex / GPT-6; 2026-11-06. Временные проекты создаются стандартными test fixtures. Ручные наблюдения и инженерская приёмка в них синтетические; браузер и независимый аудитор этим прогоном не подменяются.

## Воспроизведение

- Новые составные сценарии: `python3 -m unittest discover -s tests -p test_workflow_e2e.py -v`.
- Любой файл ниже: `python3 -m unittest discover -s tests -p '<файл>' -v`.
- Пилот: `python3 docs/tasks/PB-037-evidence/self_pilot.py . /tmp/pb037-pilot-results`. Копирует исходный проект, сохраняет обязательные argv/inputs/timeouts/always, явно включает v2, мигрирует PB-037, выполняет настроенную регрессию и повтор verify, две передачи и audit-ready. Без независимой приёмки close должен отказать. Итоги/логи/отпечатки в выходном каталоге; source project.json должен остаться побайтно прежним. Требуются Python 3.10+ и время на полный suite; лимит unit-tests остаётся 1200 с.
- Финальный исходный проект: `index --all`, `check --ready`, `verify PB-037 --preflight`, `verify PB-037`; сводка и ссылки на точный отчёт — CHECK карточки. Самопроверка структуры отдельно от CLI-поведения.

## Матрица AC-01–14

| AC | Сценарии и ожидаемый результат | Доказательства |
|---|---|---|
| 01 | quick создаётся обычной парой карточки, индекс и штатный архив | `test_quick_route.py`: preview/create; три итерации; `test_workflow_e2e.py`: manual iterations/archive |
| 02 | три candidate одного DEV-1, нет промежуточного close; старое ручное подтверждение отвергнуто | `test_workflow_e2e.py`: `test_three_manual_iterations_stale_candidate_then_archive`; `test_quick_route.py`: revise/raise |
| 03 | повышение quick→small сохраняет ID/историю; понижение без engineer-approval отклоняется | `test_quick_route.py`: review/revise/raise; `test_task_workflow_lifecycle.py`: route approval/failures; нормативный запрет нового поведения — workflow и руководство |
| 04 | manual-only не запускает команд; независимая b не повторяется при изменении a; обязательная проверка не исключается | `test_workflow_e2e.py`: manual/counters; `test_verification_preflight.py`: missing registry/unapproved exclusion; `test_verification_policy.py`: unrelated scope/obligation |
| 05 | без наблюдения not_run и handoff отказ; ручное заявление привязано к candidate | `test_workflow_e2e.py`: manual; `test_verification_v2.py`: manual presentation/record/stale |
| 06 | failed/timeout не считаются passed, передачу блокирует отсутствие свежей coverage; исключение failed требует согласования | `test_verification_v2.py`: timeout/failed reuse; `test_task_workflow_lifecycle.py`: failed handoff/latest failure; `test_verification_preflight.py`: failed exclusion |
| 07 | индекс/материалы/команды/лимиты проверяются до дорогой команды; handoff сам согласует v2 | `test_verification_preflight.py`, `test_verification_readiness.py`, `test_task_workflow_lifecycle.py`: service cycle |
| 08 | verify/handoff/accept/close не повторяют реализацию; substantive изменение повторяет связанную проверку | `test_workflow_e2e.py`: counters; `test_task_workflow_lifecycle.py`: source/plan/policy stale; `test_verification_policy.py`: task projection |
| 09 | a изменён/удалён/добавлен → a=4, b=1; неизвестная база консервативна, изменения во время прогона устаревают | `test_workflow_e2e.py`: independent counters; `test_verification_policy.py`: unknown inputs; `test_verification_v2.py`: stale during/later command mutates input |
| 10 | quick close без приёмки и auditor acceptance отвергнуты; engineer acceptance последнего candidate разрешает archive | `test_workflow_e2e.py`: manual; `test_quick_route.py`: final engineer acceptance |
| 11 | SIGKILL/recovery, foreign gate, pending и повреждённые proof не дают ложной приёмки; все перенесённые report/log побайтно сохранны | `test_task_workflow_lifecycle.py`: real_sigkill/pending/conflict/corruption; `test_workflow_install.py`: restore; `test_workflow_e2e.py`: archive hash assertions |
| 12 | legacy/explicit opt-in/history/old CLI; mandatory checks сохраняются | `test_workflow_install.py`, `test_task_workflow_schema.py`, `test_verification_v2.py`: v1 compatibility; интеграционная копия self-project |
| 13 | локально regression не запускается; release checkpoint запускает её, local результат reused | `test_workflow_e2e.py`: `test_checkpoint_runs_regression_separately_from_local_check`; `test_verification_policy.py`: explicit checkpoint |
| 14 | small/medium/large проходят verify/handoff/auditor acceptance/close; блок без приёмки/незавершённых задач не архивируется | `test_workflow_e2e.py`: nonquick routes; `test_close_lifecycle.py`: combined close/plan archive/refusals; сверка workflow/tasks/verification/update с руководством |

## PB-054 и измерения

`test_audit_material_safety.py` проверяет Finder, сырой Markdown и безопасный .txt, work/audits, добавление аудита после verify, будущий архив, byte preservation, conditional next version, свежий legacy reuse, изменение среды/повреждение отчёта/поздний failed. `test_verification_v2.py` и `test_verification_policy.py` добавляют смену команды/policy, timeout и race. Негативные сценарии выполняются на маленьких синтетических проектах.

Новый составной тест: два первых verify, повтор handoff, acceptance и close --dry-run дают a=1/b=1. Содержательная правка a → 2/1; удаление → 3/1; добавление → 4/1; финальные handoff/accept/archive не меняют 4/1. Manual-only: три новых candidate, ноль команд. Checkpoint: local=1/regression=0 до release, после — 1/1. Это число запусков, а не обещание скорости/экономии токенов.

## Границы доказательств и корректировки тестов

Реального UI в этом репозитории нет: размеры/фокус/читаемость — сценарий synthetic manual fixture, реальная визуальная приёмка не выполнялась. CLI не анализирует смысл кода для запрета quick и не удостоверяет личность/независимость сессии по имени. Эти требования проверены как правила процесса; все остальные AC имеют поведенческие/политические сценарии. Git-полномочия не менялись; commit/push/merge/release не выполнялись. Итоговый независимый аудит блока остаётся AUDIT-007.

В первых двух прогонах нового теста ошибочно ожидался запрет одинаковой атрибуции исполнителя и аудитора; исходный контракт такого удостоверения не обещает. Ожидание исправлено без изменения продукта; неуспехи сохранены. Два подготовительных self-copy запуска остановлены исполнителем на start (SIGTERM, -15), чтобы перейти к окончательным тестам; это не продуктовый отказ и не тест recovery. Поведение прерывания доказывается отдельными fault-injection fixtures.

Первый checkpoint-сценарий выявил ошибку самой fixture: условие было задано только в реестре, а локальный план по умолчанию требовал always. Условие явно перенесено в план; обязательность проекта не ослаблена. Неуспешный лог сохранён.

Первый окончательный self-copy pilot дошёл до start, затем preflight отказал из-за незарегистрированных новых материалов PB-037 до запуска регрессии. Отказ и все команды сохранены в logs/pilot-preflight-blocked; материалы зарегистрированы перед повтором.

Текущий план self-copy сохраняет реестр без ослабления (argv/default_inputs/timeout/always). Входы конкретных проверок используют каталоги и отдельные файлы docs, кроме производного индекса и сырой текущей карточки; требования/политика включены как task_projection/policy_projection. Равенство expected == covered проверяет охват всех прежних файлов; snapshot каталогов дополнительно учитывает membership. Входов на проверку 42 вместо 1326 в первом полном пилоте. Для новых файлов непосредственно в корне tasks нужен пересмотр явного плана; этот случай после baseline не моделируется.

Перед дорогой регрессией pilot запускает команды с PYTHONDONTWRITEBYTECODE=1: raw directory snapshots учитывают любые файлы, в том числе служебный Python-кэш. Это явная настройка окружения, команды/таймауты не меняются. Подготовительный запуск с обновлённым планом остановлен до verify, чтобы применить эту настройку.

## Уточнение после первого полного пилота

Первый полный verify копии вернул 1 через 1264.404 с. Последний сохранённый частичный лог не содержит assertion failures и обрывается в новых E2E; сам report.json не сохранился из-за cleanup первой версии harness. Поэтому точный статус unit-tests не выдаётся за установленный факт; вероятная причина — лимит 1200 с, но доказано только failed verify и незавершённый лог. Доступные результаты, входы, команды и состояние сохранены в logs/pilot-first-full. Harness теперь сохраняет failed-runs до удаления копии.

Повтор использует компактные directory inputs вместо тысяч отдельных leaf entries: утверждение expected == covered проверяет сохранение каждого прежнего файла docs; membership вложенных каталогов также учитывается. Карточка/производный индекс проецируются, собственные будущие baseline/history — служебные материалы. Для новых файлов непосредственно в корне tasks требуется пересмотр явного плана; после фиксации такой случай пилот не моделирует. Реестр, обязательные команды, always и timeout 1200 с сохранены. Это оптимизация представления того же охвата, не исключение failed проверки. Подробные причины/история сохранены; положительная передача первого неуспешного прогона не заявляется.

## Подтверждённые результаты компактного пилота

Полный прогон копии: 702 tests OK, 1097.628 с (unit-tests), лимит 1200 с сохранён. Следующий verify завершился 0 и напечатал REUSE для self-check, preflight-regressions, unit-tests; RUN отсутствует. Это один реальный запуск каждой обязательной команды и ноль запусков при повторе. Передачи/готовность к аудиту и финальный прогон исходного проекта фиксируются в итоговом CHECK карточки.

Итог self-copy: оба v2 отчёта passed; повтор handoff сообщил «передача уже актуальна»; audit-ready вернул 0; close --dry-run вернул 1 без engineer/auditor acceptance. source_configuration_unchanged=true, legacy_checks_preserved=3, report_count=2. Полные команды, логи, входные hashes, registry, baseline, безопасный card.txt и workflow-v1.json — в pilot/. Исходный проект остаётся legacy. Ручные подтверждения в тестах синтетические; настоящая независимая приёмка этой задачи не выполнялась.
