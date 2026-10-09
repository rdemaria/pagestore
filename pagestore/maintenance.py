"""Offline verification and reconstruction from finalized .pg files alone."""

from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .backends import FileBackend, require_filesystem_paths
from .catalog import (
    STORE_FORMAT_VERSION,
    canonical,
    digest,
    envelope,
    read_ref,
    signal_id,
    signal_prefix,
)
from .errors import CorruptionError
from .model import CheckReport, RecoveryReport
from .page_format import decode, read_page, repair_bytes
from .salvage import salvage


def check(db, *, full=False):
    """Return the non-mutating integrity CheckReport described by DB.check()."""
    report = CheckReport()
    backend = db._backend
    for copy in (0, 1):
        key = f"pages/recovery/store-{db._config['config_id']}.{copy}.pg"
        try:
            record = backend.read_page(key, full=full).recovery()
            if record["config"] != db._config:
                raise CorruptionError("Root recovery configuration disagrees")
            report.pages += 1
        except (OSError, CorruptionError, KeyError) as exc:
            report.errors.append(f"{key}: {exc}")
    try:
        names, ordinals = {}, set()
        for entry in db._scan_signal_entries():
            name, ordinal = entry["name"], entry["ordinal"]
            if name in names or ordinal in ordinals:
                raise CorruptionError("Duplicate signal name or allocation ordinal")
            names[name] = ordinal
            ordinals.add(ordinal)
    except (OSError, CorruptionError, KeyError) as exc:
        report.errors.append(f"Catalog: {exc}")
        return report
    # Never let a derived name cache hide signals from an integrity check. Use a
    # fresh reader to verify shard checksums even if db already cached their names.
    from .name_catalog import NameCatalog

    visible = []
    for name, ordinal in sorted(names.items()):
        try:
            manifest, _ = db._head(
                name,
                include_deleted=True,
                missing_ok=True,
                prefix=signal_prefix(name, ordinal),
            )
            if manifest is None:
                continue
            if manifest["root"] is not None:
                visible.append(name)
            for d in db._index(manifest).pages():
                try:
                    db._read_data(d, manifest, full=full)
                    report.pages += 1
                except (OSError, CorruptionError) as exc:
                    report.errors.append(f"{d['key']}: {exc}")
            seen = set()
            while manifest:
                if manifest["commit_id"] in seen:
                    raise CorruptionError("Recovery ancestry cycle")
                seen.add(manifest["commit_id"])
                for copy in (0, 1):
                    key = db._recovery_key(manifest, copy)
                    try:
                        record = backend.read_page(key, full=full).recovery()
                        if digest(canonical(record)) != manifest["recovery_sha256"]:
                            raise CorruptionError(
                                "Recovery content differs from manifest"
                            )
                        report.pages += 1
                    except (OSError, CorruptionError) as exc:
                        report.errors.append(f"{key}: {exc}")
                if manifest["recovery_kind"] == "checkpoint":
                    break
                parent = read_ref(backend, manifest["parent_ref"])
                if (
                    parent["commit_id"] != manifest["parent_commit_id"]
                    or parent["recovery_sha256"] != manifest["parent_recovery_sha256"]
                ):
                    raise CorruptionError("Recovery ancestry mismatch")
                manifest = parent
        except (OSError, CorruptionError, KeyError, ValueError) as exc:
            report.errors.append(f"{name}: {exc}")
    if backend.exists(NameCatalog.HEAD):
        try:
            catalog = NameCatalog(db)
            if catalog.names() != visible:
                raise CorruptionError(
                    "Name catalog differs from signal HEADs; rebuild_catalog() required"
                )
            if any(
                catalog.location(name) != ordinal for name, ordinal in names.items()
            ):
                raise CorruptionError("Name catalog signal locations disagree")
        except (OSError, CorruptionError, KeyError) as exc:
            report.errors.append(f"Name catalog: {exc}")
    return report


