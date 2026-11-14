"""Обследование проекта и предложение checks-политики (PB-063).

``setup preview`` — read-only обход дерева проекта без Git: классификация
путей, статическое обнаружение unittest-тестов (AST, без импорта и
исполнения кода проекта) и предложение черновика ``checks-policy.json``
схемы 1 (контракт PB-057). Предложение — не одобренная политика:
``approved_basis`` с меткой ``TODO`` заполняет инженер, запись выполняет
``setup apply`` через восстановимую файловую операцию.

Границы предложения:

- discovery ``python -m unittest`` не знает policy.excludes и служебные
  исключения inventory: тесты внутри исключаемых областей с целой цепочкой
  ``__init__.py`` будут обнаружены и предсказываются тем же правилом
  доступности, что и обычные тесты;
- ``load_tests``-протокол меняет набор тестов файла: такие файлы
  перечисляются в unknowns, их ID статически не предсказываются;
- наследование TestCase разрешается статически внутри проекта
  (локальные и импортируемые базовые классы, миксины, классы,
  импортированные в тестовый модуль); внешние и неоднозначные базы,
  неразрешённые импорты class-like имён и star-import перечисляются
  в unknowns — каталог может быть неполным;
- динамические определения классов (``type()``, декораторы фабрик,
  условные определения) статически не видны — первый прогон покажет
  расхождение каталога, это ожидаемая точка ручной доработки.
"""
import ast
import datetime as dt
import fnmatch
import json
import re
import stat
import sys
from pathlib import Path

import checks_contract
import checks_inventory
import verification_policy

TEST_GLOB = "test*.py"
MAX_SURVEY_ENTRIES = 50000
DEFAULT_TIMEOUT_SECONDS = 300
# Каталоги окружения/сборки без точки в имени: предлагаются к исключению.
ENV_DIR_NAMES = {"venv", "node_modules", "htmlcov", "dist", "build"}
SKIP_DECORATORS = {"skip", "skipIf", "skipUnless"}
EXPECTED_FAILURE_DECORATORS = {"expectedFailure"}
SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _slugify(value):
    return SLUG_STRIP.sub("-", value.lower()).strip("-")


def _first_line(doc):
    if not doc:
        return ""
    return " ".join(doc.strip().splitlines()[0].split())[:200]


def _decorator_name(node):
    expr = node.func if isinstance(node, ast.Call) else node
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return ""


class _SurveyLimit(Exception):
    pass


def _walk(root):
    """Обойти дерево без Git; вернуть файлы, каталоги, служебные и проблемы.

    Симлинки, специальные файлы и ошибки чтения не прерывают обследование:
    они попадают в ``problems`` — предложение остаётся черновиком для
    инженера. Обход не читает содержимое файлов и не исполняет код.
    """
    files, dirs, service, problems = [], set(), [], []
    visited = set()
    count = 0

    def descend(abs_dir, rel):
        nonlocal count
        try:
            info = abs_dir.stat()
            marker = (info.st_dev, info.st_ino)
            if marker in visited:
                problems.append(f"циклический каталог {rel or '.'}")
                return
            visited.add(marker)
            children = sorted(abs_dir.iterdir(), key=lambda p: p.name)
        except OSError as error:
            problems.append(f"нет доступа к {rel or '.'}: {error}")
            return
        for child in children:
            count += 1
            if count > MAX_SURVEY_ENTRIES:
                raise _SurveyLimit(
                    f"обход ограничен лимитом {MAX_SURVEY_ENTRIES} "
                    "записей; остальные пути считаются неизвестными")
            child_rel = f"{rel}/{child.name}" if rel else child.name
            try:
                st = child.lstat()
            except OSError as error:
                problems.append(f"нет доступа к {child_rel}: {error}")
                continue
            if stat.S_ISLNK(st.st_mode):
                problems.append(f"символическая ссылка {child_rel}")
                continue
            is_dir = stat.S_ISDIR(st.st_mode)
            reason = checks_inventory._service_reason(
                child_rel, child.name, is_dir)
            if reason is not None:
                service.append({"path": child_rel, "reason": reason})
                continue
            if is_dir:
                dirs.add(child_rel)
                descend(child, child_rel)
            elif stat.S_ISREG(st.st_mode):
                files.append(child_rel)
            else:
                problems.append(f"специальный файл {child_rel}")

    try:
        descend(root, "")
    except _SurveyLimit as error:
        problems.append(str(error))
    return files, dirs, service, problems


def _module_id(name, used):
    base = _slugify(name) or "module"
    ident, suffix = base, 2
    while ident in used:
        ident = f"{base}-{suffix}"
        suffix += 1
    used.add(ident)
    return ident


def _parse_python(path_abs, rel):
    """AST файла без исполнения; (tree, error)."""
    try:
        return ast.parse(path_abs.read_bytes(), filename=rel), None
    except (SyntaxError, ValueError, OSError, UnicodeError) as error:
        return None, f"{rel}: {type(error).__name__}: {error}"


