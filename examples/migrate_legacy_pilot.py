"""Execute an explicitly reviewed, bounded PyTimber pilot manifest over XRootD.

Usage: python examples/migrate_legacy_pilot.py /tmp/pilot-workspace --workers 2
The workspace must contain manifest.json and frozen metadata/, prepared by a
metadata-only inventory. This tool never deletes or opens source files for write.
It writes a sibling store-migration/<workspace-name> audit tree. Normal DB.ingest
provides coordination; workers own distinct signals. Source failures are reported,
not silently converted. A conflicting/partial destination interval stops its unit.
"""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import resource
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

from pagestore import DB
from pagestore.backends import XRootDBackend
from pagestore.legacy.migration import decode_numeric_page, import_verified_batch


def canonical(data):
    return (json.dumps(data, sort_keys=True, indent=2) + "\n").encode()


def durable(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    with pending.open("wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(pending, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(backend, key, raw):
    # Plain JSON audit files use .jsonl: .json is reserved for store envelopes.
    if backend.exists(key):
        if backend.read(key) != raw:
            raise ValueError("Existing audit entry differs: " + key)
    else:
        backend.publish(key, [raw])
    if hashlib.sha256(backend.read(key)).digest() != hashlib.sha256(raw).digest():
        raise ValueError("Audit readback mismatch: " + key)


def identity(info):
    return dict(size=info.size, modtime=info.modtime, id=info.id)


def publish_receipt(audit, workspace, result):
    """Retain every attempt, even when an idempotent checkpoint reuses its ID."""
    result = dict(result)
    uid = result["unit_id"]
    result["audit_key"] = (
        "done/" + uid + "/" + result["commit_id"] + "-" + uuid4().hex + ".jsonl"
    )
    raw = canonical(result)
    publish(audit, result["audit_key"], raw)
    # Local completion is only advertised after authoritative publication.
    durable(workspace / "done" / (uid + ".json"), raw)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--limit-units", type=int)
    parser.add_argument("--skip-completed", action="store_true")
    parser.add_argument(
        "--exclude-unit",
        action="append",
        default=[],
        help="Explicitly quarantine a reported unit ID; never delete its source",
    )
    args = parser.parse_args()
    if not 1 <= args.workers <= 4:
        parser.error("The bounded pilot permits one to four signal workers")
    work = args.workspace.resolve()
    manifest_raw = (work / "manifest.json").read_bytes()
    manifest = json.loads(manifest_raw)
    destination = manifest["destination"]
    if not destination.startswith("root://"):
        raise ValueError("This pilot runner requires authoritative XRootD storage")
    src_url, dst_url = urlsplit(manifest["source_root"]), urlsplit(destination)
    src_path, dst_path = src_url.path.rstrip("/"), dst_url.path.rstrip("/")
    if src_url.netloc == dst_url.netloc and (
        dst_path == src_path or dst_path.startswith(src_path + "/")
    ):
        raise ValueError("Destination must be outside the legacy source tree")
    journal_url = destination.rsplit("/", 1)[0] + "/store-migration/" + work.name
    pages = manifest["pages"]
    if not 0 < manifest["source_cap_bytes"] <= 10_000_000_000:
        raise ValueError("This pilot runner has a hard limit of 10 GB")
    if sum(p["bytes"] for p in pages) > manifest["source_cap_bytes"]:
        raise ValueError("Selection exceeds the explicit source byte budget")
    if len({p["base"] for p in pages}) != len(pages):
        raise ValueError("Duplicate physical source pages in selection")
    for p in pages:
        if p["bytes"] > 48 * 1024**2:
            raise ValueError("Pilot units must be at most 48 MiB")
    source = XRootDBackend(manifest["source_root"], writable=False)
    # Old empty SQLite filenames contain ':'; XRootD uses literal path names.
    source._url = lambda key: source.endpoint + "/" + source._path(key)
    journal = XRootDBackend(journal_url)
    start = time.monotonic()
    for src in manifest["sources"]:
        raw = source.read(src["source"])
        if hashlib.sha256(raw).hexdigest() != src["sha256"]:
            raise ValueError("Source metadata changed: " + src["source"])
        local = work / "metadata" / src["source"]
        if local.read_bytes() != raw:
            raise ValueError("Frozen metadata copy differs")
    publish(journal, "manifest.jsonl", manifest_raw)
    for name in [
        "collisions.json",
        "roster.json",
        "quota-before.txt",
        "code.tar.gz",
        "code.sha256",
        "prepare.py",
    ]:
        p = work / name
        if p.exists():
            publish(
                journal, name + ("l" if name.endswith(".json") else ""), p.read_bytes()
            )
    for src in manifest["sources"]:
        # Avoid legacy ':' names in new audit objects; provenance is in manifest.
        key = hashlib.sha256(src["source"].encode()).hexdigest() + ".db"
        publish(
            journal, "metadata/" + key, (work / "metadata" / src["source"]).read_bytes()
        )
    # Initialization is ordinary DB use and requires no special worker registry.
    with DB(destination, mode="a"):
        pass
    grouped = defaultdict(list)
    for p in pages[: args.limit_units]:
        uid = hashlib.sha256(
            (p["source"] + ":" + str(p["pageid"])).encode()
        ).hexdigest()
        if uid in args.exclude_unit:
            continue
        grouped[p["name"]].append(p)
    completed, failures, lock = [], [], threading.Lock()

    def signal_worker(name, units):
        remote = XRootDBackend(manifest["source_root"], writable=False)
        audit = XRootDBackend(journal_url)
        for m in sorted(units, key=lambda p: p["idxa"]):
            uid = hashlib.sha256(
                (m["source"] + ":" + str(m["pageid"])).encode()
            ).hexdigest()
            tick = time.monotonic()
            try:
                done_path = work / "done" / (uid + ".json")
                if args.skip_completed and done_path.exists():
                    old_raw = done_path.read_bytes()
                    old = json.loads(old_raw)
                    old_key = old.get(
                        "audit_key", "done/" + uid + "/" + old["commit_id"] + ".jsonl"
                    )
                    if audit.exists(old_key) and audit.read(old_key) == old_raw:
                        with lock:
                            completed.append(old)
                        continue
                files, identities, hashes = {}, {}, {}
                extensions = ["idx"] + (["len"] if m["reclen"] == -1 else [])
                if m["recsize"]:
                    extensions.append("rec")
                for ext in extensions:
                    key = m["base"] + "." + ext
                    before = identity(remote._call(remote._fs.stat, remote._path(key)))
                    # Enforce the metadata-derived byte bound before reading.
                    expected = m["recsize"] if ext == "rec" else m["count"] * 8
                    if before["size"] != expected:
                        raise ValueError("Source file size differs: " + key)
                    raw = remote.read(key)
                    after = identity(remote._call(remote._fs.stat, remote._path(key)))
                    if before != after or len(raw) != expected:
                        raise ValueError("Source file changed during read: " + key)
                    files[ext], identities[key] = raw, before
                    hashes[key] = hashlib.sha256(raw).hexdigest()
                    durable(work / "staged" / key, raw)
                batch = decode_numeric_page(m, files)
                intent = dict(
                    unit_id=uid,
                    metadata=m,
                    source_files=identities,
                    source_sha256=hashes,
                    manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
                )
                raw = canonical(intent)
                durable(work / "intents" / (uid + ".json"), raw)
                publish(audit, "intents/" + uid + ".jsonl", raw)
                read_seconds = time.monotonic() - tick
                result = import_verified_batch(destination, name, batch)
                result.update(
                    unit_id=uid,
                    source=m["source"],
                    pageid=m["pageid"],
                    signal=name,
                    source_bytes=m["bytes"],
                    read_seconds=read_seconds,
                    elapsed_seconds=time.monotonic() - tick,
                )
                result = publish_receipt(audit, work, result)
                with lock:
                    completed.append(result)
                    total = sum(r["source_bytes"] for r in completed)
                    print(
                        f"VERIFIED {len(completed)}/{sum(map(len, grouped.values()))} {total/1e9:.3f} GB {name} page={m['pageid']} {result['elapsed_seconds']:.2f}s",
                        flush=True,
                    )
            except Exception as exc:
                error = dict(
                    unit_id=uid,
                    signal=name,
                    source=m["source"],
                    pageid=m["pageid"],
                    error=repr(exc),
                )
                durable(work / "errors" / (uid + ".json"), canonical(error))
                with lock:
                    failures.append(error)
                    print("FAILED", json.dumps(error), flush=True)
                # Never continue this signal after an ambiguous mutation or conflict.
                break

    groups = list(grouped.items())
    # First signal establishes the serial path; then use independent signal workers.
    if groups:
        signal_worker(*groups[0])
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(signal_worker, *group) for group in groups[1:]]
        for future in as_completed(futures):
            future.result()
    unchanged = True
    for src in manifest["sources"]:
        unchanged &= (
            hashlib.sha256(source.read(src["source"])).hexdigest() == src["sha256"]
        )
    report = dict(
        destination=destination,
        journal=journal_url,
        completed_units=len(completed),
        selected_units=len(pages),
        imported_source_bytes=sum(r["source_bytes"] for r in completed),
        records=sum(r["records"] for r in completed),
        signals=len({r["signal"] for r in completed}),
        elapsed_seconds=time.monotonic() - start,
        peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        metadata_unchanged=unchanged,
        failures=failures,
        source_deletion=False,
        workers=args.workers,
        excluded_units=args.exclude_unit,
    )
    stamp = str(time.time_ns())
    raw = canonical(report)
    durable(work / ("run-" + stamp + ".json"), raw)
    publish(journal, "reports/run-" + stamp + ".jsonl", raw)
    print(json.dumps(report, indent=2), flush=True)
    if failures or not unchanged:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
