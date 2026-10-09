"""Stress catalog cardinality with many small, independently committed signals.

Run with PYTHONPATH=. or an installed package. All writes use the public ingest
API with normal checksums, fsync, and recovery copies. Only a new temporary store
is touched; it is removed on exit unless --keep is specified. JSON results are
saved after every milestone, before cleanup. No OS caches are flushed.
"""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import platform
import random
import resource
import shutil
import subprocess
import sys
import tempfile
from time import perf_counter, process_time
import traceback

import numpy as np

import pagestore
from pagestore import Batch, DB

_writer = None
_timestamps = None
_base_values = None


def signal_name(index):
    return f"benchmark:signal:{index:08d}"


def rss_bytes():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss if sys.platform == "darwin" else rss * 1024


def initialize_writer(directory, records):
    global _writer, _timestamps, _base_values
    _writer = DB(directory, mode="a")
    _timestamps = np.arange(records, dtype="int64")
    _base_values = _timestamps.astype("float64") / 32


def create_signals(bounds):
    start, stop = bounds
    before = _writer.io_metrics
    wall, cpu = perf_counter(), process_time()
    for index in range(start, stop):
        result = _writer.ingest(
            signal_name(index), [Batch(_timestamps, _base_values + index)]
        )
        if result.inserted != len(_timestamps) or result.commits != 1:
            raise AssertionError(f"Unexpected ingestion result for signal {index}")
    after = _writer.io_metrics
    return {
        "signals": stop - start,
        "seconds": perf_counter() - wall,
        "cpu_seconds": process_time() - cpu,
        "io": {key: after[key] - before[key] for key in before},
        "pid": os.getpid(),
        "peak_rss_bytes": rss_bytes(),
    }


def latency_summary(samples):
    return {
        "samples": len(samples),
        "total_seconds": sum(samples),
        "mean_ms": float(np.mean(samples) * 1000),
        "p50_ms": float(np.percentile(samples, 50) * 1000),
        "p95_ms": float(np.percentile(samples, 95) * 1000),
        "p99_ms": float(np.percentile(samples, 99) * 1000),
        "max_ms": max(samples) * 1000,
    }


def verify(times, values, index, records):
    expected = np.arange(records, dtype="int64")
    if (
        times.dtype != np.dtype("int64")
        or values.dtype != np.dtype("float64")
        or not np.array_equal(times, expected)
        or not np.array_equal(values, expected.astype("float64") / 32 + index)
    ):
        raise AssertionError(f"Readback differs for signal {index}")