def _is_testcase_key(key):
    """Ключ базы, гарантированно ведущий к unittest.TestCase."""
    return key == "TestCase" or (key is not None
                                 and key.rsplit(".", 1)[-1] == "TestCase")


def _base_key(node):
    """Ключ базового класса: имя, dotted-цепочка или None."""
    while isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts))
    return None


def _decorator_names(node):
    return {_decorator_name(dec) for dec in node.decorator_list}


def _provably_nonclass(expr):
    """Значение, которое заведомо не может быть классом TestCase.

    Доказывают не-класс только литералы, коллекции как целое и lambda:
    ``X = [A, B]`` связывает имя со списком, а не с элементом. Вызов
    (``type("X", bases, ns)`` и фабрики), ``Name``/``Attribute`` и
    ``Subscript`` (``Base = [Original][0]``) могут дать класс — форма
    AST не доказывает отсутствие TestCase, поэтому результат
    неопределёнен и для class-like импорта даёт unknowns.
    """
    return isinstance(expr, (ast.Constant, ast.List, ast.Tuple, ast.Set,
                             ast.Dict, ast.JoinedStr, ast.Lambda))


def _bound_names(target):
    """Имена, связываемые целью присваивания (Name/Tuple/Starred)."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _bound_names(elt)
    elif isinstance(target, ast.Starred):
        yield from _bound_names(target.value)


def _assign_kind(expr):
    """Привязка test-имени в теле класса → 'lambda'|'dead'|'unknown'.

    unittest собирает атрибуты test*, только если getattr даёт callable:
    lambda — вызываемое, литералы/None/коллекции и property() — нет,
    прочие выражения статически не определить.
    """
    if isinstance(expr, ast.Lambda):
        return "lambda"
    if isinstance(expr, (ast.Constant, ast.List, ast.Tuple, ast.Set,
                         ast.Dict, ast.JoinedStr)):
        return "dead"
    if (isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id == "property"):
        return "dead"
    if (isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id in {"staticmethod", "classmethod"}):
        return "lambda"
    return "unknown"


def _c3_merge(seqs):
    """C3-merge линеаризаций баз — порядок ``type.__mro__``.

    При несогласованной иерархии (Python отклонил бы класс при
    создании) возвращает конкатенацию без повторов.
    """
    seqs = [list(s) for s in seqs if s]
    out = []
    while True:
        seqs = [s for s in seqs if s]
        if not seqs:
            return out
        for seq in seqs:
            cand = seq[0]
            if all(cand not in s[1:] for s in seqs):
                out.append(cand)
                for s in seqs:
                    if s and s[0] == cand:
                        s.pop(0)
                break
        else:
            for s in seqs:
                out.extend(e for e in s if e not in out)
            return out


def _module_doc_of(trees, rel):
    tree = trees.get(rel)
    return _first_line(ast.get_docstring(tree)) if tree is not None else ""


class _ClassIndex:
    """Индекс классов и импортов всех разобранных py-файлов проекта.

    Нужен для статического разрешения наследования unittest.TestCase:
    ``unittest discover`` собирает тесты всех TestCase-потомков в
    namespace тестового модуля, включая унаследованные методы и
    импортированные базовые классы. Разрешение ограничено файлами
    проекта; внешние/неоднозначные базы фиксируются в unknowns.
    """

    def __init__(self, trees, file_set):
        self.trees = trees
        self.file_set = file_set
        self.classes = {}       # rel -> {name: classinfo}
        self.from_imports = {}  # rel -> {local: (module, level, orig)}
        self.aliases = {}       # rel -> {alias: dotted_module}
        self.star_imports = {}  # rel -> [module|None]
        self.module_bound = {}  # rel -> {name: не-класс-привязка}
        self.rebound = {}       # rel -> [classish-имена, вытесненные выражением]
        for rel, tree in trees.items():
            self._scan(rel, tree)
        self._memo = {}
        self._active = set()

    def _scan(self, rel, tree):
        """Привязки верхнего уровня — last-binding-wins, как в Python.

        ``class Base`` с последующим ``Base = None`` не оставляет имя
        в namespace модуля: discover такой класс не собирает. Поэтому
        ClassDef/from-импорты/alive-привязки учитываются только если
        это ПОСЛЕДНЯЯ привязка имени; имя, вытесненное выражением с
        неопределённым значением, уходит в ``rebound`` для unknowns.
        """
        defs, from_imp, aliases, stars = {}, {}, {}, []
        last, bound, rebound = {}, set(), []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                tests, kinds = {}, {}
                for sub in node.body:
                    if (isinstance(sub, (ast.FunctionDef,
                                         ast.AsyncFunctionDef))
                            and sub.name.startswith("test")):
                        decos = _decorator_names(sub)
                        kinds.pop(sub.name, None)
                        if "property" in decos:
                            tests.pop(sub.name, None)
                            kinds[sub.name] = "dead"
                        else:
                            tests[sub.name] = {
                                "doc": _first_line(ast.get_docstring(sub)),
                                "skip": bool(decos & SKIP_DECORATORS),
                                "xfail": bool(decos
                                              & EXPECTED_FAILURE_DECORATORS),
                            }
                    elif isinstance(sub, ast.Assign):
                        for target in sub.targets:
                            if (isinstance(target, ast.Name)
                                    and target.id.startswith("test")):
                                tests.pop(target.id, None)
                                kinds[target.id] = _assign_kind(sub.value)
                    elif (isinstance(sub, ast.AnnAssign)
                            and sub.value is not None
                            and isinstance(sub.target, ast.Name)
                            and sub.target.id.startswith("test")):
                        tests.pop(sub.target.id, None)
                        kinds[sub.target.id] = _assign_kind(sub.value)
                defs[node.name] = {
                    "bases": [_base_key(b) for b in node.bases],
                    "tests": tests,
                    "test_kinds": kinds,
                    "doc": _first_line(ast.get_docstring(node)),
                    "decorators": _decorator_names(node),
                }
                last[node.name] = "class"
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                last[node.name] = "func"
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    if alias.name == "*":
                        stars.append(module or None)
                    else:
                        local = alias.asname or alias.name
                        from_imp[local] = (module, node.level, alias.name)
                        last[local] = "from"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    bound_name = alias.asname or alias.name.split(".")[0]
                    if alias.asname:
                        aliases[alias.asname] = alias.name
                    last[bound_name] = "import"
            elif isinstance(node, ast.Assign):
                kind = ("dead" if _provably_nonclass(node.value)
                        else "assign")
                for target in node.targets:
                    for name in _bound_names(target):
                        last[name] = kind
            elif (isinstance(node, ast.AnnAssign)
                    and node.value is not None
                    and isinstance(node.target, ast.Name)):
                last[node.target.id] = (
                    "dead" if _provably_nonclass(node.value) else "assign")
            elif isinstance(node, ast.AnnAssign):
                pass  # ``x: int`` без значения не связывает имя
            elif isinstance(node, ast.AugAssign):
                for name in _bound_names(node.target):
                    last[name] = "assign"
            elif isinstance(node, ast.Delete):
                for target in node.targets:
                    for name in _bound_names(target):
                        last[name] = "dead"
        # Сверка: выживают только последние привязки; класс/импорт,
        # вытесненный выражением, — неопределённость, литералом/func —
        # детерминированное удаление из namespace.
        for name in list(defs):
            kind = last.get(name)
            if kind != "class":
                if kind == "assign":
                    rebound.append(name)
                del defs[name]
        for name in list(from_imp):
            kind = last.get(name)
            if kind != "from":
                if kind == "assign":
                    rebound.append(name)
                del from_imp[name]
        for name in list(aliases):
            if last.get(name) != "import":
                del aliases[name]
        for name, kind in last.items():
            if kind in ("dead", "func"):
                bound.add(name)
        self.classes[rel] = defs
        self.from_imports[rel] = from_imp
        self.aliases[rel] = aliases
        self.star_imports[rel] = stars
        self.module_bound[rel] = bound
        self.rebound[rel] = sorted(set(rebound))

    def module_file(self, dotted, from_rel, level):
        """Файлы-кандидаты модуля; 0 — нет, 1 — разрешён, >1 — конфликт."""
        rel_path = dotted.replace(".", "/") if dotted else ""
        if level:
            base = _dirname(from_rel)
            for _ in range(level - 1):
                base = _dirname(base)
            bases = [base]
        else:
            bases = ["", _dirname(from_rel)]
        candidates = set()
        for base in bases:
            if rel_path:
                candidates.add(_join(base, rel_path + ".py"))
                candidates.add(_join(base, rel_path + "/__init__.py"))
            else:
                candidates.add(_join(base, "__init__.py"))
        return sorted(c for c in candidates
                      if c in self.file_set and c.endswith(".py"))

    def _name_in_module(self, trel, name, seen):
        """Имя в пространстве модуля файла → ('class', rel, name) |
        ('submodule', rel, name) | None, включая цепочки реэкспорта."""
        if name in self.classes.get(trel, {}):
            return ("class", trel, name)
        imp = self.from_imports.get(trel, {}).get(name)
        if imp is not None and (trel, name) not in seen:
            module, level, orig = imp
            return self._resolve_from(module, level, orig, trel,
                                      seen | {(trel, name)})
        return None

    def _resolve_from(self, module, level, orig, from_rel, seen):
        """``from module import orig`` в файле from_rel → класс/подмодуль.

        Разрешает собственные классы модуля, реэкспорты через его
        from-импорты (с защитой от циклов) и подмодуль M.orig.
        """
        found = self.module_file(module, from_rel, level)
        if len(found) != 1:
            return None
        trel = found[0]
        resolved = self._name_in_module(trel, orig, seen)
        if resolved is not None:
            return resolved
        # Имя может быть подмодулем: from M import X → M/X.py.
        sub = self.module_file(
            f"{module}.{orig}" if module else orig, from_rel, level)
        if len(sub) == 1:
            return ("submodule", sub[0], orig)
        return None

    def _resolve_class_ref(self, key, file_rel):
        """База/имя → ('class', rel, name) | 'testcase' | 'submodule' | None."""
        if key is None:
            return None
        if _is_testcase_key(key):
            return "testcase"
        aliases = self.aliases.get(file_rel, {})
        head, _, rest = key.partition(".")
        if head in aliases:
            key = aliases[head] + ("." + rest if rest else "")
            head, _, rest = key.partition(".")
        if "." in key:
            module, _, cls = key.rpartition(".")
            found = self.module_file(module, file_rel, 0)
            if len(found) == 1:
                return self._name_in_module(found[0], cls, frozenset())
            return None
        local = self.classes.get(file_rel, {})
        if key in local:
            return ("class", file_rel, key)
        imp = self.from_imports.get(file_rel, {}).get(key)
        if imp is not None:
            module, level, orig = imp
            return self._resolve_from(module, level, orig, file_rel,
                                      frozenset())
        return None

    def analyze(self, rel, name):
        """Статус класса: testcase?, эффективные test-методы, mro, сомнения."""
        key = (rel, name)
        if key in self._memo:
            return self._memo[key]
        info = self.classes.get(rel, {}).get(name)
        if info is None:
            return {"testcase": False, "methods": frozenset(),
                    "mro": (), "uncertain": (), "own_tests": {}}
        if key in self._active:
            return {"testcase": None, "methods": frozenset(),
                    "mro": (), "own_tests": {},
                    "uncertain": (f"{rel}:{name}: цикл наследования",)}
        self._active.add(key)
        testcase, uncertain = False, []
        unknown_base = False
        parent_keys, parent_mros = [], []
        for base in info["bases"]:
            resolved = self._resolve_class_ref(base, rel)
            if resolved == "testcase":
                testcase = True
            elif isinstance(resolved, tuple) and resolved[0] == "class":
                trel, tname = resolved[1], resolved[2]
                child = self.analyze(trel, tname)
                testcase = testcase or child["testcase"] is True
                if child["testcase"] is None:
                    unknown_base = True
                uncertain.extend(child["uncertain"])
                parent_keys.append((trel, tname))
                parent_mros.append(list(child["mro"])
                                   or [(trel, tname)])
            else:
                unknown_base = True
                label = base if base is not None else "<динамическая база>"
                uncertain.append(f"{rel}:{name}: неразрешённая база "
                                 f"'{label}'")
        if testcase is not True and unknown_base:
            testcase = None
        # Линеаризация MRO (C3) — первое связывание имени в ней
        # решает, как и в Python: def/lambda собираются discover'ом,
        # литерал/property снимают унаследованный метод, прочее
        # выражение — неопределённость вызываемости.
        mro = [key] + [k for k in _c3_merge(parent_mros + [parent_keys])
                       if k != key]
        candidates = set()
        for k in mro:
            kinfo = self.classes.get(k[0], {}).get(k[1])
            if kinfo is not None:
                candidates.update(kinfo["tests"])
                candidates.update(kinfo["test_kinds"])
        methods, doubtful = set(), []
        for n in candidates:
            for k in mro:
                kinfo = self.classes.get(k[0], {}).get(k[1])
                if kinfo is None:
                    continue
                if n in kinfo["tests"]:
                    methods.add(n)
                    break
                kind = kinfo["test_kinds"].get(n)
                if kind == "lambda":
                    methods.add(n)
                    break
                if kind == "dead":
                    break
                if kind == "unknown":
                    doubtful.append(
                        f"{rel}:{name}: test-метод '{n}' разрешается "
                        "в выражение — вызываемость не определена "
                        "статически")
                    break
        uncertain.extend(doubtful)
        result = {"testcase": testcase, "methods": frozenset(methods),
                  "mro": tuple(mro),
                  "uncertain": tuple(dict.fromkeys(uncertain)),
                  "own_tests": info["tests"], "doc": info["doc"],
                  "decorators": info["decorators"]}
        self._active.discard(key)
        self._memo[key] = result
        return result

    def describe(self, emit_rel, emit_name, method, analysis):
        """Описание записи каталога по цепочке docstring'ов."""
        emit_info = self.classes.get(emit_rel, {}).get(emit_name, {})
        for drel, dname in analysis["mro"]:
            dinfo = self.classes.get(drel, {}).get(dname, {})
            mtests = dinfo.get("tests", {})
            if method in mtests:
                mt = mtests[method]
                for doc in (mt["doc"], dinfo.get("doc", ""),
                            emit_info.get("doc", ""),
                            _module_doc_of(self.trees, drel),
                            _module_doc_of(self.trees, emit_rel)):
                    if doc:
                        return doc
                return (f"unittest-метод {emit_name}.{method} "
                        f"(описание не определено статически)")
        return f"unittest-метод {emit_name}.{method}"

    def flags(self, emit_rel, emit_name, method, analysis):
        """skip/expected_failure записи с учётом иерархии декораторов."""
        skip_reason = xfail = None
        for drel, dname in analysis["mro"]:
            dinfo = self.classes.get(drel, {}).get(dname, {})
            mtests = dinfo.get("tests", {})
            if method in mtests:
                mt = mtests[method]
                if mt["skip"] and skip_reason is None:
                    skip_reason = ("декоратор skip на методе "
                                   "(обнаружен при обследовании)")
                if mt["xfail"]:
                    xfail = True
                break
        for drel, dname in analysis["mro"]:
            dinfo = self.classes.get(drel, {}).get(dname, {})
            decos = dinfo.get("decorators", set())
            hit = sorted(decos & SKIP_DECORATORS)
            if hit:
                skip_reason = skip_reason or (
                    f"декоратор @{hit[0]} на классе "
                    "(обнаружен при обследовании)")
                break
        return skip_reason, bool(xfail)


