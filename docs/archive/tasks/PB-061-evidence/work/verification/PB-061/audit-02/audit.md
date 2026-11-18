# PB-061: повторный аудит HANDOFF-02

Время: 2026-11-10T20:58:29+03:00; исполнитель: Codex; модель: GPT-6.1 Sol (Medium).

Вердикт: **changes_requested**. A-02, A-03, A-04 закрыты; A-01/P1 остаётся открытым; добавлено A-05/P2. Полный протокол и критерии исправления — AUDIT-02 карточки PB-061.

## Независимые сценарии

Команда из корня: `python3 work/verification/PB-061/audit-02/probes.py`. Только временные проекты, cleanup в finally; рабочая реализация не изменяется. Требуется среда, позволяющая реальный sandbox-exec.

- ACK_FORGERY: тестовый модуль без TestCase печатает ожидаемый JSON с CHECKS-REPORT-V1, читает ack из stdin и завершает процесс os._exit(0). Все run/verify/gate возвращают true при отсутствии определённых и выполненных тестов. Живость процесса не подтверждает происхождение отчёта: недоверенный процесс получает тот же канал ack. A-01 не исправлено. Нужно доказанное происхождение результата либо явная блокировка защищённого подтверждения для self-reported adapter; одного текста об ограничении недостаточно.
- CHMOD_PROBE: тот же UID и writable key, chmod 0555 корня; capability=local, separate_identity=false. Прежний обход A-02 закрыт.
- DIRECTORY_WRITABILITY: существующие каталоги дом/родительский каталог/keys получают null из-за EEXIST от O_CREAT|O_EXCL на самом каталоге. Это проверка существования пути вместо права записи внутри него; положительный protected остаётся недостижимым через этот путь (A-05).
- PROTECTED_ENV: command с required_environment/always_run проходит; HOME/TMPDIR внутри arena, PATH есть. ORDINARY_ENV: обычный runner наследует контрольную переменную. A-03 закрыто.
- TRANSITIVE_REVOCATION: после отзыва run исходный receipt, aggregate1, aggregate2 и gate отклоняются, даже если публичные копии источников удалены. A-04 закрыто.

Факты сохранены в probes.log. В исходниках: checks_unittest.collect ack подтверждает любой живой процесс, испускающий строку отчёта; checks_trust._home_writability вызывает _writable для существующих каталогов с append=False.

## Проверки и ограничения

`python3 -m unittest discover -s tests -p 'test_checks_*.py'`: повтор после завершения prepare-audit — **250 тестов, OK**, tests.log. Первый прогон tests-during-prepare.log содержит 13 отказов CLI из-за незавершённой операции prepare-audit; причиной был порядок запуска аудитором, это не finding реализации.

`verify PB-061 --audit-ready`: отказ «критерии не подтверждены свежими проверками» на criterion_view после проверки актуальной передачи/отчётов. В предыдущем аудите остаются открытые находки, поэтому computed checkboxes сняты. `card show` подтверждал current HANDOFF-02; отчёт 07e20714fa594fe9b36fee0ad743ecb1 — passed, inputs_unchanged=true. Полный legacy suite в повторном аудите не запускался.

Отдельная OS/CI identity не предоставлена; защищённое production-развёртывание не проверялось и не принималось. История HANDOFF-01/AUDIT-01 и реализация не переписывались.
