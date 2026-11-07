"""Versioned Markdown cards. Read-only parsing; no execution of card commands."""
import datetime as dt
import hashlib
from pathlib import Path
import re

import document_links
import documents

VERSION = 2
HEADER = re.compile(
    r"\A# ([^\n]+)\n\s*\nID: ([^\n]+)\nStatus: ([^\n]+)\n"
    r"Priority: ([^\n]+)\nDepends-On: ([^\n]+)"
    r"(?:\nExecutor: ([^\n]+))?(?:\nModel: ([^\n]+))?"
    r"\nCreated: ([^\n]+)\nClosed: ([^\n]+)(?:\n|$)"
)
KINDS = {'EXEC': 'execution', 'CHECK': 'verification', 'HANDOFF': 'handoff', 'AUDIT': 'audit'}
TITLES = {'EXEC': {'Выполнение', 'Исправление'}, 'CHECK': {'Проверка'},
          'HANDOFF': {'Передача'}, 'AUDIT': {'Аудит'}}
ENTRY = re.compile(r'^(EXEC|CHECK|HANDOFF|AUDIT)-([0-9]{2,}) — (.+)$')
FINDING = re.compile(r'^(A-[0-9]{2,}) — Замечание$')
COMMON = {'Время', 'Исполнитель', 'Модель'}
HEADER_FIELDS = {'ID', 'Status', 'Priority', 'Depends-On', 'Executor', 'Model', 'Created', 'Closed'}
REQUIRED = {
    'EXEC': {'Изменения', 'Файлы'},
    'CHECK': {'Результат', 'Критерии', 'Среда', 'Факт', 'Доказательства', 'Ограничения'},
    'HANDOFF': {'Выполнение', 'Проверки', 'Состояние', 'Фокус аудита'},
    'AUDIT': {'Передача', 'Результат', 'Критерии', 'Независимо проверено', 'Доказательства', 'Ограничения'},
    'A': {'Серьёзность', 'Проблема', 'Воспроизведение', 'Факт', 'Влияние', 'Критерий исправления'},
}
OPTIONAL = {'EXEC': {'Замечания'}, 'CHECK': {'Команда', 'Сценарий'}, 'HANDOFF': set(),
            'AUDIT': {'Закрытые замечания', 'Замечания'}, 'A': {'Критерии'}}
TASK_SECTIONS = ['Цель и границы', 'Контекст', 'Критерии приёмки', 'План', 'Ресурсы']
PROTOCOL_SECTIONS = ('Выполнение и проверки', 'Передача')
PROTOCOL_PREFIXES = ('Аудит', 'Повторный аудит', 'Исправлено')


def require(ok, message):
    if not ok:
        raise ValueError('card format: ' + message)


def metadata(text):
    errors = []
    meta = documents.parse_meta(text, 'card', errors)
    require(not errors, '; '.join(errors))
    return meta or ({'card_format_version': 2} if documents.external_card(text) else {})


def version(text):
    value = metadata(text).get('card_format_version', 1)
    require(type(value) is int and value > 0, 'card_format_version должен быть положительным целым')
    return value


def lines_outside_code(text):
    fence = None
    for number, line in enumerate(text.splitlines()):
        if fence is not None:
            if document_links.closes_fence(line, fence):
                fence = None
            continue
        match = document_links.FENCE.match(line)
        if match:
            fence = match.group(1)
            continue
        yield number, line
    require(fence is None, 'незакрытый fenced-блок')


def digest(text):
    return 'sha256:' + hashlib.sha256(text.encode('utf-8')).hexdigest()