def _import_uncertainties(index, file_rel):
    """Неопределённости импортов тестового модуля для unknowns.

    Обнаружение unittest собирает TestCase-классы из всего namespace
    модуля: импортированное, но статически неразрешённое имя может
    оказаться тестовым классом. Помечаем только class-like имена
    (заглавная буква) из неразрешённых не-stdlib модулей и star-import.
    """
    out = []
    pending = {}
    for local_name, (module, level, orig) in \
            index.from_imports.get(file_rel, {}).items():
        head = (module or "").split(".")[0] if module else ""
        if head in sys.stdlib_module_names:
            continue
        found = index.module_file(module, file_rel, level)
        if len(found) == 1:
            # Модуль разрешён, но class-like имя может не резолвиться
            # (реэкспорт через неразрешённую цепочку, присваивание и т.п.)
            # — тогда это скрытая неопределённость каталога. Привязки,
            # заведомо не являющиеся классами (константы, коллекции,
            # lambda, вызовы), неопределённостью не считаются.
            if (local_name[:1].isupper()
                    and orig not in index.module_bound.get(found[0], ())
                    and index._resolve_from(
                        module, level, orig, file_rel,
                        frozenset()) is None):
                out.append(
                    f"{file_rel}: from {module} import {orig} — "
                    f"имя не разрешено статически в {found[0]}; "
                    "может быть TestCase-классом")
            continue
        if local_name[:1].isupper():
            pending.setdefault(module or ".", []).append(orig)
    for module, names in sorted(pending.items()):
        out.append(f"{file_rel}: from {module} import "
                   f"{', '.join(sorted(set(names)))} — модуль не "
                   "разрешён статически; имена могут быть "
                   "TestCase-классами")
    for module in index.star_imports.get(file_rel, []):
        out.append(f"{file_rel}: from {module or '.'} import * — "
                   "состав namespace статически неизвестен")
    return out


