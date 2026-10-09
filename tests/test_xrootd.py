"""Protocol-shaped fake for deterministic visibility and ambiguous-result tests."""

from collections import defaultdict
import errno
import hashlib
from pathlib import Path, PurePosixPath
from types import SimpleNamespace as NS
from urllib.parse import unquote, urlsplit
import zlib

import numpy as np
import pytest

from pagestore import (
    DB,
    CommitOutcomeUnknownError,
    CorruptionError,
    LockTimeoutError,
    RaggedArray,
    StoreError,
    UnsupportedBackendError,
)
from pagestore.backends import XRootDBackend
from pagestore.backends import xrootd
from pagestore.catalog import canonical, envelope, signal_prefix

FLAGS = NS(
    OpenFlags=NS(NEW=8, READ=16, REFRESH=128, POSC=4096, WRITE=32768),
    MkDirFlags=NS(MAKEPATH=1),
    DirListFlags=NS(STAT=1),
    StatInfoFlags=NS(IS_DIR=2, POSC_PENDING=64),
    QueryCode=NS(CHECKSUM=3),
)


def reply(value=None, error=None):
    return NS(ok=error is None, errno=error or 0, message=f"status {error}"), value


class Server:
    def __init__(self):
        self.files = {}
        self.dirs = {"/"}
        self.events = []
        self.hidden = defaultdict(int)
        self.short = defaultdict(int)
        self.fail_mv = None
        self.late = []
        self.checksum = "adler32"
        self.checksum_delay = 0
        self.denied = False
        self.close_delay = 0

    def mkdir(self, path, flags=0, timeout=0):
        self.events.append(("mkdir", path, flags))
        if self.denied:
            return reply(error=3010)
        if path in self.dirs or path in self.files:
            return reply(error=3018)
        if flags:
            self.dirs.update(str(p) for p in PurePosixPath(path).parents)
        elif str(PurePosixPath(path).parent) not in self.dirs:
            return reply(error=3011)
        self.dirs.add(path)
        return reply()

    def stat(self, path, timeout=0):
        self.events.append(("stat", path))
        if self.denied:
            return reply(error=3010)
        if self.hidden[path]:
            self.hidden[path] -= 1
            return reply(error=3011)
        if path in self.dirs:
            return reply(NS(size=0, flags=2))
        if path in self.files:
            return reply(NS(size=len(self.files[path]), flags=0))
        return reply(error=3011)

    def mv(self, source, dest, timeout=0):
        self.events.append(("mv", source, dest))
        if self.fail_mv and self.fail_mv(dest):
            self.late.append((source, dest))
            return reply(error=3012)  # Server error: cannot prove non-execution.
        if source not in self.files:
            return reply(error=3011)
        self.files[dest] = self.files.pop(source)
        return reply()

    def rm(self, path, timeout=0):
        self.events.append(("rm", path))
        if path not in self.files:
            return reply(error=3011)
        del self.files[path]
        return reply()

    def rmdir(self, path, timeout=0):
        self.events.append(("rmdir", path))
        if any(p.startswith(path + "/") for p in self.files | dict.fromkeys(self.dirs)):
            return reply(error=3018)
        self.dirs.remove(path)
        return reply()

    def dirlist(self, path, flags=0, timeout=0):
        self.events.append(("dirlist", path, flags))
        if self.denied:
            return reply(error=3010)
        if path not in self.dirs:
            return reply(error=3011)
        names = {
            PurePosixPath(p).name
            for p in self.files | dict.fromkeys(self.dirs)
            if p != path and str(PurePosixPath(p).parent) == path
        }
        return reply(
            [
                NS(
                    name=n,
                    statinfo=(
                        (
                            NS(size=0, flags=2)
                            if path + "/" + n in self.dirs
                            else NS(size=len(self.files[path + "/" + n]), flags=0)
                        )
                        if flags & FLAGS.DirListFlags.STAT
                        else None
                    ),
                )
                for n in sorted(names)
            ]
        )

    def query(self, code, path, timeout=0):
        self.events.append(("checksum", path))
        if self.checksum_delay:
            self.checksum_delay -= 1
            return reply(b"adler32 00000000")
        if self.checksum == "unsupported":
            return reply(error=3013)
        raw = self.files[path]
        value = (
            f"{zlib.adler32(raw):08x}"
            if self.checksum == "adler32"
            else hashlib.sha256(raw).hexdigest()
        )
        return reply(f"{self.checksum} {value}".encode())


