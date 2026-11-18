# PB-073 — отчёт self-pilot выбора проверок (повтор после архивирования PB-064)

Дата прогона: 2026-11-16. Стенд: macOS-27.2.1 x86_64, Python 3.14.6,
`sandbox-exec` присутствует, отдельной OS/CI identity нет.

Скрипт: `docs/tasks/PB-073-evidence/self_pilot.py` — новый активный
инструмент, наследник архивного `docs/archive/tasks/PB-064-evidence/
self_pilot.py` (архив не изменялся). Карточка PB-064 архивирована и не
активна, поэтому на каждой временной копии инструмент создаёт
синтетическую карточку `PILOT-01` штатными файлами (`docs/tasks/
PILOT-01.md` + `.meta.json`) и командой `index`; в исходном проекте
карточки PILOT-01 нет. Две чистые копии репозитория без `.git`/`work/`:
`copy_a_legacy` (без `checks_policy`) и `copy_b_optin` (`setup apply`,
политика `ai-dev-playbook-self-pilot` rev 1, `trust_mode=local`,
`fallback.max_seconds=7200`, `max_bytes=250_000_000`). Отпечаток
исходного репозитория до/после совпал; `checks_policy` в основном
проекте отсутствует.

Каталог проверок: 1092 экземпляра discovery / 1086 уникальных ID
(6 ID разделяются smoke-набором `load_tests`); suite'ы opt-in:
block=39, checks=323, cli=164, core=510, verification=56.

## Замеры

### Шаг 1 — первичный полный baseline

| Метрика | legacy `verify` | opt-in `checks run --scope project --mode force` |
|---|---|---|
| Команд исполнено | 3 (`self-check`, `preflight-regressions`, `unit-tests`) | 7 (все проверки политики) |
| Стенных секунд | 1983.7 | 1917.5 |
| Хешировано файлов (суммарно) | 6066 | 5121 |
| Хешировано байт (суммарно) | 221 698 872 | 151 478 026 |
| Входов плана/снимка | 3033 уникальных файла | plan: 114 366 394 Б / 3229 файлов; runner-снимок: 37 111 632 Б / 1892 файла |

Итог: opt-in `Итог: run=7` без reused/blocked; все 7 результатов
`passed` (`work/checks/results/<run_id>/` на копии).

### Шаг 2 — локальная правка `tests/test_checks_setup.py`

| Метрика | legacy `verify --reuse` | opt-in `checks plan` + `run --scope task --mode stale` |
|---|---|---|
| Команд исполнено | 3 (полный набор, reuse не помог) | 1 (`checks-unit-tests`, 181.8с) |
| Стенных секунд | 2047.4 | 186.9 (включая plan 5.2с; сам run 181.8с) |
| Хешировано файлов | 6066 | 3954 |
| Хешировано байт | 221 699 018 | 128 930 545 |
| reuse/not_applicable | — | `self-check: reused`; остальные проверки `not_applicable` |

Ключевой результат пилота сохранён: при точечной правке тестового
файла opt-in контур исполнил одну затронутую suite-проверку вместо
полного unittest-discovery (~182с против ~1910с у legacy
`unit-tests`), валидный `self-check` сохранён через reuse.

### Шаг 2b — транзитивная правка `ai-dev-playbook/scripts/checks_setup.py`

`checks plan` за 5.2с (read-only, без исполнения): граф 13 рёбер,
0 unknown-областей; `core-unit-tests` и `cli-unit-tests` выбраны
`run` (транзитивное замыкание через `playbook.py`), `block-unit-tests`
также в наборе запуска — консервативный выбор на реальном дереве.

### Шаг 3 — повтор без изменений и release-гейт

| Метрика | legacy `verify --reuse` | opt-in `checks plan` + `run --scope project --checkpoint release --mode stale` |
|---|---|---|
| Команд исполнено | 0 | 0 |
| Стенных секунд | 94.4 | 5.0 (run) + 5.1 (plan) |
| Хешировано файлов | 3033 | 4290 |
| Хешировано байт | 110 849 509 | 135 451 698 |
| Результат | reuse по входам | `reused=7`, непокрытых обязательств: 0 |

- `checks release-ready PILOT-01` (rev 1, local): rc=0 — покрытие
  диагностическое.
- `lifecycle handoff` создал `checks-coverage-report`; повторный
  идентичный handoff идемпотентен — rc=0, отчётов не прибавилось
  (логи `36-`/`37-`; флаг `handoff_idempotent` в `summary.json` —
  false, артефакт проверки точной фразы stdout, как в PB-064).
- `verify PILOT-01 --audit-ready`: rc=0 — передача и проверки свежие,
  независимый аудит не выполнялся.
- `close PILOT-01 --dry-run`: отказ rc=1 — «нет последнего
  accepted-аудита».

## Границы доверия local vs protected

На отдельной временной копии (`protected_probe_copy`, policy rev 2,
`trust_mode=protected`, command-only подмножество без `self-check`):

- `verifier run` по проверке `kind=test` отклонён rc=1 —
  самозаявленные unittest-исходы в защищённом режиме не принимаются.
- Command-only набор: `verifier init`, `approve`, `probe`, `run` —
  rc=0; квитанция `rc-e1afece0d0cc4ca5` прошла `verifier verify` и
  `verifier gate` (coverage ok, `capability=local`).
- `checks release-ready` под rev 2: отказ rc=1 — покрытие
  квитанциями полное, но граница OS/CI identity не доказана;
  `protected_confirmed` не заявляется.

## Изменения относительно пилота PB-064

- Фикстурная карточка `PILOT-01` создаётся на копиях (PB-064
  архивирована); исходный репозиторий и архив не изменяются.
- Каталог вырос: 1092 ID против 1082; suite checks 323 против 313
  (включая новые регрессии PB-073).
- Вывод запускается вне дерева проекта (`/tmp`), затем артефакты
  (`summary.json`, `commands.json`, `policy-applied.json`, `logs/`)
  перенесены в этот каталог; `tree_fingerprint` исходника совпал.

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
  логами (повторный handoff rc=0 без новых отчётов).

## Воспроизведение

```bash
python3 docs/tasks/PB-073-evidence/self_pilot.py . /tmp/pb073-pilot-out
python3 docs/tasks/PB-073-evidence/self_pilot.py . /tmp/pb073-pilot-plan --plan-only
```

Выходной каталог внутри репозитория допускается только внутри
`docs/tasks/PB-073-evidence` — при этом надо учитывать, что
`tree_fingerprint` покрывает `docs/`: прогон требует неподвижного
исходного дерева на всё время исполнения (~2 часа) и внешний вывод
является безопасным вариантом.