def fields(raw, kind, label):
    """Recognize only unindented field names, outside fenced examples."""
    result, current = {}, None
    raw_lines = raw.splitlines()
    visible = dict(lines_outside_code(raw))
    for number, line in enumerate(raw_lines):
        match = re.fullmatch(r'([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9 _-]*):(?:[ \t]+(.*))?', line) if number in visible else None
        if match:
            key, value = match.groups()
            value = value or ''
            require(key not in result, f'{label}: повтор поля {key}')
            require(key in REQUIRED[kind] | OPTIONAL[kind] | (COMMON if kind != 'A' else set()),
                    f'{label}: неизвестное поле {key}')
            result[key], current = value, key
        elif current is not None:
            result[current] += '\n' + line
        else:
            require(not line.strip(), f'{label}: текст вне поля')
    result = {key: val.strip() for key, val in result.items()}
    expected = REQUIRED[kind] | (COMMON if kind != 'A' else set())
    require(expected <= result.keys(), f'{label}: отсутствуют поля {sorted(expected - result.keys())}')
    require(all(result.values()), f'{label}: пустое поле')
    if kind != 'A':
        try:
            stamp = dt.datetime.fromisoformat(result['Время'].replace('Z', '+00:00'))
        except ValueError:
            stamp = None
        require(stamp is not None and stamp.tzinfo is not None, f'{label}: Время требует ISO с поясом')
        require('\n' not in result['Исполнитель'] and '\n' not in result['Модель'],
                f'{label}: атрибуция должна быть однострочной')
    return result


def refs(value):
    return [part.strip() for part in value.split(',')]