class File:
    def __init__(self, server):
        self.server = server
        self.writing = False
        self.open_error = None

    def open(self, url, flags=0, timeout=0):
        if self.open_error is not None:
            return reply(error=self.open_error)
        self.path = "/" + unquote(urlsplit(url).path).lstrip("/")
        self.server.events.append(("open", self.path, flags, url))
        if self.server.denied:
            return reply(error=3010)
        if flags & FLAGS.OpenFlags.NEW:
            if self.path in self.server.files:
                self.open_error = 3018
                return reply(error=3018)
            self.server.files[self.path] = b""  # exclusive reservation starts at open
            self.buffer = bytearray()
            self.writing = True
        else:
            status, _ = self.server.stat(self.path)
            if not status.ok:
                return status, None
            self.buffer = self.server.files[self.path]
        return reply()

    def write(self, buffer, offset=0, timeout=0):
        self.server.events.append(("write", self.path, len(buffer)))
        assert offset == len(self.buffer)
        self.buffer.extend(buffer)
        return reply()

    def sync(self, timeout=0):
        self.server.events.append(("sync", self.path))
        return reply()

    def close(self, timeout=0):
        self.server.events.append(("close", self.path))
        if self.writing:
            self.server.files[self.path] = bytes(self.buffer)
            self.server.hidden[self.path] = self.server.close_delay
            self.writing = False
        return reply()

    def stat(self, force=False, timeout=0):
        assert force
        return reply(NS(size=len(self.buffer), flags=0))

    def read(self, offset=0, size=0, timeout=0):
        self.server.events.append(("read", self.path))
        if self.server.short[self.path]:
            self.server.short[self.path] -= 1
            return reply(b"")
        return reply(bytes(self.buffer[offset : offset + size]))


@pytest.fixture
def server(monkeypatch):
    server = Server()
    client = NS(FileSystem=lambda endpoint: server, File=lambda: File(server))
    monkeypatch.setattr(xrootd, "_bindings", lambda: (client, FLAGS))
    return server


def test_remote_database_round_trip_and_read_only(server):
    url = "root://example//store"
    with DB(url, default_max_page_size=4096, mode="a") as db:
        db.store({"µ/beam": (np.arange(120), np.arange(120, dtype=">f8"))})
        db.store({"µ/beam": ([119, 120], [900.0, 901.0])})
        db.store({"dt": (["2026-01-01T00:00:00Z"], [1])})
        db.store(
            {"ragged": ([1, 2], RaggedArray.from_arrays([np.arange(2), np.arange(4)]))}
        )
        db.ingest("bulk", [([1, 2], [3, 4]), ([3], [5])])
        assert db.search("beam") == ["µ/beam"]
        np.testing.assert_array_equal(db.get_signal("µ/beam", 119)[1], [900, 901])
        assert db.count_signal("bulk") == 3
        assert db.rebuild_catalog() == 4
        assert db.check(full=True).ok
        assert db.backend_info.coordination == "xrootd-exclusive"
        assert not any(p.endswith(".LOCK/owner") for p in server.files)
    before = list(server.events)
    with DB(url) as reader:
        assert reader.get_signal("dt")[0].dtype == np.dtype("datetime64[ns]")
        assert reader.get_signal("ragged")[1][1].tolist() == [0, 1, 2, 3]
        assert reader.search() == ["bulk", "dt", "ragged", "µ/beam"]
        assert "mode='r'" in repr(reader)
        with pytest.raises(PermissionError):
            reader.store({"new": ([1], [2])})
        with pytest.raises(PermissionError):
            reader._backend.publish("no", [b"x"])
    assert not any(
        e[0] in {"write", "mkdir", "rm", "rmdir", "mv"}
        for e in server.events[len(before) :]
    )
    with pytest.raises(FileExistsError):
        DB(url, mode="x")


@pytest.mark.parametrize("url", ["root://example//store", "root://example//eos/store"])
def test_default_remote_open_does_not_create_missing_store(server, url):
    before = (dict(server.files), set(server.dirs))
    with pytest.raises(FileNotFoundError):
        DB(url)
    assert (server.files, server.dirs) == before
    assert not any(
        e[0] in {"write", "mkdir", "rm", "rmdir", "mv"} for e in server.events
    )


