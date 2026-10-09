# XRootD validation

Validated on 2026-10-09 with Python 3.14.6, NumPy 2.4.6, and XRootD Python
bindings 6.1.0. These are correctness checks on synthetic data, not a throughput
benchmark or a qualification for 100 TB of production measurements.

## Live servers

- CERN EOS: `root://eosproject-a.cern.ch//eos/project/a/abpdata/tests`, corresponding
  to `/eos/project-a/abpdata/tests` through the existing SSHFS mount. Kerberos
  authentication used the current user's ticket. All stores were new UUID-named
  children of that authorized test directory; no legacy measurements were read.
- Generic XRootD: version 5.6.9, extracted under `/tmp/pagestore-xrootd-server`
  without installing system packages. A temporary server allowed localhost
  clients and exported only its dedicated synthetic-data directory. The server
  was stopped after testing.

The opt-in [integration tests](../tests/test_xrootd_integration.py) passed on both:
three tests on EOS and two on the generic server (the SSHFS alias test is specific
to the EOS deployment). They cover:

- Independent processes creating and loading different signals, then querying and
  fully checking the shared store; catalog rebuild and upsert of existing records.
- Actual cross-process lock contention and acquisition after release.
- A mounted-path alias and a native URL observing the same store and lock namespace;
  read-only access through the real SSHFS mount.
- One million numeric records, spanning multiple default 8 MiB pages and multiple
  protocol writes per page, with full content comparison and integrity checks.
- A reader capturing complete signal snapshots while another process publishes
  five updates to that signal.

The final EOS stores are retained at these paths relative to the mounted test
parent:

| Test | Retained directory |
| --- | --- |
| Parallel writers and locks | `test-8a875e02fd3d45f7909a063b66164ee7` |
| Mounted alias and read-only SSHFS | `test-883b842b541a487abc6fcff8ef3a33c8` |
| Large pages and concurrent readers | `test-d24ecdf299f54ce9b2d3afca340db816` |

Earlier failed/probe stores were also retained for diagnosis. They are synthetic
test artifacts, not migration destinations or production stores.

## Locking findings

The generic server accepted repeated `mkdir` on an existing directory; this cannot
implement an exclusive lock. It did enforce exclusive `OpenFlags.NEW` creation.
XrdCl retained a failed-open status on a reused file handle, so a lock contender
must allocate a fresh handle for each retry.

EOS rejected repeated namespace `mkdir` with `EEXIST`. However, a dedicated probe
accepted two `NEW | WRITE` opens of the same file before the first close; only an
open after close was rejected. Consequently EOS uses `eos-mkdir` coordination,
while generic XRootD uses `xrootd-exclusive`. These are persisted, incompatible
writer protocols. EOS owner records have unique filenames for each acquisition
to avoid stale file identity/redirect caches. Incomplete guards are never stolen.

The EOS primitive probe is retained as
`lock-probe-48e648be98f14af186e018daf59d796a` under the same test parent.

## Failure injection and remaining qualification

[Deterministic tests](../tests/test_xrootd.py) exercise delayed close visibility,
stale checksum/content reads, short reads, damaged pages, lost mutation responses,
late HEAD publication, interrupted close, catalog-finalization uncertainty,
permission failures, lock contention, and incompatible coordination profiles.
[Filesystem tests](../tests/test_backends.py) cover SSHFS detection, rejected mount
write overrides, and owned read buffers while cached files change.

Still required before production migration: multi-host partition/failover and
durability qualification, credential expiry/renewal and crash reconciliation on
the deployment, and the legacy migrator. An acknowledged server operation is not
evidence of independent backups. Native remote offline recovery/salvage is not yet
implemented; those tools require filesystem directories.

Run against an explicitly disposable parent:

```sh
PAGESTORE_TEST_XROOTD_URL=root://eosproject-a.cern.ch//eos/project/a/abpdata/tests \
PAGESTORE_TEST_MOUNT_PARENT=/eos/project-a/abpdata/tests \
python -m pytest -q -s tests/test_xrootd_integration.py
```

Each run creates new child stores and retains them. Without the environment
variables, ordinary test runs skip the live integration tests.
