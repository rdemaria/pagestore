"""Strict, read-only decoding helpers for bounded PyTimber migration pilots.

This is the historical PyTimber binary format, not pagestore.legacy's pickle
format. Callers supply bytes from frozen files; this module never opens a source
database, decompresses files in place, or deletes source data. Unsupported
encodings and duplicate timestamps are rejected rather than normalized silently.
"""

import hashlib
import json
import struct

import numpy as np

from .. import Batch, DB, RaggedArray, SignalNotFoundError


def decode_numeric_page(metadata, files):
    """Verify a legacy MD5 and decode an uncompressed numeric PyTimber page.

    ``files`` maps idx/len/rec extensions to complete immutable file bytes.
    The index must be finite and strictly increasing. Metadata counts, endpoint
    timestamps, byte lengths and record shapes must agree exactly. No sorting,
    timestamp conversion, approximate equality or deduplication is performed.
    """
    m = metadata
    if m["comp"] is not None:
        raise ValueError("Compressed source pages require a separate decoder")
    td, vd = np.dtype(m["idxtype"]), np.dtype(m["rectype"])
    if td.kind not in "iuf" or vd.kind not in "biufc":
        raise ValueError("Pilot decoder supports numeric timestamps and records only")
    for spelling, dtype in ((m["idxtype"], td), (m["rectype"], vd)):
        if dtype.itemsize > 1 and (
            not isinstance(spelling, str) or spelling[:1] not in {"<", ">"}
        ):
            raise ValueError("Legacy dtype must have explicit endianness")
    count, length = int(m["count"]), int(m["reclen"])
    if count <= 0 or length < -1:
        raise ValueError("Invalid legacy count or record length")
    extensions = ["idx"] + (["len"] if length == -1 else [])
    if m["recsize"]:
        extensions.append("rec")
    if set(files) != set(extensions):
        raise ValueError("Unexpected or missing legacy file")
    md5 = hashlib.md5()
    for ext in extensions:
        md5.update(files[ext])
    if not m["checksum"] or md5.hexdigest() != m["checksum"]:
        raise ValueError("Legacy checksum mismatch or absent checksum")
    if len(files["idx"]) != count * td.itemsize:
        raise ValueError("Legacy timestamp byte length mismatch")
    times = np.frombuffer(files["idx"], dtype=td)
    if (
        not np.all(np.isfinite(times))
        or np.any(times[1:] <= times[:-1])
        or times[0] != m["idxa"]
        or times[-1] != m["idxb"]
    ):
        raise ValueError("Invalid bounds, nonfinite or non-increasing timestamps")
    raw = files.get("rec", b"")
    if len(raw) != m["recsize"] or len(raw) % vd.itemsize:
        raise ValueError("Legacy record byte length mismatch")
    values = np.frombuffer(raw, dtype=vd)
    if length == -1:
        if len(files["len"]) != count * 8:
            raise ValueError("Legacy length-vector byte length mismatch")
        lengths = np.frombuffer(files["len"], dtype="<i8")
        if np.any(lengths < 0) or sum(map(int, lengths)) != len(values):
            raise ValueError("Invalid ragged record lengths")
        offsets = np.r_[np.uint64(0), np.cumsum(lengths, dtype="u8")]
        values = RaggedArray(values, offsets)
    elif not m["recsize"]:
        values = values.reshape(count, 0)
    elif length:
        values = values.reshape(count, length)
    elif len(values) != count:
        raise ValueError("Legacy scalar count mismatch")
    return Batch(times, values)


