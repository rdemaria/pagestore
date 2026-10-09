"""Durable filesystem publication and per-signal coordination."""

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import re
import stat
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4
from weakref import WeakValueDictionary

from ..errors import (
    CommitOutcomeUnknownError,
    CorruptionError,
    LockTimeoutError,
    UnsupportedBackendError,
)

_mutexes = WeakValueDictionary()
_mutex_guard = threading.Lock()
_LOCAL = {
    "ext2",
    "ext3",
    "ext4",
    "xfs",
    "btrfs",
    "zfs",
    "tmpfs",
    "overlay",
    "ramfs",
    "apfs",
}


@dataclass(frozen=True)
class BackendInfo:
    transport: str
    filesystem: str
    profile: str
    coordination: str
    publication: str = "atomic-replace"


def _mount_type(path):
    resolved = str(Path(path).resolve())
    matches = []
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return "unknown"
    for line in lines:
        left, right = line.split(" - ", 1)
        mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), left.split()[4])
        if (
            mount == "/"
            or resolved == mount
            or resolved.startswith(mount.rstrip("/") + "/")
        ):
            fields = right.split()
            fs = fields[0]
            if (
                fs.startswith("fuse")
                and "sshfs" not in fs
                and "eos" in " ".join(fields).lower()
            ):
                fs = "fuse.eos"
            matches.append((len(mount), fs))
    return max(matches)[1] if matches else "unknown"


