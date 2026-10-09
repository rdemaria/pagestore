"""Durable filesystem publication and per-signal coordination."""

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4
from weakref import WeakValueDictionary

from ..errors import (
    CommitOutcomeUnknownError,
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
            if fs.startswith("fuse") and "eos" in " ".join(fields).lower():
                fs = "fuse.eos"
            matches.append((len(mount), fs))
    return max(matches)[1] if matches else "unknown"


class FileBackend:
    def __init__(self, url, *, writable=True, lock_timeout=30.0):
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
            else "nfs" if fs.startswith("nfs") else "eos" if "eos" in fs else "generic"
        )
        profile = profile or detected
        if profile not in {"local", "nfs", "eos", "generic"}:
            raise UnsupportedBackendError(f"Unknown filesystem profile: {profile}")
        if writable and profile == "eos":
            raise UnsupportedBackendError(
                "Writable EOS needs a qualified namespace adapter; this release supports local/NFS filesystem operations"
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
        if family not in {"mkdir", "flock"}:
            raise UnsupportedBackendError(f"Unknown coordination family: {family}")
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

    def read(self, key):
        data = self.path(key).read_bytes()
        self.metrics["read_bytes"] += len(data)
        return data

    def exists(self, key):
        return self.path(key).exists()

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
