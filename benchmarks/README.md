# Local bulk-load baseline

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
