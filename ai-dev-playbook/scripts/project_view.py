"""Read-only project overlays and bounded, disk-backed payload preparation.

ViewPath deliberately refuses filesystem coercion: validators must keep the
view when reading, rather than accidentally falling through to the live tree.
"""
import contextlib
import fnmatch
import hashlib
import os
import shutil
import stat
from pathlib import Path

CHUNK_SIZE = 1024 * 1024


def digest_file(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(CHUNK_SIZE), b''):
            h.update(block)
    return 'sha256:' + h.hexdigest()


def _ordinary_path(value):
    """Require canonical staging/source paths without following any symlink."""
    path = Path(value).absolute()
    if ".." in path.parts:
        raise ValueError(f"noncanonical payload path: {value}")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f'symlink in payload path: {value}')
    return path


def _relative(value):
    path = Path(value)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError(f'unsafe project path: {value}')
    return path.as_posix()


class ReadSet:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.files = {}
        self.states = {}
        self.collections = {}
        self.links = {}

    def remember(self, group, rel, value):
        target = getattr(self, group)
        if rel in target and target[rel] != value:
            raise ValueError(f'input changed during preparation: {rel}')
        target[rel] = value

    def snapshot(self):
        return {name: dict(getattr(self, name))
                for name in ('files', 'states', 'collections', 'links')}

    def recheck(self):
        _ordinary_path(self.root)
        for rel in self.links:
            _ordinary_path(self.root / rel)
        for rel, expected in self.states.items():
            _ordinary_path(self.root / rel)
            if _state(self.root / rel) != expected:
                raise ValueError(f'stale input state: {rel}')
        for rel, expected in self.files.items():
            path = _ordinary_path(self.root / rel)
            if _state(path)[0] != 'file' or digest_file(path) != expected:
                raise ValueError(f'stale input bytes: {rel}')
        for rel, expected in self.collections.items():
            _ordinary_path(self.root / rel)
            if _members(self.root / rel) != expected:
                raise ValueError(f'stale collection: {rel}')


def _state(path):
    try:
        s = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return ('absent',)
    if stat.S_ISLNK(s.st_mode):
        return ('symlink', os.readlink(path))
    kind = 'file' if stat.S_ISREG(s.st_mode) else 'dir' if stat.S_ISDIR(s.st_mode) else 'other'
    return (kind, stat.S_IMODE(s.st_mode))


def _members(path):
    if not path.exists():
        return None
    if not path.is_dir() or path.is_symlink():
        raise ValueError(f'not an ordinary collection: {path}')
    return tuple(sorted((p.name, _state(p)[0]) for p in path.iterdir()))


class ProjectView:
    def __init__(self, root, read_set=None):
        self.root = Path(root).resolve()
        self.read_set = read_set or ReadSet(self.root)
        if self.read_set.root != self.root:
            raise ValueError("read-set belongs to another project")
        self.path = ViewPath(self, '.')

    def _live(self, rel):
        rel = _relative(rel)
        _ordinary_path(self.root)
        root_state = _state(self.root)
        self.read_set.remember("states", ".", root_state)
        if root_state[0] != "dir":
            raise ValueError("project root is not an ordinary directory")
        path = self.root
        for part in Path(rel).parts:
            path = path / part
            state = _state(path)
            if state[0] != 'absent':
                self.read_set.remember('states', path.relative_to(self.root).as_posix(), state)
            if state[0] == 'symlink':
                raise ValueError(f'symlink in project path: {rel}')
        return path

    def source(self, rel):
        return self._live(rel), True

    def state(self, rel):
        path, live = self.source(rel)
        value = _state(path) if path is not None else ('absent',)
        if live:
            self.read_set.remember('states', rel, value)
        return value

    def members(self, rel):
        path, live = self.source(rel)
        result = _members(path) if path is not None else None
        if live:
            self.read_set.remember('collections', rel, result)
        return result

    @contextlib.contextmanager
    def open_binary(self, rel):
        path, live = self.source(rel)
        if path is None:
            raise FileNotFoundError(rel)
        if self.state(rel)[0] != 'file':
            raise ValueError(f'not an ordinary file: {rel}')
        h = hashlib.sha256()
        with path.open('rb') as stream:
            reader = _HashReader(stream, h)
            yield reader
            # Complete the same open stream to bind the full file bytes.
            while reader.read(CHUNK_SIZE):
                pass
        if live:
            self.read_set.remember('files', rel, 'sha256:' + h.hexdigest())

    def read_bytes(self, rel):
        with self.open_binary(rel) as stream:
            return stream.read()


