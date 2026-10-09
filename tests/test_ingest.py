import multiprocessing

import numpy as np
import pytest

from pagestore import Batch, DB, IngestError, OverlapError
from pagestore.maintenance import recover


def _initialize_worker(barrier):
    global _start_barrier
    _start_barrier = barrier


def _worker(arguments):
    path, name, start, method = arguments
    _start_barrier.wait(timeout=15)
    with DB(path, mode="a") as db:
        batch = Batch(np.arange(start, start + 1000), np.arange(start, start + 1000))
        if method == "store":
            return db.store({name: batch}, max_page_size=8192)[name].inserted
        return db.ingest(
            name,
            [batch],
            max_page_size=8192,
        ).inserted


def _overlap_worker(arguments):
    path, index = arguments
    _start_barrier.wait(timeout=15)
    with DB(path, mode="a") as db:
        result = db.store({"shared": ([0, 10000 + index], [-index - 1, index])})[
            "shared"
        ]
        return result.inserted, result.replaced


@pytest.mark.parametrize("directory_locks", [False, True])
@pytest.mark.parametrize("method", ["store", "ingest"])
def test_parallel_signals_and_same_signal_coordination(
    tmp_path, directory_locks, method
):
    path = (
        f"file://{tmp_path}/db?profile=generic" if directory_locks else tmp_path / "db"
    )
    context = multiprocessing.get_context("spawn")
    with context.Pool(
        4, initializer=_initialize_worker, initargs=(context.Barrier(4),)
    ) as pool:
        # All four workers open the absent store concurrently through normal DB().
        assert (
            pool.map(
                _worker, [(path, f"signal-{i}", i * 1000, method) for i in range(4)]
            )
            == [1000] * 4
        )
        assert (
            pool.map(_worker, [(path, "shared", i * 1000, method) for i in range(4)])
            == [1000] * 4
        )
        # Colliding ordinary store() calls must retain every disjoint addition.
        assert pool.map(_overlap_worker, [(path, i) for i in range(4)]) == [(1, 1)] * 4
    with DB(path, mode="r") as db:
        assert db.count_signal("shared") == 4004
        times, values = db.get_signal("shared")
        np.testing.assert_array_equal(
            times, np.r_[np.arange(4000), np.arange(10000, 10004)]
        )
        assert values[0] in {-1, -2, -3, -4}
        np.testing.assert_array_equal(values[1:4000], np.arange(1, 4000))
        np.testing.assert_array_equal(values[4000:], np.arange(4))
        assert db.search() == ["shared", "signal-0", "signal-1", "signal-2", "signal-3"]
        assert db.check(full=True).ok


def test_ingest_bounded_groups_checkpoint_and_partial_progress(tmp_path):
    with DB(tmp_path / "db", default_max_page_size=4096, mode="a") as db:

        def batches():
            for i in range(5):
                yield Batch(np.arange(i * 256, (i + 1) * 256), np.arange(256))

        result = db.ingest("x", batches(), commit_bytes=4096)
        assert result.commits == 5 and result.inserted == result.total == 1280
        assert (result.batch_index, result.record_offset) == (4, 256)
        manifest, _ = db._head("x")
        assert manifest["recovery_kind"] == "checkpoint"
        assert len(list(db._index(manifest).pages())) > 5
        with pytest.raises(IngestError) as caught:
            db.ingest(
                "y",
                [
                    Batch(np.arange(256), np.arange(256)),
                    Batch(np.array([0]), np.array([9])),
                ],
                commit_bytes=4096,
            )
        assert caught.value.progress.commits == 1
        assert caught.value.progress.total == 256
        assert isinstance(caught.value.cause, OverlapError)
        assert db.count_signal("y") == 256
        assert db.ingest("empty", []).commits == 0
        assert "empty" not in db.search()
        assert db.check(full=True).ok
    assert recover(tmp_path / "db", tmp_path / "new").complete


def test_ingest_replace_and_datetime_strings(tmp_path):
    with DB(tmp_path / "db", mode="a") as db:
        db.ingest("x", [(["2024-01-01 12:00:00"], [1])], timezone="cern")
        result = db.ingest("x", [(["2024-01-01T11:00:00Z"], [2])], on_overlap="replace")
        assert result.replaced == 1 and result.inserted == 0
        assert db.get_signal("x")[1].tolist() == [2]


def test_empty_datetime_batches_and_page_sized_coalescing(tmp_path, monkeypatch):
    import pagestore.model as model

    original = model.concatenate
    largest_copy = 0

    def bounded(batches):
        nonlocal largest_copy
        if len(batches) > 1:
            largest_copy = max(largest_copy, sum(b.nbytes for b in batches))
        return original(batches)

    monkeypatch.setattr(model, "concatenate", bounded)
    with DB(tmp_path / "db", mode="a") as db:
        db.store({"date": (["2024-01-01"], [1])})
        assert db.ingest("date", [([], [])]).commits == 0
        page_size = 32768
        result = db.ingest(
            "x",
            (
                Batch(np.arange(i, i + 1024), np.ones(1024))
                for i in range(0, 65536, 1024)
            ),
            max_page_size=page_size,
        )
        assert result.total == 65536
        assert largest_copy <= page_size
        manifest, _ = db._head("x")
        assert all(d["size"] <= page_size for d in db._index(manifest).pages())
        assert db.check(full=True).ok
