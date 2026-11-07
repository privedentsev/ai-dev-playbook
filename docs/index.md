# Индекс документации

| Тема | Когда читать | Документ |
|---|---|---|
| Руководство пользователя | Как пользоваться комплектом, режимы и примеры запросов | [Руководство](user-guide/index.md) |
| Продукт | Цели и границы | [Продукт](product.md) |
| Устройство | Изменение CLI или структуры | [Архитектура](architecture.md), [карта](code-map.md) |
| План | Выбор работы и порядка реализации | [Roadmap](roadmap.md), [планы](plans/index.md), [задачи](tasks/index.md) |
| Спецификации | Требования к планируемым изменениям и актуальному поведению | [Индекс спецификаций](specs/index.md) |
| Проектирование | Предлагаемое и принятое техническое устройство изменений | [Дизайны](designs/index.md), [ADR](adr/index.md) |
| Обновление комплекта | Версии, границы файлов, этапы обновления | [План обновлений](update-plan.md), [контракт](../ai-dev-playbook/rules/update.md) |
| Процесс | Режим, проверки, Git | [Режимы](../ai-dev-playbook/rules/workflow.md), [проверки](../ai-dev-playbook/rules/verification.md), [Git](../ai-dev-playbook/rules/git.md) |
| Настройки | Перед запуском работы | [Проект](../ai-dev-playbook/project.json), [ресурсы](../ai-dev-playbook/resources.json) |

## Подготовленные изменения

- Выборочная подготовка файлов и ограниченная история операций: [требования](specs/scoped-file-operations.md), [design](designs/scoped-file-operations.md), [ADR-0005](adr/0005-scoped-file-operations.md), [план PB-045–PB-050 и AUDIT-008](plans/scoped-file-operations.md). Подготовка до задач; выполнение не разрешено.

- Автоматический выбор проверок по зависимостям: [требования](specs/dependency-aware-verification.md), [design](designs/dependency-aware-verification.md), [ADR-0006](adr/0006-dependency-aware-verification.md), [план PB-057–064 и AUDIT-009](plans/dependency-aware-verification.md). Подготовлено к согласованию; реализация не начата.

- Варианты аудита плана и исправления без возврата архива: [требования](specs/block-acceptance.md), [design](designs/block-acceptance.md), [ADR-0007](adr/0007-block-acceptance.md), [план PB-065–071 и AUDIT-010](plans/block-acceptance.md). Документы и карточки draft; реализация не начата.
