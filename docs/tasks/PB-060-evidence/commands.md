# PB-060 — команды воспроизведения и ограничения

Среда: macOS, Python 3.14, checkout feature/dependency-aware-verification; изменения не закоммичены. trust_mode проектных политик в fixtures — local.

## Адресные проверки

```sh
python3 -m unittest discover -s tests -p "test_checks_run.py" -v
python3 -m unittest discover -s tests -p "test_checks_select.py" -v
```

`test_checks_run.py` (30 тестов): адаптер unittest в реальном subprocess (green/zero-tests/skip/пропавший ID/crash/subtest/expected_failure/unexpected_success/timeout), append-only результаты и индекс, freshness по входам/графу/среде/TTL/always_run/version, поздний failed, повреждённые запись и индекс, мутация входов во время команды, cross-task reuse в одном проекте и отказ для другого project_id, force/project scope, `checks run|status` через playbook.main на установленном временном проекте.

## Полный прогон (действующий контракт, без opt-in экономии)

```sh
python3 ai-dev-playbook/scripts/playbook.py index
python3 ai-dev-playbook/scripts/playbook.py check --ready
python3 ai-dev-playbook/scripts/playbook.py verify PB-060
```

## Ручное воспроизведение на временном проекте

```sh
python3 ai-dev-playbook/scripts/playbook.py init /tmp/shop --apply
# в /tmp/shop: заполнить docs, добавить "checks_policy": "checks-policy.json" в ai-dev-playbook/project.json
python3 ai-dev-playbook/scripts/playbook.py --root /tmp/shop checks plan DEV-1
python3 ai-dev-playbook/scripts/playbook.py --root /tmp/shop checks run DEV-1 --scope task --mode stale
python3 ai-dev-playbook/scripts/playbook.py --root /tmp/shop checks status DEV-1
```

## Ограничения

- Локальные результаты диагностические: `trust_mode=local` не защищает от владельца проекта; квитанции и изоляция кандидата — PB-061. Подключение политики к self-project — PB-063.
- Адаптер покрывает `python -m unittest discover|имена`; `-k` и иные команды отклоняются. Command-проверки — по exit-коду без счётчика тестов; manual — not_run.
- Мутация входов во время запуска проверена командой, пишущей в собственный вход; реальный параллельный writer не моделировался.
