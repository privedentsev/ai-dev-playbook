# PB-079 — независимый аудит

## Аудит 2026-11-17T15:38:34+03:00; исполнитель: Codex; модель: GPT-6.

Вердикт: **accepted**, замечаний нет. Проверены AC-01, AC-02, AC-03 актуальной HANDOFF-01. Код реализации не изменялся. Приёмка AUDIT-009 этим аудитом не выполняется.

- Начальный `verify PB-079 --audit-ready` подтвердил актуальную CLI-передачу и свежесть полного verify `caac4ffccd94445489fb46f6d49aee03`: self-check/preflight-regressions/unit-tests passed, inputs_unchanged=true. Полный suite исполнителя (1134 теста) независимо целиком не повторялся.
- Независимая матрица `python3 -m unittest discover -s tests -p 'test_checks*.py' -v`: **365 тестов, OK**, включая trust, gate, e2e, lifecycle/legacy и 11 новых регрессий. [Лог](checks.log). Запуск вне внешней песочницы с настоящим sandbox-exec. Отдельный первый адресный прогон также дал 11/11 OK.
- Проверены общий checks_coverage, порядок exec_seq/finished_at, aggregate и исходные исполнения, TTL/always_run, candidate/policy/task bindings, project copies, потеря/порча доверенного источника, replay/revocation, manual, capability и protected_confirmed. Последовательные passed→failed/timeout/blocked блокируют покрытие и CLI; новый passed восстанавливает его.
- Дополнительно выполнена [проба перекрытия](probe_overlap.py): два настоящих исполнения, ранний passed удерживается перед выдачей receipt до завершения и выдачи позднего failed. Wrapper меняет только расписание выдачи; результаты, подписи, журнал и метки не редактируются. Обе подписи валидны, candidate общий; оба gate оставляют probe uncovered, реальный CLI rc=1. Последующий реальный passed восстанавливает покрытие. [JSON](overlap.json). Воспроизведение: `python3 work/verification/PB-079/audit-01/probe_overlap.py .` вне внешней песочницы.
- Все 3051 отслеживаемых файла docs/archive побайтно совпадают с HEAD; новых архивных файлов нет. [Проверка](preservation.json). [Кандидат и SHA-256](candidate.json).

Первый запуск новых тестов внутри внешней песочницы не исполнил sandbox-exec корректно (13 assertion failures на 11 тестах); повтор вне неё успешен. Первая широкая матрица была запущена аудитором одновременно с prepare-audit: 46 отказов связаны с временной lifecycle-блокировкой установки fixture, а не с реализацией. [Исходный лог](checks-initial.log); после завершения prepare-audit вся матрица прошла. Эти запуски не считаются продуктовыми находками.

Ограничения: стенды принадлежат той же OS identity, capability=local; отдельная OS/CI identity не проверялась. Полный dependency-aware self-pilot отдельно не повторялся. Состав и границы чужих исходных изменений сохранены. Результаты структурных CLI-проверок, приёмки и close --dry-run записываются в соседние логи; реальное закрытие не выполняется без отдельного поручения.
