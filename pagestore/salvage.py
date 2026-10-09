"""Offline import of verified data pages without any original commit metadata."""

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from uuid import UUID, uuid4

from .backends import FileBackend
from .catalog import canonical, envelope, signal_id, signal_prefix
from .errors import CorruptionError, PageStoreError
from .model import DEFAULT_MAX_PAGE_SIZE, SalvageReport, integer
from .page_format import read_salvage_page, repair_bytes
from .page_index import PageIndex
from .timestamps import decode_time


def _candidate(path):
    """Validate standalone identity, all sections, and the reconstructible envelope."""
    page = read_salvage_page(path)
    h = page.header
    if h["page_kind"] == "recovery":
        return None, None
    if h["signal_id"] != signal_id(h["signal_name"]):
        raise CorruptionError("Data-page signal identity mismatch")
    for key in ("database_id", "page_id"):
        if not isinstance(h[key], str) or UUID(h[key]).hex != h[key]:
            raise CorruptionError(f"Invalid data-page {key}")
    integer(h["ordinal"], "page ordinal", 0)
    integer(h["max_page_size"], "max_page_size", 1)
    if h["payload_bytes"] != page.batch().nbytes:
        raise CorruptionError("Data-page payload size mismatch")
    repair = None
    raw = memoryview(page.buffer)
    if page.damaged_envelope:
        try:
            raw = repair_bytes(page)
            repair = "original_digest"
        except CorruptionError:
            raw = repair_bytes(page, allow_new_digest=True)
            repair = "new_digest"
    descriptor = {
        "sha256": bytes(raw[-32:]).hex(),
        "size": len(raw),
        **{
            k: h[k]
            for k in (
                "page_id",
                "ordinal",
                "count",
                "first",
                "last",
                "schema",
                "statistics",
                "payload_bytes",
            )
        },
    }
    return {
        "source": str(path),
        "database_id": h["database_id"],
        "signal_name": h["signal_name"],
        "time_kind": h["time_kind"],
        "max_page_size": h["max_page_size"],
        "repair": repair,
        "descriptor": descriptor,
    }, raw


def _paths(source, pages):
    paths = source.rglob("*.pg") if pages is None else (source / Path(p) for p in pages)
    for path in paths:
        resolved = path.resolve()
        if not resolved.is_relative_to(source):
            raise ValueError("Selected pages must be inside the source directory")
        yield resolved


def _select(candidates, report, name):
    """Deduplicate identical page identities, but never guess between versions."""
    unique = {}
    kinds = set()
    conflict = False
    for item in candidates:
        kinds.add(item["time_kind"])
        descriptor = item["descriptor"]
        previous = unique.get(descriptor["page_id"])
        if previous is None:
            unique[descriptor["page_id"]] = item
        elif previous["descriptor"] != descriptor:
            conflict = True
        else:
            report.duplicate_pages += 1
            # Prefer intact bytes so hardlink reuse remains possible.
            if previous["repair"] is not None and item["repair"] is None:
                unique[descriptor["page_id"]] = item
    if len(kinds) != 1:
        conflict = True
    selected = list(unique.values())
    if not conflict:
        kind = next(iter(kinds))
        selected.sort(key=lambda item: decode_time(item["descriptor"]["first"], kind))
        conflict = any(
            decode_time(a["descriptor"]["last"], kind)
            >= decode_time(b["descriptor"]["first"], kind)
            for a, b in zip(selected, selected[1:])
        )
    if conflict:
        report.conflicts[name] = sorted({item["source"] for item in candidates})
        return []
    return selected