def parse(text, path=''):
    """Client contract. Unsupported versions return a Markdown fallback only."""
    v = version(text)
    if v != VERSION:
        return {'status': 'unsupported', 'card_format_version': v, 'markdown_path': path,
                'message': 'Формат карточки не поддерживается. Откройте Markdown.'}
    header_match = HEADER.match(text)
    require(header_match is not None, 'неверная шапка карточки')
    header = dict(zip(('title', 'id', 'status', 'priority', 'depends_on', 'executor', 'model',
                       'created', 'closed'), header_match.groups()))
    require(documents.TASK_ID.fullmatch(header['id']) is not None, 'неверный ID задачи')
    require(header['status'] in {'draft', 'ready', 'in_progress', 'in_review', 'done', 'blocked', 'cancelled'},
            'неверный Status')
    require(header['priority'] in {'P0', 'P1', 'P2', 'P3'}, 'неверный Priority')
    require(header['executor'] is not None and header['model'] is not None,
            'в формате 2 обязательны Executor и Model')
    for key in ('created', 'closed'):
        value = header[key]
        if value not in {'-', 'unknown'}:
            try:
                stamp = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                stamp = None
            require(stamp is not None and stamp.tzinfo is not None, f'{key}: требуется ISO с поясом')
    all_lines = text.splitlines(keepends=True)
    visible = list(lines_outside_code(text))
    # Remove the actual metadata block before structural validation, not examples.
    pos = documents._meta_positions(text.split('\n'))
    visible = [(n, line) for n, line in visible
               if not isinstance(pos, tuple) or not pos[0] <= n <= pos[1]]
    h2 = [(n, line[3:]) for n, line in visible if line.startswith('## ')]
    require([title for _, title in h2] == ['Задача', 'Журнал'], 'ожидаются разделы Задача, Журнал')
    header_fields = set()
    title_seen = False
    for n, line in visible:
        if n >= h2[0][0]:
            break
        stripped = line.strip()
        if not stripped or line == documents.EXTERNAL_META:
            continue
        if line.startswith('# '):
            require(not title_seen, 'повтор заголовка карточки')
            title_seen = True
            continue
        field_match = re.fullmatch(r'([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9 _-]*):(?:[ \t]+(.*))?', line)
        require(field_match is not None, f'недопустимая строка в шапке: {line}')
        key = field_match.group(1)
        require(key in HEADER_FIELDS, f'неизвестное поле шапки {key}')
        require(key not in header_fields, f'повтор поля шапки {key}')
        header_fields.add(key)
    require(title_seen, 'отсутствует заголовок карточки')
    require(header_fields == HEADER_FIELDS,
            f'в шапке отсутствуют поля {sorted(HEADER_FIELDS - header_fields)}')
    journal_start = h2[1][0]
    task_headings = []
    for n, line in visible:
        if h2[0][0] < n < journal_start and line.startswith('#'):
            require(line.startswith('### '), f'недопустимый заголовок в разделе задачи {line}')
            task_headings.append((n, line[4:].strip()))
    require([title for _, title in task_headings] == TASK_SECTIONS, 'неверные подразделы задачи')
    sections = task_headings
    require(not ''.join(all_lines[h2[0][0] + 1:sections[0][0]]).strip(), 'текст вне подразделов задачи')
    task = {}
    for i, (start, title) in enumerate(sections):
        end = sections[i + 1][0] if i + 1 < len(sections) else journal_start
        body = ''.join(all_lines[start + 1:end]).strip()
        require(body, f'пустой раздел {title}')
        task[title] = body
    criteria, markers = {}, {}
    for _, line in lines_outside_code(task['Критерии приёмки']):
        if not line.strip():
            continue
        m = re.fullmatch(r'- (?:\[([ x])\] )?(AC-[0-9]{2,}): (.+)', line)
        require(m is not None, 'критерии должны иметь вид - [ ] AC-01: текст либо - AC-01: текст')
        marker, ident, desc = m.groups()
        markers[ident] = None if marker is None else marker == 'x'
        require(ident not in criteria and int(ident[3:]) > 0, f'повтор/некорректный критерий {ident}')
        criteria[ident] = desc
    require(criteria, 'нет критериев')
    starts = []
    findings_at = []
    for n, line in visible:
        if n <= journal_start:
            continue
        if line.startswith('#'):
            if line.startswith('### '):
                m = ENTRY.fullmatch(line[4:])
                require(m is not None, f'неверный заголовок записи {line}')
                prefix, number, title = m.groups()
                require(int(number) > 0 and title in TITLES[prefix], f'неверный тип записи {line}')
                starts.append((n, prefix + '-' + number, prefix, title))
            elif line.startswith('#### '):
                m = FINDING.fullmatch(line[5:])
                require(m is not None, f'неверный заголовок находки {line}')
                findings_at.append((n, m[1]))
            else:
                require(False, f'недопустимый заголовок {line}')
    first = starts[0][0] if starts else len(all_lines)
    require(not ''.join(all_lines[journal_start + 1:first]).strip(), 'текст вне записей журнала')
    entries, known, findings, closed = [], {}, {}, set()
    fixed = set()
    for idx, (start, ident, prefix, title) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(all_lines)
        require(ident not in known, f'повтор ID {ident}')
        nested = [(n, fid) for n, fid in findings_at if start < n < end]
        require(not nested or prefix == 'AUDIT', f'{ident}: находки допустимы только в аудите')
        body_end = nested[0][0] if nested else end
        f = fields(''.join(all_lines[start + 1:body_end]), prefix, ident)
        record = {'id': ident, 'type': KINDS[prefix], 'title': title, 'fields': f, 'findings': [],
                  'raw': ''.join(all_lines[start:end]).strip(),
                  'sha256': digest(''.join(all_lines[start:end]).strip()),
                  'journal_sha256': digest(''.join(all_lines[journal_start:end]).strip())}

        def links(field, expected):
            values = refs(f[field])
            require(len(values) == len(set(values)), f'{ident}: повтор ссылок {field}')
            for ref in values:
                target = criteria if expected == 'AC' else findings if expected == 'A' else known
                require(ref in target and (expected in {'AC', 'A'} or ref.startswith(expected + '-')),
                        f'{ident}: неизвестная/неверная ссылка {ref} в {field}')
            return values

        if prefix in {'CHECK', 'AUDIT'}:
            links('Критерии', 'AC')
        if prefix == 'EXEC' and title == 'Исправление':
            require('Замечания' in f, f'{ident}: исправление без замечаний')
        if prefix == 'EXEC' and 'Замечания' in f:
            values = links('Замечания', 'A')
            require(not set(values) & closed, f'{ident}: замечание уже закрыто')
            fixed.update(values)
        if prefix == 'CHECK':
            require(f['Результат'] in {'passed', 'failed', 'not_run'}, f'{ident}: неверный результат')
            require('Команда' in f or 'Сценарий' in f, f'{ident}: нужна Команда или Сценарий')
        if prefix == 'HANDOFF':
            links('Выполнение', 'EXEC')
            checks = links('Проверки', 'CHECK')
            require(all(known[c]['fields']['Результат'] == 'passed' for c in checks),
                    f'{ident}: передача требует passed-проверки')
        if prefix == 'AUDIT':
            handed = links('Передача', 'HANDOFF')
            require(len(handed) == 1 and handed[0] == next(
                (e['id'] for e in reversed(entries) if e['type'] == 'handoff'), None),
                f'{ident}: аудит относится к прежней передаче')
            require(entries and entries[-1]['type'] in {'handoff', 'audit'},
                    f'{ident}: после работы нужна новая передача')
            require(f['Результат'] in {'accepted', 'changes_requested'}, f'{ident}: неверный результат')
            if 'Закрытые замечания' in f:
                resolved = links('Закрытые замечания', 'A')
                require(set(resolved) <= fixed and not set(resolved) & closed,
                        f'{ident}: закрытие без исправления/повторное закрытие')
                closed.update(resolved)
        for j, (n, fid) in enumerate(nested):
            stop = nested[j + 1][0] if j + 1 < len(nested) else end
            require(fid not in findings and int(fid[2:]) > 0, f'повтор/неверный ID {fid}')
            ff = fields(''.join(all_lines[n + 1:stop]), 'A', fid)
            require(ff['Серьёзность'] in {'P0', 'P1', 'P2', 'P3'}, f'{fid}: неверная серьёзность')
            require('Критерии' in ff or not any(v is not None for v in markers.values())
                    or fid in metadata(text).get('checkbox_legacy_findings', []),
                    f'{fid}: нужны Критерии для новой находки')
            affected = refs(ff.get('Критерии', f['Критерии']))
            require(len(affected) == len(set(affected)) and all(c in criteria for c in affected),
                    f'{fid}: неизвестные/повторные критерии')
            finding = {'id': fid, 'audit_id': ident, 'fields': ff, 'criteria': affected, 'criteria_inferred': 'Критерии' not in ff}
            findings[fid] = finding
            record['findings'].append(finding)
        if prefix == 'AUDIT':
            require(f['Результат'] != 'changes_requested' or nested, f'{ident}: нет находок')
            require(f['Результат'] != 'accepted' or not (set(findings) - closed),
                    f'{ident}: есть открытые замечания')
        known[ident] = record
        entries.append(record)
    require(all(any(start < n for start, *_ in starts) for n, _ in findings_at), 'находка вне записи')
    latest_handoff = next((e for e in reversed(entries) if e['type'] == 'handoff'), None)
    latest_audit = next((e for e in reversed(entries) if e['type'] == 'audit'), None)
    return {'status': 'supported', 'card_format_version': v, 'markdown_path': path,
            'header': header, 'task': task, 'criteria': criteria, 'criterion_markers': markers, 'entries': entries,
            'latest_handoff': latest_handoff['id'] if latest_handoff else None,
            'latest_audit': latest_audit['id'] if latest_audit else None,
            'current_verdict': entries[-1]['fields']['Результат'] if entries and entries[-1]['type'] == 'audit' else None,
            'open_findings': sorted(set(findings) - closed)}


