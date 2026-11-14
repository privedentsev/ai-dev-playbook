import json
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

from test_cli import CliCase
from test_documents import CatalogCase, doc_meta, write_doc
from test_playbook import playbook, write_card

links = playbook.document_links
documents = playbook.documents


def scan_kinds(text):
    return [(item["kind"], item["target"]) for item in links.scan(text)]


class ScanTest(unittest.TestCase):
    """Сканер ссылок: поддерживаемые конструкции, code-fence, цели и
    fragments."""
    def test_supported_constructs(self):
        """Сканер находит inline/reference/image/wikilink-ссылки"""
        text = ("[a](x.md) ![b](y.png)\n[d][r] [e][]\n"
                "[r]: r.md\n![f][r2]\n[r2]: <r 2.md>\n")
        found = scan_kinds(text)
        self.assertIn(("inline", "x.md"), found)
        self.assertIn(("image", "y.png"), found)
        self.assertIn(("ref", "r"), found)
        self.assertIn(("refdef", "r.md"), found)
        self.assertIn(("refimg", "r2"), found)
        self.assertIn(("refdef", "<r 2.md>"), found)

    def test_inline_title_and_angle_target(self):
        """Angle-цель с заголовком парсится корректно"""
        found = scan_kinds('[x](<a b.md> "Название")\n')
        self.assertEqual(found, [("inline", "<a b.md>")])

    def test_balanced_parens_in_target(self):
        """Сбалансированные скобки в цели ссылки поддерживаются"""
        found = scan_kinds("[x](a(1).md)\n")
        self.assertEqual(found, [("inline", "a(1).md")])

    def test_code_is_not_scanned(self):
        """Code-fence и инлайн-код не сканируются как ссылки"""
        text = ("```\n[x](a.md)\n```\n~~~\n[y](b.md)\n~~~\n"
                "инлайн `[z](c.md)` код\n[d](e.md)\n")
        self.assertEqual(scan_kinds(text), [("inline", "e.md")])

    def test_other_fence_inside_block_is_content(self):
        """Чужой fence внутри блока — содержимое, не граница"""
        text = "```\n~~~\n[x](a.md)\n```\n[y](b.md)\n"
        self.assertEqual(scan_kinds(text), [("inline", "b.md")])

    def test_unsupported_constructs_detected(self):
        """Неподдерживаемые конструкции wiki/html детектируются"""
        found = scan_kinds("[[Page]] и <a href=\"old.md\">t</a>\n")
        self.assertIn(("wikilink", "Page"), found)
        self.assertIn(("html", "old.md"), found)

    def test_wikilink_alias_target(self):
        """Wikilink с алиасом берёт цель до разделителя"""
        found = scan_kinds("[[Page|текст]]\n")
        self.assertEqual(found[0][1], "Page")

    def test_shortcut_reference_is_not_a_link(self):
        """Shortcut reference без определения не считается ссылкой"""
        self.assertEqual(scan_kinds("[label]\n"), [])


class LinksCase(unittest.TestCase):
    """Базовый case ссылок: проект с коллекциями и перемещениями
    документов."""
    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def path(self, relative):
        return (self.root / relative).resolve()

    def rewrite(self, text, old, new, extra=None):
        moves = {self.path(old): self.path(new)}
        for old_key, new_key in (extra or {}).items():
            moves[self.path(old_key)] = self.path(new_key)
        return links.rewrite(text, self.path(old), self.path(new), moves)


