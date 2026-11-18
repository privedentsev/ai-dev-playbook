import datetime
import difflib
import json
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).parent
sys.path.insert(0, str(ROOT / 'ai-dev-playbook/scripts'))
import playbook

log = (OUT / 'tests.log').read_text()
summary = re.search(r'Ran (\d+) tests in ([\d.]+)s\s+FAILED \(failures=1\)', log)
assert summary, 'Expected completed full test run with one recorded failure'
inputs = json.loads((ROOT / 'ai-dev-playbook/project.json').read_text())['verification']['inputs']
before = json.loads((OUT / 'snapshot-before.json').read_text())
after = json.loads((OUT / 'snapshot-after.json').read_text())
assert before == after, 'Inputs changed during independent probes/tests'
now = datetime.datetime.now(ZoneInfo('Europe/Moscow')).isoformat(timespec='seconds')
count, seconds = summary.groups()
report = f'''# PB-051 — независимый диагностический аудит

## Аудит {now}; исполнитель: Codex; модель: GPT-6.

Вердикт: **changes_requested**, задача не принята и не закрыта.
Проверены требования AC-01/AC-02, результат выполнения, история самопроверок, предшествующая приёмка зависимости и возможность завершения жизненного цикла. Инженер сообщил модель исполнителя PB-051: GPT-6.1 Sol. Исторические записи сохранены с исходной атрибуцией.

## Подтверждённый результат

- VERSION, project.json и manifest согласованы на 0.1.0; совпали 38 хешей управляемых файлов, release_id и состав артефакта. Объявлены переходы с 0.0.2 и 0.0.3.
- CHANGELOG содержит один Unreleased с целевой 0.1.0 без даты публикации. Отражены PB-020–PB-030, PB-038/PB-042, legacy/v2, совместимость и ручные действия. PB-041/PB-044 по текущим карточкам всё ещё без независимой приёмки, ограничение описано верно. Все три локальные ссылки существуют.
- Отдельные subprocess-пробы: свежий init устанавливает 0.1.0; preview перехода 0.0.3 → 0.1.0 не пишет; plan --save/apply сохраняет пользовательскую настройку и все проектные файлы. 0.0.3 здесь — согласованная фикстура на текущем коде, не исторический CLI.
- Независимый полный `python3 -m unittest discover -s tests -v`: **{count} тестов, {seconds} s, FAILED (failures=1)**, exit_code=1. Прошли 549 тестов, включая подлинный артефакт/CLI 0.0.2 и сценарии PB-031; self-project pilot PB-029 упал на close PILOT-001 из-за ссылки на протокол повторного аудита PB-052 (A-03). Это аудиторский прогон, не финальная самопроверка/передача исполнителя PB-051.
- `check --ready` до записи протокола: OK, 60 карточек. `git diff --check`: OK. Снимки объявленных входов плюс CHANGELOG совпали от начала отдельных проб до завершения полного прогона. Среда: macOS, Python 3.14.6.

## Проверка предыдущих этапов

PB-052 завершена и архивирована. Её AUDIT-01 выполнен GPT-6.1 Sol в отдельной сессии, содержит 550 тестов и отдельные пробы; явно оставляет собственные verify/HANDOFF PB-051 следующему этапу. Это приёмка исправлений PB-052, не PB-051. В контекст архивной PB-052 позже добавлена ссылка на повторный аудит GPT-6 от 08:43:55; этот аудит заявил 550/550, но его итоговая запись теперь ломает staged-проверку (A-03). Историческая приёмка реализации PB-052 не отменяется этим протоколом, однако зелёный результат нельзя переносить на нынешнее дерево. В PB-051 нет AUDIT/HANDOFF; CHECK-03 сообщает о положительной оценке в чате, но не содержит воспроизводимой самостоятельной независимой приёмки PB-051. Последний полный verify PB-051 failed; CHECK-05 также failed. Исходные шесть падений устранены, но полный прогон снова красный по другой причине.

## A-01 — P2: PB-051 не прошла финальную самопроверку и передачу

Критерии: AC-01, AC-02.
Воспроизведение: `python3 ai-dev-playbook/scripts/playbook.py card show PB-051 --handoff`; `python3 ai-dev-playbook/scripts/playbook.py verify PB-051 --dry-run`.
Факт: Status blocked, handoff_state/acceptance_state not_current, HANDOFF отсутствует; dry-run отказывает: «verify требует активную задачу ready/in_progress/in_review». Завершение PB-052 не завершило собственный цикл PB-051.
Влияние: нет проверенного состояния и журнала, к которым можно привязать независимую приёмку и close.
Критерий исправления: исполнитель завершает финальную подготовку, регистрирует материалы, готовит in_review/индекс, выполняет собственный passed verify с inputs_unchanged=true, добавляет новые CHECK/HANDOFF и регистрирует current-передачу. После этого — независимый повторный аудит, accept и close по уже данному поручению инженера.

## A-02 — P2: проверка свежести не охватывает основной результат CHANGELOG

Критерии: AC-01, AC-02.
Источник: `ai-dev-playbook/project.json → verification.inputs` содержит AGENTS.md, README.md, ai-dev-playbook, docs, tests; корневой CHANGELOG.md отсутствует. Runner автоматически добавляет настройки и карточку, но не changelog.
Воспроизведение: `python3 work/verification/PB-051/audit-20261027/freshness_probe.py`.
Факт: на временном проекте с точным списком входов репозитория настоящий runner создал passed/inputs_unchanged=true; после изменения CHANGELOG `verification.inputs_fresh` вернул true и пустой diff. Дополнительная snapshot-проба дала тот же результат. Использован контролируемый passing-check, не полный verify PB-051.
Влияние: файл, определяющий оба критерия PB-051, можно изменить после проверки без потери свежести отчёта. Тесты и наличие статуса passed не доказывают актуальность changelog.
Критерий исправления: включить корневой CHANGELOG.md во входы проверки PB-051 (например, в существующий verification.inputs); отдельной пробой подтвердить, что изменение этого файла после успешного runner делает отчёт несвежим. Затем выполнить цикл A-01.

## A-03 — P2: итог повторного аудита PB-052 нарушил staged-проверку

Источник: docs/archive/tasks/PB-052.md:28, ссылка `../../../work/audits/PB-052-reaudit-02/report.md`.
Воспроизведение: полный unittest discover, tests/test_pb029.py:757, close PILOT-001 на точной временной копии репозитория.
Факт: закрытие отказывает с «битая или внешняя локальная ссылка» именно на этот файл. В исходном дереве файл существует и check --ready проходит. `_prepare_staged` в file_operations.py исключает корневой work и отдельно копирует только work/verification; work/audits в подготовленной копии отсутствует. Правила lifecycle также не разрешают work/audits как переносимый материал. Это не исходная ошибка переписывания ссылок PB-052, а последствия размещения нового аудиторского протокола после закрытия.
Влияние: регрессия общего набора блокирует успешный verify PB-051; предыдущий PASS повторного аудита не подтверждает конечное состояние после записи его ссылки.
Исправление вынесено в отдельную draft-карточку [PB-053](../../../../docs/tasks/PB-053.md). Критерий: сохранить протокол и доказательства в поддерживаемом месте без потери байтов/истории, исправить ссылку, подтвердить полный self-project pilot и общий набор именно после окончательной записи материалов. PB-051 получает зависимость PB-053. В ходе аудита проблема не исправлялась.

## Материалы и границы

- [Полный прогон](tests.log), [CLI-пробы](probes.log), [код проб](probes.py).
- [Проба свежести](freshness.log), [код](freshness_probe.py), [отчёт временного runner](freshness-fixture-report.json).
- [Содержательные проверки](content-check.json), [начальное состояние](card-before.log), [структура](check-before.log), [отказ verify](verify-dry-run.log).
- [Снимок до](snapshot-before.json), [снимок после](snapshot-after.json).

Формат v2 требует предшествующую HANDOFF для AUDIT-записи. Создавать фиктивную передачу аудитору нельзя; диагностический протокол хранится этим материалом и связан из CHECK-06/контекста карточки. Accepted-приёмка не регистрировалась. Реализация и конфигурация не исправлялись. Уточнена шапка модели по сообщению инженера; задача остаётся blocked из-за новой зависимости PB-053, добавлена диагностическая запись, история сохранена. В CHANGELOG актуализировано ограничение перед выпуском: новый полный прогон failed, повторная проверка нужна после PB-053 и закрытия A-01/A-02. Публикация/commit/push не выполнялись.
'''
(OUT / 'report.md').write_text(report)
card = ROOT / 'docs/tasks/PB-051.md'
old = card.read_text()
assert 'CHECK-06' not in old
(OUT / 'card-original.md').write_text(old)
new = old.replace('Depends-On: PB-052', 'Depends-On: PB-052, PB-053', 1).replace('Model: неизвестна', 'Model: GPT-6.1 Sol', 1)
new = new.replace('Блокировка: корректирующая работа по CHECK-03/CHECK-05 перенесена в [PB-052](../archive/tasks/PB-052.md) по поручению инженера. Условие продолжения — завершение и независимая приёмка PB-052; затем возврат PB-051 в in_progress, собственный свежий verify и HANDOFF. История и зарегистрированные доказательства сохранены.', 'Зависимость [PB-052](../archive/tasks/PB-052.md) завершена и независимо принята. По [диагностическому аудиту](../../work/verification/PB-051/audit-20261027/report.md) открыты A-01/A-02: отсутствует собственная финальная проверка/передача PB-051, а CHANGELOG.md не включён в отпечаток входов. Новый блокер A-03 вынесен в [PB-053](PB-053.md): ссылка на поздний протокол аудита PB-052 отсутствует в staged-копии и ломает полный прогон. После PB-053 — исправление охвата входов и новые verify/CHECK/HANDOFF исполнителя, затем отдельный повторный аудит и закрытие при успехе. История и зарегистрированные доказательства сохранены.')
new = new.replace('Python 3, локальные файлы; проверки по риску: версия, manifest, init и структура. Полная модель не сообщена средой; указана неизвестна.', 'Python 3, локальные файлы; проверки по риску: версия, manifest, init и структура. Модель исполнителя GPT-6.1 Sol уточнена инженером 2026-10-27; историческая атрибуция записей сохранена.')
new += f'''\n### CHECK-06 — Проверка

Время: {now}
Исполнитель: Codex
Модель: GPT-6
Результат: failed
Критерии: AC-01, AC-02
Команда: python3 -m unittest discover -s tests -v; python3 work/verification/PB-051/audit-20261027/probes.py; python3 work/verification/PB-051/audit-20261027/freshness_probe.py
Среда: macOS, Python 3.14.6; независимая сессия, изменяющие пробы только во временных проектах.
Факт: {count} тестов за {seconds} s — FAILED (failures=1), 549 прошли; self-project pilot PB-029 упал на close PILOT-001 из-за ссылки в PB-052 на отсутствующий в staged-копии work/audits/PB-052-reaudit-02/report.md. Этот новый блокер A-03 вынесен в PB-053. Версии/38 хешей, init, update с 0.0.3 и сохранность файлов подтверждены. A-01/A-02: нет собственной актуальной передачи, изменение CHANGELOG после passed-runner не делает отчёт несвежим. Подробности и критерии исправления сохранены в протоколе.
Доказательства: [Диагностический аудит](../../work/verification/PB-051/audit-20261027/report.md), [полный прогон](../../work/verification/PB-051/audit-20261027/tests.log), [проба свежести](../../work/verification/PB-051/audit-20261027/freshness.log).
Ограничения: это независимая диагностика до HANDOFF; AUDIT v2/accepted без предшествующей передачи не создавались. PB-052 принята отдельно. Текущие тесты не заменяют финальные verify/CHECK/HANDOFF исполнителя и повторный аудит PB-051. Код и настройки не исправлялись; ограничение CHANGELOG актуализировано по результату аудита.
'''
card.write_text(new)
(OUT / 'card-change.diff').write_text(''.join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile='PB-051 before', tofile='PB-051 after audit')))
print(f'Recorded diagnostic findings; {count} tests, {seconds}s, failures=1; task blocked on PB-053')