def receipt(text, meta, acceptance=False):
    """Validate journal bindings. Legacy lifecycle is deliberately untouched."""
    if version(text) == 1:
        return
    parsed = parse(text)
    require(parsed['status'] == 'supported', 'версия не поддерживается lifecycle')
    entries = parsed['entries']
    handoff = meta.get('handoff') or {}
    handed = next((e for e in entries if e['id'] == handoff.get('entry_id')), None)
    require(handed is not None and handed['id'] == parsed['latest_handoff'], 'нет актуальной передачи журнала')
    require(handed['journal_sha256'] == handoff.get('journal_sha256'), 'переданная история изменилась')
    after = entries[entries.index(handed) + 1:]
    require(all(e['type'] == 'audit' for e in after), 'после передачи добавлена работа; повторите handoff')
    if acceptance:
        accepted = meta.get('acceptance') or {}
        audit = entries[-1] if entries else None
        require(audit and audit['type'] == 'audit' and audit['fields']['Результат'] == 'accepted',
                'нет последнего accepted-аудита')
        require(accepted.get('audit_id') == audit['id'] and accepted.get('audit_sha256') == audit['sha256'],
                'приёмка не соответствует последнему аудиту')


def criterion_lines(text):
    """Actual criteria only: not examples, not journal, not arbitrary checklists."""
    in_task, in_criteria = False, False
    for number, line in lines_outside_code(text):
        if line.startswith('## '):
            in_task = line == '## Задача'
            in_criteria = False
        elif line.startswith('### '):
            in_criteria = in_task and line == '### Критерии приёмки'
        elif in_criteria:
            match = re.fullmatch(r'- (?:\[([ x])\] )?(AC-[0-9]{2,}): (.+)', line)
            if match:
                yield number, match[2], match[3]


