# PB-061: независимый аудит HANDOFF-01

Время: 2026-11-10T18:37:04+03:00; исполнитель: Codex; модель: GPT-6.1 Sol (Medium).

Вердикт: **changes_requested**, A-01/P1, A-02/P1, A-03/P2, A-04/P1.
Полный протокол и критерии исправления записаны в AUDIT-01 карточки PB-061.

Проверки:

- `playbook.py verify PB-061 --audit-ready`: свежая передача и проверки подтверждены, exit 0.
- `python3 -m unittest discover -s tests -p 'test_checks_trust.py' -v`: 32 теста, OK в нативной среде.
- `python3 -m unittest discover -s tests -p 'test_checks_*.py'`: связанные регрессии, подробности в related-tests.log.
- `python3 work/verification/PB-061/audit-01/probes.py` из корня репозитория: четыре негативных сценария, точные факты в probes.log. Только временные проекты; cleanup выполняется автоматически; реализация не меняется.

Наблюдения независимых проб:

1. FORGED_ADAPTER: модуль без тестов пишет adapter report и завершает discovery; run, подпись и gate успешны.
2. PROBE: chmod 0555 только корня home создаёт protected_confirmed=true при доступном агенту существующем ключе и возможности вернуть права.
3. ENV / EXTERNAL_EXCEPTION: очищенное окружение потеряно; валидное required_environment/always_run вызывает необработанный TypeError.
4. REVOCATION: отзыв исходного receipt не блокирует агрегат и consumer gate после удаления публичной копии исходного receipt.

Границы: отдельная OS/CI identity не проверялась, production не затрагивался. В sandbox Codex sandbox-exec не может применить вложенный профиль: sandbox_apply: Operation not permitted; исходный лог сохранён отдельно в trust-tests-sandboxed.log. Нативный повтор успешен. Полный legacy verify не повторялся; его свежесть проверена штатным audit-ready. Штатные тесты не покрывают обнаруженные обходы.
