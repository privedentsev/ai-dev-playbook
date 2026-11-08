# PB-057: команды воспроизведения

## Адресные проверки (схемы, валидаторы, классификация, CLI)

```sh
python3 -m unittest discover -s tests -p "test_checks_contract.py" -v
```

Покрытие: fixtures всех четырёх форматов (`tests/fixtures/checks_contract/`),
строгие валидаторы (неизвестные версии/поля, bool/NaN лимиты, повторные ID
и рёбра, неразрешённые ссылки `module:|subsystem:|check:`, конфликты
исключений с объявленными входами включая незаякоренные glob,
применимость/входы/freshness), контракт адаптера и test_catalog
(описания, раздельные допуски skip/expected_failure), привязки
result/receipt, `classify_policy_change` для extension/weakening/unchanged,
исполняемый allowlist новых проверок и запрет смены identity, отложенные
`checks plan|run|status` и `setup preview|apply` (код 1 с именем
владельца), оси `--scope/--mode` (argparse, код 2), `project.json →
checks_policy` в config() и обратная совместимость проекта без ссылки.

## Регрессия

```sh
python3 -m unittest discover -s tests -v   # полный набор через verify
```

## Структурные проверки

```sh
python3 ai-dev-playbook/scripts/playbook.py index
python3 ai-dev-playbook/scripts/playbook.py check --ready
python3 ai-dev-playbook/scripts/playbook.py verify PB-057 --preflight
python3 ai-dev-playbook/scripts/playbook.py verify PB-057
```

## Ограничения

- `checks plan` — PB-059, `checks run|status` — PB-060, `setup
  preview|apply` — PB-063: синтаксис зафиксирован, команды отвечают
  контролируемым отказом с именем владельца (код выхода 1).
- Анализ зависимостей, снимки, runner, квитанции и доверенный контур не
  реализованы; валидатор receipt проверяет формат, не подпись.
- Режим не включён в self-project: `checks_policy` в project.json
  отсутствует; legacy-контракты task_workflow/report неизменны.
- Селективный glob-исключение (литеральное имя на конце) допускается и
  внутри области, и незаякоренным; широкое исключение (литерал либо хвост
  `**`/`*`), накрывающее вход, — ошибка (см. docstring).
- evidence каталога содержит только материалы самопроверки исполнителя;
  независимая приёмка — отдельной сессией/инженером.
