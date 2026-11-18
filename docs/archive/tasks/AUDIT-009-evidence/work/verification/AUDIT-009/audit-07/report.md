# AUDIT-009 — повторный независимый аудит audit-07

Вердикт независимого анализа: **accepted**. A-01–A-09 устранены; новых блокирующих замечаний нет. Принят локальный и протокольный контракт с безопасным отказом защищённых утверждений без доказанной OS/CI identity. Формальная приёмка и закрытие выполняются штатным lifecycle после новой передачи.

## Повторный аудит 2026-11-18T08:32:39+03:00; исполнитель: Codex; модель: GPT-6.1 Sol (High).

Модель указана по предоставленному инженером имени. Кандидат 63b197f; точные SHA-256 и среда — [candidate.json](candidate.json). Отдельная сессия относительно реализации и приёмки PB-081; код и постоянные тесты не исправлялись. Начальный audit-ready отказал по in_progress/отсутствию HANDOFF; фиктивная передача не создавалась. [Наблюдение](audit-ready.json). Подготовлен штатный audit-07 до запуска широких проверок.

## Исправленные контрпримеры

- A-09: [исходная реальная CLI-проба](interpreter.json) наблюдает один одобренный venv у прямого argv и адаптера: оба rc=1, gate=false. Подмена Python не воспроизводится.
- [Дополнительный runtime-контроль](runtime-context.json): положительный venv force → passed, stale → reused; правка pyvenv.cfg с сохранением size/mtime → настоящее повторение; изменение PYTHONPATH → настоящее повторение; cross-task/release затем reused. Число исполнений [1,1,2,3,3]. [Программа](probe_runtime_context.py).
- [Shim PB-081](launcher.json) отклоняется до исполнения и на force, и на stale; status=0/1. Прямой venv возвращает failed.
- A-08: [normal](bytecode-normal.json) и [dont-write](bytecode-dont-write.json) дают [1,2,2], stale/force rc=1, gate=false; caches не удаляются.
- A-04/A-05: [corrupt](old-corrupt.json), [delete](old-delete.json), [legacy](old-legacy.json) не открывают gate; новое исполнение требуется.
- A-06: [rollback](rollback.json) сохраняет блокирующий исход и повторяет исполнение.
- A-07: [verifier](verifier-gate.json) действительно исполнил passed→failed, обе квитанции валидны; task/project/reference gate=false, CLI rc=1. Исполнено вне внешней песочницы для вложенного sandbox-exec; trusted home той же OS identity, capability local.

## Границы

Реальная отдельная OS/CI identity не подтверждена; protected kind=test по-прежнему запрещён. Внешний protected rollout и self-project opt-in не выполняются. Старый полный пилот audit-05 — историческое измерение; новый plan-only и полный verify приведены ниже. Новый процент экономии не обещается. Предыдущие отрицательные CHECK/протоколы и архив не переписаны.

## Итоговые доказательства

- Полный штатный `verify AUDIT-009` — **passed**, self-check/preflight-regressions/unit-tests passed, inputs_unchanged=true. **1141 tests OK, 2197.047 с**. [Оригинальный отчёт](../7138133c57a04bba80bb0f222a964380/report.json), [журнал](../7138133c57a04bba80bb0f222a964380/unit-tests.stderr.log). Это новый независимый прогон текущего кандидата, не переиспользованный отчёт исполнителя.
- Ранние smoke-регрессии: 6 tests OK ([лог](early-smoke.log)); [preflight](preflight-before-verify.log) пройден до полного запуска.
- [Plan-only](pilot-plan.log): rc=0; 1141 экземпляр/1135 уникальных test IDs, 6 shared load_tests IDs; 372 checks, 39 block, 164 CLI, 510 core, 56 verification. [Команды](pilot-plan/commands.json). Настоящая command-квитанция проверена, capability=local, protected kind=test и protected release-ready отклонены.
- [Сохранность до закрытия](preservation-pre-close.json): 3342 прежних файлов истории и 168 файлов кода/тестов совпадают с исходными SHA-256. Смысл и данные исходных чужих drafts сохранены. При закрытии CLI вправе транспортировать ссылки; immutable-доказательства сохраняются по штатному контракту.

| AC спецификации | Независимая приёмка |
|---|---|
| AC-01 | No-Git shop, ручные payments/discounts, транзитивные потребители и реальные counters — passed. |
| AC-02 | Add/delete/rename/import/cycle/dynamic/missing baseline/same-size-mtime, плюс source/test cache — passed. |
| AC-03 | Logs/card projection/PNG/archive/unknown, exclusions — passed. |
| AC-04 | Eight reused/two run, subsystem/project fallback и budget stop — passed. |
| AC-05 | Release/cross-task/stale/force, TTL/command/environment/late failed — passed; отдельные venv/PATH/shim/pyvenv/PYTHONPATH контроли — passed. |
| AC-06 | Zero/missing/duplicate/skip/corrupt-report, ID descriptions и reused timestamps — passed. |
| AC-07 | Drift/race/arena/receipt tamper, актуальный bytecode и одобренный Python — passed в заявленной локальной/протокольной границе. |
| AC-08 | Forgery/replay/revocation/policy weakening/local-protected — passed на стендах; реальная отдельная OS/CI identity не проверена, соответствующая готовность блокируется. |
| AC-09 | Task-specific/manual/unified gates/terminal report/archive provenance и self-project lifecycle pilot полного набора — passed. |
| AC-10 | Legacy/opt-in/preview/unsupported/update/recovery/corrupt history — passed. |
| AC-11 | Полный исторический пилот audit-05 сохранён; текущий plan-only и полный интеграционный verify воспроизведены. Новое сравнительное измерение скорости не заявляется. |

## Оформление результата

Протокол завершён 2026-11-18T09:13:55+03:00. Код и постоянные тесты в аудите не изменялись; toolkit/self-manifest не менялись. Актуализированы только устаревшие описания статуса spec/design/плана и указатель документации. Прежняя атрибуция не переписана. CHECK-07 и HANDOFF-01 фиксируют новую передачу; затем AUDIT-01 и lifecycle accept регистрируют независимую приёмку. После обязательного close --dry-run подготовлен [единый запрос](close-request.json): close AUDIT-009 и promotion спецификации. После его применения исполненный план переводится в completed и архивируется [отдельным запросом](archive-plan-request.json). [Протокол приёмки спецификации](spec-acceptance.json), [точный payload](spec-payload.txt). Реальное выполнение и перенос подтверждаются служебными companion CLI и архивной карточкой, а не этим предварительным описанием операции.
