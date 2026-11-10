# Независимый аудит PB-032

Дата: 2026-10-29T05:50:34+03:00. Исполнитель: Codex. Модель: GPT-6.
Результат: changes_requested. Приёмка и закрытие не выполнены.

## Проверки

- Read-only `verify PB-032 --audit-ready`: PASS до регистрации контейнера аудита; свежая передача и успешный полный отчёт 4799df4fdd774dbc9bc92f7ae4770f90 подтверждены. Полный набор из 635 тестов заново не запускался.
- Независимый повтор `python3 -m unittest discover -s tests -p test_verification_policy.py -v`: 16/16 PASS, targeted-tests.log.
- Независимые пробы `python3 work/verification/PB-032/audit-01/probe.py`: дефект воспроизведён для command и manual, probe.log. Контрольные сценарии: согласованные снимки разрешаются, устаревший input_snapshot отклоняется.
- Изучены карточка, implementation.md, R-08/11–13/19–23, design 2.4–2.5, реализация policy и валидатор отчётов PB-031.

- После записи протокола `index --all`, последовательный `verify PB-032 --preflight` и `git diff --check`: PASS. Промежуточный отказ preflight во время ещё выполнявшегося index и успешный повтор сохранены в validation.log; это ошибка порядка команд аудитора, не дефект PB-032.

## A-01 — P1: reuse допускает неподтверждённую версию входов

Место: ai-dev-playbook/scripts/verification_policy.py, reusable().
Функция сравнивает result.input_snapshot с текущим input_snapshot, но для command проверяет лишь snapshot_before == snapshot_after, без связи с input_snapshot. Для manual candidate_snapshot вообще не участвует в сравнении. Поэтому schema-valid passed, фактически относящийся к старой версии, считается переиспользуемым для новой.

Воспроизведение: probe.py создаёт old snapshot файла src.py, меняет содержимое и получает current snapshot. У command input_snapshot=current, snapshot_before=snapshot_after=old; у manual input_snapshot=current, candidate_snapshot=old. Оба полных отчёта успешно проходят verification.validate_report; затем reusable возвращает True вместо False. Политика, definition_hash и среда совпадают. Это внутренняя несогласованность снимков, а не проверка личности/provenance или выбора последнего результата будущим runner.

Влияние: неподтверждённый passed может пережить изменение реализации и быть использован как свежий. Нарушены R-19/R-20, AC-09 и критерий карточки о reuse только при неизменных определениях/входах.

Критерий исправления: связывать все снимки одного результата с заявленными явными входами (command: input_snapshot/before/after; manual: input_snapshot/candidate), отклонять несовпадающие/неизвестные связи. Добавить обе негативные регрессии и положительные контроли; свежие verify/handoff, затем повторный независимый аудит.

## Границы

Код реализации и постоянные тесты аудитором не изменялись. Runner v2 и CLI gate остаются вне PB-032. Существующая запись PB-032 в Unreleased не редактировалась: условие инженера для закрытия и заполнения changelog не выполнено.