def test_delayed_close_and_checksum_precede_publication(server):
    backend = XRootDBackend("root://example//eos/project/store", visibility_timeout=1)
    server.close_delay = 2
    server.checksum_delay = 2
    backend.publish("page.pg", [b"hello", memoryview(b"world")])
    events = server.events
    close = next(i for i, e in enumerate(events) if e[0] == "close")
    checks = [i for i, e in enumerate(events) if e[0] == "checksum"]
    rename = next(i for i, e in enumerate(events) if e[0] == "mv")
    assert close < min(checks) < max(checks) < rename
    assert len(checks) == 3
    assert server.files["/eos/project/store/page.pg"] == b"helloworld"
    assert not any(e[0] == "read" for e in events)  # no payload round-trip
    opened = next(e for e in events if e[0] == "open")
    assert opened[3].endswith("?eos.atomic=1")


@pytest.mark.parametrize("url", ["root://example//store", "root://example//eos/store"])
def test_sharded_paths_reopen_check_and_rebuild(server, url):
    with DB(url, mode="a") as db:
        db.store({"x": (np.arange(129), np.arange(129))}, max_page_size=1)
        for generation in range(2, 130):
            db.configure_signal("x", max_page_size=generation)
        manifest, ref = db._head("x")
        assert "/manifests/01/" in ref["key"]
        assert "/recovery/01/" in db._recovery_key(manifest, 1)
        assert "/pages/01/00-" in list(db._index(manifest).pages())[-1]["key"]
    with DB(url, mode="a") as db:
        assert db.rebuild_catalog() == 1
        assert db.search() == ["x"]
        np.testing.assert_array_equal(db.get_signal("x")[1], np.arange(129))
        assert db.check(full=True).ok


@pytest.mark.parametrize("checksum", ["sha256", "unsupported"])
def test_server_checksum_and_fallback(server, checksum):
    server.checksum = checksum
    backend = XRootDBackend("root://example//store")
    backend.publish("page", [b"abc"])
    assert backend.read("page") == b"abc"


def test_visibility_retry_for_referenced_json_and_page_corruption(server):
    with DB("root://example//store", visibility_timeout=0.2, mode="a") as db:
        db.store({"x": ([1, 2], [3, 4])})
        manifest, ref = db._head("x")
        path = db._backend._path(ref["key"])
        server.hidden[path] = 2
        server.short[path] = 1
        assert db.get_signal("x")[1].tolist() == [3, 4]
        descriptor = next(db._index(manifest).pages())
        page_path = db._backend._path(descriptor["key"])
        page = db._backend.read_page(descriptor["key"])
        section = next(s for s in page.header["sections"] if s["role"] == "values")
        damaged = bytearray(server.files[page_path])
        damaged[section["offset"]] ^= 1
        server.files[page_path] = bytes(damaged)
        db._backend.visibility_timeout = 0
        with pytest.raises(CorruptionError):
            db.get_signal("x")


def test_unknown_head_keeps_lock_even_when_old_head_is_visible(server):
    url = "root://example//store"
    with DB(url, lock_timeout=0, visibility_timeout=0, mode="a") as db:
        db.store({"x": ([1], [2])})
        head_path = "/store/" + db._signal_prefix("x") + "/HEAD.json"
        old = server.files[head_path]
        server.fail_mv = lambda path: path == head_path
        with pytest.raises(StoreError) as exc:
            db.store({"x": ([2], [3])})
        assert isinstance(exc.value.cause, CommitOutcomeUnknownError)
        assert exc.value.cause.signal == "x"
        assert server.files[head_path] == old
        lock = "/store/" + db._signal_prefix("x") + "/LOCK.LOCK"
        assert lock in server.dirs and lock + "/owner" in server.files
        with pytest.raises(CommitOutcomeUnknownError):
            db._backend.publish("anything", [b"bad"])
        # The timed-out server request can execute after the exception was delivered.
        source, dest = server.late.pop()
        server.files[dest] = server.files.pop(source)
        server.fail_mv = None
        other = XRootDBackend(url, lock_timeout=0)
        with pytest.raises(LockTimeoutError):
            with other.lock(db._signal_prefix("x") + "/LOCK"):
                pass
        with DB(url, mode="r") as reader:
            assert reader.get_signal("x")[1].tolist() == [2, 3]


def test_unknown_catalog_finalization_retains_both_locks(server):
    with DB("root://example//store", visibility_timeout=0, mode="a") as db:
        publications = 0

        def fail(path):
            nonlocal publications
            if path == "/store/catalog/HEAD.json":
                publications += 1
                return publications == 4  # empty, reservation, intent, finalization
            return False

        server.fail_mv = fail
        with pytest.raises(StoreError) as exc:
            db.store({"x": ([1], [2])})
        assert isinstance(exc.value.cause, CommitOutcomeUnknownError)
        assert "/store/catalog/LOCK.LOCK" in server.dirs
        assert "/store/" + db._signal_prefix("x") + "/LOCK.LOCK" in server.dirs