def _module_name_for(rel, start):
    """ dotted-имя модуля файла относительно start-каталога discover."""
    if start == ".":
        return rel[:-3].replace("/", ".")
    if rel.startswith(start + "/"):
        return rel[len(start) + 1:-3].replace("/", ".")
    return None


def _catalog_entries_for(index, file_rel, start, check_id):
    """Предсказанные записи каталога тестового файла с наследованием.

    ID записи использует ``__module__`` класса — файл, где класс
    определён, а не импортирующий тестовый модуль (семантика
    unittest). Возвращает (entries, uncertain).
    """
    entries, uncertain = [], _import_uncertainties(index, file_rel)
    # Module-level перепривязка: ClassDef/from-импорт, вытесненный
    # выражением неизвестного значения, — имя может оказаться
    # TestCase-классом в namespace discover.
    for name in index.rebound.get(file_rel, ()):
        uncertain.append(
            f"{file_rel}: '{name}' перепривязано выражением — может "
            "быть TestCase-классом в namespace модуля")
    local = index.classes.get(file_rel, {})
    candidates = []  # (emit_name, defining_rel, analysis)
    for name in local:
        analysis = index.analyze(file_rel, name)
        if analysis["testcase"] is not False or analysis["own_tests"]:
            uncertain.extend(analysis["uncertain"])
        if analysis["testcase"] is True:
            candidates.append((name, file_rel, analysis))
    # Импортированные TestCase-классы собираются discover'ом; ID берёт
    # __module__ определяющего файла, локальные имена затеняют.
    for local_name, (module, level, orig) in \
            index.from_imports.get(file_rel, {}).items():
        if local_name in local:
            continue
        resolved = index._resolve_class_ref(local_name, file_rel)
        if not (isinstance(resolved, tuple)
                and resolved[0] == "class"):
            continue
        trel, tname = resolved[1], resolved[2]
        analysis = index.analyze(trel, tname)
        if analysis["testcase"] is not False or analysis["own_tests"]:
            uncertain.extend(analysis["uncertain"])
        if analysis["testcase"] is True:
            candidates.append((tname, trel, analysis))
    uncertain = sorted(set(uncertain))
    for emit_name, drel, analysis in candidates:
        dmodule = _module_name_for(drel, start)
        if dmodule is None:
            uncertain.append(
                f"{file_rel}: класс {emit_name} определён вне "
                f"start-каталога {start} — ID не предсказан")
            continue
        for method in sorted(analysis["methods"]):
            entry = {"id": f"{dmodule}.{emit_name}.{method}",
                     "description": index.describe(
                         file_rel, emit_name, method, analysis),
                     "group": check_id}
            skip_reason, xfail = index.flags(
                file_rel, emit_name, method, analysis)
            if skip_reason:
                entry["skip"] = {"allowed": True, "reason": skip_reason}
            if xfail:
                entry["expected_failure"] = {
                    "allowed": True,
                    "reason": "декоратор @expectedFailure "
                              "(обнаружен при обследовании)"}
            entries.append(entry)
    return entries, sorted(set(uncertain))


