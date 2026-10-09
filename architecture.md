# PageStore architecture

This document specifies the target of the package refactor. It is not a
description of the current implementation. Existing code and tests are useful
examples, but neither the public API nor the storage format must remain compatible.

## Goal and decisions

PageStore is a lightweight store for named, timestamped measurement signals,
optimized for bulk writes and reads. Values are stored in binary pages so that
reading homogeneous data mostly consists of mapping or copying array buffers.
Each signal has its own page-size limit, defaulting to **8 MiB**. This targets
less than one second to first data over a suitable network connection; actual
latency also depends on throughput, round trips, storage and metadata access.
Large signals can use larger pages to avoid millions of small files. Small queries
should touch few pages; large queries should avoid excessive file/request counts.

The primary workload is bulk loading fresh NXCALS data with several workers,
normally assigning **different signals to different workers**. Sustained ingest
should approach the available source/network/disk throughput. Overlapping data,
competing writers for one signal, and corrective atomic updates are exceptional.
Optimize the common path for sequential page writes and bounded metadata work;
do not make every fresh batch pay for general merge/rebalancing machinery.

The following decisions guide the implementation:

- Support numeric timestamps and explicit datetimes. A signal keeps one timestamp
  representation, while its value dtype and record shape may change over time.
- Keep `(timestamps, values)` as the ordinary read result. Use a one-dimensional
  object array of NumPy scalars/arrays when the selected records are heterogeneous
  or jagged. Provide a typed batch iterator for bulk processing.
- Store both data and indexes as files or objects. **Do not use SQLite**, including
  as a required local index, lock service, or cache.
- A plain path always selects filesystem access. Infer the filesystem profile
  and coordination mechanism from the system; URL schemes select remote
  transports. Explicit profiles are optional overrides, not required knowledge.
  Support local filesystems, mounted network filesystems, EOS/XRootD, and object
  stores through backend adapters with explicit capabilities.
- Define byte order in the format, independently of the writer's machine.
  Every page is self-describing and carries its own integrity hashes.
- A complete set of finalized page files, including redundant recovery pages,
  must reconstruct acknowledged database state without `store.json`, HEADs,
  manifests, or the original directory layout.
- Give each bulk worker independent per-signal ownership; there is no global
  writer lock or global catalog update for each batch of an existing signal.
  New signal creation is rare and may pay for a shared discovery-catalog update.
  Serialize publication to each signal independently. Readers must see a complete
  committed version while another client writes the same signal.
- Publish immutable pages and manifests through one small, atomically updated
  pointer per signal. A write involving several signals commits each independently.

The initial core depends on NumPy and the Python standard library. Remote clients
are optional extras. Compression, arbitrary Python objects, multi-signal
transactions, and a public history API are outside the initial implementation.
NXCALS extraction remains outside the storage core: workers supply NumPy batches
or iterators of batches. No NXCALS ordering guarantee is assumed without validation.

## Logical data model

### Names and timestamps

A signal name is a nonempty, case-sensitive Unicode string. Names are not paths;
slashes and punctuation have no storage meaning. Use the exact UTF-8 name when
computing its storage identifier, without implicit Unicode normalization.

A signal has one immutable `time_kind`, inferred on its first nonempty write:

| Kind | Public array | Stored representation | Meaning |
| --- | --- | --- | --- |
| Integer | `int64` | Little-endian signed 64-bit integers | Measurement ticks; units belong to the caller |
| Floating point | `float64` | Little-endian IEEE binary64 | Numeric measurement axis; no inferred epoch or unit |
| Datetime | `datetime64[ns]` | Little-endian signed 64-bit nanoseconds | UTC-based NumPy datetime convention |

Smaller integer/float inputs are normalized to these types. Unsigned integers must
fit in `int64`. Later writes and query bounds must be exactly representable in the
existing kind; reject rounding, overflow, numeric/datetime mixing, `NaN`, infinity,
and `NaT`. Datetime unit conversion must be checked for overflow and precision
loss. Accept NumPy datetime inputs, Python datetime objects, and ISO date/time
strings. Numeric values are never implicitly epoch seconds.