@pytest.mark.parametrize("namespace", ["/store", "/eos/store"])
def test_remote_delete_and_recreate(server, namespace):
    url = "root://example/" + namespace
    with DB(url, mode="a") as writer, DB(url, mode="r") as reader:
        writer.store({"x": (np.arange(40), np.arange(40))})
        assert reader.search() == ["x"]
        assert writer.delete_signal("x", 10, 20) == 11
        assert reader.count_signal("x") == 29
        assert writer.delete_signal("x") == 29
        assert reader.search() == []
        assert writer.rebuild_catalog() == 0
        assert writer.check(full=True).ok
        writer.store({"x": (["2026-01-01"], [42])})
        assert reader.search() == ["x"]
        assert reader.get_signal("x")[1].tolist() == [42]
        assert writer.check(full=True).ok


def test_unknown_delete_keeps_signal_and_catalog_locks(server):
    url = "root://example//store"
    with DB(url, lock_timeout=0, visibility_timeout=0, mode="a") as writer:
        writer.store({"x": ([1, 2], [3, 4])})
        head_path = "/store/" + writer._signal_prefix("x") + "/HEAD.json"
        server.fail_mv = lambda path: path == head_path
        with pytest.raises(CommitOutcomeUnknownError):
            writer.delete_signal("x")
        for lock in (writer._signal_prefix("x") + "/LOCK", "catalog/LOCK"):
            assert f"/store/{lock}.LOCK/owner" in server.files
        with DB(url, mode="r") as reader:
            assert reader.search() == ["x"]
            source, dest = server.late.pop()
            server.files[dest] = server.files.pop(source)
            assert reader.search() == []  # Same pending intent; new signal HEAD.
        start = len(server.events)
        writer.refresh()
        assert server.events[start:] == []
        assert writer._backend._uncertain
        assert f"/store/{writer._signal_prefix('x')}/LOCK.LOCK/owner" in server.files
        assert "/store/catalog/LOCK.LOCK/owner" in server.files
        with pytest.raises(CommitOutcomeUnknownError):
            writer.delete_signal("x")


def test_exclusive_locks_permissions_and_no_age_stealing(server):
    a = XRootDBackend("root://example//store", lock_timeout=0)
    b = XRootDBackend("root://example//store", lock_timeout=0)
    with a.lock("signal"):
        with pytest.raises(LockTimeoutError):
            with b.lock("signal"):
                pass
    with b.lock("signal"):
        pass
    for event in server.events:
        if (
            event[0] == "open"
            and event[1].endswith(".LOCK/owner")
            and event[2] & FLAGS.OpenFlags.NEW
        ):
            assert not event[2] & FLAGS.OpenFlags.POSC
            assert not urlsplit(event[3]).query
    server.denied = True
    with pytest.raises(PermissionError):
        b.exists("file")
    with pytest.raises(PermissionError):
        with b.lock("signal"):
            pass


def test_contender_can_acquire_after_failed_open_and_owner_release(server, monkeypatch):
    first = XRootDBackend("root://example//store")
    second = XRootDBackend("root://example//store", lock_timeout=0.2)
    held = first.lock("signal")
    held.__enter__()
    released = False

    def release(_delay):
        nonlocal released
        assert not released
        held.__exit__(None, None, None)
        released = True

    monkeypatch.setattr(xrootd.time, "sleep", release)
    with second.lock("signal"):
        assert released
    assert "/store/signal.LOCK/owner" not in server.files


def test_eos_uses_exclusive_namespace_directory_with_unique_owners(server):
    url = "root://example//eos/project/store"
    a = XRootDBackend(url, lock_timeout=0)
    b = XRootDBackend(url, lock_timeout=0)
    assert a.info.coordination == "eos-mkdir"
    with a.lock("signal"):
        owners = [p for p in server.files if "/signal.LOCK/owner-" in p]
        assert len(owners) == 1
        with pytest.raises(LockTimeoutError):
            with b.lock("signal"):
                pass
        assert owners[0] in server.files
    assert "/eos/project/store/signal.LOCK" not in server.dirs
    with b.lock("signal"):
        new = [p for p in server.files if "/signal.LOCK/owner-" in p]
        assert len(new) == 1 and new != owners
    with pytest.raises(UnsupportedBackendError):
        XRootDBackend(url + "?profile=xrootd")


