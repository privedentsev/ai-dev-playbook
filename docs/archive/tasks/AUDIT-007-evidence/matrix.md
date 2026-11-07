# Матрица AC-01–14

Источники: spec:proportional-workflow, revision 1, change proportional-workflow; design и ADR-0003. Все команды собственных проб — subprocess против установленной копии CLI. Общая регрессия запускается настроенным verify AUDIT-007; фактический результат — CHECK карточки.

| AC | Проверка | Вывод |
|---|---|---|
| 01 | independent-policy: обычная DEV-1, индекс, start, штатный close/archive | PASS |
| 02 | manual-terminal: три изменения src/a, один ID; старые candidate отклоняются, до handoff in_progress | PASS (синтетический просмотр) |
| 03 | Сверка workflow/tasks/guide; quick_route revise/raise и lifecycle route approval/history в общей регрессии | Нормативный запрет нового поведения; CLI не анализирует семантику кода |
| 04 | manual-only — 0 команд, изменение a.txt оставляет b=1; verification_preflight/policy проверяют обязательные ID и исключения | PASS этих сценариев; A2 ограничивает независимость при смене команды |
| 05 | manual без наблюдения не принят; устаревшие входы/сценарий отклоняются; terminal+подтверждение отвергается без browser | **FAIL A3** |
| 06 | failed-and-preflight: exit 7 и timeout 0.1 s блокируют handoff/close; failed exclusion в общей регрессии | PASS воспроизведённых отказов |
| 07 | испорченный индекс даёт preflight exit 1 до запуска; handoff повторно актуален без роста счётчиков; preflight/readiness suite | PASS |
| 08 | служебные переходы не повторяют команды; чужой/повреждённый лог отклоняется; environment-reuse после смены среды разрешает приёмку | **FAIL A1** |
| 09 | src/a изменение повторяет только a; argv a повторяет также b; смена среды не повторяет команду, которая теперь падает; race suite | **FAIL A1, A2** |
| 10 | quick close без acceptance и роль auditor отклонены; явная синтетическая engineer acceptance разрешает archive | PASS согласованности; CLI не удостоверяет личность |
| 11 | recovery_probe: SIGKILL/restore/repeat, настоящий чужой flock, tampered log; новые 14 файлов archive bytes; прежние 351 relocation hashes | PASS этих проб; update/close recovery дополнительно в общей регрессии |
| 12 | workflow_install/schema/legacy-v1 tests: init/update, опт-ин, сохранение команд, old CLI refusal; сверка owning source | Результат поведенческого набора — CHECK |
| 13 | workflow_e2e checkpoint и verification_policy: local отдельно от release, counters; guide не обещает внешнюю блокировку | Результат поведенческого набора — CHECK |
| 14 | workflow_e2e small/medium/large, close_lifecycle block audit/promotion/archive; сверка rules/guide и Git explicit_request | Маршруты и ограничения сохранены; итоговая приёмка блока отклонена |

Непроверенное: настоящий UI/браузер и внешние merge/release-интеграции. Тестовые заявления инженера исключительно синтетические. Существующий suite не содержит регрессий A1–A3; его PASS не равен приёмке всей матрицы.
