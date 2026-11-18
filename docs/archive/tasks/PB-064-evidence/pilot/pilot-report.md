# PB-064 — отчёт self-pilot выбора проверок

Дата прогона: 2026-11-14. Стенд: macOS-27.2.1 x86_64, Python 3.14.6,
`sandbox-exec` присутствует, отдельной OS/CI identity нет.

Скрипт: `docs/tasks/PB-064-evidence/self_pilot.py`. Две чистые копии
репозитория без `.git`/`work/`: `copy_a_legacy` (без `checks_policy`)
и `copy_b_optin` (`setup apply`, политика `ai-dev-playbook-self-pilot`
rev 1, `trust_mode=local`, `fallback.max_seconds=7200`,
`max_bytes=250_000_000`). Отпечаток исходного репозитория до/после
совпал; `checks_policy` в основном проекте отсутствует.

Каталог проверок: 1082 экземпляра discovery / 1076 уникальных ID;
suite'ы opt-in: block=39, checks=313, cli=164, core=510,
verification=56.

## Замеры

### Шаг 1 — первичный полный baseline

| Метрика | legacy `verify` | opt-in `checks run --scope project --mode force` |
|---|---|---|
| Команд исполнено | 3 (`self-check`, `preflight-regressions`, `unit-tests`) | 7 (все проверки политики) |
| Стенных секунд | 1870.0 | 1815.2 |
| Хешировано файлов (суммарно) | 5718 | 4938 |
| Хешировано байт (суммарно) | 203 921 216 | 142 234 219 |
| Входов плана/снимка | 2859 уникальных файлов | plan: 105 371 303 Б / 3046 файлов; runner-снимок: 36 862 916 Б / 1892 файла |

Итог: `run_id 1bc6c0d1…` passed; opt-in `Итог: run=7` без reused/blocked.

### Шаг 2 — локальная правка `tests/test_checks_setup.py`

| Метрика | legacy `verify --reuse` | opt-in `checks plan` + `run --scope task --mode stale` |
|---|---|---|
| Команд исполнено | 3 (полный набор, reuse не помог) | 1 (`checks-unit-tests`, 124.0с) |
| Стенных секунд | 1862.9 | 133.1 (включая plan 4.7с; сам run 128.3с) |
| Хешировано файлов | 5718 | 3771 |
| Хешировано байт | 203 921 362 | 119 774 123 |
| reuse/not_applicable | — | `self-check: reused`; 5 проверок `not_applicable` |

Ключевой результат пилота: при точечной правке тестового файла opt-in
контур исполнил одну затронутую suite-проверку вместо полного
unittest-discovery (~124с против ~1736с у legacy `unit-tests`),
сохранив валидный результат `self-check` через reuse.

### Шаг 2b — транзитивная правка `ai-dev-playbook/scripts/checks_setup.py`

`checks plan` за 4.7с (123 803 637 Б / 3992 файла; граф 13 рёбер,
0 unknown-областей): все 7 проверок `run` — `checks_setup.py`
транзитивно импортируется `playbook.py`, поэтому зависимые suite'ы
консервативно выбраны полностью. Демонстрирует транзитивное
замыкание зависимостей (AC-02/AC-04 на реальном дереве).

### Шаг 3 — повтор без изменений и release-гейт

| Метрика | legacy `verify --reuse` | opt-in `checks plan` + `run --scope project --checkpoint release --mode stale` |
|---|---|---|
| Команд исполнено | 0 | 0 |
| Стенных секунд | 85.9 | 5.0 (run) + 5.0 (plan) |
| Хешировано файлов | 2859 | 4107 |
| Хешировано байт | 101 960 681 | 126 318 326 |
| Результат | reuse по входам | `reused=7`, непокрытых обязательств: 0 |

- `checks release-ready PB-064` (rev 1, local): 7 покрыто, готово
  (диагностическое покрытие).
- `lifecycle handoff` создал `checks-coverage-report` (`отчётов: 1`);
  повторный идентичный handoff идемпотентен — число отчётов осталось 1
  (логи `36-`/`37-`; флаг `handoff_idempotent` в `summary.json`
  зафиксировал несовпадение формулировки stdout, поведение проверено
  по самим логам).
- `verify PB-064 --audit-ready`: пройден — передача и проверки свежие,
  независимый аудит не выполнен.
- `close PB-064 --dry-run`: отказ rc=1 — «нет последнего
  accepted-аудита».

## Границы доверия local vs protected

На отдельной временной копии с policy rev 2 (`trust_mode=protected`):

- `verifier run` по проверке `kind=test` отклонён rc=1 — самозаявленные
  unittest-исходы в защищённом режиме не принимаются.
- Command-only набор (`preflight-regressions`): `verifier init`,
  `approve`, `probe`, `run` — rc=0; квитанция
  `rc-4422cbcd47aa4536` прошла `verifier verify` и `verifier gate`
  (coverage ok, `capability=local`).
- `checks release-ready` под rev 2: отказ rc=1 — покрытие квитанциями
  полное, но граница OS/CI identity не доказана (R-20);
  `protected_confirmed` не заявляется.

## Найденные и исправленные дефекты по ходу пилота

1. `checks_plan` не передавал проекции карточки/политики в
   `checks_select.plan` — `checks plan` падал с
   «отсутствует проекция task_projection», когда уже существовали
   результаты и проверка объявляла `task_projection`-вход.
   Исправлено в `ai-dev-playbook/scripts/playbook.py`; регрессионное
   утверждение добавлено в `tests/test_checks_e2e.py` (AC-09).
2. `self_pilot.py`: лимит `max_seconds` 3600→7200 (оценка полного
   обязательного набора 4680с) и очистка чужих `test*`/`checks_*`
   модулей `sys.modules` между копиями (коллизия имён discovery).

## Ограничения

- `trust_mode=local`: результаты диагностические; защищённое
  подтверждение требует контура PB-061 и отдельной OS identity.
- Verifier arena материализует кандидата без `work/`: `self-check`
  там невыполним, поэтому rev-2 демонстрация использует command-only
  подмножество.
- Политика opt-in собрана вручную в скрипте; self-project её не
  принимает — применение только к временным копиям.
- Замеры однопроходные на одной машине; проценты экономии не
  заявляются — только счётчики команд, файлов, байтов и секунд.
- `handoff_idempotent` в `summary.json` = false — артефакт проверки
  точной фразы stdout; фактическая идемпотентность подтверждена
  логами (отчётов: 1 → отчётов: 1).

## Воспроизведение

```bash
python3 docs/tasks/PB-064-evidence/self_pilot.py . /tmp/pb064-pilot-out
python3 docs/tasks/PB-064-evidence/self_pilot.py . /tmp/pb064-pilot-plan --plan-only
```
