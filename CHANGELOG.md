# Changelog

## Unreleased

### Changed

- Signal searches now use a persistent, checksummed name catalog with a lazy
  in-memory cache. Each search checks for other workers' new signals and reloads
  only changed catalog shards. DB opening and exact-name reads remain lazy.
- Only new signal creation acquires the shared catalog lock. Updates to existing
  signals retain independent per-signal coordination.
- Creation intents preserve discoverability when a writer is interrupted between
  publishing a signal and finalizing the name catalog. Replaced name-catalog
  shards are reclaimed; measurement and recovery files are unaffected.
- Integrity checks enumerate authoritative signal HEADs independently of the name
  cache, and page-only recovery rebuilds name discovery.

### Added

- [Store layout documentation](doc/store_layout.md) and repository conventions in
  `AGENT.md`, including the requirement to maintain the layout alongside format
  changes.
- `DB.rebuild_catalog()` rebuilds the derived name catalog from HEADs and manifests
  without reading measurement pages. It also clears interrupted creation intents
  and unreferenced catalog shards.
- `CatalogIncompleteError` identifies a committed, recoverable write whose name
  catalog finalization failed. Repair with `rebuild_catalog()` without replaying
  measurement data.
- Reproducible million-signal creation, cache-memory, and search benchmarks, with
  measured results in [benchmarks/README.md](benchmarks/README.md).
- Tests for cache refresh, parallel creation, interrupted publication, corruption,
  catalog repair, and readers encountering retired shards. The suite passes 115
  tests.

### Compatibility

The measurement/page format and public search semantics are unchanged. For stores
created by the original 0.0.0 implementation, the first writable search or new-signal
creation builds the missing catalog once. Read-only clients retain the HEAD-scan
fallback until a writable client builds it. All writers must use catalog-aware
code afterward; rebuild the catalog after adding signals with older code.

### Performance

On a local ext4 database with one million signals and 32 records per signal,
reopening and searching with OS caches retained measured:

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
