from contextlib import nullcontext
import shutil

import pytest

from pagestore import CatalogIncompleteError, CorruptionError, DB, StoreError
from pagestore.catalog import unpack
from pagestore.name_catalog import NameCatalog


def test_lazy_search_refresh_and_no_signal_reads(tmp_path, monkeypatch):
    path = tmp_path / "db"
    with DB(path, mode="a") as writer, DB(path, mode="r") as reader:
        assert reader.search() == []
        writer.store({"b": ([1], [2]), "a/µ": ([1], [3])})
        assert reader._name_catalog._raw is None
        reads = []
        original_read = reader._backend.read

        def read(key, **kwargs):
            reads.append(key)
            assert not key.startswith("signals/")
            return original_read(key, **kwargs)

        monkeypatch.setattr(reader._backend, "read", read)
        assert reader.search() == ["a/µ", "b"]
        first_count = len(reads)
        assert first_count == 3  # HEAD and two name shards; no manifests.
        assert reader.search("µ$") == ["a/µ"]
        assert reads[first_count:] == [NameCatalog.HEAD]
        result = reader.search()
        result.clear()
        assert reader.search() == ["a/µ", "b"]
        old_names = reader._name_catalog._names
        writer.store({"b": ([2], [4])})
        assert reader.search() == ["a/µ", "b"]
        assert reader._name_catalog._names is old_names
        writer.store({"c": ([1], [5])})
        reads.clear()
        assert reader.search() == ["a/µ", "b", "c"]
        assert len(reads) == 2  # only the changed shard is loaded


def test_existing_signal_writes_do_not_touch_catalog(tmp_path, monkeypatch):
    with DB(tmp_path / "db", mode="a") as db:
        db.store({"x": ([1], [1])})
        original_publish = db._backend.publish
        original_lock = db._backend.lock

        def publish(key, *args, **kwargs):
            assert not key.startswith("catalog/")
            return original_publish(key, *args, **kwargs)

        def lock(key, *args, **kwargs):
            assert not key.startswith("catalog/")
            return original_lock(key, *args, **kwargs)

        monkeypatch.setattr(db._backend, "publish", publish)
        monkeypatch.setattr(db._backend, "lock", lock)
        db.store({"x": ([2], [2])})
        db.ingest("x", [([3], [3])])
        db.configure_signal("x", max_page_size=16384)
        assert db.search() == ["x"]


def test_cached_creation_intent_tracks_the_signal_commit(tmp_path, monkeypatch):
    path = tmp_path / "db"
    with DB(path, mode="a") as writer, DB(path, mode="r") as reader:
        original = writer._backend.publish

        def publish(key, *args, **kwargs):
            if key.startswith("signals/") and key.endswith("HEAD.json"):
                # The durable intent is present, but measurement HEAD is not.
                assert reader.search() == []
                intent_head = reader._name_catalog._raw
                result = original(key, *args, **kwargs)
                # The same cached catalog HEAD must now resolve the committed name.
                assert reader.search() == ["new"]
                assert reader._name_catalog._raw == intent_head
                return result
            return original(key, *args, **kwargs)

        monkeypatch.setattr(writer._backend, "publish", publish)
        writer.store({"new": ([1], [2])})
        assert reader.search() == ["new"]


def test_missing_catalog_rebuild_and_readonly_fallback(tmp_path, monkeypatch):
    path = tmp_path / "db"
    with DB(path, mode="a") as db:
        db.store({"x": ([1], [2])})
    shutil.rmtree(path / "catalog")
    with DB(path, mode="r") as db:
        assert db.search() == ["x"]
        assert not (path / "catalog").exists()
        with pytest.raises(PermissionError):
            db.rebuild_catalog()
    with DB(path, mode="a") as db:
        assert not (path / "catalog").exists()  # open is still constant work
        assert db.search() == ["x"]
        assert (path / NameCatalog.HEAD).exists()
        monkeypatch.setattr(
            db, "_scan_signal_names", lambda: pytest.fail("Unexpected scan")
        )
        assert db.search("x") == ["x"]


