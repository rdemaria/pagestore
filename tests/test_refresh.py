"""Explicit cache invalidation preserves streaming snapshots and writer safety."""

import shutil

import numpy as np
import pytest

from pagestore import DB, SignalNotFoundError


@pytest.mark.parametrize("mode", ["r", "a"])
def test_refresh_reads_other_workers_updates_and_name_changes(tmp_path, mode):
    path = tmp_path / "db"
    with DB(path, mode="a") as writer:
        writer.store({"x": ([1, 2], [10, 20]), "removed": ([1], [5])})
        with DB(path, mode=mode) as reader:
            previous = reader.get_signal("x")
            assert reader.search() == ["removed", "x"]
            writer.store({"x": ([2, 3], [22, 30]), "new": ([1], [42])})
            writer.delete_signal("removed")
            assert reader.refresh() is None
            assert reader.search() == ["new", "x"]
            assert reader.get_signal("x")[1].tolist() == [10, 22, 30]
            assert reader.info_signal("x").count == 3
            assert reader.get_signal("new")[1].tolist() == [42]
            with pytest.raises(SignalNotFoundError):
                reader.get_signal("removed")
            assert previous[1].tolist() == [10, 20]
            writer.store({"removed": (["2026-10-09"], [99])})
            reader.refresh()
            assert reader.search() == ["new", "removed", "x"]
            t, v = reader.get_signal("removed")
            assert t.dtype == np.dtype("datetime64[ns]")
            assert v.tolist() == [99]


def test_refresh_preserves_entered_stream_snapshot_and_retained_batches(tmp_path):
    path = tmp_path / "db"
    with DB(path, mode="a") as writer:
        writer.store({"x": ([1, 2, 3], [10, 20, 30])}, max_page_size=1)
        with DB(path, mode="r") as reader, reader.iter_signal("x") as stream:
            retained = next(stream)
            deferred = reader.iter_signal("x")  # Captures its snapshot on entry.
            writer.delete_signal("x")
            writer.store({"x": ([4, 5], [40, 50])})
            reader.refresh()
            assert reader.get_signal("x")[1].tolist() == [40, 50]
            assert np.concatenate([b.values for b in stream]).tolist() == [20, 30]
            assert retained.values.tolist() == [10]
            with deferred:
                assert np.concatenate([b.values for b in deferred]).tolist() == [40, 50]


@pytest.mark.parametrize("mode", ["r", "a"])
def test_refresh_is_lazy_and_forces_catalog_and_identity_rereads(
    tmp_path, monkeypatch, mode
):
    path = tmp_path / "db"
    with DB(path, mode="a") as writer:
        writer.store({"x": ([1], [2])})
    with DB(path, mode=mode) as db:
        assert db.search() == ["x"]
        assert db.get_signal("x")[1].tolist() == [2]
        metrics = db.io_metrics
        before = {p: p.stat().st_mtime_ns for p in path.rglob("*")}
        with monkeypatch.context() as patch:
            for name in (
                "read",
                "read_page",
                "listdir",
                "glob",
                "exists",
                "publish",
                "lock",
                "disk_usage",
            ):
                patch.setattr(
                    db._backend,
                    name,
                    lambda *a, **kw: pytest.fail("refresh performed I/O"),
                )
            assert db.refresh() is None
        assert db.io_metrics == metrics
        assert {p: p.stat().st_mtime_ns for p in path.rglob("*")} == before
        reads = []
        original = db._backend.read

        def read(key, **kwargs):
            reads.append(key)
            return original(key, **kwargs)

        monkeypatch.setattr(db._backend, "read", read)
        assert db.search() == ["x"]
        assert any(key.startswith("catalog/shards/") for key in reads)
        assert db.get_signal("x")[1].tolist() == [2]
        assert any(key.endswith("/SIGNAL.json") for key in reads)
        if mode == "r":
            with pytest.raises(PermissionError):
                db.store({"x": ([2], [3])})


def test_refresh_after_catalog_loss_does_not_repair_or_scan(tmp_path, monkeypatch):
    path = tmp_path / "db"
    with DB(path, mode="a") as db:
        db.store({"x": ([1], [2])})
        assert db.search() == ["x"]
        shutil.rmtree(path / "catalog")
        with monkeypatch.context() as patch:
            patch.setattr(
                db, "_scan_signal_entries", lambda: pytest.fail("Unexpected scan")
            )
            db.refresh()
        assert not (path / "catalog").exists()
        assert db.get_signal("x")[1].tolist() == [2]
        assert not (path / "catalog").exists()


def test_refresh_clears_recovery_verification_without_repairing_immediately(tmp_path):
    with DB(tmp_path / "db", mode="a") as db:
        db.store({"x": ([1], [2])})
        manifest, _ = db._head("x")
        recovery = db._backend.path(db._recovery_key(manifest, 0))
        recovery.unlink()
        db.refresh()
        assert not recovery.exists()
        db.store({"x": ([2], [3])})
        assert recovery.exists()  # Next write verifies/repairs its predecessor again.
        assert db.check(full=True).ok


def test_refresh_rejects_closed_instances(tmp_path):
    db = DB(tmp_path / "db", mode="a")
    db.close()
    with pytest.raises(ValueError, match="closed"):
        db.refresh()
    assert "closed=True" in repr(db)
