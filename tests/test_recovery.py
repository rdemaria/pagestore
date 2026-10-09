from pathlib import Path
import shutil

import numpy as np
import pytest

from pagestore import CorruptionError, DB, RecoveryIncompleteError, StoreError
from pagestore.maintenance import recover
from pagestore.page_format import PREFIX, read_page, repair_bytes


def flatten(source, destination):
    destination.mkdir()
    for index, path in enumerate(source.rglob("*.pg")):
        shutil.copyfile(path, destination / f"arbitrary-{index}.pg")


def flip(path, offset):
    with path.open("r+b") as stream:
        stream.seek(offset)
        value = stream.read(1)[0]
        stream.seek(offset)
        stream.write(bytes([value ^ 1]))


def test_flat_page_only_recovery_and_empty_store(tmp_path):
    source = tmp_path / "source"
    with DB(source, default_max_page_size=4096, mode="a") as db:
        db.store({"a": (np.arange(100), np.arange(100)), "b": (["2024-01-01"], [1.5])})
        db.store({"a": ([3, 101], [333, 101])})
        db.configure_signal("a", max_page_size=16384)
        expected = db.get()
    flat = tmp_path / "flat"
    flatten(source, flat)
    report = recover(flat, tmp_path / "new")
    assert report.complete, report.errors
    with DB(tmp_path / "new", mode="r") as db:
        assert db.check(full=True).ok
        assert db.info_signal("a").max_page_size == 16384
        for name in expected:
            for actual, wanted in zip(db.get_signal(name), expected[name]):
                np.testing.assert_array_equal(actual, wanted)
    with DB(tmp_path / "empty", default_max_page_size=12345, mode="a"):
        pass
    flatten(tmp_path / "empty", tmp_path / "empty-flat")
    assert recover(tmp_path / "empty-flat", tmp_path / "empty-new").complete
    with DB(tmp_path / "empty-new", default_max_page_size=12345, mode="a") as db:
        assert db.search() == []


@pytest.mark.parametrize(
    "location", ["prefix", "primary", "backup", "trailer", "digest", "padding"]
)
def test_duplicate_header_repair(tmp_path, location):
    with DB(tmp_path / "source", mode="a") as db:
        db.store({"x": ([1, 2], [10, 20])})
        manifest, _ = db._head("x")
        descriptor = next(db._index(manifest).pages())
        path = db._backend.path(descriptor["key"])
        original = path.read_bytes()
        h = read_page(path).header
        offset = {
            "prefix": 0,
            "primary": PREFIX.size + 10,
            "backup": h["backup_offset"] + 10,
            "trailer": len(original) - 100,
            "digest": len(original) - 1,
            "padding": h["sections"][0]["offset"] - 1,
        }[location]
        flip(path, offset)
        assert not db.check(full=True).ok
        if (
            location != "padding"
        ):  # normal mapped reads check metadata, not unused padding
            with pytest.raises(CorruptionError):
                db.get_signal("x")
        damaged = read_page(path, full=True, allow_damaged_envelope=True)
        assert repair_bytes(damaged, descriptor["sha256"]) == original
    report = recover(tmp_path / "source", tmp_path / "new")
    assert report.complete and report.repaired_pages
    with DB(tmp_path / "new", mode="r") as db:
        np.testing.assert_array_equal(db.get_signal("x")[1], [10, 20])
        assert db.check(full=True).ok


def test_payload_corruption_cannot_be_repaired(tmp_path):
    with DB(tmp_path / "source", mode="a") as db:
        db.store({"x": ([1, 2], [10, 20])})
        manifest, _ = db._head("x")
        d = next(db._index(manifest).pages())
        path = db._backend.path(d["key"])
        flip(path, read_page(path).header["sections"][-1]["offset"])
        assert not db.check(full=True).ok
    report = recover(tmp_path / "source", tmp_path / "new")
    assert not report.complete
    assert "committed page" in report.errors[0]
    assert report.recovered_signals == []


