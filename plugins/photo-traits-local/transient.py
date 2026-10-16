"""Private disposable sessions on one POSIX host; no legacy-root adoption.

An open flock lease, not a PID or a timestamp, protects each active session.
Only marked sessions with the exact generated file layout may be removed.
All deletion is descriptor-relative and refuses symlinks and hardlinks.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import stat
import weakref

ROOT_MARKER = '.phototraits-transient-v1'
ROOT_CONTENT = b'PhotoTraits disposable sessions v1\n'
LEASE = '.phototraits-session-v1'
HEX = r'[a-f0-9]{32}'
EVENT = HEX + r'-(plan|confirmation|cancel|human-review|export-generated)\.json'
RUN_FILE = r'plan\.json|' + HEX + r'-(attempt|response|result)\.json'


class UnsafeStorage(ValueError):
    pass


def require(condition):
    if not condition:
        raise UnsafeStorage('Temporary storage ownership or layout could not be verified.')


def private(info, directory=False):
    require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            and info.st_uid == os.getuid() and not info.st_mode & 0o077
            and (directory or info.st_nlink == 1))


def same(a, b):
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


@contextmanager
def directory(name, parent=None):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def absolute_directory(path):
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            require(part not in ('.', '..'))
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def read_marker(parent, name, expected):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        private(os.fstat(fd))
        require(os.read(fd, len(expected) + 1) == expected)
    finally:
        os.close(fd)


def write_marker(parent, name, data):
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(os.write(fd, data) == len(data))
        os.fsync(fd)
        return Lease(fd)
    except BaseException:
        os.close(fd)
        raise


class Lease:
    """Close the OS lock even when an unused engine is garbage collected."""
    def __init__(self, fd):
        self.fd = fd
        self._close = weakref.finalize(self, os.close, fd)

    def fileno(self):
        require(self._close.alive)
        return self.fd

    def close(self):
        self._close()


class SessionRoot:
    def __init__(self, root):
        self.path = Path(root).absolute()
        require('..' not in self.path.parts and self.path.resolve() == self.path)
        # An existing, even empty, unmarked directory is never adopted.
        with absolute_directory(self.path.parent) as parent:
            try:
                os.mkdir(self.path.name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            else:
                with directory(self.path.name, parent) as fd:
                    write_marker(fd, ROOT_MARKER, ROOT_CONTENT).close()
            with directory(self.path.name, parent) as fd:
                self.identity = os.fstat(fd)
        with self.open():
            pass

    @contextmanager
    def open(self):
        # Open every ancestor without following a link, including under races.
        with absolute_directory(self.path) as fd:
            private(os.fstat(fd), directory=True)
            require(same(self.identity, os.fstat(fd)))
            read_marker(fd, ROOT_MARKER, ROOT_CONTENT)
            yield fd

    @staticmethod
    def marker(sid):
        require(isinstance(sid, str) and re.fullmatch(HEX, sid))
        return ('PhotoTraits disposable session v1 ' + sid + '\n').encode('ascii')

    def create(self, sid):
        content = self.marker(sid)
        with self.open() as root:
            os.mkdir(sid, mode=0o700, dir_fd=root)
            with directory(sid, root) as fd:
                lease = write_marker(fd, LEASE, content)
        return self.path / sid, lease

    def _layout(self, fd, run=False):
        """Validate the whole bounded layout before deleting any of its files."""
        private(os.fstat(fd), directory=True)
        entries = {}
        for name in os.listdir(fd):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not run and re.fullmatch(HEX, name) and stat.S_ISDIR(info.st_mode):
                with directory(name, fd) as child:
                    entries[name] = (info, self._layout(child, run=True))
            else:
                allowed = re.fullmatch(RUN_FILE, name) if run else (
                    name == LEASE or re.fullmatch(EVENT, name) or re.fullmatch(HEX + r'\.jpg', name))
                require(allowed)
                private(info)
                entries[name] = (info, None)
        return entries

    def _delete(self, fd, entries):
        for name, (info, children) in entries.items():
            if name == LEASE:
                continue  # Ownership survives a partial failure; retry remains safe.
            require(same(info, os.stat(name, dir_fd=fd, follow_symlinks=False)))
            if children is None:
                os.unlink(name, dir_fd=fd)
            else:
                with directory(name, fd) as child:
                    require(same(info, os.fstat(child)))
                    self._delete(child, children)
                os.rmdir(name, dir_fd=fd)

    @contextmanager
    def session(self, sid, lease):
        content = self.marker(sid)
        with self.open() as root, directory(sid, root) as fd:
            private(os.fstat(fd), directory=True)
            require(same(os.fstat(lease.fileno()), os.stat(LEASE, dir_fd=fd, follow_symlinks=False)))
            read_marker(fd, LEASE, content)
            yield root, fd

    def purge_photos(self, sid, lease, handles):
        with self.session(sid, lease) as (_, fd):
            entries = {}
            for handle in handles:
                require(isinstance(handle, str) and re.fullmatch(HEX, handle))
                name = handle + '.jpg'
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                private(info)
                entries[name] = (info, None)
            self._delete(fd, entries)

    def remove(self, sid, lease):
        """Caller must hold this session's lease and have stopped all writers."""
        with self.session(sid, lease) as (root, fd):
            info = os.fstat(fd)
            layout = self._layout(fd)
            self._delete(fd, layout)
            require(os.listdir(fd) == [LEASE])
            require(same(info, os.stat(sid, dir_fd=root, follow_symlinks=False)))
            os.unlink(LEASE, dir_fd=fd)
            os.rmdir(sid, dir_fd=root)

    def sweep(self):
        """Reap abandoned marked sessions only; a held lease always wins."""
        removed = 0
        with self.open() as root:
            for sid in os.listdir(root):
                if not re.fullmatch(HEX, sid):
                    continue
                try:
                    with directory(sid, root) as fd:
                        private(os.fstat(fd), directory=True)
                        lock_fd = os.open(LEASE, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                        with os.fdopen(lock_fd, 'r+b') as lease:
                            private(os.fstat(lock_fd))
                            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            self.remove(sid, lease)
                            removed += 1
                except (OSError, UnsafeStorage):
                    # Active, unmarked, redirected, or unknown layout: leave it intact.
                    continue
        return removed
