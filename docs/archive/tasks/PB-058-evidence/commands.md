# PB-058: команды воспроизведения и ограничения

Среда: macOS, Python 3.14, checkout `feature/dependency-aware-verification`;
изолированные файловые fixtures — tempfile-деревья в тестах, хеши считаются
внедряемым `hash_fn` (обёртка над `project_view.digest_file`).

## Адресные проверки

```bash
python3 -m unittest discover -s tests -p "test_checks_inventory.py" -v
python3 -m unittest discover -s tests -p "test_checks_contract.py" -v
```

Факт: 62 теста `test_checks_inventory` — классификация модулей и
unknown_paths, служебные и одобренные исключения с причиной, коллизия
исключения с обнаруженным входом (включая фильтр модуля и исключённое
поддерево), symlink/escape/cycle/специальные файлы, add/delete/rename,
равные mtime/size при других байтах, absent, одно хеширование файла на
снимок, используемые PNG/ZIP как входы, разделение содержательного и
служебного в task_projection, отдельный формат снимка (lifecycle-подобный
`{files}` отклонён), гонки при хешировании, составе и появлении
absent-входов/предков (включая общий префикс и исключённое поддерево),
повреждённый/утраченный/чужой baseline — `complete: false`, полный охват
объявленного каталога при объединении с модульным фильтром, метаданные
первого хеширования при перекрытии входов, литеральный путь модуля как
вход, совместный сценарий policy → inventory → resolved_inputs →
snapshot.

## Структурные проверки

```bash
python3 ai-dev-playbook/scripts/playbook.py index
python3 ai-dev-playbook/scripts/playbook.py check --ready
python3 ai-dev-playbook/scripts/playbook.py adopt . --force --apply
```

## Полная проверка

```bash
python3 ai-dev-playbook/scripts/playbook.py verify PB-058
```

Отчёт runner сохраняется в `work/verification/PB-058/<run_id>/report.json`.

## Ограничения

Самопроверка исполнителя. Слой предоставляет inventory/snapshot/diff;
выбор обязательств, explain, runner и reuse — PB-059/060, доверенный
контур — PB-061. Режим в self-project не включён (`checks_policy`
отсутствует в project.json). Гонки проверяются внедрением мутаций через
`hash_fn`, не реальным параллельным writer.