def _imports_of(tree):
    """(первый сегмент, lineno) абсолютных импортов файла."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name.split(".")[0], node.lineno))
        elif (isinstance(node, ast.ImportFrom) and node.level == 0
              and node.module):
            found.append((node.module.split(".")[0], node.lineno))
    return found


def _has_load_tests(tree):
    return any(isinstance(node, ast.FunctionDef) and node.name == "load_tests"
               for node in tree.body)


def _dirname(rel):
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _join(base, rel):
    return rel if base in {"", "."} else f"{base}/{rel}"


def survey(root):
    """Обследовать проект; вернуть структуру предложения и unknowns.

    Ничего не пишет и не исполняет: обход каталогов, чтение байтов
    ``*.py`` только для ``ast.parse`` (без импорта модулей).
    """
    root = Path(root)
    _require(root.is_dir() and not root.is_symlink(),
             f"setup preview: {root} не является каталогом проекта")
    files, dirs, service, problems = _walk(root)
    file_set = set(files)

    # Верхнеуровневые области — только каталоги; файлы корня модулей не
    # образуют и попадают в unknowns.
    top_names = sorted({rel.split("/", 1)[0] for rel in dirs})
    excluded_areas, module_specs = [], []
    used_ids = set()
    module_by_dir = {}
    for name in top_names:
        hidden = name.startswith(".")
        env = name in ENV_DIR_NAMES
        if hidden or env:
            reason = ("скрытый каталог: исключён по умолчанию обследования"
                      if hidden else
                      "каталог окружения/сборки: исключён по умолчанию "
                      "обследования")
            excluded_areas.append({"dir": name, "reason": reason})
            continue
        ident = _module_id(name, used_ids)
        module_specs.append({"id": ident, "title": name, "dir": name,
                             "subsystem_id": "core",
                             "paths": [f"{name}/**"]})
        module_by_dir[name] = ident
    module_ids = {spec["id"] for spec in module_specs}
    excluded_dirs = {item["dir"] for item in excluded_areas}

    def owner_of(rel):
        return module_by_dir.get(rel.split("/", 1)[0])

    # --- unittest discovery: группы стартовых каталогов ---
    test_files = sorted(
        f for f in files
        if fnmatch.fnmatchcase(f.rsplit("/", 1)[-1], TEST_GLOB))
    py_files = sorted(f for f in files if f.endswith(".py"))

    def chain_ok(start, file_dir):
        """Достижим ли файл из start по цепочке __init__.py.

        Стартовый каталог сам пакетом быть не обязан; каждый каталог
        цепочки ниже него — обязан (семантика unittest discover).
        """
        if start == ".":
            rel = file_dir
        elif file_dir == start:
            rel = ""
        elif file_dir.startswith(start + "/"):
            rel = file_dir[len(start) + 1:]
        else:
            return False
        if rel == "":
            return True
        parts = rel.split("/")
        for idx in range(1, len(parts) + 1):
            prefix = "/".join(parts[:idx])
            init = _join(start, prefix) + "/__init__.py"
            if init not in file_set:
                return False
        return True

    has_root_tests = any(_dirname(f) == "" for f in test_files)

    def shallowest_start(file_dir):
        candidates = ["."] if has_root_tests else []
        parts = file_dir.split("/") if file_dir else []
        candidates.extend("/".join(parts[:i])
                          for i in range(1, len(parts) + 1))
        if file_dir == "":
            candidates.append(".")
        for start in candidates:
            if chain_ok(start, file_dir):
                return start
        return file_dir or "."

    starts, orphan_tests = {}, []
    for rel in test_files:
        file_dir = _dirname(rel)
        start = shallowest_start(file_dir)
        if start != "." and start.split("/", 1)[0] not in module_by_dir:
            orphan_tests.append(rel)
            continue
        starts.setdefault(start, []).append(rel)

    # --- AST-разбор py-файлов: каталог тестов + импорты ---
    trees, parse_errors = {}, []
    for rel in py_files:
        tree, error = _parse_python(root / rel, rel)
        if error:
            parse_errors.append(error)
        else:
            trees[rel] = tree

    index = _ClassIndex(trees, file_set)
    check_specs, catalog_entries = [], []
    load_tests_files, id_collisions = [], []
    class_uncertainties = []
    used_check_ids, catalog_seen = set(), {}
    for start in sorted(starts):
        covered = starts[start]
        base = "root" if start == "." else start.rsplit("/", 1)[-1]
        slug = _slugify(base) or "tests"
        check_id, suffix = f"unittest-{slug}", 2
        while check_id in used_check_ids:
            check_id = f"unittest-{slug}-{suffix}"
            suffix += 1
        used_check_ids.add(check_id)
        if start == ".":
            check_modules = sorted(module_ids)
        else:
            check_modules = [module_by_dir[start.split("/", 1)[0]]]
        spec = {"id": check_id,
                "title": f"Unittest-набор {start}",
                "purpose": f"Обнаружение и исполнение unittest-тестов "
                           f"каталога {start}",
                "kind": "test", "modules": check_modules,
                "adapter": checks_contract.BUILTIN_ADAPTER,
                "argv": ["python3", "-m", "unittest", "discover",
                         "-s", start, "-v"],
                "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
                "start_dir": start, "covered": covered}
        if start == ".":
            # Файлы вне модулей и исключаемых областей не покрываются
            # модульными входами — объявляем их явно для digest снимка:
            # все py-файлы (покрытые тесты — подмножество py_files,
            # а также потенциальные базы/помощники наследования).
            inputs = sorted(
                rel for rel in py_files
                if owner_of(rel) is None
                and rel.split("/", 1)[0] not in excluded_dirs)
            if inputs:
                spec["inputs"] = inputs
        check_specs.append(spec)
        for rel in covered:
            tree = trees.get(rel)
            if tree is None:
                continue  # ошибка разбора уже в problems
            if _has_load_tests(tree):
                load_tests_files.append(rel)
                continue
            entries, uncertain = _catalog_entries_for(
                index, rel, start, check_id)
            class_uncertainties.extend(uncertain)
            for entry in entries:
                if entry["id"] in catalog_seen:
                    id_collisions.append(entry["id"])
                    continue
                catalog_seen[entry["id"]] = check_id
                catalog_entries.append(entry)

    # --- предлагаемые зависимости: статически неразрешённые импорты ---
    stem_index = {}
    dir_names = {}
    for rel in py_files:
        stem_index.setdefault(Path(rel).stem, []).append(rel)
    for rel in dirs:
        dir_names.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)

    edges, ambiguous, unresolved = {}, [], {}
    for rel in py_files:
        consumer = owner_of(rel)
        tree = trees.get(rel)
        if consumer is None or tree is None:
            continue
        for seg, lineno in _imports_of(tree):
            if seg in sys.stdlib_module_names:
                continue
            targets, owners = [], set()
            for cand in stem_index.get(seg, []):
                owner = owner_of(cand)
                if owner is not None:
                    owners.add(owner)
                    targets.append(cand)
            for cand in dir_names.get(seg, []):
                owner = owner_of(cand)
                if owner is not None:
                    owners.add(owner)
                    targets.append(cand + "/")
            deps = owners - {consumer}
            if len(deps) > 1:
                ambiguous.append(
                    f"{rel}:{lineno}: import {seg} — владельцы "
                    f"{sorted(deps)}")
            elif len(deps) == 1:
                dep = f"module:{sorted(deps)[0]}"
                key = (f"module:{consumer}", dep, "import")
                if key not in edges:
                    edges[key] = {
                        "consumer": key[0], "dependency": dep,
                        "kind": "import", "source": "explicit",
                        "evidence": f"survey: {rel}:{lineno} → "
                                    f"{targets[0]}"}
            elif not owners:
                unresolved[seg] = unresolved.get(seg, 0) + 1

    catalog_entries.sort(key=lambda e: e["id"])
    excludes = [{"pattern": f"{item['dir']}/**",
                 "reason": item["reason"], "origin": "survey"}
                for item in excluded_areas]
    unknowns = {
        "root_files": sorted(f for f in files if "/" not in f),
        "orphan_test_files": sorted(orphan_tests),
        "load_tests_files": sorted(load_tests_files),
        "id_collisions": sorted(set(id_collisions)),
        "parse_errors": parse_errors,
        "problems": problems,
        "ambiguous_imports": ambiguous,
        "unresolved_imports": dict(sorted(unresolved.items())),
        "class_uncertainties": sorted(set(class_uncertainties)),
    }
    return {
        "root": str(root),
        "stats": {"files": len(files), "dirs": len(dirs),
                  "py_files": len(py_files),
                  "test_files": len(test_files)},
        "service_excluded": service,
        "excluded_areas": excluded_areas,
        "modules": module_specs,
        "checks": check_specs,
        "dependencies": [edges[key] for key in sorted(edges)],
        "excludes": excludes,
        "catalog_entries": catalog_entries,
        "unknowns": unknowns,
    }


def build_policy(result, root_name, *, now=None):
    """Собрать черновик checks-policy.json схемы 1 из обследования.

    ``approved_basis`` содержит TODO-маркеры: ``setup apply`` отклоняет
    политику без заполненного решения инженера.
    """
    moment = now or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    entries = result["catalog_entries"]
    base = _slugify(root_name) or "project"
    policy = {
        "schema_version": checks_contract.POLICY_SCHEMA_VERSION,
        "policy_id": f"{base}-checks",
        "revision": 1,
        "project_id": base,
        "trust_mode": "local",
        "approved_basis": {
            "who": "TODO",
            "at": moment.isoformat(),
            "source": "TODO",
            "detail": "TODO: основание одобрения политики проверок "
                      "инженером",
        },
        "subsystems": [{"id": "core", "title": "Основная подсистема"}],
        "modules": [{k: v for k, v in spec.items() if k != "dir"}
                    for spec in result["modules"]],
        "checks": [],
        "dependencies": result["dependencies"],
        "excludes": result["excludes"],
        "environment": {
            "toolchain": [{"id": "python",
                           "version": f"{sys.version_info.major}."
                                      f"{sys.version_info.minor}"}],
            "external": [],
        },
        "fallback": {
            "expansion_order": list(checks_contract.EXPANSION_ORDER),
            "approved_commands": [],
            "limits": {"max_seconds": 3600},
            "unknown_scope": "project",
        },
        "test_catalog": {
            "adapter": checks_contract.BUILTIN_ADAPTER,
            "fingerprint": verification_policy.fingerprint(entries),
            "entries": entries,
        },
    }
    for spec in result["checks"]:
        policy["checks"].append(
            {k: v for k, v in spec.items()
             if k not in {"start_dir", "covered"}})
    return policy


def policy_json(policy):
    return json.dumps(policy, ensure_ascii=False, indent=2) + "\n"


def _print_unknowns(unknowns, emit):
    def section(title, items, limit=20):
        if not items:
            return
        emit(f"  {title}:")
        for item in items[:limit]:
            emit(f"    - {item}")
        if len(items) > limit:
            emit(f"    … и ещё {len(items) - limit}")

    emit("Неопределённости и проблемы обследования:")
    section("файлы в корне без модуля (unknown_scope=project)",
            unknowns["root_files"])
    section("тестовые файлы в исключаемой области — проверки не "
            "предложены", unknowns["orphan_test_files"])
    section("load_tests-протокол: test ID не предсказаны статически, "
            "допишите записи каталога вручную",
            unknowns["load_tests_files"])
    section("дубликаты test ID между проверками", unknowns["id_collisions"])
    section("ошибки разбора Python", unknowns["parse_errors"])
    section("симлинки/спецфайлы/ошибки обхода", unknowns["problems"])
    section("неоднозначные импорты (несколько модулей-владельцев)",
            unknowns["ambiguous_imports"])
    section("наследование/импорты классов: каталог может быть "
            "неполным", unknowns["class_uncertainties"], limit=30)
    if unknowns["unresolved_imports"]:
        pairs = ", ".join(f"{name}×{count}"
                          for name, count in
                          unknowns["unresolved_imports"].items())
        emit(f"  неразрешённые импорты (возможно внешние "
             f"пакеты): {pairs}")


def preview(root, *, out=None):
    """Напечатать read-only обследование и черновик политики."""
    emit = out.append if out is not None else print
    result = survey(root)
    policy = build_policy(result, Path(result["root"]).name)
    stats = result["stats"]
    emit(f"setup preview: обследование {result['root']} "
         "(read-only: без записи и без исполнения кода проекта)")
    emit(f"Файлов: {stats['files']} (python: {stats['py_files']}, "
         f"тестов: {stats['test_files']}), каталогов: {stats['dirs']}")
    if result["service_excluded"]:
        names = ", ".join(sorted({item["path"]
                                  for item in result["service_excluded"]}))
        emit(f"Служебные исключения обхода: {names}")
    if result["excludes"]:
        emit("Предлагаемые исключения (origin=survey):")
        for item in result["excludes"]:
            emit(f"  - {item['pattern']} — {item['reason']}")
    emit(f"Модули ({len(result['modules'])}):")
    for spec in result["modules"]:
        emit(f"  - {spec['id']} ← {spec['dir']}/ "
             f"({', '.join(spec['paths'])})")
    emit(f"Проверки ({len(result['checks'])}):")
    for spec in result["checks"]:
        emit(f"  - {spec['id']}: {' '.join(spec['argv'])} "
             f"(timeout {spec['timeout_seconds']}s, тестов: "
             f"{len(spec['covered'])}, модули: "
             f"{', '.join(spec['modules']) or '—'})")
    emit(f"Каталог тестов: {len(result['catalog_entries'])} записей, "
         f"отпечаток {policy['test_catalog']['fingerprint'][:19]}…")
    if result["dependencies"]:
        emit(f"Предлагаемые зависимости: {len(result['dependencies'])} "
             "рёбер import (source=explicit)")
    _print_unknowns(result["unknowns"], emit)
    try:
        checks_contract.validate_policy(policy, _require)
        contract_state = "прошёл validate_policy"
    except ValueError as error:
        contract_state = f"НЕ прошёл validate_policy: {error}"
    emit("")
    emit("Это ПРЕДЛОЖЕНИЕ, а не одобренная политика: заполните "
         "approved_basis (who/at/source/detail), сохраните JSON в "
         "checks-policy.json и выполните:")
    emit("  python3 ai-dev-playbook/scripts/playbook.py setup apply")
    emit(f"Валидация черновика: {contract_state}")
    emit("--- checks-policy.json (черновик) ---")
    emit(policy_json(policy).rstrip("\n"))
    return result, policy