class _HashReader:
    def __init__(self, stream, digest):
        self.stream, self.digest = stream, digest

    def read(self, size=-1):
        data = self.stream.read(size)
        self.digest.update(data)
        return data


class FutureView(ProjectView):
    def __init__(self, root, staged, writes=(), removes=(), create_dirs=(), remove_dirs=(), read_set=None):
        super().__init__(root, read_set)
        self.staged = _ordinary_path(staged)
        if self.staged == self.root:
            raise ValueError('staged must be separate from the live root')
        self.writes = {_relative(p) for p in writes}
        self.removes = {_relative(p) for p in removes}
        self.create_dirs = {_relative(p) for p in create_dirs}
        self.remove_dirs = {_relative(p) for p in remove_dirs}
        if '.' in self.writes | self.removes | self.create_dirs | self.remove_dirs:
            raise ValueError('project root cannot be a future action')
        if (self.writes & self.removes or self.create_dirs & self.remove_dirs
                or (self.writes | self.removes) & (self.create_dirs | self.remove_dirs)):
            raise ValueError('contradictory future paths')
        if any(path.startswith(directory + '/')
               for directory in self.remove_dirs
               for path in self.writes | self.create_dirs):
            raise ValueError('removed directory contains a future write')
        live_view = ProjectView(self.root, self.read_set)
        for rel in self.create_dirs:
            if (live_view.path / rel).exists():
                raise ValueError(f'created directory already exists: {rel}')
        for rel in self.removes:
            if (live_view.path / rel).is_dir():
                raise ValueError(f'file/directory collision: {rel}')
        for rel in self.remove_dirs:
            source = live_view.path / rel
            if source.exists() and not source.is_dir():
                raise ValueError(f'file/directory collision: {rel}')
            for child in source.rglob('*'):
                child_rel = child.relative_to(live_view.path).as_posix()
                if child.is_dir():
                    covered = child_rel in self.remove_dirs
                else:
                    covered = child_rel in self.removes
                if not covered:
                    raise ValueError(f'removed directory would not be empty: {rel}: {child_rel}')
        self.virtual_dirs = set(self.create_dirs)
        for rel in self.writes | self.create_dirs:
            self.virtual_dirs.update(p.as_posix() for p in Path(rel).parents)
        for rel in self.virtual_dirs:
            if rel in self.writes or rel in self.removes:
                raise ValueError(f'file/directory collision: {rel}')
            original_state = live_view.state(rel)
            if original_state[0] not in {'absent', 'dir'} and rel not in self.removes:
                raise ValueError(f'file/directory collision: {rel}')
        for rel in self.writes:
            if live_view.state(rel)[0] == 'dir':
                raise ValueError(f'file/directory collision: {rel}')
            path = self.staged / rel
            current = self.staged
            for part in Path(rel).parts:
                current /= part
                if current.is_symlink():
                    raise ValueError(f'symlink in staged: {rel}')
            if not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError(f'payload is missing or linked: {rel}')

    def source(self, rel):
        if rel in self.writes:
            payload = _ordinary_path(self.staged / rel)
            if payload.is_file() and payload.stat().st_nlink != 1:
                raise ValueError(f'payload became linked: {rel}')
            return payload, False
        if rel in self.removes or any(rel == p or rel.startswith(p + '/') for p in self.remove_dirs):
            return None, False
        return super().source(rel)

    def state(self, rel):
        if rel in self.virtual_dirs:
            return ('dir', 0o755)
        return super().state(rel)

    def members(self, rel):
        if self.state(rel)[0] != 'dir':
            return None
        live = self._live(rel)
        original = _members(live)
        self.read_set.remember('collections', rel, original)
        names = {name for name, _ in original or ()}
        for candidate in self.writes | self.virtual_dirs:
            if Path(candidate).parent.as_posix() == rel and candidate != '.':
                names.add(Path(candidate).name)
        result = []
        for name in sorted(names):
            child = (Path(rel) / name).as_posix()
            kind = self.state(child)[0]
            if kind != 'absent':
                result.append((name, kind))
        return tuple(result)