class RewriteTest(LinksCase):
    """Переписывание ссылок при перемещениях: входящие/исходящие,
    self-ссылки, взаимные, image."""
    def test_unrelated_move_preserves_authored_destinations(self):
        """Несвязанное перемещение сохраняет авторские назначения ссылок"""
        source = self.path("docs/specs/main.md")
        old = self.path("docs/tasks/PILOT-001.md")
        new = self.path("docs/archive/tasks/PILOT-001.md")
        text = ('[spec](../specs/other.md?view=1#part)\n'
                '![image](./image.png)\n[ref]: <../specs/other.md>\n'
                '[task](../tasks/PILOT-001.md)\n')
        result = links.rewrite(text, source, source, {old: new})
        self.assertEqual(result["text"], text.replace(
            '../tasks/PILOT-001.md', '../archive/tasks/PILOT-001.md'))
        self.assertEqual(len(result["changes"]), 1)


    def test_outgoing_link_in_moved_doc(self):
        """Исходящая ссылка перемещённого документа переписывается"""
        result = self.rewrite("[b](b.md)\n", "docs/specs/a.md",
                              "docs/archive/specs/a.md")
        self.assertEqual(result["text"], "[b](../../specs/b.md)\n")
        self.assertEqual(len(result["changes"]), 1)

    def test_incoming_link_rewritten(self):
        # источник не двигается, но цель в карте
        """Входящая ссылка на перемещённый документ переписывается"""
        moves = {self.path("docs/specs/a.md"): self.path("docs/archive/specs/a.md")}
        result = links.rewrite("[a](specs/a.md)\n", self.path("docs/readme.md"),
                               self.path("docs/readme.md"), moves)
        self.assertEqual(result["text"], "[a](archive/specs/a.md)\n")

    def test_self_link_keeps_filename(self):
        """Самоссылка сохраняет имя файла при перемещении"""
        result = self.rewrite("[сама](A-1.md)\n", "docs/tasks/A-1.md",
                              "docs/archive/tasks/A-1.md")
        self.assertEqual(result["text"], "[сама](A-1.md)\n")

    def test_mutual_links(self):
        """Взаимные ссылки двух перемещённых документов переписываются"""
        moves = {"docs/specs/a.md": "docs/archive/specs/a.md",
                 "docs/specs/b.md": "docs/archive/specs/b.md"}
        result = self.rewrite("[b](b.md)\n", "docs/specs/a.md",
                              "docs/archive/specs/a.md", moves)
        self.assertEqual(result["text"], "[b](b.md)\n")

    def test_image_rewritten(self):
        """Image-ссылка переписывается при перемещении"""
        result = self.rewrite("![схема](img.png)\n", "docs/specs/a.md",
                              "docs/archive/specs/a.md")
        self.assertEqual(result["text"], "![схема](../../specs/img.png)\n")

    def test_reference_definition_rewritten(self):
        """Reference-определение переписывается"""
        result = self.rewrite("[x]: b.md\nиспользование [t][x]\n",
                              "docs/specs/a.md", "docs/archive/specs/a.md")
        self.assertIn("[x]: ../../specs/b.md", result["text"])
        self.assertIn("[t][x]", result["text"])

    def test_angle_target_with_spaces_keeps_style(self):
        """Angle-цель с пробелами сохраняет стиль записи"""
        result = self.rewrite("[x](<a b.md>)\n", "docs/specs/a.md",
                              "docs/archive/specs/a.md")
        self.assertEqual(result["text"], "[x](<../../specs/a b.md>)\n")

    def test_encoded_target_stays_encoded(self):
        """Percent-encoded цель остаётся закодированной"""
        result = self.rewrite("[x](sub%20dir/a.md)\n", "docs/specs/a.md",
                              "docs/archive/specs/a.md")
        self.assertEqual(result["text"], "[x](../../specs/sub%20dir/a.md)\n")

    def test_query_and_fragment_preserved(self):
        """Query и fragment сохраняются при переписывании"""
        moves = {self.path("docs/a.md"): self.path("docs/archive/a.md")}
        result = links.rewrite("[a](a.md?raw=1#sec)\n",
                               self.path("docs/b.md"), self.path("docs/b.md"),
                               moves)
        self.assertEqual(result["text"], "[a](archive/a.md?raw=1#sec)\n")

    def test_bare_fragment_unchanged(self):
        """Голый fragment не изменяется"""
        result = self.rewrite("[раздел](#sec)\n", "docs/a.md",
                              "docs/archive/a.md")
        self.assertEqual(result["changes"], [])

    def test_external_links_unchanged(self):
        """Внешние ссылки не изменяются"""
        result = self.rewrite("[x](https://ex.com/a.md) [y](mailto:a@b.c)\n",
                              "docs/a.md", "docs/archive/a.md")
        self.assertEqual(result["changes"], [])

    def test_code_blocks_not_rewritten(self):
        """Ссылки в code-блоках не переписываются"""
        text = ("```\n[x](a.md)\n```\n`[y](b.md)`\n[z](a.md)\n")
        result = self.rewrite(text, "docs/a.md", "docs/archive/a.md")
        self.assertIn("```\n[x](a.md)\n```", result["text"])
        self.assertIn("`[y](b.md)`", result["text"])
        self.assertIn("[z](a.md)", result["text"])

    def test_no_cascading_replacement(self):
        # замены по позициям, не по строкам: одинаковые цели не накапливаются
        """Переписывание не каскадирует по цепочке замен"""
        moves = {self.path("docs/a.md"): self.path("docs/archive/a.md")}
        result = links.rewrite("[a](a.md) [a2](a.md#two)\n",
                               self.path("docs/b.md"), self.path("docs/b.md"),
                               moves)
        self.assertEqual(result["text"],
                         "[a](archive/a.md) [a2](archive/a.md#two)\n")

    def test_title_preserved(self):
        """Заголовок ссылки сохраняется при переписывании"""
        result = self.rewrite("[x](a.md \"Подсказка\")\n", "docs/a.md",
                              "docs/archive/a.md")
        self.assertIn("(a.md \"Подсказка\")", result["text"])


