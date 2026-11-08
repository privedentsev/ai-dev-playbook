"""Контракт автоматического выбора и доверенных результатов проверок (PB-057).

Версионированные схемы policy/graph/result/receipt, контракт адаптера и
классификация изменений одобренной политики. Анализ зависимостей и снимки
(PB-058), выбор (PB-059), runner и результаты (PB-060), доверенный контур
(PB-061), lifecycle/release (PB-062) и подключение проекта (PB-063)
реализуются последующими карточками; здесь — только контракты, валидаторы
и окончательный синтаксис CLI. Локальная запись ``approved`` в JSON не
является авторизацией: одобрение политики — заявленное решение инженера
``approved_basis``, а проверку полномочий выполняет внешний контур.

Версии схем независимы от card_format_version, task_workflow и report v1/v2.
Неизвестные версии и поля отклоняются, legacy-контракты не меняются.

``checks-policy.json`` (schema_version 1) — отдельный версионированный
документ проекта; ссылка ``project.json → checks_policy`` включается явной
миграцией PB-063. Поля:

- ``policy_id`` slug, ``revision`` положительное целое, ``project_id``;
- ``trust_mode``: ``local`` (диагностика без границы прав) | ``protected``
  (квитанции внешнего verifier);
- ``approved_basis``: ``{who, at, source, detail}`` — основание инженера;
- ``modules``: ``{id, title, subsystem_id, paths, shared?}``; общее
  владение путём требует ``shared: true`` у всех владельцев;
- ``subsystems``: ``{id, title}`` — границы fallback;
- ``checks``: ``{id, title, purpose, kind, modules, adapter?, argv?,
  timeout_seconds?, scenario?, inputs?, applicability?}``; kind —
  ``test`` (adapter+argv+timeout), ``command`` (argv+timeout, постусловие
  по exit-коду) или ``manual`` (scenario);
- ``dependencies``: ребро ``{consumer, dependency, kind, source,
  evidence?, analyzer_version?}``; концы — ``module:<id>``,
  ``subsystem:<id>`` или ``check:<id>``; kind — import/resource/config/
  api/build; source — explicit/analyzer/observed, analyzer требует
  analyzer_version;
- ``excludes``: ``{pattern, reason, origin}``; широкое исключение
  (литерал либо хвост ``**``/``*``), накрывающее объявленный вход,
  и любое исключение внутри объявленной области — ошибка.
  Незаякоренный glob (пустой литеральный префикс) наблюдает корень
  проекта и проверяется тем же правилом; селективный glob с
  литеральным именем на конце допускается, но glob, совпадающий с
  явным литеральным входом, — ошибка при любой селективности;
- ``environment``: ``{toolchain: [{id, version?}], external: [{id,
  freshness}]}``; freshness — ``version`` | ``ttl`` | ``always_run``;
- ``fallback``: ``{expansion_order, approved_commands, limits,
  unknown_scope}``; лимиты — положительные конечные числа;
- ``test_catalog``: ``{adapter, fingerprint, entries}``; entry —
  ``{id, description, group?, skip?, expected_failure?}`` с непустым
  описанием. Допуски skipped и expected_failure задаются раздельно
  ``{allowed, reason?}``; unexpected_success допуска не имеет.

``checks-graph.json`` (schema_version 1): снимок графа анализа —
``{project_id, policy_digest, created_at, analyzer_version, edges,
unknown}``. ``unknown`` — ``{scope: {kind: module|subsystem|project,
id?}, reason}`` с доказанной границей.

Результат проверки (schema_version 1): ``{result_id, project_id,
check_id, kind, binding, status, ...}``. ``binding`` связывает результат
с ``policy_digest``, ``definition_digest``, adapter/runner identity,
``input_digest``, ``dependency_closure_digest``, ``environment`` и
``task_requirements_digest``. ``reused`` ссылается на исходный
результат. kind=test требует ``tests`` с expected/actual outcomes;
command — ``command`` с exit_code и логами; manual — ``manual`` с
observation/scenario_digest/confirmation. ``blocked`` и
``not_applicable`` требуют ``reason``.

Квитанция (schema_version 1): ``{receipt_id, project_id,
candidate_digest, policy_digest, policy_revision, engine, issuer,
issued_at, subject, signature}``. ``subject.kind`` — ``run`` |
``aggregate``; aggregate перечисляет ``source_receipts``. Проверка
криптографии — внешний контур (PB-061); валидатор проверяет формат.

CLI (окончательный синтаксис; операции — владельцы карт):
- ``checks plan <TASK>`` — read-only объяснение; PB-059;
- ``checks run <TASK> [--scope task|project] [--mode stale|force]
  [--checkpoint NAME]`` — PB-060, потребление release — PB-062;
- ``checks status <TASK>`` — покрытие и ограничения доверия; PB-060;
- ``setup preview|apply`` — обследование и одобренная запись; PB-063.

Область (task/project) и режим (stale/force) — ортогональные оси;
``--mode force`` выполняет весь набор области заново. Адаптер
``python-unittest`` (protocol 1): catalog со стабильными test_id и
непустыми описаниями; outcomes — passed/failed/error/skipped/
expected_failure/unexpected_success; skip допустим только по записи
``skip.allowed`` каталога.

``classify_policy_change(old, new)`` классифицирует правку политики:
``extension`` — расширение связей/контроля (новые проверки, модули,
зависимости, входы, сужение исключений, ужесточение лимитов);
``weakening`` — ослабление (удаление/отключение проверок и связей,
новые или расширенные исключения, расширение допустимых
skip/expected_failure, смена argv/adapter/trust/runner, рост лимитов).
Новая проверка kind=test|command — extension только в пределах уже
одобренных команд (argv существующих проверок и
fallback.approved_commands); новая исполняемая команда — weakening.
Ослабление требует нового решения инженера и не авторизуется полем
``approved`` старой политики.
"""
import fnmatch
import math
import re


POLICY_SCHEMA_VERSION = 1
GRAPH_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1
ADAPTER_PROTOCOL_VERSION = 1
BUILTIN_ADAPTER = "python-unittest"
POLICY_FILENAME = "checks-policy.json"
PROJECT_LINK_FIELD = "checks_policy"

TRUST_MODES = {"local", "protected"}
CHECK_KINDS = {"test", "command", "manual"}
DEPENDENCY_KINDS = {"import", "resource", "config", "api", "build"}
DEPENDENCY_SOURCES = {"explicit", "analyzer", "observed"}
EXCLUDE_ORIGINS = {"survey", "manual", "template"}
FRESHNESS_KINDS = {"version", "ttl", "always_run"}
RESULT_STATUSES = {"passed", "failed", "timeout", "not_run",
                   "not_applicable", "blocked"}
TEST_OUTCOMES = {"passed", "failed", "error", "skipped",
                 "expected_failure", "unexpected_success"}
UNKNOWN_SCOPES = {"module", "subsystem", "project"}
RECEIPT_KINDS = {"run", "aggregate"}
SIGNATURE_ALGORITHMS = {"ed25519", "hmac-sha256"}
REF_KINDS = {"module", "subsystem", "check"}
EXPANSION_ORDER = ["module", "subsystem", "project"]

