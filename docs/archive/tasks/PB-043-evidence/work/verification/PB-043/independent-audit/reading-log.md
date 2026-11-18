# Журнал чтения аудитора

Чтение выполнено адресно; весь репозиторий и архив не прочитаны. Некоторые агрегированные выводы инструментов усечены; они не учитываются как полное чтение.

| Пути | Область/цель |
|---|---|
| AGENTS.md | инструкции из контекста пользователя; поиск связанных правил |
| docs/tasks/index.md, docs/index.md | выбор карточки и документов; первый вывод индекса усечён |
| docs/tasks/PB-043.md | полный файл и повтор содержимого через card show --handoff |
| docs/tasks/PB-043.meta.json | метаданные/связи и отчёт |
| docs/specs/handoff-context.md | полностью: контракт AC-01–04 |
| tests/test_handoff_context.py | полностью: 6 сценариев |
| ai-dev-playbook/scripts/task_cards.py | поиск handoff/context; 517–610, выбор/рендер |
| ai-dev-playbook/scripts/playbook.py | 1090–1185, 1260–1283: dispatch/флаги |
| ai-dev-playbook/scripts/lifecycle.py | 1–75, 780–875, 1890–1970, 2035–2125: promotion, регистрация, handoff/accept; часть агрегированных выводов усечена |
| ai-dev-playbook/scripts/file_operations.py | поиск pending/lock, без полного чтения |
| ai-dev-playbook/rules/workflow.md | таблица режимов; повтор 77–116 для verify/audit |
| ai-dev-playbook/rules/verification.md | чтение с усечением; повтор 34–90, порядок и аудит полностью |
| ai-dev-playbook/rules/task-card-format.md | поиск + 22–38, 60–85 |
| ai-dev-playbook/rules/context.md, ai-dev-playbook/rules/tasks.md | поиск EXEC/CHECK/HANDOFF/verify; не полное чтение |
| ai-dev-playbook/templates/task.md | полностью |
| ai-dev-playbook/templates/AGENTS.md, ai-dev-playbook/README.md | адресный поиск handoff/передача, вывод частично усечён |
| docs/user-guide/index.md, docs/architecture.md, docs/code-map.md | адресный поиск; architecture 31–38 повторно |
| ai-dev-playbook/project.json, ai-dev-playbook/resources.json | конфигурация входов/проверок/ресурсов |
| docs/tasks/PB-043-evidence/scope.md, context-measurement.json | полностью, границы самопроверки и замер |
| tests/test_close_lifecycle.py, tests/test_task_cards.py | 1–150 и 1–90, фикстуры независимой пробы |
| work/verification/PB-043/18e1a58302674edabe2470dc1cdf831e/report.json | машинное чтение; вывод снимка усечён, статус/проверки прочитаны |
| work/verification/PB-043/18e1a58302674edabe2470dc1cdf831e/unit-tests.stderr.log | только последние 4 строки |
| docs/specs/index.md | поиск handoff-context |

Найденные хеши остальных файлов snapshot не означают чтение их содержимого. tests/test_compact_context.py и tests/test_criterion_checkboxes.py исполнены, не прочитаны. Независимые tests.log/probe.log прочитаны по итогам/ошибкам. Память: MEMORY.md — поиск и строки 195–218, границы аудита и разрешения закрытия.