class CheckTextTest(LinksCase):
    """Проверка ссылок: broken/fine, reference, fragment,
    исторические moves."""
    def check(self, text, base, moves=None):
        base_dir = self.path(base).parent
        moves = {self.path(k): self.path(v) for k, v in (moves or {}).items()}
        return links.check_text(text, "doc.md", base_dir, self.root, moves)

    def test_broken_and_fine(self):
        """Проверка ссылок различает broken и fine"""
        (self.root / "docs" / "ok.md").parent.mkdir(parents=True)
        (self.root / "docs" / "ok.md").write_text("x\n", encoding="utf-8")
        errors = self.check("[ok](ok.md) [bad](missing.md)\n", "docs/doc.md")
        self.assertEqual(len(errors), 1)
        self.assertIn("missing.md", errors[0])

    def test_reference_requires_definition(self):
        """Reference-ссылка без определения — ошибка"""
        (self.root / "docs" / "ok.md").parent.mkdir(parents=True)
        (self.root / "docs" / "ok.md").write_text("x\n", encoding="utf-8")
        errors = self.check("[a][r]\n[r]: ok.md\n[b][missing]\n", "docs/doc.md")
        self.assertEqual(len(errors), 1)
        self.assertIn("missing", errors[0])

    def test_image_target_checked(self):
        """Цель image-ссылки проверяется на существование"""
        errors = self.check("![img](gone.png)\n", "docs/doc.md")
        self.assertEqual(len(errors), 1)

    def test_fragment_only_not_checked(self):
        """Ссылка только на fragment не проверяется"""
        self.assertEqual(self.check("[s](#x)\n", "docs/doc.md"), [])

    def test_historical_resolution_via_moves(self):
        # immutable-файл ссылается по старому адресу; цель перенесена
        """Историческая ссылка разрешается через карту moves"""
        (self.root / "docs" / "archive").mkdir(parents=True)
        (self.root / "docs" / "archive" / "a.md").write_text("x\n")
        errors = self.check("[a](../../a.md)\n",
                            "docs/tasks/T-1-evidence/r.md",
                            moves={"docs/a.md": "docs/archive/a.md"})
        self.assertEqual(errors, [])

    def test_historical_missing_target_reported(self):
        """Отсутствующая историческая цель сообщается ошибкой"""
        errors = self.check("[a](../../a.md)\n",
                            "docs/tasks/T-1-evidence/r.md",
                            moves={"docs/a.md": "docs/archive/a.md"})
        self.assertEqual(len(errors), 1)

    def test_historical_target_after_reopen_or_multiple_moves(self):
        """Цель после reopen/нескольких moves разрешается"""
        (self.root / "docs").mkdir()
        (self.root / "docs" / "a.md").write_text("returned")
        self.assertEqual(self.check("[a](a.md)", "docs/report.md", moves={
            "docs/a.md": "docs/archive/a.md",
            "docs/archive/a.md": "docs/a.md"}), [])
        (self.root / "docs" / "a.md").unlink()
        (self.root / "docs" / "c.md").write_text("current")
        self.assertEqual(self.check("[a](a.md)", "docs/report.md", moves={
            "docs/a.md": "docs/b.md", "docs/b.md": "docs/c.md"}), [])

    def test_oldest_origin_across_moves_and_reopen(self):
        """oldest origin разрешается через цепочку moves и reopen"""
        a, b, c = [self.path(f"docs/{name}") for name in ("a", "b", "c")]
        self.assertEqual(links.origin_path(c / "report.md", {a: b, b: c}),
                         a / "report.md")
        self.assertEqual(links.origin_path(a / "report.md", {a: b, b: a}),
                         a / "report.md")