@pytest.mark.parametrize(
    "failure", ["before_head", "after_head", "recovery", "catalog", "catalog_sync"]
)
def test_interrupted_creation_remains_discoverable(tmp_path, monkeypatch, failure):
    path = tmp_path / "db"
    with DB(path, mode="a") as db:
        db.rebuild_catalog()
        db._name_catalog.reserve("new")
        original = db._backend.publish

        def publish(key, *args, **kwargs):
            signal_head = key.startswith("signals/") and key.endswith("HEAD.json")
            if (
                (failure == "before_head" and signal_head)
                or (
                    failure == "recovery"
                    and key.startswith("signals/")
                    and "/recovery/" in key
                )
                or (failure == "catalog" and key.startswith("catalog/shards/"))
            ):
                raise OSError("injected before publication")
            value = original(key, *args, **kwargs)
            if (failure == "after_head" and signal_head) or (
                failure == "catalog_sync"
                and key == NameCatalog.HEAD
                and not unpack(db._backend.read(key))["pending"]
            ):
                raise OSError("injected after publication")
            return value

        monkeypatch.setattr(db._backend, "publish", publish)
        with pytest.raises(StoreError) as caught:
            db.store({"new": ([1], [2])})
        if failure in {"catalog", "catalog_sync"}:
            assert isinstance(caught.value.cause, CatalogIncompleteError)
            assert caught.value.cause.committed
        expected = [] if failure == "before_head" else ["new"]
        with DB(path, mode="r") as reader:
            assert reader.search() == expected
        monkeypatch.setattr(db._backend, "publish", original)
        if expected:
            db.repair_recovery("new")
        assert db.rebuild_catalog() == len(expected)
        assert db.search() == expected
        assert not unpack(db._backend.read(NameCatalog.HEAD))["pending"]
        if not expected:
            db.store({"new": ([1], [2])})
        assert db.search() == ["new"]
        assert db.check(full=True).ok


@pytest.mark.parametrize("damage", ["head", "shard", "missing_shard"])
def test_catalog_corruption_is_detected_and_rebuilt(tmp_path, damage):
    path = tmp_path / "db"
    with DB(path, mode="a") as db:
        db.store({"x": ([1], [2])})
        assert db.search() == ["x"]  # prime the cache before damage
        head = unpack(db._backend.read(NameCatalog.HEAD))
        key = (
            NameCatalog.HEAD
            if damage == "head"
            else next(iter(head["shards"].values()))["key"]
        )
        if damage == "missing_shard":
            db._backend.path(key).unlink()
        else:
            db._backend.path(key).write_bytes(b"broken")
        assert db.get_signal("x")[1].tolist() == [2]
        report = db.check(full=True)
        assert not report.ok
        assert any("Name catalog" in error for error in report.errors)
        assert report.pages >= 5  # still inspected measurement/recovery pages
        with DB(path, mode="r") as reader:
            with pytest.raises(CorruptionError):
                reader.search()
        assert db.rebuild_catalog() == 1
        assert db.search() == ["x"]
        assert db.check(full=True).ok


def test_check_detects_unregistered_signal_heads(tmp_path, monkeypatch):
    with DB(tmp_path / "db", mode="a") as db:
        db.store({"x": ([1], [2])})
        # Simulate a committed signal missing from the derived name catalog.
        monkeypatch.setattr(db._name_catalog, "creating", lambda *args: nullcontext())
        db.store({"y": ([1], [3])})
        assert db.search() == ["x"]
        report = db.check(full=True)
        assert not report.ok and report.pages == 8
        assert db.rebuild_catalog() == 2
        assert db.search() == ["x", "y"]
        assert db.check(full=True).ok


def test_reader_retries_retired_shards_and_storage_stays_bounded(tmp_path, monkeypatch):
    path = tmp_path / "db"
    with DB(path, mode="a") as writer, DB(path, mode="r") as reader:
        writer.store({"x": ([1], [2])})
        original_read = reader._backend.read
        retired = False

        def read(key, **kwargs):
            nonlocal retired
            if key.startswith("catalog/shards/") and not retired:
                retired = True
                writer.store({"y": ([1], [3])})
                writer.rebuild_catalog()  # removes the reader's captured old shard
            return original_read(key, **kwargs)

        monkeypatch.setattr(reader._backend, "read", read)
        assert reader.search() == ["x", "y"]
        for _ in range(3):
            writer.rebuild_catalog()
        head = unpack(writer._backend.read(NameCatalog.HEAD))
        assert len(list((path / "catalog/shards").glob("*.json"))) == len(
            head["shards"]
        )


def test_lazy_exact_location_and_cached_reads(tmp_path, monkeypatch):
    path = tmp_path / "db"
    with DB(path, mode="a") as writer:
        writer.store({"x": ([1], [2]), "y": ([1], [3])})
    with DB(path, mode="r") as reader:
        reads = []
        original = reader._backend.read

        def read(key, **kwargs):
            reads.append(key)
            return original(key, **kwargs)

        monkeypatch.setattr(reader._backend, "read", read)
        monkeypatch.setattr(
            reader._backend, "listdir", lambda key: pytest.fail("Unexpected scan")
        )
        assert reader.get_signal("x")[1].tolist() == [2]
        assert len([key for key in reads if key.startswith("catalog/shards/")]) == 1
        assert not reader._name_catalog._names  # no full name search/cache load
        reads.clear()
        assert reader.get_signal("x")[1].tolist() == [2]
        assert not any(key.startswith("catalog/") for key in reads)


