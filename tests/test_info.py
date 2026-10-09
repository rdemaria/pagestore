"""Store accounting, signal metadata, and cheap database representations."""

from dataclasses import asdict
import os
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pytest

from pagestore import DB, RaggedArray, SignalInfo, StoreInfo, SignalNotFoundError


def allocated_size(path):
    if not hasattr(path.stat(), "st_blocks") or not shutil.which("du"):
        pytest.skip("Allocated-size comparison requires stat blocks and du")
    return int(
        subprocess.check_output(["du", "-s", "-B1", str(path)], text=True).split()[0]
    )


def tree_state(path):
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in path.rglob("*")}


def test_empty_store_info_is_non_mutating_and_counts_disk_overhead(tmp_path):
    path = tmp_path / "db"
    with DB(path, mode="a") as db:
        before = tree_state(path)
        result = db.info()
        assert isinstance(result, StoreInfo)
        assert asdict(result) == dict(
            size_bytes=allocated_size(path),
            signal_count=0,
            record_count=0,
            size_basis="allocated",
        )
        assert result.size_bytes > 0  # configuration and paired recovery files
        assert tree_state(path) == before
        assert not (path / "catalog").exists()
    with DB(path, mode="r") as reader:
        assert reader.info() == result
        assert tree_state(path) == before
    with pytest.raises(ValueError, match="closed"):
        reader.info()


def test_store_and_signal_info_after_updates_deletions_and_reservations(
    tmp_path, monkeypatch
):
    path = tmp_path / "db"
    with DB(path, mode="a") as db:
        db.store(
            {
                "dense": ([1, 2, 3], np.arange(12).reshape(3, 4)),
                "ragged": (
                    [1, 2],
                    RaggedArray.from_arrays([np.arange(2), np.arange(5)]),
                ),
                "removed": ([1, 2], [3, 4]),
            }
        )
        old_manifest, _ = db._head("dense")
        old_pages = [path / p["key"] for p in db._index(old_manifest).pages()]
        db.store({"dense": ([3, 4], np.full((2, 4), 42))})
        assert db.delete_signal("dense", 2, 2) == 1
        assert db.delete_signal("removed") == 2
        db._signal_prefix("reserved", create=True)
        assert all(p.exists() for p in old_pages)
        details = db.info_signal("dense")
        assert isinstance(details, SignalInfo) and details.count == 3
        assert db.configure_signal("dense", max_page_size=16384).name == "dense"
        with pytest.raises(SignalNotFoundError):
            db.info_signal("removed")

        # Unknown and partial files occupy space even though they are not live data.
        extra = path / "unreferenced" / ".upload.pending"
        extra.parent.mkdir()
        extra.write_bytes(b"x" * 18000)
        (extra.parent / "damaged.pg").write_bytes(b"broken page")
        expected_size = allocated_size(path)
        before = tree_state(path)
        original_read = db._backend.read

        def read(key, **kwargs):
            assert "/index/" not in key and not key.endswith(".pg")
            return original_read(key, **kwargs)

        monkeypatch.setattr(db._backend, "read", read)
        monkeypatch.setattr(
            db._backend, "read_page", lambda *a, **kw: pytest.fail("Payload read")
        )
        monkeypatch.setattr(db, "_index", lambda *a: pytest.fail("Page index read"))
        assert db.info() == StoreInfo(expected_size, 2, 5, "allocated")
        assert tree_state(path) == before


def test_info_refreshes_other_writer_changes_and_handles_missing_catalog(tmp_path):
    path = tmp_path / "db"
    with DB(path, mode="a") as writer, DB(path, mode="r") as reader:
        assert reader.info().signal_count == 0
        writer.store({"x": ([1, 2], [3, 4]), "y": ([1], [5])})
        assert (reader.info().signal_count, reader.info().record_count) == (2, 3)
        writer.delete_signal("x")
        assert (reader.info().signal_count, reader.info().record_count) == (1, 1)
        writer.store({"x": ([1, 2, 3], [6, 7, 8])})
        assert reader.info().record_count == 4
        shutil.rmtree(path / "catalog")
        before = tree_state(path)
        assert writer.info().record_count == 4
        assert reader.info().signal_count == 2
        assert tree_state(path) == before
        assert not (path / "catalog").exists()


def test_repr_is_descriptive_without_any_storage_access(tmp_path, monkeypatch):
    path = tmp_path / "db"
    with DB(path, mode="x") as db:
        for method in ("read", "listdir", "exists", "disk_usage"):
            monkeypatch.setattr(
                db._backend, method, lambda *a, **kw: pytest.fail("repr performed I/O")
            )
        monkeypatch.setattr(db, "info", lambda: pytest.fail("repr scanned store"))
        assert repr(db) == f"DB({str(path)!r}, mode='x', profile='local', closed=False)"
    assert "closed=True" in repr(db)


def test_filesystem_size_handles_sparse_files_hardlinks_and_symlinks(tmp_path):
    path = tmp_path / "db"
    with DB(path, mode="a") as db:
        sparse = path / "sparse"
        with sparse.open("wb") as f:
            f.seek(8 * 1024**2)
            f.write(b"end")
        os.link(sparse, path / "hardlink")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "large").write_bytes(b"x" * 1024**2)
        (path / "external").symlink_to(outside, target_is_directory=True)
        (path / "loop").symlink_to(path, target_is_directory=True)
        (path / "broken").symlink_to(path / "missing")
        result = db.info()
        assert result.size_basis == "allocated"
        assert result.size_bytes == allocated_size(path)
        assert result.size_bytes < sparse.stat().st_size


def test_weak_mount_size_uses_logical_bytes(tmp_path, monkeypatch):
    from pagestore.backends import filesystem

    path = tmp_path / "db"
    with DB(path, mode="a") as db:
        db.store({"x": ([1], [2])})
    expected = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    monkeypatch.setattr(filesystem, "_mount_type", lambda path: "fuse.sshfs")
    with DB(path, mode="r") as db:
        assert db.info() == StoreInfo(expected, 1, 1, "logical")


def test_info_handles_deletion_between_catalog_and_manifest_read(tmp_path, monkeypatch):
    path = tmp_path / "db"
    with DB(path, mode="a") as writer, DB(path, mode="r") as reader:
        writer.store({"x": ([1, 2], [3, 4]), "y": ([1], [5])})
        original = reader._head

        def head(name, **kwargs):
            if name == "x":
                writer.delete_signal("x")
            return original(name, **kwargs)

        monkeypatch.setattr(reader, "_head", head)
        result = reader.info()
        assert (result.signal_count, result.record_count) == (1, 1)
        assert result.size_bytes == allocated_size(path)


def test_store_info_does_not_trust_or_repair_a_corrupt_name_cache(tmp_path):
    path = tmp_path / "db"
    with DB(path, mode="a") as db:
        db.store({"x": ([1, 2], [3, 4]), "y": ([1], [5])})
        (path / "catalog/HEAD.json").write_bytes(b"broken derived metadata")
        before = tree_state(path)
        result = db.info()
        assert (result.signal_count, result.record_count) == (2, 3)
        assert result.size_bytes == allocated_size(path)
        assert tree_state(path) == before