def normalize_checkboxes(text):
    """Canonical requirement text; no other bytes or examples are normalized."""
    if version(text) != 2:
        return text
    lines = text.splitlines(keepends=True)
    for number, ident, description in criterion_lines(text):
        ending = '\n' if lines[number].endswith('\n') else ''
        lines[number] = f'- {ident}: {description}' + ending
    return ''.join(lines)


def render_checkboxes(text, states):
    lines = text.splitlines(keepends=True)
    for number, ident, description in criterion_lines(text):
        ending = '\n' if lines[number].endswith('\n') else ''
        marker = 'x' if states[ident]['checked'] else ' '
        lines[number] = f'- [{marker}] {ident}: {description}' + ending
    return ''.join(lines)


def _check_matches_historical(check, binding, meta, card_path=None, root=None):
    expected_sha = binding.get('check_sha256')
    if not expected_sha or not meta or not meta.get('moves'):
        return False
    raw = check.get('raw')
    if not raw:
        return False
    base_root = Path.cwd() if root is None else root if hasattr(root, "view") else Path(root)
    moves = meta.get('moves', [])
    card_move = None
    if card_path:
        for m in reversed(moves):
            if m.get('to') == card_path or card_path.endswith(m.get('to', '')):
                card_move = m
                break
    if not card_move:
        for m in reversed(moves):
            to_val = m.get('to', '')
            if to_val.endswith('.md') and 'archive' in to_val:
                card_move = m
                break
    if not card_move:
        card_move = moves[-1]

    source_old = (base_root / card_move['to']).resolve()
    source_new = (base_root / card_move['from']).resolve()
    reverse_moves = {(base_root / m['to']).resolve(): (base_root / m['from']).resolve()
                     for m in moves if 'from' in m and 'to' in m}
    try:
        rewritten = document_links.rewrite(raw, source_old, source_new, reverse_moves)
        return digest(rewritten['text'].strip()) == expected_sha
    except Exception:
        return False


