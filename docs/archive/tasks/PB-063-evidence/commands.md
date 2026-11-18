# PB-063: команды воспроизведения и граница стенда

Среда: macOS (Darwin 27), Python 3.14.6. `checks_policy` в self-project
не объявлена — сам комплект продолжает работать по прежнему legacy
контракту verify; новый механизм проверяется поведенческими тестами на
временных проектах и изолированных деревьях (self-config — только явное
решение инженера после пилота, PB-064/AUDIT-009).

## Адресные проверки

```sh
python3 -m unittest discover -s tests -p "test_checks_setup.py" -v   # 29 тестов PB-063
python3 -m unittest discover -s tests -p "test_checks_*.py"          # регрессии checks
python3 -m unittest test_file_operations test_cli                    # регрессии операций/CLI
python3 -m unittest test_verification_smoke                          # быстрые регрессии версий/ссылок
python3 ai-dev-playbook/scripts/playbook.py index
python3 ai-dev-playbook/scripts/playbook.py check --ready
python3 ai-dev-playbook/scripts/playbook.py adopt . --force --apply  # self-manifest
python3 ai-dev-playbook/scripts/playbook.py setup preview            # read-only обследование self-project
python3 ai-dev-playbook/scripts/playbook.py verify PB-063            # полный verify карточки
```

Полный набор: `python3 -m unittest discover -s tests` (все test_*.py).
`unit-tests` в project.json: timeout_seconds 1800 → 2400 — рост `work/`
(каталоги отчётов verify копируются `test_self_project_copy_pilot`)
вывел полный набор за прежнюю границу; сам набор тестов не менялся.

## Покрытие критериев тестами (tests/test_checks_setup.py)

- AC-01 preview read-only: `test_preview_writes_nothing_and_runs_no_code`
  — sentinel-файл на уровне модуля теста не создаётся, снимок дерева
  до/после идентичен; `test_catalog_matches_real_discovery` —
  предсказанные ID совпадают с фактическим `unittest discover`;
  `test_preview_reports_excludes_unknowns_and_problems` — исключения
  `.venv/**`, `node_modules/**`, unknown-файлы корня, симлинк и
  тесты в исключаемой области (`.github`) без предложенной проверки;
  `test_load_tests_and_parse_errors_surfaced` — load_tests-файлы и
  ошибки разбора в unknowns, ID не выдумываются;
  `test_unresolved_and_ambiguous_imports_reported` — неразрешённые и
  неоднозначные импорты перечислены, самоссылок модулей нет;
  `test_inherited_testcase_methods_match_discovery`,
  `test_cross_file_base_class_and_mixin`,
  `test_imported_testcase_class_collected_in_module` — наследование
  TestCase и импортированные классы: предсказанные ID совпадают с
  `unittest discover` (ID импортированного класса — по `__module__`
  определяющего файла);
  `test_unresolvable_base_surfaced_in_unknowns`,
  `test_unresolvable_class_like_import_surfaced` — неразрешённые базы
  и class-like импорты перечислены в `unknowns.class_uncertainties`;
  `test_reexport_chain_matches_discovery` — цепочка реэкспорта
  `base → helpers → test` (A-03): ID по `__module__` исходного
  определения совпадает с discover; `test_reexport_cycle_and_unresolvable_surfaced`
  — цикл реэкспорта и неразрешённое имя в разрешённом модуле дают
  class_uncertainties без зависания; `test_shadowed_test_method_removed_from_catalog`
  — `test_ok = None` в потомке удаляет унаследованный метод из
  каталога (A-04); `test_lambda_assignment_is_collected_test` —
  `test_x = lambda` собирается discover'ом и предсказывается;
  `test_unknown_expr_shadow_surfaced_in_unknowns` —
  `test_ok = make()` исключается и помечается неопределённостью;
  `test_mro_diamond_shadowing_matches_discovery` — diamond-MRO
  `Child(Left, Right)` с `Left.test_ok = None`: C3-порядок убирает
  метод из Child, predicted == discover (A-04);
  `test_factory_testcase_class_surfaced_in_unknowns` —
  `Base = type("Base", (TestCase,), {...})`: вызов не доказывает
  не-класс, class-like импорт помечается class_uncertainties (A-05);
  `test_container_element_import_surfaced_in_unknowns` —
  `Base = [Original][0]`: элемент коллекции может быть классом,
  Subscript/Attribute не доказывают не-класс → class_uncertainties
  (A-05); `test_module_rebind_drops_class_from_catalog` —
  `class Base(TestCase)` + `Base = None`: last-binding-wins убирает
  класс из namespace, predicted == discover без unknowns (A-06);
  `test_module_rebind_unknown_surfaced_in_unknowns` —
  `Base = make()` после ClassDef: записи нет, неопределённость есть
  (A-06); `test_imported_class_rebind_drops_from_catalog` —
  `from m import Base` + `Base = None`: импортированное имя
  вытеснено литералом, записей нет (A-06).
