"""Compare synthetic fresh-signal loading with direct durable writes.

Run with an installed package or PYTHONPATH=., preferably on the target mount.
Temporary benchmark directories are removed after both phases.
"""

import argparse
import json
import multiprocessing
from pathlib import Path
import resource
from tempfile import TemporaryDirectory
from time import perf_counter

import numpy as np

from pagestore import Batch, DB
from pagestore.backends import FileBackend

_barrier = None


def initialize(barrier):
    global _barrier
    _barrier = barrier


def worker(task):
    directory, name, records, page_size, direct = task
    timestamps = np.arange(records, dtype="i8")
    values = timestamps.astype("f8")
    _barrier.wait()
    start = perf_counter()
    if direct:
        backend = FileBackend(directory)
        rows = max(1, page_size // 16)
        for ordinal, offset in enumerate(range(0, records, rows)):
            backend.publish(
                f"{name}/{ordinal}.bin",
                [
                    memoryview(timestamps[offset : offset + rows]).cast("B"),
                    memoryview(values[offset : offset + rows]).cast("B"),
                ],
            )
        elapsed = perf_counter() - start
        first_batch_ms = None
        publications = backend.metrics["publications"]
        written = backend.metrics["written_bytes"]
    else:
        with DB(directory, mode="a") as db:

            def batches():
                for offset in range(0, records, 262144):
                    yield Batch(
                        timestamps[offset : offset + 262144],
                        values[offset : offset + 262144],
                    )

            db.ingest(name, batches(), max_page_size=page_size)
            elapsed = perf_counter() - start
            read_start = perf_counter()
            with db.iter_signal(name) as stream:
                batch = next(stream)
                # Touch both buffers: constructing mmap views alone is not first data.
                float(batch.timestamps[0]) + float(batch.values[0])
            first_batch_ms = 1000 * (perf_counter() - read_start)
            publications = db.io_metrics["publications"]
            written = db.io_metrics["written_bytes"]
    return {
        "seconds": elapsed,
        "payload_bytes": 16 * records,
        "written_bytes": written,
        "publications": publications,
        "warm_first_batch_ms": first_batch_ms,
        "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory",
        default=None,
        help="parent directory on the filesystem to measure",
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--records", type=int, default=1048576, help="records per signal/worker"
    )
    parser.add_argument("--page-size", type=int, default=8 * 1024**2)
    args = parser.parse_args()
    if min(args.workers, args.records, args.page_size) < 1:
        parser.error("workers, records, and page-size must be positive")
    with TemporaryDirectory(
        dir=args.directory, prefix="pagestore-benchmark-"
    ) as temporary:
        for direct in (True, False):
            directory = str(Path(temporary) / ("direct" if direct else "pagestore"))
            if not direct:
                with DB(directory, mode="a") as db:
                    backend = db.backend_info.__dict__
            context = multiprocessing.get_context("spawn")
            with context.Pool(
                args.workers,
                initializer=initialize,
                initargs=(context.Barrier(args.workers),),
            ) as pool:
                results = pool.map(
                    worker,
                    [
                        (directory, f"signal-{i}", args.records, args.page_size, direct)
                        for i in range(args.workers)
                    ],
                )
            payload = sum(r["payload_bytes"] for r in results)
            seconds = max(r["seconds"] for r in results)
            print(
                json.dumps(
                    {
                        "mode": "direct" if direct else "pagestore",
                        "workers": args.workers,
                        "MiB_per_second": payload / seconds / 1024**2,
                        "seconds": seconds,
                        "write_amplification": sum(r["written_bytes"] for r in results)
                        / payload,
                        "backend": None if direct else backend,
                        "worker_results": results,
                    },
                    indent=2,
                )
            )


if __name__ == "__main__":
    main()
