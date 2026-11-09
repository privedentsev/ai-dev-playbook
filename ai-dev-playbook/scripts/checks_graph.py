"""Граф зависимостей проверок и Python AST-анализатор (PB-059).

Строит документ ``checks-graph.json`` (schema_version 1, контракт
``checks_contract`` PB-057): объявленные в политике explicit/observed
ребра плюс рёбра статического анализа импортов Python-файлов. Анализ
read-only: исходники разбираются через ``ast``, но код проекта и
discovery не исполняются (R-03, design §5).

Семантика ребра ``consumer → dependency``: изменение dependency
затрагивает consumer. Прежний сохранённый граф объединяется с новым
снимком: удаление файла или импорта не теряет прежних потребителей —
старые рёбра действуют, пока существует снимок, их содержащий (R-04).

Динамический импорт (``__import__``, ``importlib`` с нелитеральным
аргументом, алиасы ``from importlib import import_module``), импорт
файла вне любого модуля и неразбираемый исходник фиксируются в
``unknown`` с доказанной областью ``module``/``project`` — это
отсутствие доказательства независимости, а не доказательство её
наличия (R-04). Неразрешённые имена stdlib/внешних пакетов рёбер не
дают: они покрываются идентичностью среды, а не графом модулей; связь
удалённого проектного модуля сохраняет прежний снимок графа.
"""
import ast
import json
import sys
from pathlib import Path

import checks_contract
import checks_inventory
import verification_policy


ANALYZER_VERSION = "ast-v1"
GRAPH_FILENAME = "checks-graph.json"

_DYNAMIC_BUILTINS = {"__import__"}
_DYNAMIC_ATTRS = {("importlib", "import_module")}
_DYNAMIC_ATTR_NAMES = {"import_module"}
_DYNAMIC_FROM_IMPORTLIB = {"import_module", "__import__"}
_STDLIB = set(getattr(sys, "stdlib_module_names", ()))


def _raise(condition, message):
    if not condition:
        raise ValueError(message)


def owners_of(rel, policy):
    """Модули политики, владеющие путём; принадлежность — чистый glob."""
    return sorted(m["id"] for m in policy["modules"]
                  if any(checks_contract._match(rel, pattern)
                         for pattern in m["paths"]))


def _ancestors(rel):
    """Каталоги-предки файла от ближайшего к корню проекта."""
    parts = rel.split("/")[:-1]
    return ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]


def _resolve(name_parts, bases, files):
    """Проектные файлы модуля ``a.b``: ``a/b.py`` либо ``a/b/__init__.py``."""
    found = []
    for base in bases:
        rel = "/".join([base, *name_parts]) if base else "/".join(name_parts)
        for candidate in (rel + ".py", rel + "/__init__.py"):
            if candidate in files and candidate not in found:
                found.append(candidate)
    return found


def _resolve_dotted(dotted, bases, files):
    """Все префиксы пути ``a.b.c``: пакеты уровней тоже зависимости."""
    parts = dotted.split(".")
    found = []
    for end in range(1, len(parts) + 1):
        for target in _resolve(parts[:end], bases, files):
            if target not in found:
                found.append(target)
    return found


