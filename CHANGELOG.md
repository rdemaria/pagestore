# Changelog

## Unreleased

### Changed

- `DB(path_or_url)` now defaults to read-only (`mode="r"`). Creating or modifying
  a store requires explicit `mode="a"` or `mode="x"`. Migration, recovery, salvage,
  and writer examples now select writable mode explicitly.
- Rename per-signal `DB.info(name)` to `DB.info_signal(name)`; no compatibility
  alias is retained. `DB.info()` now returns `StoreInfo` with total storage size,
  live signal count, and record count. Size includes metadata, retained history,
  recovery copies, and pending files, with an explicit allocated/logical basis.
  Filesystem accounting includes directory allocation and handles hard links and
  sparse files. XRootD sums server file lengths using batched directory statistics.
- Add a DB representation showing location, mode, profile, and closed state without
  scanning storage. Store-wide info is an explicit, non-mutating metadata scan.
- Use adaptive base-128 directory trees for signals, measurement pages, index
  nodes, manifests, and paired recovery records. Collections start flat and add
  levels from persisted allocation counters without moving old objects or counting
  directory entries. A million signals need at most two extra directory levels.
- Reserve stable signal locations in the compact name catalog and anchor them in
  immutable `SIGNAL.json` records. Catalog rebuilds preserve reserved and deleted
  signal locations; existing-signal writes retain per-signal coordination. Exact
  lookups lazily load one shard and cache anchored locations.
- Support only the current experimental store layout (format 3), with no old-layout
  compatibility or automatic conversion. Unsupported versions are rejected before
  mutation. Binary pages remain version 1; recovery/salvage write the current layout.
- Signal searches now use a persistent, checksummed name catalog with a lazy
  in-memory cache. Each search checks for other workers' new signals and reloads
  only changed catalog shards. DB opening and exact-name reads remain lazy.
- Signal creation/recreation and full deletion acquire the shared catalog lock.
  Measurement updates and partial deletions retain per-signal coordination.
- Creation intents preserve discoverability when a writer is interrupted between
  publishing a signal and finalizing the name catalog. Replaced name-catalog
  shards are reclaimed; measurement and recovery files are unaffected.
- Integrity checks enumerate authoritative signal HEADs independently of the name
  cache, and page-only recovery rebuilds name discovery.

### Added

- Bounded PyTimber migration pilot tooling with frozen source manifests, strict
  legacy checksum/shape validation, record digests independent of page boundaries,
  immutable audit attempts, fresh-reader verification, and explicit quarantines.
  Source files are read only; partial/conflicting destination intervals are refused.
  The initial decoder supports uncompressed numeric pages; it is not a general
  legacy conversion or source-retirement command.
- `DB.refresh()` clears cached names, shards, locations, identities, and recovery
  verification results. Subsequent operations lazily reload metadata, including
  updates from other workers. It works read-only, performs no storage I/O, and
  preserves iterator snapshots and ambiguous-commit protection. External mount
  caches and in-flight writes remain subject to the backend's visibility rules.
- Explicit documentation and process tests for concurrent ordinary `store()`
  calls: simultaneous DB creation, independent signals, and overlapping updates
  to the same signal. No separate parallel API or mode is required; `ingest()`
  remains an optional streaming input method using the same coordination.
- `DB.delete_signal(name, t1=None, t2=None, *, timezone=None)` removes an inclusive
  interval and returns its record count. Missing names and empty selections are
  no-ops. Full deletion hides the name and commits an empty recovery checkpoint;
  partial deletion rewrites only boundary pages. Old files remain for existing
  readers and recovery, so deletion does not immediately reclaim disk space.
- Catalog intents now handle full deletion and recreation, including interrupted
  publication. Integrity checks and committed-state recovery preserve deletion
  tombstones; `RecoveryReport.deleted_signals` reports them separately.
- Public DB, container, result, and exception docstrings describe inputs, return
  values, time semantics, ownership, and partial-commit behavior.
- Native `root://`/`roots://` storage via the optional `pagestore[xrootd]` client:
  streamed uploads, server sync/close, size and checksum confirmation, remote
  rename publication, full page verification, EOS exclusive-directory locks and
  generic XRootD exclusive-file locks, with distinct persisted coordination families.
