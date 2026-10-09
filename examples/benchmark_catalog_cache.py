"""Measure optional in-memory catalogs against an existing, quiescent benchmark DB.

This is a read-only benchmark prototype, not a change to DB's cache semantics.
It compares caching only sorted signal names with additionally caching decoded
HEAD targets (the latest manifests and their references). It does not preload
page-index nodes or measurement pages. Run each mode in a fresh process.
"""

import argparse
import json
from pathlib import Path
import random
import re
import sys
from time import perf_counter

from pagestore import DB

from benchmark_many_signals import (
    latency_summary,
    rss_bytes,
    signal_name,
    verify,
)


def current_rss():
    """Linux resident bytes, including native allocations; no tracemalloc overhead."""
    import os

    return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf(
        "SC_PAGE_SIZE"
    )


class CachedReadOnlyDB(DB):
    def __init__(self, path):
        super().__init__(path, mode="r")
        self.names = None
        self.heads = {}

    def search(self, regexp=""):
        self._open()
        if self.names is None:
            self.names = self._scan_signal_names()
        pattern = re.compile(regexp)
        return [name for name in self.names if pattern.search(name)]

    def _head(self, name, *, missing_ok=False):
        if name in self.heads:
            return self.heads[name]
        return super()._head(name, missing_ok=missing_ok)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--signals", type=int, required=True)
    parser.add_argument("--records", type=int, default=32)
    parser.add_argument("--mode", choices=("names", "manifests"), required=True)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--search-samples", type=int, default=10)
    parser.add_argument(
        "--memory-budget-mib",
        type=int,
        default=8192,
        help="stop preloading manifests after this additional RSS",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (
        min(
            args.signals,
            args.records,
            args.samples,
            args.search_samples,
            args.memory_budget_mib,
        )
        < 1
    ):
        parser.error("numeric arguments must be positive")
    result = {
        "mode": args.mode,
        "signals": args.signals,
        "cache_semantics": "quiescent read-only snapshot; no invalidation prototype",
        "python": sys.version.split()[0],
        "records_per_signal": args.records,
    }
    with CachedReadOnlyDB(args.directory) as db:
        baseline = current_rss()
        start = perf_counter()
        db.names = db._scan_signal_names()
        result["names_load_seconds"] = perf_counter() - start
        if len(db.names) != args.signals or any(
            name != signal_name(i) for i, name in enumerate(db.names)
        ):
            raise AssertionError("Unexpected signal roster")
        result["names_rss_bytes"] = current_rss()
        result["names_incremental_rss_bytes"] = current_rss() - baseline
        result["baseline_rss_bytes"] = baseline
        if args.mode == "manifests":
            start = perf_counter()
            for index, name in enumerate(db.names):
                db.heads[name] = DB._head(db, name)
                if (index + 1) % 10000 == 0:
                    resident = current_rss()
                    print(
                        json.dumps(
                            {
                                "event": "cache_progress",
                                "signals": index + 1,
                                "incremental_rss_bytes": resident - baseline,
                            }
                        ),
                        flush=True,
                    )
                    if resident - baseline > args.memory_budget_mib * 1024**2:
                        break
            result["manifests_load_seconds"] = perf_counter() - start
            result["cached_manifests"] = len(db.heads)
            result["manifest_cache_complete"] = len(db.heads) == args.signals
        result["cache_rss_bytes"] = current_rss()
        result["cache_incremental_rss_bytes"] = current_rss() - baseline

        queries = (
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
        )
        for label, pattern, expected in queries:
            timings = []
            for _ in range(args.search_samples):
                start = perf_counter()
                names = db.search(pattern)
                timings.append(perf_counter() - start)
                if expected is not None and names != expected:
                    raise AssertionError(f"Incorrect cached search: {pattern}")
                if expected is None and len(names) != args.signals:
                    raise AssertionError("Incorrect cached enumeration")
                del names
            result[label] = latency_summary(timings)

        rng = random.Random(1729 + args.signals)
        # Match the baseline random sequence before testing 12-signal extraction.
        ids = rng.sample(range(args.signals), min(args.samples, args.signals))
        timings = []
        for index in ids:
            start = perf_counter()
            data = db.get_signal(signal_name(index))
            timings.append(perf_counter() - start)
            verify(*data, index, args.records)
        result["exact_read"] = latency_summary(timings)
        timings = []
        for _ in range(args.samples):
            selected = rng.sample(range(args.signals), min(12, args.signals))
            names = [signal_name(index) for index in selected]
            start = perf_counter()
            data = db.get(names)
            timings.append(perf_counter() - start)
            for index, name in zip(selected, names):
                verify(*data[name], index, args.records)
        result["extract_12_signals"] = latency_summary(timings)
        result["peak_rss_bytes"] = rss_bytes()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "event": "cache_complete",
                "mode": args.mode,
                "cache_incremental_rss_bytes": result["cache_incremental_rss_bytes"],
                "output": str(args.output),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
