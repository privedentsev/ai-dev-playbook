# AUDIT-009 — повторный независимый аудит audit-04

Вердикт: **changes_requested**. Новая **A-07 (P1)**: `verifier gate` возвращает полное покрытие и CLI rc=0 после реально более позднего failed того же кандидата. Исправление A-06 и принятые исправления PB-078 подтверждены; этого недостаточно для приёмки всего блока. AUDIT-009 остаётся in_progress. Исправление: [PB-079](../../PB-079.md).

## Повторный аудит 2026-11-17T14:26:00+03:00; исполнитель: Codex; модель: GPT-6.

Кандидат `1c99fe8`, полная идентичность/среда/SHA-256 модулей: [candidate.json](candidate.json). Прочитаны актуальные передачи AUDIT-009/PB-078, изменения общего состояния порядка, индекса, runner и protected coverage, протокол PB-078 audit-05. Самостоятельные исправления не выполнялись, архивные карточки не переоткрывались. Штатный snapshot/provenance: `work/verification/AUDIT-009/audit-04/`.

## A-07 — Эталонный verifier gate игнорирует поздний неуспех

Серьёзность: **P1**. Критерии карточки: AC-01, AC-02, AC-03; спецификация R-11/R-16, сквозные AC-05/AC-08/AC-09. Эталонный CLI consumer не использует новый общий расчёт актуального покрытия.

Воспроизведение из корня репозитория:

```bash
python3 docs/tasks/AUDIT-009-evidence/audit-04/probe_verifier_gate.py .
```

Проба использует временный проект и trusted home из постоянной fixture ProtectedLatestOutcomeTest. Выполнены настоящие verifier subprocess-команды с sandbox-exec, получены настоящие подписанные квитанции. Управляемый внешний flag меняет исход команды, оставаясь вне candidate: это тот же поддерживаемый сценарий позднего неуспеха внешнего входа, который используется постоянным тестом test_later_failed_blocks_earlier_passed. История, квитанции и UUID не редактируются; mock нет. Метки двух последовательных запусков заданы аргументом now, часы машины не менялись.

[Полное доказательство](verifier-gate.json):

1. Первый verifier run: **passed**, валидная подпись квитанции.
2. Второй verifier run на **том же candidate_digest** и той же одобренной политике: **failed**, валидная подпись квитанции. Первый finished_at=10:00, второй=10:01; отката часов или перекрытия здесь нет.
3. `checks_gate.evaluate` в областях task и project: probe в **uncovered**, covered=[]; поздний неуспех корректно блокирует старый успех.
4. `checks_verifier.gate` на тех же данных: **ok=true, covered=1, obligated=1, rejections=[]**.
5. Реальный subprocess CLI `playbook.py --root <tmp-project> verifier gate --home <tmp-home>`: **rc=0**, stdout «обязательств 1, покрыто 1, квитанций 2».

Причина: `ai-dev-playbook/scripts/checks_verifier.py:561–585` обходит проектные копии квитанций и добавляет check_id в verified при **любом** passed. Последующий failed вообще не снимает покрытие. Строки 586–594 объявляют успех по этому объединению. Состояние порядка из `checks_select` и доверенная актуальная история, применённые в `checks_gate._protected_receipts`, здесь не используются.

Влияние: документированный эталонный consumer сообщает успешное покрытие и exit 0 при реальном текущем неуспехе; результат расходится с основным gate. Это отдельный маршрут, не исправленный PB-078. Сам локальный стенд не доказывает обход реальной OS/CI identity: capability=local и protected_confirmed=false честно сохранены обоими consumer. Возможное ложное protected_confirmed при реально защищённом развёртывании следует из общего кода, но на таком развёртывании не проверялось и не заявляется как измеренный факт.

Критерий исправления: `verifier gate` вычисляет актуальное покрытие тем же общим контрактом из доверенной истории и не принимает старый passed поверх более позднего failed/timeout/blocked. Порядок часов/seq, TTL, aggregate, отзывы и текущий candidate должны учитываться согласованно. Добавить реальную CLI-регрессию, доказать восстановление новым passed и независимость авторитетной истории от проектных копий. Отдельная задача — PB-079, без зависимости от незавершённой AUDIT-009.

## Повторные независимые результаты

| Сценарий | Результат |
|---|---|
| A-06 — исходный clock rollback | Task/project gate=false; latest=failed seq=1; stale реально исполняется и падает, attempts=3. [JSON](rollback.json). |
| Перекрывающиеся исполнения PB-078 A-01 | Поздно завершившийся failed не скрывается passed другого seq; gate=false, последующий stale реально исполняет payment. [JSON](overlap.json). |
| Утрата непобедившего nf-источника | После утраты и rebuild corrupt=payment, gate=false. [JSON](loss.json). |
| Исцеление + rebuild + новый rollback | Новый реальный passed восстанавливает gate=true; rebuild сохраняет nf; следующий противоречащий passed даёт gate=false/corrupt=payment. [JSON](healed-rebuild.json); [контроль без удаления](healed-control.json). |
| Старые A-04, corrupt/delete | До/после потери gate=false; stale rc=1, attempts=3. [corrupt](old-corrupt.json), [delete](old-delete.json). |
| Старый A-05, legacy равные часы | Latest=failed, gate=false. [JSON](old-legacy.json). |
| Полная матрица tests/checks | Состав: no-Git/subprocess counters; graph/add/delete/rename/dynamic/unknown; logs/card/binary; fallback/budget; release/cross-task/stale/force; adapter negatives; race/tamper/replay/revocation; manual/lifecycle/archive; legacy/opt-in/restore. **354 теста, OK, 258.133 с**, вне внешней песочницы ([журнал](checks-tests.log)). Новая A-07 этим набором не покрыта. |
| Самоприменение | Новый plan-only на побайтно совпадающей временной копии: rc=0, PLAN-ONLY OK; discovery 1123 экземпляра / 1117 уникальных IDs; метрики каталога/границ в [журнале](pilot-plan.log). |

Полный самопилот был запущен на отдельной временной копии: preview/apply/initial plan и legacy preflight прошли; после подтверждения A-07 остановлены только наши pilot/verify процессы (SIGTERM, rc=143). Это **незавершённый прогон**, не продуктовый failed и не подтверждённые сравнительные метрики. [Причина и процессы остановки](pilot-stopped.json), [частичный журнал](pilot-full.log), [сверка копии](source-snapshot-match.json). Полный legacy verify AUDIT-009 и полный self-pilot не завершались при уже известной блокирующей находке; исторические метрики PB-064/PB-073 не выдаются за новые.

## Процедура и следующий шаг

Начальный `verify AUDIT-009 --audit-ready` отказал: in_progress, нет актуальной передачи ([лог](audit-ready.log)). Не создавались фиктивные passed CHECK/HANDOFF ради приёмки. Отрицательное заключение сохраняется в собственной EXEC/CHECK и diagnostic lifecycle; result=cancelled/kind=diagnostic означает отрицательный протокол, **не отмену AUDIT-009**.

Реальная отдельная OS/CI identity не проверялась: trusted home тестовый и принадлежит той же OS identity. Модульные mock-сценарии и реальные subprocess на локальном стенде различаются. Protected-эксплуатация не принята. Архив и исходные чужие правки сохраняются. Следующий шаг — реализация PB-079 в отдельной сессии, её независимая приёмка, затем повтор AUDIT-009 и завершение оставшихся сквозных доказательств.
