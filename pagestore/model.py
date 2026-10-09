"""Array containers, normalization, and public result types."""

from dataclasses import dataclass, field
import operator

import numpy as np

from .timestamps import normalize_timestamps

DEFAULT_MAX_PAGE_SIZE = 8 * 1024**2


def integer(value, name, minimum=0):
    if isinstance(value, (bool, np.bool_)):
        raise TypeError(f"{name} must be an integer, not bool")
    try:
        value = operator.index(value)
    except TypeError as exc:
        raise TypeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def value_dtype(dtype):
    dtype = np.dtype(dtype)
    supported = (
        dtype.kind in "biu"
        and dtype.itemsize <= 8
        or dtype.kind == "f"
        and dtype.itemsize in (2, 4, 8)
        or dtype.kind == "c"
        and dtype.itemsize in (8, 16)
        or dtype.kind in "SU"
    )
    if not supported or dtype.fields or dtype.subdtype or dtype.metadata:
        raise TypeError(f"Unsupported record dtype: {dtype}")
    return dtype.newbyteorder("<")


@dataclass
class RaggedArray:
    """Variable-length array records with a common dtype and trailing shape.

    ``values`` concatenates records along its first dimension; unsigned integer
    ``offsets`` has N+1 entries, starts at zero, and ends at len(values). Equal
    consecutive offsets represent empty records. Construction validates offsets
    and the supported value dtype; it may share input array memory.
    """

    values: np.ndarray
    offsets: np.ndarray

    def __post_init__(self):
        self.values = np.asarray(self.values)
        value_dtype(self.values.dtype)
        if self.values.ndim == 0:
            raise ValueError("Ragged values must have a leading dimension")
        offsets = np.asarray(self.offsets)
        if offsets.ndim != 1 or offsets.dtype.kind not in "iu" or len(offsets) == 0:
            raise ValueError("Offsets must be a nonempty integer vector")
        if (
            offsets[0] != 0
            or np.any(offsets[1:] < offsets[:-1])
            or int(offsets[-1]) != len(self.values)
        ):
            raise ValueError(
                "Offsets must start at zero, be nondecreasing, and end at len(values)"
            )
        self.offsets = offsets.astype("u8", copy=False)

    def __len__(self):
        return len(self.offsets) - 1

    def record(self, index):
        """Return a view of the record at a nonnegative integer index."""
        return self.values[int(self.offsets[index]) : int(self.offsets[index + 1])]

    def take(self, indices):
        """Select records by slice or integer indices, sharing contiguous payloads."""
        if isinstance(indices, slice):
            start, end, step = indices.indices(len(self))
            if step == 1:
                end = max(start, end)
                lo, hi = int(self.offsets[start]), int(self.offsets[end])
                return RaggedArray(
                    self.values[lo:hi], self.offsets[start : end + 1] - lo
                )
            indices = np.arange(start, end, step, dtype=np.intp)
        else:
            indices = np.asarray(indices)
        if len(indices) and np.all(indices[1:] == indices[:-1] + 1):
            start, end = int(indices[0]), int(indices[-1]) + 1
            lo, hi = int(self.offsets[start]), int(self.offsets[end])
            return RaggedArray(self.values[lo:hi], self.offsets[start : end + 1] - lo)
        records = [self.record(int(i)) for i in indices]
        if not records:
            return RaggedArray(self.values[:0], np.zeros(1, dtype="u8"))
        return RaggedArray.from_arrays(records)

    @classmethod
    def from_arrays(cls, records):
        """Concatenate array records with equal dtypes and trailing dimensions.

        Each record must have a leading variable-length dimension. Empty input
        yields an empty float64 RaggedArray. Scalar records are not ragged arrays.
        """
        records = [np.asarray(record) for record in records]
        if not records:
            return cls(np.empty(0), np.zeros(1, dtype="u8"))
        first = records[0]
        if first.ndim < 1:
            raise ValueError(
                "Ragged records must be arrays with at least one dimension"
            )
        dtype = value_dtype(first.dtype)
        if any(
            r.ndim < 1
            or r.shape[1:] != first.shape[1:]
            or value_dtype(r.dtype) != dtype
            for r in records
        ):
            raise ValueError(
                "Ragged records must have equal dtypes and trailing shapes"
            )
        offsets = np.zeros(len(records) + 1, dtype="u8")
        offsets[1:] = np.cumsum([len(r) for r in records], dtype="u8")
        return cls(np.concatenate(records), offsets)


