"""Scoped preparation helpers; durable operation ownership stays in the engine."""

import hashlib
import os
import re
import shutil
import stat
import sys

import project_view

HASH_RE = re.compile(r'sha256:[0-9a-f]{64}\Z')


class Payload:
    """A bounded byte source bound to the digest of the stream that was read."""

    def __init__(self, source, digest=None, mode=None):
        if not isinstance(source, project_view.ViewPath):
            source = project_view._ordinary_path(source)
            if not source.is_file():
                raise ValueError('payload source must be an ordinary file')
        self.source = source
        self.source_mode = stat.S_IMODE(source.stat().st_mode)
        self.mode = self.source_mode if mode is None else mode
        if type(self.mode) is not int or not 0 <= self.mode <= 0o7777:
            raise ValueError("invalid payload mode")
        self.sha256 = digest or file_digest(source)
        if not isinstance(self.sha256, str) or not HASH_RE.fullmatch(self.sha256):
            raise ValueError('invalid payload digest')

    def copy_to(self, outgoing):
        checksum = hashlib.sha256()
        if stat.S_IMODE(self.source.stat().st_mode) != self.source_mode:
            raise ValueError(f"payload mode changed: {self.source}")
        with open_binary(self.source) as incoming:
            for chunk in iter(lambda: incoming.read(project_view.CHUNK_SIZE), b''):
                checksum.update(chunk)
                outgoing.write(chunk)
        if (stat.S_IMODE(self.source.stat().st_mode) != self.source_mode
                or 'sha256:' + checksum.hexdigest() != self.sha256):
            raise ValueError(f'payload changed while copying: {self.source}')


def open_binary(path):
    if isinstance(path, project_view.ViewPath):
        return path.view.open_binary(path.rel)
    return path.open('rb')


def file_digest(path):
    if not isinstance(path, project_view.ViewPath):
        path = project_view._ordinary_path(path)
    checksum = hashlib.sha256()
    with open_binary(path) as incoming:
        for chunk in iter(lambda: incoming.read(project_view.CHUNK_SIZE), b''):
            checksum.update(chunk)
    return 'sha256:' + checksum.hexdigest()


def restore_read_set(root, snapshot):
    read_set = project_view.ReadSet(root)
    if snapshot is None:
        return read_set
    required = {'files', 'states', 'collections'}
    if not isinstance(snapshot, dict) or set(snapshot) not in (required, required | {'links'}):
        raise ValueError('invalid read-set shape')
    for name, values in snapshot.items():
        if not isinstance(values, dict):
            raise ValueError('invalid read-set collection')
        for rel, value in values.items():
            if not isinstance(rel, str) or project_view._relative(rel) != rel:
                raise ValueError('invalid read-set path')
            if name == 'links':
                if value is not False:
                    raise ValueError('invalid read-set symlink guard')
            elif name == 'states':
                if not isinstance(value, (list, tuple)) or not value:
                    raise ValueError('invalid read-set state')
                if value[0] == 'absent':
                    if len(value) != 1:
                        raise ValueError('invalid absent state')
                elif (len(value) != 2 or value[0] not in {'dir', 'file', 'other'}
                      or type(value[1]) is not int or not 0 <= value[1] <= 0o7777):
                    raise ValueError('invalid read-set file state')
                value = tuple(value)
            elif name == 'collections':
                if value is not None:
                    if not isinstance(value, (list, tuple)):
                        raise ValueError('invalid read-set members')
                    members = []
                    for item in value:
                        if (not isinstance(item, (list, tuple)) or len(item) != 2
                                or not isinstance(item[0], str)
                                or item[0] in {'', '.', '..'} or '/' in item[0] or '\\' in item[0]
                                or item[1] not in {'file', 'dir', 'symlink', 'other'}):
                            raise ValueError('invalid read-set member')
                        members.append(tuple(item))
                    if members != sorted(set(members)):
                        raise ValueError('duplicate or unordered read-set members')
                    value = tuple(members)
            elif not isinstance(value, str) or not HASH_RE.fullmatch(value):
                raise ValueError('invalid read-set hash')
            read_set.remember(name, rel, value)
    return read_set


def prepare(root, op_dir, plan, safe_path, require, journal, verify_expected):
    read_set = restore_read_set(root, plan.get('read_set'))
    read_set.recheck()
    live = project_view.ProjectView(root, read_set)
    for rel in plan['inputs']:
        live.state(rel)
    staged = op_dir / 'staged'
    staged.mkdir()
    for rel, content in plan['writes'].items():
        target = staged / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, Payload):
            original = content.source
            if isinstance(original, project_view.ViewPath):
                original, _ = original.view.source(original.rel)
            if original is not None and original.is_relative_to(root):
                dependency = original.relative_to(root).as_posix()
                current_state = live.state(dependency)
                if current_state != ("file", content.source_mode):
                    raise ValueError(f"payload source mode changed: {dependency}")
                read_set.remember('files', dependency, content.sha256)
        with target.open('xb') as outgoing:
            if isinstance(content, Payload):
                content.copy_to(outgoing)
            else:
                outgoing.write(content)
            outgoing.flush()
            if isinstance(content, Payload):
                os.chmod(target, content.mode)
            os.fsync(outgoing.fileno())
    for rel in plan["create_dirs"]:
        (staged / rel).mkdir(parents=True, exist_ok=True)
    plan["_staged_modes"] = {rel: stat.S_IMODE((staged / rel).stat().st_mode)
                             for rel in plan["writes"]}
    view = project_view.FutureView(root, staged, plan['writes'], plan['removes'],
                                   plan['create_dirs'], plan['remove_dirs'], read_set=read_set)
    verify_expected(view.path, plan['expected'], safe_path, require)
    for check in plan['staged_checks']:
        check(view.path)
    read_set.recheck()
    journal(op_dir, 'staged')
    plan['_future_view'] = view
    return view


def recheck(root, plan, safe_path, require, verify_expected):
    view = plan['_future_view']
    view.read_set.recheck()
    verify_expected(view.path, plan['expected'], safe_path, require)
    for rel, mode in plan["_staged_modes"].items():
        if stat.S_IMODE((view.staged / rel).stat().st_mode) != mode:
            raise ValueError(f"staged mode changed: {rel}")


def validate_backup(index, plan, require):
    drift = [rel for rel, digest in index['hashes'].items()
             if plan['inputs'].get(rel) != digest]
    require(not drift, 'backup does not match planned originals: ' + ', '.join(drift))


def cleanup_staged(op_dir):
    staged = op_dir / 'staged'
    try:
        if staged.is_symlink():
            raise ValueError('staged became a symlink')
        if staged.exists():
            shutil.rmtree(staged)
    except (OSError, ValueError) as error:
        warning = f'staged cleanup failed: {error}'
        print(warning, file=sys.stderr)
        return warning
    return None


def physical_root(root):
    """Service state is outside the tracked document read-set."""
    if isinstance(root, project_view.ViewPath):
        if root.rel != '.':
            raise ValueError('operation requires the project root')
        return root.view.root
    return root


def bind_planning_reads(root, spec):
    if not isinstance(root, project_view.ViewPath):
        return root, spec
    physical = physical_root(root)
    if not isinstance(spec, dict):
        return physical, spec
    combined = restore_read_set(physical, spec.get('read_set'))
    for name, values in root.view.read_set.snapshot().items():
        for rel, value in values.items():
            combined.remember(name, rel, value)
    return physical, {**spec, 'read_set': combined.snapshot()}
