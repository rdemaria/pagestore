"""XRootD transport with server-side publication and profile-specific coordination.

No mounted path, subprocess per file, or local disk staging is involved. Server
sync/close and checksum visibility precede rename; HEAD is confirmed by content.
An ambiguous mutation disables this writer and retains all held locks.
"""

from contextlib import contextmanager
import errno
from fnmatch import fnmatchcase
import hashlib
import math
import os
from pathlib import PurePosixPath
import socket
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import uuid4
import zlib

from .filesystem import BackendInfo
from ..errors import (
    CommitOutcomeUnknownError,
    CorruptionError,
    LockTimeoutError,
    UnsupportedBackendError,
)

# XProtocol/XProtocol.hh server error numbers, not local POSIX errno values.
_ERRORS = {
    3000: errno.EINVAL,
    3001: errno.EINVAL,
    3002: errno.ENAMETOOLONG,
    3003: errno.EAGAIN,
    3006: errno.ENOTSUP,
    3009: errno.ENOSPC,
    3010: errno.EACCES,
    3011: errno.ENOENT,
    3013: errno.ENOTSUP,
    3016: errno.EISDIR,
    3018: errno.EEXIST,
    3021: errno.EDQUOT,
    3025: errno.EROFS,
    3030: errno.EACCES,
}
_CHUNK = 4 * 1024**2


def _bindings():
    # Optional dependency, never loaded by local-only applications.
    try:
        from XRootD import client
        from XRootD.client import flags
    except ImportError as exc:
        raise UnsupportedBackendError(
            "XRootD requires its Python bindings: install pagestore[xrootd]"
        ) from exc
    return client, flags


def _duration(value, name):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return value