@dataclass
class Batch:
    """One array of timestamps and matching dense or RaggedArray values.

    Dense values have shape (N, *record_shape). DB write methods validate and
    normalize timestamps and records; construction itself does not copy or sort
    arrays. Streamed batches retain their stored schema and may use mapped memory.
    """

    timestamps: np.ndarray
    values: np.ndarray | RaggedArray

    def __len__(self):
        return len(self.timestamps)

    @property
    def schema(self):
        """Return layout, explicit stored dtype, and per-record trailing shape."""
        ragged = isinstance(self.values, RaggedArray)
        values = self.values.values if ragged else self.values
        return {
            "layout": "ragged" if ragged else "dense",
            "dtype": value_dtype(values.dtype).str,
            "shape": list(values.shape[1:]),
        }

    @property
    def nbytes(self):
        """Return array payload bytes, including timestamps and ragged offsets."""
        if isinstance(self.values, RaggedArray):
            return (
                self.timestamps.nbytes
                + self.values.values.nbytes
                + self.values.offsets.nbytes
            )
        return self.timestamps.nbytes + self.values.nbytes

    def take(self, indices):
        """Select matching timestamps and records with a slice or integer indices."""
        values = (
            self.values.take(indices)
            if isinstance(self.values, RaggedArray)
            else self.values[indices]
        )
        return Batch(self.timestamps[indices], values)


@dataclass(frozen=True)
class WriteResult:
    """Acknowledged signal commit identity and upsert record counts.

    ``inserted`` counts new timestamps, ``replaced`` counts overwritten incoming
    timestamps, and ``total`` is the signal's record count after this commit.
    Configuration/checkpoint commits have zero inserted and replaced counts.
    """

    generation: int
    inserted: int
    replaced: int
    total: int
    commit_id: str


@dataclass
class IngestResult:
    """Acknowledged ingestion progress, including a normalized input cursor.

    Counts accumulate over data commits; ``total`` is the most recent signal
    count. ``generation`` and ``commit_id`` include the final checkpoint, while
    ``commits`` counts only data groups. ``batch_index`` and ``record_offset`` are
    zero-based positions after sorting/deduplication; offset is the number already
    consumed within that batch. Empty ingestion leaves the defaults unchanged.
    """

    commits: int = 0
    inserted: int = 0
    replaced: int = 0
    total: int = 0
    generation: int | None = None
    commit_id: str | None = None
    batch_index: int = 0
    record_offset: int = 0


@dataclass(frozen=True)
class StoreInfo:
    """Store size in bytes and counts of live signals and timestamped records.

    ``size_bytes`` includes metadata, recovery files, retired pages, and temporary
    files. ``size_basis="allocated"`` uses filesystem block allocation, including
    directories, with hard links counted once and symlinks not followed. When
    allocation is unavailable (including XRootD/weak mounts), ``"logical"`` uses
    file lengths; server replication and filesystem overhead are then unknown.

    A vector or ragged record counts once per timestamp, regardless of its
    number of array elements. Deleted signals and uncommitted records do not
    contribute to the counts. Totals are not a transaction across signals.
    """

    size_bytes: int
    signal_count: int
    record_count: int
    size_basis: str


