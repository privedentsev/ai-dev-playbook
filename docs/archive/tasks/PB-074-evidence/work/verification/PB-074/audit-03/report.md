# PB-074 — третий независимый аудит

## Повторный аудит 2026-11-17T00:54:53+03:00; исполнитель: Codex; модель: GPT-6.

Вердикт: **accepted**, HANDOFF-03. A-03 исправлена; A-01/A-02 закрыты предыдущим аудитом. Новых замечаний нет. Приёмка относится только к PB-074; общий AUDIT-009 этим не закрывается.

Кандидат: рабочее дерево поверх `6311aa96f59db025b4fde3919775e03760193b3a`; macOS 27.2.1, Python 3.14.6. Аудитор не менял реализацию, требования, self-manifest и переданную историю. Закрытие PB-074 после успешной приёмки явно поручено инженером в текущем запросе.

### Проверка критериев

- **AC-01**: повторены реальные CLI-контрпримеры directory/delete/corrupt из audit-01. В каждом случае task gate после утраты и rebuild закрыт, plan/status показывают run, stale rc=1 и attempts=3. [directory](directory.json), [delete](delete.json), [corrupt](corrupt.json). Регрессии общего набора покрывают failed/timeout/blocked/manual, другой task, повторный stale и восстановление cross-task reuse после нового passed и восстановления целостности истории.
- **AC-02**: повторены режимы clean/default/forward составной legacy-пробы audit-02. После утраты failed project/task gate закрыты, task stale реально исполняет проверку (attempts_after_stale=3), новый seq-passed восстанавливает покрытие (attempts_total=4). [clean](clean.json), [default](oldindex.json), [forward](forward.json). Теперь default через исправленный update_index сам формирует failed-индекс; отдельные unit/E2E принудительно создают старый passed-индекс и доказывают корректный rebuild. Матрица unit-регрессии проверяет оба UUID-порядка × корректный/старый индекс, rebuild до/после удаления, оба gate, stale и восстановление. Однозначная legacy-история и порядок seq-записей не регрессировали.
- **AC-03**: независимо выполнен весь `test_checks_*`: **344 теста, OK**, 243.348 с ([лог](checks.log)). Набор включает 10 unit + 11 E2E адресных регрессий PB-074, а также selector/run/gate/trust, opt-in/legacy и lifecycle-сценарии. Прогон выполнен вне внешней песочницы, чтобы вложенный sandbox-exec не отклонялся средой аудиторского инструмента; subprocess-защита продукта не отключалась. Действующий отчёт штатного verify `1c159a084277437c9f8de5cdc3643b66`: passed, inputs_unchanged=true, self-check/preflight-regressions/unit-tests passed. По CHECK-05 полный unit-tests содержит 1113 тестов; полный legacy verify независимо не повторялся.

### Почему A-03 закрыта

Проверены все точки порядка: selector, update_index, сверка в load_results и сохранение прежней записи в rebuild_index используют `_latest_better`. Claim индекса содержит status: при равных finished_at/run_seq утраченный failed важнее оставшегося passed независимо от UUID. Пригодный новый seq-passed сохраняет штатное восстановление и reuse. Фактические subprocess и gate-результаты подтверждают исправление, структурные проверки за это доказательство не выдаются.

### Процесс и ограничения

- Начальный `verify PB-074 --audit-ready` отказал по неподтверждённым критериям ([лог](audit-ready-initial.log)): A-03 до этого протокола остаётся open_finding для всех трёх критериев. Handoff/verify проверки выполняются до проверки критериев. После закрытия находки в AUDIT-03 и штатной синхронизации через card sync готовность повторно проверяется; исходный отказ не скрывается.
- Legacy-пробы — совместимые фикстуры с управляемыми временем/UUID. Естественная коллизия часов и реальная миграция не заявляются. В legacy-пробе runner/gate API исполняют реальный command subprocess; пробы directory/delete/corrupt используют полный CLI. Все проекты временные и без Git.
- Результаты local остаются диагностическими в смысле модели доверия. Protected-регрессии пройдены, но эксплуатация под отдельной OS/CI identity не доказана.
- Код пробы: [legacy](probe_legacy_loss.py), [CLI history](probe_history_loss.py). Для повторения из корня проекта передайте `.` первым аргументом скрипту, для legacy дополнительно `clean` либо `forward`.

- До закрытия сохранность архива, материалов AUDIT-009, PB-074-evidence и audit-01/audit-02 подтверждена: 3028 файлов, 0 изменений SHA-256 ([сверка](preservation.json)).

- После закрытия A-03 и card sync повторный audit-ready **прошёл**, подтвердив свежую передачу и все критерии ([лог](audit-ready-final.log)); preflight **прошёл**, подтвердив структуру и переносимость материалов ([лог](preflight.log)). Начальный отказ устранён штатной процедурой, без изменения реализации или старой истории.

- CLI-приёмка зарегистрирована: `lifecycle accept PB-074 --result accepted --kind acceptance --role auditor --actor Codex --model GPT-6`, audit_id=AUDIT-03 ([лог](accept.log)).

- `close PB-074 --dry-run` прошёл: проверено конечное архивное дерево; переносятся карточка/sidecar/материалы PB-074, обновляются индекс и входящие ссылки из AUDIT-009 ([план](close-dry-run.log)). Содержание и статус общего AUDIT-009 не принимаются этой операцией.