def test_generic_lock_works_when_mkdir_is_idempotent(server, monkeypatch):
    original = server.mkdir

    def mkdir(path, **kwargs):
        if path in server.dirs:
            return reply()
        return original(path, **kwargs)

    monkeypatch.setattr(server, "mkdir", mkdir)
    a = XRootDBackend("root://example//store", lock_timeout=0)
    b = XRootDBackend("root://example//store", lock_timeout=0)
    with a.lock("signal"):
        with pytest.raises(LockTimeoutError):
            with b.lock("signal"):
                pass


def test_unverifiable_lock_owner_disables_writer_and_retains_lock(server, monkeypatch):
    backend = XRootDBackend("root://example//store")
    original = backend._read_once

    def missing(key):
        if key.endswith(".LOCK/owner"):
            raise FileNotFoundError(key)
        return original(key)

    with pytest.raises(CommitOutcomeUnknownError):
        with backend.lock("signal"):
            monkeypatch.setattr(backend, "_read_once", missing)
    assert "/store/signal.LOCK/owner" in server.files
    with pytest.raises(CommitOutcomeUnknownError):
        backend.publish("no", [b"x"])


def test_interrupted_upload_close_retains_lock_and_old_file(server, monkeypatch):
    backend = XRootDBackend("root://example//store")
    backend.publish("HEAD.json", [envelope({"generation": 1})])
    old = server.files["/store/HEAD.json"]
    close = File.close

    def interrupted(handle, **kwargs):
        result = close(handle, **kwargs)
        if handle.path.endswith(".pending"):
            raise KeyboardInterrupt()
        return result

    monkeypatch.setattr(File, "close", interrupted)
    with pytest.raises(CommitOutcomeUnknownError):
        with backend.lock("signal"):
            backend.publish("HEAD.json", [envelope({"generation": 2})], replace=True)
    assert server.files["/store/HEAD.json"] == old
    assert "/store/signal.LOCK/owner" in server.files
    assert any(p.endswith(".pending") for p in server.files)


def test_stale_content_confirmation_retries_before_acknowledging(server, monkeypatch):
    backend = XRootDBackend("root://example//store", visibility_timeout=0.2)
    old = envelope({"generation": 1})
    new = envelope({"generation": 2})
    backend.publish("HEAD.json", [old])
    original = backend._read_once
    attempts = 0

    def stale(key):
        nonlocal attempts
        if key == "HEAD.json":
            attempts += 1
            if attempts <= 2:
                return old
        return original(key)

    monkeypatch.setattr(backend, "_read_once", stale)
    backend.publish("HEAD.json", [new], replace=True)
    assert attempts == 3
    assert backend.read("HEAD.json") == new


def test_remote_coordination_cannot_be_bypassed_through_local_filesystem(
    server, tmp_path
):
    with DB("root://example//store", mode="a") as db:
        config = db._config
    (tmp_path / "store.json").write_bytes(envelope(config))
    with pytest.raises(UnsupportedBackendError, match="XRootD"):
        DB(tmp_path, mode="a")
    with DB(tmp_path, mode="r") as db:
        assert db._config == config


def test_mount_alias_never_uses_mount_io(server, tmp_path, monkeypatch):
    from pagestore.backends import filesystem

    monkeypatch.setattr(filesystem, "_mount_type", lambda _: "fuse.sshfs")
    mount = tmp_path / "store"
    with pytest.raises(UnsupportedBackendError, match="xrootd_url"):
        DB(mount, mode="a")
    with pytest.raises(UnsupportedBackendError, match="xrootd_url"):
        DB(f"file://{mount}?profile=generic", mode="a")
    with DB(mount, xrootd_url="root://example//eos/test/store", mode="a") as db:
        db.store({"x": ([1], [2])})
    assert not mount.exists()
    with DB("root://example//eos/test/store", mode="r") as direct:
        assert direct.get_signal("x")[1].tolist() == [2]


@pytest.mark.parametrize(
    "url",
    [
        "root://host/",
        "root://host//a/../b",
        "root://host//a?oops=1",
        "root://user:secret@host//a",
        "root://host//a#fragment",
    ],
)
def test_invalid_remote_urls(server, url):
    with pytest.raises(ValueError):
        XRootDBackend(url)
    assert not server.events


