import numpy as np
import pytest

from pagestore import CorruptionError, DB
from pagestore.catalog import canonical
from pagestore.page_format import data_plan, decode, read_page


def test_explicit_storage_declarations_and_contradictions(tmp_path):
    with DB(tmp_path / "db", mode="a") as db:
        db.store({"x": (np.array([1, 2], dtype=">i8"), np.array([3, 4], dtype=">f4"))})
        manifest, _ = db._head("x")
        descriptor = next(db._index(manifest).pages())
        page = read_page(db._backend.path(descriptor["key"]), full=True)
        assert [s["dtype"] for s in page.header["sections"]] == ["<i8", "<f4"]
        assert all(s["byte_order"] == "little" for s in page.header["sections"])
        plan = data_plan(page.batch(), page.header)
        assert len(plan.to_bytes()) == plan.size
        # Preserve all lengths and recompute both envelope hashes to exercise
        # semantic validation rather than merely catching a checksum mismatch.
        plan.header["sections"][0]["byte_order"] = "middle"
        plan.raw_header = canonical(plan.header)
        with pytest.raises(CorruptionError, match="encoding"):
            decode(plan.to_bytes(), full=True)


def test_boolean_bytes_are_canonicalized(tmp_path):
    values = np.array([0, 2, 255], dtype="u1").view("?")
    with DB(tmp_path / "db", mode="a") as db:
        db.store({"x": ([1, 2, 3], values)})
        assert db.get_signal("x")[1].tolist() == [False, True, True]
        assert db.check(full=True).ok


def test_truncation_and_both_headers_destroyed(tmp_path):
    with DB(tmp_path / "db", mode="a") as db:
        db.store({"x": ([1], [2])})
        manifest, _ = db._head("x")
        d = next(db._index(manifest).pages())
        page = read_page(db._backend.path(d["key"]))
        raw = bytes(page.buffer)
        for length in (0, 10, len(raw) - 1):
            with pytest.raises(CorruptionError):
                decode(raw[:length], full=True)
        damaged = bytearray(raw)
        damaged[20] ^= 1
        damaged[page.header["backup_offset"] + 20] ^= 1
        with pytest.raises(CorruptionError, match="Neither"):
            decode(damaged, full=True, allow_damaged_envelope=True)