def criterion_states(parsed, meta, projection, fresh, historical=False, root=None):
    """Single state calculation shared by validation, Markdown and JSON."""
    checks, last_execution = {}, -1
    for index, entry in enumerate(parsed['entries']):
        if entry['type'] == 'execution':
            last_execution = index
        elif entry['type'] == 'verification':
            for ident in refs(entry['fields']['Критерии']):
                checks[ident] = (index, entry)
    open_by_criterion = {}
    for entry in parsed['entries']:
        for finding in entry['findings']:
            if finding['id'] in parsed['open_findings']:
                for ident in finding['criteria']:
                    open_by_criterion.setdefault(ident, []).append(finding['id'])
    bindings = meta.get('criterion_evidence', {})
    states = {}
    for ident in parsed['criteria']:
        state = {'checked': False, 'check_id': None, 'reason': 'not_checked',
                 'open_findings': open_by_criterion.get(ident, [])}
        selected = checks.get(ident)
        if selected:
            index, check = selected
            state['check_id'] = check['id']
            binding = bindings.get(check['id'], {})
            exec_before = max((i for i in range(index) if parsed['entries'][i]['type'] == 'execution'), default=-1)
            exec_entry = parsed['entries'][exec_before] if exec_before >= 0 else None
            check_matches = binding.get('check_sha256') == check['sha256']
            if not check_matches and historical:
                check_matches = _check_matches_historical(check, binding, meta, parsed.get('markdown_path'), root=root)
            if state['open_findings']:
                state['reason'] = 'open_finding'
            elif check['fields']['Результат'] != 'passed':
                state['reason'] = check['fields']['Результат']
            elif index < last_execution:
                state['reason'] = 'work_after_check'
            elif not check_matches:
                state['reason'] = 'unbound_or_changed_check'
            elif not historical and binding.get('card_projection') != projection:
                state['reason'] = 'requirements_changed'
            elif not fresh(binding):
                state['reason'] = 'evidence_stale'
            elif parsed['header']['status'] in {'blocked', 'cancelled'}:
                state['reason'] = parsed['header']['status']
            else:
                state.update(checked=True, reason='historical' if historical else 'passed')
        states[ident] = state
    mismatches = [ident for ident, marker in parsed['criterion_markers'].items()
                  if marker is not None and marker != states[ident]['checked']]
    return states, mismatches


def card_projection(text):
    """SHA-256 проекции значимого содержимого карточки."""
    card_version = version(text)
    require(card_version in {1, 2}, "версия не поддерживается lifecycle")
    if card_version == 2:
        text = normalize_checkboxes(text)
    lines = text.split("\n")
    positions = documents._meta_positions(lines)
    if isinstance(positions, tuple):
        start, end = positions
        lines = lines[:start] + lines[end + 1:]
    kept, fence, skip = [], None, False
    for line in lines:
        if fence is not None:
            if document_links.closes_fence(line, fence):
                fence = None
            if not skip:
                kept.append(line)
            continue
        marker = document_links.FENCE.match(line)
        if marker:
            fence = marker.group(1)
            if not skip:
                kept.append(line)
            continue
        heading = re.match(r"^ {0,3}##\s+(.+?)\s*$", line)
        if heading:
            title = heading.group(1)
            skip = (card_version == 2 and title == "Журнал") or title in PROTOCOL_SECTIONS or any(
                title.startswith(prefix) for prefix in PROTOCOL_PREFIXES)
            if not skip:
                kept.append(line)
            continue
        if skip:
            continue
        if re.match(r"^(Status|Closed):", line):
            continue
        kept.append(line)
    return digest("\n".join(kept))


def execution_state(text, path=''):
    """Current execution entry (id, combined sha256) for version 2 task cards, else (None, None)."""
    try:
        if version(text) != VERSION:
            return None, None
        parsed = parse(text, path)
        if parsed.get('status') != 'supported':
            return None, None
        execs = [e for e in parsed['entries'] if e['type'] == 'execution']
        if not execs:
            return None, None
        combined_sha = digest('\n'.join(f"{e['id']}:{e['sha256']}" for e in execs))
        return execs[-1]['id'], combined_sha
    except (ValueError, KeyError):
        return None, None



def public_markdown(text):
    """Hide only the actual metadata block, retaining all user examples."""
    positions = documents._meta_positions(text.splitlines())
    if isinstance(positions, tuple):
        start, end = positions
        lines = text.split('\n')
        return '\n'.join(lines[:start] + lines[end + 1:])
    return text


def select_entry(parsed, ident):
    for entry in parsed['entries']:
        if entry['id'] == ident:
            return entry
        for finding in entry['findings']:
            if finding['id'] == ident:
                return finding
    raise ValueError(f'Нет записи/замечания {ident}')


