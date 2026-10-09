from pathlib import Path
import subprocess
import sys

import pytest

from pagestore import DB, LockTimeoutError, UnsupportedBackendError
from pagestore.backends import FileBackend
from pagestore.errors import CommitOutcomeUnknownError


def test_profile_detection_and_persisted_coordination(tmp_path, monkeypatch):
    from pagestore.backends import filesystem

    monkeypatch.setattr(filesystem, "_mount_type", lambda path: "nfs4")
    with DB(tmp_path / "db") as db:
        assert db.backend_info.profile == "nfs"
        assert db.backend_info.coordination == "mkdir"
        db.store({"x": ([1], [2])})
    monkeypatch.setattr(filesystem, "_mount_type", lambda path: "ext4")
    with DB(tmp_path / "db") as db:
        assert db.backend_info.coordination == "mkdir"
        assert db.get_signal("x")[1].tolist() == [2]
    with DB(tmp_path / "local"):
        pass
    monkeypatch.setattr(filesystem, "_mount_type", lambda path: "nfs4")
    with pytest.raises(UnsupportedBackendError, match="flock"):
        DB(tmp_path / "local")
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
    with DB(path, mode="r") as db:
        assert db.count_signal("x") == 1
        assert db.check(full=True).ok
    assert {p: p.stat().st_mtime_ns for p in path.rglob("*")} == before
    with pytest.raises(FileExistsError):
        DB(path, mode="x")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "pagestore.db").write_bytes(b"legacy placeholder")
    with pytest.raises(ValueError, match="pagestore.legacy"):
        DB(legacy)
    with pytest.raises(FileNotFoundError):
        DB(tmp_path / "missing", mode="r")
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize(
    "url",
    [
        "root://host//eos/db",
        "s3://bucket/path",
        "https://host/db",
        "file:///tmp/unused?profile=bogus",
    ],
)
def test_unsupported_backends_fail_explicitly(url):
    with pytest.raises(UnsupportedBackendError):
        DB(url)


def test_local_mount_detection_resolves_symlinks_and_boundaries(tmp_path, monkeypatch):
    from pagestore.backends.filesystem import _mount_type

    real_read = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/self/mountinfo":
            return (
                f"1 0 0:1 / / rw - ext4 /dev/root rw\n"
                f"2 1 0:2 / {tmp_path}/share rw - nfs4 server:/export rw\n"
                f"3 1 0:3 / {tmp_path}/eos rw - fuse eos-fuse rw\n"
            )
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    (tmp_path / "share").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "share")
    assert _mount_type(tmp_path / "alias" / "new") == "nfs4"
    assert _mount_type(tmp_path / "share2") == "ext4"
    assert _mount_type(tmp_path / "eos" / "new") == "fuse.eos"


def test_new_import_does_not_load_legacy():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pagestore,sys; assert 'sqlite3' not in sys.modules; assert 'pagestore.legacy' not in sys.modules; assert not hasattr(pagestore,'PageStore')",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