@pytest.mark.parametrize(
    "phase",
    [
        "shard",
        "reservation_before",
        "reservation_after",
        "identity_before",
        "identity_after",
    ],
)
def test_interrupted_location_reservation_is_hidden_and_retryable(
    tmp_path, monkeypatch, phase
):
    from pagestore.catalog import signal_prefix

    path = tmp_path / "db"
    with DB(path, mode="a") as db:
        db.rebuild_catalog()
        original = db._backend.publish

        def publish(key, *args, **kwargs):
            reservation = key == NameCatalog.HEAD
            identity = key.endswith("/SIGNAL.json")
            if (
                (phase == "shard" and key.startswith("catalog/shards/"))
                or (phase == "reservation_before" and reservation)
                or (phase == "identity_before" and identity)
            ):
                raise OSError("injected reservation failure")
            result = original(key, *args, **kwargs)
            if (phase == "reservation_after" and reservation) or (
                phase == "identity_after" and identity
            ):
                raise OSError("injected reservation failure")
            return result

        monkeypatch.setattr(db._backend, "publish", publish)
        with pytest.raises(StoreError):
            db.store({"new": ([1], [2])})
        with DB(path, mode="r") as reader:
            assert reader.search() == []
        assert not (path / signal_prefix("new", 0) / "HEAD.json").exists()
        monkeypatch.setattr(db._backend, "publish", original)
        db.store({"new": ([1], [2])})
        assert db._signal_prefix("new") == signal_prefix("new", 0)
        assert db.search() == ["new"]
        assert db.check(full=True).ok


def test_reader_does_not_cache_unanchored_reservation_across_rebuild(
    tmp_path, monkeypatch
):
    from pagestore import SignalNotFoundError
    from pagestore.catalog import signal_location

    path = tmp_path / "db"
    with DB(path, mode="a") as writer, DB(path, mode="r") as reader:
        original = writer._backend.publish

        def publish(key, *args, **kwargs):
            if key.endswith("/SIGNAL.json"):
                raise OSError("identity was not published")
            return original(key, *args, **kwargs)

        monkeypatch.setattr(writer._backend, "publish", publish)
        with pytest.raises(StoreError):
            writer.store({"new": ([1], [2])})
        with pytest.raises(SignalNotFoundError):
            reader.get_signal("new")
        assert "new" not in reader._name_catalog._locations
        monkeypatch.setattr(writer._backend, "publish", original)
        assert writer.rebuild_catalog() == 0
        writer.store({"other": ([1], [3]), "new": ([1], [2])})
        assert signal_location(writer._signal_prefix("new"))[0] == 1
        assert reader.get_signal("new")[1].tolist() == [2]


def test_rebuild_preserves_reservations_and_deleted_locations(tmp_path):
    path = tmp_path / "db"
    with DB(path, mode="a") as db:
        db._signal_prefix("reserved", create=True)
        db.store({"deleted": ([1], [2]), "live": ([1], [3])})
        db.delete_signal("deleted")
        prefixes = {n: db._signal_prefix(n) for n in ("reserved", "deleted", "live")}
    shutil.rmtree(path / "catalog")
    with DB(path, mode="r") as reader:
        assert reader.search() == ["live"]
        assert reader.get_signal("live")[1].tolist() == [3]
        assert not (path / "catalog").exists()
    with DB(path, mode="a") as db:
        assert db.rebuild_catalog() == 1
        assert all(db._signal_prefix(n) == p for n, p in prefixes.items())
        assert (
            db._name_catalog._decode(db._backend.read(NameCatalog.HEAD))[
                "next_signal_ordinal"
            ]
            == 3
        )
        db.store({"reserved": ([1], [4]), "deleted": ([2], [5])})
        assert all(db._signal_prefix(n) == p for n, p in prefixes.items())
        assert db.check(full=True).ok


def test_duplicate_allocation_detected_by_rebuild_and_check(tmp_path):
    from pagestore.catalog import envelope, signal_prefix

    with DB(tmp_path / "db", mode="a") as db:
        db.store({"x": ([1], [2])})
        # Simulate two identity anchors claiming the same allocation slot.
        db._backend.publish(
            signal_prefix("other", 0) + "/SIGNAL.json",
            [envelope(db._identity("other", 0))],
        )
        with pytest.raises(CorruptionError, match="Duplicate"):
            db.rebuild_catalog()
        report = db.check(full=True)
        assert not report.ok and any("Duplicate" in error for error in report.errors)
