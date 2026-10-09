import shutil

import numpy as np
import pytest

from pagestore import (
    CatalogIncompleteError,
    CorruptionError,
    DB,
    RaggedArray,
    RecoveryIncompleteError,
    SignalNotFoundError,
)
from pagestore.catalog import unpack
from pagestore.maintenance import recover, salvage
from pagestore.name_catalog import NameCatalog


@pytest.mark.parametrize(
    "low, high, expected",
    [
        (3, 6, [0, 1, 2, 7, 8, 9]),
        (None, 2, list(range(3, 10))),
        (7, None, list(range(7))),
        (5, 5, [0, 1, 2, 3, 4, 6, 7, 8, 9]),
        (None, None, []),
        (-10, 100, []),
    ],
)
def test_inclusive_deletion_and_empty_signal(tmp_path, low, high, expected):
    with DB(tmp_path / "db", mode="a") as db:
        db.store({"a[0]": (np.arange(10), np.arange(10) * 2), "a0": ([0], [99])})
        assert db.delete_signal("a[0]", low, high) == 10 - len(expected)
        assert db.get_signal("a0")[1].tolist() == [99]
        if expected:
            t, v = db.get_signal("a[0]")
            assert t.tolist() == expected
            assert v.tolist() == [i * 2 for i in expected]
            assert db.count_signal("a[0]") == len(expected)
            assert db.info_signal("a[0]").count == len(expected)
        else:
            assert db.search() == ["a0"]
            for read in (db.get_signal, db.count_signal, db.info_signal):
                with pytest.raises(SignalNotFoundError):
                    read("a[0]")
            assert db.delete_signal("a[0]") == 0
        assert db.check(full=True).ok


def test_only_boundary_payloads_read_and_other_pages_reused(tmp_path, monkeypatch):
    with DB(tmp_path / "db", mode="a") as db:
        for start in range(0, 40, 10):
            db.store(
                {"x": (np.arange(start, start + 10), np.arange(start, start + 10))}
            )
        parent, _ = db._head("x")
        pages = list(db._index(parent).pages())
        original = db._read_data
        reads = []

        def read(descriptor, *args, **kwargs):
            reads.append(descriptor["page_id"])
            assert kwargs["full"]
            return original(descriptor, *args, **kwargs)

        monkeypatch.setattr(db, "_read_data", read)
        assert db.delete_signal("x", 4, 25) == 22
        assert reads == [pages[0]["page_id"], pages[2]["page_id"]]
        monkeypatch.setattr(db, "_read_data", original)
        assert db.get_signal("x")[0].tolist() == list(range(4)) + list(range(26, 40))
        current, _ = db._head("x")
        assert pages[3] in list(db._index(current).pages())
        assert db.check(full=True).ok

        def reject(*args, **kwargs):
            pytest.fail("Deleting fully covered pages must not read payload")

        monkeypatch.setattr(db, "_read_data", reject)
        assert db.delete_signal("x", 30, 39) == 10
        assert db.delete_signal("x") == 8
        assert db.search() == []
        # Tombstone integrity must be checked even though search is now empty.
        assert db.check(full=True).ok


def test_noop_and_invalid_ranges_do_not_publish(tmp_path, monkeypatch):
    with DB(tmp_path / "db", mode="a") as db:
        db.store({"x": ([0.5, 10.5], [1, 2])})
        head = db._head("x")

        def reject(*args, **kwargs):
            pytest.fail("No-op/invalid deletion must not publish")

        monkeypatch.setattr(db._backend, "publish", reject)
        assert db.delete_signal("missing") == 0
        assert db.delete_signal("x", 1, 5) == 0
        assert db.delete_signal("x", 11, 20) == 0
        with pytest.raises(ValueError, match="t1"):
            db.delete_signal("x", 5, 1)
        with pytest.raises(ValueError):
            db.delete_signal("x", 1, 2, timezone="not/a/timezone")
        assert db._head("x") == head
    with DB(tmp_path / "db", mode="r") as db:
        with pytest.raises(PermissionError):
            db.delete_signal("x")
    with pytest.raises(ValueError, match="closed"):
        db.delete_signal("x")