class FileBackend:
    init_coordination = "mkdir"

    def __init__(
        self, url, *, writable=True, lock_timeout=30.0, visibility_timeout=30.0
    ):
        raw = os.fspath(url)
        profile = None
        if isinstance(url, str) and "://" in raw:
            parsed = urlsplit(raw)
            if parsed.scheme != "file":
                raise UnsupportedBackendError(
                    f"Transport {parsed.scheme!r} is not implemented; use a filesystem path"
                )
            if parsed.netloc not in ("", "localhost") or parsed.fragment:
                raise ValueError(
                    "file URLs require an empty/localhost host and no fragment"
                )
            query = parse_qs(parsed.query)
            if set(query) - {"profile"} or len(query.get("profile", [])) > 1:
                raise ValueError("Only a single ?profile= override is supported")
            profile = query.get("profile", [None])[0]
            raw = unquote(parsed.path)
        self.root = Path(raw).resolve()
        fs = _mount_type(self.root)
        detected = (
            "local"
            if fs in _LOCAL
            else (
                "nfs"
                if fs.startswith("nfs")
                else "sshfs" if "sshfs" in fs else "eos" if "eos" in fs else "generic"
            )
        )
        profile = profile or detected
        self._weak_visibility = detected in {"eos", "sshfs"} or profile in {
            "eos",
            "sshfs",
        }
        self.visibility_timeout = float(visibility_timeout)
        if not 0 <= self.visibility_timeout < float("inf"):
            raise ValueError("visibility_timeout must be finite and nonnegative")
        if profile not in {"local", "nfs", "eos", "sshfs", "generic"}:
            raise UnsupportedBackendError(f"Unknown filesystem profile: {profile}")
        if writable and (detected in {"eos", "sshfs"} or profile in {"eos", "sshfs"}):
            raise UnsupportedBackendError(
                "EOS/SSHFS mount close is not a remote commit; supply xrootd_url="
                " with the authoritative URL of this store, or open that root:// URL directly"
            )
        if writable and profile == "local" and detected in {"nfs", "eos"}:
            raise UnsupportedBackendError(
                "Cannot use local flock coordination on detected network storage"
            )
        self.info = BackendInfo(
            "filesystem", fs, profile, "flock" if profile == "local" else "mkdir"
        )
        self.writable = writable
        self.lock_timeout = float(lock_timeout)
        if not 0 <= self.lock_timeout < float("inf"):
            raise ValueError("lock_timeout must be finite and nonnegative")
        self.metrics = {"read_bytes": 0, "written_bytes": 0, "publications": 0}

    def bind_coordination(self, family):
        if family not in {"mkdir", "flock", "xrootd-exclusive", "eos-mkdir"}:
            raise UnsupportedBackendError(f"Unknown coordination family: {family}")
        if self.writable and family in {"xrootd-exclusive", "eos-mkdir"}:
            raise UnsupportedBackendError(
                "This store requires XRootD coordination; supply xrootd_url"
            )
        if self.writable and family == "flock" and self.info.coordination != "flock":
            raise UnsupportedBackendError(
                "This database requires local flock; its current filesystem needs directory locks"
            )
        if family != self.info.coordination:
            self.info = BackendInfo(
                self.info.transport, self.info.filesystem, self.info.profile, family
            )

    def path(self, key):
        p = Path(key)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError("Storage keys must be relative and cannot contain '..'")
        return self.root / p

    def _require_write(self):
        if not self.writable:
            raise PermissionError("Database is read-only")

    @staticmethod
    def _sync_dir(path):
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def mkdir(self, path):
        self._require_write()
        missing = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            try:
                directory.mkdir()
            except FileExistsError:
                pass
            self._sync_dir(directory.parent)

    def read(self, key, *, expected=None):
        def attempt():
            data = self.path(key).read_bytes()
            self.metrics["read_bytes"] += len(data)
            if self._weak_visibility:
                from ..catalog import digest, unpack

                if expected is not None and digest(data) != expected:
                    raise CorruptionError(f"Mounted file checksum mismatch: {key}")
                if key.endswith(".json"):
                    unpack(data)
            return data

        return self._visible_read(attempt, missing=expected is not None)

    def _visible_read(self, operation, *, missing=False):
        if not self._weak_visibility:
            return operation()
        deadline = time.monotonic() + self.visibility_timeout
        delay = 0.01
        while True:
            try:
                return operation()
            except FileNotFoundError:
                if not missing or time.monotonic() >= deadline:
                    raise
            except CorruptionError:
                if time.monotonic() >= deadline:
                    raise
            time.sleep(min(delay, max(0, deadline - time.monotonic())))
            delay = min(0.5, delay * 2)

    def read_page(self, key, *, full=False, expected=None):
        from ..page_format import decode, read_page

        if self._weak_visibility:
            # A mount may fill or replace cache files after close. Decode an owned
            # snapshot, never expose a live mmap of that cache to the caller.
            return self._visible_read(
                lambda: decode(
                    self.path(key).read_bytes(), full=True, expected=expected
                ),
                missing=expected is not None,
            )
        return read_page(self.path(key), full=full, expected=expected)

    def listdir(self, key=""):
        return [p.name for p in self.path(key).iterdir()]

    def glob(self, pattern):
        return (p.relative_to(self.root).as_posix() for p in self.root.glob(pattern))

    def exists(self, key):
        return self.path(key).exists()

    def disk_usage(self):
        """Return total allocated bytes and basis, falling back to file lengths.

        Count directories and every file, including hidden/temporary files. Do
        not follow symlinks; count filesystem hard links once. A disappearing
        child is skipped, but missing roots and permission errors propagate.
        """
        allocated = logical = 0
        have_blocks = not self._weak_visibility
        hardlinks = set()

        def account(info):
            nonlocal allocated, logical, have_blocks
            is_dir = stat.S_ISDIR(info.st_mode)
            if not is_dir and info.st_nlink > 1:
                identity = info.st_dev, info.st_ino
                if identity in hardlinks:
                    return
                hardlinks.add(identity)
            blocks = getattr(info, "st_blocks", None)
            if blocks is None or blocks < 0:
                have_blocks = False
            else:
                allocated += blocks * 512
            if not is_dir:
                logical += info.st_size

        account(self.root.stat())
        stack = [self.root]
        while stack:
            directory = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        account(info)
                        if stat.S_ISDIR(info.st_mode):
                            stack.append(entry.path)
            except FileNotFoundError:
                if directory == self.root:
                    raise
        return (allocated, "allocated") if have_blocks else (logical, "logical")

    def publish(self, key, chunks, *, replace=False):
        """Write final bytes once, sync, then expose the complete file by rename."""
        self._require_write()
        path = self.path(key)
        self.mkdir(path.parent)
        pending = path.with_name(f".{uuid4().hex}.pending")
        try:
            with pending.open("xb") as stream:
                for chunk in chunks:
                    stream.write(chunk)
                    self.metrics["written_bytes"] += len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if not replace and path.exists():
                raise FileExistsError(path)
            os.replace(pending, path)
            self._sync_dir(path.parent)
            self.metrics["publications"] += 1
        finally:
            pending.unlink(missing_ok=True)

    def remove(self, key):
        """Durably remove an owned, unreferenced metadata file."""
        self._require_write()
        path = self.path(key)
        path.unlink(missing_ok=True)
        self._sync_dir(path.parent)

    def link(self, source, key):
        """Publish a hard link to a verified, quiescent immutable source page.

        Explicitly requested by salvage only. Never fall back to a potentially
        enormous copy when linking is unsupported or crosses filesystems.
        """
        self._require_write()
        path = self.path(key)
        self.mkdir(path.parent)
        os.link(source, path, follow_symlinks=False)
        self._sync_dir(path.parent)
        self.metrics["publications"] += 1

    @contextmanager
    def lock(self, key, *, family=None):
        self._require_write()
        family = family or self.info.coordination
        path = self.path(key + (".lock" if family == "flock" else ".LOCK"))
        self.mkdir(path.parent)
        identity = str(path)
        with _mutex_guard:
            mutex = _mutexes.setdefault(identity, threading.Lock())
        deadline = time.monotonic() + self.lock_timeout
        if not mutex.acquire(timeout=self.lock_timeout):
            raise LockTimeoutError(f"Timed out acquiring {path}")
        handle = None
        acquired = False
        retain_directory = False
        try:
            if family == "flock":
                try:
                    import fcntl
                except ImportError as exc:
                    raise UnsupportedBackendError(
                        "flock is unavailable on this platform"
                    ) from exc
                handle = path.open("a+b")  # Never unlink this inode.
            while True:
                try:
                    if family == "flock":
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:
                        path.mkdir()  # Never steal a directory lock based on age.
                    acquired = True
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EEXIST, errno.EAGAIN, errno.EACCES}:
                        raise
                    if time.monotonic() >= deadline:
                        raise LockTimeoutError(
                            f"Timed out acquiring {path}; directory locks require manual cleanup after a dead writer"
                        ) from exc
                    time.sleep(min(0.02, max(0, deadline - time.monotonic())))
            yield
        except CommitOutcomeUnknownError:
            # A remote namespace request may still be outstanding. Do not allow a
            # successor writer until the outcome has been reconciled offline.
            retain_directory = True
            raise
        finally:
            if handle is not None:
                handle.close()
            elif acquired and not retain_directory:
                path.rmdir()
            mutex.release()