def test_read_only_and_coordination_fail_before_mutation(server):
    backend = XRootDBackend("root://example//store", writable=False)
    with pytest.raises(PermissionError):
        with backend.lock("lock"):
            pass
    assert not server.events
    with pytest.raises(UnsupportedBackendError, match="flock"):
        XRootDBackend("root://example//store").bind_coordination("flock")


@pytest.mark.parametrize("operation", ["recover", "salvage"])
def test_offline_reconstruction_rejects_remote_urls_before_path_conversion(
    tmp_path, operation
):
    from pagestore import maintenance

    with pytest.raises(UnsupportedBackendError, match="filesystem directories"):
        getattr(maintenance, operation)(tmp_path, "root://example//store")


def test_unknown_location_reservation_retains_catalog_lock(server):
    with DB(
        "root://example//store", lock_timeout=0, visibility_timeout=0, mode="a"
    ) as db:
        db.rebuild_catalog()
        server.fail_mv = lambda path: path == "/store/catalog/HEAD.json"
        with pytest.raises(StoreError) as exc:
            db.store({"x": ([1], [2])})
        assert isinstance(exc.value.cause, CommitOutcomeUnknownError)
        assert "/store/catalog/LOCK.LOCK/owner" in server.files
        assert not any(key.endswith("/SIGNAL.json") for key in server.files)
        with DB("root://example//store", mode="r") as reader:
            assert reader.search() == []
            source, dest = server.late.pop()
            server.files[dest] = server.files.pop(source)
            assert reader.search() == []
        other = XRootDBackend("root://example//store", lock_timeout=0)
        with pytest.raises(LockTimeoutError):
            with other.lock("catalog/LOCK"):
                pass


@pytest.mark.parametrize("namespace", ["/store", "/eos/store"])
def test_remote_store_info_includes_history_and_all_files_without_payload_reads(
    server, namespace
):
    from pagestore import StoreInfo

    url = "root://example/" + namespace
    with DB(url, mode="a") as writer:
        writer.store({"x": ([1, 2], [3, 4]), "deleted": ([1], [5])})
        writer.store({"x": ([2, 3], [6, 7])})
        writer.delete_signal("deleted")
        writer._backend.publish("unreferenced/.upload.pending", [b"partial"])
    with DB(url, mode="r") as reader:
        expected = sum(
            len(raw)
            for key, raw in server.files.items()
            if key.startswith(namespace + "/")
        )
        start = len(server.events)
        assert reader.info() == StoreInfo(expected, 1, 3, "logical")
        events = server.events[start:]
        assert not any(e[0] == "read" and e[1].endswith(".pg") for e in events)
        listing_start = next(
            i for i, e in enumerate(events) if e[0] == "dirlist" and e[1] == namespace
        )
        assert not any(e[0] == "stat" for e in events[listing_start:])
        assert not any(e[0] in {"write", "mkdir", "mv", "rm", "rmdir"} for e in events)
        start = len(server.events)
        assert url in repr(reader) and "mode='r'" in repr(reader)
        assert server.events[start:] == []


def test_remote_disk_usage_stat_fallback_and_permission_errors(server, monkeypatch):
    backend = XRootDBackend("root://example//store")
    backend.publish("sub/.partial", [b"hello"])
    original = server.dirlist

    def no_stats(path, flags=0, timeout=0):
        return original(path, flags=0, timeout=timeout)

    monkeypatch.setattr(server, "dirlist", no_stats)
    assert backend.disk_usage() == (5, "logical")
    server.denied = True
    with pytest.raises(PermissionError):
        backend.disk_usage()


@pytest.mark.parametrize("namespace", ["/store", "/eos/store"])
def test_remote_refresh_fetches_new_data_without_mutation(server, namespace):
    url = "root://example/" + namespace
    with DB(url, mode="a") as writer, DB(url, mode="r") as reader:
        writer.store({"x": ([1], [2]), "deleted": ([1], [3])})
        assert reader.search() == ["deleted", "x"]
        assert reader.get_signal("x")[1].tolist() == [2]
        writer.store({"x": ([2], [4]), "new": ([1], [5])})
        writer.delete_signal("deleted")
        start = len(server.events)
        assert reader.refresh() is None
        assert server.events[start:] == []
        assert reader.search() == ["new", "x"]
        assert reader.get_signal("x")[1].tolist() == [2, 4]
        events = server.events[start:]
        assert all(e[2] & FLAGS.OpenFlags.REFRESH for e in events if e[0] == "open")
        assert not any(e[0] in {"write", "mkdir", "mv", "rm", "rmdir"} for e in events)
