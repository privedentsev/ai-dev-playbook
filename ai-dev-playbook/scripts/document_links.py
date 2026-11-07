"""Единый разбор Markdown-ссылок для check и rewrite по карте переносов.

Поддерживаемый контракт первого выпуска (design §5):

- inline-ссылки ``[текст](цель)`` и изображения ``![alt](src)``;
  назначение — ``<цель с пробелами>`` либо bare без пробелов и
  несбалансированных скобок, с необязательным title в ``"…"``, ``'…'``
  или ``(…)``;
- reference definitions ``[label]: цель`` (отступ до трёх пробелов) и
  reference-ссылки ``[текст][label]`` / ``[label][]``; shortcut-ссылки
  ``[label]`` не являются отдельной целью — их адресат обновляется через
  определение, а при проверке неопределённый ``[label]`` считается текстом;
- подписи ссылок разбираются со сбалансированными скобками и
  экранированием (``[a [b] c](цель)``, ``[![img](i)](цель)``);
- назначение с пробелами, %-encoding, query и fragment сохраняется;
  при сборке токена кодируются ``#``, ``?``, ``%``, ``:`` первого
  сегмента и служебные символы — адресат после повторного разбора
  тождественен исходному файлу; чистый ``#fragment``/``?query`` без пути
  не привязан к файлу и не меняется при переносе того же документа;
- fenced-блоки, indented-блоки и code spans (включая многострочные)
  не разбираются и не переписываются.

Карта переносов — ``{старый resolved-путь: новый}`` для файлов и каталогов
целиком. Адресат ссылки разрешается относительно старого расположения
источника, отображается через карту (точное совпадение или самый длинный
перемещённый предок), затем пересчитывается относительно нового
расположения источника. Это покрывает входящие, исходящие, взаимные и
self-ссылки без последовательных замен строк.

Immutable-материалы не переписываются: :func:`check_text` для них
разрешает ссылку по исходному расположению файла и отображает адресат
через карту — исторический адрес не считается битым из-за смены каталога.

Неподдерживаемые конструкции — wikilinks ``[[цель]]`` и HTML-ссылки
``<a href>``/``<img src>`` (атрибут в кавычках или без) — не
переписываются. Ссылка затронута, если её относительный адресат меняется:
при переносе цели или самого источника; затронутая неподдерживаемая
ссылка — конфликт для ручного разбора, то же для ссылок из управляемого
ядра комплекта. Ссылки из файлов вне заявленной области сканирования не
считаются проверенными.
"""

import bisect
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

INDEX_START = "<!-- playbook-index:start -->"
INDEX_END = "<!-- playbook-index:end -->"

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
INLINE_CODE = re.compile(r"(`+)(?!`)[^\n]*?(?<!`)\1(?!`)")
INDENTED_CODE = re.compile(r"^(?: {4}|\t)")
_BLANK = re.compile(r"^[ \t\r\n]*$")
_STRUCTURAL = re.compile(r"^ {0,3}(?:#{1,6}(?:[ \t]|$)|>|[-*+][ \t]|\d+[.)][ \t])")
REFDEF = re.compile(r"^ {0,3}\[([^\]\n]+)\]:[ \t]*(<[^>\n]+>|[^\s]+)",
                    re.MULTILINE)
WIKILINK = re.compile(r"(!)?\[\[([^\]\n]+)\]\]")
HTML_LINK = re.compile(
    r"<(?:a|img)\b[^>]*?(?:href|src)\s*=\s*(?:[\"']([^\"'\n]+)[\"']|([^\s>]+))",
    re.IGNORECASE)


def closes_fence(line, fence):
    """Строка закрывает открытый fence: только последовательность нужного типа.

    Закрывающий fence не может иметь info-строки — последовательность
    с суффиксом внутри открытого блока не закрывает его.
    """
    marker = FENCE_CLOSE.match(line)
    return marker is not None and marker.group(1)[0] == fence[0] \
        and len(marker.group(1)) >= len(fence)


