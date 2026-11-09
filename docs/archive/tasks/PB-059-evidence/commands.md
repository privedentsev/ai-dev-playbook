# PB-059 — команды воспроизведения и ограничения

## Адресные проверки

```bash
# Граф: AST-анализатор, union, unknown, closure (22 теста)
python3 -m unittest discover -s tests -p "test_checks_graph.py" -v

# Селектор: границы расширения, explain, обязательства, бюджет,
# reuse-оценка, read-only, CLI checks plan (31 тест)
python3 -m unittest discover -s tests -p "test_checks_select.py" -v

# Регрессии контракта и инвентаря (смежные модули PB-057/058)
python3 -m unittest discover -s tests -p "test_checks_*.py" -v
```

## Полный набор

```bash
python3 ai-dev-playbook/scripts/playbook.py verify PB-059 --preflight
python3 ai-dev-playbook/scripts/playbook.py verify PB-059
```

## Ограничения

- `checks plan` на self-project отвечает контролируемым отказом:
  `checks_policy` не подключена (PB-063). Поведение проверено на
  изолированных tempfile-проектах и установленном комплекте в тестах.
- Хранимое состояние `work/checks/` (baseline-снимок, граф, результаты)
  производят PB-060/063; `checks plan` только читает их и при утрате
  расширяет набор консервативно.
- Оценка reuse проверяет binding (policy/definition/closure/input/
  environment digests, adapter/runner, поздний failed); фактический
  runner и формат хранения результатов — PB-060, identity runner
  зафиксирован константой `RUNNER`.
- Подсчёт `hashed_bytes` приблизителен по st_size; гонки файловой
  системы контролирует слой PB-058, а не счётчик.
