# PB-062: команды воспроизведения и граница стенда

Среда: macOS (Darwin 27), Python 3.14. `checks_policy` в self-project
не объявлена — сам комплект продолжает работать по прежнему контракту
verify-отчётов; новый механизм проверяется поведенческими тестами на
временных проектах (opt-in self-project — PB-063).

## Адресные проверки

```sh
python3 -m unittest discover -s tests -p "test_checks_gate.py" -v  # 18 тестов PB-062
python3 -m unittest discover -s tests -p "test_checks_*.py"         # регрессии checks
python3 -m unittest test_lifecycle test_workflow_e2e                # регрессии lifecycle
python3 ai-dev-playbook/scripts/playbook.py index
python3 ai-dev-playbook/scripts/playbook.py check --ready
python3 ai-dev-playbook/scripts/playbook.py adopt . --force --apply  # self-manifest
python3 ai-dev-playbook/scripts/playbook.py verify PB-062            # полный verify карточки
```

## Покрытие критериев тестами (tests/test_checks_gate.py)

- AC-01: `test_uncovered_before_run`, `test_manual_without_confirm_blocks_ok`,
  `test_confirm_covers_and_survives_rerun` — единый расчёт; ручное
  обязательство без `checks confirm` блокирует ok; `not_run` не
  маскирует подтверждение.
- AC-01/08 protected: `test_local_results_do_not_cover_protected`
  (локальные результаты не закрывают), `test_receipt_covers_but_boundary_required`
  (квитанции покрывают, но `ok` требует `capability=protected`),
  `test_forged_receipt_rejected` (подпись чужим ключом в доме
  verifier — отказ), `test_stale_candidate_receipt_not_evidence`
  (квитанция прежнего кандидата — история, не покрытие и не отказ),
  `test_run_receipt_ttl_expiry_rejected` и
  `test_aggregate_receipt_ttl_expiry_rejected` (A-01 аудита:
  просроченный по `ttl` внешний факт не покрывает ни run-, ни
  aggregate-квитанцию), `test_older_expired_does_not_block_fresh` и
  `test_older_expired_aggregate_does_not_block_fresh` (A-04 аудита:
  просроченная квитанция не блокирует более свежее доказательство;
  вердикт не зависит от имён и удаления проектных копий).
- AC-01/02 protected-история: `ProtectedLatestOutcomeTest` —
  `test_later_failed_blocks_earlier_passed` (A-03 аудита: поздний
  failed того же кандидата блокирует старый passed; удаление
  проектных копий не воскрешает покрытие; новый passed восстанавливает
  его) и `test_aggregate_cannot_override_later_failure` (aggregate
  подтверждает исходное время исполнения и не отменяет более поздний
  failed), `test_same_timestamp_aggregate_keeps_failed_latest`
  (A-05 аудита: при равных `finished_at` порядок задаёт `exec_seq` —
  позиция исходного run в журнале; aggregate старого passed не
  отменяет поздний failed, восстановление — только новым реальным
  passed).
- AC-02/05: `test_release_scope_reuses_cross_task` — scope=project
  на checkpoint=release требует весь набор; результаты задачи без
  `task_projection`-входов переиспользуются другой карточкой.
  `checks run --scope project --mode force` — существующий флаг
  полного перезапуска; commit/имя ветки в коде авторизацией не
  являются (проверяется отсутствием такой логики; зафиксировано в
  rules/verification.md).
- AC-03: `test_input_change_uncovers` — stale после изменения входов;
  `LifecycleGateTest` — без `checks_policy` поведение legacy
  (`_checks_gate` → None), с политикой непокрытое обязательство
  отклоняет handoff/accept. Перенос evidence, concurrent edits,
  restore и запрет внешнего release покрыты существующими
  регрессиями lifecycle (test_lifecycle, test_workflow_e2e,
  test_material_relocation) — новых механизмов они не отменяют.
- Автоматическое создание карточки: `checks release-ready` и
  `checks confirm` — read-only/append-only операции без создания
  задач; проверяется отсутствием такой логики и зафиксировано в
  rules/verification.md.

## Ограничения

- Стенд без отдельной OS identity: `verifier probe` →
  `capability=local`; protected-вердикт покрыт тестом с подменой
  probe, реальная защищённая эксплуатация не заявляется (R-20,
  ограничение PB-061 сохраняется).
- `checks_policy` в self-project не подключена (PB-063) — поведение
  на реальном проекте проверено только через временные фикстуры.