- `DB(..., xrootd_url=...)` maps an EOS/SSHFS path to its authoritative XRootD
  store for all I/O. Unconfigured weak mounts reject writes; read-only mounted
  pages use fully verified byte snapshots and bounded visibility retries.
- `io_timeout` and `visibility_timeout` options, explicit handling of ambiguous
  remote mutations, deterministic fault tests, and opt-in real-server tests.
  Live synthetic EOS tests include parallel writes and mount/URL coordination.
- Public `read_page(path)` returns a signal name, timestamps, and records from a
  standalone measurement page after verifying every section and the whole-page
  SHA-256. It requires no database metadata, returns owned arrays, and reports
  corruption without modifying the file.
- `maintenance.salvage()` reconstructs a new store from verified measurement pages
  without original catalogs or recovery records. It preserves intact page bytes,
  optionally reuses them through hard links, reports overlapping versions for
  explicit page selection, and writes a provenance log and `SalvageReport`.
- Catastrophic-loss salvage can rebuild a damaged envelope or missing metadata
  tail from an intact header and fully verified arrays. Regenerated whole-page
  digests are reported explicitly; strict recovery keeps its trusted-digest rule.
  Normal page encoding and write publication are unchanged.
- [Store layout documentation](doc/store_layout.md) and repository conventions in
  `AGENT.md`, including the requirement to maintain the layout alongside format
  changes.
- `DB.rebuild_catalog()` rebuilds the derived catalog from identities, HEADs, and manifests
  without reading measurement pages. It also clears interrupted creation intents
  and unreferenced catalog shards.
- `CatalogIncompleteError` identifies a committed, recoverable write whose name
  catalog finalization failed. Repair with `rebuild_catalog()` without replaying
  measurement data.
- Reproducible million-signal creation, cache-memory, and search benchmarks, with
  measured results in [benchmarks/README.md](benchmarks/README.md).
- Tests for cache refresh, parallel creation, interrupted publication, corruption,
  catalog repair, readers encountering retired shards, and standalone data-page
  salvage after loss of catalogs and recovery records.

### Compatibility

Only store format 3 is supported during experimentation. Earlier store layouts are
rejected, not upgraded in place. Offline recovery and salvage write the current
layout; data-only salvage operates on supported binary pages independently of
their source directories. Binary pages remain version 1. The original implementation
remains isolated under `pagestore.legacy`. Data-only salvage cannot infer deletions.

Remote stores record `eos-mkdir` or `xrootd-exclusive` coordination and require
the new backend code. Changing between coordination families requires offline
conversion; opening a mounted alias does not silently change the writer protocol.

For a current-format store with a missing name catalog, the first writable search
or new-signal creation rebuilds that derived cache. Read-only clients scan signal
HEADs until it is rebuilt. This fallback does not provide old-layout compatibility.

### Performance

Historical measurements on the earlier layout, before adaptive allocation and
location columns were introduced: on a local ext4 database with one million
signals and 32 records per signal, reopening and searching with OS caches retained measured:

- Read-only DB opening: 0.24 ms median.
- First search, including lazy catalog loading: 0.43 s.
- Repeated regex search selecting 12 names: 114 ms median, versus 42.40 s for the
  previous implementation's warmed scan.
- Listing all signal names: 6.15 ms median.
- Extracting 12 signals with warm measurement caches: 2.00 ms median.

The name cache uses about 94 MiB of additional reader memory and 27 MiB on disk.
Building it once from the existing million-signal store took 187 s. These local
measurements do not establish cold-device, network, or 100 TB payload performance.

## 0.0.0 — 2026-10-09

- Restarted PageStore around immutable, checksummed array pages, explicit
  little-endian storage, per-signal manifests, and paired recovery pages.
- Added numeric and datetime timestamps, string timestamp parsing with UTC as the
  default, bulk ingestion, configurable page sizes, and filesystem coordination.
- Moved the original implementation to `pagestore.legacy`.
- Added modern `pyproject.toml` packaging and published the initial PyPI release.