- AC-01 apply/recovery: `test_apply_writes_policy_and_link` —
  checks-policy.json записан канонически, `project.json → checks_policy`,
  legacy-ключи неизменны, `config`/`_load_policy`/`index` проходят;
  `test_apply_interruption_restores_preimage` — KeyboardInterrupt в
  final check оставляет pending, `restore_operation` возвращает
  пред-образ (draft-байты и project.json без ссылки);
  `test_new_files_inherit_module_rules` — новый файл под `src/` и
  `tests/` получает владельца-модуль в inventory, `.venv/` исключён.
- AC-02 совместимость: `test_apply_preserves_task_workflow_v2` —
  `schema_version: 2` и `task_workflow` сохраняются; повторное
  применение отклоняется (`test_apply_rejects_missing_and_second_application`);
  неподдерживаемая `schema_version` и TODO-`approved_basis` отклонены
  (`test_apply_rejects_unsupported_schema`,
  `test_apply_rejects_unapproved_basis`); `--policy PATH` читает
  альтернативный источник и пишет канонический checks-policy.json
  (`test_apply_uses_alternate_policy_source`). Прерывание/restore
  покрыто тем же тестом восстановления; generic-кейсы engine —
  tests/test_file_operations.py.
- AC-03 описания тестов (A-02 аудита): всем ~980 test-методам добавлены
  однострочные docstring'и условия/ожидания — ID и имена не менялись;
  в каталоге self-project 1065 записей, 0 fallback-описаний
  «unittest-метод …», дубликатов описаний нет. Предсказанный каталог
  сверен с фактическим `unittest discover`: 1065 ID совпадают точно,
  без расхождений в обе стороны. `class_uncertainties` self-project —
  9 честных предупреждений о class-like именах (`REPO`, `REPO_VERSION`
  из test_cli), значения которых заданы вызовами и статически не
  доказаны не-классами (A-05).
- Документация/шаблоны (AC-03): rules/verification.md (раздел PB-063),
  docs/user-guide/modes.md, docs/architecture.md, docs/code-map.md,
  CHANGELOG.md; шаблон `ai-dev-playbook/templates/checks-policy.json`
  уже содержит стабильный ID и description записи каталога — ID не
  менялись. Self-config не включён: `checks_policy` в project.json
  отсутствует (проверено `python3 -c` по project.json и зафиксировано
  тестами ApplyCase на временных проектах).

## Ограничения

- Предсказание каталога — статическое: наследование TestCase,
  импортированные TestCase-классы, цепочки реэкспорта
  (`from M import X`, где X сам импортирован) и множественное
  наследование разрешаются по индексу классов проекта; состав
  test-методов вычисляется по C3-линеаризации MRO — первое связывание
  `test_*` в порядке MRO решает (def/lambda собираются, литералы и
  property снимают метод, выражения с неопределённой вызываемостью
  помечаются). Привязки верхнего уровня модуля — last-binding-wins:
  `class Base`/`from m import Base`, вытесненные литералом, func или
  `del`, уходят из namespace без записей и без unknowns; вытесненные
  выражением неизвестного значения — в `class_uncertainties`.
  Доказанно не-классом — только литералы, коллекции как целое и
  lambda; вызовы, `Name`/`Attribute`/`Subscript` (включая элементы
  коллекций вида `[Original][0]`) классом быть могут. Свежие
  module-level присваивания без предыдущей привязки класса/импорта
  (`Base = make()` в новом имени) отдельно не проверяются —
  задокументированное ограничение. Динамические механизмы (`load_tests`, метаклассы,
  `__init_subclass__`, exec, инъекция тестов в runtime, классы вне
  start-каталога discover, фабричные классы `type(...)`, атрибутные
  алиасы `Base = base.Base`, factory-привязки `test_x = make()`)
  статически не видны — они перечислены в unknowns/
  `class_uncertainties`, ID не выдумываются. Файлы с `load_tests` и
  неразрешённые базы перечислены в unknowns, их ID инженер дописывает
  вручную после первого прогона. Первый `checks run` покажет
  расхождение каталога — это штатная точка доработки черновика.
- Discovery `python -m unittest` не знает policy.excludes: тесты внутри
  исключаемых областей с целой цепочкой `__init__.py` будут обнаружены —
  preview предсказывает их тем же правилом и требует записей каталога.
- Обход ограничен лимитом 50000 записей; при превышении остаток —
  unknown (fallback проекта консервативно расширяет набор).
- Поведенческая проверка apply — на временных проектах с установленным
  комплектом; на self-project apply не выполнялся и не планируется без
  отдельного решения (PB-064/AUDIT-009).
