# Независимый аудит PB-073

Результат: changes_requested; A-01 (P1), AC-01.

## A-01: baseline не сохраняет обязательство после нового failed

Реальный CLI, временный проект без Git, одна command-проверка `payment` модуля payments, без applicability=always. Команда проходит только первую попытку, все последующие падают; входы проекта не меняются.

1. `checks run DEV-01 --scope project --mode force`: rc=0, passed; baseline записан.
2. Та же команда: rc=1, failed. Baseline уже соответствует текущим входам, запрет его продвижения ничего не меняет.
3. `checks run DEV-01 --scope task --mode stale`: rc=0, payment=not_applicable, счётчик остаётся 2.
4. `checks_gate.evaluate(...scope=task)` возвращает ok=true, uncovered=[], payment=not_applicable при последнем failed.

Источник: checks_runner.py:553–561 сохраняет старый baseline при ошибке; checks_select.py:589–596 выбирает применимость по diff и пропускает анализ истории у not_applicable. Условие полного покрытия при записи baseline необходимо, но недостаточно для AC-01. Потеря остаётся после уже установленного успешного baseline; история непокрытого обязательства не учитывается. Нарушены R-11/R-16 и AC-01 карточки.

Исправление: сохранять непокрытые обязательства независимо от наличия diff, не теряя законный cross-task reuse; добавить постоянную CLI-регрессию passed project → failed project → task stale/gate. До нового успешного покрытия task gate должен оставаться закрытым, stale — снова исполнять payment. Аналогично проверить timeout/blocked/manual и запуск другой задачи.

Воспроизведение из корня: `python3 work/verification/PB-073/audit-01/probe_baseline_failure.py`. Скрипт выводит факты, rc=0 не означает исправления; смотреть stale_rc, attempts, gate.

## Другие проверки

- До аудита `verify PB-073 --audit-ready`: rc=0, передача/verify свежие; финальный отчёт исполнителя 1021b993b3c544938fb0bad5e4eda992: passed.
- test_checks_run: 45 passed; test_checks_select: 34 passed.
- Исходная controlled-clock проба A-02: поздний failed выбран; project gate закрыт. Повтор probe_fixes: rc=0, исходный сценарий исправлен, законный cross-task reuse сохранён.
- Новый cross-task контроль с task_projection: исходное обязательство исполняется заново для первой задачи, покрытие не подменяется результатом второй (не является находкой).
- Новый self-pilot --plan-only: доходит до protected boundary, затем command в sandbox-exec падает rc=71. Полный пилот и protected-развёртывание независимо не подтверждены. В этом ограниченном окружении test_checks_gate: 4 failures/4 errors из 18; сбои protected verifier/aggregate не приписаны диффу PB-073.
- Первый запуск test_checks_select/probe_fixes совпал с pending prepare-audit; повторы после завершения операции прошли. Сбой окружения не считается дефектом продукта.
- Полный verify и двухчасовой пилот не повторялись: приёмка уже блокирована воспроизведённым P1. Архив и исходники не изменялись аудитором.
