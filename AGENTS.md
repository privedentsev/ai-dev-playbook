# Инструкции проекта

Отвечай кратко, конкретно и по теме. Для заметок в Obsidian сохраняй структуру и смысл без лишней воды.

Этот репозиторий разрабатывает сам комплект `ai-dev-playbook/`. Перед работой определи режим по [правилам](ai-dev-playbook/rules/workflow.md), найди связанную карточку в [индексе](docs/tasks/index.md) и начни с `python3 ai-dev-playbook/scripts/playbook.py card show <ID>`; для продолжения/аудита читай передачу со связанными EXEC/CHECK через `--handoff`, отдельную запись — через `--entry <ID>`, историю — через `--full`, затем нужные документы через [индекс документации](docs/index.md). Не читай весь архив без причины.

Карта путей и команд — [project.json](ai-dev-playbook/project.json), ресурсы — [resources.json](ai-dev-playbook/resources.json). Правила задач, проверок и Git находятся в `ai-dev-playbook/rules/`. Исходники в `ai-dev-playbook/scripts/`, переносимые тексты в `ai-dev-playbook/rules/` и `ai-dev-playbook/templates/`.

Перед завершением обнови карточку и затронутые документы, выполни относящиеся к изменению проверки. Не выдавай проверку структуры за проверку поведения CLI. Не сохраняй секреты. Commit, push, merge и release — только по явной просьбе инженера.

Для самопроверки: `python3 ai-dev-playbook/scripts/playbook.py index` и `python3 ai-dev-playbook/scripts/playbook.py check --ready`. Обновление комплекта — по локальному артефакту: `update plan <каталог>` без записи, `--save` + `update apply`, `update restore` после прерывания; установки без manifest — `adopt`. Контракт — `ai-dev-playbook/rules/update.md`. После правки файлов комплекта актуализируй self-manifest: `playbook.py adopt . --force --apply`.
