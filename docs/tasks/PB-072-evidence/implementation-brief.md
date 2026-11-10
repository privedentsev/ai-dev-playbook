# PB-072 — памятка исполнителю

Статус: подготовка; будущий интерфейс relocate-material ещё не реализован. При старте читать [карточку](../PB-072.md), [требования](../../specs/material-archive-safety.md), [design](../../designs/material-archive-safety.md) и [матрицу](test-matrix.md).

## Причина и материал

Копию штатного report.json вручную положили в owner-evidence и зарегистрировали под коротким ID. Handoff отдельно зарегистрировал оригинальный runner-каталог. Исходные пути разные, будущий архивный report.json один. Отказ close предотвращает перезапись. Ранний каталог сейчас такой конфликт не считает. Исходные пути/хеши и сохранённая ошибка — [source-case.json](source-case.json); сами отчёты повторно сюда не копировать.

## План исправления текущего случая

1. Сохранить baseline и SHA обоих исходных файлов; сначала воспроизвести на tempfile-дереве. Не использовать рабочую PB-060 как разрушаемый тестовый fixture.
2. После реализации новой команды подготовить preview переноса только `verify-070dadde` в `docs/tasks/PB-060-evidence/manual-reports/070dadde-report.json`. Оригинальный runner-каталог не переносить при этой операции.
3. Проверить before/after bytes, цели ссылок, новый source, companion и разные архивные назначения. Испытать отказ, final failure и restore на fixture.
4. Рабочую нормализацию включить в EXEC PB-072 до финального verify. Сохранить старые записи PB-060 и исходные receipts; не править вручную journal/check/audit hashes ради current.
5. Проверить актуальность PB-060. Если входы изменились, нужны штатные новые verify/CHECK/handoff и приёмка; `--reuse` допустим только при доказанном совпадении всех legacy-входов. Из-за глобальных docs/scripts старый полный отчёт может не подходить. Не объявлять отказ freshness новой коллизией.
6. Добиться успешного close --dry-run PB-060 после её действительных gate; реальное закрытие выполняется только по поручению. PB-072 не зависит от закрытия PB-060.

## Проверки реализации

```sh
python3 -m unittest discover -s tests -p 'test_material_registration.py' -v
python3 -m unittest discover -s tests -p 'test_audit_material_safety.py' -v
python3 -m unittest discover -s tests -p 'test_verification_preflight.py' -v
python3 -m unittest discover -s tests -p 'test_close_lifecycle.py' -v
python3 -m unittest discover -s tests -p 'test_lifecycle.py' -v
# После добавления отдельного набора переноса:
python3 -m unittest discover -s tests -p 'test_material_relocation.py' -v
python3 ai-dev-playbook/scripts/playbook.py adopt . --force --apply
python3 ai-dev-playbook/scripts/playbook.py index
python3 ai-dev-playbook/scripts/playbook.py check --ready
python3 ai-dev-playbook/scripts/playbook.py verify PB-072 --preflight
python3 ai-dev-playbook/scripts/playbook.py verify PB-072
```

Новый набор тестов и команда relocate-material сейчас отсутствуют; эти команды — план проверки реализации, не выполненные результаты. Порядок и результаты фактических запусков записать в CHECK. Тяжёлый полный runner не запускать до готовности материалов и ранних проверок. Структурный check не проверяет поведение.

## Ограничения

Один активный владелец и один локальный файл; без shared/архивных материалов, директорий, слияния по SHA и обхода независимой приёмки. Нельзя автоматически менять другие проектные политики, старые аудиты или чужой незакоммиченный код. Все новые доказательства имеют собственного владельца PB-072.