@dataclass(frozen=True)
class SignalInfo:
    """Committed signal metadata and statistics for its active measurement pages.

    ``first``/``last`` use the signal's timestamp kind. ``payload_bytes`` counts
    arrays and offsets; ``stored_bytes`` counts complete active data-page files,
    excluding metadata, retired pages, and filesystem allocation overhead.
    ``schemas`` contains per-layout/dtype/shape aggregates and value statistics.
    """

    name: str
    time_kind: str
    generation: int
    max_page_size: int
    count: int
    first: object
    last: object
    page_count: int
    payload_bytes: int
    stored_bytes: int
    schemas: list[dict]


@dataclass
class CheckReport:
    """Integrity scan results: successfully checked page count and error messages."""

    pages: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self):
        """Whether the requested scan found no errors; not a durability guarantee."""
        return not self.errors


@dataclass
class RecoveryReport:
    """Committed-state reconstruction results for an offline destination.

    ``recovered_signals`` lists restored live signals; ``deleted_signals`` lists
    preserved empty deletion checkpoints. Errors make reconstruction incomplete.
    Unconfirmed page IDs had no surviving commit record; ignored pages could not
    be used, while repaired_pages lists restored envelopes.
    """

    destination: str
    recovered_signals: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    repaired_pages: list[str] = field(default_factory=list)
    unconfirmed_pages: list[str] = field(default_factory=list)
    ignored_pages: int = 0
    deleted_signals: list[str] = field(default_factory=list)

    @property
    def complete(self):
        """Whether all discovered committed states were reconstructed without errors."""
        return not self.errors


@dataclass
class SalvageReport:
    """Results for supplied pages; original commit status/completeness is unknown."""

    destination: str
    source_database_id: str | None = None
    commit_status: str = "unknown"
    salvaged_signals: list[str] = field(default_factory=list)
    salvaged_pages: int = 0
    salvaged_records: int = 0
    copied_pages: int = 0
    linked_pages: int = 0
    duplicate_pages: int = 0
    ignored_recovery_pages: int = 0
    rejected_pages: dict[str, str] = field(default_factory=dict)
    conflicts: dict[str, list[str]] = field(default_factory=dict)
    repaired_pages: list[str] = field(default_factory=list)
    regenerated_digests: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self):
        """All supplied data candidates were usable; not proof of historical completeness."""
        return not (self.errors or self.conflicts or self.rejected_pages)


def schema_key(batch):
    s = batch.schema
    return s["layout"], s["dtype"], tuple(s["shape"])


def concatenate(batches):
    if len(batches) == 1:
        return batches[0]
    timestamps = np.concatenate([b.timestamps for b in batches])
    if isinstance(batches[0].values, RaggedArray):
        sizes = np.concatenate([np.diff(b.values.offsets) for b in batches])
        offsets = np.zeros(len(timestamps) + 1, dtype="u8")
        offsets[1:] = sizes.cumsum(dtype="u8")
        values = RaggedArray(
            np.concatenate([b.values.values for b in batches]), offsets
        )
    else:
        values = np.concatenate([b.values for b in batches])
    return Batch(timestamps, values)


def page_batches(batches, maximum):
    """Coalesce ordered input with at most roughly one page of copy buffers.

    Reserve conservative envelope space; the writer checks exact final sizes.
    Small test/configured limits use the exact-size splitter directly.
    """
    if maximum < 16384:
        yield from merge_batches(batches)
        return
    target = maximum - 4096
    pieces, size = [], 0
    for batch in batches:
        if pieces and schema_key(pieces[-1]) != schema_key(batch):
            yield concatenate(pieces)
            pieces, size = [], 0
        start = 0
        average = max(1, batch.nbytes / len(batch))
        while start < len(batch):
            if pieces and target - size < average:
                yield concatenate(pieces)
                pieces, size = [], 0
            count = min(len(batch) - start, max(1, int((target - size) / average)))
            piece = batch.take(slice(start, start + count))
            pieces.append(piece)
            size += piece.nbytes
            start += count
            if size >= target:
                yield concatenate(pieces)
                pieces, size = [], 0
    if pieces:
        yield concatenate(pieces)