def _roster(latest, records):
    chain, seen = [], set()
    current = latest
    while current["record_kind"] != "checkpoint":
        cid = current["commit_id"]
        if cid in seen:
            raise CorruptionError("Recovery ancestry cycle")
        seen.add(cid)
        chain.append(current)
        parent = records.get(current["parent_commit_id"])
        if (
            parent is None
            or digest(canonical(parent)) != current["parent_recovery_sha256"]
            or parent["generation"] + 1 != current["generation"]
            or parent["signal_id"] != latest["signal_id"]
        ):
            raise CorruptionError(
                f"Missing or inconsistent predecessor for commit {cid}"
            )
        current = parent
    roster = {p["page_id"]: p for p in current["pages"]}
    if len(roster) != len(current["pages"]):
        raise CorruptionError("Duplicate page in checkpoint")
    for item in reversed(chain):
        for pid in item["removed"]:
            if pid not in roster:
                raise CorruptionError("Recovery delta retires an unknown page")
            del roster[pid]
        for d in item["added"]:
            if d["page_id"] in roster:
                raise CorruptionError("Recovery delta repeats a live page")
            roster[d["page_id"]] = d
    totals = {
        "count": sum(d["count"] for d in roster.values()),
        "page_count": len(roster),
        "stored_bytes": sum(d["size"] for d in roster.values()),
        "payload_bytes": sum(d["payload_bytes"] for d in roster.values()),
    }
    if totals != latest["totals"]:
        raise CorruptionError(
            "Recovery totals disagree with the reconstructed page set"
        )
    return list(roster.values())


