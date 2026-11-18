# Повторный независимый аудит PB-073

Результат: accepted. Замечание A-01 закрыто. Новых замечаний нет.

## Проверенное исправление

- `checks_select.plan` возвращает `not_applicable` в `run`, если последний значимый результат — failed/timeout/blocked/not_run. Поэтому история непокрытого обязательства учитывается и при пустом diff. Последующий passed снимает это условие; законный cross-task reuse сохраняется.
- `_execute_command` после таймаута получает целый exit_code завершённого процесса до валидации/записи результата. Невозможность Popen отдельно проверена: blocked с exit_code=null допустим контрактом и сохраняется в истории.
- Прежняя CLI-проба аудитора: последовательность passed project → failed project → stale task даёт третье исполнение (attempts=3), stale_rc=1, gate.ok=false, payment uncovered. A-01 исправлено.
- Пять новых постоянных E2E-регрессий прошли: failed/timeout/blocked после baseline, ручное подтверждение, другая задача/восстановление покрытия. Набор test_checks_e2e — 17 passed, 2 failed из 19; оба отказа на verifier run (см. ограничения).
- Дополнительная реальная CLI-проба: после успешного project force удаляется внешняя исполняемая команда. Project force и task stale возвращают 1, последний результат blocked, task gate закрыт; сбой запуска не исчезает из обязательств.
- test_checks_run: 45 passed; test_checks_select: 34 passed.
- Controlled-clock A-02: поздний failed выбран, project gate закрыт. probe_fixes: rc=0; исходные failed-повторы, восстановление и reuse подтверждены.

## Передача и документы

HANDOFF-03 с CHECK-03, verify ee0c402e782b40f48dc6aa69abae8b4b: status=passed, inputs_unchanged=true. Независимая проверка inputs_fresh=true, execution_id/sha256 соответствует EXEC-02, card_projection совпадает (freshness.json). До аудита --audit-ready отказал только на критерии с ещё открытым A-01: в task_cards criterion state open_finding имеет приоритет над CHECK. Само исправление аудитор проверил независимо; закрытие A-01 ниже устраняет эту причину без переписывания истории.

Design согласован с новым правилом. Новый активный self-pilot и его материалы CHECK-01/CHECK-02 проверены; положительный сценарий полного пилота не затрагивает новую ветку непокрытых исходов. Plan-only после исправления имеет отдельный отчёт исполнителя pilot-planonly/plan-only-summary.json. Архив не изменён. Основной проект не переключён на checks_policy. AUDIT-009 в рамках этого аудита не принимается.

## Ограничения

Полный verify и двухчасовой self-pilot независимо повторно не запускались; использован свежий полный отчёт исполнителя, дополненный адресными тестами и CLI-контрпримером аудитора. Два E2E protected-теста остановились на verifier run. Прямой запуск sandbox-exec с минимальным allow default профилем и /usr/bin/true также отклонён: sandbox_apply: Operation not permitted. Защищённое развёртывание/отдельная OS identity не подтверждены. Отказы verifier в этой среде не считаются дефектом PB-073. Аудитор не менял реализацию и чужие draft-документы.

Приёмка регистрируется через lifecycle accept; close выполняется только в dry-run, архивирование не поручено.