def salvage(source, destination, *, pages=None, reuse="copy", scratch_directory=None):
    """Create a new store from verified data pages, ignoring all old commit records.

    Source must be quiescent. ``pages`` optionally selects paths inside source;
    otherwise all *.pg files are scanned. Every conflicting signal is excluded and
    reported for explicit selection. Intact pages are reused byte-for-byte. Use
    reuse="hardlink" to avoid copying payloads on the same filesystem; linked source
    files must remain immutable. Damaged envelopes are repaired into new files.

    No original catalog or recovery page is required. The embedded database UUID
    is preserved, while new commits/configuration/indexes are created. Different
    source database UUIDs cannot be merged. An empty source cannot establish an
    original database identity. Success never implies recovery of the original
    committed state, absence of missing pages, or known commit status.

    Metadata is spooled into 256 temporary partitions (optionally under
    scratch_directory); only one partition and individual page buffers are loaded
    at a time. The detailed provenance log and report are saved in destination.
    """
    from .db import DB, _page_key

    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_dir():
        raise NotADirectoryError(source)
    if destination.exists() or destination == source or source in destination.parents:
        raise FileExistsError(
            "Salvage destination must be new and outside the source tree"
        )
    if reuse not in {"copy", "hardlink"}:
        raise ValueError("reuse must be 'copy' or 'hardlink'")
    backend = FileBackend(destination)
    report = SalvageReport(str(destination))
    identities = set()
    with tempfile.TemporaryDirectory(
        prefix="pagestore-salvage-", dir=scratch_directory
    ) as temp:
        scratch = Path(temp)
        for path in _paths(source, pages):
            try:
                item, raw = _candidate(path)
                if item is None:
                    report.ignored_recovery_pages += 1
                    continue
                identities.add(item["database_id"])
                shard = signal_id(item["signal_name"])[:2]
                with (scratch / f"{shard}.jsonl").open("ab") as stream:
                    stream.write(canonical(item) + b"\n")
                del raw
            except (OSError, CorruptionError, ValueError, TypeError, KeyError) as exc:
                report.rejected_pages[str(path)] = str(exc)
        if len(identities) != 1:
            report.errors.append(
                "No verified data pages found"
                if not identities
                else "Data pages belong to multiple databases; select one database explicitly"
            )
            backend.publish("salvage-report.json", [canonical(asdict(report))])
            return report
        report.source_database_id = next(iter(identities))
        config = {
            "format_version": 1,
            "database_id": report.source_database_id,
            "config_id": uuid4().hex,
            "default_max_page_size": DEFAULT_MAX_PAGE_SIZE,
            "coordination": backend.info.coordination,
            "lock_namespace": "per-signal-v1",
        }
        backend.publish("store.json", [envelope(config)])
        provenance_path = scratch / "provenance.jsonl"
        with DB(destination) as db, provenance_path.open("wb") as provenance:
            for partition in sorted(scratch.glob("[0-9a-f][0-9a-f].jsonl")):
                grouped = {}
                with partition.open("rb") as stream:
                    for line in stream:
                        item = json.loads(line)
                        grouped.setdefault(item["signal_name"], []).append(item)
                for name, candidates in sorted(grouped.items()):
                    selected = _select(candidates, report, name)
                    if not selected:
                        continue
                    copied, entries = [], []
                    try:
                        for item in selected:
                            current, raw = _candidate(Path(item["source"]))
                            if current != item:
                                raise CorruptionError("Source changed during salvage")
                            descriptor = item["descriptor"]
                            key = _page_key(
                                signal_prefix(name),
                                descriptor["ordinal"],
                                descriptor["page_id"],
                            )
                            if reuse == "hardlink" and item["repair"] is None:
                                db._backend.link(item["source"], key)
                                action = "hardlink"
                                report.linked_pages += 1
                            else:
                                db._backend.publish(key, [raw])
                                action = "repair" if item["repair"] else "copy"
                                report.copied_pages += 1
                            del raw
                            copied.append(dict(descriptor, key=key))
                            entries.append(
                                dict(item, destination_key=key, action=action)
                            )
                        kind = selected[0]["time_kind"]
                        index = PageIndex(db._backend, signal_prefix(name), kind)
                        index.update(copied)
                        commit = db._commit(
                            name,
                            kind,
                            index,
                            None,
                            None,
                            copied,
                            [],
                            max(d["ordinal"] for d in copied) + 1,
                            max(item["max_page_size"] for item in selected),
                            checkpoint=True,
                        )
                        report.salvaged_signals.append(name)
                        report.salvaged_pages += len(copied)
                        report.salvaged_records += sum(d["count"] for d in copied)
                        for entry in entries:
                            entry["destination_commit_id"] = commit.commit_id
                            provenance.write(canonical(entry) + b"\n")
                            if entry["repair"]:
                                report.repaired_pages.append(entry["source"])
                            if entry["repair"] == "new_digest":
                                report.regenerated_digests.append(entry["source"])
                    except (
                        OSError,
                        PageStoreError,
                        ValueError,
                        TypeError,
                        KeyError,
                    ) as exc:
                        report.errors.append(f"{name}: {exc}")

        def provenance_chunks():
            with provenance_path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    yield chunk

        backend.publish("salvage-pages.jsonl", provenance_chunks())
        backend.publish("salvage-report.json", [canonical(asdict(report))])
    return report
