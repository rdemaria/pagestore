# Benchmarks

## Large signal catalogs

The retained million-signal results below predate store format 3 and its adaptive
layout/location catalog. They are historical measurements, not current-format
performance results. The retained store is unchanged and cannot be opened by the
current implementation; use a fresh store for current-format benchmarks.

`examples/benchmark_many_signals.py` exercises the public `DB.ingest` API using
one million distinct signals, each with 32 int64 timestamps and float64 scalar
values: 32 million records and 512 MB of logical payload (decimal units). Each
signal has unique values so the readback checks detect cross-signal mistakes.
Sixteen worker processes own separate signals. Checksums, file/directory fsync,
locking, and both recovery copies remain enabled.

```sh
PYTHONPATH=. python examples/benchmark_many_signals.py --directory /tmp \
    --signals 1000000 --records 32 --workers 16 \
    --keep --output benchmarks/results/million-signals.json
```

At 1,000, 10,000, 100,000, and 1,000,000 signals, writers pause while a fresh
reader process measures:

- Opening and closing `DB` in read-only and writable (`mode="a"`) modes, 100 times
  each. Read-only is now the default; these measurements selected modes explicitly.
- Exact-name reads, first over 100 random signals and then over the same signals
  again. Every timestamp, value, and dtype is verified outside the timed call.
- `db.get(names)` for 12 random signal names, 100 times, verifying all 384 records
  per extraction. Report p50/p95/p99/max latency.
- `db.search` with an anchored exact-name regex, a regex matching 12 signals, and
  an unrestricted search. Each regex runs once per milestone; the complete
  returned roster is checked against the generated signal names.

The first read pass follows ingestion with the OS cache retained; it is not a
cold-cache or network measurement. Search runs after the read tests, and later
searches can benefit from metadata cached by earlier ones. Worker peak RSS comes
from `getrusage`; reader peak RSS is isolated in a fresh process per milestone.
Write timings include dispatch, input generation, commits, and final checkpoint
handling; the first stage also includes worker startup. Query and final storage
scan times are excluded from ingestion throughput.

An initial signal's measured footprint is used to check remaining free bytes
and inodes with 30% headroom before growing the database. At the end, an exact
tree walk counts files, directories, logical file sizes, and allocated blocks by
file category. `st_blocks` includes file/directory allocation but excludes
filesystem-wide inode tables, journals, and other shared metadata. Free-space
snapshots are also reported, but changes can include unrelated filesystem use.

The report is saved after each milestone. Temporary data is removed at the end;
`--keep` retains the path printed at startup. A failure leaves `complete: false`
and a traceback in the report. Use smaller `--signals`, `--samples`, and
`--milestones` values for a quick smoke run.

This workload isolates **signal cardinality**, small-file overhead, and catalog
operations. It does not write 100 TB or establish sustained throughput, recovery
time, or cold-read latency for a 100 TB deployment.

### In-memory catalog comparison

`examples/benchmark_catalog_cache.py` compares two read-only cache prototypes
against a retained, quiescent store from the benchmark above. Run that benchmark
with `--keep`, then substitute its printed store path below. Each cache mode runs
in a fresh process:

```sh
PYTHONPATH=. python examples/benchmark_catalog_cache.py /tmp/STORE/store \
    --signals 1000000 --mode names --output /tmp/cache-names.json
PYTHONPATH=. python examples/benchmark_catalog_cache.py /tmp/STORE/store \
    --signals 1000000 --mode manifests --output /tmp/cache-manifests.json
```

The first mode retains sorted names from the authoritative HEAD scan used by the
original `DB.search`. The second also retains every decoded current manifest and
its reference. It does not preload
page-index nodes or payloads. Reports include load time, current and peak process
RSS, incremental RSS above the opened DB, ten repetitions of each name search,
and 100 verified extractions of 12 signals. The manifest mode loads names first
and then calls `_head` for each name; its total load time includes two catalog
passes. A production loader could combine them.

The prototypes assume the database stays unchanged. They intentionally do not
define refresh/invalidation behavior. They use the authoritative HEAD scan to
reproduce the original comparison even after the production catalog refactor.
They are Linux-only memory measurements (`/proc/self/statm`); RSS includes native
allocations but excludes the kernel's filesystem cache. Manifest preloading stops
if additional RSS exceeds 8 GiB, recording the actual cached count instead of
claiming a complete cache. `--memory-budget-mib` changes that limit.