class XRootDBackend:
    def __init__(
        self,
        url,
        *,
        writable=True,
        lock_timeout=30.0,
        io_timeout=30,
        visibility_timeout=30.0,
    ):
        parsed = urlsplit(os.fspath(url))
        if (
            parsed.scheme not in {"root", "roots"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not parsed.path.startswith("/")
        ):
            raise ValueError(
                "Expected root://host//absolute/store (without credentials or fragment)"
            )
        # Validate ports now rather than leaving malformed authorities to the client.
        parsed.port
        path = unquote(parsed.path).lstrip("/")
        if (
            not path
            or any(p in {".", ".."} for p in path.split("/"))
            or any(c in path for c in "\x00\r\n?&#")
        ):
            raise ValueError("Invalid XRootD store path")
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        if set(query) - {"profile"} or len(query.get("profile", [])) > 1:
            raise ValueError("Only ?profile=xrootd or ?profile=eos is supported")
        profile = query.get(
            "profile", ["eos" if path.startswith("eos/") else "xrootd"]
        )[0]
        if profile not in {"xrootd", "eos"}:
            raise UnsupportedBackendError(f"Unknown XRootD profile: {profile}")
        if path.startswith("eos/") and profile != "eos":
            raise UnsupportedBackendError(
                "EOS namespaces require the EOS coordination profile"
            )
        self.lock_timeout = _duration(lock_timeout, "lock_timeout")
        self.visibility_timeout = _duration(visibility_timeout, "visibility_timeout")
        if (
            isinstance(io_timeout, bool)
            or int(io_timeout) != io_timeout
            or not 1 <= io_timeout <= 65535
        ):
            raise ValueError(
                "io_timeout must be an integer between 1 and 65535 seconds"
            )
        self.io_timeout = int(io_timeout)
        self.endpoint = f"{parsed.scheme}://{parsed.netloc}"
        self.prefix = "/" + str(PurePosixPath(path))
        self.root = self.endpoint + "/" + quote(self.prefix, safe="/")
        self.info = BackendInfo(
            "xrootd",
            "remote",
            profile,
            "eos-mkdir" if profile == "eos" else "xrootd-exclusive",
            "sync-checksum-rename",
        )
        self.writable = writable
        self.metrics = {"read_bytes": 0, "written_bytes": 0, "publications": 0}
        self._client, self._flags = _bindings()
        self._fs = self._client.FileSystem(self.endpoint)
        self._directories = set()
        self._uncertain = False
        self.init_coordination = self.info.coordination

    def _path(self, key):
        if (
            not isinstance(key, str)
            or key.startswith("/")
            or any(p in {".", ".."} for p in key.split("/"))
            or any(c in key for c in "\x00\r\n?&#")
        ):
            raise ValueError("Storage keys must be relative namespace paths")
        return self.prefix + ("/" + key if key else "")

    def _url(self, key, *, upload=False):
        result = self.endpoint + "/" + quote(self._path(key), safe="/")
        if upload and self.info.profile == "eos":
            result += "?eos.atomic=1"
        return result

    def _unknown(self, operation):
        self._uncertain = True
        return CommitOutcomeUnknownError(self.root, operation)

    def _call(self, function, *args, mutation=False, **kwargs):
        try:
            status, result = function(*args, timeout=self.io_timeout, **kwargs)
        except BaseException as exc:
            if mutation:
                raise self._unknown(function.__name__) from exc
            raise
        if status.ok:
            return result
        number = getattr(status, "errno", None)
        message = f"XRootD {function.__name__}: {status.message}"
        if mutation and number not in _ERRORS:
            raise self._unknown(function.__name__) from OSError(message)
        raise OSError(
            _ERRORS.get(number, errno.EAGAIN if number == 3020 else errno.EIO), message
        )

    def _require_write(self):
        if not self.writable:
            raise PermissionError("Database is read-only")
        if self._uncertain:
            raise self._unknown(
                "previous ambiguous operation; reconcile locks before reopening"
            )

    def bind_coordination(self, family):
        if family not in {"flock", "mkdir", "xrootd-exclusive", "eos-mkdir"}:
            raise UnsupportedBackendError(f"Unknown coordination family: {family}")
        if self.writable and family != self.info.coordination:
            raise UnsupportedBackendError(
                f"XRootD cannot write a store requiring {family}; offline coordination conversion is required"
            )

    def _retry(self, operation, *, missing=False):
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
            except OSError as exc:
                if exc.errno != errno.EAGAIN or time.monotonic() >= deadline:
                    raise
            time.sleep(min(delay, max(0, deadline - time.monotonic())))
            delay = min(0.5, delay * 2)

    def _read_once(self, key):
        handle = self._client.File()
        self._call(
            handle.open,
            self._url(key),
            flags=self._flags.OpenFlags.READ | self._flags.OpenFlags.REFRESH,
        )
        try:
            size = self._call(handle.stat, force=True).size
            data = bytearray()
            while len(data) < size:
                block = self._call(
                    handle.read, offset=len(data), size=min(_CHUNK, size - len(data))
                )
                if not block:
                    raise CorruptionError(f"Short remote read: {key}")
                data.extend(block)
                self.metrics["read_bytes"] += len(block)
            return bytes(data)
        finally:
            self._call(handle.close)

    def read(self, key, *, expected=None):
        def attempt():
            raw = self._read_once(key)
            if expected is not None and hashlib.sha256(raw).hexdigest() != expected:
                raise CorruptionError(f"Remote checksum mismatch: {key}")
            if key.endswith(".json"):
                from ..catalog import unpack

                unpack(raw)
            return raw

        return self._retry(attempt, missing=expected is not None)

    def read_page(self, key, *, full=False, expected=None):
        from ..page_format import decode

        # Buffers own the complete downloaded page. Always verify remote payloads.
        return self._retry(
            lambda: decode(self._read_once(key), full=True, expected=expected),
            missing=expected is not None,
        )

    def exists(self, key):
        try:
            self._call(self._fs.stat, self._path(key))
            return True
        except FileNotFoundError:
            return False

    def listdir(self, key=""):
        entries = self._call(self._fs.dirlist, self._path(key))
        names = [entry.name for entry in entries]
        if any(not n or n in {".", ".."} or "/" in n for n in names):
            raise CorruptionError("Invalid remote directory entry")
        return names

    def disk_usage(self):
        """Sum all server-reported file lengths without downloading payloads.

        XRootD stat does not expose allocated blocks or replica overhead. Use
        directory listings with stat information to avoid a round trip per file;
        fall back to individual stat only for entries missing that information.
        """
        total = 0
        stack = [""]
        while stack:
            key = stack.pop()
            try:
                entries = self._retry(
                    lambda: self._call(
                        self._fs.dirlist,
                        self._path(key),
                        flags=self._flags.DirListFlags.STAT,
                    )
                )
            except FileNotFoundError:
                if not key:
                    raise
                continue
            for entry in entries:
                name = entry.name
                if not name or name in {".", ".."} or "/" in name:
                    raise CorruptionError("Invalid remote directory entry")
                child = key + "/" + name if key else name
                info = getattr(entry, "statinfo", None)
                if info is None:
                    try:
                        info = self._retry(
                            lambda: self._call(self._fs.stat, self._path(child))
                        )
                    except FileNotFoundError:
                        continue
                if info.flags & self._flags.StatInfoFlags.IS_DIR:
                    stack.append(child)
                else:
                    if info.size < 0:
                        raise CorruptionError("Invalid remote file size")
                    total += info.size
        return total, "logical"

    def glob(self, pattern):
        self._path(pattern)

        def walk(prefix, parts):
            if not parts:
                yield prefix
                return
            part, *rest = parts
            if any(c in part for c in "*?["):
                try:
                    names = self.listdir(prefix)
                except FileNotFoundError:
                    return
                for name in names:
                    if fnmatchcase(name, part):
                        yield from walk(prefix + "/" + name if prefix else name, rest)
            else:
                key = prefix + "/" + part if prefix else part
                if rest or self.exists(key):
                    yield from walk(key, rest)

        return walk("", pattern.split("/"))

    def _mkdir(self, key):
        if key in self._directories:
            return
        try:
            self._call(
                self._fs.mkdir,
                self._path(key),
                flags=self._flags.MkDirFlags.MAKEPATH,
                mutation=True,
            )
        except FileExistsError:
            info = self._call(self._fs.stat, self._path(key))
            if not info.flags & self._flags.StatInfoFlags.IS_DIR:
                raise NotADirectoryError(key)
        self._directories.add(key)

    def _confirm_upload(self, key, size, sha256, adler32):
        def attempt():
            info = self._call(self._fs.stat, self._path(key))
            if info.size != size or info.flags & self._flags.StatInfoFlags.POSC_PENDING:
                raise CorruptionError(f"Upload not finalized: {key}")
            try:
                result = self._call(
                    self._fs.query, self._flags.QueryCode.CHECKSUM, self._path(key)
                )
            except OSError as exc:
                if exc.errno != errno.ENOTSUP:
                    raise
                result = b""
            if isinstance(result, bytes):
                result = result.decode("ascii").strip("\x00\n ")
            fields = result.split()
            expected = {"adler32": f"{adler32:08x}", "sha256": sha256}
            if len(fields) == 2 and fields[0].lower() in expected:
                actual = (
                    fields[1]
                    .lower()
                    .removeprefix("0x")
                    .zfill(len(expected[fields[0].lower()]))
                )
                if actual != expected[fields[0].lower()]:
                    raise CorruptionError(f"Server checksum differs: {key}")
            elif hashlib.sha256(self._read_once(key)).hexdigest() != sha256:
                raise CorruptionError(f"Uploaded bytes differ: {key}")

        self._retry(attempt, missing=True)

    def publish(self, key, chunks, *, replace=False):
        self._require_write()
        self._path(key)
        parent = key.rpartition("/")[0]
        self._mkdir(parent)
        if not replace and self.exists(key):
            raise FileExistsError(key)
        pending = (parent + "/" if parent else "") + f".{uuid4().hex}.pending"
        handle = self._client.File()
        self._call(
            handle.open,
            self._url(pending, upload=True),
            flags=self._flags.OpenFlags.NEW
            | self._flags.OpenFlags.WRITE
            | self._flags.OpenFlags.POSC,
            mutation=True,
        )
        sha = hashlib.sha256()
        adler, offset = 1, 0
        closed = False
        renamed = False
        try:
            buffer = bytearray()

            def write(block):
                nonlocal adler, offset
                self._call(handle.write, block, offset=offset, mutation=True)
                sha.update(block)
                adler = zlib.adler32(block, adler)
                offset += len(block)
                self.metrics["written_bytes"] += len(block)

            for chunk in chunks:
                view = memoryview(chunk).cast("B")
                for start in range(0, len(view), _CHUNK):
                    buffer.extend(view[start : start + _CHUNK])
                    if len(buffer) >= _CHUNK:
                        write(bytes(buffer))
                        buffer.clear()
            if buffer:
                write(bytes(buffer))
            self._call(handle.sync, mutation=True)
            self._call(handle.close, mutation=True)
            closed = True
            self._confirm_upload(pending, offset, sha.hexdigest(), adler)
            self._call(self._fs.mv, self._path(pending), self._path(key), mutation=True)
            renamed = True
            # Mutable metadata is small; compare exact content after publication.
            # Data files have unique names and were checksum-confirmed before rename.
            if replace or key.endswith(".json"):
                try:
                    self._retry(
                        lambda: self._confirm_content(key, sha.hexdigest()),
                        missing=True,
                    )
                except Exception as exc:
                    raise self._unknown(key) from exc
            self.metrics["publications"] += 1
        finally:
            if not closed:
                # A close may finalize pending bytes, but cannot expose the final key.
                # Do not retry a timed-out mutation or remove its pending evidence.
                if not self._uncertain:
                    self._call(handle.close, mutation=True)
            if not renamed and not self._uncertain:
                self.remove(pending)

    def _confirm_content(self, key, expected):
        if hashlib.sha256(self._read_once(key)).hexdigest() != expected:
            raise CorruptionError(f"Published content not yet visible: {key}")

    def remove(self, key):
        self._require_write()
        try:
            self._call(self._fs.rm, self._path(key), mutation=True)
        except FileNotFoundError:
            pass

    @contextmanager
    def lock(self, key, *, family=None):
        from ..catalog import canonical

        self._require_write()
        if family not in {None, self.info.coordination}:
            raise UnsupportedBackendError("Incompatible remote coordination profile")
        lock = key + ".LOCK"
        eos = self.info.profile == "eos"
        self._mkdir(lock.rpartition("/")[0] if eos else lock)
        token = uuid4().hex
        owner_key = lock + ("/owner-" + token if eos else "/owner")
        deadline = time.monotonic() + self.lock_timeout
        while True:
            # XrdCl can retain a failed-open status on a File instance. Each
            # contention retry needs a fresh handle or it never reaches the server.
            handle = self._client.File()
            try:
                # mkdir is idempotent on some XRootD servers. NEW is the actual
                # exclusive operation. Do NOT use POSC or eos.atomic for locks:
                # a disconnect must leave even an empty/incomplete owner in place.
                if eos:
                    # EOS can accept two NEW opens before the first close. Its
                    # MGM mkdir is exclusive; use that namespace operation instead.
                    self._call(self._fs.mkdir, self._path(lock), mutation=True)
                else:
                    self._call(
                        handle.open,
                        self._url(owner_key),
                        flags=self._flags.OpenFlags.NEW | self._flags.OpenFlags.WRITE,
                        mutation=True,
                    )
                break
            except (FileExistsError, BlockingIOError) as exc:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"Timed out acquiring {lock}; never steal a remote lock"
                    ) from exc
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        owner = canonical(
            {
                "token": token,
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "started": time.time(),
            }
        )
        # Any failure from here leaves an incomplete lock for manual reconciliation.
        if eos:
            # Unique owner keys also avoid stale inode/redirect caches on reuse.
            self.publish(owner_key, [owner])
        else:
            try:
                self._call(handle.write, owner, offset=0, mutation=True)
                self.metrics["written_bytes"] += len(owner)
                self._call(handle.sync, mutation=True)
            finally:
                if not self._uncertain:
                    self._call(handle.close, mutation=True)
            self._confirm_upload(
                owner_key,
                len(owner),
                hashlib.sha256(owner).hexdigest(),
                zlib.adler32(owner),
            )
        try:
            yield
        except CommitOutcomeUnknownError:
            self._uncertain = True
            raise
        finally:
            if not self._uncertain:
                try:
                    if self._read_once(owner_key) != owner:
                        raise CorruptionError(f"Lock owner changed: {lock}")
                except BaseException as exc:
                    raise self._unknown(f"cannot verify lock owner: {lock}") from exc
                self.remove(owner_key)
                if eos:
                    self._call(self._fs.rmdir, self._path(lock), mutation=True)
                    self._directories.discard(lock)
                # Generic XRootD keeps the directory; only its owner file locks it.