def _call_name(node):
    """Имя вызываемого: ``f(...)`` → ``f``, ``a.b(...)`` → ``(a, b)``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return (node.value.id, node.attr)
    return None


def _literal_str(node):
    return (node.value if isinstance(node, ast.Constant)
            and isinstance(node.value, str) else None)


def analyze_python(rel, source, files):
    """Разобрать один .py-файл: импорты → проектные файлы, динамика.

    Возвращает ``{targets: {path: evidence}, dynamic: [reason]}``.
    ``bases`` — корень проекта, все каталоги-предки файла и все
    верхнеуровневые каталоги проекта: flat- и src-layout разрешаются
    без исполнения кода; лишний базис даёт консервативное ребро, не
    пробел. Целевые пути проверяются по составу ``files``.
    """
    tree = ast.parse(source, filename=rel)
    bases = [""] + _ancestors(rel)
    tops = sorted({p.split("/", 1)[0] for p in files if "/" in p})
    for top in tops:
        if top not in bases:
            bases.append(top)
    targets, dynamic = {}, []
    local_dynamic = set()
    local_importlib = {"importlib"}

    def bind(dotted, lineno):
        for target in _resolve_dotted(dotted, bases, files):
            targets.setdefault(target, f"{rel}:{lineno}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    local_importlib.add(alias.asname or "importlib")
                else:
                    bind(alias.name, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "importlib" and not node.level:
                local_dynamic |= {
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name in _DYNAMIC_FROM_IMPORTLIB}
            if node.level:
                base_parts = rel.split("/")[:-1]
                base = "/".join(base_parts[:len(base_parts)
                                          - (node.level - 1)])
                module = (base + "/" + node.module).replace("/", ".") \
                    if node.module else base.replace("/", ".")
            else:
                module = node.module or ""
            if module:
                bind(module, node.lineno)
            for alias in node.names:
                if alias.name == "*":
                    continue
                bind(f"{module}.{alias.name}" if module else alias.name,
                     node.lineno)
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in _DYNAMIC_BUILTINS or name in _DYNAMIC_ATTRS \
                    or name in local_dynamic or \
                    (isinstance(name, tuple)
                     and name[0] in local_importlib
                     and name[1] in _DYNAMIC_ATTR_NAMES):
                literal = (_literal_str(node.args[0]) if node.args
                           else None)
                if literal is None:
                    dynamic.append(f"динамический импорт, строка "
                                   f"{node.lineno}")
                else:
                    bind(literal, node.lineno)
    return {"targets": targets, "dynamic": dynamic}


def analyze_files(root, policy, *, read_file=None):
    """Проанализировать все .py-файлы проекта статически.

    ``read_file`` — точка измерения/внедрения; по умолчанию читаются
    байты из дерева без исполнения. Файлы вне модулей тоже разбираются:
    их динамический импорт расширяет неизвестную область до проекта.
    Возвращает ``{edges, unknown, inventory, files}``.
    """
    root = Path(root)
    inventory = checks_inventory.build_inventory(root, policy)
    files = [rel for rel, entry in inventory["entries"].items()
             if entry["type"] == "file"]
    known = set(files)
    read = read_file or (
        lambda rel: (root / rel).read_bytes().decode("utf-8"))
    edges, unknown = {}, []
    for rel in sorted(p for p in files if p.endswith(".py")):
        try:
            found = analyze_python(rel, read(rel), known)
        except (SyntaxError, UnicodeDecodeError, ValueError) as error:
            found = {"targets": {}, "dynamic":
                     [f"неразбираемый Python: {error}"]}
        consumers = owners_of(rel, policy)
        for reason in found["dynamic"]:
            if consumers:
                for owner in consumers:
                    unknown.append({"scope": {"kind": "module",
                                              "id": owner},
                                    "reason": f"{rel}: {reason}"})
            else:
                unknown.append({"scope": {"kind": "project"},
                                "reason": f"{rel}: {reason}"})
        for target, evidence in sorted(found["targets"].items()):
            if target == rel or not consumers:
                continue
            dependencies = owners_of(target, policy)
            if not dependencies:
                for owner in consumers:
                    unknown.append({
                        "scope": {"kind": "module", "id": owner},
                        "reason": f"{rel}: импорт вне модулей {target}"})
                continue
            for consumer in consumers:
                for dependency in dependencies:
                    if consumer == dependency:
                        continue
                    key = (f"module:{consumer}",
                           f"module:{dependency}", "import")
                    edges[key] = {
                        "consumer": key[0], "dependency": key[1],
                        "kind": "import", "source": "analyzer",
                        "analyzer_version": ANALYZER_VERSION,
                        "evidence": evidence}
    return {"edges": list(edges.values()), "unknown": unknown,
            "inventory": inventory, "files": files}


def _edge_key(edge):
    return (edge["consumer"], edge["dependency"], edge["kind"])


def _unknown_key(item):
    return (item["scope"]["kind"], item["scope"].get("id", ""),
            item["reason"])


def union_graph(previous, fresh):
    """Объединить прежний и новый снимки графа: старые рёбра не теряются.

    Для одинакового ключа ребра побеждает свежее происхождение —
    доказательство обновляется, связь не исчезает. Загруженный документ
    иного проекта или политики сюда не подаётся: совместимость проверяет
    ``graph_compatible`` до вызова.
    """
    edges = {}
    for edge in previous.get("edges", []):
        edges[_edge_key(edge)] = edge
    for edge in fresh["edges"]:
        edges[_edge_key(edge)] = edge
    unknown = {}
    for item in previous.get("unknown", []):
        unknown[_unknown_key(item)] = item
    for item in fresh["unknown"]:
        unknown[_unknown_key(item)] = item
    doc = dict(fresh)
    doc["edges"] = [edges[key] for key in sorted(edges)]
    doc["unknown"] = [unknown[key] for key in sorted(unknown)]
    return doc


def build_graph(root, policy, *, previous=None, now=None,
                read_file=None):
    """Собрать ``checks-graph.json``: анализ + ребра политики + union.

    ``previous`` — сохранённый снимок графа (``load_graph``), при
    совместимости project_id/policy_digest объединяемый с новым;
    утраченный или чужой граф не подменяет свежий анализ. Объявленные
    ``policy.dependencies`` участвуют в снимке как есть — наблюдения
    могут расширять карту, но не сужать её (R-03).
    """
    analysis = analyze_files(root, policy, read_file=read_file)
    edges = {}
    for edge in policy["dependencies"]:
        edges[_edge_key(edge)] = edge
    for edge in analysis["edges"]:
        edges[_edge_key(edge)] = edge
    fresh = {
        "schema_version": checks_contract.GRAPH_SCHEMA_VERSION,
        "project_id": policy["project_id"],
        "policy_digest": verification_policy.fingerprint(policy),
        "created_at": checks_inventory._now(now),
        "analyzer_version": ANALYZER_VERSION,
        "edges": [edges[key] for key in sorted(edges)],
        "unknown": analysis["unknown"],
    }
    checks_contract.validate_graph(fresh, _raise)
    if previous is not None and \
            graph_compatible(previous, policy)["compatible"]:
        doc = union_graph(previous, fresh)
        checks_contract.validate_graph(doc, _raise)
        return doc
    return fresh


def graph_compatible(doc, policy):
    """Может ли снимок графа служить базой при текущей политике."""
    try:
        checks_contract.validate_graph(doc, _raise)
    except ValueError as error:
        return {"compatible": False, "reason": str(error)}
    if doc["project_id"] != policy["project_id"]:
        return {"compatible": False, "reason": "другой project_id"}
    if doc["policy_digest"] != verification_policy.fingerprint(policy):
        return {"compatible": False,
                "reason": "граф привязан к другой ревизии политики"}
    return {"compatible": True, "reason": "policy_digest совпадает"}


def load_graph(path):
    """Загрузить снимок графа; отсутствие и порча — ошибка."""
    data = json.loads(Path(path).read_bytes())
    if not isinstance(data, dict):
        raise ValueError("checks-graph: ожидается JSON-объект")
    return checks_contract.validate_graph(data, _raise)


def reverse_closure(edges, start):
    """Обратный транзитивный обход: кто зависит от рефа ``start``.

    Возвращает ``{ref: chain}``, где chain — список рёбер от ``start``
    до ref в порядке следования; циклы безопасны за счёт visited (R-04).
    Рефы вида ``kind:id`` (module/subsystem/check).
    """
    reverse = {}
    for edge in edges:
        reverse.setdefault(edge["dependency"], []).append(edge)
    reached = {start: []}
    frontier = [start]
    while frontier:
        current = frontier.pop(0)
        for edge in reverse.get(current, []):
            consumer = edge["consumer"]
            if consumer in reached:
                continue
            reached[consumer] = reached[current] + [edge]
            frontier.append(consumer)
    return reached