def test_delete_across_index_branches_and_write_again(tmp_path, monkeypatch):
    import pagestore.page_index as index_module

    monkeypatch.setattr(index_module, "MAX_LEAF", 4)
    monkeypatch.setattr(index_module, "MAX_CHILDREN", 4)
    with DB(tmp_path / "db", default_max_page_size=1, mode="a") as db:
        db.store({"x": (np.arange(80), np.arange(80))})
        assert db.delete_signal("x", 7, 72) == 66
        expected = list(range(7)) + list(range(73, 80))
        assert db.get_signal("x")[0].tolist() == expected
        assert db.info_signal("x").page_count == 14
        assert db.count_signal("x", 5, 74, skip=1) == 2
        db.store({"x": ([20, 50], [200, 500])})
        assert db.get_signal("x")[0].tolist() == sorted(expected + [20, 50])
        assert db.check(full=True).ok


def test_mixed_and_ragged_records_preserve_schemas(tmp_path):
    mixed = np.empty(5, dtype=object)
    mixed[:] = [
        np.int16(1),
        np.ones(2, dtype="f4"),
        np.float64(3),
        np.arange(3, dtype="i2"),
        np.int16(5),
    ]
    ragged = RaggedArray.from_arrays([np.arange(i, dtype="f4") for i in range(6)])
    with DB(tmp_path / "db", mode="a") as db:
        db.store({"mixed": (np.arange(5), mixed), "ragged": (np.arange(6), ragged)})
        assert db.delete_signal("mixed", 1, 2) == 2
        actual = db.get_signal("mixed")[1]
        for got, expected in zip(actual, mixed[[0, 3, 4]]):
            np.testing.assert_array_equal(got, expected)
            assert got.dtype == expected.dtype
        assert db.delete_signal("ragged", 2, 4) == 3
        assert [a.tolist() for a in db.get_signal("ragged")[1]] == [
            [],
            [0],
            list(range(5)),
        ]
        assert db.check(full=True).ok


def test_datetime_bounds_and_timezone_override(tmp_path):
    with DB(tmp_path / "db", timezone="cern", mode="a") as db:
        db.store({"x": (["2026-01-01T12:00:00Z", "2026-01-01T13:00:00Z"], [1, 2])})
        assert db.delete_signal("x", "2026-01-01 13:00", "2026-01-01 13:00") == 1
        assert db.get_signal("x")[1].tolist() == [2]
        assert db.delete_signal("x", "2026-01-01 13:00", timezone="utc") == 1
        assert db.search() == []


def test_cached_search_snapshot_and_recreation(tmp_path):
    path = tmp_path / "db"
    with DB(path, mode="a") as writer, DB(path, mode="r") as reader:
        writer.store({"x": ([1, 2], [10, 20])}, max_page_size=4000)
        old, _ = writer._head("x")
        assert reader.search() == ["x"]
        with reader.iter_signal("x") as stream:
            assert writer.delete_signal("x") == 2
            assert reader.search() == []
            assert next(stream).values.tolist() == [10, 20]
        assert writer.rebuild_catalog() == 0
        writer.ingest("x", [(["2026-01-01"], [42])])
        assert reader.search() == ["x"]
        new, _ = writer._head("x")
        assert new["generation"] > old["generation"]
        assert new["next_ordinal"] > old["next_ordinal"]
        assert new["time_kind"] == "datetime64[ns]"
        assert new["max_page_size"] == 8 * 1024**2
        assert reader.get_signal("x")[1].tolist() == [42]
        assert writer.check(full=True).ok


