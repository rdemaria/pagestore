# PageStore

PageStore stores named time series in immutable, checksummed array pages. The new
API uses a filesystem catalog with per-signal coordination and no SQLite index.
The design and remaining milestones are documented in
[architecture.md](https://github.com/rdemaria/pagestore/blob/main/architecture.md).

## Installation and development

Requires Python 3.10 or newer. Install from PyPI with:

```sh
python -m pip install pagestore
```

NumPy is installed automatically. From a checkout, install with:

```sh
python -m pip install .
```

For an editable development installation and the test suite:

```sh
python -m pip install -e ".[testing]"
python -m pytest -q
```

`sh mktest` also writes an HTML coverage report to `htmlcov/`. Build and validate
the source distribution and wheel with:

```sh
python -m pip install build twine
python -m build
python -m twine check dist/*
```

Packaging is configured in `pyproject.toml`; the version comes from
`pagestore/version.py`. Both distributions include the legacy namespace.

## Store and read

```python
from pagestore import DB

with DB("./measurements") as db:
    db.store({"temperature": ([1, 2, 3], [10.0, 11.0, 12.0])})
    db.store({"temperature": ([3, 4], [12.5, 13.0])})
    timestamps, values = db.get_signal("temperature", 2, 4)
    # timestamps: [2, 3, 4]; values: [11.0, 12.5, 13.0]
    print(db.info("temperature"))
```

Bounds are inclusive. Writes sort timestamps, keep the last incoming duplicate,
and replace existing records at equal timestamps. `skip=1` selects every second
record across the entire selected interval; `max_count` limits the final result.
`get("regexp")` searches names using `re.search`; `get(["exact/name"])` selects
exact names. Unknown exact names raise `SignalNotFoundError`.

Numeric timestamps remain numeric measurement axes. Integer timestamps are int64;
floating timestamps are float64. A signal's timestamp kind is fixed by its first
nonempty write, and subsequent conversions must be exact.

## Datetimes and timezones

```python
with DB("./measurements", timezone="utc") as db:
    db.store({"beam": (["2026-01-01 12:00:00.123456789"], [42.0])},
             timezone="cern")
    timestamps, values = db.get_signal(
        "beam", "2026-01-01T11:00:00Z", "2026-01-01T12:00:00Z")
```

`timezone="utc"` is the default, matching [NXCALS's timestamp convention](https://nxcals-docs.web.cern.ch/current/user-guide/extraction-api/).
`"cern"` means `Europe/Zurich`; `"local"` uses the machine's timezone including
DST rules. IANA names also work. Method-level `timezone=` overrides the instance
default for parsing naive ISO strings and Python datetimes. Explicit offsets and
aware datetimes retain their own meaning. Ambiguous or nonexistent naive times at
DST transitions are rejected; supply an offset or an aware datetime with `fold`.

Datetimes are stored and returned as UTC `numpy.datetime64[ns]`, with exact
nanosecond precision. NumPy datetime arrays are already UTC. For raw NXCALS int64
nanoseconds, convert explicitly without copying:

```python
timestamps = timestamps_ns.view("datetime64[ns]")
```

## Parallel bulk loads

Assign different signals to different workers; each worker opens its own `DB`.
`ingest` consumes an iterator of `Batch` objects or `(timestamps, values)` tuples.
It buffers approximately one commit group, rather than the entire extraction.

```python
from pagestore import Batch, DB

def load_signal(path, name, extracted_batches):
    with DB(path) as db:
        return db.ingest(name, extracted_batches)
```

Each signal defaults to **8 MiB per serialized page**, including metadata and
hashes. Set `max_page_size=64 * 1024**2` on `store` or `ingest` for larger records
or datasets, or call `db.configure_signal(name, max_page_size=...)` later. One
indivisible record larger than the limit gets its own oversized page. Changes to
the size policy affect new pages.

Fresh, disjoint writes read and rewrite no existing measurement pages. Ingestion
rejects overlaps by default; use `on_overlap="replace"` for corrections. Commits
default to roughly 64 MiB. A final checkpoint makes the page set independently
recoverable. Earlier groups remain committed if a later group fails:

```python
from pagestore import IngestError

try:
    result = db.ingest("signal", batches)
except IngestError as error:
    print(error.progress, error.cause)
```

Progress includes acknowledged counts, generation, commit ID, batch index, and
record offset in the **sorted, deduplicated batch**. A cursor inside an unsorted
source batch must be applied after the same normalization. `StoreError` similarly
reports successful signals if a later signal fails. There is no multi-signal
transaction. A `RecoveryIncompleteError` cause means HEAD is committed but the
recovery copies need completion; call `db.repair_recovery(name)` without replaying
the upsert. An unknown publication outcome carries its commit ID for reconciliation.

## Records and ownership

Dense values have shape `(records, *record_shape)`. Record dtype and shape may
change over time. Reads covering different schemas return a one-dimensional
object array, preserving each record's NumPy dtype and shape. Supported stored
types are bool, integers through 64 bits, float16/32/64, complex64/128, and
fixed-width byte/Unicode strings. Arbitrary Python objects and structured dtypes
are rejected.

Use `RaggedArray.from_arrays(records)` for arrays with a variable first record
dimension and equal dtype/trailing dimensions, then pass `Batch(times, ragged)`.
All stored multibyte arrays use explicit little-endian encoding. Eager results
own writable, native-endian memory. Streaming batches can retain read-only mapped
pages even after the stream or database is closed:

```python
with db.iter_signal("signal", skip=1) as stream:
    for batch in stream:
        process(batch.timestamps, batch.values)
```

## Integrity and recovery

Each page contains two header copies, section hashes, and an embedded whole-page
SHA-256. Paired recovery pages describe confirmed commits. Ordinary mapped reads
validate metadata; explicitly verify all payload bytes with `db.check(full=True)`.

```python
from pagestore.maintenance import recover

report = recover("./damaged-or-flattened-pages", "./recovered")
assert report.complete, report.errors
```

Recovery is offline and creates a separate directory. Finalized `.pg` files alone
are sufficient, including the recovery pages; their original directory structure
is unnecessary. Recovery can reconstruct damaged metadata from a surviving copy
when the reconstructed page matches its expected hash. Missing/corrupt measurement
payloads require another copy or backup. The report identifies incomplete recovery,
repaired pages, and unconfirmed orphan pages that were excluded from live data.
Use `db.checkpoint(name)` to create a fresh recovery checkpoint. Obsolete files
are currently retained; automatic reclamation is not implemented.

## Filesystems and current scope

A plain path selects filesystem storage. Mount detection selects local `flock`
or directory locks for NFS/generic mounts; `db.backend_info` exposes the decision.
`file:///path?profile=generic` is an optional explicit override. Lock coordination
is persisted so clients cannot silently use incompatible protocols. Directory
locks are never stolen based on age; after a crashed writer, establish that it is
dead and reconcile the committed state before removing its lock directory.

This first implementation is tested on a local filesystem, including multiple
processes and the directory-lock protocol. NFS still needs qualification on the
deployment servers. Writable EOS, native `root://`, and `s3://` adapters are not
implemented and fail explicitly. Instances are thread-confined. Use `mode="r"`
for read-only access or `mode="x"` to require a new database.

Run `python -m pytest -q` for the test suite. Run
`PYTHONPATH=. python examples/benchmark.py --workers 4 --records 1048576` for a synthetic
fresh-signal/direct-write comparison on the chosen filesystem. It does not measure
NXCALS service latency or establish a network time-to-first-data guarantee.

## Legacy API

```python
from pagestore.legacy import PageStore, Page, Data, DataSet
```

The old SQLite/pickle implementation and existing file format remain available
only through this namespace. Its
[original README](https://github.com/rdemaria/pagestore/blob/main/pagestore/legacy/README.md)
and [examples](https://github.com/rdemaria/pagestore/tree/main/examples/legacy/)
are retained. Open only trusted legacy files;
the new `DB` neither opens nor modifies that format. Copy legacy measurements
explicitly into a separate new database when needed.
