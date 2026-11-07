"""Policy primitives for workflow v2; no command execution or lifecycle writes.

Callers validate v2 metadata first. Snapshots are explicit read sets, never Git
status. Baseline/current must cover the same project universe; None means unknown.
The runner (PB-033) owns provenance validation and immutable result ordering.
"""
import copy
import fnmatch
import hashlib
import json
import re

import task_cards
from pathlib import Path


def fingerprint(value):
    return 'sha256:' + hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
        allow_nan=False).encode()).hexdigest()


def _relative(value, pattern=False):
    if not isinstance(value, str) or not value or '\\' in value or '\0' in value:
        raise ValueError('Expected a normalized relative POSIX path')
    parts = value.split('/')
    if any(p in {'', '.', '..'} for p in parts) or value.startswith('/'):
        raise ValueError('Expected a normalized relative POSIX path')
    if not pattern and any(c in value for c in '*?['):
        raise ValueError('Glob is not an explicit path')
    return value


def _match(path, pattern):
    """Segment glob: * excludes slash, ** matches zero or more segments."""
    path_parts, pats = path.split('/'), pattern.split('/')

    def walk(i, j):
        if j == len(pats):
            return i == len(path_parts)
        if pats[j] == '**':
            return walk(i, j + 1) or (i < len(path_parts) and walk(i + 1, j))
        return (i < len(path_parts) and fnmatch.fnmatchcase(path_parts[i], pats[j])
                and walk(i + 1, j + 1))
    return walk(0, 0)


def applicability(rule='always', *, scope, baseline, current, checkpoint=None,
                  checkpoint_checks=(), check_id=None):
    """Return required plus explain reason; uncertainty requires the check.

    Scope entries are roots (not globs). Checkpoint evidence is evaluated only
    when a checkpoint is explicitly requested; local close implies none.
    """
    app = {'kind': rule} if isinstance(rule, str) else rule
    kind = app.get('kind')
    if kind == 'always':
        return {'required': True, 'reason': 'always'}
    if kind == 'checkpoint':
        required = checkpoint is not None and (
            app.get('checkpoint') in {None, checkpoint} or check_id in checkpoint_checks)
        return {'required': required, 'reason': 'checkpoint' if required else 'outside checkpoint'}
    if kind != 'when_paths':
        raise ValueError('Unknown applicability')
    patterns = [_relative(p, pattern=True) for p in app['paths']]
    if not patterns:
        raise ValueError('Empty when_paths')
    if baseline is None or current is None or not scope:
        return {'required': True, 'reason': 'unknown baseline/current/scope'}
    roots = [_relative(p) for p in scope]
    paths = set(baseline) | set(current)
    changed = sorted(p for p in paths if baseline.get(p) != current.get(p))
    for path in changed:
        _relative(path)
        if any(_match(path, p) for p in patterns):
            return {'required': True, 'reason': 'changed path: ' + path}
    for root in sorted(roots):
        for pat in patterns:
            # Literal prefix bounds every possible match. Intersecting roots
            # conservatively include broad scope and patterns inside that root.
            prefix = []
            for part in pat.split('/'):
                if any(c in part for c in '*?['):
                    break
                prefix.append(part)
            literal = '/'.join(prefix)
            if (not literal or root == literal or root.startswith(literal + '/')
                    or literal.startswith(root + '/') or _match(root, pat)):
                return {'required': True, 'reason': 'scope overlaps: ' + root}
    return {'required': False, 'reason': 'scope and baseline diff do not intersect patterns'}


_TASK_SERVICE = {'materials', 'handoff', 'acceptance', 'moves', 'audit_packages',
                 'criterion_evidence', 'checkbox_legacy_findings'}


def task_projection(markdown, metadata):
    """Explicit managed task input, preserving unknown metadata conservatively.

    Known managed protocol sections follow the existing card projection.
    Unknown prose and metadata remain significant. Ordinary Markdown inputs
    always use full file bytes.
    """
    meta = {k: copy.deepcopy(v) for k, v in metadata.items() if k not in _TASK_SERVICE}
    workflow = meta.get('workflow')
    if isinstance(workflow, dict):
        workflow.pop('history', None)
        workflow.pop('transitions', None)
    return {'text': task_cards.card_projection(markdown), 'metadata': meta}


def policy_projection(settings, registry_ids=None):
    """Relevant definitions plus all unknown policy fields (fail conservative)."""
    result = copy.deepcopy(settings)
    if registry_ids is not None:
        ids = set(registry_ids)
        registry = result.get('registry', [])
        found = {item['id'] for item in registry}
        if not ids <= found:
            raise ValueError('Unknown registry dependency')
        result['registry'] = sorted((item for item in registry if item['id'] in ids),
                                    key=lambda item: item['id'])
    return result