def test_recovery_preserves_partial_and_full_deletions(tmp_path):
    source, flat, destination = [tmp_path / p for p in ("source", "flat", "new")]
    with DB(source, mode="a") as db:
        db.store(
            {"partial": (np.arange(10), np.arange(10)), "gone": ([1, 2], [10, 20])}
        )
        assert db.delete_signal("partial", 2, 7) == 6
        assert db.delete_signal("gone") == 2
    flat.mkdir()
    for index, path in enumerate(source.rglob("*.pg")):
        shutil.copyfile(path, flat / f"{index}.pg")
    report = recover(flat, destination)
    assert report.complete, report.errors
    assert report.recovered_signals == ["partial"]
    assert report.deleted_signals == ["gone"]
    with DB(destination, mode="a") as db:
        assert db.search() == ["partial"]
        assert db.get_signal("partial")[0].tolist() == [0, 1, 8, 9]
        assert db.delete_signal("gone") == 0
        assert db.check(full=True).ok
    again = recover(destination, tmp_path / "again")
    assert again.complete and again.deleted_signals == ["gone"]


def test_data_only_salvage_cannot_know_deletions(tmp_path):
    source = tmp_path / "db"
    with DB(source, mode="a") as db:
        db.store({"x": ([1, 2], [3, 4])})
        db.delete_signal("x")
    report = salvage(source, tmp_path / "salvaged")
    assert report.ok and report.commit_status == "unknown"
    with DB(tmp_path / "salvaged", mode="a") as db:
        assert db.get_signal("x")[1].tolist() == [3, 4]


@pytest.mark.parametrize(
    "failure", ["before_head", "after_head", "recovery", "catalog"]
)
def test_interrupted_full_deletion_is_resolved_from_head(
    tmp_path, monkeypatch, failure
):
    path = tmp_path / "db"
    with DB(path, mode="a") as writer, DB(path, mode="r") as reader:
        writer.store({"x": ([1, 2], [3, 4])})
        assert reader.search() == ["x"]
        original = writer._backend.publish

        def publish(key, chunks, **kwargs):
            head = key.startswith("signals/") and key.endswith("HEAD.json")
            if head:
                # The pending deletion must resolve to the still-live HEAD.
                assert reader.search() == ["x"]
            if (failure == "before_head" and head) or (
                failure == "recovery"
                and "/pages/recovery/" in key
                and key.startswith("signals/")
            ):
                raise OSError("injected failure")
            if failure == "catalog" and key == NameCatalog.HEAD:
                chunks = list(chunks)
                if not unpack(b"".join(chunks))["pending"]:
                    raise OSError("injected catalog failure")
            result = original(key, chunks, **kwargs)
            if head:
                assert reader.search() == []
                if failure == "after_head":
                    raise OSError("injected after HEAD")
            return result

        monkeypatch.setattr(writer._backend, "publish", publish)
        error = {
            "before_head": OSError,
            "after_head": RecoveryIncompleteError,
            "recovery": RecoveryIncompleteError,
            "catalog": CatalogIncompleteError,
        }[failure]
        with pytest.raises(error):
            writer.delete_signal("x")
        assert reader.search() == (["x"] if failure == "before_head" else [])
        monkeypatch.setattr(writer._backend, "publish", original)
        writer.repair_recovery("x")
        assert writer.rebuild_catalog() == int(failure == "before_head")
        assert writer.check(full=True).ok


def test_corrupt_boundary_refuses_delete_and_tombstone_can_be_repaired(
    tmp_path, monkeypatch
):
    with DB(tmp_path / "db", mode="a") as db:
        db.store({"x": ([1, 2, 3], [4, 5, 6])})
        original_head = db._head("x")

        def corrupt(*args, **kwargs):
            raise CorruptionError("broken boundary payload")

        with monkeypatch.context() as patch:
            patch.setattr(db, "_read_data", corrupt)
            with pytest.raises(CorruptionError):
                db.delete_signal("x", 2, 2)
        assert db._head("x") == original_head
        assert db.delete_signal("x") == 3
        tombstone, _ = db._head("x", include_deleted=True)
        db._backend.path(db._recovery_key(tombstone, 0)).write_bytes(b"broken")
        assert not db.check(full=True).ok
        db.repair_recovery("x")
        assert db.check(full=True).ok