### Results: one million signals, 2026-10-09

The retained database is `/tmp/pagestore-many-signals-_nud5zw4/store`. It contains
1,000,000 signals with 32 records each. Initial ingestion used the original 0.0.0
implementation, on local ext4, with Python 3.14.6 and NumPy 2.4.6. The full
[baseline report](results/million-signals-ext4-2026-10-09.json) records every
milestone. Source hashes distinguish it from the later working-tree refactor.

| Signals | Open read-only p50 | Exact read p50, first pass | Extract 12 p50 | Exact-regex search, one sample |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | 0.193 ms | 0.164 ms | 2.34 ms | 0.047 s |
| 10,000 | 0.199 ms | 0.158 ms | 2.21 ms | 0.433 s |
| 100,000 | 0.194 ms | 0.164 ms | 2.37 ms | 4.43 s |
| 1,000,000 | 0.194 ms | 0.639 ms | 7.75 ms | 232.53 s |

At one million signals, subsequent scans took 42.40 s for a 12-match regex and
40.75 s to list every name. DB opening and direct-name reads were already cheap;
opening two million small metadata files on every search was the bottleneck.

Ingestion took 2,665 s (44.4 min), averaging 375 signals/s including worker setup
and durable publication. Throughput was typically 540–590 signals/s, then fell
to roughly 110–115 signals/s near 890,000 signals and stayed there. A host-wide
[pressure observation](results/million-signals-system-observations-2026-10-09.jsonl)
showed I/O stalls and no memory pressure, but this shared-host run does not
establish the cause. The [progress log](results/million-signals-ext4-2026-10-09.jsonl)
preserves the slow tail. These creation timings predate name-catalog maintenance;
they are not refactored creation-throughput claims.

The original store occupied 7,000,003 files and 5,000,260 directories: 11.43 GB
of apparent file contents, with 45.16 GB allocated across files and directories,
for 512 MB of logical measurement payload. Filesystem inode tables/journals are
excluded. Small, independently recoverable signals are expensive in filesystem
objects; the search refactor does not eliminate that overhead. The original
final storage audit was paused for 149.74 s to isolate the cache experiments;
the report keeps its raw duration and an explicitly adjusted active duration.

The read-only cache experiments found:

| Snapshot cache | Additional RSS | 12-match regex p50 | Load time |
| --- | ---: | ---: | ---: |
| Names only | 87.5 MiB | 166 ms | 40.30 s |
| Names + current manifests | 5.85 GiB | 164 ms | 89.98 s, two passes |

Full manifests offered no name-search advantage. Reports:
[names](results/million-signals-cache-names-2026-10-09.json),
[manifests](results/million-signals-cache-manifests-2026-10-09.json).

### Persistent catalog refactor

The production implementation persists 256 sorted name shards behind one atomic,
checksummed catalog HEAD, lazily caches their names, and checks that small HEAD on
every search. It updates only the changed shard after a new signal is committed.
Ordinary updates to existing signals perform no global catalog publication or
locking. Creation intents make interrupted creations discoverable. The catalog
is derived metadata, with explicit rebuild and page-only recovery support.

The [first refactored run](results/million-signals-refactored-2026-10-09.json)
built the catalog from the retained signal HEADs in **187.20 s**, writing
28,078,251 bytes (26.78 MiB) across 257 JSON files. No measurement pages changed.
The reader then ran in a separate process from the rebuild:

| Operation at 1M signals | Original implementation | Refactored implementation |
| --- | ---: | ---: |
| Open read-only, p50 | 0.194 ms | 0.173 ms |
| Open writable, p50 | 0.301 ms | 0.272 ms |
| First regex search, fresh process cache | 232.53 s | 0.426 s |
| Repeated regex selecting 12 names | 42.40 s, one warmed scan | 117 ms p50, 123 ms p95 |
| List all names | 40.75 s, one warmed scan | 6.13 ms p50 |
| Extract 12 signals, p50 / p95 | 7.75 / 9.11 ms | 3.61 / 5.60 ms |