def merge_batches(batches):
    """Stable last-record-wins merge, with a vectorized homogeneous fast path."""
    batches = [b for b in batches if len(b)]
    if not batches:
        return []
    times = (
        batches[0].timestamps
        if len(batches) == 1
        else np.concatenate([b.timestamps for b in batches])
    )
    sorted_unique = len(times) < 2 or np.all(times[1:] > times[:-1])
    if sorted_unique:
        out = []
        group = []
        for b in batches:
            if group and schema_key(group[-1]) != schema_key(b):
                out.append(concatenate(group))
                group = []
            group.append(b)
        out.append(concatenate(group))
        return out
    order = np.argsort(times, kind="stable")
    ordered = times[order]
    order = order[np.r_[ordered[1:] != ordered[:-1], True]]
    keys = [schema_key(b) for b in batches]
    if all(k == keys[0] for k in keys):
        return [concatenate(batches).take(order)]
    ends = np.cumsum([len(b) for b in batches])
    starts = np.r_[0, ends[:-1]]
    sources = np.searchsorted(ends, order, side="right")
    out, pieces = [], []
    start = 0
    while start < len(order):
        source = int(sources[start])
        end = start + 1
        while end < len(order) and sources[end] == source:
            end += 1
        piece = batches[source].take(order[start:end] - starts[source])
        if pieces and schema_key(pieces[-1]) != schema_key(piece):
            out.append(concatenate(pieces))
            pieces = []
        pieces.append(piece)
        start = end
    if pieces:
        out.append(concatenate(pieces))
    return out


def normalize(data, kind=None, timezone="utc"):
    if isinstance(data, Batch):
        inputs = [data]
    elif isinstance(data, tuple) and len(data) == 2 and not isinstance(data[0], Batch):
        inputs = [Batch(*data)]
    else:
        inputs = list(data)
        if not all(isinstance(b, Batch) for b in inputs):
            raise TypeError(
                "Expected (timestamps, values), Batch, or a sequence of Batch objects"
            )
    batches = []
    for batch in inputs:
        times, inferred = normalize_timestamps(batch.timestamps, kind, timezone)
        values = (
            batch.values
            if isinstance(batch.values, RaggedArray)
            else np.asarray(batch.values)
        )
        if not isinstance(values, RaggedArray) and values.ndim == 0:
            raise ValueError("Values require a leading record dimension")
        if len(times) != len(values):
            raise ValueError("Timestamp and record counts differ")
        if not len(times):
            continue
        kind = inferred
        if isinstance(values, np.ndarray) and values.dtype.kind == "O":
            if values.ndim != 1:
                raise ValueError(
                    "Mixed record containers must be one-dimensional object arrays"
                )
            pieces = []
            for i, record in enumerate(values):
                if not isinstance(record, (np.generic, np.ndarray)):
                    raise TypeError("Mixed records must be NumPy scalars or arrays")
                record = np.asarray(record)
                value_dtype(record.dtype)
                piece = Batch(times[i : i + 1], record.reshape((1,) + record.shape))
                if pieces and schema_key(pieces[-1]) != schema_key(piece):
                    batches.append(concatenate(pieces))
                    pieces = []
                pieces.append(piece)
            if pieces:
                batches.append(concatenate(pieces))
        else:
            b = Batch(times, values)
            b.schema  # validate the physical record type before any publication
            batches.append(b)
    return merge_batches(batches), kind


def statistics(batch):
    values = (
        batch.values.values if isinstance(batch.values, RaggedArray) else batch.values
    )
    result = {"min": None, "max": None, "nan_count": 0}
    if values.dtype.kind in "biuf" and values.size:
        valid = values
        if values.dtype.kind == "f":
            mask = np.isnan(values)
            result["nan_count"] = int(mask.sum())
            if result["nan_count"]:
                valid = values[~mask]
        if valid.size:
            for name, value in (("min", valid.min()), ("max", valid.max())):
                value = value.item()
                result[name] = (
                    str(value)
                    if isinstance(value, float) and not np.isfinite(value)
                    else value
                )
    return result