def definition(check, registry):
    """Bind task-local check to the entire trusted definition and input contract."""
    reg_id = check.get('registry_id')
    if reg_id is not None and reg_id not in registry:
        raise ValueError('Unknown registry check')
    return {'check': copy.deepcopy(check), 'registry': copy.deepcopy(registry.get(reg_id))}


def snapshot(root, inputs, *, task=None, policy=None, environment=None):
    """Read explicit inputs, recording absent paths and directory membership.

    Reject symlinks in every ancestor and nested entry, special files, work,
    noncanonical paths, and file/directory type mismatches. No relocation is
    inferred; evidence relocation belongs to the lifecycle move-map validator.
    """
    root = root if hasattr(root, "view") else Path(root).absolute()
    for parent in (root, *root.parents):
        if parent.is_symlink():
            raise ValueError('Symlink project root')
    values = {}

    def record(key, value):
        if key in values:
            raise ValueError('Duplicate input: ' + key)
        values[key] = value

    def inspect(path):
        if path.is_symlink():
            raise ValueError('Symlink input: ' + str(path))
        if path.is_file():
            return {'type': 'file', 'sha256': 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()}
        if path.is_dir():
            return {'type': 'directory', 'entries': {
                p.name: inspect(p) for p in sorted(path.iterdir())}}
        if path.exists():
            raise ValueError('Special input: ' + str(path))
        return {'type': 'absent'}

    for inp in inputs:
        spec = {'type': 'path', 'path': inp} if isinstance(inp, str) else inp
        kind = spec['type']
        if kind in {'file', 'directory', 'path'}:
            rel = _relative(spec['path'])
            if rel.split('/')[0] == 'work':
                raise ValueError('work is not a source input')
            path = root
            for part in rel.split('/'):
                if path.exists() and not path.is_dir():
                    raise ValueError('Input ancestor is not a directory')
                path = path / part
                if path.is_symlink():
                    raise ValueError('Symlink ancestor')
            value = inspect(path)
            if kind != 'path' and value['type'] not in {kind, 'absent'}:
                raise ValueError('Input type mismatch')
            record('path:' + rel, value)
        elif kind in {'task_projection', 'policy_projection'}:
            value = task if kind == 'task_projection' else policy
            if value is None:
                raise ValueError('Missing projection')
            record(kind, copy.deepcopy(value))
        elif kind == 'environment':
            for name in spec['names']:
                if environment is None or name not in environment:
                    raise ValueError('Unknown environment fact: ' + name)
                record('environment:' + name, copy.deepcopy(environment[name]))
        else:
            raise ValueError('Unknown input type')
    return {'files': {key: fingerprint(value) for key, value in sorted(values.items())}}


def compare_snapshots(before, after):
    """Unknown/opaque snapshots never prove independence."""
    if (not isinstance(before, dict) or not isinstance(after, dict)
            or not isinstance(before.get('files'), dict)
            or not isinstance(after.get('files'), dict)
            or not before['files'] or not after['files']):
        return {'fresh': False, 'changed': ['unknown inputs']}
    a, b = before['files'], after['files']
    changed = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    return {'fresh': not changed, 'changed': changed}


def reusable(result, *, definition_hash, input_snapshot, environment, policy_hash):
    """Caller supplies compatible policy hash; global plan revisions may differ.

    The runner supplies the result policy_binding as policy_hash in this view;
    the global report hash continues to describe the entire run.
    """
    return (result.get('status') == 'passed'
            and result.get('definition_hash') == definition_hash
            and result.get('policy_hash') == policy_hash
            and result.get('environment') == environment
            and compare_snapshots(result.get('input_snapshot'), input_snapshot)['fresh']
            and ((result.get('kind') == 'manual'
                  and compare_snapshots(result.get('candidate_snapshot'),
                                        result.get('input_snapshot'))['fresh']) or
                 (result.get('kind') == 'command' and result.get('exit_code') == 0
                  and compare_snapshots(result.get('snapshot_before'),
                                        result.get('input_snapshot'))['fresh']
                  and compare_snapshots(result.get('snapshot_after'),
                                        result.get('input_snapshot'))['fresh'])))


def coverage(required, results):
    """Evaluate already validated, fresh, compatible results keyed by stable ID.

    Results is a map, not an unordered report history: the runner must select
    the latest current result per ID. A failed required ID cannot be hidden by
    a passed result of a different ID. Exemptions never create passed evidence.
    """
    states = {}
    for ident, app in sorted(required.items()):
        if not app['required']:
            states[ident] = 'not_applicable'
        else:
            status = results.get(ident, {}).get('status', 'not_run')
            states[ident] = status if status in {'passed', 'failed', 'timeout', 'not_run'} else 'not_run'
    return {'checks': states, 'satisfied': all(
        state in {'passed', 'not_applicable'} for state in states.values())}