class PlanRewritesTest(LinksCase):
    """План переписываний ссылок: входящие обновления, wikilink/html
    конфликты, frozen-файлы."""
    def setUp(self):
        super().setUp()
        (self.root / "docs").mkdir()

    def file(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def plan(self, files, extra=None, **kwargs):
        moves = {self.path("docs/a.md"): self.path("docs/archive/a.md")}
        for old, new in (extra or {}).items():
            moves[self.path(old)] = self.path(new)
        return links.plan_rewrites(files, moves, self.root, **kwargs)

    def test_incoming_update(self):
        """План переписываний включает входящие обновления ссылок"""
        doc = self.file("docs/b.md", "[a](a.md)\n")
        plan = self.plan([doc])
        self.assertEqual(plan["updates"][doc]["updated"],
                         "[a](archive/a.md)\n")
        self.assertEqual(plan["conflicts"], [])

    def test_unaffected_wikilink_ignored(self):
        """Не затронутый wikilink игнорируется планом"""
        doc = self.file("docs/b.md", "[[elsewhere]]\n")
        plan = self.plan([doc])
        self.assertEqual(plan["conflicts"], [])

    def test_affected_wikilink_is_conflict(self):
        """Затронутый wikilink — конфликт, а не переписывание"""
        doc = self.file("docs/b.md", "[[a]]\n")
        plan = self.plan([doc])
        self.assertEqual(len(plan["conflicts"]), 1)
        self.assertEqual(plan["conflicts"][0]["kind"], "unsupported")
        self.assertNotIn(doc, plan["updates"])

    def test_affected_html_is_conflict(self):
        """Затронутая html-ссылка — конфликт"""
        doc = self.file("docs/b.md", "<a href=\"a.md\">x</a>\n")
        plan = self.plan([doc])
        self.assertEqual(plan["conflicts"][0]["kind"], "unsupported")

    def test_managed_core_conflict_not_rewritten(self):
        """Managed core-файл с конфликтом не переписывается"""
        core = self.file("kit/rules/r.md", "[a](../../docs/a.md)\n")
        plan = self.plan([core], managed=[self.path("kit")])
        self.assertEqual(plan["conflicts"][0]["kind"], "managed-core")
        self.assertNotIn(core, plan["updates"])

    def test_frozen_file_skipped(self):
        """Frozen-файл пропускается планом переписываний"""
        frozen = self.file("docs/ev/r.md", "[a](../a.md)\n")
        plan = self.plan([frozen], frozen=[self.path("docs/ev")])
        self.assertEqual(plan["frozen"], [frozen])
        self.assertEqual(plan["updates"], {})


class IndexCase(CatalogCase):
    """Рендер индексов коллекций: группы, статусы, детерминизм,
    splice ручных частей."""
    COLLECTIONS = {"spec": {"active": "docs/specs",
                            "archive": "docs/archive/specs",
                            "index": "docs/specs/index.md"},
                   "plan": {"active": "docs/plans",
                            "archive": "docs/archive/plans",
                            "index": "docs/plans/index.md"}}

    def setUp(self):
        super().setUp()
        self.configure(dict(self.COLLECTIONS))
        (self.root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
        self.plans_archive = self.root / "docs" / "archive" / "plans"

    def write_indexes(self):
        catalog = self.catalog()
        for kind, entry in catalog["collections"].items():
            entry["index"].parent.mkdir(parents=True, exist_ok=True)
            entry["index"].write_text(
                documents.render_index_file(catalog, kind), encoding="utf-8")

    def test_empty_collection_renders_groups(self):
        """Пустая коллекция рендерит группы индекса"""
        region = documents.index_region(self.catalog(), "spec")
        self.assertIn("## Актуальные", region)
        self.assertIn("## Архив", region)
        self.assertIn("пока нет", region)

    def test_index_lists_states_and_dates(self):
        """Индекс перечисляет документы со статусами и датами"""
        write_doc(self.specs, "alpha.md", doc_meta())
        catalog = self.catalog()
        region = documents.index_region(catalog, "spec")
        self.assertIn("| [alpha.md](alpha.md) | accepted | 2026-01-02 |",
                      region)

    def test_archived_document_in_archive_group(self):
        """Архивный документ попадает в группу архива"""
        write_doc(self.specs_archive, "old.md",
                  doc_meta(document_id="spec:old", state="superseded",
                           archive={"at": "2026-01-05T00:00:00Z",
                                    "reason": "заменён", "successor": None,
                                    "operation_id": "op-1",
                                    "original_path": "docs/specs/old.md"}))
        region = documents.index_region(self.catalog(), "spec")
        archive = region.split("## Архив")[1]
        self.assertIn("| [old.md](../archive/specs/old.md) | superseded "
                      "| 2026-01-05 | заменён | — |", archive)

    def test_plan_index_links_tasks_without_status(self):
        """Индекс планов ссылается на задачи без статуса"""
        self.add_task("DEV-1", "in_progress")
        write_doc(self.root / "docs" / "plans", "road.md",
                  doc_meta(document_id="plan:road", kind="plan",
                           tasks=["DEV-1"]))
        region = documents.index_region(self.catalog(), "plan")
        self.assertIn("[DEV-1](../tasks/DEV-1.md)", region)
        self.assertNotIn("in_progress", region)

    def test_render_is_deterministic(self):
        """Рендер индекса детерминирован"""
        write_doc(self.specs, "alpha.md", doc_meta())
        catalog = self.catalog()
        self.assertEqual(documents.render_index_file(catalog, "spec"),
                         documents.render_index_file(catalog, "spec"))

    def test_splice_preserves_manual_parts(self):
        """Splice сохраняет ручные части индекса"""
        region = documents.index_region(self.catalog(), "spec")
        manual = ("# Свой заголовок\n\nРучное описание.\n\n"
                  f"{links.INDEX_START}\nстарый текст\n{links.INDEX_END}\n\n"
                  "Подвал руками.\n")
        spliced = documents.splice_index(manual, region)
        self.assertIn("# Свой заголовок", spliced)
        self.assertIn("Подвал руками.", spliced)
        self.assertIn("## Актуальные", spliced)
        self.assertNotIn("старый текст", spliced)
        # повторный проход на результате — те же байты
        again = documents.splice_index(spliced, region)
        self.assertEqual(again, spliced)

    def test_splice_without_markers_rejected(self):
        """Splice без маркеров отклоняется"""
        self.assertIsNone(documents.splice_index("# Ручной файл\n", "x"))
        bad = f"{links.INDEX_START}\nтело\n"
        self.assertIsNone(documents.splice_index(bad, "x"))

    def test_check_accepts_fresh_index(self):
        """check принимает свежий индекс"""
        write_doc(self.specs, "alpha.md", doc_meta())
        self.write_indexes()
        code, out, err = self.cli("check")
        self.assertEqual(code, 0, err)

    def test_check_detects_stale_index(self):
        """check обнаруживает устаревший индекс"""
        self.write_indexes()
        write_doc(self.specs, "beta.md", doc_meta(document_id="spec:beta"))
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("устарел", err)

    def test_check_detects_manual_index_without_markers(self):
        """check обнаруживает ручной индекс без маркеров"""
        self.specs.mkdir(parents=True, exist_ok=True)
        (self.specs / "index.md").write_text("# Ручной индекс\n",
                                             encoding="utf-8")
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("playbook-index", err)

    def test_check_detects_missing_index(self):
        """check обнаруживает отсутствующий индекс"""
        (self.root / "docs" / "specs" / "index.md").unlink(missing_ok=True)
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("нет файла индекса", err)


class MovesCase(CatalogCase):
    """Move-map ссылок: immutable-резолюция, конфликты карт,
    маскировка stale links."""
    def setUp(self):
        super().setUp()
        self.configure()
        self.specs.mkdir(parents=True, exist_ok=True)
        index = self.specs / "index.md"
        index.write_text(documents.render_index_file(self.catalog(), "spec"),
                         encoding="utf-8")

    def task_with_moves(self, moves, materials):
        write_card(self.root / "docs" / "archive" / "tasks", "DEV-1", "done")
        card = self.root / "docs" / "archive" / "tasks" / "DEV-1.md"
        meta = {"schema_version": 1, "moves": moves, "materials": materials}
        card.write_text(card.read_text(encoding="utf-8")
                        + f"\n{documents.META_START}\n```json\n"
                        + json.dumps(meta, ensure_ascii=False)
                        + f"\n```\n{documents.META_END}\n", encoding="utf-8")
        self.cli("index")

    def test_immutable_link_resolved_via_move_map(self):
        # карточка и её evidence перенесены в архив; отчёт внутри ссылается
        # по старому расположению (docs/tasks/…), а не по текущему
        """Immutable-ссылка разрешается через move-map"""
        evidence = self.root / "docs" / "archive" / "tasks" / \
            "DEV-1-evidence" / "sub"
        evidence.mkdir(parents=True)
        write_card(self.root / "docs" / "archive" / "tasks", "DEV-2", "done")
        (evidence / "r.md").write_text("[лог](../../DEV-2.md)\n",
                                       encoding="utf-8")
        self.task_with_moves(
            moves=[{"from": "docs/tasks/DEV-1-evidence",
                    "to": "docs/archive/tasks/DEV-1-evidence"},
                   {"from": "docs/tasks/DEV-2.md",
                    "to": "docs/archive/tasks/DEV-2.md"}],
            materials=[{"id": "m-1", "role": "report",
                        "source": "docs/archive/tasks/DEV-1-evidence",
                        "storage": "local", "required": True,
                        "immutable": True, "used_by": []}])
        code, out, err = self.cli("check")
        self.assertEqual(code, 0, err)

    def test_immutable_without_map_is_broken(self):
        # DEV-2 лежит по старому адресу, карты переноса нет: из текущего
        # (архивного) расположения immutable-файла ссылка не разрешается.
        """Immutable-ссылка без move-map — broken"""
        evidence = self.root / "docs" / "archive" / "tasks" / \
            "DEV-1-evidence" / "sub"
        evidence.mkdir(parents=True)
        write_card(self.root / "docs" / "tasks", "DEV-2", "in_progress")
        (evidence / "r.md").write_text("[лог](../../DEV-2.md)\n",
                                       encoding="utf-8")
        self.task_with_moves(moves=[], materials=[
            {"id": "m-1", "role": "report",
             "source": "docs/archive/tasks/DEV-1-evidence",
             "storage": "local", "required": True, "immutable": True,
             "used_by": []}])
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("DEV-2.md", err)

    def test_moves_do_not_mask_stale_links_in_normal_docs(self):
        """Moves не маскируют stale-ссылки в обычных документах"""
        doc = self.root / "docs" / "notes.md"
        doc.write_text("[x](tasks/DEV-2.md)\n", encoding="utf-8")
        write_card(self.root / "docs" / "archive" / "tasks", "DEV-2", "done")
        self.task_with_moves(
            moves=[{"from": "docs/tasks/DEV-2.md",
                    "to": "docs/archive/tasks/DEV-2.md"}],
            materials=[])
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("tasks/DEV-2.md", err)

    def test_conflicting_move_maps_rejected(self):
        """Конфликтующие move-map отклоняются"""
        write_card(self.root / "docs" / "tasks", "DEV-3", "in_progress")
        card = self.root / "docs" / "tasks" / "DEV-3.md"
        meta = {"schema_version": 1,
                "moves": [{"from": "docs/a.md", "to": "docs/x/a.md"}]}
        card.write_text(card.read_text(encoding="utf-8")
                        + f"\n{documents.META_START}\n```json\n"
                        + json.dumps(meta) + f"\n```\n{documents.META_END}\n",
                        encoding="utf-8")
        self.task_with_moves(
            moves=[{"from": "docs/a.md", "to": "docs/y/a.md"}],
            materials=[])
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("конфликтует", err)

    def test_move_self_target_rejected(self):
        """Move с целью в себя отклоняется"""
        self.task_with_moves(
            moves=[{"from": "docs/a.md", "to": "docs/a.md"}], materials=[])
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("в себя", err)


class AuditRegressionTest(LinksCase):
    """Постоянные регрессии находок аудита PB-022 (A1–A4)."""

    # A1: %-кодирование не меняет адресата
    def test_encoded_destination_identity(self):
        """A1: encoded-цели (%23/%3F/%25) сохраняют идентичность"""
        source = self.path("docs/src.md")
        for token, name in [("a%23b.md", "a#b.md"), ("a%3Fb.md", "a?b.md"),
                            ("a%2520b.md", "a%20b.md"),
                            ("./a%3Ab.md", "a:b.md")]:
            with self.subTest(token=token):
                expected = (self.path("docs") / name).resolve()
                out = links.rewrite(f"[x]({token})\n", source, source,
                                    {})["text"]
                token_out = links.scan(out)[0]["target"]
                parsed = urlsplit(token_out)
                self.assertEqual(parsed.scheme, "")
                actual = (self.path("docs") / unquote(parsed.path)).resolve()
                self.assertEqual(actual, expected, out)

    def test_encoded_angle_destination_identity(self):
        """A1: encoded angle-цель сохраняет идентичность"""
        source = self.path("docs/src.md")
        out = links.rewrite("[x](<a%20b.md>)\n", source, source,
                            {})["text"]
        token = links.scan(out)[0]["target"]
        actual = (self.path("docs")
                  / unquote(urlsplit(token[1:-1]).path)).resolve()
        self.assertEqual(actual, (self.path("docs") / "a b.md").resolve())

    def test_index_link_encodes_fragment_chars(self):
        """A2: index-ссылка кодирует fragment-символы"""
        target = self.path("docs/a#b.md")
        token = documents._index_link(self.root, self.path("docs"), target)
        actual = (self.path("docs") / unquote(urlsplit(token).path)).resolve()
        self.assertEqual(actual, target)

    def test_angle_query_fragment_stay_inside_token(self):
        """A2: query/fragment в angle-цели остаются внутри токена"""
        result = self.rewrite("[x](<a b.md?raw=1#sec>)\n", "docs/a.md",
                              "docs/archive/a.md")
        self.assertEqual(result["text"], "[x](<../a b.md?raw=1#sec>)\n")
        found = links.scan(result["text"])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["target"], "<../a b.md?raw=1#sec>")

    # A2: кодовые примеры не разбираются
    def test_multiline_code_span_not_scanned(self):
        """A3: многострочный code-span не сканируется"""
        self.assertEqual(scan_kinds("`пример\n[x](b.md)\nконец`\n"), [])

    def test_multiline_code_span_not_rewritten(self):
        """A3: многострочный code-span не переписывается"""
        text = "`пример\n[x](b.md)\nконец`\n[y](b.md)\n"
        result = self.rewrite(text, "docs/a.md", "docs/archive/a.md")
        self.assertEqual(result["text"],
                         "`пример\n[x](b.md)\nконец`\n[y](../b.md)\n")

    def test_indented_code_not_scanned_or_rewritten(self):
        """A3: indented code не сканируется и не переписывается"""
        text = "    [x](b.md)\n\n[y](b.md)\n"
        self.assertEqual(scan_kinds(text), [("inline", "b.md")])
        result = self.rewrite(text, "docs/a.md", "docs/archive/a.md")
        self.assertEqual(result["text"],
                         "    [x](b.md)\n\n[y](../b.md)\n")

    def test_indented_continuation_of_paragraph_is_scanned(self):
        """A3: indented-продолжение абзаца сканируется"""
        text = "абзац\n    [x](b.md)\n"
        self.assertEqual(scan_kinds(text), [("inline", "b.md")])

    def test_indented_code_not_checked(self):
        """A3: indented code не проверяется check_text"""
        errors = links.check_text("    [x](missing.md)\n", "a.md",
                                  self.path("docs"), self.root)
        self.assertEqual(errors, [])

    def test_unmatched_backtick_keeps_later_span_masked(self):
        # `` без пары — литерал; следующий `span` маскируется как код
        """A3: непарный backtick сохраняет маскировку span"""
        text = "``литерал\n`[x](b.md)`\n[y](b.md)\n"
        result = self.rewrite(text, "docs/a.md", "docs/archive/a.md")
        self.assertEqual(result["text"],
                         "``литерал\n`[x](b.md)`\n[y](../b.md)\n")

    def test_escaped_backtick_does_not_open_span(self):
        """A3: экранированный backtick не открывает span"""
        text = "\\`не код\n[x](b.md)\n"
        result = self.rewrite(text, "docs/a.md", "docs/archive/a.md")
        self.assertEqual(result["text"], "\\`не код\n[x](../b.md)\n")

    def test_backslash_parity_around_code_span(self):
        # 0 и 2 слеша: span — код и сохраняется; 1 и 3: backtick — литерал
        """A3: чётность backslash вокруг code-span"""
        for slashes, masked in [(0, True), (1, False), (2, True), (3, False)]:
            text = "\\" * slashes + "`[x](b.md)`\n[y](b.md)\n"
            with self.subTest(slashes=slashes):
                found = links.scan(text)
                kinds = [item["target"] for item in found]
                self.assertEqual(kinds, ["b.md"] if masked
                                 else ["b.md", "b.md"])
                result = self.rewrite(text, "docs/a.md", "docs/archive/a.md")
                tail = "b.md" if masked else "../b.md"
                self.assertEqual(result["text"],
                                 text.replace("[x](b.md)", f"[x]({tail})")
                                 .replace("[y](b.md)", "[y](../b.md)"))

    # A3: затронутая неподдерживаемая ссылка — конфликт при переносе источника
    def test_moved_source_html_and_wikilink_conflict(self):
        """A4: html/wikilink затронутого источника — конфликт"""
        (self.root / "docs").mkdir(parents=True)
        (self.root / "docs" / "b.md").write_text("x\n")
        source = self.path("docs/a.md")
        target = self.path("docs/archive/a.md")
        for text in ('<a href="b.md">x</a>\n', "[[b.md]]\n"):
            with self.subTest(text=text):
                source.write_text(text)
                plan = links.plan_rewrites([source],
                                           {source: target}, self.root)
                self.assertEqual(len(plan["conflicts"]), 1)
                self.assertEqual(plan["conflicts"][0]["kind"], "unsupported")

    def test_unquoted_html_affected_target_conflict(self):
        """A4: html-цель без кавычек с affected target — конфликт"""
        (self.root / "docs").mkdir(parents=True)
        other = self.path("docs/b.md")
        other.write_text("x\n")
        source = self.path("docs/a.md")
        source.write_text("<a href=b.md>x</a>\n")
        plan = links.plan_rewrites(
            [source], {other: self.path("docs/archive/b.md")}, self.root)
        self.assertEqual(len(plan["conflicts"]), 1)
        self.assertEqual(plan["conflicts"][0]["kind"], "unsupported")

    def test_unaffected_unsupported_link_is_silent(self):
        """A4: незатронутая unsupported-ссылка молчит"""
        (self.root / "docs").mkdir(parents=True)
        (self.root / "docs" / "b.md").write_text("x\n")
        source = self.path("docs/a.md")
        source.write_text('<a href="b.md">x</a>\n')
        plan = links.plan_rewrites([source], {}, self.root)
        self.assertEqual(plan["conflicts"], [])

    # A4: подписи со вложенными/экранированными скобками
    def test_nested_label_relocated_and_checked(self):
        """A4: вложенный label в ссылке переписывается и проверяется"""
        result = self.rewrite("[a [b] c](b.md)\n", "docs/a.md",
                              "docs/archive/a.md")
        self.assertEqual(result["text"], "[a [b] c](../b.md)\n")
        errors = links.check_text("[a [b] c](missing.md)\n", "a.md",
                                  self.path("docs"), self.root)
        self.assertEqual(len(errors), 1)

    def test_image_inside_label(self):
        """A4: image внутри label сканируется как отдельная ссылка"""
        found = scan_kinds("[![i](i.png)](b.md)\n")
        self.assertEqual(sorted(found),
                         [("image", "i.png"), ("inline", "b.md")])

    def test_escaped_brackets_in_label(self):
        """A4: экранированные скобки в label не ломают парсинг"""
        found = scan_kinds(r"[a \] b](x.md)" + "\n")
        self.assertEqual(found, [("inline", "x.md")])

    def test_image_inside_link_both_relocated(self):
        """A4: image внутри link обе цели переписываются"""
        result = self.rewrite("[![i](i.png)](b.md)\n", "docs/a.md",
                              "docs/archive/a.md")
        self.assertEqual(result["text"], "[![i](../i.png)](../b.md)\n")

    def test_missing_image_inside_link_checked(self):
        """A4: отсутствующая image внутри link — две ошибки"""
        errors = links.check_text("[![i](missing.png)](missing.md)\n",
                                  "a.md", self.path("docs"), self.root)
        self.assertEqual(len(errors), 2)

    # A5: совместный перенос источника и цели wikilink — не конфликт
    def test_joint_move_wikilink_no_conflict(self):
        """A4: совместный move wikilink без конфликта"""
        (self.root / "docs").mkdir(parents=True)
        source = self.path("docs/a.md")
        other = self.path("docs/b.md")
        other.write_text("x\n")
        source.write_text("[[b.md]]\n")
        plan = links.plan_rewrites(
            [source], {source: self.path("docs/archive/a.md"),
                       other: self.path("docs/archive/b.md")}, self.root)
        self.assertEqual(plan["conflicts"], [])

    def test_wikilink_suffix_still_resolves(self):
        """A4: wikilink-суффикс продолжает разрешаться"""
        (self.root / "docs").mkdir(parents=True)
        source = self.path("docs/a.md")
        source.write_text("[[b]]\n")
        # [[b]] → docs/b.md переносится, источник нет: конфликт
        plan = links.plan_rewrites(
            [source], {self.path("docs/b.md"): self.path("docs/x/b.md")},
            self.root)
        self.assertEqual(len(plan["conflicts"]), 1)

    def test_wikilink_extensionless_joint_move_no_conflict(self):
        """A4: wikilink без расширения при joint move без конфликта"""
        (self.root / "docs").mkdir(parents=True)
        source = self.path("docs/a.md")
        other = self.path("docs/b.md")
        other.write_text("x\n")
        source.write_text("[[b]]\n")
        plan = links.plan_rewrites(
            [source], {source: self.path("docs/archive/a.md"),
                       other: self.path("docs/archive/b.md")}, self.root)
        self.assertEqual(plan["conflicts"], [])

    def test_wikilink_fragment_detects_moved_target(self):
        """A4: wikilink-fragment обнаруживает перемещённую цель"""
        (self.root / "docs").mkdir(parents=True)
        other = self.path("docs/b.md")
        other.write_text("x\n")
        source = self.path("docs/a.md")
        source.write_text("[[b#section]]\n")
        plan = links.plan_rewrites(
            [source], {other: self.path("docs/archive/b.md")}, self.root)
        self.assertEqual(len(plan["conflicts"]), 1)

    def test_wikilink_ambiguous_candidates_conflict(self):
        """A4: неоднозначные wikilink-кандидаты — конфликт"""
        (self.root / "docs").mkdir(parents=True)
        for name in ("b", "b.md"):
            self.path(f"docs/{name}").write_text("x\n")
        source = self.path("docs/a.md")
        source.write_text("[[b]]\n")
        plan = links.plan_rewrites(
            [source], {self.path("docs/b.md"): self.path("docs/archive/b.md")},
            self.root)
        self.assertEqual(len(plan["conflicts"]), 1)


if __name__ == "__main__":
    unittest.main()