def context_view(parsed):
    """An index, not an alternative journal or a scope restriction for audit."""
    current = {}
    for entry in parsed['entries']:
        current[entry['type']] = entry['id']
    selected = set(current.values())
    for entry in parsed['entries']:
        if entry['id'] == parsed['latest_handoff']:
            for key in ('Выполнение', 'Проверки'):
                selected.update(refs(entry['fields'][key]))
        if set(refs(entry['fields'].get('Замечания', ''))) & set(parsed['open_findings']):
            selected.add(entry['id'])
    entries = [{'id': e['id'], 'type': e['type'], 'title': e['title'],
                'result': e['fields'].get('Результат')}
               for e in parsed['entries'] if e['id'] in selected]
    handed = next((i for i,e in enumerate(parsed['entries']) if e['id'] == parsed['latest_handoff']), -1)
    after = [e['id'] for e in parsed['entries'][handed + 1:] if e['type'] != 'audit']
    return {'header': parsed['header'], 'task': parsed['task'],
            'acceptance_state': parsed['acceptance_state'], 'handoff_state': parsed['handoff_state'],
            'handoff_reason': parsed.get('handoff_reason'),
            'acceptance_reason': parsed.get('acceptance_reason'),
            'latest_handoff': parsed['latest_handoff'], 'latest_audit': parsed['latest_audit'],
            'current_verdict': parsed['current_verdict'], 'work_after_handoff': after,
            'open_findings': [select_entry(parsed, fid) for fid in parsed['open_findings']],
            'entries': entries}


def handoff_view(parsed):
    """Select original linked records, without following free-text references."""
    selected = set()
    if parsed['latest_handoff']:
        handoff = select_entry(parsed, parsed['latest_handoff'])
        selected.add(handoff['id'])
        for field in ('Выполнение', 'Проверки'):
            selected.update(refs(handoff['fields'][field]))
    return {'context': context_view(parsed),
            'entries': [entry for entry in parsed['entries'] if entry['id'] in selected]}


def render_handoff(parsed):
    view = handoff_view(parsed)
    lines = [render_context(parsed), '## Записи последней передачи']
    lines += [entry['raw'] for entry in view['entries']]
    if not view['entries']:
        lines.append('Передача ещё не записана.')
    for finding in view['context']['open_findings']:
        lines += ['#### ' + finding['id'] + ' — Замечание',
                  '\n'.join(key + ': ' + value for key, value in finding['fields'].items())]
    return '\n\n'.join(lines)


def render_context(parsed):
    view = context_view(parsed)
    header = view['header']
    lines = [f"# {header['title']}", f"Статус: {header['status']}; передача CLI: {view['handoff_state']}; приёмка CLI: {view['acceptance_state']}"]
    if view['handoff_reason']:
        lines.append('Ограничение передачи: ' + view['handoff_reason'])
    if view['acceptance_reason']:
        lines.append('Ограничение приёмки: ' + view['acceptance_reason'])
    if header['status'] == 'in_review' and view['handoff_state'] == 'not_current':
        lines.append('Передача не подтверждена: статус in_review сам по себе не означает готовность к аудиту.')
    lines += [f"Передача: {view['latest_handoff'] or '—'}; аудит: {view['latest_audit'] or '—'}; вердикт журнала: {view['current_verdict'] or '—'}"]
    if view['work_after_handoff']:
        lines.append('Работа без актуальной передачи: ' + ', '.join(view['work_after_handoff']))
    for title, body in view['task'].items():
        lines += ['', '### ' + title, body]
    lines += ['', '### Указатель актуальных записей']
    for entry in view['entries']:
        lines.append(f"- {entry['id']} — {entry['title']}" + (f" ({entry['result']})" if entry['result'] else ''))
    for finding in view['open_findings']:
        lines.append(f"- {finding['id']} — {finding['fields']['Серьёзность']}: {finding['fields']['Проблема']} (аудит {finding['audit_id']})")
    if not view['open_findings']:
        lines.append('Открытых замечаний нет.')
    ident = header['id']
    lines += ['', f'Подробности: python3 ai-dev-playbook/scripts/playbook.py card show {ident} --entry <ID>',
              f'Полная история: python3 ai-dev-playbook/scripts/playbook.py card show {ident} --full']
    return '\n'.join(lines)
