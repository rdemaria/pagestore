# Repository conventions and workflow

- Read `architecture.md` for design intent and `doc/store_layout.md` for the
  implemented format. Update the layout document in the same change whenever
  storage, catalogs, page encoding, timestamp semantics, or recovery changes.
- This is a fresh implementation; public API and internal structure may evolve.
  Keep the original implementation isolated under `pagestore.legacy` and document
  compatibility implications in `CHANGELOG.md` and the README.
- Use file-based metadata, not SQLite or pickle, in the new store. Preserve explicit
  endianness, integrity hashes, redundant recovery metadata, and reconstruction
  from data/recovery pages alone. Do not weaken durability to improve benchmarks.
- Keep committed-state recovery separate from data-only catastrophic salvage.
  Data pages must independently identify and verify measurements. Report overlapping
  versions for explicit selection; never guess which version was current.
- Plain paths select filesystem storage; infer its profile. Datetime input defaults
  to UTC, with explicit timezone selection. The default data-page limit is 8 MiB
  and can be overridden per signal.
- Optimize bulk ingestion by parallel workers owning different signals. Existing
  signal updates must stay independent; rare new-signal creation may pay shared
  catalog-maintenance costs. Keep opening and searching large catalogs fast.
- Use `/tmp` for local benchmarks and preserve retained databases, especially
  `/tmp/pagestore-many-signals-_nud5zw4/store`. Record filesystem/cache conditions,
  first and repeated search times, extraction latency, memory, and correctness.
- Add meaningful tests for behavior changes; run `python -m pytest -q` and
  `git diff --check` before delivery. Documentation-only changes need link and
  content checks, not another full benchmark or test run.
- Keep changes focused, update the unreleased changelog for user-visible changes,
  and distinguish implemented behavior from future plans. Commit and push to
  `main` when requested; publish releases only when requested.