def exclusion_allowed(check_id, decisions, failed_history):
    """Failed history is immutable; approval permits exclusion, never success."""
    if check_id not in failed_history:
        return True
    return any(d.get('check_id') == check_id and d.get('decision') == 'not_applicable'
               and d.get('reason') and d.get('actor') and d.get('at')
               and isinstance(d.get('engineer_approval'), dict)
               and all(d['engineer_approval'].get(k) for k in ('who', 'at', 'source', 'detail'))
               for d in decisions)


def required_checks(settings, plan, *, baseline, current, checkpoint=None,
                    failed_history=()):
    """Resolve project obligations and task checks without weakening either.

    Project IDs and task-local IDs are distinct namespaces. A required project
    definition must be referenced by a task check; omission is an explicit
    error, even if another command passed. A plan-local applicability may add
    obligations, never cancel project requirements.
    """
    registry = {item['id']: item for item in settings['registry']}
    required, errors = {}, []
    common = dict(scope=plan['scope'], baseline=baseline, current=current,
                  checkpoint=checkpoint)
    cp_ids = settings.get('checkpoints', {}).get(checkpoint, [])
    project = {}
    for ident, item in sorted(registry.items()):
        app = applicability(item.get('applicability', 'always'), check_id=ident,
                            checkpoint_checks=cp_ids, **common)
        if checkpoint is not None and ident in cp_ids:
            app = {'required': True, 'reason': 'checkpoint membership'}
        project[ident] = app
    referenced = set()
    for check in plan['checks']:
        ident, reg_id = check['id'], check.get('registry_id')
        if ident in required:
            raise ValueError('Duplicate check ID')
        if reg_id is not None and reg_id not in registry:
            raise ValueError('Unknown registry dependency')
        if reg_id:
            referenced.add(reg_id)
        local = applicability(check.get('applicability', 'always'), **common)
        parent = project.get(reg_id, {'required': False, 'reason': 'inline scenario'})
        app = {'required': parent['required'] or local['required'],
               'reason': parent['reason'] + '; task: ' + local['reason']}
        required[ident] = app
    for ident, app in project.items():
        if app['required'] and ident not in referenced:
            errors.append('Missing required registry check: ' + ident)
    for ident in failed_history:
        if ident not in required or not required[ident]['required']:
            if not exclusion_allowed(ident, plan.get('decisions', []), failed_history):
                errors.append('Unapproved exclusion of failed check: ' + ident)
    return {'checks': required, 'errors': errors, 'ready': not errors}


def project_files(root, *, exclude=()):
    """Content baseline independent of Git, with explicit runtime exclusions.

    ViewPath is retained so lifecycle operations can recheck the complete read
    set and directory membership immediately before writing.
    """
    result = {}
    excluded = set(exclude)
    def walk(directory):
        for path in sorted(directory.iterdir()):
            rel = path.relative_to(root).as_posix()
            if (path.name in {'.git', '__pycache__', '.pytest_cache', '.DS_Store'}
                    or rel == 'work' or any(rel == p or rel.startswith(p + '/') for p in excluded)):
                continue
            if path.is_symlink():
                raise ValueError('Symlink in baseline: ' + rel)
            if path.is_dir():
                result[rel] = 'directory'
                walk(path)
            elif path.is_file():
                result[rel] = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                raise ValueError('Special file in baseline: ' + rel)
    walk(root)
    return result


def baseline_files(root, metadata):
    baseline = metadata.get('workflow', {}).get('baseline')
    if baseline is None:
        return None
    rel = _relative(baseline['source'])
    path = root
    for part in rel.split('/'):
        path = path / part
        if path.is_symlink():
            raise ValueError('Symlink baseline')
    content = path.read_bytes()
    if 'sha256:' + hashlib.sha256(content).hexdigest() != baseline['sha256']:
        raise ValueError('Baseline checksum mismatch')
    value = json.loads(content)
    if value.get('schema_version') != 1 or not isinstance(value.get('files'), dict):
        raise ValueError('Invalid baseline')
    return value['files']


def applicability_files(root, rel, text, metadata, index_rel):
    """Baseline diff excludes only known task service writes, not requirements.

    The baseline reference cannot include its own digest. Attribution, state,
    generated index and baseline container are service operations; ordinary
    evidence files and substantive task fields remain visible to path policy.
    """
    baseline_source = (metadata.get('workflow', {}).get('baseline') or {}).get('source')
    if not baseline_source:
        baseline_source = str(Path(rel).parent / (Path(rel).stem + '-evidence/baseline.json'))
    files = project_files(root, exclude=(index_rel, str(Path(rel).with_suffix('.meta.json')), baseline_source))
    files.pop(str(Path(rel).parent / (Path(rel).stem + '-evidence')), None)
    service_text = re.sub(r'^(Executor|Model):.*$', '', text, flags=re.M)
    projection = task_projection(service_text, metadata)
    projection['metadata']['workflow'].pop('baseline', None)
    files[rel] = fingerprint(projection)
    return files