SLUG = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")

POLICY_FIELDS = {"schema_version", "policy_id", "revision", "project_id",
                 "trust_mode", "approved_basis", "modules", "subsystems",
                 "checks", "dependencies", "excludes", "environment",
                 "fallback", "test_catalog"}
MODULE_FIELDS = {"id", "title", "subsystem_id", "paths", "shared"}
SUBSYSTEM_FIELDS = {"id", "title"}
CHECK_FIELDS = {"id", "title", "purpose", "kind", "modules", "adapter",
                "argv", "timeout_seconds", "scenario", "inputs",
                "applicability", "required_environment"}
EDGE_FIELDS = {"consumer", "dependency", "kind", "source", "evidence",
               "analyzer_version"}
EXCLUDE_FIELDS = {"pattern", "reason", "origin"}
ENVIRONMENT_FIELDS = {"toolchain", "external"}
TOOLCHAIN_FIELDS = {"id", "version"}
EXTERNAL_FIELDS = {"id", "freshness"}
FRESHNESS_FIELDS = {"kind", "version", "ttl_seconds"}
FALLBACK_FIELDS = {"expansion_order", "approved_commands", "limits",
                   "unknown_scope"}
LIMIT_FIELDS = {"max_seconds", "max_bytes"}
CATALOG_FIELDS = {"adapter", "fingerprint", "entries"}
CATALOG_ENTRY_FIELDS = {"id", "description", "group", "skip",
                        "expected_failure"}
ALLOW_FIELDS = {"allowed", "reason"}
BASIS_FIELDS = {"who", "at", "source", "detail"}

GRAPH_FIELDS = {"schema_version", "project_id", "policy_digest",
                "created_at", "analyzer_version", "edges", "unknown"}
UNKNOWN_FIELDS = {"scope", "reason"}
SCOPE_FIELDS = {"kind", "id"}

RESULT_FIELDS = {"schema_version", "result_id", "project_id", "check_id",
                 "kind", "binding", "status", "reused", "started_at",
                 "finished_at", "duration_seconds", "tests", "command",
                 "manual", "reason", "evidence"}
BINDING_FIELDS = {"policy_digest", "definition_digest", "adapter",
                  "runner", "input_digest", "dependency_closure_digest",
                  "environment", "task_requirements_digest"}
IDENTITY_FIELDS = {"id", "version"}
REUSED_FIELDS = {"result_id", "run_id", "issued_at", "input_digest"}
TESTS_FIELDS = {"expected", "actual"}
ACTUAL_FIELDS = {"id", "outcome", "reason"}
COMMAND_RESULT_FIELDS = {"exit_code", "stdout_log", "stderr_log"}
MANUAL_FIELDS = {"observation", "scenario_digest", "confirmation"}
CONFIRMATION_FIELDS = {"who", "at", "statement"}

RECEIPT_FIELDS = {"schema_version", "receipt_id", "project_id",
                  "candidate_digest", "policy_digest", "policy_revision",
                  "engine", "issuer", "issued_at", "subject", "signature"}
SUBJECT_FIELDS = {"kind", "results", "source_receipts"}
SUBJECT_RESULT_FIELDS = {"check_id", "result_digest", "status"}
SIGNATURE_FIELDS = {"algorithm", "key_id", "value"}

INPUT_FIELDS = {"type", "path", "names"}
INPUT_TYPES = {"file", "directory", "path", "task_projection",
               "policy_projection", "environment"}
APPLICABILITY_FIELDS = {"kind", "paths", "checkpoint"}
APPLICABILITY_KINDS = {"always", "when_paths", "checkpoint"}


def _label_str(label):
    return f"{label}: "


def _unknown(obj, allowed, label, require):
    require(isinstance(obj, dict) and not isinstance(obj, bool),
            f"{label} должен быть объектом")
    extra = sorted(set(obj) - allowed)
    require(not extra, f"{label}: неизвестные поля {extra}")


def _str(value, label, require, *, slug=False, ident=False):
    require(isinstance(value, str) and bool(value.strip()),
            f"{label} должен быть непустой строкой")
    if slug:
        require(SLUG.fullmatch(value) is not None,
                f"{label} должен быть slug [a-z0-9][a-z0-9_-]*: {value!r}")
    if ident:
        require(value.isascii() and IDENT.fullmatch(value) is not None,
                f"{label} должен быть ASCII-идентификатором: {value!r}")
    return value


def _int(value, label, require, *, minimum=0):
    require(isinstance(value, int) and not isinstance(value, bool)
            and value >= minimum,
            f"{label} должен быть целым числом >= {minimum}")
    return value


def _num(value, label, require, *, positive=True):
    require(type(value) in (int, float) and math.isfinite(value)
            and (value > 0 if positive else value >= 0),
            f"{label} должен быть {'положительным' if positive else 'неотрицательным'} "
            f"конечным числом")
    return value


