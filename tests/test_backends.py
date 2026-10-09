from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from pagestore import DB, LockTimeoutError, UnsupportedBackendError
from pagestore.backends import FileBackend
from pagestore.errors import CommitOutcomeUnknownError


def test_profile_detection_and_persisted_coordination(tmp_path, monkeypatch):
    from pagestore.backends import filesystem

    monkeypatch.setattr(filesystem, "_mount_type", lambda path: "nfs4")
    with DB(tmp_path / "db", mode="a") as db:
        assert db.backend_info.profile == "nfs"
        assert db.backend_info.coordination == "mkdir"
        db.store({"x": ([1], [2])})
    monkeypatch.setattr(filesystem, "_mount_type", lambda path: "ext4")
    with DB(tmp_path / "db", mode="a") as db:
        assert db.backend_info.coordination == "mkdir"
        assert db.get_signal("x")[1].tolist() == [2]
    with DB(tmp_path / "local", mode="a"):
        pass
    monkeypatch.setattr(filesystem, "_mount_type", lambda path: "nfs4")
    with pytest.raises(UnsupportedBackendError, match="flock"):
        DB(tmp_path / "local", mode="a")
    with DB(tmp_path / "local", mode="r"):
        pass


def test_directory_lock_timeout_and_unknown_commit_retention(tmp_path):
    backend = FileBackend(f"file://{tmp_path}/db?profile=generic", lock_timeout=0.01)
    other = FileBackend(f"file://{tmp_path}/db?profile=generic", lock_timeout=0.01)
    with backend.lock("signal"):
        with pytest.raises(LockTimeoutError):
            with other.lock("signal"):
                pass
    with other.lock("signal"):
        pass
    with pytest.raises(CommitOutcomeUnknownError):
        with backend.lock("signal"):
            raise CommitOutcomeUnknownError("x", "abc")
    assert backend.path("signal.LOCK").is_dir()
    with pytest.raises(LockTimeoutError):
        with other.lock("signal"):
            pass


def test_read_only_and_legacy_detection(tmp_path):
    path = tmp_path / "db"
    with DB(path, mode="x") as db:
        db.store({"x": ([1], [2])})
    before = {p: p.stat().st_mtime_ns for p in path.rglob("*")}
    with DB(path) as db:
        assert db.count_signal("x") == 1
        assert db.check(full=True).ok
    assert {p: p.stat().st_mtime_ns for p in path.rglob("*")} == before
    with pytest.raises(FileExistsError):
        DB(path, mode="x")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "pagestore.db").write_bytes(b"legacy placeholder")
    with pytest.raises(ValueError, match="pagestore.legacy"):
        DB(legacy, mode="a")
    with pytest.raises(FileNotFoundError):
        DB(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("profile", ["local", "generic"])
def test_default_reader_never_creates_or_repairs_storage(
    tmp_path, monkeypatch, profile
):
    path = tmp_path / "store"
    url = f"file://{path}?profile={profile}"
    with pytest.raises(FileNotFoundError):
        DB(url)
    assert not path.exists()
    path.mkdir()
    with pytest.raises(FileNotFoundError):
        DB(url)
    assert list(path.iterdir()) == []

    with DB(url, mode="a") as writer:
        writer.store({"x": ([1, 2], [10, 20])})
    shutil.rmtree(path / "catalog")
    next((path / "pages" / "recovery").glob("*.pg")).unlink()
    before = {
        p: (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
        for p in path.rglob("*")
    }

    def reject_mutation(*args, **kwargs):
        pytest.fail("Default read-only access attempted a mutation or writer lock")

    monkeypatch.setattr(FileBackend, "publish", reject_mutation)
    monkeypatch.setattr(FileBackend, "lock", reject_mutation)
    with DB(url) as reader:
        assert "mode='r'" in repr(reader)
        assert reader.search() == ["x"]
        assert reader.get_signal("x")[1].tolist() == [10, 20]
        assert not reader.check(full=True).ok  # Report the missing copy; do not repair.
        reader.refresh()
        for method, args, kwargs in [
            ("store", ({"x": ([3], [30])},), {}),
            ("ingest", ("x", [([3], [30])]), {}),
            ("delete_signal", ("x",), {}),
            ("configure_signal", ("x",), {"max_page_size": 4096}),
            ("checkpoint", ("x",), {}),
            ("repair_recovery", ("x",), {}),
            ("rebuild_catalog", (), {}),
        ]:
            with pytest.raises(PermissionError):
                getattr(reader, method)(*args, **kwargs)
    assert {
        p: (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
        for p in path.rglob("*")
    } == before


@pytest.mark.parametrize(
    "url",
    [
        "s3://bucket/path",
        "https://host/db",
        "file:///tmp/unused?profile=bogus",
    ],
)
def test_unsupported_backends_fail_explicitly(url):
    with pytest.raises(UnsupportedBackendError):
        DB(url, mode="a")


def test_local_mount_detection_resolves_symlinks_and_boundaries(tmp_path, monkeypatch):
    from pagestore.backends.filesystem import _mount_type

    real_read = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/self/mountinfo":
            return (
                f"1 0 0:1 / / rw - ext4 /dev/root rw\n"
                f"2 1 0:2 / {tmp_path}/share rw - nfs4 server:/export rw\n"
                f"3 1 0:3 / {tmp_path}/eos rw - fuse eos-fuse rw\n"
                f"4 1 0:4 / {tmp_path}/ssh rw - fuse.sshfs lxplus:/eos rw\n"
            )
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    (tmp_path / "share").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "share")
    assert _mount_type(tmp_path / "alias" / "new") == "nfs4"
    assert _mount_type(tmp_path / "share2") == "ext4"
    assert _mount_type(tmp_path / "eos" / "new") == "fuse.eos"
    assert _mount_type(tmp_path / "ssh" / "new") == "fuse.sshfs"


def test_weak_mount_reads_verify_owned_snapshots(tmp_path, monkeypatch):
    from pagestore.backends import filesystem
    from pagestore import CorruptionError

    root = tmp_path / "db"
    with DB(root, mode="a") as db:
        db.store({"x": ([1, 2], [3, 4])})
        manifest, _ = db._head("x")
        descriptor = next(db._index(manifest).pages())
    monkeypatch.setattr(filesystem, "_mount_type", lambda _: "fuse.sshfs")
    real_read = Path.read_bytes
    attempts = 0

    def delayed(path):
        nonlocal attempts
        if path == root / "store.json":
            attempts += 1
            if attempts == 1:
                return b'{"body":'
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", delayed)
    with DB(root, mode="r", visibility_timeout=0.1) as db:
        assert attempts == 2
        page = db._backend.read_page(descriptor["key"])
        times = page.batch().timestamps
        data = bytearray(real_read(root / descriptor["key"]))
        section = next(s for s in page.header["sections"] if s["role"] == "values")
        data[section["offset"]] ^= 1
        (root / descriptor["key"]).write_bytes(data)
        assert times.tolist() == [1, 2]  # retained buffer is independent of mount file
        assert page.batch().values.tolist() == [3, 4]
        db._backend.visibility_timeout = 0
        with pytest.raises(CorruptionError):
            db.get_signal("x")


def test_new_import_does_not_load_legacy():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pagestore,sys; assert 'sqlite3' not in sys.modules; assert 'XRootD.client' not in sys.modules; assert 'pagestore.legacy' not in sys.modules; assert not hasattr(pagestore,'PageStore')",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
