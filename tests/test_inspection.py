import shutil

import numpy as np
import pytest

from pagestore import CorruptionError, DB, RaggedArray, read_page
from pagestore.page_format import PREFIX, data_plan, read_page as decode_page


def standalone_page(tmp_path, timestamps, values):
    source = tmp_path / "source"
    with DB(source, mode="a") as db:
        db.store({"signal/µ": (timestamps, values)})
        manifest, _ = db._head("signal/µ")
        descriptor = next(db._index(manifest).pages())
        path = tmp_path / "anonymous.pg"
        shutil.copyfile(db._backend.path(descriptor["key"]), path)
    shutil.rmtree(source)
    return path


@pytest.mark.parametrize(
    "timestamps,expected",
    [
        (np.array([1, 2], dtype=">i8"), np.array([1, 2], dtype="i8")),
        ([1.5, 2.5], np.array([1.5, 2.5], dtype="f8")),
        (
            ["2024-01-01T01:00:00+01:00", "2024-01-02T00:00:00Z"],
            np.array(["2024-01-01", "2024-01-02"], dtype="datetime64[ns]"),
        ),
    ],
)
def test_standalone_extraction_preserves_semantics_and_owns_arrays(
    tmp_path, timestamps, expected
):
    values = np.array([[1, 2], [3, 4]], dtype=">i2")
    path = standalone_page(tmp_path, timestamps, values)
    original = path.read_bytes()
    name, times, records = read_page(path)
    assert name == "signal/µ"
    np.testing.assert_array_equal(times, expected)
    np.testing.assert_array_equal(records, values)
    assert times.dtype == expected.dtype
    assert records.dtype == np.dtype("i2")
    assert times.flags.owndata and times.flags.writeable
    assert records.flags.owndata and records.flags.writeable
    records[0, 0] = 999
    assert path.read_bytes() == original
    path.unlink()
    np.testing.assert_array_equal(times, expected)
    assert records[1, 1] == 4


def test_standalone_ragged_records_are_owned_object_arrays(tmp_path):
    values = RaggedArray.from_arrays(
        [
            np.ones((2, 3), dtype=">f4"),
            np.empty((0, 3), dtype=">f4"),
        ]
    )
    path = standalone_page(tmp_path, [1, 2], values)
    name, times, records = read_page(path)
    assert name == "signal/µ" and records.dtype == object
    assert records.shape == (2,)
    for i in range(2):
        np.testing.assert_array_equal(records[i], values.record(i))
        assert records[i].dtype == np.dtype("f4")
        assert records[i].flags.owndata and records[i].flags.writeable


@pytest.mark.parametrize(
    "damage", ["timestamps", "values", "primary", "backup", "digest", "padding"]
)
def test_read_page_always_verifies_all_hashes(tmp_path, damage):
    path = standalone_page(tmp_path, [1, 2], [10, 20])
    header = decode_page(path).header
    original = path.read_bytes()
    offsets = {
        "primary": PREFIX.size + 10,
        "backup": header["backup_offset"] + 10,
        "digest": len(original) - 1,
        "padding": header["sections"][0]["offset"] - 1,
        **{section["role"]: section["offset"] for section in header["sections"]},
    }
    damaged = bytearray(original)
    damaged[offsets[damage]] ^= 1
    path.write_bytes(damaged)
    with pytest.raises(CorruptionError):
        read_page(path)
    assert path.read_bytes() == damaged


def test_recovery_pages_are_not_returned_as_measurements(tmp_path):
    with DB(tmp_path / "db", mode="a") as db:
        path = db._backend.path(f"pages/recovery/store-{db._config['config_id']}.0.pg")
        with pytest.raises(ValueError, match="measurement page"):
            read_page(path)


def test_consistently_hashed_but_inconsistent_signal_identity_is_rejected(tmp_path):
    path = standalone_page(tmp_path, [1, 2], [10, 20])
    page = decode_page(path, full=True)
    metadata = dict(page.header, signal_name="different")
    path.write_bytes(data_plan(page.batch(), metadata).to_bytes())
    with pytest.raises(CorruptionError, match="signal identity"):
        read_page(path)
