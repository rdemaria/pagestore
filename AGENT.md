# Repository conventions and workflow

- Read `architecture.md` for design intent and `doc/store_layout.md` for the
  implemented format. Update the layout document in the same change whenever
  storage, catalogs, page encoding, timestamp semantics, or recovery changes.
- This is a fresh implementation; public API and internal structure may evolve.
  Keep the original implementation isolated under `pagestore.legacy` and document
  breaking changes in `CHANGELOG.md` and the README. During experimentation,
  maintain only the current store layout; do not add compatibility branches or
  automatic conversions. Reject unsupported store versions before mutation.
- Use file-based metadata, not SQLite or pickle, in the new store. Preserve explicit
  endianness, integrity hashes, redundant recovery metadata, and reconstruction
  from data/recovery pages alone. Do not weaken durability to improve benchmarks.
- Keep committed-state recovery separate from data-only catastrophic salvage.
  Data pages must independently identify and verify measurements. Report overlapping
  versions for explicit selection; never guess which version was current.
- Plain paths select filesystem storage; infer its profile. Datetime input defaults
  to UTC, with explicit timezone selection. The default data-page limit is 8 MiB
  and can be overridden per signal.
- `DB` opens read-only by default. Writers, including migration and maintenance
  code, must explicitly request `mode="a"` or `mode="x"`.
- EOS/SSHFS close is not proof of remote commitment. Route weak mounted writes
  through an explicit authoritative XRootD URL; keep ambiguous mutation locks
  until reconciliation. Use only isolated synthetic stores under an authorized
  test parent when qualifying a remote backend.
- Optimize bulk ingestion by parallel workers owning different signals. Measurement
  updates and partial deletions stay independent; signal creation, recreation,
  and full deletion may pay shared catalog costs. Keep opening and searching large
  catalogs fast. Preserve deletion tombstones in committed-state recovery.
- Ordinary DB operations must handle concurrent workers through backend
  coordination. Do not add a separate parallel API, worker registration, or a
  parallel-mode switch. Streaming is an input/buffering choice, not a concurrency
  requirement; test normal store() calls across processes, including collisions.
- Grow signal and object directory trees adaptively with radix 128; do not fix
  their depth or relocate existing objects. Persist allocation counters, reserve
  stable signal locations, and retain identity anchors through deletion/rebuild.
  Bound normal fanout, including paired recovery files; do not count directory
  entries on the write path.
  Use one current layout and coordination namespace; update format identification
  when incompatible changes would otherwise make existing stores ambiguous.
- Use `/tmp` for local benchmarks and preserve retained databases, especially
  `/tmp/pagestore-many-signals-_nud5zw4/store`. Record filesystem/cache conditions,
  first and repeated search times, extraction latency, memory, and correctness.
- Add meaningful tests for behavior changes; run `python -m pytest -q` and
  `git diff --check` before delivery. Documentation-only changes need link and
  content checks, not another full benchmark or test run.
- Keep changes focused, update the unreleased changelog for user-visible changes,
  and distinguish implemented behavior from future plans. Commit and push to
  `main` when requested; publish releases only when requested.
