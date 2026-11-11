# PB-061 — независимый аудит 05

Результат: accepted; A-06 и A-07 закрыты.

- EXEC-04 прямо ссылается на A-06/A-07; старый журнал сохранён.
- Полный отчёт f23706d1d192477eba0a72343324d903: passed, 1024 теста; execution_id/SHA-256 совпадают с EXEC-04. Независимый пересчёт: fresh=true, changed=[]. Полный suite повторно не запускался.
- Независимо запущены 255 регрессий test_checks_*.py: OK, включая 43 проверки доверия, блок self-reported kind=test, forgery/replay/cross-project/revocations, транзитивный отзыв aggregate, окружение и реальный sandbox-exec.
- Начальный запуск внутри sandbox Codex дал ограничения вложенного sandbox-exec; повтор вне sandbox во время prepare-audit получил четыре отказа init из-за pending lifecycle operation. После завершения операции повтор всех 255 тестов прошёл. Исходные логи сохранены.
- Начальный audit-ready отклонён из-за незакрытых A-06/A-07 по AC-04; свежесть handoff/verify отдельно подтверждена. Закрытие замечаний выполняется AUDIT-05 и штатной приёмкой, без обхода gate.
- Сверены код verifier/consumer, изменения runner/adapter/CLI, требования R-17/R-18/R-20, deployment contract и changelog. Новых дефектов не обнаружено.
- Ограничения: отдельной OS/CI identity нет, capability=local; защищённая эксплуатация не принята. Подтверждаются наблюдаемые command-результаты; kind=test отклоняется. Интеграция lifecycle/release и opt-in остаются PB-062/PB-063.

Доказательства: tests.log, freshness.json; исходные отказы tests-codex-sandbox.log и tests-during-prepare.log. Последующие preflight/accept/close проверяются штатным CLI.