def recover(source, destination):
    """Rebuild in a new directory, using only finalized pages (even flattened).

    The source must be quiescent. A report with complete=False does not silently
    substitute an older generation for missing/corrupt committed measurements.
    Empty deletion checkpoints are retained and listed in deleted_signals;
    recovered_signals lists only live signals. Retired payloads are not restored.
    """
    from .db import DB, _page_key
    from .timestamps import decode_time

    require_filesystem_paths(source, destination)
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if destination.exists() or destination == source or source in destination.parents:
        raise FileExistsError(
            "Recovery destination must be a new directory outside the source tree"
        )
    report = RecoveryReport(str(destination))
    if not source.is_dir():
        raise NotADirectoryError(source)
    records, configs, data = {}, {}, {}
    data_signals = set()
    source_files = list(source.rglob("*.pg"))
    # Keep paths, not mapped payloads, while scanning a potentially enormous dataset.
    for path in source_files:
        try:
            page = read_page(path, full=True, allow_damaged_envelope=True)
            h = page.header
            if h["page_kind"] == "recovery":
                if page.damaged_envelope:
                    page = decode(repair_bytes(page), full=True)
                    report.repaired_pages.append(str(path))
                record = page.recovery()
                cid = record["commit_id"]
                if record["scope"] == "database":
                    previous = configs.get(record["config"]["config_id"])
                    if previous is not None and previous != record["config"]:
                        report.errors.append(
                            "Conflicting verified root recovery copies"
                        )
                    configs[record["config"]["config_id"]] = record["config"]
                elif record["scope"] == "signal":
                    if cid in records and records[cid] != record:
                        report.errors.append(
                            f"Conflicting verified recovery copies for {cid}"
                        )
                    records[cid] = record
                else:
                    raise CorruptionError("Unknown recovery scope")
            else:
                data.setdefault(h["page_id"], []).append(path)
                data_signals.add(h["signal_name"])
        except (OSError, CorruptionError, KeyError, ValueError):
            # An intact paired record or duplicate data file can still supply this page.
            report.ignored_pages += 1
    candidates = list(configs.values()) or [r["config"] for r in records.values()]
    if report.errors:
        return report
    if not candidates:
        report.errors.append("No verified committed recovery records found")
        return report
    ids = {c["database_id"] for c in candidates}
    if len(ids) != 1:
        report.errors.append("Page set contains more than one database")
        return report
    config = candidates[0]
    if any(c != config for c in candidates):
        report.errors.append(
            "Conflicting root configurations; select a consistent backup"
        )
        return report
    if config.get("format_version") != STORE_FORMAT_VERSION:
        report.errors.append("Unsupported source database format")
        return report
    grouped = {}
    for record in records.values():
        if record["database_id"] != config["database_id"]:
            report.errors.append("Recovery record belongs to another database")
            return report
        grouped.setdefault(record["signal_name"], []).append(record)
    confirmed = {
        d["page_id"]
        for r in records.values()
        for d in r.get("pages", r.get("added", []))
    }
    report.unconfirmed_pages = sorted(set(data) - confirmed)
    for name in sorted(data_signals - set(grouped)):
        report.errors.append(
            f"{name}: data pages have no surviving commit record; their commit status is unknown"
        )

    backend = FileBackend(destination)
    backend.mkdir(destination)
    # Coordination is a destination capability; measurement identity is preserved.
    config = dict(
        config,
        coordination=backend.info.coordination,
        config_id=uuid4().hex,
    )
    backend.publish("store.json", [envelope(config)])
    with DB(destination, mode="a") as db:
        for name, history in sorted(grouped.items()):
            try:
                history.sort(key=lambda r: r["generation"], reverse=True)
                latest = history[0]
                if (
                    len(history) > 1
                    and history[1]["generation"] == latest["generation"]
                ):
                    raise CorruptionError(
                        "Ambiguous latest commit: two branches have the same generation"
                    )
                if latest["signal_id"] != signal_id(name):
                    raise CorruptionError("Signal identity mismatch")
                roster = _roster(latest, records)
                roster.sort(key=lambda d: decode_time(d["first"], latest["time_kind"]))
                if any(
                    decode_time(a["last"], latest["time_kind"])
                    >= decode_time(b["first"], latest["time_kind"])
                    for a, b in zip(roster, roster[1:])
                ):
                    raise CorruptionError("Recovered page intervals overlap")
                copied = []
                prefix = db._signal_prefix(name, create=True)
                for descriptor in roster:
                    validated = None
                    for path in data.get(descriptor["page_id"], []):
                        try:
                            page = read_page(
                                path,
                                full=True,
                                expected=descriptor["sha256"],
                                allow_damaged_envelope=True,
                            )
                            h = page.header
                            if (
                                h["signal_name"] != name
                                or h["signal_id"] != signal_id(name)
                                or h["database_id"] != config["database_id"]
                                or h["time_kind"] != latest["time_kind"]
                                or h["file_length"] != descriptor["size"]
                                or any(
                                    h[k] != descriptor[k]
                                    for k in (
                                        "page_id",
                                        "first",
                                        "last",
                                        "count",
                                        "schema",
                                        "statistics",
                                        "payload_bytes",
                                    )
                                )
                            ):
                                raise CorruptionError(
                                    "Recovery descriptor disagrees with data page"
                                )
                            validated = (
                                repair_bytes(page, descriptor["sha256"])
                                if page.damaged_envelope
                                else memoryview(page.buffer)
                            )
                            if page.damaged_envelope:
                                report.repaired_pages.append(str(path))
                            break
                        except (OSError, CorruptionError, KeyError, ValueError):
                            continue
                    if validated is None:
                        raise CorruptionError(
                            f"Missing or corrupt committed page {descriptor['page_id']}"
                        )
                    key = _page_key(
                        prefix,
                        descriptor["ordinal"],
                        descriptor["page_id"],
                    )
                    db._backend.publish(key, [validated])
                    copied.append(dict(descriptor, key=key))
                index = db._new_index(name, latest["time_kind"])
                index.update(copied)
                parent = {
                    "commit_id": latest["commit_id"],
                    "generation": latest["generation"],
                    "recovery_sha256": digest(canonical(latest)),
                }
                db._commit(
                    name,
                    latest["time_kind"],
                    index,
                    parent,
                    None,
                    copied,
                    [],
                    latest["next_ordinal"],
                    latest["max_page_size"],
                    checkpoint=True,
                )
                if copied:
                    report.recovered_signals.append(name)
                else:
                    report.deleted_signals.append(name)
            except (OSError, CorruptionError, ValueError, KeyError) as exc:
                report.errors.append(f"{name}: {exc}")
        db.rebuild_catalog()
        db._backend.publish("recovery-report.json", [canonical(asdict(report))])
    return report