def _masked_text(text):
    """Текст с обнулённым кодом: fenced-блоки, indented code, code spans.

    Fenced-блоки маскируются по строкам; indented-блок — строка с отступом
    в четыре пробела или таб, не продолжающая абзац (приближение
    CommonMark: indented-блок не может прерывать абзац). Code spans
    `` `…` `` затем маскируются по всему тексту, включая многострочные;
    закрывающая последовательность должна повторять длину открывающей.
    """
    flags = [False] * len(text)
    fence, prev_para, offset = None, False, 0
    for line in text.splitlines(keepends=True):
        span = (offset, offset + len(line))
        offset = span[1]
        if fence is not None:
            flags[span[0]:span[1]] = [True] * (span[1] - span[0])
            if closes_fence(line, fence):
                fence = None
            prev_para = False
            continue
        marker = FENCE.match(line)
        if marker:
            flags[span[0]:span[1]] = [True] * (span[1] - span[0])
            fence = marker.group(1)
            prev_para = False
            continue
        if _BLANK.match(line):
            prev_para = False
            continue
        if INDENTED_CODE.match(line) and not prev_para:
            flags[span[0]:span[1]] = [True] * (span[1] - span[0])
            continue
        prev_para = not INDENTED_CODE.match(line) \
            and _STRUCTURAL.match(line) is None
    index = 0
    while index < len(text):
        if flags[index] or text[index] != "`":
            index += 1
            continue
        # backtick после нечётной серии обратных слешей — литерал,
        # code span не открывает; `\\` экранирует сам слеш
        backslashes = 0
        behind = index - 1
        while behind >= 0 and text[behind] == "\\" and not flags[behind]:
            backslashes += 1
            behind -= 1
        if backslashes % 2 == 1:
            index += 1
            continue
        opening = index
        while index < len(text) and text[index] == "`" and not flags[index]:
            index += 1
        run = index - opening
        cursor = index
        while cursor < len(text):
            if flags[cursor] or text[cursor] != "`":
                cursor += 1
                continue
            end = cursor
            while end < len(text) and text[end] == "`" and not flags[end]:
                end += 1
            if end - cursor == run:
                flags[opening:end] = [True] * (end - opening)
                index = end
                break
            cursor = end
        # run без закрывающей пары — литерал; поиск продолжается дальше
    return "".join("\x00" if hidden else char
                   for hidden, char in zip(flags, text))


def normalize_label(label):
    return " ".join(label.split()).casefold()


def marker_span(text, start_marker, end_marker):
    """(start, end) — индексы строк маркеров вне fenced-блоков.

    Маркеры внутри fenced-блоков (примеры) область не образуют.
    Возвращает строку-ошибку: ``absent``, ``end-without-start``,
    ``unclosed``, ``duplicate-start``, ``duplicate-end``.
    """
    fence, start, end = None, None, None
    for index, line in enumerate(text.split("\n")):
        if fence is not None:
            if closes_fence(line, fence):
                fence = None
            continue
        marker = FENCE.match(line)
        if marker:
            fence = marker.group(1)
            continue
        stripped = line.strip()
        if stripped == start_marker:
            if start is not None:
                return "duplicate-start"
            start = index
        elif stripped == end_marker:
            if start is None:
                return "end-without-start"
            if end is not None:
                return "duplicate-end"
            end = index
    if start is None:
        return "end-without-start" if end is not None else "absent"
    if end is None:
        return "unclosed"
    return (start, end)


