import numpy as np
import pytest

from pagestore import Batch, DB, RaggedArray, SignalNotFoundError, StoreError


def test_sort_duplicates_upsert_and_selection(tmp_path):
    with DB(tmp_path / "db", default_max_page_size=4096, mode="a") as db:
        result = db.store({"a/path": ([3, 2, 1, 2], [30, 20, 10, 22])})["a/path"]
        assert (result.inserted, result.replaced, result.total) == (3, 0, 3)
        result = db.store({"a/path": ([2, 4], [23, 40])})["a/path"]
        assert (result.inserted, result.replaced, result.total) == (1, 1, 4)
        t, v = db.get_signal("a/path")
        np.testing.assert_array_equal(t, [1, 2, 3, 4])
        np.testing.assert_array_equal(v, [10, 23, 30, 40])
        assert db.search("path$") == ["a/path"]
        assert list(db.get(["a/path", "a/path"])) == ["a/path"]
        assert db.get("nothing") == {}
        for low in (None, 0, 1, 2, 4, 5):
            for high in (None, 5):
                for skip in (0, 1, 5):
                    for maximum in (None, 0, 1, 3):
                        actual = db.get_signal(
                            "a/path", low, high, skip=skip, max_count=maximum
                        )[0]
                        mask = np.ones(len(t), dtype=bool)
                        if low is not None:
                            mask &= t >= low
                        if high is not None:
                            mask &= t <= high
                        expected = t[mask][:: skip + 1]
                        if maximum is not None:
                            expected = expected[:maximum]
                        np.testing.assert_array_equal(actual, expected)
                        assert db.count_signal(
                            "a/path", low, high, skip=skip, max_count=maximum
                        ) == len(actual)
        assert db.check(full=True).ok
    assert (
        t.flags.owndata and v.flags.owndata and t.flags.writeable and v.flags.writeable
    )
    with DB(tmp_path / "db", mode="r") as db:
        np.testing.assert_array_equal(db.get_signal("a/path")[1], v)
        with pytest.raises(PermissionError):
            db.store({"x": ([1], [2])})


def test_many_pages_global_skip_and_no_old_payload_reads(tmp_path, monkeypatch):
    import pagestore.page_index as index_module

    monkeypatch.setattr(index_module, "MAX_LEAF", 4)
    monkeypatch.setattr(index_module, "MAX_CHILDREN", 4)
    with DB(tmp_path / "db", default_max_page_size=1, mode="a") as db:
        db.store({"x": (np.arange(80), np.arange(80))})
        before = set((tmp_path / "db").rglob("index/**/*.json"))
        original = db._read_data

        def reject(*args, **kwargs):
            raise AssertionError("fresh append must not read existing payload")

        monkeypatch.setattr(db, "_read_data", reject)
        db.store({"x": ([80, 81], [80, 81])})
        after = set((tmp_path / "db").rglob("index/**/*.json"))
        assert len(after - before) <= 8  # changed leaf and its ancestors only
        assert db.count_signal("x") == 82  # root aggregate, no payload read
        monkeypatch.setattr(db, "_read_data", original)
        expected = np.arange(7, 74)[::6][:8]
        np.testing.assert_array_equal(
            db.get_signal("x", 7, 73, skip=5, max_count=8)[0], expected
        )
        assert db.info_signal("x").page_count == 82
        assert db.check(full=True).ok


@pytest.mark.parametrize(
    "dtype",
    [
        "?",
        "i1",
        ">i2",
        ">i4",
        ">i8",
        "u1",
        ">u8",
        ">f2",
        ">f4",
        ">f8",
        ">c8",
        ">c16",
        "S8",
        ">U8",
    ],
)
def test_value_dtypes_and_endianness(tmp_path, dtype):
    values = np.array([[1, 2], [3, 4]], dtype=dtype)
    with DB(tmp_path / "db", mode="a") as db:
        db.store({"x": (np.array([1, 2], dtype=">i8"), values)})
        t, actual = db.get_signal("x")
        np.testing.assert_array_equal(actual, values)
        assert actual.dtype == values.dtype.newbyteorder("=")
        assert db.check(full=True).ok


def test_mixed_schema_and_ragged_roundtrip(tmp_path):
    mixed = np.empty(4, dtype=object)
    mixed[:] = [
        np.int16(3),
        np.array([1, 2], dtype="f4"),
        np.float64(9),
        np.array([], dtype="u2"),
    ]
    with DB(tmp_path / "db", mode="a") as db:
        db.store({"mix": ([1, 2, 3, 4], mixed)})
        t, actual = db.get_signal("mix")
        assert actual.shape == (4,)
        for a, b in zip(actual, mixed):
            assert a.dtype == b.dtype and a.shape == b.shape
            np.testing.assert_array_equal(a, b)
        assert db.get_signal("mix", 2, 2)[1].shape == (1, 2)
        db.store({"copy": (t, actual)})
        ragged = RaggedArray.from_arrays(
            [
                np.ones((3, 2), dtype="f4"),
                np.empty((0, 2), dtype="f4"),
                np.zeros((1, 2), dtype="f4"),
            ]
        )
        db.store({"ragged": Batch(np.array([3, 1, 2]), ragged)})
        with db.iter_signal("ragged", skip=1) as stream:
            batch = next(stream)
            assert isinstance(batch.values, RaggedArray)
            assert batch.values.offsets.tolist() == [0, 0, 3]
        actual = db.get_signal("ragged")[1]
        assert [r.shape for r in actual] == [(0, 2), (1, 2), (3, 2)]
        assert db.check(full=True).ok


