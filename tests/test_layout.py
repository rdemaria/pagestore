"""Fanout, persistent allocation, and version rejection for the current layout."""

from collections import Counter
from pathlib import PurePosixPath

import numpy as np
import pytest

from pagestore import DB, CorruptionError
from pagestore.catalog import envelope, ordinal_directory, ordinal_path, signal_prefix
from pagestore.maintenance import recover, salvage
from pagestore.page_format import recovery_plan


def test_million_signal_and_object_directory_fanout():
    # Only compute keys: do not create a million files or touch retained stores.
    buckets = Counter(
        signal_prefix(f"benchmark:signal:{i:08d}", i).rsplit("/", 1)[0]
        for i in range(1_000_000)
    )
    assert max(buckets.values()) == 128
    assert max(len(PurePosixPath(p).parts) for p in buckets) == 3

    files = Counter(ordinal_directory(i) for i in range(1_000_000))
    directories = set(files) - {""}
    for directory in list(directories):
        while "/" in directory:
            directory = directory.rsplit("/", 1)[0]
            directories.add(directory)
    children = Counter(p.rpartition("/")[0] for p in directories)
    assert max(files.values()) == 128
    assert max(children.values()) <= 128
    assert max(files[p] + children[p] for p in files) <= 256
    assert max(2 * files[p] + children[p] for p in files) <= 384


@pytest.mark.parametrize(
    "ordinal, expected",
    [
        (0, "00"),
        (127, "7f"),
        (128, "01/00"),
        (16383, "7f/7f"),
        (16384, "01/00/00"),
        (128**3, "01/00/00/00"),
    ],
)
def test_radix_boundaries(ordinal, expected):
    assert ordinal_path(ordinal) == expected


def test_cross_boundaries_reopen_and_recover(tmp_path, monkeypatch):
    import pagestore.page_index as index_module

    monkeypatch.setattr(index_module, "MAX_LEAF", 4)
    monkeypatch.setattr(index_module, "MAX_CHILDREN", 4)
    path = tmp_path / "store"
    with DB(path, mode="a") as db:
        db.store({"x": (np.arange(600), np.arange(600))}, max_page_size=1)
        manifest, _ = db._head("x")
        assert manifest["next_index_ordinal"] > 128
        old_index_keys = set((path / db._signal_prefix("x") / "index").rglob("*.json"))
        old_page_keys = {d["key"] for d in db._index(manifest).pages()}
        assert any("/pages/02/" in key for key in old_page_keys)
        assert any("/index/01/" in str(key) for key in old_index_keys)
        next_index = manifest["next_index_ordinal"]
        for i in range(127):
            db.configure_signal("x", max_page_size=8192 + i)
        manifest, ref = db._head("x")
        assert manifest["generation"] == 128
        assert "/manifests/01/" not in ref["key"]

    # All allocation state must survive closing the writer.
    with DB(path, mode="a") as db:
        db.store({"x": ([600], [600])})
        manifest, ref = db._head("x")
        assert manifest["generation"] == 129
        assert manifest["next_index_ordinal"] > next_index
        assert "/manifests/01/" in ref["key"]
        assert "/recovery/01/" in db._recovery_key(manifest, 0)
        assert all(p.exists() for p in old_index_keys)
        assert old_page_keys < {d["key"] for d in db._index(manifest).pages()}
        np.testing.assert_array_equal(db.get_signal("x")[1], np.arange(601))
        assert db.rebuild_catalog() == 1
        assert db.check(full=True).ok
        signal = path / db._signal_prefix("x")
        for directory in signal.rglob("*"):
            if directory.is_dir():
                assert len(list(directory.iterdir())) <= 384

    report = recover(path, tmp_path / "recovered")
    assert report.complete, report.errors
    with DB(tmp_path / "recovered", mode="r") as db:
        np.testing.assert_array_equal(db.get_signal("x")[1], np.arange(601))
        assert db.check(full=True).ok


@pytest.mark.parametrize("reconstruct", [recover, salvage])
def test_reconstruction_preserves_measurement_bytes(tmp_path, reconstruct):
    path = tmp_path / "source"
    with DB(path, mode="a") as db:
        db.store({"x": (np.arange(3), np.arange(3))}, max_page_size=1)
        manifest, _ = db._head("x")
        descriptors = list(db._index(manifest).pages())
        original = {p["page_id"]: db._backend.read(p["key"]) for p in descriptors}

    report = reconstruct(path, tmp_path / "new")
    assert not report.errors, report.errors
    with DB(tmp_path / "new", mode="a") as db:
        assert db._config["format_version"] == 3
        assert len(PurePosixPath(db._signal_prefix("x")).parts) == 2
        assert db.search() == ["x"]
        manifest, _ = db._head("x")
        assert all(
            db._backend.read(d["key"]) == original[d["page_id"]]
            for d in db._index(manifest).pages()
        )
        db.store({"x": ([3], [3])})
        np.testing.assert_array_equal(db.get_signal("x")[0], np.arange(4))
        assert db.check(full=True).ok


