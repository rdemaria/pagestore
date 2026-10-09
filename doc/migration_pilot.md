# Bounded EOS migration pilot — 2026-10-09

Completed a source-preserving pilot into `/eos/project-a/abpdata/cerndatadb`, using
native `root://eosproject-a.cern.ch//eos/project/a/abpdata/cerndatadb` access.
No legacy source files were written or deleted. Three anomalous source pages are
explicitly excluded; this is not a complete migration of the legacy databases.

| Result | Measured value |
| --- | ---: |
| Imported legacy payload | 9,879,458,552 bytes (9.879 GB) |
| Accepted / selected legacy pages | 688 / 691 |
| Signals | 183 |
| Records | 240,616,407 |
| Store, including metadata and retained history | 9,911,187,941 logical bytes |
| Audit archive | 74,302,021 logical bytes before this final report |
| Store plus audit archive | 9,985,489,962 logical bytes |
| Local scratch, counting hard links once | 29,819,367,424 allocated bytes |
| Fully verified destination pages | 2,218 |
| Unchanged source metadata snapshots / imported component identities | 15 / 1,536 |

The selection was capped at 10,000,000,000 bytes. It reserved 9,899,999,712 bytes for
import candidates and used another 13,152,064 bytes for bounded overlap comparisons.
No other source measurement payloads were scanned. Source metadata, output overhead,
and local recovery fixtures were accounted separately. The observed project quota
increase was approximately 9.985 GB logical / 19.971 GB physical; this is project-wide
accounting and can include unrelated activity. Including local fixtures, additional
physical storage was approximately 50 GB, below the agreed 1–2 TB allowance.

All imported records matched fresh native readers using SHA-256 over explicitly
framed timestamp dtype/bytes, value dtype, individual shape and exact value bytes.
The digest is independent of page boundaries, canonicalizes byte order to little
endian, and preserves NaN payloads and signed zero. The full scan returned no
integrity errors. Scalar, fixed-array and ragged samples also matched the installed
PyTimber reader directly. Numeric float64 axes were preserved.

Committed-state recovery from only exported `.pg` files reproduced every imported
signal and record. Data-only salvage of the active measurement pages did the same,
using hard links to retain payload bytes unchanged. Original catalogs were absent
from these fixtures. A separate local fault fixture rejected a modified payload,
reported two overlapping valid versions, and succeeded when the known original
page was explicitly selected. These fixtures did not modify the EOS store.

The global metadata report contains 662 overlap candidates: 661 cross-database and
one within a database. Two sampled cross-database cases contained 9,789 identical
shared records, with no differing values. The remaining candidates are unresolved.
The following pages were excluded, with their source files retained:

| Source database under `lhc/` | Page | Finding |
| --- | ---: | --- |
| `LongTermMeasDB/LongTermMeasDB.db` | 37804 | `Data/037/804.idx`: zero bytes, expected 40. |
| `LongTermMeasDB/LongTermMeasDB2.db` | 130713 | `Data2/0130/713.len`: zero bytes, expected 2,896. |
| `LongTermMeasDB/LongTermMeasDB5.db` | 118656 | Non-monotonic timestamps, including 1,764 identical duplicate pairs. |

One initial audit-publication failure exposed a receipt-name collision when an
idempotent checkpoint reused its commit ID. Attempts now have unique names, and
local completion follows authoritative receipt publication. The already committed
unit was re-read and reconciled without duplicate records or replacement writes.
The remaining safe units completed on resumption; the three source anomalies were
explicitly quarantined. There was no forced interruption of an active EOS write.

| Timing | Result |
| --- | ---: |
| Open fresh DB | 3.9 ms |
| First / repeated search | 0.838 s / 3.4 ms |
| First batch from each of twelve sample signals | 43–180 ms |
| Main four-worker import pass, including per-unit verification | 969.85 s |
| Final resume, including receipt checks | 37.04 s |
| Main import peak RSS | 505.8 MiB |
| Final full verification and reconstruction workflow | 831.31 s |
| Strict recovery / data-only salvage construction | 31.07 s / 23.56 s |

These measurements used warm XRootD connections/server caches, four threads owning
different signals, and per-legacy-page commits and fresh reads. They are not a
cold-cache, million-signal, or maximum streaming-throughput benchmark. An initial
Python record-digest bottleneck was vectorized while retaining the identical digest
definition; persistent workers and larger commit groups remain the next production
performance qualification. Multi-host failover and credential expiry were not tested
by this pilot.

Retained evidence is in
`/eos/project-a/abpdata/store-migration/pagestore-pilot-20261009/`, with a local copy
and recovery fixtures in `/tmp/pagestore-pilot-20261009/`. The EOS directory contains
frozen metadata, `manifest.jsonl`, `roster.jsonl`, `collisions.jsonl`, source intents,
immutable completion receipts, run logs, quota observations, and `verification/`
reports. Files with a `.jsonl` suffix here contain complete JSON audit documents;
the suffix avoids the store backend's reserved `.json` envelope convention.

Exact code archives are pinned alongside SHA-256 files. The final implementation
archive is `code-r3.tar.gz`, SHA-256
`b4f61dd4b5a4d6ae4a358a0e01d6ceaa55e0db400d1bb668f79653e56f427818`, based on Git
`4229eb87f21407c1d36d44c53ab969316f948274` plus the working tree. No new release was
published. Environment: Python 3.14.6, NumPy 2.4.6, XRootD 6.1.0, PyTimber
4.1.7.0.dev0. Final local tests: **256 passed, 3 opt-in live tests skipped**;
`git diff --check` passed. Earlier live backend qualification is documented in
[xrootd_validation.md](xrootd_validation.md).

Pilot helpers are in [legacy/migration.py](../pagestore/legacy/migration.py) and
[migrate_legacy_pilot.py](../examples/migrate_legacy_pilot.py). They handle reviewed,
uncompressed numeric pages; they are not a general legacy converter or retirement
command. Resolve the exclusions and remaining overlap candidates, complete physical
reference accounting and production qualification, and review a separate retirement
manifest before deleting any legacy source.