def measure_queries(directory, count, records, samples):
    """Use a fresh process per milestone to isolate reader peak RSS."""
    result = {"cache_state": "OS cache retained; first pass follows ingestion"}
    for mode in ("r", "a"):
        timings = []
        for _ in range(samples):
            start = perf_counter()
            with DB(directory, mode=mode):
                pass
            timings.append(perf_counter() - start)
        result[f"open_{mode}"] = latency_summary(timings)

    rng = random.Random(1729 + count)
    ids = rng.sample(range(count), min(samples, count))
    with DB(directory, mode="r") as db:
        for pass_name in ("first_pass", "repeat"):
            timings = []
            for index in ids:
                start = perf_counter()
                data = db.get_signal(signal_name(index))
                timings.append(perf_counter() - start)
                verify(*data, index, records)
            result[f"exact_read_{pass_name}"] = latency_summary(timings)

        timings = []
        extraction_size = min(12, count)
        for _ in range(samples):
            selected = rng.sample(range(count), extraction_size)
            names = [signal_name(index) for index in selected]
            start = perf_counter()
            extracted = db.get(names)
            timings.append(perf_counter() - start)
            if set(extracted) != set(names):
                raise AssertionError("Extraction returned unexpected signal names")
            for index, name in zip(selected, names):
                verify(*extracted[name], index, records)
        result["extract_12_signals"] = {
            **latency_summary(timings),
            "signals_per_extraction": extraction_size,
            "records_per_extraction": extraction_size * records,
        }

        # Regexes deliberately select few names: selectivity does not avoid the
        # current full catalog scan. Verify results outside each timed operation.
        exact = signal_name(count // 2)
        queries = (
            ("search_exact_regex", "^" + exact + "$", [exact]),
            (
                "search_12_regex",
                r"^benchmark:signal:000000(?:0[0-9]|1[01])$",
                [signal_name(i) for i in range(min(12, count))],
            ),
            ("search_all", "", None),
        )
        for label, pattern, expected in queries:
            start = perf_counter()
            names = db.search(pattern)
            seconds = perf_counter() - start
            if expected is None:
                if len(names) != count or any(
                    name != signal_name(i) for i, name in enumerate(names)
                ):
                    raise AssertionError("Full search differs from the written roster")
            elif names != expected:
                raise AssertionError(f"Incorrect results for {pattern}")
            result[label] = {"seconds": seconds, "matches": len(names)}
            print(
                json.dumps({"event": label, "signals": count, **result[label]}),
                flush=True,
            )
            del names
    result["peak_rss_bytes"] = rss_bytes()
    return result


def query_process(connection, *args):
    try:
        connection.send({"result": measure_queries(*args)})
    except BaseException:
        connection.send({"error": traceback.format_exc()})
    finally:
        connection.close()


def run_queries(context, *args):
    reader, sender = context.Pipe(duplex=False)
    process = context.Process(target=query_process, args=(sender, *args))
    process.start()
    sender.close()
    try:
        message = reader.recv()
    except EOFError as exc:
        raise RuntimeError("Query worker exited without results") from exc
    finally:
        process.join()
        reader.close()
    if "error" in message:
        raise RuntimeError(message["error"])
    if process.exitcode:
        raise RuntimeError(f"Query worker exited with {process.exitcode}")
    return message["result"]


def scan_storage(directory):
    """Exact file/dir totals; st_blocks excludes filesystem-wide inode/journal costs."""
    totals = {
        "files": 0,
        "directories": 0,
        "file_bytes": 0,
        "file_allocated_bytes": 0,
        "directory_allocated_bytes": 0,
    }
    categories = {}
    for root, dirs, files in os.walk(directory):
        totals["directories"] += 1
        totals["directory_allocated_bytes"] += os.stat(root).st_blocks * 512
        for name in files:
            path = Path(root) / name
            stat = path.stat()
            totals["files"] += 1
            totals["file_bytes"] += stat.st_size
            totals["file_allocated_bytes"] += stat.st_blocks * 512
            if name.endswith(".pg"):
                category = (
                    "recovery_pages" if path.parent.name == "recovery" else "data_pages"
                )
            elif name.endswith(".lock"):
                category = "locks"
            else:
                category = "catalog"
            item = categories.setdefault(
                category, {"files": 0, "bytes": 0, "allocated_bytes": 0}
            )
            item["files"] += 1
            item["bytes"] += stat.st_size
            item["allocated_bytes"] += stat.st_blocks * 512
    return {**totals, "categories": categories}


def free_space(path):
    stat = os.statvfs(path)
    return {
        "bytes": stat.f_bavail * stat.f_frsize,
        "inodes": stat.f_favail if stat.f_files else None,
    }


def metadata():
    package = Path(pagestore.__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        digest.update(path.relative_to(package).as_posix().encode())
        digest.update(path.read_bytes())
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).parent,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = None
    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pagestore": pagestore.__version__,
        "platform": platform.platform(),
        "logical_cpus": os.cpu_count(),
        "source_revision": revision,
        "package_source_sha256": digest.hexdigest(),
        "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory", default=None, help="parent directory on the target filesystem"
    )
    parser.add_argument("--signals", type=int, default=1_000_000)
    parser.add_argument("--records", type=int, default=32, help="records per signal")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--chunk-size", type=int, default=256)
    parser.add_argument(
        "--samples",
        type=int,
        default=100,
        help="open/read/extraction timing samples per milestone",
    )
    parser.add_argument("--milestones", default="1000,10000,100000,1000000")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="JSON report, updated after every milestone",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="retain the temporary database after the benchmark",
    )
    args = parser.parse_args()
    if min(args.signals, args.records, args.workers, args.chunk_size, args.samples) < 1:
        parser.error("numeric arguments must be positive")
    if args.signals > 100_000_000:
        parser.error("signal names support at most 100,000,000 signals")
    try:
        milestones = sorted(
            {args.signals}
            | {int(n) for n in args.milestones.split(",") if 0 < int(n) < args.signals}
        )
    except ValueError:
        parser.error("milestones must be comma-separated integers")

    temporary = Path(
        tempfile.mkdtemp(prefix="pagestore-many-signals-", dir=args.directory)
    )
    directory = temporary / "store"
    report = {
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "environment": metadata(),
        "parameters": {**vars(args), "output": str(args.output)},
        "directory": str(directory),
        "milestones": [],
        "complete": False,
        "space_before": free_space(temporary),
    }

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")

    print(
        json.dumps(
            {"event": "start", "directory": str(directory), "signals": args.signals}
        ),
        flush=True,
    )
    started = perf_counter()
    context = multiprocessing.get_context("spawn")
    try:
        with DB(directory, mode="x") as db:
            report["backend"] = asdict(db.backend_info)
            report["initialization_io"] = db.io_metrics
        save()
        previous, written, write_seconds = 0, 0, 0.0
        with context.Pool(
            args.workers,
            initializer=initialize_writer,
            initargs=(str(directory), args.records),
        ) as pool:
            for count in milestones:
                stage_start = perf_counter()
                stage = {
                    "signals": count,
                    "new_signals": count - previous,
                    "io": {"written_bytes": 0, "read_bytes": 0, "publications": 0},
                    "worker_peak_rss_bytes": {},
                    "worker_cpu_seconds": 0.0,
                }
                tasks = (
                    (first, min(first + args.chunk_size, count))
                    for first in range(previous, count, args.chunk_size)
                )
                last_progress = stage_start
                for result in pool.imap_unordered(create_signals, tasks):
                    written += result["signals"]
                    stage["worker_cpu_seconds"] += result["cpu_seconds"]
                    stage["worker_peak_rss_bytes"][str(result["pid"])] = result[
                        "peak_rss_bytes"
                    ]
                    for key, value in result["io"].items():
                        stage["io"][key] += value
                    if perf_counter() - last_progress >= 10:
                        elapsed = perf_counter() - stage_start
                        print(
                            json.dumps(
                                {
                                    "event": "write_progress",
                                    "signals": written,
                                    "target": count,
                                    "stage_seconds": elapsed,
                                    "signals_per_second": (written - previous)
                                    / elapsed,
                                }
                            ),
                            flush=True,
                        )
                        last_progress = perf_counter()
                stage["write_seconds"] = perf_counter() - stage_start
                write_seconds += stage["write_seconds"]
                stage["signals_per_second"] = (count - previous) / stage[
                    "write_seconds"
                ]
                stage["cumulative_write_seconds"] = write_seconds
                stage["space_after_write"] = free_space(temporary)
                # Check resource headroom from measured signal layout before scaling.
                if not previous:
                    with DB(directory, mode="r") as reader:
                        footprint = scan_storage(
                            directory / reader._signal_prefix(signal_name(0))
                        )
                    remaining = args.signals - count
                    estimated_bytes = remaining * (
                        footprint["file_allocated_bytes"]
                        + footprint["directory_allocated_bytes"]
                    )
                    estimated_inodes = remaining * (
                        footprint["files"] + footprint["directories"]
                    )
                    report["sample_signal_storage"] = footprint
                    report["remaining_space_estimate"] = {
                        "bytes": estimated_bytes,
                        "inodes": estimated_inodes,
                    }
                    available = stage["space_after_write"]
                    if estimated_bytes * 1.3 > available["bytes"] or (
                        available["inodes"] is not None
                        and estimated_inodes * 1.3 > available["inodes"]
                    ):
                        raise RuntimeError(
                            "Insufficient free bytes/inodes for remaining signals plus 30% headroom"
                        )
                print(
                    json.dumps(
                        {
                            "event": "measure_queries",
                            "signals": count,
                            "write_seconds": stage["write_seconds"],
                            "signals_per_second": stage["signals_per_second"],
                        }
                    ),
                    flush=True,
                )
                stage["queries"] = run_queries(
                    context, str(directory), count, args.records, args.samples
                )
                report["milestones"].append(stage)
                save()
                previous = count
        print(json.dumps({"event": "scan_storage", "signals": written}), flush=True)
        scan_start = perf_counter()
        report["storage"] = scan_storage(directory)
        report["storage_scan_seconds"] = perf_counter() - scan_start
        report["payload_bytes"] = args.signals * args.records * 16
        report["total_write_seconds"] = write_seconds
        report["elapsed_before_cleanup_seconds"] = perf_counter() - started
        report["complete"] = True
        save()
    except BaseException:
        report["error"] = traceback.format_exc()
        save()
        raise
    finally:
        if not args.keep:
            print(
                json.dumps({"event": "cleanup", "directory": str(temporary)}),
                flush=True,
            )
            cleanup_start = perf_counter()
            shutil.rmtree(temporary)
            report["cleanup_seconds"] = perf_counter() - cleanup_start
            report["retained"] = False
        else:
            report["retained"] = True
        report["total_elapsed_seconds"] = perf_counter() - started
        save()
    print(
        json.dumps(
            {
                "event": "complete",
                "report": str(args.output),
                "seconds": report["total_elapsed_seconds"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