def test_one_bad_recovery_copy_and_delta_replay(tmp_path):
    with DB(tmp_path / "source", default_max_page_size=4000, mode="a") as db:
        for start in (0, 100, 200):
            db.store(
                {"x": (np.arange(start, start + 100), np.arange(start, start + 100))}
            )
        for path in (tmp_path / "source").rglob("*.0.pg"):
            path.write_bytes(b"destroyed")
        assert not db.check(full=True).ok
    report = recover(tmp_path / "source", tmp_path / "new")
    assert report.complete, report.errors
    assert report.ignored_pages == 4
    with DB(tmp_path / "new", mode="a") as db:
        assert db.count_signal("x") == 300


def test_missing_delta_refuses_silent_rollback(tmp_path):
    with DB(tmp_path / "source", mode="a") as db:
        db.store({"x": ([0], [0])})
        middle = db.store({"x": ([1], [1])})["x"].commit_id
        db.store({"x": ([2], [2])})
    for path in (tmp_path / "source").rglob(f"{middle}.*.pg"):
        path.unlink()
    report = recover(tmp_path / "source", tmp_path / "new")
    assert not report.complete and "predecessor" in report.errors[0]


def test_failure_before_head_leaves_old_snapshot(tmp_path, monkeypatch):
    with DB(tmp_path / "source", mode="a") as db:
        db.store({"x": ([0], [0])})
        original = db._backend.publish

        def fail(key, *args, **kwargs):
            if key.endswith("HEAD.json"):
                raise OSError("injected failure before publication")
            return original(key, *args, **kwargs)

        monkeypatch.setattr(db._backend, "publish", fail)
        with pytest.raises(StoreError):
            db.store({"x": ([1], [1])})
        assert db.count_signal("x") == 1
    report = recover(tmp_path / "source", tmp_path / "new")
    assert report.complete
    assert len(report.unconfirmed_pages) == 1
    with DB(tmp_path / "new", mode="a") as db:
        assert db.count_signal("x") == 1


@pytest.mark.parametrize("failure", ["first_copy", "second_copy", "head_sync"])
def test_visible_commit_recovery_failure_is_explicit_and_repairable(
    tmp_path, monkeypatch, failure
):
    with DB(tmp_path / "source", mode="a") as db:
        db.store({"x": ([0], [0])})
        original = db._backend.publish

        def fail(key, *args, **kwargs):
            if "signals/" in key and "/recovery/" in key:
                if (
                    failure == "first_copy"
                    and key.endswith(".0.pg")
                    or failure == "second_copy"
                    and key.endswith(".1.pg")
                ):
                    raise OSError("injected recovery failure")
            result = original(key, *args, **kwargs)
            if failure == "head_sync" and key.endswith("HEAD.json"):
                raise OSError("injected fsync failure after rename")
            return result

        monkeypatch.setattr(db._backend, "publish", fail)
        with pytest.raises(StoreError) as caught:
            db.store({"x": ([1], [1])})
        error = caught.value.cause
        assert isinstance(error, RecoveryIncompleteError) and error.committed
        assert db.count_signal("x") == 2
        assert not db.check(full=True).ok
        monkeypatch.setattr(db._backend, "publish", original)
        db.repair_recovery("x")
        assert db.check(full=True).ok
        assert db.count_signal("x") == 2


def test_next_writer_repairs_incomplete_predecessor(tmp_path):
    with DB(tmp_path / "source", mode="a") as db:
        commit = db.store({"x": ([0], [0])})["x"].commit_id
    for path in (tmp_path / "source").rglob(f"{commit}.*.pg"):
        path.unlink()
    with DB(tmp_path / "source", mode="a") as db:
        db.store({"x": ([1], [1])})
        assert db.check(full=True).ok
    assert recover(tmp_path / "source", tmp_path / "new").complete