class ViewPath:
    def __init__(self, view, rel):
        self.view, self.rel = view, _relative(rel)

    def __str__(self):
        return str(self.view.root / self.rel)

    def __fspath__(self):
        raise TypeError('ViewPath must not be converted to a live filesystem path')

    def __repr__(self):
        return f'ViewPath({self.rel!r})'

    def __hash__(self):
        return hash((id(self.view), self.rel))

    def __eq__(self, other):
        return isinstance(other, ViewPath) and self.view is other.view and self.rel == other.rel

    def __lt__(self, other):
        return self.rel < other.rel

    def __truediv__(self, other):
        path = Path(os.path.normpath(str(Path(self.rel) / str(other))))
        return ViewPath(self.view, path)

    @property
    def name(self):
        return Path(self.rel).name

    @property
    def stem(self):
        return Path(self.rel).stem

    @property
    def suffix(self):
        return Path(self.rel).suffix

    @property
    def parts(self):
        return (self.view.root / self.rel).parts

    @property
    def parent(self):
        return ViewPath(self.view, Path(self.rel).parent)

    @property
    def parents(self):
        return tuple(ViewPath(self.view, p) for p in Path(self.rel).parents)

    def with_suffix(self, suffix):
        return ViewPath(self.view, Path(self.rel).with_suffix(suffix))

    def with_name(self, name):
        return ViewPath(self.view, Path(self.rel).with_name(name))

    def as_posix(self):
        return str(self).replace(os.sep, '/')

    def relative_to(self, other):
        if isinstance(other, ViewPath):
            if other.view is not self.view:
                raise ValueError('different project views')
            return Path(self.rel).relative_to(other.rel)
        return (self.view.root / self.rel).relative_to(other)

    def is_relative_to(self, other):
        try:
            self.relative_to(other)
            return True
        except ValueError:
            return False

    def resolve(self):
        self.view._live(self.rel)
        return self

    def exists(self):
        return self.view.state(self.rel)[0] != 'absent'

    def is_file(self):
        return self.view.state(self.rel)[0] == 'file'

    def is_dir(self):
        return self.view.state(self.rel)[0] == 'dir'

    def is_symlink(self):
        try:
            path, live = self.view.source(self.rel)
            if live:
                self.view.read_set.remember('links', self.rel, False)
            return path.is_symlink() if path is not None else False
        except ValueError as error:
            if 'symlink' in str(error):
                return True
            raise

    def stat(self):
        self.view.state(self.rel)
        source, _ = self.view.source(self.rel)
        if source is None:
            raise FileNotFoundError(str(self))
        return source.stat()

    def read_bytes(self):
        return self.view.read_bytes(self.rel)

    def read_text(self, encoding='utf-8'):
        return self.read_bytes().decode(encoding)

    def iterdir(self):
        members = self.view.members(self.rel)
        if members is None:
            raise FileNotFoundError(str(self))
        for name, _ in members:
            yield self / name

    def glob(self, pattern):
        if '/' in pattern:
            raise ValueError('only single-level glob is supported')
        if not self.is_dir():
            return
        for child in self.iterdir():
            if fnmatch.fnmatchcase(child.name, pattern):
                yield child

    def rglob(self, pattern):
        if not self.is_dir():
            return
        for child in self.iterdir():
            if fnmatch.fnmatchcase(child.name, pattern):
                yield child
            if child.is_dir() and not child.is_symlink():
                yield from child.rglob(pattern)


def prepare_payload(staged, rel, source):
    """Copy one ordinary payload, streaming; never link or copy a tree."""
    rel = _relative(rel)
    staged, source = _ordinary_path(staged), _ordinary_path(source)
    if source.is_symlink() or not source.is_file():
        raise ValueError('payload source must be an ordinary file')
    destination = staged / rel
    current = staged
    for part in Path(rel).parts:
        current /= part
        if current.is_symlink():
            raise ValueError('symlink in staged destination')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as incoming, destination.open('xb') as outgoing:
        shutil.copyfileobj(incoming, outgoing, CHUNK_SIZE)
    return {'path': rel, 'size': destination.stat().st_size, 'sha256': digest_file(destination)}
