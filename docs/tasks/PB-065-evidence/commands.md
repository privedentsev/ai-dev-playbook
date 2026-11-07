# PB-065: команды воспроизведения

## Адресные проверки (контракты политики, графа, evaluator, форматов, CLI)

```sh
python3 -m unittest tests.test_block_workflow -v
```

Покрытие: схемы (неизвестные версии/поля), members/final_audit/checkpoints
(разбиение mixed, повторное членство), implementation_edges (per_task-
отказ, self-edge, дубли, циклы), card refs (inline и external .meta.json),
Depends-On done-семантика и запрет AUDIT ↔ members/fix в final/mixed,
evaluate() для start/check/handoff/audit_ready/accept/close/plan по всем
режимам и ролям (member, final_audit, fix), block_status, валидаторы
review package/findings report/block receipt, capability-gate,
`policy preview|apply` (атомарная запись политики + ссылок, идемпотентность,
отказ понижения ревизии), `block status` и отложенные команды.

## Регрессия (после интеграции в documents/lifecycle/playbook)

```sh
python3 -m unittest discover -s tests -v   # полный набор через verify
```

Прогон подмножества во время разработки: 422 теста — OK.

## Структурные проверки

```sh
python3 ai-dev-playbook/scripts/playbook.py index
python3 ai-dev-playbook/scripts/playbook.py check --ready
python3 ai-dev-playbook/scripts/playbook.py verify PB-065 --preflight
python3 ai-dev-playbook/scripts/playbook.py verify PB-065
```

## Ограничения

- Первый прогон verify `6e07d4401b454e5aad7c1b25948945d9` остановлен по
  timeout: полный набор вырос до 744 тестов (~1245s) и не уложился в
  прежний лимит 1200s. Лимит `unit-tests` в project.json поднят до 1800s;
  отчёт сохранён как честный failed, история не переписывается.
- `block prepare-review|revalidate` — PB-067, `create-fix` — PB-068,
  `accept|close` — PB-069: синтаксис зафиксирован, команды отвечают
  контролируемым отказом с именем владельца (код выхода 1).
- Runner конечного candidate и групповое закрытие не реализованы.
- Формат включён только при явной `documents.capabilities`:
  self-project её не содержит; legacy, inline/external metadata и
  старые схемы без поля `block` работают по-прежнему.
- evidence каталога содержит только материалы самопроверки исполнителя;
  независимая приёмка — отдельной сессией/инженером.