def test_page_limit_settings_and_oversized_record(tmp_path):
    with DB(tmp_path / "db", mode="a") as db:
        db.store({"default": ([1], [2])})
        assert db.info_signal("default").max_page_size == 8 * 1024**2
        db.store({"small": (np.arange(300), np.ones((300, 16)))}, max_page_size=8192)
        assert db.info_signal("small").page_count > 1
        manifest, _ = db._head("small")
        assert all(d["size"] <= 8192 for d in db._index(manifest).pages())
        db.configure_signal("small", max_page_size=32768)
        db.store({"small": ([301], np.ones((1, 16)))})
        assert db.info_signal("small").max_page_size == 32768
        db.store({"large": ([1], np.ones((1, 8192)))}, max_page_size=8192)
        assert (
            db.info_signal("large").page_count == 1
            and db.info_signal("large").stored_bytes > 8192
        )
        assert db.check(full=True).ok


def test_stream_snapshot_and_array_lifetime(tmp_path):
    with DB(tmp_path / "db", mode="a") as db:
        db.store({"x": ([1, 2], [10, 20])})
        stream = db.iter_signal("x")
        stream.__enter__()
        db.store({"x": ([2, 3], [21, 30])})
        batch = next(stream)
        stream.close()
        assert list(stream) == []
    np.testing.assert_array_equal(batch.values, [10, 20])
    assert not batch.values.flags.writeable


def test_validation_before_write_and_partial_store(tmp_path):
    with DB(tmp_path / "db", mode="a") as db:
        assert db.store({"empty": ([], [])}) == {}
        assert db.search() == []
        with pytest.raises(ValueError):
            db.store({"ok": ([1], [2]), "bad": ([1, 2], [3])})
        assert db.search() == []
        db.store({"date": (["2024-01-01"], [1])})
        with pytest.raises(StoreError) as caught:
            db.store({"ok": ([1], [2]), "date": ([1], [3])})
        assert list(caught.value.results) == ["ok"]
        assert caught.value.failed_name == "date"
        with pytest.raises(SignalNotFoundError):
            db.get_signal("missing")
        for kwargs in ({"skip": True}, {"max_count": -1}, {"skip": 1.5}):
            with pytest.raises((TypeError, ValueError)):
                db.get_signal("ok", **kwargs)
        with pytest.raises(ValueError):
            db.get_signal("ok", 2, 1)
        with pytest.raises(ValueError):
            db.search("[")


def test_randomized_upserts(tmp_path):
    rng = np.random.default_rng(451)
    expected = {}
    with DB(tmp_path / "db", default_max_page_size=3500, mode="a") as db:
        for _ in range(20):
            times = rng.integers(-100, 100, size=40)
            values = rng.normal(size=40)
            expected.update(zip(times.tolist(), values.tolist()))
            db.store({"x": (times, values)})
        actual = db.get_signal("x")
        np.testing.assert_array_equal(actual[0], sorted(expected))
        np.testing.assert_array_equal(
            actual[1], [expected[k] for k in sorted(expected)]
        )
        assert db.check(full=True).ok


def test_statistics_empty_records_and_fixed_string_widths(tmp_path):
    with DB(tmp_path / "db", mode="a") as db:
        db.store(
            {
                "float": ([1, 2, 3, 4], [np.nan, np.inf, -np.inf, 3.0]),
                "empty": ([1, 2], np.empty((2, 0), dtype="f4")),
            }
        )
        stats = db.info_signal("float").schemas[0]
        assert (
            stats["min"] == -np.inf
            and stats["max"] == np.inf
            and stats["nan_count"] == 1
        )
        assert db.info_signal("empty").schemas[0]["min"] is None
        db.store(
            {
                "strings": [
                    Batch(np.array([1]), np.array(["a"], dtype="U8")),
                    Batch(np.array([2]), np.array(["b"], dtype="U4")),
                ]
            }
        )
        times, values = db.get_signal("strings")
        assert values[0].dtype == np.dtype("U8") and values[1].dtype == np.dtype("U4")
        db.store({"copy": (times, values)})
        assert db.info_signal("copy").schemas == db.info_signal("strings").schemas
        assert db.check(full=True).ok