def _digest(value, label, require):
    require(isinstance(value, str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", value or "") is not None,
            f"{label} должен быть sha256-отпечатком")
    return value


def _iso(value, label, require):
    ok = False
    if isinstance(value, str):
        try:
            import datetime as dt
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            ok = parsed.tzinfo is not None
        except ValueError:
            ok = False
    require(ok, f"{label} должен быть ISO-временем с часовым поясом")
    return value


def _path(value, label, require, *, pattern=False):
    """Нормализованный относительный POSIX-путь или glob без выхода за корень."""
    require(isinstance(value, str) and value and "\\" not in value
            and "\0" not in value and not value.startswith("/"),
            f"{label}: ожидается относительный POSIX-путь")
    parts = value.split("/")
    require(all(p not in {"", ".", ".."} for p in parts),
            f"{label}: неканонический путь {value!r}")
    if not pattern:
        require(not any(c in value for c in "*?["),
                f"{label}: glob не является явным путём {value!r}")
    return value


def _ref(value, label, require):
    """Ссылка вида kind:id; kind — module|subsystem|check."""
    _str(value, label, require)
    kind, sep, ident = value.partition(":")
    require(sep and kind in REF_KINDS,
            f"{label}: ссылка должна иметь вид module:|subsystem:|check:, "
            f"получено {value!r}")
    _str(ident, f"{label} id", require, ident=True)
    return kind, ident


def _unique_ids(items, label, require):
    ids = set()
    for item in items:
        ident = item["id"]
        require(ident not in ids, f"{label}: повторный ID {ident!r}")
        ids.add(ident)
    return ids


def _validate_basis(value, label, require):
    _unknown(value, BASIS_FIELDS, label, require)
    for field in ("who", "at", "source", "detail"):
        require(field in value, f"{label}: отсутствует поле '{field}'")
    _str(value["who"], f"{label}.who", require)
    _iso(value["at"], f"{label}.at", require)
    _str(value["source"], f"{label}.source", require)
    _str(value["detail"], f"{label}.detail", require)


def _validate_inputs(value, label, require):
    require(isinstance(value, list), f"{label} должен быть списком")
    for idx, entry in enumerate(value):
        item_label = f"{label}[{idx}]"
        if isinstance(entry, str):
            _path(entry, item_label, require)
            continue
        _unknown(entry, INPUT_FIELDS, item_label, require)
        kind = entry.get("type")
        require(kind in INPUT_TYPES,
                f"{item_label}: неизвестный тип входа {kind!r}")
        if kind in {"file", "directory", "path"}:
            _path(entry.get("path"), f"{item_label}.path", require)
        elif kind == "environment":
            names = entry.get("names")
            require(isinstance(names, list) and names
                    and all(isinstance(n, str) and n.strip() for n in names),
                    f"{item_label}.names должен быть непустым списком строк")


def _validate_applicability(value, label, require):
    if isinstance(value, str):
        require(value in {"always", "checkpoint"},
                f"{label}: строковая применимость — 'always' или 'checkpoint'")
        return
    _unknown(value, APPLICABILITY_FIELDS, label, require)
    kind = value.get("kind")
    require(kind in APPLICABILITY_KINDS,
            f"{label}: неизвестный kind {kind!r}")
    if kind == "when_paths":
        paths = value.get("paths")
        require(isinstance(paths, list) and paths,
                f"{label}.paths должен быть непустым списком")
        for path in paths:
            _path(path, f"{label}.paths", require, pattern=True)
    elif kind == "checkpoint":
        _str(value.get("checkpoint"), f"{label}.checkpoint", require)


def _validate_edge(edge, label, require):
    _unknown(edge, EDGE_FIELDS, label, require)
    for field in ("consumer", "dependency", "kind", "source"):
        require(field in edge, f"{label}: отсутствует поле '{field}'")
    _ref(edge["consumer"], f"{label}.consumer", require)
    _ref(edge["dependency"], f"{label}.dependency", require)
    require(edge["kind"] in DEPENDENCY_KINDS,
            f"{label}: неизвестный kind {edge['kind']!r}")
    require(edge["source"] in DEPENDENCY_SOURCES,
            f"{label}: неизвестный source {edge['source']!r}")
    if edge["source"] == "analyzer":
        _str(edge.get("analyzer_version"), f"{label}.analyzer_version", require)
    elif "analyzer_version" in edge:
        _str(edge["analyzer_version"], f"{label}.analyzer_version", require)
    if "evidence" in edge:
        _str(edge["evidence"], f"{label}.evidence", require)


def _literal_head(pattern):
    """Литеральный префикс glob до первого метасимвола."""
    head = []
    for part in pattern.split("/"):
        if any(c in part for c in "*?["):
            break
        head.append(part)
    return "/".join(head)


def _match(path, pattern):
    """Сегментный glob: ``*`` не пересекает ``/``, ``**`` — ноль и более."""
    path_parts, pats = path.split("/"), pattern.split("/")

    def walk(i, j):
        if j == len(pats):
            return i == len(path_parts)
        if pats[j] == "**":
            return (walk(i, j + 1)
                    or (i < len(path_parts) and walk(i + 1, j)))
        return (i < len(path_parts)
                and fnmatch.fnmatchcase(path_parts[i], pats[j])
                and walk(i + 1, j + 1))
    return walk(0, 0)


def _paths_conflict(a, b):
    """Один путь является другим или лежит внутри другого."""
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _exclude_broad(pattern):
    """Исключение покрывает всё под своей литеральной областью.

    Чистый литерал либо последний сегмент ``**``/``*`` — широкий охват;
    селективный glob с литеральным именем на конце — нет.
    """
    head = _literal_head(pattern)
    if pattern == head:
        return True
    return pattern.split("/")[-1] in {"**", "*"}


def _exclude_conflicts(exclude_head, exclude_pattern, declared):
    """Коллизия исключения с объявленным входом (R-07).

    Glob, совпадающий с явным литеральным входом, — ошибка при любой
    селективности: пересечение доказуемо статически. Незаякоренный
    glob (пустой литеральный префикс) наблюдает корень проекта:
    любой объявленный путь находится внутри его области. Широкое
    исключение, накрывающее объявленную область, либо любое
    исключение внутри неё — ошибка. Селективный glob внутри общей
    области модуля допускается как явно заявленное точечное
    исключение; совпадение с конкретным входом этим не защищено.
    """
    declared_head = _literal_head(declared)
    if declared == declared_head and _match(declared_head,
                                            exclude_pattern):
        return True
    if (exclude_head == "" or exclude_head == declared_head
            or declared_head.startswith(exclude_head + "/")):
        return _exclude_broad(exclude_pattern)
    return exclude_head.startswith(declared_head + "/")


def validate_policy(policy, require):
    """Валидировать checks-policy.json схемы 1; возвратить policy."""
    _unknown(policy, POLICY_FIELDS, "policy", require)
    require(type(policy.get("schema_version")) is int
            and policy["schema_version"] == POLICY_SCHEMA_VERSION,
            f"policy: неподдерживаемая schema_version "
            f"{policy.get('schema_version')!r}, требуется {POLICY_SCHEMA_VERSION}")
    for field in ("policy_id", "revision", "project_id", "trust_mode",
                  "approved_basis", "modules", "subsystems", "checks",
                  "dependencies", "excludes", "environment", "fallback",
                  "test_catalog"):
        require(field in policy, f"policy: отсутствует поле '{field}'")
    _str(policy["policy_id"], "policy.policy_id", require, slug=True)
    _int(policy["revision"], "policy.revision", require, minimum=1)
    _str(policy["project_id"], "policy.project_id", require)
    require(policy["trust_mode"] in TRUST_MODES,
            f"policy.trust_mode должен быть local|protected, "
            f"получено {policy['trust_mode']!r}")
    _validate_basis(policy["approved_basis"], "policy.approved_basis", require)

    subsystems = policy["subsystems"]
    require(isinstance(subsystems, list) and subsystems,
            "policy.subsystems должен быть непустым списком")
    subsystem_ids = set()
    for idx, item in enumerate(subsystems):
        label = f"policy.subsystems[{idx}]"
        _unknown(item, SUBSYSTEM_FIELDS, label, require)
        for field in ("id", "title"):
            require(field in item, f"{label}: отсутствует поле '{field}'")
        _str(item["id"], f"{label}.id", require, ident=True)
        _str(item["title"], f"{label}.title", require)
        subsystem_ids.add(item["id"])
    require(len(subsystem_ids) == len(subsystems),
            "policy.subsystems: повторный ID")

    modules = policy["modules"]
    require(isinstance(modules, list) and modules,
            "policy.modules должен быть непустым списком")
    module_ids = set()
    path_owners = {}
    for idx, item in enumerate(modules):
        label = f"policy.modules[{idx}]"
        _unknown(item, MODULE_FIELDS, label, require)
        for field in ("id", "title", "subsystem_id", "paths"):
            require(field in item, f"{label}: отсутствует поле '{field}'")
        _str(item["id"], f"{label}.id", require, ident=True)
        _str(item["title"], f"{label}.title", require)
        require(item["subsystem_id"] in subsystem_ids,
                f"{label}: неизвестный subsystem_id {item['subsystem_id']!r}")
        paths = item["paths"]
        require(isinstance(paths, list) and paths,
                f"{label}.paths должен быть непустым списком")
        for path in paths:
            _path(path, f"{label}.paths", require, pattern=True)
        shared = item.get("shared", False)
        require(isinstance(shared, bool), f"{label}.shared должен быть boolean")
        for path in paths:
            head = _literal_head(path) or path
            for other, owner in path_owners.items():
                if _paths_conflict(head, other):
                    require(shared and owner["shared"],
                            f"{label}: путь {path!r} пересекается с "
                            f"{owner['path']!r} модуля {owner['id']!r} "
                            f"без общего владения (shared)")
            path_owners[head] = {"id": item["id"], "path": path,
                                 "shared": shared}
        module_ids.add(item["id"])
    require(len(module_ids) == len(modules), "policy.modules: повторный ID")

    declared_refs = {("module", m) for m in module_ids}
    declared_refs |= {("subsystem", s) for s in subsystem_ids}

    declared_inputs = []
    checks = policy["checks"]
    require(isinstance(checks, list) and checks,
            "policy.checks должен быть непустым списком")
    check_ids = set()
    for idx, item in enumerate(checks):
        label = f"policy.checks[{idx}]"
        _unknown(item, CHECK_FIELDS, label, require)
        for field in ("id", "title", "purpose", "kind"):
            require(field in item, f"{label}: отсутствует поле '{field}'")
        _str(item["id"], f"{label}.id", require, ident=True)
        _str(item["title"], f"{label}.title", require)
        _str(item["purpose"], f"{label}.purpose", require)
        kind = item["kind"]
        require(kind in CHECK_KINDS,
                f"{label}: kind должен быть test|command|manual, "
                f"получено {kind!r}")
        mods = item.get("modules", [])
        require(isinstance(mods, list)
                and all(m in module_ids for m in mods),
                f"{label}.modules: неизвестный модуль или не список")
        if kind == "test":
            require(item.get("adapter") == BUILTIN_ADAPTER,
                    f"{label}: kind=test требует adapter {BUILTIN_ADAPTER!r}, "
                    f"получено {item.get('adapter')!r}")
        else:
            require("adapter" not in item,
                    f"{label}: adapter не допускается для kind={kind}")
        if kind in {"test", "command"}:
            argv = item.get("argv")
            require(isinstance(argv, list) and argv
                    and all(isinstance(a, str) and "\0" not in a for a in argv)
                    and argv[0].strip(),
                    f"{label}: kind={kind} требует непустой argv")
            _num(item.get("timeout_seconds"),
                 f"{label}.timeout_seconds", require)
            require("scenario" not in item,
                    f"{label}: scenario не допускается для kind={kind}")
        else:
            _str(item.get("scenario"), f"{label}.scenario", require)
            require("argv" not in item and "timeout_seconds" not in item,
                    f"{label}: argv/timeout не допускаются для kind=manual")
        if "inputs" in item:
            _validate_inputs(item["inputs"], f"{label}.inputs", require)
            for entry in item["inputs"]:
                raw = entry if isinstance(entry, str) else entry.get("path")
                if raw:
                    declared_inputs.append((raw, item["id"]))
        if "applicability" in item:
            _validate_applicability(item["applicability"],
                                    f"{label}.applicability", require)
        if "required_environment" in item:
            require(isinstance(item["required_environment"], list)
                    and all(isinstance(x, str) and x.strip()
                            for x in item["required_environment"]),
                    f"{label}.required_environment должен быть списком строк")
        check_ids.add(item["id"])
    require(len(check_ids) == len(checks), "policy.checks: повторный ID")
    declared_refs |= {("check", c) for c in check_ids}

    dependencies = policy["dependencies"]
    require(isinstance(dependencies, list),
            "policy.dependencies должен быть списком")
    edge_keys = set()
    for idx, edge in enumerate(dependencies):
        label = f"policy.dependencies[{idx}]"
        _validate_edge(edge, label, require)
        consumer = _ref(edge["consumer"], f"{label}.consumer", require)
        dependency = _ref(edge["dependency"], f"{label}.dependency", require)
        require(consumer in declared_refs,
                f"{label}.consumer: неизвестная ссылка {edge['consumer']!r}")
        require(dependency in declared_refs,
                f"{label}.dependency: неизвестная ссылка {edge['dependency']!r}")
        require(consumer != dependency,
                f"{label}: самоссылка {edge['consumer']!r}")
        key = (edge["consumer"], edge["dependency"], edge["kind"])
        require(key not in edge_keys,
                f"{label}: повторное ребро {key!r}")
        edge_keys.add(key)

    excludes = policy["excludes"]
    require(isinstance(excludes, list),
            "policy.excludes должен быть списком")
    exclude_heads = []
    for idx, item in enumerate(excludes):
        label = f"policy.excludes[{idx}]"
        _unknown(item, EXCLUDE_FIELDS, label, require)
        for field in ("pattern", "reason", "origin"):
            require(field in item, f"{label}: отсутствует поле '{field}'")
        _path(item["pattern"], f"{label}.pattern", require, pattern=True)
        _str(item["reason"], f"{label}.reason", require)
        require(item["origin"] in EXCLUDE_ORIGINS,
                f"{label}: неизвестный origin {item['origin']!r}")
        # Пустой head значим: незаякоренный glob наблюдает корень.
        exclude_heads.append((_literal_head(item["pattern"]),
                              item["pattern"]))
    for head, pattern in exclude_heads:
        for owner in path_owners.values():
            if _exclude_conflicts(head, pattern, owner["path"]):
                require(False, f"policy.excludes: исключение {pattern!r} "
                        f"конфликтует с объявленным путём {owner['path']!r} "
                        f"модуля {owner['id']!r}")
        for raw, check_id in declared_inputs:
            if _exclude_conflicts(head, pattern, raw):
                require(False, f"policy.excludes: исключение {pattern!r} "
                        f"конфликтует с объявленным входом {raw!r} "
                        f"проверки {check_id!r}")

    env = policy["environment"]
    _unknown(env, ENVIRONMENT_FIELDS, "policy.environment", require)
    toolchain = env.get("toolchain", [])
    require(isinstance(toolchain, list),
            "policy.environment.toolchain должен быть списком")
    tool_ids = set()
    for idx, item in enumerate(toolchain):
        label = f"policy.environment.toolchain[{idx}]"
        _unknown(item, TOOLCHAIN_FIELDS, label, require)
        _str(item.get("id"), f"{label}.id", require)
        require(item["id"] not in tool_ids,
                f"{label}: повторный ID {item['id']!r}")
        tool_ids.add(item["id"])
        if "version" in item:
            _str(item["version"], f"{label}.version", require)
    external = env.get("external", [])
    require(isinstance(external, list),
            "policy.environment.external должен быть списком")
    ext_ids = set()
    for idx, item in enumerate(external):
        label = f"policy.environment.external[{idx}]"
        _unknown(item, EXTERNAL_FIELDS, label, require)
        _str(item.get("id"), f"{label}.id", require)
        require(item["id"] not in ext_ids,
                f"{label}: повторный ID {item['id']!r}")
        ext_ids.add(item["id"])
        freshness = item.get("freshness")
        _unknown(freshness, FRESHNESS_FIELDS, f"{label}.freshness", require)
        kind = freshness.get("kind")
        require(kind in FRESHNESS_KINDS,
                f"{label}.freshness.kind должен быть "
                f"version|ttl|always_run, получено {kind!r}")
        if kind == "ttl":
            _num(freshness.get("ttl_seconds"),
                 f"{label}.freshness.ttl_seconds", require)
            require("version" not in freshness,
                    f"{label}.freshness: version не допускается для ttl")
        elif kind == "version":
            _str(freshness.get("version"),
                 f"{label}.freshness.version", require)
            require("ttl_seconds" not in freshness,
                    f"{label}.freshness: ttl_seconds не допускается для version")
        else:
            require("version" not in freshness
                    and "ttl_seconds" not in freshness,
                    f"{label}.freshness: для always_run поля version/"
                    f"ttl_seconds не допускаются")

    fallback = policy["fallback"]
    _unknown(fallback, FALLBACK_FIELDS, "policy.fallback", require)
    for field in ("expansion_order", "limits", "unknown_scope"):
        require(field in fallback, f"policy.fallback: отсутствует '{field}'")
    require(fallback["expansion_order"] == EXPANSION_ORDER,
            f"policy.fallback.expansion_order должен быть "
            f"{EXPANSION_ORDER!r}")
    require(fallback["unknown_scope"] in UNKNOWN_SCOPES,
            f"policy.fallback.unknown_scope должен быть "
            f"module|subsystem|project")
    commands = fallback.get("approved_commands", [])
    require(isinstance(commands, list),
            "policy.fallback.approved_commands должен быть списком")
    for idx, argv in enumerate(commands):
        require(isinstance(argv, list) and argv
                and all(isinstance(a, str) and "\0" not in a for a in argv)
                and argv[0].strip(),
                f"policy.fallback.approved_commands[{idx}]: некорректный argv")
    limits = fallback["limits"]
    _unknown(limits, LIMIT_FIELDS, "policy.fallback.limits", require)
    _num(limits.get("max_seconds"),
         "policy.fallback.limits.max_seconds", require)
    if "max_bytes" in limits:
        _int(limits["max_bytes"], "policy.fallback.limits.max_bytes",
             require, minimum=1)

    catalog = policy["test_catalog"]
    _unknown(catalog, CATALOG_FIELDS, "policy.test_catalog", require)
    require(catalog.get("adapter") == BUILTIN_ADAPTER,
            f"policy.test_catalog.adapter должен быть {BUILTIN_ADAPTER!r}")
    _digest(catalog.get("fingerprint"),
            "policy.test_catalog.fingerprint", require)
    entries = catalog.get("entries")
    require(isinstance(entries, list) and entries,
            "policy.test_catalog.entries должен быть непустым списком")
    test_ids = set()
    for idx, item in enumerate(entries):
        label = f"policy.test_catalog.entries[{idx}]"
        _unknown(item, CATALOG_ENTRY_FIELDS, label, require)
        for field in ("id", "description"):
            require(field in item, f"{label}: отсутствует поле '{field}'")
        _str(item["id"], f"{label}.id", require)
        _str(item["description"], f"{label}.description", require)
        require(item["id"] not in test_ids,
                f"{label}: повторный test_id {item['id']!r}")
        test_ids.add(item["id"])
        if "group" in item:
            _str(item["group"], f"{label}.group", require)
        # Допуски skipped и expected_failure задаются раздельно:
        # unexpected_success допуска не имеет.
        for rule in ("skip", "expected_failure"):
            if rule in item:
                _unknown(item[rule], ALLOW_FIELDS, f"{label}.{rule}",
                         require)
                require(isinstance(item[rule].get("allowed"), bool),
                        f"{label}.{rule}.allowed должен быть boolean")
                if item[rule]["allowed"]:
                    _str(item[rule].get("reason"),
                         f"{label}.{rule}.reason", require)
    return policy


def validate_graph(graph, require, known_refs=None):
    """Валидировать снимок графа схемы 1; known_refs — множество (kind, id)."""
    _unknown(graph, GRAPH_FIELDS, "graph", require)
    require(type(graph.get("schema_version")) is int
            and graph["schema_version"] == GRAPH_SCHEMA_VERSION,
            f"graph: неподдерживаемая schema_version "
            f"{graph.get('schema_version')!r}, требуется {GRAPH_SCHEMA_VERSION}")
    for field in ("project_id", "policy_digest", "created_at",
                  "analyzer_version", "edges", "unknown"):
        require(field in graph, f"graph: отсутствует поле '{field}'")
    _str(graph["project_id"], "graph.project_id", require)
    _digest(graph["policy_digest"], "graph.policy_digest", require)
    _iso(graph["created_at"], "graph.created_at", require)
    _str(graph["analyzer_version"], "graph.analyzer_version", require)
    edges = graph["edges"]
    require(isinstance(edges, list), "graph.edges должен быть списком")
    edge_keys = set()
    for idx, edge in enumerate(edges):
        label = f"graph.edges[{idx}]"
        _validate_edge(edge, label, require)
        consumer = _ref(edge["consumer"], f"{label}.consumer", require)
        dependency = _ref(edge["dependency"], f"{label}.dependency", require)
        require(consumer != dependency, f"{label}: самоссылка")
        if known_refs is not None:
            require(consumer in known_refs,
                    f"{label}.consumer: неизвестная ссылка "
                    f"{edge['consumer']!r}")
            require(dependency in known_refs,
                    f"{label}.dependency: неизвестная ссылка "
                    f"{edge['dependency']!r}")
        key = (edge["consumer"], edge["dependency"], edge["kind"])
        require(key not in edge_keys, f"{label}: повторное ребро {key!r}")
        edge_keys.add(key)
    unknown = graph["unknown"]
    require(isinstance(unknown, list), "graph.unknown должен быть списком")
    for idx, item in enumerate(unknown):
        label = f"graph.unknown[{idx}]"
        _unknown(item, UNKNOWN_FIELDS, label, require)
        scope = item.get("scope")
        _unknown(scope, SCOPE_FIELDS, f"{label}.scope", require)
        kind = scope.get("kind")
        require(kind in UNKNOWN_SCOPES,
                f"{label}.scope.kind должен быть module|subsystem|project")
        if kind == "project":
            require("id" not in scope,
                    f"{label}.scope: id не допускается для project")
        else:
            _str(scope.get("id"), f"{label}.scope.id", require, ident=True)
        _str(item.get("reason"), f"{label}.reason", require)
    return graph


def _validate_tests(tests, label, require):
    _unknown(tests, TESTS_FIELDS, label, require)
    expected = tests.get("expected")
    require(isinstance(expected, list)
            and all(isinstance(t, str) and t.strip() for t in expected),
            f"{label}.expected должен быть списком test_id")
    require(len(set(expected)) == len(expected),
            f"{label}.expected: повторный test_id")
    actual = tests.get("actual")
    require(isinstance(actual, list),
            f"{label}.actual должен быть списком")
    actual_ids = set()
    for idx, item in enumerate(actual):
        item_label = f"{label}.actual[{idx}]"
        _unknown(item, ACTUAL_FIELDS, item_label, require)
        _str(item.get("id"), f"{item_label}.id", require)
        require(item["id"] not in actual_ids,
                f"{item_label}: повторный test_id {item['id']!r}")
        actual_ids.add(item["id"])
        outcome = item.get("outcome")
        require(outcome in TEST_OUTCOMES,
                f"{item_label}: неизвестный outcome {outcome!r}")
        if outcome == "skipped":
            _str(item.get("reason"), f"{item_label}.reason", require)


def validate_result(result, require):
    """Валидировать результат проверки схемы 1; возвратить result."""
    _unknown(result, RESULT_FIELDS, "result", require)
    require(type(result.get("schema_version")) is int
            and result["schema_version"] == RESULT_SCHEMA_VERSION,
            f"result: неподдерживаемая schema_version "
            f"{result.get('schema_version')!r}, требуется {RESULT_SCHEMA_VERSION}")
    for field in ("result_id", "project_id", "check_id", "kind",
                  "binding", "status", "started_at", "finished_at"):
        require(field in result, f"result: отсутствует поле '{field}'")
    _str(result["result_id"], "result.result_id", require)
    _str(result["project_id"], "result.project_id", require)
    _str(result["check_id"], "result.check_id", require, ident=True)
    kind = result["kind"]
    require(kind in CHECK_KINDS,
            f"result.kind должен быть test|command|manual, "
            f"получено {kind!r}")
    status = result["status"]
    require(status in RESULT_STATUSES,
            f"result: неизвестный status {status!r}")

    binding = result["binding"]
    _unknown(binding, BINDING_FIELDS, "result.binding", require)
    for field in ("policy_digest", "definition_digest", "adapter",
                  "runner", "input_digest", "dependency_closure_digest",
                  "environment"):
        require(field in binding, f"result.binding: отсутствует '{field}'")
    for field in ("policy_digest", "definition_digest", "input_digest",
                  "dependency_closure_digest"):
        _digest(binding[field], f"result.binding.{field}", require)
    if "task_requirements_digest" in binding:
        _digest(binding["task_requirements_digest"],
                "result.binding.task_requirements_digest", require)
    for side in ("adapter", "runner"):
        _unknown(binding[side], IDENTITY_FIELDS,
                 f"result.binding.{side}", require)
        _str(binding[side].get("id"), f"result.binding.{side}.id", require)
        _str(binding[side].get("version"),
             f"result.binding.{side}.version", require)
    require(isinstance(binding["environment"], dict)
            and not isinstance(binding["environment"], bool),
            "result.binding.environment должен быть объектом")

    _iso(result["started_at"], "result.started_at", require)
    _iso(result["finished_at"], "result.finished_at", require)
    if "duration_seconds" in result:
        _num(result["duration_seconds"], "result.duration_seconds",
             require, positive=False)
    if "reused" in result:
        reused = result["reused"]
        _unknown(reused, REUSED_FIELDS, "result.reused", require)
        _str(reused.get("result_id"), "result.reused.result_id", require)
        if "run_id" in reused:
            _str(reused["run_id"], "result.reused.run_id", require)
        if "issued_at" in reused:
            _iso(reused["issued_at"], "result.reused.issued_at", require)
        if "input_digest" in reused:
            _digest(reused["input_digest"],
                    "result.reused.input_digest", require)

    if status in {"passed", "failed", "timeout"}:
        if kind == "test":
            _validate_tests(result.get("tests"), "result.tests", require)
            require("command" not in result and "manual" not in result,
                    "result: command/manual не допускаются для kind=test")
        elif kind == "command":
            command = result.get("command")
            _unknown(command, COMMAND_RESULT_FIELDS, "result.command",
                     require)
            require(isinstance(command.get("exit_code"), int)
                    and not isinstance(command.get("exit_code"), bool),
                    "result.command.exit_code должен быть целым числом")
            _str(command.get("stdout_log"),
                 "result.command.stdout_log", require)
            _str(command.get("stderr_log"),
                 "result.command.stderr_log", require)
            require("tests" not in result and "manual" not in result,
                    "result: tests/manual не допускаются для kind=command")
        else:
            manual = result.get("manual")
            _unknown(manual, MANUAL_FIELDS, "result.manual", require)
            _str(manual.get("observation"),
                 "result.manual.observation", require)
            _digest(manual.get("scenario_digest"),
                    "result.manual.scenario_digest", require)
            confirmation = manual.get("confirmation")
            _unknown(confirmation, CONFIRMATION_FIELDS,
                     "result.manual.confirmation", require)
            _str(confirmation.get("who"),
                 "result.manual.confirmation.who", require)
            _iso(confirmation.get("at"),
                 "result.manual.confirmation.at", require)
            _str(confirmation.get("statement"),
                 "result.manual.confirmation.statement", require)
            require("tests" not in result and "command" not in result,
                    "result: tests/command не допускаются для kind=manual")
    if status in {"blocked", "not_applicable"}:
        _str(result.get("reason"), "result.reason", require)
    if "evidence" in result:
        require(isinstance(result["evidence"], dict)
                and not isinstance(result["evidence"], bool),
                "result.evidence должен быть объектом")
    return result


def validate_receipt(receipt, require):
    """Валидировать формат квитанции схемы 1 (без проверки подписи)."""
    _unknown(receipt, RECEIPT_FIELDS, "receipt", require)
    require(type(receipt.get("schema_version")) is int
            and receipt["schema_version"] == RECEIPT_SCHEMA_VERSION,
            f"receipt: неподдерживаемая schema_version "
            f"{receipt.get('schema_version')!r}, требуется {RECEIPT_SCHEMA_VERSION}")
    for field in ("receipt_id", "project_id", "candidate_digest",
                  "policy_digest", "policy_revision", "engine", "issuer",
                  "issued_at", "subject", "signature"):
        require(field in receipt, f"receipt: отсутствует поле '{field}'")
    _str(receipt["receipt_id"], "receipt.receipt_id", require)
    _str(receipt["project_id"], "receipt.project_id", require)
    _digest(receipt["candidate_digest"], "receipt.candidate_digest", require)
    _digest(receipt["policy_digest"], "receipt.policy_digest", require)
    _int(receipt["policy_revision"], "receipt.policy_revision",
         require, minimum=1)
    _unknown(receipt["engine"], IDENTITY_FIELDS, "receipt.engine", require)
    _str(receipt["engine"].get("id"), "receipt.engine.id", require)
    _str(receipt["engine"].get("version"), "receipt.engine.version", require)
    _str(receipt["issuer"], "receipt.issuer", require)
    _iso(receipt["issued_at"], "receipt.issued_at", require)

    subject = receipt["subject"]
    _unknown(subject, SUBJECT_FIELDS, "receipt.subject", require)
    kind = subject.get("kind")
    require(kind in RECEIPT_KINDS,
            f"receipt.subject.kind должен быть run|aggregate")
    results = subject.get("results")
    require(isinstance(results, list) and results,
            "receipt.subject.results должен быть непустым списком")
    seen = set()
    for idx, item in enumerate(results):
        label = f"receipt.subject.results[{idx}]"
        _unknown(item, SUBJECT_RESULT_FIELDS, label, require)
        _str(item.get("check_id"), f"{label}.check_id", require, ident=True)
        _digest(item.get("result_digest"), f"{label}.result_digest", require)
        require(item.get("status") in RESULT_STATUSES,
                f"{label}: неизвестный status {item.get('status')!r}")
        key = item["check_id"]
        require(key not in seen, f"{label}: повторный check_id {key!r}")
        seen.add(key)
    if kind == "aggregate":
        sources = subject.get("source_receipts")
        require(isinstance(sources, list) and sources
                and all(isinstance(s, str) and s.strip() for s in sources),
                "receipt.subject.source_receipts для aggregate должен "
                "быть непустым списком receipt_id")
    else:
        require("source_receipts" not in subject,
                "receipt.subject: source_receipts только для aggregate")

    signature = receipt["signature"]
    _unknown(signature, SIGNATURE_FIELDS, "receipt.signature", require)
    require(signature.get("algorithm") in SIGNATURE_ALGORITHMS,
            f"receipt.signature.algorithm должен быть "
            f"ed25519|hmac-sha256")
    _str(signature.get("key_id"), "receipt.signature.key_id", require)
    _str(signature.get("value"), "receipt.signature.value", require)
    return receipt


def _change(classification, kind, ref, detail):
    return {"classification": classification, "kind": kind,
            "ref": ref, "detail": detail}


def _by_id(items):
    return {item["id"]: item for item in items}


def _classify_entries(old, new, label, changes, *,
                      extension_detail, weakening_detail):
    old_map, new_map = _by_id(old), _by_id(new)
    for ident in sorted(set(new_map) - set(old_map)):
        changes.append(_change("extension", label, ident, extension_detail))
    for ident in sorted(set(old_map) - set(new_map)):
        changes.append(_change("weakening", label, ident, weakening_detail))
    return old_map, new_map


def classify_policy_change(old, new):
    """Классифицировать правку одобренной политики old → new.

    Возвращает ``{verdict, changes}``: verdict — ``weakening`` при любом
    ослаблении, иначе ``extension`` при расширениях, иначе ``unchanged``.
    Identity (policy_id/project_id) неизменна: смена — ошибка. Поля
    revision и approved_basis не участвуют в классификации: новое
    ослабление требует нового решения инженера независимо от старого
    одобрения. Локальное поле ``approved`` авторизацией не является и в
    схеме отсутствует.
    """
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise ValueError("classify_policy_change: ожидаются объекты policy")
    for field in ("policy_id", "project_id"):
        if old.get(field) != new.get(field):
            raise ValueError(f"classify_policy_change: изменение {field} "
                             f"не является правкой политики")
    changes = []

    if old.get("trust_mode") != new.get("trust_mode"):
        changes.append(_change("weakening", "trust_mode", "trust_mode",
                               f"{old.get('trust_mode')} → "
                               f"{new.get('trust_mode')}"))

    # Модули/подсистемы/проверки/зависимости/исключения.
    _classify_entries(old.get("modules", []), new.get("modules", []),
                      "modules", changes,
                      extension_detail="добавлен модуль",
                      weakening_detail="удалён модуль")
    _classify_entries(old.get("subsystems", []), new.get("subsystems", []),
                      "subsystems", changes,
                      extension_detail="добавлена подсистема",
                      weakening_detail="удалена подсистема")
    for module in _by_id(new.get("modules", [])).values():
        prev = _by_id(old.get("modules", [])).get(module["id"])
        if prev is None:
            continue
        if set(prev.get("paths", [])) != set(module.get("paths", [])):
            added = set(module["paths"]) - set(prev.get("paths", []))
            removed = set(prev.get("paths", [])) - set(module["paths"])
            if removed:
                changes.append(_change(
                    "weakening", "modules", module["id"],
                    f"сокращены пути: {sorted(removed)}"))
            if added:
                changes.append(_change(
                    "extension", "modules", module["id"],
                    f"расширены пути: {sorted(added)}"))
        if prev.get("subsystem_id") != module.get("subsystem_id"):
            changes.append(_change(
                "weakening", "modules", module["id"],
                "изменена граница подсистемы"))

    old_checks = _by_id(old.get("checks", []))
    new_checks = _by_id(new.get("checks", []))
    # Исполняемый allowlist: argv существующих проверок и approved_commands.
    approved_argv = {tuple(c.get("argv") or ())
                     for c in old.get("checks", [])}
    approved_argv |= {tuple(a) for a in
                      old.get("fallback", {}).get("approved_commands", [])}
    for ident in sorted(set(new_checks) - set(old_checks)):
        check = new_checks[ident]
        argv = tuple(check.get("argv") or ())
        if (check.get("kind") in {"test", "command"}
                and argv and argv not in approved_argv):
            changes.append(_change(
                "weakening", "checks", ident,
                "новая проверка расширяет исполняемый allowlist"))
        else:
            changes.append(_change("extension", "checks", ident,
                                   "добавлена проверка"))
    for ident in sorted(set(old_checks) - set(new_checks)):
        changes.append(_change("weakening", "checks", ident,
                               "удалена проверка"))
    for ident, check in sorted(new_checks.items()):
        prev = old_checks.get(ident)
        if prev is None:
            continue
        for field, detail in (("kind", "изменён kind"),
                              ("adapter", "изменён adapter"),
                              ("argv", "изменён одобренный argv"),
                              ("scenario", "изменён scenario")):
            if prev.get(field) != check.get(field):
                changes.append(_change("weakening", "checks", ident, detail))
        old_timeout = prev.get("timeout_seconds")
        new_timeout = check.get("timeout_seconds")
        if old_timeout != new_timeout:
            if old_timeout is None or new_timeout is None:
                changes.append(_change("weakening", "checks", ident,
                                       "изменён лимит времени"))
            elif new_timeout > old_timeout:
                changes.append(_change("weakening", "checks", ident,
                                       f"лимит {old_timeout} → {new_timeout}"))
            else:
                changes.append(_change("extension", "checks", ident,
                                       f"лимит ужесточён {old_timeout} → "
                                       f"{new_timeout}"))
        for field, added_detail, removed_detail in (
                ("modules", "расширены связанные модули",
                 "сокращены связанные модули"),
                ("inputs", "добавлены входы",
                 "удалены входы"),
                ("required_environment", "добавлены требования среды",
                 "удалены требования среды")):
            if prev.get(field) != check.get(field):
                old_set = prev.get(field) or []
                new_set = check.get(field) or []
                old_keys = {repr(v) for v in old_set}
                new_keys = {repr(v) for v in new_set}
                if old_keys - new_keys:
                    changes.append(_change("weakening", "checks", ident,
                                           removed_detail))
                if new_keys - old_keys:
                    changes.append(_change("extension", "checks", ident,
                                           added_detail))
        if prev.get("applicability") != check.get("applicability"):
            changes.append(_change("weakening", "checks", ident,
                                   "изменена применимость"))
        if prev.get("purpose") != check.get("purpose"):
            changes.append(_change("weakening", "checks", ident,
                                   "изменено назначение проверки"))

    dep_old = {(e["consumer"], e["dependency"], e["kind"])
               for e in old.get("dependencies", [])}
    dep_new = {(e["consumer"], e["dependency"], e["kind"])
               for e in new.get("dependencies", [])}
    for edge in sorted(dep_new - dep_old):
        changes.append(_change("extension", "dependencies", repr(edge),
                               "добавлена связь"))
    for edge in sorted(dep_old - dep_new):
        changes.append(_change("weakening", "dependencies", repr(edge),
                               "удалена значимая связь"))

    ex_old = {e["pattern"] for e in old.get("excludes", [])}
    ex_new = {e["pattern"] for e in new.get("excludes", [])}
    for pattern in sorted(ex_old - ex_new):
        changes.append(_change("extension", "excludes", pattern,
                               "исключение снято — область наблюдения шире"))
    for pattern in sorted(ex_new - ex_old):
        changes.append(_change("weakening", "excludes", pattern,
                               "новое исключение — область наблюдения уже"))

    env_old, env_new = old.get("environment", {}), new.get("environment", {})
    tool_old = {t["id"]: t for t in env_old.get("toolchain", [])}
    tool_new = {t["id"]: t for t in env_new.get("toolchain", [])}
    for ident in sorted(set(tool_new) - set(tool_old)):
        changes.append(_change("extension", "environment", ident,
                               "добавлена зависимость toolchain"))
    for ident in sorted(set(tool_old) - set(tool_new)):
        changes.append(_change("weakening", "environment", ident,
                               "удалена зависимость toolchain"))
    for ident in sorted(set(tool_old) & set(tool_new)):
        if tool_old[ident] != tool_new[ident]:
            changes.append(_change("weakening", "environment", ident,
                                   "изменена идентичность toolchain"))
    ext_old = {e["id"]: e for e in env_old.get("external", [])}
    ext_new = {e["id"]: e for e in env_new.get("external", [])}
    for ident in sorted(set(ext_new) - set(ext_old)):
        changes.append(_change("extension", "environment", ident,
                               "добавлена внешняя зависимость"))
    for ident in sorted(set(ext_old) - set(ext_new)):
        changes.append(_change("weakening", "environment", ident,
                               "удалена внешняя зависимость"))
    for ident in sorted(set(ext_old) & set(ext_new)):
        if ext_old[ident].get("freshness") != ext_new[ident].get("freshness"):
            changes.append(_change("weakening", "environment", ident,
                                   "изменена политика свежести"))

    fb_old, fb_new = old.get("fallback", {}), new.get("fallback", {})
    if fb_old.get("expansion_order") != fb_new.get("expansion_order"):
        changes.append(_change("weakening", "fallback", "expansion_order",
                               "изменён порядок расширения"))
    if fb_old.get("unknown_scope") != fb_new.get("unknown_scope"):
        order = EXPANSION_ORDER
        old_i = order.index(fb_old.get("unknown_scope")) \
            if fb_old.get("unknown_scope") in order else -1
        new_i = order.index(fb_new.get("unknown_scope")) \
            if fb_new.get("unknown_scope") in order else -1
        cls = "extension" if new_i > old_i else "weakening"
        changes.append(_change(cls, "fallback", "unknown_scope",
                               f"{fb_old.get('unknown_scope')} → "
                               f"{fb_new.get('unknown_scope')}"))
    lim_old, lim_new = fb_old.get("limits", {}), fb_new.get("limits", {})
    for field in ("max_seconds", "max_bytes"):
        if lim_old.get(field) != lim_new.get(field):
            old_v, new_v = lim_old.get(field), lim_new.get(field)
            if old_v is None or new_v is None:
                cls = "weakening" if new_v is None else "extension"
            else:
                cls = "weakening" if new_v > old_v else "extension"
            changes.append(_change(cls, "fallback", field,
                                   f"{old_v} → {new_v}"))
    cmd_old = {tuple(a) for a in fb_old.get("approved_commands", [])}
    cmd_new = {tuple(a) for a in fb_new.get("approved_commands", [])}
    for argv in sorted(cmd_new - cmd_old):
        changes.append(_change("weakening", "fallback", repr(list(argv)),
                               "расширен список одобренных команд"))
    for argv in sorted(cmd_old - cmd_new):
        changes.append(_change("extension", "fallback", repr(list(argv)),
                               "сокращён список одобренных команд"))

    cat_old = _by_id(old.get("test_catalog", {}).get("entries", []))
    cat_new = _by_id(new.get("test_catalog", {}).get("entries", []))
    if (old.get("test_catalog", {}).get("adapter")
            != new.get("test_catalog", {}).get("adapter")):
        changes.append(_change("weakening", "test_catalog", "adapter",
                               "изменён адаптер каталога"))
    for ident in sorted(set(cat_new) - set(cat_old)):
        entry = cat_new[ident]
        granted = [rule for rule in ("skip", "expected_failure")
                   if (entry.get(rule) or {}).get("allowed")]
        if granted:
            changes.append(_change(
                "weakening", "test_catalog", ident,
                f"новый тест сразу разрешает {'/'.join(granted)}"))
        else:
            changes.append(_change("extension", "test_catalog", ident,
                                   "добавлен тест"))
    for ident in sorted(set(cat_old) - set(cat_new)):
        changes.append(_change("weakening", "test_catalog", ident,
                               "удалён тест"))
    for ident in sorted(set(cat_old) & set(cat_new)):
        prev, curr = cat_old[ident], cat_new[ident]
        for rule in ("skip", "expected_failure"):
            old_allow = (prev.get(rule) or {}).get("allowed", False)
            new_allow = (curr.get(rule) or {}).get("allowed", False)
            if old_allow != new_allow:
                changes.append(_change(
                    "weakening" if new_allow else "extension",
                    "test_catalog", ident,
                    f"{rule} разрешён" if new_allow
                    else f"{rule} запрещён"))
        if prev.get("description") != curr.get("description"):
            changes.append(_change("weakening", "test_catalog", ident,
                                   "изменено описание теста"))

    verdict = "unchanged"
    if any(c["classification"] == "weakening" for c in changes):
        verdict = "weakening"
    elif changes:
        verdict = "extension"
    return {"verdict": verdict, "changes": changes}