class RecordDigest:
    """SHA-256 over framed records, independent of page and batch boundaries.

    Little-endian numeric bytes preserve signed zero and NaN payload bits. Each
    record frames timestamp dtype, value dtype and shape, timestamp and payload.
    Dense and ragged containers with the same individual records compare equal.
    """

    def __init__(self):
        self.hash = hashlib.sha256(b"pagestore-migration-records-v1\0")
        self.count = 0

    def update(self, batch):
        times = np.asarray(batch.timestamps)
        td = times.dtype.newbyteorder("<")
        times = times.astype(td, copy=False)
        ragged = isinstance(batch.values, RaggedArray)
        if not ragged:
            self._dense(times, batch.values)
            return self
        lengths = np.diff(batch.values.offsets)
        if len(lengths) and np.all(lengths == lengths[0]):
            self._dense(
                times,
                batch.values.values.reshape(
                    (len(batch), int(lengths[0])) + batch.values.values.shape[1:]
                ),
            )
            return self
        schema_cache = {}
        for i in range(len(batch)):
            record = np.asarray(batch.values.record(i) if ragged else batch.values[i])
            vd = record.dtype.newbyteorder("<")
            key = (vd.str, record.shape)
            framing = schema_cache.get(key)
            if framing is None:
                schema = json.dumps(
                    [td.str, vd.str, list(record.shape)], separators=(",", ":")
                ).encode()
                framing = struct.pack("<Q", len(schema)) + schema
                schema_cache[key] = framing
            raw = record.astype(vd, copy=False).tobytes(order="C")
            self.hash.update(framing)
            self.hash.update(times[i : i + 1].tobytes())
            self.hash.update(struct.pack("<Q", len(raw)))
            self.hash.update(raw)
            self.count += 1
        return self

    def _dense(self, times, values):
        """Vectorize the exact per-record framing in bounded 4 MiB buffers."""
        values = np.asarray(values)
        vd = values.dtype.newbyteorder("<")
        schema = json.dumps(
            [times.dtype.str, vd.str, list(values.shape[1:])], separators=(",", ":")
        ).encode()
        framing = struct.pack("<Q", len(schema)) + schema
        width = int(np.prod(values.shape[1:], dtype=np.int64)) * vd.itemsize
        dtype = np.dtype(
            [
                ("frame", f"V{len(framing)}"),
                ("time", f"V{times.dtype.itemsize}"),
                ("length", "<u8"),
                ("value", f"V{width}"),
            ]
        )
        step = max(1, 4 * 1024**2 // dtype.itemsize)
        for start in range(0, len(times), step):
            stop = min(len(times), start + step)
            packed = np.empty(stop - start, dtype=dtype)
            packed["frame"] = np.void(framing)
            packed["time"] = np.ascontiguousarray(times[start:stop]).view(
                f"V{times.dtype.itemsize}"
            )
            packed["length"] = width
            if width:
                raw = np.ascontiguousarray(values[start:stop], dtype=vd)
                packed["value"] = raw.reshape(-1).view(f"V{width}")
            self.hash.update(packed.tobytes())
        self.count += len(times)

    def hexdigest(self):
        """Return the digest without consuming or resetting the stream."""
        return self.hash.hexdigest()


def digest_stream(batches):
    """Return (record count, logical SHA-256) for an iterable of Batch objects."""
    digest = RecordDigest()
    for batch in batches:
        digest.update(batch)
    return digest.count, digest.hexdigest()


def import_verified_batch(destination, name, batch):
    """Import one bounded, strictly ordered unit and verify a fresh read.

    The caller must persist source identities and an intent first. A unit is one
    data commit, followed by a recovery checkpoint. On resumption, an identical
    complete interval is accepted and checkpointed; any partial or conflicting
    interval is rejected. Existing locks/ambiguous results are never cleared.
    The input must already have passed source integrity and collision checks.
    Timestamp dtypes must already be the store's signed int64 or float64 numeric
    representation; other widths need an explicit conversion before this step.
    """
    time_dtype = np.asarray(batch.timestamps).dtype
    if time_dtype.kind not in "if" or time_dtype.itemsize != 8:
        raise ValueError("Migration requires explicit int64 or float64 timestamps")
    if not len(batch) or np.any(batch.timestamps[1:] <= batch.timestamps[:-1]):
        raise ValueError("Migration unit must be nonempty and strictly ordered")
    expected = digest_stream([batch])
    low, high = batch.timestamps[0], batch.timestamps[-1]
    with DB(destination, mode="a") as writer:
        try:
            with writer.iter_signal(name, low, high) as stream:
                current = digest_stream(stream)
        except SignalNotFoundError:
            current = (0, None)
        if current[0]:
            if current != expected:
                raise ValueError("Destination interval conflicts with migration unit")
            writer.checkpoint(name)
            action = "verified_existing"
        else:
            result = writer.ingest(
                name, [batch], commit_bytes=max(64 * 1024**2, batch.nbytes)
            )
            if result.inserted != len(batch) or result.replaced:
                raise RuntimeError("Unexpected migration commit counts")
            action = "imported"
        manifest, _ = writer._head(name)
    with DB(destination, mode="r") as reader:
        with reader.iter_signal(name, low, high) as stream:
            actual = digest_stream(stream)
    if actual != expected:
        raise ValueError("Fresh destination read differs from verified source")
    return dict(
        action=action,
        records=expected[0],
        logical_sha256=expected[1],
        commit_id=manifest["commit_id"],
        generation=manifest["generation"],
    )