@pytest.mark.parametrize("version", [1, 2, 4])
def test_unsupported_store_layout_rejected_without_mutation(tmp_path, version):
    with DB(tmp_path / "db", mode="a") as db:
        config = dict(db._config, format_version=version)
        db._backend.publish("store.json", [envelope(config)], replace=True)
    before = {
        p: (p.stat().st_mtime_ns, p.read_bytes())
        for p in (tmp_path / "db").rglob("*")
        if p.is_file()
    }
    for mode in ("r", "a"):
        with pytest.raises(CorruptionError, match="Unsupported database format"):
            DB(tmp_path / "db", mode=mode)
    assert before == {
        p: (p.stat().st_mtime_ns, p.read_bytes())
        for p in (tmp_path / "db").rglob("*")
        if p.is_file()
    }

    source = tmp_path / "pages"
    source.mkdir()
    record = {
        "scope": "database",
        "record_kind": "checkpoint",
        "config": config,
        "commit_id": config["config_id"],
    }
    (source / "config.pg").write_bytes(recovery_plan(record).to_bytes())
    report = recover(source, tmp_path / "recovered")
    assert not report.complete and "Unsupported" in report.errors[0]
    assert not (tmp_path / "recovered").exists()


def test_signal_tree_grows_without_moving_existing_paths(tmp_path, monkeypatch):
    from pagestore.catalog import signal_location, unpack
    from pagestore.name_catalog import NameCatalog

    path = tmp_path / "store"
    with DB(path, mode="a") as db:
        for i in range(128):
            db.store({f"s{i}": ([0], [i])})
        prefixes = {f"s{i}": db._signal_prefix(f"s{i}") for i in range(128)}
        assert all(len(PurePosixPath(p).parts) == 2 for p in prefixes.values())
        assert len(list((path / "signals").iterdir())) == 128
        db.store({"s128": ([0], [128])})
        assert db._signal_prefix("s128").startswith("signals/01/00-")
        assert all(db._signal_prefix(n) == p for n, p in prefixes.items())

        # Skip unused slots to exercise the next depth without creating 16K stores.
        head = unpack(db._backend.read(NameCatalog.HEAD))
        head["next_signal_ordinal"] = 16383
        db._backend.publish(NameCatalog.HEAD, [envelope(head)], replace=True)
        db.store({"last-shallow": ([0], [1]), "first-deep": ([0], [2])})
        assert db._signal_prefix("last-shallow").startswith("signals/7f/7f-")
        assert db._signal_prefix("first-deep").startswith("signals/01/00/00-")
        assert signal_location(db._signal_prefix("first-deep"))[0] == 16384
        prefixes.update(
            {n: db._signal_prefix(n) for n in ("s128", "last-shallow", "first-deep")}
        )
        original_listdir = db._backend.listdir

        def listdir(key):
            assert key == "signals" or all(len(p) == 2 for p in key.split("/")[1:])
            return original_listdir(key)

        monkeypatch.setattr(db._backend, "listdir", listdir)
        assert db.rebuild_catalog() == 131
        assert db.check(full=True).ok

    with DB(path, mode="a") as db:
        assert all(db._signal_prefix(n) == p for n, p in prefixes.items())
        db.store({"next": ([0], [3])})
        assert db._signal_prefix("next").startswith("signals/01/00/01-")
        assert db.delete_signal("s0") == 1
        assert db.rebuild_catalog() == 131
        db.store({"s0": ([10], [42])})
        assert db._signal_prefix("s0") == prefixes["s0"]
        assert db.get_signal("s0")[1].tolist() == [42]
        assert db.check(full=True).ok


@pytest.mark.parametrize(
    "prefix",
    [
        "signals/00/01-" + "a" * 64,
        "signals/80-" + "a" * 64,
        "signals/00-" + "A" * 64,
        "signals/01/x/00-" + "a" * 64,
    ],
)
def test_noncanonical_signal_paths_rejected(prefix):
    from pagestore.catalog import signal_location

    with pytest.raises(CorruptionError):
        signal_location(prefix)