The cached names added 93.5 MiB of RSS. The first search read only 28.08 MB of
catalog data; subsequent searches read 37,803 bytes per call, with no writes.
Results verify all million names and every value/timestamp in the sampled
extractions. OS caches were retained, including freshly written catalog shards;
this is not a cold-device or network test. Differences in extraction latency can
reflect OS cache state: the refactor does not cache measurement manifests.

A [separate reopen run](results/million-signals-refactored-reopen-2026-10-09.json)
confirmed 0.433 s for first search, 114 ms p50 for the 12-match regex, 6.15 ms to
list all names, and 2.00 ms p50 / 2.25 ms p95 for extracting 12 signals with warmer
measurement caches. Read-only open was 0.242 ms p50. No rebuild was performed in
that run.

A separate [creation smoke test](results/refactored-creation-1000-2026-10-09.json)
used four workers to create and verify 1,000 signals with the refactored protocol.
Creation took 18.31 s including worker startup; the smaller temporary fixture was
removed. Creation deliberately pays additional shared catalog/fsync costs. The
million-signal store was not recreated, so there is no claim of million-signal
creation throughput for the refactored implementation.

Reproduce queries without changing measurement data:

```sh
PYTHONPATH=. python examples/benchmark_search.py \
    /tmp/pagestore-many-signals-_nud5zw4/store --signals 1000000 \
    --output /tmp/pagestore-search.json
```

Add `--rebuild` to time the one-time catalog build in a separate process. The
benchmark never removes the retained database. A fresh process measures lazy
loading separately from ten repeated searches and 100 verified extractions.

For the 100 TB ambition, this establishes fast discovery at one million names.
It does not qualify payload throughput or recovery at that volume. Even perfectly
packed 8 MiB pages require roughly 11.9 million data pages for 100 TB (decimal),
versus 1.49 million at 64 MiB, before partial-page and recovery overhead. Backend
namespace scaling, larger signal-specific page sizes, garbage collection, and
recovery at volume still need separate measurements.

## Local bulk-load baseline

Measured on 2026-10-09 with Python 3.14.6, NumPy 2.4.6, the workspace's Linux
Btrfs filesystem, and default 8 MiB pages. Each worker loads a separate fresh
signal with int64 timestamps and float64 scalar values, in 262,144-record source
batches. Workers synchronize after preparing input arrays. Timings include
opening the database, commits, and final checkpointing; they exclude source array
generation and process startup. The direct baseline uses the same file publication
and fsync routine, without PageStore metadata or checksums.

These are short single-run measurements, not sustained device qualification.
Filesystem caching, scheduling, and server conditions can materially change them.

| Workers | Total payload | Direct writes | PageStore ingestion | PageStore bytes/payload | Peak RSS per worker |
| --- | --- | --- | --- | --- | --- |
| 1 | 64 MiB | 602 MiB/s | 313 MiB/s | 1.00072 | 119 MiB |
| 4 | 128 MiB | 1594 MiB/s | 824 MiB/s | 1.00088 | 86–87 MiB |

Warm time to the first mapped batch, including touching a timestamp and value,
was about 0.36–0.43 ms. This is **not** a cold network latency measurement.

Profiling identified whole-commit concatenation as an avoidable cost. Replacing
it with page-sized coalescing reduced the one-worker peak RSS from about 198 MiB
to 119 MiB for the same 64 MiB payload. The implementation retains both checksum
passes and fsync publication. The remaining gap to direct writes means ingestion
is not yet uniformly storage-limited; future profiling should examine pipelined
preparation and publication on the actual deployment backend.

Reproduce from the repository:

```bash
PYTHONPATH=. python examples/benchmark.py --directory . --workers 1 --records 4194304
PYTHONPATH=. python examples/benchmark.py --directory . --workers 4 --records 2097152
```

Use `--page-size` to compare larger per-signal pages and `--directory` to target
the deployment mount. The command prints per-worker durations, publication counts,
written bytes, peak RSS, and warm first-batch latency, then removes its temporary
datasets. It does not connect to NXCALS. Fresh-write tests separately assert that
existing measurement pages are neither read nor rewritten and that index updates
copy only changed paths.
