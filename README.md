# PageStore

PageStore stores named time series in immutable, checksummed array pages. The new
API uses a filesystem catalog with per-signal coordination and no SQLite index.
The design and remaining milestones are documented in
[architecture.md](https://github.com/rdemaria/pagestore/blob/main/architecture.md).
The implemented directory layout, indexes, binary pages, and recovery records are
explained in [doc/store_layout.md](doc/store_layout.md).

Directory trees grow adaptively with 128 slots per level. The first 128 signals
or per-signal objects need no extra directory level; later allocations add levels
without moving existing files. A million signals need at most two extra levels.
Stable signal locations are cached in the name catalog and repeated in per-signal
identity records. During experimentation, only the current layout (store format 3)
is supported. Earlier layouts are rejected without conversion; binary pages remain
version 1. The original SQLite-based implementation stays under `pagestore.legacy`.

## Installation and development

Requires Python 3.10 or newer. Install from PyPI with:

```sh
python -m pip install pagestore
```

NumPy is installed automatically. From a checkout, install with:

```sh
python -m pip install .
```

For native XRootD access, install the optional client bindings with
`python -m pip install 'pagestore[xrootd]'` (or `'.[xrootd]'` from a checkout).

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

`DB(path_or_url)` opens an existing store read-only by default. A missing store
raises `FileNotFoundError`; reads never initialize or repair storage. To create or
modify a store, pass `mode="a"` explicitly (or `mode="x"` for exclusive creation).

```python
from pagestore import DB

with DB("./measurements", mode="a") as db:
    db.store({"temperature": ([1, 2, 3], [10.0, 11.0, 12.0])})
    db.store({"temperature": ([3, 4], [12.5, 13.0])})
    timestamps, values = db.get_signal("temperature", 2, 4)
    # timestamps: [2, 3, 4]; values: [11.0, 12.5, 13.0]
    print(db.info_signal("temperature"))
    totals = db.info()
    print(totals.size_bytes, totals.signal_count, totals.record_count)
```

Read an existing store without requesting write access:

```python
with DB("./measurements") as db:
    timestamps, values = db.get_signal("temperature")
```

Bounds are inclusive. Writes sort timestamps, keep the last incoming duplicate,
and replace existing records at equal timestamps. `skip=1` selects every second
record across the entire selected interval; `max_count` limits the final result.
`get("regexp")` searches names using `re.search`; `get(["exact/name"])` selects
exact names. Unknown exact names raise `SignalNotFoundError`.

Search loads a compact, checksummed name catalog on first use and keeps names in
memory. Every search checks for other workers' newly created signals; measurement
updates need no shared catalog lock. DB opening stays lazy; an initial exact-name
lookup loads just one catalog shard and its signal identity, then caches the location.
Signal creation, recreation, and full deletion pay the catalog maintenance cost;
updates and partial deletions of existing signals use only their signal lock.

Call `db.refresh()` to drop PageStore's cached metadata before reading again:

```python
db.refresh()
timestamps, values = db.get_signal("temperature")
```

The next operation reloads the needed metadata, including name and location
catalogs. `refresh()` returns `None`, performs no I/O, and works in read-only mode.
Normal reads already check signal HEADs, and searches already check catalog changes.
Existing iterators retain their original snapshots. Refresh cannot flush an
EOS/SSHFS mount cache or wait for another writer's commit; use the authoritative
XRootD URL for direct remote access.

`info_signal(name)` returns one signal's active-page metadata and statistics.
`info()` returns `StoreInfo(size_bytes, signal_count, record_count, size_basis)`.
Size includes the entire store: metadata, recovery copies, old pages, locks, and
temporary files. On filesystems exposing disk allocation, `size_basis="allocated"`
counts allocated blocks including directories, counts hard links once, and does
not follow symlinks. XRootD and weak mounts report `size_basis="logical"` using
file lengths, since server allocation and replica overhead are unavailable.
Counts cover live signals and records, with each timestamp counting once even
when its value is an array. `info()` scans directory metadata and signal manifests
without reading measurements; it can be slow for a large store. Concurrent writers
can change totals during the scan. It does not modify storage.

`repr(db)` shows its location, mode, backend profile, and closed state without
storage access. The former `info(name)` API is now `info_signal(name)`.

## Delete records

```python
with DB("./measurements", mode="a") as db:
    removed = db.delete_signal("temperature", t1=2, t2=4)  # inclusive
    removed = db.delete_signal("temperature")            # all remaining records
```

`delete_signal` uses an exact name and returns the number of records removed.
Either time bound can be omitted; string datetime bounds accept the same
`timezone=` option as reads. Missing signals and empty selections return `0`.
Deleting the last record removes the name from search; subsequent exact reads
raise `SignalNotFoundError`. A later `store` or `ingest` can recreate the name,
including with a new timestamp kind.

Deletion commits atomically per signal. Fully covered pages require no payload
read; boundary pages are verified and rewritten. A full deletion records an empty
recovery checkpoint, so committed-state recovery preserves it. Existing readers
keep their captured snapshots. Physical files remain until future garbage
collection, so this method does **not immediately free disk space**. Data-only
salvage cannot know a page was deleted and may recover its former records.

All public DB methods and exported result/container types have API docstrings,
available through Python `help(DB)` and `help(DB.delete_signal)`.

## Catalog maintenance

Rebuild a missing or damaged derived name catalog from signal metadata:

```python
with DB("./measurements", mode="a") as db:
    print(db.rebuild_catalog())  # signal count; reads HEADs/manifests, not data pages
```

The first writable search also builds a missing catalog automatically. Read-only
clients scan signal HEADs until it exists. Exact-name data reads remain independent
of this cache. Rebuilding a catalog does not convert unsupported store layouts.

Numeric timestamps remain numeric measurement axes. Integer timestamps are int64;
floating timestamps are float64. A signal's timestamp kind is fixed by its first
nonempty write, and subsequent conversions must be exact.

## Datetimes and timezones

```python
with DB("./measurements", mode="a", timezone="utc") as db:
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

## Concurrent writers

Each worker opens its own `DB` and uses ordinary methods. For example, a worker
loading extracted batches calls `store()` as usual:

```python
from pagestore import DB

with DB(path, mode="a") as db:
    for timestamps, values in extracted_batches:
        db.store({name: (timestamps, values)})
```

The store handles initialization and writer coordination automatically. Different
signals progress independently; writes to the same signal serialize, preserving
records outside each update. For equal timestamps, the last committed upsert wins.
No parallel mode, worker registration, or caller-managed lock is required. Assigning
different signals to workers improves throughput but is not required for correctness.
Keep a separate DB instance per process or thread.

## Streaming writes

`ingest` optionally consumes an iterator of `Batch` objects or `(timestamps, values)`
tuples with bounded buffering and grouped commits. It uses the same coordination
as `store()` and is useful for large streams regardless of the number of workers.

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
A `CatalogIncompleteError` cause means the data commit and recovery copies are
durable, but name-catalog finalization failed. Search still resolves its saved
creation intent; run `db.rebuild_catalog()` without replaying the data.

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

Read and fully verify a single measurement page without opening a database:

```python
from pagestore import read_page

name, timestamps, records = read_page("./surviving-page.pg")
```

This checks both headers, every array section, and the whole-page SHA-256, and
raises `CorruptionError` on corruption. It performs no writes or repairs. Returned
arrays own writable, native-endian memory, as with `get_signal`; ragged records
use an object array of NumPy arrays. Datetimes retain UTC `datetime64[ns]` semantics.
An intact recovery page raises `ValueError` because it contains no measurements.
Use `salvage` below to assess and import recoverable damaged envelopes.

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
Use `db.checkpoint(name)` to create a fresh recovery checkpoint. Obsolete measurement,
index, manifest, and recovery files are retained; their automatic reclamation is
not implemented. Replaced, derived name-catalog shards are reclaimed automatically.

For catastrophic loss where only measurement pages survive, use the separate
salvage operation:

```python
from pagestore.maintenance import salvage

report = salvage("./surviving-pages", "./salvaged")
print(report.salvaged_signals, report.rejected_pages, report.conflicts)
```

It verifies each data page using its own headers and hashes, preserves signal
names and timestamp semantics, and reuses intact page bytes in a new store. No
original catalog or recovery file is needed. Damaged metadata can be reconstructed
from a surviving header and verified arrays; pages with damaged payloads are
excluded and reported. A lost whole-page digest can be regenerated only after all
sections pass verification, with the result recorded explicitly.

Salvage cannot establish which pages were committed or whether pages are missing.
Overlapping versions exclude the affected signal until you select pages explicitly:
`salvage(source, another_new_destination, pages=["chosen-page.pg", ...])`.
Other valid signals can still be imported. `report.ok` describes the supplied
pages, not completeness of the original database.

The default copies intact files byte-for-byte. `reuse="hardlink"` avoids payload
copies on the same filesystem; shared source files must remain immutable. It
never falls back silently to copying if linking fails. Repaired pages always get
new files. Sources must be quiescent and destinations new. Reports and per-page
provenance are saved in the destination. See [the store layout](doc/store_layout.md)
for details and temporary-inventory sizing.

## Filesystems and current scope

A plain path selects filesystem storage. Mount detection selects local `flock`
or directory locks for NFS/generic mounts; `db.backend_info` exposes the decision.
`file:///path?profile=generic` is an optional explicit override. Lock coordination
is persisted so clients cannot silently use incompatible protocols. Directory
locks are never stolen based on age; after a crashed writer, establish that it is
dead and reconcile the committed state before removing its lock directory.

This first implementation is tested on a local filesystem, including multiple
processes and the directory-lock protocol. NFS still needs qualification on the
deployment servers. `s3://` is not implemented. Instances are thread-confined.
Read-only access (`mode="r"`) is the default. Use `mode="a"` to open or create a
writable store, or `mode="x"` to require a new database.

### XRootD and EOS

```python
url = "root://eosproject-a.cern.ch//eos/project/a/abpdata/cerndatadb"
with DB(url, mode="a") as db:
    db.store({"temperature": ([1, 2], [10., 11.])})
```

`root://` and `roots://` use the XRootD Python client directly, with its configured
authentication (including Kerberos). No subprocess or local staging file is used
per page. `/eos/` namespaces select the EOS profile automatically; an explicit
`?profile=eos` selects EOS for another namespace. An EOS namespace cannot be
overridden to the generic XRootD profile. EOS uploads use
[`eos.atomic=1`](https://eos-docs.web.cern.ch/clicommands/cp.html) on unique staging
names. Publication waits for server sync/close, size and checksum confirmation,
then a server-side rename. Mutable metadata is read back by content before success.
EOS uses exclusive namespace `mkdir` locks (`eos-mkdir`); generic XRootD uses
exclusive owner-file creation (`xrootd-exclusive`). This distinction matters: EOS
can accept concurrent `NEW` opens before a file closes, while some generic servers
treat `mkdir` as idempotent. The selected protocol is persisted; incompatible
writable clients are rejected. Both profiles require atomic overwrite by rename;
there is no delete-then-rename fallback.

An SSHFS/EOS mount may acknowledge close before EOS commits the file, and may
serve cached reads afterward. Supply the authoritative URL when opening a mounted
path for writing:

```python
with DB("/eos/project-a/abpdata/cerndatadb", mode="a", xrootd_url=url) as db:
    print(db.backend_info)
```

This routes **all reads, writes, and locks through XRootD**. The path is an alias;
the supplied URL identifies the store and must point at the intended directory.
For this CERN instance, `/eos/project-a/` on the mount maps to `/eos/project/a/`
in XRootD. Generic SSHFS mounts cannot supply this mapping automatically.
Writable access through an unconfigured EOS/SSHFS mount is rejected, including
attempts to override its profile to `generic` or `local`. Read-only mounted access
is allowed; pages are copied into verified byte snapshots instead of memory-mapped.
Such readers can see older committed snapshots because of mount caching.

`io_timeout=30` limits each XRootD request in seconds; `visibility_timeout=30`
controls bounded retries for incomplete or not-yet-visible referenced files.
Remote pages always undergo full hash verification. `lock_timeout=30` controls
writer contention. A timeout or ambiguous response to a mutation retains held
locks and disables further writes on that instance. An old HEAD does not prove
rollback: outstanding operations must be reconciled before locks are removed.

Synthetic integration tests on EOS cover parallel writers, upserts, catalog
rebuilds, checksummed reads, process contention, and mounted aliases. Distributed
outages and server durability still require deployment testing; a successful
single-host test is not a multi-host fault qualification. See the
[validation record](doc/xrootd_validation.md) for tested servers and retained fixtures. `recover` and `salvage`
remain offline tools taking filesystem directories, not native remote URLs.
Run the opt-in tests only against a disposable parent (new test stores are retained):

```sh
PAGESTORE_TEST_XROOTD_URL=root://host//path/to/tests \
    python -m pytest -q -s tests/test_xrootd_integration.py
```

Set `PAGESTORE_TEST_MOUNT_PARENT` to the matching mounted directory to also test
the alias and read-only mount paths.

Run `python -m pytest -q` for the test suite. Run
`PYTHONPATH=. python examples/benchmark.py --workers 4 --records 1048576` for a synthetic
fresh-signal/direct-write comparison on the chosen filesystem. It does not measure
NXCALS service latency or establish a network time-to-first-data guarantee.

To stress catalog size, create one million signals with 32 records each:

```sh
PYTHONPATH=. python examples/benchmark_many_signals.py --directory /tmp \
    --signals 1000000 --records 32 --workers 16 \
    --output benchmarks/results/million-signals.json
```

This measures database opening, name searches, and extraction of 12 signals at
increasing catalog sizes, with ordinary durable writes. It checks free bytes and
inodes before scaling, reports file and directory overhead, and removes the
temporary database afterward. Use `--keep` to retain it. See the
[benchmark notes](https://github.com/rdemaria/pagestore/blob/main/benchmarks/README.md)
for methodology and results.

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

For the bounded EOS migration pilot, `pagestore.legacy.migration` validates
uncompressed numeric PyTimber pages and verifies imported records without changing
source files. `examples/migrate_legacy_pilot.py` executes a frozen, reviewed manifest
and keeps immutable audit attempts in a sibling migration directory. It rejects
missing/corrupt sources, unordered or duplicate timestamps, and conflicting
destination intervals. It is a pilot runner, not a general legacy converter;
overlap resolution and source retirement remain separate steps.
See the [bounded EOS pilot results](doc/migration_pilot.md) for measured ingestion,
integrity, recovery, space usage, and the explicitly excluded legacy pages.