def _bracket_end(chars, opening):
    """Позиция `]`, закрывающего `[` на ``opening``, со вложенностью."""
    depth, index = 0, opening
    while index < len(chars):
        char = chars[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index
        elif char == "\n":
            return None
        index += 1
    return None


def _dest_end(chars, paren):
    """(token_span, конец конструкции) для `(` на ``paren`` либо None."""
    index = paren + 1
    while index < len(chars) and chars[index] in " \t\n":
        index += 1
    if index < len(chars) and chars[index] == "<":
        close = index + 1
        while close < len(chars) and chars[close] not in ">\n\x00":
            close += 1
        if close >= len(chars) or chars[close] != ">":
            return None
        span = (index, close + 1)
        index = close + 1
    else:
        start, depth = index, 0
        while index < len(chars):
            char = chars[index]
            if char == "\\":
                index += 2
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    break
                depth -= 1
            elif char in " \t\n\x00":
                break
            index += 1
        if index == start or depth:
            return None
        span = (start, index)
    title = index
    while title < len(chars) and chars[title] in " \t\n":
        title += 1
    if title < len(chars) and chars[title] in "\"'":
        close = title + 1
        while close < len(chars) and chars[close] != chars[title] \
                and chars[close] not in "\n\x00":
            close += 1
        if close >= len(chars) or chars[close] != chars[title]:
            return None
        index = close + 1
    elif title < len(chars) and chars[title] == "(":
        close = title + 1
        while close < len(chars) and chars[close] != ")" \
                and chars[close] not in "\n\x00":
            close += 1
        if close >= len(chars) or chars[close] != ")":
            return None
        index = close + 1
    while index < len(chars) and chars[index] in " \t\n":
        index += 1
    if index >= len(chars) or chars[index] != ")":
        return None
    return span, index + 1


def scan(text):
    """Разобрать ссылки текста → список записей по позиции в тексте.

    Запись: ``kind`` — ``inline | image | refdef | ref | refimg |
    wikilink | html``; ``line``; ``span`` — абсолютные позиции токена
    назначения (для ref/wikilink/html — вся конструкция); ``target`` —
    исходный токен назначения либо метка/адресат конструкции; ``angle`` —
    назначение было в ``<>``; ``label`` — нормализованная метка.
    Подписи разбираются со сбалансированными скобками и экранированием.
    """
    masked = _masked_text(text)
    starts = [0] + [match.end() for match in re.finditer("\n", text)]

    def line_of(position):
        return bisect.bisect_right(starts, position)

    chars = list(masked)

    def erase(begin, end):
        chars[begin:end] = ["\x00"] * (end - begin)

    links = []
    for match in REFDEF.finditer(masked):
        token = match.group(2)
        links.append({"kind": "refdef", "line": line_of(match.start(2)),
                      "span": match.span(2), "target": token,
                      "angle": token.startswith("<"),
                      "label": normalize_label(match.group(1))})
        erase(match.start(), match.end())
    index = 0
    while index < len(chars):
        char = chars[index]
        if char == "\\":
            index += 2
            continue
        image = char == "!" and index + 1 < len(chars) \
            and chars[index + 1] == "["
        opening = index + 1 if image else index
        if char != "[" and not image:
            index += 1
            continue
        close = _bracket_end(chars, opening)
        if close is None:
            index += 1
            continue
        label_text = "".join(chars[opening + 1:close])
        after = close + 1
        if after < len(chars) and chars[after] == "(":
            parsed = _dest_end(chars, after)
            if parsed is not None:
                (begin, end), finish = parsed
                token = "".join(chars[begin:end])
                links.append({"kind": "image" if image else "inline",
                              "line": line_of(begin), "span": (begin, end),
                              "target": token, "angle": token.startswith("<"),
                              "label": normalize_label(label_text)})
                # стираются только скобки и адресат: внутри подписи может
                # быть вложенная конструкция [![img](src)](target)
                erase(index, opening + 1)
                erase(close, finish)
                index = opening + 1
                continue
        if after < len(chars) and chars[after] == "[":
            second = _bracket_end(chars, after)
            if second is not None:
                label2 = "".join(chars[after + 1:second]) or label_text
                links.append({"kind": "refimg" if image else "ref",
                              "line": line_of(index), "span": (index, second + 1),
                              "target": label2, "angle": False,
                              "label": normalize_label(label2)})
                erase(index, opening + 1)
                erase(close, second + 1)
                index = opening + 1
                continue
        index += 1
    work = "".join(chars)
    for match in WIKILINK.finditer(work):
        target = match.group(2).split("|", 1)[0].strip()
        links.append({"kind": "wikilink", "line": line_of(match.start()),
                      "span": match.span(), "target": target,
                      "angle": False, "label": target})
        erase(match.start(), match.end())
    work = "".join(chars)
    for match in HTML_LINK.finditer(work):
        target = match.group(1) or match.group(2)
        links.append({"kind": "html", "line": line_of(match.start()),
                      "span": match.span(), "target": target,
                      "angle": False, "label": target})
    links.sort(key=lambda item: (item["span"], item["line"]))
    return links


def _split(token):
    """(angle, path, query, fragment) либо None для внешних/пустых адресатов."""
    angle = token.startswith("<") and token.endswith(">") and len(token) > 1
    core = token[1:-1] if angle else token
    link = urlsplit(core)
    if link.scheme or link.netloc or not link.path:
        return None
    return angle, unquote(link.path), link.query, link.fragment


_BARE_EXTRA = " \t\"'()<>"


def _encode_path(text, colon, bare):
    """%-кодирование компонента пути: сохраняет адресата после разбора.

    ``#``, ``?``, ``%`` и управляющие символы кодируются всегда — иначе
    повторный разбор токена изменил бы путь/fragment или источник энтропии.
    Для bare-формы дополнительно кодируются пробелы, скобки, кавычки,
    ``<``/``>``; ``:`` первого сегмента — чтобы ``a:b.md`` не читался
    как URI-схема. Не-ASCII текст остаётся читаемым.
    """
    out = []
    for char in text:
        if char in "#?%" or ord(char) < 0x20 or ord(char) == 0x7F \
                or (bare and char in _BARE_EXTRA) \
                or (colon and char == ":"):
            out.append(f"%{ord(char):02X}")
        else:
            out.append(char)
    return "".join(out)


def format_dest(path, query, fragment, angle):
    """Собрать токен назначения из частей с сохранением стиля ``<>``.

    Путь кодируется компонентно через :func:`_encode_path`: адресат после
    повторного ``urlsplit``/``unquote`` тождественен исходному файлу.
    """
    suffix = ("?" + query if query else "") + ("#" + fragment if fragment else "")
    bare = not angle or any(char in path for char in ">\n")
    first, _, rest = path.partition("/")
    head = _encode_path(first, colon=True, bare=bare)
    tail = "/" + "/".join(_encode_path(seg, colon=False, bare=bare)
                          for seg in rest.split("/")) if rest else ""
    if not bare:
        # суффикс остаётся внутри <>: <путь?query#fragment> — один токен
        return f"<{head}{tail}{suffix}>"
    return head + tail + suffix


def map_path(path, moves):
    """Отобразить resolved-путь через карту (самый длинный предок)."""
    best = None
    for old, new in moves.items():
        if path == old or old in path.parents:
            if best is None or len(old.parts) > len(best[0].parts):
                best = (old, new)
    if best is None:
        return path
    return best[1] / path.relative_to(best[0])


def origin_path(path, moves):
    """Oldest origin through an ordered move history, including close/reopen."""
    current, seen, edges = path, {path}, []
    for _ in range(len(moves)):
        best = None
        for order, (old, new) in enumerate(moves.items()):
            if current == new or new in current.parents:
                if best is None or len(new.parts) > len(best[2].parts):
                    best = (order, old, new)
        if best is None:
            return current if edges else None
        order, old, new = best
        current = old / current.relative_to(new)
        edges.append((order, current))
        if current in seen:
            # A → B → A: bytes were authored at the oldest source, A.
            return min(edges, key=lambda edge: edge[0])[1]
        seen.add(current)
    return min(edges, key=lambda edge: edge[0])[1] if edges else None


def _retarget(token, base_from, base_to, moves):
    """Новый токен назначения; None — ссылка не локальный файл."""
    parts = _split(token)
    if parts is None:
        return None
    angle, path, query, fragment = parts
    resolved = (base_from / path).resolve()
    # Keep authored bytes when the relative destination is unaffected.
    # Closing an unrelated task must not normalize redundant path segments.
    if not _rel_changed(resolved, base_from, base_to, moves):
        return token
    mapped = map_path(resolved, moves)
    relative = Path(os.path.relpath(str(mapped), str(base_to))).as_posix()
    return format_dest(relative, query, fragment, angle)


def rewrite(text, source_old, source_new, moves):
    """Переписать ссылки текста при переносе source_old → source_new.

    ``moves`` — полная карта переносов ``{old: new}`` (resolved-пути),
    включая сам источник при его перемещении. Назначения разрешаются
    относительно ``source_old.parent`` и пересчитываются относительно
    ``source_new.parent``. Возвращает ``{"text", "changes"}``; changes —
    записи ``{line, old, new}`` в порядке появления.
    """
    edits = []
    for link in scan(text):
        if link["kind"] not in ("inline", "image", "refdef"):
            continue
        token = link["target"]
        new = _retarget(token, source_old.parent, source_new.parent, moves)
        if new is None or new == token:
            continue
        edits.append({"line": link["line"], "old": token, "new": new,
                      "span": link["span"]})
    for edit in sorted(edits, key=lambda item: item["span"], reverse=True):
        start, end = edit["span"]
        text = text[:start] + edit["new"] + text[end:]
    for edit in edits:
        del edit["span"]
    return {"text": text, "changes": edits}


def _rel_changed(resolved, base_from, base_to, moves):
    """True, если относительный адрес resolved-цели изменится."""
    old_rel = Path(os.path.relpath(str(resolved), str(base_from))).as_posix()
    new_rel = Path(os.path.relpath(str(map_path(resolved, moves)),
                                   str(base_to))).as_posix()
    return old_rel != new_rel


def _affected(token, base_from, base_to, moves):
    """True, если относительный адресат токена изменится после переноса.

    Затронутость — не только перемещение цели: при переносе источника
    относительный адрес неподвижной цели тоже меняется, и такую ссылку
    нужно переписать или отметить конфликтом.
    """
    parts = _split(token)
    if parts is None:
        return False
    resolved = (base_from / parts[1]).resolve()
    return _rel_changed(resolved, base_from, base_to, moves)


def _wiki_target(token, base_from, moves):
    """Фактический resolved-адресат wikilink → (путь, ambiguous).

    Токен разбирается на path/query/fragment до выбора кандидата;
    ``.md`` добавляется только к path без суффикса. Реальная цель —
    кандидат, существующий на диске или ключ ``moves``; отсутствующий
    кандидат зависимостью не считается. Несколько реальных кандидатов —
    неоднозначность, возвращается ``(None, True)``.
    """
    parts = _split(token)
    if parts is None:
        return None, False
    candidates = [parts[1]]
    if not Path(parts[1]).suffix:
        candidates.append(parts[1] + ".md")
    real = []
    for candidate in candidates:
        resolved = (base_from / candidate).resolve()
        if resolved.exists() or resolved in moves:
            real.append(resolved)
    if len(real) > 1:
        return None, True
    return (real[0] if real else None), False


def plan_rewrites(files, moves, root, managed=(), frozen=()):
    """План переписывания ссылок для набора файлов и полной карты переносов.

    Вызывается до записи: файлы стоят на исходных местах, ``moves``
    описывает запланированные переносы. Для каждого файла источник —
    его текущий путь, новое расположение — ``map_path(path, moves)``.
    ``managed`` — resolved-префиксы управляемого ядра: затронутая ссылка
    оттуда — конфликт, файл не переписывается. ``frozen`` — resolved-пути
    immutable-материалов: пропускаются, их исторические адреса разрешает
    check. Возвращает ``{"updates", "conflicts", "frozen"}``; в updates —
    ``original``/``updated``/``changes``. Конфликт — ``{path, line,
    kind, detail}``, kind: ``managed-core`` либо ``unsupported``.
    """
    updates, conflicts, skipped = {}, [], []
    for path in files:
        resolved = path.resolve()
        label = str(resolved)
        if any(resolved == item or item in resolved.parents for item in frozen):
            skipped.append(path)
            continue
        base_dir = resolved.parent
        base_new = map_path(resolved, moves).parent
        text = resolved.read_text(encoding="utf-8")
        in_core = any(resolved == item or item in resolved.parents
                      for item in managed)
        own_conflicts = []
        for link in scan(text):
            if link["kind"] in ("ref", "refimg"):
                continue
            if link["kind"] == "wikilink":
                target_path, ambiguous = _wiki_target(link["target"],
                                                      base_dir, moves)
                hit = ambiguous or (target_path is not None
                                    and _rel_changed(target_path, base_dir,
                                                     base_new, moves))
            else:
                hit = _affected(link["target"], base_dir, base_new, moves)
            if not hit:
                continue
            if in_core:
                own_conflicts.append({"path": label, "line": link["line"],
                                      "kind": "managed-core",
                                      "detail": link["target"]})
            elif link["kind"] in ("wikilink", "html"):
                own_conflicts.append({"path": label, "line": link["line"],
                                      "kind": "unsupported",
                                      "detail": link["target"]})
        conflicts += own_conflicts
        if in_core and own_conflicts:
            continue
        result = rewrite(text, resolved, map_path(resolved, moves), moves)
        if result["changes"]:
            updates[path] = {"original": text, "updated": result["text"],
                             "changes": result["changes"]}
    conflicts.sort(key=lambda item: (item["path"], item["line"]))
    return {"updates": updates, "conflicts": conflicts, "frozen": skipped}


def check_text(text, label, base_dir, root, moves=None):
    """Проверить ссылки текста → список ошибок с префиксом ``label``.

    ``base_dir`` — каталог, относительно которого записаны ссылки (для
    immutable-материалов — исходное расположение до переноса); ``moves``
    отображает найденный исторический адресат на текущий путь. Для
    обычных документов передавать ``moves=None``: устаревший адрес там —
    битая ссылка, а не история.
    """
    moves = moves or {}
    links = scan(text)
    definitions = {link["label"] for link in links if link["kind"] == "refdef"}
    errors = []
    for link in links:
        if link["kind"] in ("ref", "refimg"):
            if link["label"] not in definitions:
                errors.append(f"{label}: нет определения ссылки {link['target']}")
            continue
        if link["kind"] not in ("inline", "image", "refdef"):
            continue
        parts = _split(link["target"])
        if parts is None:
            continue
        try:
            destination = (base_dir / parts[1]).resolve()
        except ValueError:
            errors.append(f"{label}: битая или внешняя локальная ссылка "
                          f"{link['target']}")
            continue
        seen = set()
        # Historical paths may be current again after reopen. Follow moves
        # only while the target is absent, never redirect an existing target.
        for _ in range(len(moves)):
            if destination.exists() or destination in seen:
                break
            seen.add(destination)
            destination = map_path(destination, moves)
        if not destination.is_relative_to(root) or not destination.exists():
            errors.append(f"{label}: битая или внешняя локальная ссылка "
                          f"{link['target']}")
    return errors
