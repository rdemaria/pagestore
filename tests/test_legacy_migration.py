import hashlib
import json
import struct

import numpy as np
import pytest

from pagestore import Batch, DB, RaggedArray
from pagestore.legacy.migration import (
    decode_numeric_page,
    digest_stream,
    import_verified_batch,
)


def fixture(ragged=False):
    times = np.array([1.5, 2.5, 3.5], dtype="<f8")
    values = np.arange(6, dtype=">i2")
    files = {"idx": times.tobytes(), "rec": values.tobytes()}
    if ragged:
        files["len"] = np.array([0, 2, 4], dtype="<i8").tobytes()
    raw = b"".join(files[k] for k in ["idx", "len", "rec"] if k in files)
    meta = dict(
        comp=None,
        idxtype="<f8",
        rectype=">i2",
        count=3,
        reclen=-1 if ragged else 2,
        recsize=12,
        idxa=1.5,
        idxb=3.5,
        checksum=hashlib.md5(raw).hexdigest(),
    )
    return meta, files


@pytest.mark.parametrize("ragged", [False, True])
def test_verified_legacy_roundtrip_and_boundary_independent_digest(tmp_path, ragged):
    meta, files = fixture(ragged)
    batch = decode_numeric_page(meta, files)
    expected = digest_stream([batch])
    assert (
        digest_stream([batch.take(slice(0, 1)), batch.take(slice(1, None))]) == expected
    )
    with DB(tmp_path / "db", mode="a") as db:
        db.ingest("test", [batch])
        with db.iter_signal("test") as stream:
            assert digest_stream(stream) == expected
        assert db.check(full=True).ok
    assert files["rec"] == np.arange(6, dtype=">i2").tobytes()


def test_reject_corruption_bad_metadata_and_duplicate_timestamps():
    meta, files = fixture()
    with pytest.raises(ValueError, match="checksum"):
        decode_numeric_page(meta, {**files, "rec": bytes(12)})
    with pytest.raises(ValueError, match="bounds"):
        decode_numeric_page({**meta, "idxb": 7}, files)
    with pytest.raises(ValueError, match="Compressed"):
        decode_numeric_page({**meta, "comp": "gzip"}, files)
    with pytest.raises(ValueError, match="explicit endianness"):
        decode_numeric_page({**meta, "idxtype": "float64"}, files)
    duplicate = {**files, "idx": np.array([1.5, 1.5, 3.5], dtype="<f8").tobytes()}
    meta["checksum"] = hashlib.md5(duplicate["idx"] + duplicate["rec"]).hexdigest()
    with pytest.raises(ValueError, match="non-increasing"):
        decode_numeric_page(meta, duplicate)


def test_digest_preserves_shape_dtype_signed_zero_and_nan_payload():
    base = Batch(np.array([1.0]), np.array([0.0]))
    reference = digest_stream([base])
    for values in [np.array([-0.0]), np.array([[0.0]]), np.array([0], dtype="i8")]:
        assert digest_stream([Batch(base.timestamps, values)]) != reference
    a = np.array([0x7FF8000000000001], dtype="u8").view("f8")
    b = np.array([0x7FF8000000000002], dtype="u8").view("f8")
    assert digest_stream([Batch(base.timestamps, a)]) != digest_stream(
        [Batch(base.timestamps, b)]
    )
    assert digest_stream(
        [Batch(base.timestamps, np.array([[1.0, 2.0]]))]
    ) == digest_stream(
        [Batch(base.timestamps, RaggedArray.from_arrays([np.array([1.0, 2.0])]))]
    )


def test_import_resume_after_lost_ack_and_reject_conflicts(tmp_path):
    path = tmp_path / "db"
    batch = decode_numeric_page(*fixture())
    first = import_verified_batch(path, "x", batch)
    assert first["action"] == "imported"
    # A lost journal acknowledgement is reconciled from complete committed data.
    resumed = import_verified_batch(path, "x", batch)
    assert resumed["action"] == "verified_existing"
    assert resumed["logical_sha256"] == first["logical_sha256"]
    with pytest.raises(ValueError, match="conflicts"):
        import_verified_batch(path, "x", Batch(batch.timestamps, batch.values + 1))
    with DB(path, mode="r") as db:
        assert db.count_signal("x") == 3
        assert db.check(full=True).ok


def test_timestamp_conversion_must_be_explicit_before_migration(tmp_path):
    path = tmp_path / "db"
    with pytest.raises(ValueError, match="explicit int64 or float64"):
        import_verified_batch(
            path, "x", Batch(np.array([1], dtype="i4"), np.array([2.0]))
        )
    assert not path.exists()


def test_resume_committed_data_before_final_checkpoint(tmp_path, monkeypatch):
    path = tmp_path / "db"
    batch = decode_numeric_page(*fixture())
    with DB(path, mode="a") as db:
        db.store({"x": batch})
    assert import_verified_batch(path, "x", batch)["action"] == "verified_existing"
    with DB(path, mode="r") as db:
        manifest, _ = db._head("x")
        assert manifest["recovery_kind"] == "checkpoint"


@pytest.mark.parametrize("shape", [(), (0,), (2,), (2, 3)])
def test_vectorized_digest_matches_record_framing(shape):
    times = np.array([1, 2, 3], dtype="<f8")
    values = np.arange(3 * int(np.prod(shape)), dtype=">f8").reshape((3,) + shape)
    reference = hashlib.sha256(b"pagestore-migration-records-v1\0")
    for i, value in enumerate(values):
        value = np.asarray(value)
        schema = json.dumps(
            ["<f8", "<f8", list(value.shape)], separators=(",", ":")
        ).encode()
        raw = value.astype("<f8").tobytes()
        reference.update(struct.pack("<Q", len(schema)) + schema)
        reference.update(times[i : i + 1].tobytes())
        reference.update(struct.pack("<Q", len(raw)) + raw)
    assert digest_stream([Batch(times, values)]) == (3, reference.hexdigest())


def test_repeated_commit_receipts_and_unacknowledged_attempt(tmp_path, monkeypatch):
    from examples.migrate_legacy_pilot import canonical, publish_receipt
    from pagestore.backends import FileBackend

    remote = FileBackend(tmp_path / "audit")
    work = tmp_path / "work"
    first = publish_receipt(remote, work, dict(unit_id="u", commit_id="c", seconds=1))
    second = publish_receipt(remote, work, dict(unit_id="u", commit_id="c", seconds=2))
    assert first["audit_key"] != second["audit_key"]
    assert remote.read(first["audit_key"]) == canonical(first)
    assert remote.read(second["audit_key"]) == canonical(second)

    def fail(*args, **kwargs):
        raise OSError("lost acknowledgement")

    monkeypatch.setattr(remote, "publish", fail)
    with pytest.raises(OSError, match="lost acknowledgement"):
        publish_receipt(remote, work, dict(unit_id="u", commit_id="c", seconds=3))
    assert (work / "done" / "u.json").read_bytes() == canonical(second)