`DB(..., timezone="utc")` sets the parsing default. Read/write methods accept an
optional `timezone=` override; `None` inherits the database instance setting.
`"utc"` is UTC, `"cern"` is `Europe/Zurich`, and `"local"` resolves the operating
system timezone including historical daylight-saving rules. IANA names are also
accepted. This follows [NXCALS's UTC convention](https://nxcals-docs.web.cern.ch/current/user-guide/extraction-api/)
for naive timestamps. The option interprets naive strings/datetimes only; explicit
UTC offsets and aware datetimes identify their own instant. NumPy datetime arrays
already represent UTC instants. Returned datetime arrays are always UTC.

Support ISO dates and `YYYY-MM-DD[T or space]HH:MM[:SS[.fffffffff]]`, optionally
followed by `Z` or `±HH:MM`. Preserve all nine fractional digits. Reject nonexistent
or ambiguous naive local wall times at DST transitions; callers supply an explicit
offset or an aware datetime with a selected fold. Reject out-of-range values and
precision finer than nanoseconds. For NXCALS integer nanoseconds, explicitly use
`np.asarray(timestamps, dtype="int64").view("datetime64[ns]")`; integer arrays
otherwise remain caller-defined numeric axes.

Timestamps are strictly increasing and unique in committed data. Normalize a write
by stable sorting and keeping the last input record for duplicate timestamps.
Across multiple input batches, their order defines input order. On collision with
stored data, the incoming record wins, including its dtype and shape. Floating
timestamps use exact binary64 equality, with `-0.0` normalized to `0.0`.

### Record schemas and batches

The schema of a page is `(layout, value_dtype, record_shape)`. Timestamp kind is
part of the signal descriptor. A dtype or shape change starts a new schema run;
adjacent pages may have the same schema because of page-size limits.

- A dense batch has timestamps of shape `(n,)` and values of shape
  `(n, *record_shape)`. Scalar records have `record_shape=()`.
- A ragged batch has timestamps `(n,)`, offsets `(n + 1,)`, and values
  `(m, *record_shape)`. Record `i` is `values[offsets[i]:offsets[i + 1]]`.
  Only the first record dimension varies; offsets are `uint64`, start at zero,
  are nondecreasing, and end at `m`. Empty records are valid.
- A page contains a single schema. Changes of rank, trailing shape, dtype, or
  dense/ragged layout produce separate runs. Nonadjacent equal schemas are not
  combined across intervening records of another schema.

Initially support bool, signed/unsigned integers up to 64 bits, float16/32/64,
complex64/128, and fixed-width byte/Unicode strings as values. Preserve their
widths; do not promote different stored dtypes during reads. Structured dtypes,
arbitrary objects, and platform-dependent extended numeric types are rejected.
Value NaNs and infinities are allowed. Object dtype is only an input/output
container, never an on-disk value encoding.

`Batch(timestamps, values)` represents homogeneous input/output.
`RaggedArray(values, offsets)` represents ragged values; provide
`RaggedArray.from_arrays(records)` as a convenience constructor. Its input records
must have equal dtype and trailing shape. Do not guess whether a regular matrix
was intended to be ragged.

For mixed tuple input, a one-dimensional object array is interpreted record by
record. Each item must be a supported NumPy scalar or ndarray, and contributes its
own dense schema. This makes mixed read results writable again without losing
record dtype/shape. Use explicit ragged input to avoid a schema run for every
change in vector length. Ordinary Python lists use normal NumPy type inference.

### Byte order and portable semantics

The initial format uses **canonical little-endian storage**, on every platform.
Every array section declares both `byte_order` and an explicit storage dtype:
`<i8` for integer/datetime timestamps, `<f8` for floating timestamps, `<u8` for
ragged offsets, and, for example, `<f4` or `<c16` for values. Byte-sized types and
fixed byte strings use `byte_order="not_applicable"` and `|b1`, `|i1`, `|u1`, or
`|S<n>`. Never persist native-order `=`, an unspecified order, or platform aliases
such as `int`, `long`, or `float`. The two declarations must agree. Unknown or
contradictory declarations are format errors, not an invitation to guess.

Signed integers use two's complement; real values use the specified IEEE binary
width. Complex values contain real then imaginary components of the corresponding
IEEE type, each little-endian. Bools are one byte, with values zero or one.
`|S<n>` means exactly `n` uninterpreted bytes per element, without an implied text
encoding. `<U<n>` means `n` little-endian 32-bit Unicode code-point slots, with
NumPy fixed-width string padding, independent of the platform's `wchar_t`.
Datetimes explicitly declare nanoseconds since `1970-01-01T00:00:00`, using NumPy's
UTC-based convention without a leap-second representation. Numeric axes explicitly
declare caller-defined units rather than inheriting a datetime interpretation.

Headers distinguish the logical type (kind, width, shape, timestamp meaning) from
its physical byte encoding. Equal logical values supplied as `>f8` and `<f8`
belong to the same schema; a byte-order difference alone does not end a page.
The encoder converts both bytes and dtype interpretation to canonical order.
Changing dtype metadata alone would reinterpret values incorrectly. Hash the
final stored bytes, never an array's pre-conversion native representation. See
NumPy's [byte-order rules](https://numpy.org/doc/stable/user/byteswapping.html).

Readers construct views with the recorded storage dtype. Eager `get` results use
native-order owned arrays while preserving logical kind/width/shape. A mapped
batch may retain its explicit little-endian dtype on a big-endian host; converting
it to native order requires a copy. Neither path changes measurement semantics.

## Public API

The main public exports are `DB`, `Batch`, `RaggedArray`, result descriptors, and
exceptions. Pages, manifests, storage keys, and merge machinery remain internal.

```python
from pagestore import DB, Batch, RaggedArray

with DB("/data/pagestore", mode="a") as db:
    db.store({"temperature": ([1, 2, 3], [0.1, 0.2, 0.3])})
    db.store({"temperature": ([3, 4, 5], [0.35, 0.45, 0.55])})
    timestamps, values = db.get_signal("temperature", 2, 4)
    # timestamps: [2, 3, 4]; values: [0.2, 0.35, 0.45]
```

Target signatures (type names are descriptive):

```text
DB(url, *, mode="a", default_max_page_size=None, lock_timeout=30.0, timezone="utc")
db.close()
db.backend_info -> BackendInfo
db.search(regexp="") -> list[str]
db.rebuild_catalog() -> int  # metadata-only rebuild; number of visible signals
db.info(name) -> SignalInfo
db.configure_signal(name, *, max_page_size) -> SignalInfo

db.get_signal(name, t1=None, t2=None, *, max_count=None, skip=0, timezone=None)
    -> tuple[np.ndarray, np.ndarray]
db.get(selector=None, t1=None, t2=None, *, max_count=None, skip=0, timezone=None)
    -> dict[str, tuple[np.ndarray, np.ndarray]]

db.count_signal(name, t1=None, t2=None, *, max_count=None, skip=0, timezone=None) -> int
db.count(selector=None, t1=None, t2=None, *, max_count=None, skip=0, timezone=None)
    -> dict[str, int]

db.iter_signal(name, t1=None, t2=None, *, max_count=None, skip=0, timezone=None)
    -> context-managed iterator[Batch]
db.store(data, *, max_page_size=None, timezone=None) -> dict[str, WriteResult]
db.ingest(name, batches, *, max_page_size=None, on_overlap="error",
          commit_bytes=64 * 1024**2, timezone=None) -> IngestResult
```

`mode="r"` opens an existing database without modifying storage, `"a"` opens or
creates one, and `"x"` creates a new database and fails if it already exists.
The database creation default is `default_max_page_size=8 * 1024**2` bytes.
`None` selects that value for a new database or the stored default for an existing
one; an explicitly conflicting database default is an error. Each new signal
copies the default into its own persisted `max_page_size`, unless overridden on
its first write. It is not a database-wide limit on all signals.

`store(..., max_page_size=bytes)` sets the size policy for signals in that write;
alternatively pass `{name: bytes}` to override only named entries. Names in this
mapping must belong to the input data. `None` preserves existing signal settings
and uses the database default for new signals. Empty writes remain no-ops.
`configure_signal` changes an existing signal's setting without changing its
measurements; unknown names raise `SignalNotFoundError`. Both routes validate a
positive integer byte count and commit the setting in the signal manifest and
recovery snapshot, under the same coordination as a data write.

For example, `db.store({"large": batch}, max_page_size=64 * 1024**2)` creates or
updates that signal with a 64 MiB limit, while other signals keep their settings.
`db.configure_signal("large", max_page_size=256 * 1024**2)` can raise it later.
Instances are thread-confined; use independent instances in different threads
or processes.

`ingest` consumes an iterator of homogeneous batches for one signal with bounded
buffering. A worker normally calls it once for its assigned signal/time window.
It publishes independent groups of completed pages, defaulting to approximately
64 MiB of serialized data per commit, with at least one page per group. This is
independent of the per-page 8 MiB default; a configured larger page can exceed the
group target. A final partial group is committed at end of input. Never buffer
the entire extraction to provide one large transaction.

The default `on_overlap="error"` refuses an incoming group that intersects stored
timestamp ranges; it does not silently overwrite an existing measurement during
fresh ingestion. `"replace"` explicitly permits the ordinary upsert fallback.
Validate and normalize ordering within each input batch; already sorted unique
arrays take the zero-sort path. Out-of-order, disjoint time windows are allowed.
Earlier groups remain committed if a later group fails. `IngestResult` reports
aggregate counts and the last acknowledged generation/input cursor; `IngestError`
carries that progress on failure, including a zero-based input batch index and
record offset within that batch after stable sorting/deduplication when a batch
spans commits. Resume a partially consumed batch in its normalized order; the
cursor is not a row number in the unsorted source. `commits` counts data groups,
while `generation` includes the final checkpoint commit. Never retry already acknowledged groups implicitly.

### Selection rules

- Bounds are **inclusive**: `t1 <= t <= t2`. `None` means unbounded; equal bounds
  select an exact timestamp, and reversed bounds raise `ValueError`.
- `selector=None` selects all signals. A string uses Python `re.search`; an
  iterable of strings selects exact names. `get_signal` always takes an exact
  name. To fetch a literal name through `get`, use `[name]`.
- Search/regex results are sorted by name. Explicit lists preserve input order
  with duplicates removed. Invalid regexes raise `ValueError`.
- An unknown exact name raises `SignalNotFoundError`, a `KeyError` subclass.
  A regex with no matches returns an empty result.
- Filter the time interval, then take every `skip + 1` record, then apply
  `max_count`. These operations apply to the whole selected signal, across page
  and schema boundaries. `skip` is a nonnegative integer; `max_count` is `None`
  or a nonnegative integer, with zero returning no records. Reject bools here.
- `count_signal` equals the length returned by `get_signal` with the same arguments
  **and the same committed version**. `max_count` limits returned records, not the
  number inspected before skipping. Limits apply separately to each signal.

For example, selected timestamps `[1, 2, 3, 4, 5]` with `skip=1, max_count=2`
produce `[1, 3]`. Skipping is subsampling, not aggregation or time-based resampling.

### Read results and ownership

For one dense schema, `get_signal` returns a dense values array. For mixed schemas
or ragged data, it returns a one-dimensional object array whose items retain their
NumPy dtype and shape. Allocate that object array explicitly, then fill its
elements; `np.array(records, dtype=object)` may infer unwanted extra dimensions.

An empty selection returns an empty timestamp array of the signal's kind. Values
use the sole active dense schema if one exists; otherwise use an empty object
array. A one-record selection uses that record's selected schema, regardless of
other schemas outside the selection.

Ordinary `get` results own writable memory and survive `db.close()`. Mixed arrays
also own their individual record buffers. This predictable contract may require
a copy even when a query touches only one page.

`iter_signal` yields homogeneous batches in timestamp order and captures one
manifest when the stream is entered. It may split at page/schema boundaries.
Local arrays can be read-only views of mapped pages; remote arrays can be views of
downloaded buffers or local cache files. Array buffer ownership must keep those
resources alive even if a batch is retained after advancing or closing the stream.
Closing stops further iteration and releases resources not retained by arrays.
Do not forcibly unmap buffers still referenced by returned arrays. A ragged slice
may copy/rebase offsets while sharing its value buffer.

```python
with db.iter_signal("temperature", 2, 4) as batches:
    for batch in batches:
        process(batch.timestamps, batch.values)
```

### Write and metadata results

`store` accepts a mapping from exact signal names to `(timestamps, values)`, a
`Batch`, or a finite sequence of batches. Empty input is a no-op and does not
create a signal. Validate names, array lengths, supported schemas, and input
timestamps before publishing anything. Existing-signal compatibility is checked
against its current manifest under coordination.

Commit signals in mapping order, holding at most one signal lock at a time.
Return `WriteResult(generation, inserted, replaced, total, commit_id)` for each nonempty
signal; counts refer to distinct normalized timestamps. If a later signal fails,
raise `StoreError` containing the successful results, failed name, and original
exception. Already committed signals remain committed. A network timeout during
publication may have an unknown outcome; report the commit ID for reconciliation
instead of claiming rollback. See the commit protocol below.

A successful result also means that both recovery-page copies for that commit
have been durably written. If HEAD commits but this redundancy step fails, report
`RecoveryIncompleteError` with `committed=True`, signal and commit ID. Repair the
recovery copies idempotently; do not replay the upsert or report that it rolled back.

`SignalInfo` contains the name, time kind, generation, `max_page_size`, record count,
first/last timestamp, page count, payload bytes, stored bytes, and schema summaries.
Report value ranges **per schema**, not by coercing incompatible schemas into one range.
For bool/integer/real values, store scalar min/max over all record elements plus
NaN count; ignore NaNs for extrema and return `None` when no ordered value exists.
Infinities participate in extrema. Complex/string ranges are `None`. This keeps
metadata bounded even for large tensor records. Schema summaries also carry
record counts; they do not imply one continuous interval of that schema.
Page counts and data byte totals refer to active measurement pages; recovery-copy
storage is reported separately.

## File catalog and directory layout

There is no shared database file. Each signal has an immutable page-descriptor
index, addressed by a small commit manifest. Only its `HEAD.json` pointer changes.
Updating an existing signal never rewrites an index of all database signals.
Creating a new signal also maintains a compact, derived name catalog.

```text
store.json                         # format, database UUID, page/coordination settings
catalog/HEAD.json                  # checksummed shard references + creation intents
catalog/LOCK.lock                  # local profile; LOCK.LOCK for mkdir coordination
catalog/shards/ab/<uuid>.json       # sorted names, sharded by signal hash
pages/recovery/store-<config-uuid>.0.pg
pages/recovery/store-<config-uuid>.1.pg  # also preserve an empty store's configuration
signals/
  ab/<sha256-of-exact-name>/
    HEAD.json                      # name, kind, generation, manifest key + digest
    LOCK/                          # only for profiles using directory locks
    manifests/<commit-uuid>.json
    index/<node-uuid>.json         # immutable bounded page-descriptor index nodes
    pages/00-<uuid>.pg
    pages/01-<uuid>.pg
    pages/01/00-<uuid>.pg            # ordinal 100
    pages/01/01/00-<uuid>.pg         # ordinal 10100
    pages/recovery/<commit-uuid>.0.pg
    pages/recovery/<commit-uuid>.1.pg  # identical independent recovery copy
    tmp/<operation-uuid>/...        # preparation files use .pending, not .pg
```

The first two hex digits shard signal directories. Always check that the stored
name matches the requested name; a hash collision is an error. Do not expose user
names as path components. Paths inside manifests are relative keys confined to
the database root.

Within each signal, format the page ordinal in base-100 directory components,
using two decimal digits per component; append a UUID to the last component.
Pages are initially flat and grow into deeper paths without moving old pages.
The manifest stores the exact keys and next ordinal. Concurrent or aborted writes
may reuse an ordinal, but their UUIDs make their complete page keys distinct.
Never overwrite a page or manifest key.

`HEAD.json` contains `format_version`, `database_id`, `signal_id`, `name`,
`time_kind`, `generation`, `commit_id`, `manifest_key`, and `manifest_sha256`.
The backend returns an opaque revision token alongside it. The token may be an
object-store ETag; do not treat an ETag as a content checksum.

Each manifest repeats the signal identity, kind, generation and commit ID, records
its parent commit ID and next ordinal, and includes signal settings, aggregate
counts/bytes, a page-index root key/hash and this commit's added/removed page IDs.
Added pages carry full descriptors. It does not repeat every unchanged descriptor.
Each descriptor includes:

- Page ID/key, page-format version, embedded page SHA-256, payload bytes, and
  file bytes. Page IDs allow recovery to rebuild keys after files have been moved.
- Count, first/last timestamp, and schema `(layout, dtype, record_shape)`.
- Per-page value statistics used by `info`.

Encode integer/datetime bounds as decimal strings in JSON so generic JSON readers
cannot lose 64-bit precision. Encode float bounds using `float.hex()` and decode
with `float.fromhex()`. Statistics use a tagged dtype/value encoding, including
explicit infinity tags; do not emit nonstandard JSON NaN/Infinity tokens.
Use deterministic UTF-8 JSON serialization and hash the exact stored bytes.

The authoritative active page set is exactly the set reachable from the page-index
root in the manifest directly referenced by HEAD. Parent manifests record commit
ancestry, not additional active data.
Files found by listing are not automatically live.
Normal reads never scan page directories or rebuild an index.

The catalog is redundant metadata, not the only record of identity or commit
membership. Each data page repeats its full signal name, timestamp semantics,
schema and page identity. Two recovery pages repeat each confirmed commit's
incremental recovery record or full checkpoint, plus root configuration.
Reconstruction replays these committed records, not directory names, file
modification times or upload order of otherwise unreferenced data pages.

`search` lazily loads a compact, persistent name catalog into memory. Up to 256
immutable JSON shards hold sorted names, partitioned by the first byte of the
signal-name hash. A checksummed `catalog/HEAD.json` contains their keys and SHA-256
digests, the database identity, and pending creation names. Each search rereads
this small HEAD; unchanged shards remain cached. Changed shards are loaded and
the merged name list sorted. Regex searches scan that list in memory. An empty
regex returns a copy of the sorted names. Opening a DB and exact-name measurement
reads do not load this catalog or enumerate the namespace.

First creation takes the signal lock, then the catalog lock. Before publishing
the signal HEAD, durably add a creation intent to the catalog HEAD. Publish the
signal commit and its paired recovery pages, then add the name to its shard and
remove the intent in one catalog HEAD replacement. Readers resolve remaining
intents against signal HEADs: committed creations remain visible after a crash;
uncommitted creations are excluded. Catalog finalization failures after a durable
signal commit raise `CatalogIncompleteError(committed=True)`. Existing-signal
writes never acquire the catalog lock or invalidate the name cache.

After durable catalog publication, retire replaced name shards. A reader whose
captured shard has been removed retries against the newer catalog HEAD. If HEAD
is unchanged, a missing shard is corruption. Persistent failure to read a stable
catalog raises an explicit retry error rather than returning incomplete results.
Cleanup failure may leave harmless unreferenced name shards.

`rebuild_catalog()` scans signal HEADs/manifests, without reading measurement
pages, and atomically replaces the derived catalog. It holds only the catalog
lock; existing-signal writers can continue. It removes pending intents and old
name shards. Corrupt caches require this explicit repair; direct-name data reads
remain independent. Integrity checking enumerates authoritative signal HEADs and
compares the name catalog, so a missing catalog entry cannot hide data from it.
Page-only recovery rebuilds the name catalog from the reconstructed signal HEADs.

Stores written by 0.0.0 without this catalog remain readable. Their first writable
search or new-signal creation builds it once; read-only searches retain the old
HEAD-scan fallback until a writable client builds it. All writers must use the
catalog-aware implementation once the catalog exists. After an older writer adds
signals, stop that writer and explicitly rebuild before relying on name search.
The measurement/page format is unchanged; the catalog is never recovery authority.

Use an immutable ordered tree of bounded descriptor blocks. Leaf nodes contain
page descriptors sorted by first timestamp; internal nodes contain child keys,
hashes, timestamp bounds, counts and byte totals. Target at most 256 KiB of encoded
metadata per node; split overflowing nodes and grow the root as needed. Large
individual descriptors use a referenced object rather than making nodes unbounded.
Only changed leaves and ancestor paths are copied for a commit. Batch updates so
several new pages sharing a path rewrite it once. Unchanged subtrees are reused.

Append lookup visits the right edge; insertion into a disjoint historical gap
visits the relevant path. For `p` existing pages and `k` new pages, catalog work
must scale with new descriptor blocks plus changed tree paths, not a complete
scan/rewrite of all `p` descriptors. Ordinary reads traverse timestamp bounds and
only fetch relevant blocks. Cache immutable nodes across operations.

Recovery records are deltas between full checkpoints. A delta contains its parent
commit ID/recovery digest, complete descriptors for added pages, retired page IDs,
current signal settings and resulting totals. Append-only deltas have no retired
pages. The first signal commit provides a full checkpoint; subsequent fresh-data
commits write only deltas. Produce a full recovery checkpoint at bulk-session
completion or through maintenance, not once per incoming chunk. Its O(p) metadata
cost is explicit and measured separately from steady-state ingest. No read must
replay this recovery history: the current page-index root answers live queries.
Skip the end-of-session checkpoint when the current record is already a complete
checkpoint. A failed session can leave a valid checkpoint-plus-delta chain; it
does not need to rewrite the full index to preserve acknowledged progress.

## Binary page format

Use one uncompressed `.pg` file per page, suitable for both filesystem mapping and
object-store range reads. Avoid pickle and avoid one remote request per record.
The same envelope supports `page_kind="data"` and `page_kind="recovery"`. Recovery
pages contain catalog checkpoints or deltas rather than measurement records and
count as part of the page set required for database reconstruction.

The initial format consists of:

1. A 16-byte prefix: magic `PGSTORE\0` (8 bytes), unsigned little-endian major
   version (2), flags (2), and JSON-header length (4).
2. A UTF-8 JSON header, limited to 64 KiB, its 32-byte SHA-256, then zero padding
   to a 64-byte boundary.
3. Contiguous, 64-byte-aligned array sections: timestamps, optional ragged offsets,
   and values. Each header entry gives its role, absolute byte offset, byte length,
   explicit byte order/storage dtype, shape, and section SHA-256. A recovery page
   instead has a `recovery_json` section encoded as UTF-8 bytes (`|u1`); its snapshot
   is not subject to the 64 KiB header limit.
4. A byte-for-byte duplicate JSON header and its own 32-byte SHA-256 near the end
   of the file, followed by a fixed 128-byte trailer.

The trailer has fixed offsets, with every integer explicitly little-endian:

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 8 | Magic `PGEND\0\0\0` |
| 8 | 2 | Major format version |
| 10 | 2 | Flags |
| 12 | 4 | JSON-header length |
| 16 | 8 | Absolute offset of the backup header |
| 24 | 8 | Complete file length, including this trailer |
| 32 | 32 | Reserved, zero-filled |
| 64 | 32 | SHA-256 of the JSON-header bytes |
| 96 | 32 | Page SHA-256 |

Define `page_sha256 = SHA256(file_bytes[0:file_length - 32])`. Only the stored
page-digest field itself is excluded; the prefix, both header copies and their
hashes, all payload bytes, padding, and the rest of the trailer are covered.
This avoids a self-referential hash. Manifests and recovery descriptors repeat
this same digest. Each page can therefore be verified without a catalog. It is
not the SHA-256 of the file including its own final digest, and an object-store
ETag is not a substitute.

Both header copies record the full database ID, exact signal name and signal ID,
page ID/ordinal, page kind, upload-batch ID, format versions, and
logical root settings needed to interpret the page. They also repeat total file
length, backup-header offset and prefix flags so a damaged envelope can be rebuilt
from an intact header. Data headers additionally
include time kind/unit/epoch, record count, schema, bounds and value statistics.
Array sections declare C order and the canonical encodings specified above.
No dtype, shape, timestamp meaning or signal identity is inferred from another
file or from the filename. A data page may contain zero value elements, but
never zero records; a recovery page has zero measurement records by definition.

Every recovery record includes root configuration, signal identity and settings
(including `max_page_size`), its confirmed commit ID/generation, parent commit ID
and record kind. A `checkpoint` contains the complete active data-page roster,
including unchanged reused pages. A `delta` contains the parent recovery digest,
added page descriptors, retired page IDs, and resulting counts/bytes. Replaying
these deltas from a verified checkpoint reconstructs the exact current roster.
Neither external index nodes nor ordinary manifest files are required for replay.

The two copies of a recovery record are identical `.pg` bytes stored under
separate keys. Publish them only through the confirmed-commit protocol below.
Data pages carry stable upload identities, not an authoritative commit generation:
publication/recovery records establish membership. This also permits reusing an
already uploaded page after a metadata-only rebase without rewriting its payload.

Recovery pages declare `scope="signal"` or `scope="database"`. Database-scope
pages contain versioned root configuration rather than a signal roster; their
signal fields are explicitly null. Database creation also writes two such pages,
so an empty store's ID and nondefault settings are recoverable. Configuration
changes produce a new paired snapshot before being acknowledged. No recovery
snapshot contains authentication secrets or depends on an originating mount path.

The encoder works on a bounded page of resident input buffers. Compute section
hashes/statistics on canonical bytes, plan final offsets and file length, and
finish both header copies before starting output. Stream the completed header,
payload sections, backup header and trailer through a running page hash; append
the resulting digest last. Unused padding is zero. This writes payload once and
does not require reading the newly stored page back merely to hash a patched
header. Metadata and section hashing may make additional memory passes, which
must be measured, but must not force another network/disk pass over fresh data.

Use direct streaming uploads when supported. Local staging is a fallback for
transport constraints or oversized records, not a mandatory detour for every
page. Preparation files/objects remain outside the finalized page namespace and
use `.pending` names. Bounded pipeline buffers preserve ownership until the
backend has finished writing; callers must not mutate borrowed input meanwhile.

The decoder validates a header's hash before trusting its contents, then checks
magic/version, bounded lengths, explicit byte order, shape products, section
alignment/bounds/non-overlap, file length, and manifest agreement when available.
The trailer locates the backup header even if the prefix or primary header is
damaged; the primary header and its adjacent hash remain usable if the trailer is
damaged. Recovery verifies either surviving copy independently. If two valid
copies disagree, or neither validates, report ambiguity/corruption rather than
guessing. Damage to one copy still makes the original full-page hash fail; a
recovered interpretation must not be presented as an intact original page.

For metadata-only repair, reconstruct the damaged envelope bytes from the verified
copy in a new file, verify every payload section, and compare the reconstructed
page hash with an intact stored digest or recovery-snapshot descriptor. Record the
repair explicitly. If no trustworthy complete-page digest survives, export only
as a reported salvage result; do not mark the original page fully verified.

`check(full=True)` and recovery verify the page hash, section hashes, timestamp
order/uniqueness, and ragged offsets. Normal partial reads verify metadata hashes
and any sections fetched in full; they do not claim to have checked unfetched or
partially fetched payload bytes. Hash checking detects corruption; repairing
missing or damaged payload requires another verified copy or a backup. Duplicate
headers and recovery pages repair metadata damage, not arbitrary lost measurements.

Map local buffers read-only using Python `mmap` and NumPy views. Direct remote
access uses range reads or a bounded local page cache; mmap requires a local file.
NumPy's [memory-mapping documentation](https://numpy.org/doc/stable/reference/generated/numpy.memmap.html)
and Python's [mmap documentation](https://docs.python.org/3/library/mmap.html)
describe the underlying facilities. The container described here is a new format,
not an NPY file. Compression would require a new codec and a different read path.

## Backend selection and automatic filesystem profiles

Parse the location once in `DB`. A string path or `os.PathLike`, whether relative
or absolute, always selects filesystem transport; `file://` is its explicit URL
equivalent. The filesystem may be local, NFS, EOS/FUSE or another mounted system.
Users normally write `DB("/data/db")`, `DB("/mnt/share/db")` or
`DB("/eos/.../db")` without a profile parameter. Remote URL schemes select their
own transport and default profile. An optional `?profile=...` remains available
for explicit overrides and diagnostics.

Resolve the filesystem profile as follows:

1. Resolve symlinks and inspect the closest existing ancestor when creating a
   database. Detect the actual mount containing it, not a name such as `/eos`.
2. On Linux, read `/proc/self/mountinfo`, account for bind mounts and the process's
   mount namespace, and select the longest matching mount-point boundary. Use
   filesystem type, subtype, mount source and relevant options to select a
   registered profile. NFS/NFS4 selects NFS coordination; recognized EOS/FUSE
   mounts select the EOS profile. Use equivalent OS mount/statfs facilities on
   other platforms. The [mountinfo specification](https://man7.org/linux/man-pages/man5/proc_pid_mountinfo.5.html)
   defines the Linux fields.
3. Known local filesystems use the local profile. An unrecognized filesystem or
   unavailable mount inspection uses a generic filesystem profile with exclusive
   directory locks and atomic-replace requirements, rather than assuming it is
   local or requiring the user to choose a profile.
4. Read existing coordination settings and resolve a compatible implementation
   automatically. A pinned explicit override takes precedence over detection but
   must still satisfy that contract; changing a URL must not silently change the
   lock namespace of an existing store.

Record the detected type, selected profile, coordination family and capability
decisions in a diagnostic descriptor exposed as `db.backend_info`. Re-detect on
opening a new instance rather than persisting a machine's mount path as the
permanent profile. Metadata-only/read-only opening must not create probe files.
For writable opening, use known platform/backend guarantees and bounded capability
checks where appropriate; a single-client probe does not prove distributed safety.
If essential primitives are unavailable, report the missing capability rather
than asking users routinely to supply a profile. The rest of the package must
not branch on URL strings or call filesystem functions directly.

| Location / inferred profile | Data transport | Writer coordination | HEAD publication |
| --- | --- | --- | --- |
| `/data/db` on a detected local filesystem | Local file operations | Per-signal process lock, e.g. `flock`, plus an in-process mutex | Same-directory temporary file and atomic replace |
| `/mnt/share/db` on detected NFS | Mounted network filesystem | Atomic exclusive `mkdir` of the signal lock directory | Atomic replace under the lock, with profile-specific cache refresh |
| `/eos/.../db` on detected EOS/FUSE | EOS mount adapter | Verified EOS namespace exclusive-create operation | Verified namespace replace/move operation |
| Any filesystem path without a recognized specialized profile | Generic filesystem operations | Exclusive directory lock | Atomic replace under the lock, subject to required capabilities |
| `root://host//eos/.../db` | XRootD client | EOS/XRootD deployment's exclusive namespace operation | Its atomic publication operation, after upload completion |
| `s3://bucket/prefix` | Object client | Conditional HEAD update; conflicting writers retry | `If-None-Match: *` for creation; `If-Match` for replacement |

Profiles are implementation contracts, not claims that every server exposing a
protocol has those guarantees. Detection chooses the implementation; qualification
establishes its guarantees. Do not infer local locking merely from a plain path.
Unknown schemes/profiles fail clearly. Other object stores register equivalent
adapters only when their required operations are available.

Persist the coordination family and namespace in `store.json`. Different URLs
reaching the same database must use the same logical lock or conditional-update
authority. For example, EOS mount and XRootD clients must contend on the same
signal lock key. A local `flock` writer and a directory-lock writer cannot safely
coexist; reject incompatible writable profiles. Changing coordination requires
an offline configuration change. Keep local advisory-lock files permanently so
all processes continue to lock the same inode.

For mounted storage and XRootD, verify exclusive creation, atomic HEAD replacement,
fresh HEAD reads while holding the writer guard, and visibility of completed page
uploads on the actual supported deployment. EOS exposes configurable cache,
locking, flush, and rename behavior, so a POSIX-looking path alone is insufficient
evidence of these guarantees; see the [EOS client documentation](https://eos-docs.web.cern.ch/diopside/manual/using.html).
This backend qualification is an implementation prerequisite, not a reason to
restore SQLite. If primitives are insufficient, writable access needs a configured
coordinator providing the same contract; otherwise that profile is read-only.

The EOS adapter should evaluate the documented atomic-upload facility
(`eos.atomic=1`) as a publication primitive; it defers visibility of the target
until upload completion. Validate replacement of an existing HEAD and failure
behavior specifically, rather than inferring them from first-upload behavior.
See [EOS atomic uploads](https://eos-docs.web.cern.ch/clicommands/cp.html).
Never emulate atomic replacement by deleting HEAD and then uploading or renaming
its replacement: readers would lose the previously committed pointer.

S3-style compare-and-swap serializes **commits** to a signal without holding a lock
while uploading pages. Two writers can prepare concurrently; only the writer whose
expected HEAD revision still matches may commit. On conflict, reread the signal
and redo the merge before retrying, with a bounded retry policy. AWS documents the
required [conditional write operations](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html).
Do not substitute unconditional overwrite or assume an S3-compatible service has
identical capabilities.

For directory locks, write an owner token, host, process ID and start information
inside the acquired directory. An existing directory remains locked even if its
owner metadata is incomplete. Release only one's own lock. Wait with bounded
backoff up to `lock_timeout`; return `LockTimeoutError` on expiry. Never steal a
lock based only on age or a remote PID check: a paused writer could resume and
overwrite another writer's commit. Stale-lock recovery requires proving the old
writer cannot resume and its in-flight publication requests cannot execute later.
Automatic expiring leases require fencing support and are not part of the initial
directory-lock implementation.

The backend interface has these responsibilities:

```text
read_head(signal_key) -> HeadSnapshot | None  # decoded head + opaque revision
list_heads() -> iterator[HeadSnapshot]
read_range(key, offset, length) -> bytes
open_local(key) -> optional local-buffer owner
put_immutable(key, source) -> ObjectInfo      # complete, readable object on success
writer_guard(signal_key, timeout) -> context manager
publish_head(signal_key, expected_revision, head, guard) -> revision
delete_unreferenced(keys)                    # offline maintenance only
```

`writer_guard` can be a no-op for compare-and-swap backends. `publish_head` must
still enforce the expected revision. Filesystem implementations do the comparison
under their exclusive lock; object backends delegate it to the server.
Initialization of `store.json` also requires exclusive creation and complete-file
publication. Readers must never interpret a partially initialized root as empty.
Creation races must converge on the same valid configuration or fail cleanly.

Capabilities include range reads, local mapping, strong HEAD reads, exclusive
creation, atomic replacement, conditional replacement, listing consistency, and
durability level. Atomic visibility and persistence after a power/server failure
are separate guarantees. Local publication flushes and fsyncs files, then syncs
affected directories after rename; remote adapters use their documented durable
completion operation. Python's [OS documentation](https://docs.python.org/3/library/os.html#os.replace)
describes local replace/fsync primitives. A backend must state weaker durability
explicitly and must not acknowledge an upload merely queued in a client buffer.

## Write algorithm and commit protocol

### Parallel bulk ingestion

The expected deployment runs one owning worker per signal, with multiple signals
in flight. Each worker uses an independent `DB` instance; there is no shared
mutable Python dataset or globally updated signal index. On lock-based backends,
an `ingest` session may retain its signal guard across commit groups and keep the
current manifest/index nodes in memory. Other signals and all readers continue
independently. Object-store sessions retain their expected HEAD revision and use
conditional publication. Competing workers for the same signal remain supported
through the conflict path, but are not the primary throughput benchmark.

For each fresh input group:

1. Validate shape/schema and timestamp order with array operations. Sorted unique
   input needs no sort or gather. Reuse canonical contiguous buffers; convert
   byte order or layout only when needed. Never construct Python objects per
   scalar record or concatenate a growing signal array.
2. Check the incoming time span against the page-descriptor index. New signals,
   appends after the last timestamp, and inserts into genuinely empty time gaps
   go directly to page encoding. No existing value page is read. If ranges
   intersect, `ingest` either rejects that group or uses its explicit replacement
   policy; ordinary `store` uses the general upsert path.
3. Split only incoming data into configured pages. Do not reopen a committed tail
   page to fill it, compact historical pages, or rebalance data while ingesting.
   A small final page is preferable to rereading and rewriting stored payload.
4. Pipeline extraction of the next batch, array validation/encoding/hashing, and
   storage writes with bounded queues and backpressure. The first implementation
   should use caller-level parallel workers plus a small bounded I/O pipeline,
   not create an additional process pool per signal.
5. Publish completed page groups through the protocol below. One HEAD update,
   small manifest and paired delta records cover several data pages. Reuse the
   worker's index cache; update only affected descriptor blocks and ancestors.
   Group directory-sync and metadata operations where the backend permits, while
   preserving the stated durable-before-acknowledgment ordering.

`commit_bytes` bounds ordinary in-flight commit groups; actual memory also includes
input batches, codec buffers, a bounded index cache and any indivisible oversized
record. It is not an unbounded prefetch target. Callers control worker count to
match source, network and disk capacity. NXCALS fetching is measured separately
from PageStore encoding and destination writes.

### General commit protocol

Use the same publication rules for the fast planner and the exceptional merge
planner, on every backend:

1. Normalize incoming batches without modifying caller arrays. Check sortedness
   first; stable-sort/remove duplicate timestamps only when required.
2. Acquire the signal writer guard, then read its current HEAD and manifest.
   On first creation, use an absent revision and infer the timestamp kind. Resolve
   this signal's `max_page_size` from its persisted policy or this write's override;
   only a new signal without an override inherits the database default.
   Before extending a recovery chain, ensure the parent is reconstructible from
   its checkpoint and paired delta records. Repair a missing predecessor record
   from its confirmed manifest/index while those are intact; if this cannot be
   established, stop rather than acknowledge a dependent but unrecoverable delta.
   An owning session caches this verified ancestry boundary across its commits.
3. Locate pages intersecting the incoming time span. Read/merge affected pages in
   order; reuse untouched descriptors. Incoming records win equal timestamps.
   Untouched records inside the incoming span remain: this is an upsert, not a
   replacement of the complete time interval.
4. Produce consecutive schema runs and split them into bounded pages. Reuse
   untouched data pages, including small boundary pages. Allocate fresh page keys
   for rewritten content; combining unrelated underfilled pages belongs to
   explicit compaction.
5. Finish and publish every new immutable page. Compute its exact descriptors and
   update count/byte totals from changed subtree summaries. Per-schema value
   ranges can be reduced from descriptor metadata by `info`; computing them must
   not require a scan of every historical descriptor on each fresh-data commit.
6. Write changed index blocks and ancestor paths, then a small immutable manifest
   with a fresh commit ID, parent commit ID, generation `old + 1` (first is 1),
   new index-root reference and added/retired descriptors. Validate the affected
   ordering boundaries and subtree totals; do not scan every existing page.
7. Atomically publish HEAD against the revision read in step 2. This is the sole
   visibility/commit point. Filesystem writers still hold the guard; conditional
   writers treat a revision conflict as an uncommitted attempt. If the winning
   change is disjoint and schema/settings remain compatible, reuse already
   uploaded pages and rebase only index/commit metadata. Replan affected payload
   only when data overlaps or a policy/schema conflict requires it.
8. After confirming HEAD, publish both recovery-page copies: a full checkpoint
   for the first commit or an explicit checkpoint operation, and an incremental
   delta for ordinary later writes. Include logical root/signal settings.
   Upload from `.pending` staging to immutable finalized
   `.pg` keys. Never publish these pages before confirmation or for a losing
   conditional-write attempt: their existence is recovery evidence of a commit.
   A filesystem writer retains its guard through this step; a conditional writer
   may be overtaken, but its recovery record still describes its confirmed commit.
9. Return success only after both copies are durable, then release the guard.
   Retain old pages/manifests for existing readers. Cleanup is separate from commit.

The HEAD update remains the visibility point. A crash between steps 7 and 8 can
leave a visible but unacknowledged commit without complete recovery redundancy.
With an intact HEAD/manifest, finish its recovery copies idempotently. If all
external metadata is also lost in this window, page-only recovery may recover
only the last state with a complete, verified recovery chain. Successfully acknowledged
writes always have two recovery copies. This boundary is explicit; writing
`committed=true` into prepared data pages would not solve it.

No mutable page is shared between generations. The active invariant is
`page[i].last_timestamp < page[i + 1].first_timestamp`. New pages can share files
with previous generations only by reusing unchanged immutable descriptors.

Normalizing unsorted input requires O(incoming records) memory; this API does not
promise external sorting. For sorted input, merge using array searches and slices,
with working memory bounded by input plus a small number of page buffers. Avoid
Python iteration over scalar measurements and repeated `np.concatenate` of a
growing result. Merge timestamps first and gather value spans with their original
schema; never concatenate incompatible dtypes as an intermediate step.

After a publication timeout, reconcile using the unique commit ID. A fresh HEAD
may point directly to that commit or to a descendant whose parent chain contains
it; both mean the write committed. Retain committed ancestor manifests until an
offline lineage checkpoint so this is checkable. When the backend cannot establish
an authoritative outcome, raise `CommitUnknownError` carrying the signal and
commit ID. Blindly repeating an old upsert after a later writer commits could
overwrite newer values and is not a safe automatic retry.

For directory-lock backends, an ambiguous publication is also an ambiguous lock
release: keep the lock until the request has authoritatively completed, been
cancelled, or been fenced off. A fresh read still showing the old HEAD is not
proof that a delayed request cannot commit later. Conditional publication avoids
this particular race because the server rechecks the expected revision when
applying the write. Local advisory-lock profiles must use synchronous publication
whose operation cannot survive the writer and execute after its lock is released.

### Page sizing

`max_page_size` is a **per-signal maximum serialized data-page size**, defaulting
to exactly 8 MiB (8,388,608 bytes). It includes timestamps, ragged offsets, values,
both headers, hashes, padding and the trailer, so the ordinary complete page
transfer fits the stated limit. Fill pages up to that limit without splitting a
record. Tails and short schema runs may be smaller; there is no minimum size or
additional database-wide ceiling. A single record whose encoded page exceeds the
limit gets its own explicitly marked oversized page. Recovery-page snapshots are
metadata and have separate sizing/format limits.

Dense payload sizing uses
`n * (8 + value_dtype.itemsize * product(record_shape))`. Ragged payload sizing
uses `8 * n + 8 * (n + 1) + values.nbytes`. The codec adds its exact or conservative
envelope/alignment size when planning a split, then checks the final encoded file
length. Close each page before adding a record would exceed the signal's limit,
except for an indivisible oversized record. Split repeatedly until every output
page obeys the size rule, including a first bulk insert and multiple oversized
records. Recompute header/hash metadata after every split.

Record the creation size policy in each page header. Changing a signal's limit
affects newly created or rewritten pages; it does not invalidate or immediately
rewrite old pages. Explicit compaction/repacking applies the current policy to
existing data, combining only adjacent compatible schemas or splitting pages as
needed, through the normal commit protocol. It does not change values.

The smaller default limits the initial full-page transfer for network reads;
range reads can return less. Larger limits, for example 64 or 256 MiB, trade more
data per request for fewer pages, smaller page indexes and fewer remote requests.
Choose them per signal according to record size, total volume and query pattern.
Do not silently increase the persisted policy based on dataset growth. Benchmark
time to first returned batch as well as sustained throughput; the sub-second
target is not a guarantee independent of the network and backend.

## Read planning

Read HEAD once per signal and pin the referenced immutable manifest for the
operation. Traverse the page-index tree's ordered bounds for overlapping pages;
fetch only needed immutable index nodes. No full descriptor-list load or recovery
log replay is required before returning the first batch.
Within each boundary page, use `searchsorted(..., side="left")` for `t1` and
`searchsorted(..., side="right")` for `t2`. Do not synthesize infinite sentinels
for integer or datetime timestamps.

Carry the sampling phase and remaining `max_count` across pages. Read timestamp
sections before value sections, and stop when the limit is satisfied. For remote
dense data, request contiguous selected spans; for ragged data, first read the
needed offsets and then the corresponding values. Small sparse requests may be
coalesced to avoid excessive round trips. Initial range selection may fetch the
whole timestamp section; sparse on-disk timestamp indexes are a later optimization.

`count_signal` sums descriptor counts for fully covered pages and reads only
timestamp sections of partial pages. If there are `n` records in the selected
interval, the sampled count is `(n + skip) // (skip + 1)`, then capped by
`max_count`. It never reads record payloads. `info` uses catalog metadata only;
per-schema ranges may require reducing page descriptors, cached by index-root
digest. It never scans value payloads to rebuild statistics on the write path.
`get_signal` allocates its final arrays once, or collects bounded batches and
concatenates once; it shares selection logic with `iter_signal` and `count_signal`.

Different signals in a `get` call may have different commit times. There is no
database-wide snapshot, and `search` is not a transaction across names. For one
signal, a reader sees one complete version, even if HEAD advances during its read.
Backend caching may delay visibility only as explicitly documented by its profile;
writers require a fresh authoritative revision to prevent lost updates.

Cache immutable manifests/pages by database ID and key/digest, using atomic local
cache writes and a bounded size. Cached content never changes under the same key.
Eviction must not invalidate live array buffers. Corrupt cache entries may be
discarded and fetched again; corrupt authoritative content raises an error.

## Failure handling, checking, and reclamation

| Failure point | Visible state | Recovery |
| --- | --- | --- |
| While writing a temporary page | Old HEAD | Remove abandoned temporary data offline |
| After pages, before manifest | Old HEAD | New pages are unreferenced |
| After manifest, before HEAD | Old HEAD | New manifest and pages are uncommitted |
| Conditional HEAD conflict | Winning writer's version | Reload and replan; never publish the stale plan |
| HEAD replacement succeeds but response is lost | Possibly committed | Reconcile commit ID or report unknown outcome |
| After confirmed HEAD, before both recovery copies | New HEAD; write not acknowledged as complete | Finish recovery copies without repeating the write |
| HEAD/root/manifest corruption | Page data and recovery snapshots may remain intact | Reconstruct metadata from verified page snapshots |
| One header or recovery copy is corrupt | Its independent copy may remain intact | Verify the surviving copy and rebuild into a separate destination |
| Writer dies holding a directory lock | Last committed HEAD | Establish that writer cannot resume, then remove stale lock |
| Referenced file is missing or invalid | Committed data unavailable | Bounded transient-read retry, then explicit integrity error |

Readers do not acquire writer locks. Immutable old pages make concurrent reads
safe only if reclamation does not remove them. **The initial version performs no
automatic physical deletion.** Keeping files for an arbitrary grace period is
not sufficient protection for a long-running reader.

Provide a read-only `check` maintenance operation that pins manifests and validates
identities, references, ordered non-overlapping bounds, counts, schema descriptors,
file lengths, embedded header hashes, and both recovery copies for acknowledged
snapshots. `check(full=True)` additionally verifies embedded full-page/section
hashes and full page contents. Report errors with signal, generation, page key,
and failed invariant. Orphans are distinct from
corrupt live data. Do not silently promote the newest-looking orphan manifest.

### Reconstruction from pages

Provide `maintenance.recover(source, destination)` as an offline operation that
creates a separate database and a machine-readable recovery report. The input
can be a store or a directory of finalized `.pg` files collected without their
original paths. No original root configuration, HEAD, manifest, SQL database or
filename convention is required to interpret the files. Recovery pages are part
of this set; raw data pages alone cannot prove whether an overlapping write won
its commit race.

1. Scan finalized pages, excluding preparation files. Validate both header copies,
   trailer and hashes; attempt the specified metadata-only repair when one copy
   is damaged. Group by embedded database ID, recovery scope and signal identity;
   do not combine different databases. Index each verified data page by page ID
   and digest, not its current path. Duplicate identical files are harmless;
   conflicting contents for the same identity are reported.
2. Read verified recovery records, merging their two physical copies. Select the
   highest confirmed generation per signal, find its latest applicable complete
   checkpoint, and follow/replay the exact parent/digest chain to that generation.
   A missing delta cannot be skipped. Apply retired IDs then added descriptors
   and settings, validating parent IDs/digests, generation order, non-overlap and
   resulting counts. Retiring an unknown page or introducing an incompatible
   duplicate ID is an integrity error. Two distinct confirmed commits claiming
   one generation are also an error, not a last-file-wins rule. A later complete
   checkpoint can supersede a damaged earlier chain without needing that history.
3. Resolve every required page ID/digest from the scanned files, fully verify its
   data, and recompute counts, timestamp bounds, schema summaries and value ranges.
   Compare against the reconstructed committed state. Rebuild keys from actual
   recovered locations;
   orphan data pages never enter the selected signal simply because they contain
   newer timestamps or were uploaded after another page.
4. Recover logical root settings from database-scope recovery pages and their
   copies in signal snapshots, checking compatible format/configuration versions.
   Reconstruct per-signal policies from each selected signal snapshot. Infer the
   destination filesystem profile anew and establish compatible coordination
   there; old mount paths and credentials
   are not semantic dataset metadata. Regenerate `store.json`, descriptor-index
   nodes, manifests and HEADs,
   then publish fresh checkpoint recovery pages before declaring success.
5. Report selected generations, verification results, metadata repaired from a
   duplicate, unused/orphan pages, missing/corrupt data and unresolved conflicts.
   Preserve the source files. Never silently roll back a known newer snapshot or
   fill a gap with a superseded value from an older page.

If the newest state requires irreparable pages or missing recovery deltas, return
an incomplete recovery report instead of a successful complete reconstruction.
An explicit salvage mode
may export verified records or restore a chosen older complete snapshot, labeling
its missing ranges, unknown commit status and rollback. If all recovery copies for
a generation are lost, data-page metadata can still identify and decode records,
but cannot in general reconstruct that generation's exact overwrite decisions.

Duplicate headers protect against localized metadata damage. Separate recovery
copies protect against loss/corruption of one snapshot file; keep them in distinct
failure domains when the backend offers that placement. Hashes detect payload
damage but contain no error-correcting data: recover missing measurements only
from a verified replica, retained identical page or backup. The baseline design
does not claim to survive loss of all copies of a payload or commit snapshot.

Garbage collection is an offline operation with all readers and writers stopped,
including remote clients, in-flight publication requests, and outstanding commit
reconciliation. First publish a checkpoint manifest for each signal: preserve its
active page descriptors, increment its generation, and start a new ancestry chain
with `parent_commit_id=null` and `lineage_checkpoint=true`. This maintenance
checkpoint uses the ordinary commit protocol, including both durable recovery
copies, and ends the need to retain older commit ancestry. It does not change
the logical measurements. If either recovery copy is missing, do not collect.

Trace the current manifests and any explicitly retained backup roots. Their data
pages, reachable index nodes and required manifests are live, together with both
recovery copies of each retained checkpoint and every delta needed after it.
Retain root-configuration recovery copies as well. Superseded recovery copies,
older unpinned manifests, unreferenced data pages, and abandoned preparation files
are collection candidates.
Apply the plan only within that quiescent maintenance window. Checkpointing avoids
retaining obsolete index branches and recovery history forever. Online collection would require a
distributed reader-pinning or lease protocol and is deferred. Compaction alone
does not reclaim disk space.

A backup records root configuration and chosen signal HEADs/manifests, then copies
their referenced immutable data pages/index nodes and both root recovery copies, plus
both copies of a complete signal checkpoint and all required subsequent deltas.
Concurrent writes are safe while collection is disabled, but the backup is a
collection of per-signal snapshots. Restore into a separate destination, preserve
page keys, verify hashes, and publish checkpoint manifests/HEADs and both recovery
copies so restored signals do not depend on omitted commit ancestry.

Use explicit exceptions: `SignalNotFoundError`, `SchemaError`, `ReadOnlyError`,
`UnsupportedBackendError`, `UnsupportedFormatError`, `LockTimeoutError`,
`WriteConflictError`, `CommitUnknownError`, `RecoveryIncompleteError`,
`IntegrityError`, `StoreError`, and `IngestError`.
Include context and preserve underlying I/O exceptions; do not catch an I/O error
and return an empty dataset. Unknown newer format versions fail before mutation.

## Package structure

```text
pagestore/
  __init__.py               # deliberately small public exports
  db.py                     # public API and operation orchestration
  model.py                  # Batch, RaggedArray, Schema, info/write results
  timestamps.py             # exact normalization, validation, bound encoding
  selection.py              # shared range/sampling plan
  merge.py                  # pure upsert and schema-run/page splitting
  ingest.py                 # per-signal streaming sessions and bounded write pipeline
  page_format.py            # binary encoder/decoder and buffer ownership
  catalog.py                # small HEAD/manifests, validation and signal discovery
  page_index.py             # immutable ordered descriptor blocks and path updates
  writer.py                 # preparation, publication, retry/reconciliation
  cache.py                  # bounded immutable local cache
  maintenance.py            # check, compaction, offline GC and backup helpers
  recovery.py               # checkpoint/delta replay, page scanning, catalog rebuilding
  errors.py
  backends/
    base.py                 # capability and storage/coordination contracts
    registry.py             # URL/path parsing and adapter selection
    detection.py            # system mount discovery and automatic profile selection
    local.py
    mounted.py              # qualified NFS/EOS mount profiles
    xrootd.py                # optional client dependency
    s3.py                   # optional client dependency
  migration/
    legacy.py               # explicit import of the old format
```

Data-model, selection and merge functions are pure and independent of transport.
The codec knows bytes and schemas, not locks or catalogs. The catalog knows
committed descriptors, not how to merge records. Only backend adapters own
filesystem/remote operations and coordination primitives. Avoid replacing the
old `DataSet` wrapper with another mandatory collection class when a dictionary
of tuples already serves the public API.

## Refactoring sequence and validation

The first end-to-end milestone is **parallel fresh-data ingestion on independent
signals, readback and page-only recovery**. Measure its throughput before spending
time optimizing the exceptional overlap path. Keep correctness tests for that
path, but do not use it as the default implementation of every write.

1. **Contracts and pure data operations.** Implement timestamp validation, schemas,
   dense/ragged batches, duplicate resolution, inclusive bounds and sampling.
   Use a simple in-memory timestamp-to-record map as the overwrite oracle.
2. **Page codec.** Round-trip supported dtypes/shapes, jagged and empty records,
   large integers and datetime bounds. Use explicit big- and little-endian inputs
   and byte fixtures to verify identical canonical storage, native eager results
   and correctly typed mapped views. Flip bytes in each header, section, trailer
   and digest; check detection and recovery from one surviving metadata copy.
   Verify array lifetime after closing a stream.
3. **Backend contracts and file catalog.** Implement the local backend, bounded
   immutable descriptor index, small manifests and checkpoint/delta recovery.
   Add fault injection around every publication step. Confirm that opening,
   searching and reading need no SQLite file or service.
4. **Bulk ingestion and DB reads/writes.** First implement `ingest` with one owner
   per signal, bounded input/I/O buffers and grouped commits. Run several worker
   processes on different signals and establish the throughput baseline below.
   Verify both fresh append and disjoint historical-window insertion without
   reading old payloads. Then integrate the exceptional overlap planner,
   metadata-only counts, tuple
   materialization and partial multi-signal failure reporting. Test schema changes
   in both append and overlap writes, keeping records absent from the incoming
   update. Split a first write into as many pages as needed. Test independent
   8 MiB/default and larger per-signal policies, exact encoded-size boundaries,
   oversized records and policy changes without implicit historical rewrites.
5. **Storage deployment support.** Run a common backend conformance suite against
   NFS, EOS mount/XRootD and an object store. Exercise two hosts writing the same
   signal, independent-signal writers, readers during replacement, stale caches,
   stale locks, conflicts, and a lost commit response. Mock tests cannot qualify
   a real storage filesystem's atomicity or durability guarantees. Test automatic
   mount/profile selection for plain paths, symlinks, bind mounts, missing target
   directories, unknown filesystem types and optional overrides.
6. **Recovery, maintenance and migration.** Rebuild a fresh database using only
   finalized data/recovery `.pg` files copied without their directory structure;
   remove root configuration, HEADs and manifests from the recovery fixture.
   Recover an empty store and its nondefault settings from root recovery pages.
   Include mixed schemas, byte orders, per-signal size policies, overlapping
   updates, abandoned/conflicting writes and a corrupt recovery copy. Verify
   values, names and settings against the last acknowledged snapshot. Inject
   failures before/after HEAD publication and between the two recovery copies,
   and distinguish a committed incomplete write from an uncommitted attempt.
   Replay long delta chains, including overwritten/retired pages and settings-only
   commits. Remove an intermediate delta, corrupt one copy, repair a predecessor
   before a dependent commit, and recover through a later complete checkpoint.
   Test explicit incomplete recovery when all payload or commit copies are lost,
   and ensure garbage collection retains a reconstructible current snapshot.
   Implement integrity reporting, compaction and an offline reclamation plan.
   Import legacy stores into a separate destination; never change their files
   in place. Read legacy SQLite metadata only in the
   migration tool, preferably from a consistent local copy. Legacy pickle loading
   is an explicit trusted-source operation, isolated from normal database reads.
7. **Performance and documentation.** Update examples and the README to the new
   API. Benchmark append and overlap writes, narrow and bulk reads, count/info,
   regex discovery, and mixed/jagged data. Measure bytes transferred, request
   count, peak memory, manifest/recovery snapshot size, rewrite amplification,
   and time to first batch, including cold network reads with the 8 MiB default.

### Bulk throughput acceptance

Use representative extracted NXCALS array shapes/dtypes and a synthetic source
with the same distribution so source-service time can be separated from storage
time. Benchmark one worker and increasing numbers of workers on different
signals, with both new signals and already large signals. Repeat for the 8 MiB
default and selected larger per-signal limits.

- Compare sustained data throughput with direct sequential writes/uploads to the
  same backend, using comparable file sizes, worker count and durability. The
  goal is to approach that measured limit, not an assumed link speed. Report
  end-to-end load time including final checkpointing as well as steady state.
- Instrument existing data-page bytes read and rewritten: both must be **zero**
  for fresh, disjoint ingestion. Data payload write amplification should be one
  stored copy, excluding configured backend replication and exceptional retries.
- Metadata per ordinary group consists of new descriptors, changed index paths,
  one small manifest/HEAD publication and two recovery deltas. Increasing the
  pre-existing signal size must not introduce a full-index scan/copy per group.
- Measure checksum/statistics CPU time, sorting/conversion copies, queue memory,
  network/disk utilization, request count, sync/commit latency and lock waiting.
  Workers updating existing signals must not wait on a shared catalog writer
  lock. Benchmark first-time creation separately: it deliberately pays for
  durable, promptly visible name discovery.
- If hashing, metadata round trips or a serialization loop limits throughput
  before network/disk does, optimize that measured stage without dropping
  integrity hashes or acknowledging writes before the required durability point.

Retain these counters as regression checks. A faster microbenchmark is not a
substitute for concurrent throughput on the actual storage filesystem.

Contract tests must cover empty/no-match results; inclusive endpoints; duplicate
timestamps within/across batches; overwrite precedence; dtype/shape changes;
sampling across several pages; NaN values; record sizes above the target; and
consistent counts. Concurrency tests must use separate processes, and backend
qualification must include separate clients/hosts where supported.

The old tests encode behaviors that should be reconsidered, including SQL LIKE
patterns, public page-ID allocation, record-only size accounting and page-ID reuse.
The legacy implementation also rewrites referenced files in place, uses pickle
for values/metadata, and has inconsistent merge and empty-selection paths. Use
those as regression scenarios, not as requirements to preserve implementation.

The first release with this design is a breaking API/storage-format release.
Normal `DB` opening must detect the legacy format and request explicit migration.
Remote schemes are documented as supported only after their adapters pass the
conformance suite; the architecture does not make all network filesystems safe
merely by removing SQLite.


## Implementation status: first filesystem milestone

The new public API is implemented in `pagestore.DB`; the original SQLite/pickle
classes are isolated under `from pagestore.legacy import PageStore, Page, Data,
DataSet`. The original README and examples are retained alongside that legacy API.
No new-format opening imports SQLite or pickle or migrates an existing store.

Implemented: numeric and timezone-aware datetime normalization, dense/mixed/ragged
records, per-signal page limits, the binary envelope and SHA-256 checks, durable
filesystem publication, an immutable copy-on-write page index, metadata queries,
streaming bulk ingestion, paired checkpoint/delta recovery, failure outcome
reporting, and reconstruction into a new directory from flattened pages alone.
`db.check(full=True)`, `db.checkpoint(name)`, and `db.repair_recovery(name)` expose
verification and recovery maintenance. `pagestore.maintenance.recover` returns a
structured report and writes `recovery-report.json` into a created destination.
Recovery reports unconfirmed data pages separately and never promotes them into
live data without a surviving commit record.

The initial index uses at most 128 descriptors per leaf and 64 children per
internal node, with a 256 KiB serialized node ceiling and a bounded node cache.
Append commits copy only affected paths; the final ingest checkpoint traverses
the roster once. `info` currently reduces page metadata on demand; it does not
read measurement payloads. Normal mmap reads validate envelope metadata; full
payload and section verification is explicitly available through `check(full=True)`.
Upsert reads of old pages always verify their full hashes before rewriting them.

Local filesystems use `flock`; NFS and generic mounted filesystems use exclusive
directory locks and atomic rename. Mount detection and a persisted coordination
family prevent mixing incompatible lock protocols. The filesystem implementation
is tested locally, including directory locks and independent processes. NFS
qualification on the target servers remains outstanding. EOS writable access,
native XRootD, and S3 are rejected until their deployment-specific publication
adapters are implemented and qualified; they are not silently treated as local.

Still pending: remote adapters and multi-host qualification, a pipelined remote
transfer path, offline reclamation/repacking, and a legacy migration command.
The local synthetic benchmark in `examples/benchmark.py` compares parallel
fresh-signal ingestion with direct writes using the same filesystem publication
and fsync routine. Actual NXCALS extraction and cold network time-to-first-data
must be measured on the deployment storage; 8 MiB is a size policy, not a latency
guarantee.
