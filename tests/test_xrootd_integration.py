"""Opt-in real-server tests. The URL must name a disposable test parent directory.

PAGESTORE_TEST_XROOTD_URL=root://localhost:1094//tmp/test python -m pytest -q tests/test_xrootd_integration.py
Only new UUID-named children are written; they are retained for inspection.
"""

from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import os
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import numpy as np
import pytest

from pagestore import DB, RaggedArray, LockTimeoutError
from pagestore.backends import XRootDBackend


def _worker(url, name, first=0):
    try:
        with DB(url, io_timeout=10, visibility_timeout=3, mode="a") as db:
            db.ingest(
                name, [(np.arange(first, first + 1000), np.arange(1000, dtype=">f8"))]
            )
    except Exception:
        import traceback

        raise RuntimeError(traceback.format_exc()) from None


def _try_lock(url, key):
    backend = XRootDBackend(url, io_timeout=10, lock_timeout=0.1)
    try:
        with backend.lock(key):
            return "acquired"
    except LockTimeoutError:
        return "blocked"


def _write_epochs(url):
    try:
        with DB(url, io_timeout=10, visibility_timeout=3, mode="a") as db:
            for generation in range(1, 6):
                db.store({"epochs": (np.arange(256), np.full(256, generation))})
    except Exception:
        import traceback

        raise RuntimeError(traceback.format_exc()) from None


@pytest.fixture
def remote_url():
    parent = os.environ.get("PAGESTORE_TEST_XROOTD_URL")
    if not parent:
        pytest.skip("Set PAGESTORE_TEST_XROOTD_URL to a disposable XRootD test parent")
    parsed = urlsplit(parent)
    result = urlunsplit(
        parsed._replace(path=parsed.path.rstrip("/") + "/test-" + uuid4().hex)
    )
    print(f"\nRetained synthetic test store: {result}")
    return result


def test_real_parallel_writes_reads_checks_and_shared_lock(remote_url):
    with DB(remote_url, mode="x", io_timeout=10, visibility_timeout=3) as db:
        db.store(
            {"ragged": ([1, 2], RaggedArray.from_arrays([np.arange(2), np.arange(4)]))}
        )
    with ProcessPoolExecutor(
        3, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        futures = [pool.submit(_worker, remote_url, f"signal-{i}") for i in range(3)]
        for future in futures:
            future.result(timeout=60)
        # Separate clients/processes contend on the actual server namespace.
        with DB(remote_url, io_timeout=10, visibility_timeout=3, mode="a") as db:
            key = db._signal_prefix("signal-0") + "/LOCK"
            with db._backend.lock(key):
                assert (
                    pool.submit(_try_lock, remote_url, key).result(timeout=20)
                    == "blocked"
                )
            assert (
                pool.submit(_try_lock, remote_url, key).result(timeout=20) == "acquired"
            )
            db.store({"signal-0": ([999, 1000], [7.0, 8.0])})
            db.checkpoint("signal-0")
            assert db.rebuild_catalog() == 4
            assert db.check(full=True).ok
    with DB(remote_url, mode="r", io_timeout=10, visibility_timeout=3) as db:
        assert db.search("signal-") == ["signal-0", "signal-1", "signal-2"]
        np.testing.assert_array_equal(db.get_signal("signal-0", 999)[1], [7.0, 8.0])
        assert db.get_signal("ragged")[1][1].tolist() == [0, 1, 2, 3]
        assert db.count_signal("signal-0") == 1001
        assert db.check(full=True).ok


def test_mounted_alias_uses_same_authority(remote_url):
    parent = os.environ.get("PAGESTORE_TEST_MOUNT_PARENT")
    if not parent:
        pytest.skip(
            "Set PAGESTORE_TEST_MOUNT_PARENT for the corresponding mounted directory"
        )
    name = urlsplit(remote_url).path.rsplit("/", 1)[1]
    mount = parent.rstrip("/") + "/" + name
    with DB(
        mount, xrootd_url=remote_url, io_timeout=10, visibility_timeout=3, mode="a"
    ) as alias:
        alias.store({"aliased": ([1, 2], [3, 4])})
        assert alias.backend_info.transport == "xrootd"
        with DB(remote_url, mode="r", io_timeout=10) as direct:
            assert direct._config == alias._config
            assert direct.get_signal("aliased")[1].tolist() == [3, 4]
        other = XRootDBackend(remote_url, io_timeout=10, lock_timeout=0.1)
        with alias._backend.lock("alias-test"):
            with pytest.raises(LockTimeoutError):
                with other.lock("alias-test"):
                    pass
    # Read-only mounted access can lag. It is a checksum-validated byte snapshot,
    # never a live mmap of a cache that may still be filling after close.
    import time

    deadline = time.monotonic() + 15
    while True:
        try:
            with DB(mount, mode="r", visibility_timeout=3) as mounted:
                assert mounted.get_signal("aliased")[1].tolist() == [3, 4]
            break
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def test_large_pages_and_readers_during_updates(remote_url):
    times = np.arange(1_000_000)
    values = times.astype(np.float64) / 8
    with DB(remote_url, mode="x", io_timeout=10, visibility_timeout=3) as db:
        db.ingest("large", [(times, values)])
        assert db.info_signal("large").page_count > 1
        db.store({"epochs": (np.arange(256), np.zeros(256, dtype=np.int64))})
        assert db.check(full=True).ok
    with ProcessPoolExecutor(
        1, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        writing = pool.submit(_write_epochs, remote_url)
        with DB(remote_url, mode="r", io_timeout=10, visibility_timeout=3) as reader:
            while True:
                t, v = reader.get_signal("epochs")
                np.testing.assert_array_equal(t, np.arange(256))
                assert len(v) == 256 and np.all(v == v[0]) and 0 <= v[0] <= 5
                if writing.done():
                    break
            writing.result(timeout=60)
            assert np.all(reader.get_signal("epochs")[1] == 5)
            t, v = reader.get_signal("large")
            np.testing.assert_array_equal(t, times)
            np.testing.assert_array_equal(v, values)
