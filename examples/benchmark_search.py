"""Benchmark production catalog/search against a retained many-signals database.

Read-only unless --rebuild is supplied, which rebuilds derived name metadata in
a separate process. Measurement pages are never changed; no OS caches are flushed.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing
from pathlib import Path
import random
from time import perf_counter
import traceback

from pagestore import DB

from benchmark_catalog_cache import current_rss
from benchmark_many_signals import (
    latency_summary,
    metadata,
    rss_bytes,
    signal_name,
    verify,
)


def rebuild(connection, directory):
    try:
        with DB(directory) as db:
            start = perf_counter()
            count = db.rebuild_catalog()
            connection.send(
                {
                    "signals": count,
                    "seconds": perf_counter() - start,
                    "peak_rss_bytes": rss_bytes(),
                    "io": db.io_metrics,
                }
            )
    except Exception:
        connection.send({"error": traceback.format_exc()})
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--signals", type=int, required=True)
    parser.add_argument("--records", type=int, default=32)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--search-samples", type=int, default=10)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.signals, args.records, args.samples, args.search_samples) < 1:
        parser.error("numeric arguments must be positive")
    report = {
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "directory": str(args.directory.resolve()),
        "signals": args.signals,
        "records_per_signal": args.records,
        "environment": metadata(),
        "cache_state": "fresh DB/process cache; OS caches retained",
        "complete": False,
    }
    report["environment"]["search_benchmark_sha256"] = hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest()

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")

    save()
    if args.rebuild:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=rebuild, args=(sender, args.directory))
        process.start()
        sender.close()
        result = receiver.recv()
        process.join()
        receiver.close()
        if "error" in result or process.exitcode:
            raise RuntimeError(result)
        if result["signals"] != args.signals:
            raise AssertionError("Unexpected rebuilt signal count")
        report["rebuild"] = result
        save()
        print(json.dumps({"event": "rebuild", **result}), flush=True)

    for mode in ("r", "a"):
        times = []
        for _ in range(args.samples):
            start = perf_counter()
            with DB(args.directory, mode=mode):
                pass
            times.append(perf_counter() - start)
        report[f"open_{mode}"] = latency_summary(times)
    with DB(args.directory, mode="r") as db:
        baseline_rss = current_rss()
        queries = [
            (
                "search_exact_regex",
                "^" + signal_name(args.signals // 2) + "$",
                [signal_name(args.signals // 2)],
            ),
            (
                "search_12_regex",
                r"^benchmark:signal:000000(?:0[0-9]|1[01])$",
                [signal_name(i) for i in range(min(12, args.signals))],
            ),
            ("search_all", "", None),
        ]
        before = db.io_metrics
        start = perf_counter()
        names = db.search(queries[0][1])
        report["first_search_seconds"] = perf_counter() - start
        assert names == queries[0][2]
        report["first_search_io"] = {k: db.io_metrics[k] - v for k, v in before.items()}
        report["cache_incremental_rss_bytes"] = current_rss() - baseline_rss
        report["cache_rss_bytes"] = current_rss()
        save()
        print(
            json.dumps(
                {"event": "first_search", "seconds": report["first_search_seconds"]}
            ),
            flush=True,
        )
        for label, pattern, expected in queries:
            times = []
            before = db.io_metrics
            for _ in range(args.search_samples):
                start = perf_counter()
                names = db.search(pattern)
                times.append(perf_counter() - start)
                if expected is not None:
                    assert names == expected
                else:
                    assert len(names) == args.signals
                    assert all(name == signal_name(i) for i, name in enumerate(names))
                del names
            report[label] = latency_summary(times)
            report[label]["io"] = {k: db.io_metrics[k] - v for k, v in before.items()}
        rng = random.Random(1729 + args.signals)
        indices = rng.sample(range(args.signals), min(args.samples, args.signals))
        times = []
        for index in indices:
            start = perf_counter()
            data = db.get_signal(signal_name(index))
            times.append(perf_counter() - start)
            verify(*data, index, args.records)
        report["exact_read"] = latency_summary(times)
        times = []
        for _ in range(args.samples):
            selected = rng.sample(range(args.signals), min(12, args.signals))
            names = [signal_name(i) for i in selected]
            start = perf_counter()
            data = db.get(names)
            times.append(perf_counter() - start)
            for index, name in zip(selected, names):
                verify(*data[name], index, args.records)
        report["extract_12_signals"] = latency_summary(times)
    report["peak_rss_bytes"] = rss_bytes()
    report["catalog_file_bytes"] = sum(
        p.stat().st_size for p in (args.directory / "catalog").rglob("*.json")
    )
    report["complete"] = True
    save()
    print(json.dumps({"event": "complete", "output": str(args.output)}), flush=True)


if __name__ == "__main__":
    main()
