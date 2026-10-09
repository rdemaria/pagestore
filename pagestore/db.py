"""The PageStore database API and filesystem commit protocol."""

from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
import re
from uuid import uuid4

import numpy as np

from .backends import FileBackend
from .catalog import (
    canonical,
    digest,
    envelope,
    read_ref,
    signal_id,
    signal_prefix,
    unpack,
    write_immutable,
)
from .errors import (
    CommitOutcomeUnknownError,
    CorruptionError,
    IngestError,
    OverlapError,
    RecoveryIncompleteError,
    SignalNotFoundError,
    StoreError,
)
from .model import (
    DEFAULT_MAX_PAGE_SIZE,
    Batch,
    IngestResult,
    RaggedArray,
    SignalInfo,
    WriteResult,
    integer,
    merge_batches,
    normalize,
    page_batches,
    schema_key,
)
from .page_format import data_plan, read_page, recovery_plan
from .page_index import PageIndex
from .name_catalog import NameCatalog
from .timestamps import (
    KINDS,
    bound,
    decode_time,
    normalize_timestamps,
    resolve_timezone,
)


def _page_key(prefix, ordinal, page_id):
    parts = [f"{ordinal % 100:02d}"]
    ordinal //= 100
    while ordinal:
        parts.append(f"{ordinal % 100:02d}")
        ordinal //= 100
    return prefix + "/pages/" + "/".join(reversed(parts)) + f"-{page_id}.pg"


class DB(AbstractContextManager):
    def __init__(
        self,
        url,
        *,
        mode="a",
        default_max_page_size=None,
        lock_timeout=30.0,
        timezone="utc",
    ):
        if mode not in {"r", "a", "x"}:
            raise ValueError("mode must be 'r', 'a', or 'x'")
        resolve_timezone(timezone)
        self.timezone = timezone
        self._closed = False
        self._backend = FileBackend(
            url, writable=mode != "r", lock_timeout=lock_timeout
        )
        self._verified_recovery = set()
        requested = (
            None
            if default_max_page_size is None
            else integer(default_max_page_size, "default_max_page_size", 1)
        )
        if not self._backend.exists("store.json") and self._backend.exists(
            "pagestore.db"
        ):
            raise ValueError(
                "Legacy database detected; open it with pagestore.legacy.PageStore and copy data into a separate new DB"
            )
        if mode == "r" or self._backend.exists("store.json"):
            if mode == "x":
                raise FileExistsError(self._backend.root)
            self._config = unpack(self._backend.read("store.json"))
        else:
            with self._backend.lock(".INIT", family="mkdir"):
                if self._backend.exists("store.json"):
                    if mode == "x":
                        raise FileExistsError(self._backend.root)
                    self._config = unpack(self._backend.read("store.json"))
                else:
                    if any(
                        p.name != ".INIT.LOCK" for p in self._backend.root.iterdir()
                    ):
                        raise CorruptionError(
                            "Directory is not an empty/new database; recover missing store.json instead of recreating it"
                        )
                    self._config = {
                        "format_version": 1,
                        "database_id": uuid4().hex,
                        "config_id": uuid4().hex,
                        "default_max_page_size": requested or DEFAULT_MAX_PAGE_SIZE,
                        "coordination": self._backend.info.coordination,
                        "lock_namespace": "per-signal-v1",
                    }
                    self._backend.publish("store.json", [envelope(self._config)])
        self._validate_config(requested)
        self._backend.bind_coordination(self._config["coordination"])
        self._name_catalog = NameCatalog(self)
        if mode != "r":
            # Existing workers need no database-wide writer lock. Root recovery
            # repair is idempotent: competing repairs publish identical bytes.
            self._ensure_root_recovery()

    def _validate_config(self, requested):
        if (
            self._config.get("format_version") != 1
            or self._config.get("lock_namespace") != "per-signal-v1"
        ):
            raise CorruptionError(
                "Unsupported database format or coordination namespace"
            )
        integer(
            self._config["default_max_page_size"], "stored default_max_page_size", 1
        )
        if requested is not None and requested != self._config["default_max_page_size"]:
            raise ValueError(
                "default_max_page_size conflicts with the existing database default"
            )

    def _ensure_root_recovery(self):
        record = {
            "scope": "database",
            "record_kind": "checkpoint",
            "config": self._config,
            "commit_id": self._config["config_id"],
        }
        raw = None
        for copy in (0, 1):
            key = f"pages/recovery/store-{self._config['config_id']}.{copy}.pg"
            try:
                intact = (
                    read_page(self._backend.path(key), full=True).recovery() == record
                )
            except (OSError, CorruptionError):
                intact = False
            if not intact:
                if raw is None:
                    raw = recovery_plan(record).to_bytes()
                self._backend.publish(key, [raw], replace=True)

    @property
    def backend_info(self):
        return self._backend.info

    @property
    def io_metrics(self):
        """Catalog/streamed I/O counters; mmap reads are not counted as read_bytes."""
        return dict(self._backend.metrics)

    def _open(self, write=False):
        if self._closed:
            raise ValueError("Database is closed")
        if write:
            self._backend._require_write()

    def close(self):
        self._closed = True
        self._name_catalog = None

    def __exit__(self, *exc):
        self.close()

    def _head(self, name, *, missing_ok=False):
        prefix = signal_prefix(name)
        try:
            head = unpack(self._backend.read(prefix + "/HEAD.json"))
        except FileNotFoundError:
            if missing_ok:
                return None, None
            raise SignalNotFoundError(name) from None
        manifest = read_ref(self._backend, head["manifest"])
        if (
            manifest["signal_name"] != name
            or manifest["signal_id"] != signal_id(name)
            or manifest["database_id"] != self._config["database_id"]
            or manifest["commit_id"] != head["commit_id"]
        ):
            raise CorruptionError("Signal identity disagrees with HEAD/database")
        return manifest, head["manifest"]

    def _index(self, manifest):
        return PageIndex(
            self._backend,
            signal_prefix(manifest["signal_name"]),
            manifest["time_kind"],
            manifest["root"],
        )

    def _scan_signal_names(self):
        """Authoritative, expensive enumeration for rebuilds and verification."""
        names = []
        for path in self._backend.path("signals").glob("*/*/HEAD.json"):
            head = unpack(path.read_bytes())
            manifest = read_ref(self._backend, head["manifest"])
            name = manifest["signal_name"]
            if (
                path.parent != self._backend.path(signal_prefix(name))
                or manifest["database_id"] != self._config["database_id"]
                or manifest["signal_id"] != signal_id(name)
                or manifest["commit_id"] != head["commit_id"]
            ):
                raise CorruptionError("Catalog signal identity mismatch")
            names.append(name)
        return sorted(names)

    def rebuild_catalog(self):
        """Rebuild name discovery from signal HEADs; return the signal count.

        Reads metadata only. Also clears interrupted creation intents and retired
        name shards. Other catalog-aware writers may remain active. Required after
        writes with older versions that do not maintain the name catalog.
        """
        self._open(write=True)
        return self._name_catalog.rebuild()

    def search(self, regexp=""):
        """Search sorted names, loading the compact catalog on first use.

        Each call checks for other workers' creations. Existing measurement writes
        do not invalidate the name cache. Read-only older stores without a catalog
        use a HEAD scan; a writable first search builds the catalog once.
        """
        self._open()
        try:
            pattern = re.compile(regexp)
        except (re.error, TypeError) as exc:
            raise ValueError("Invalid signal regular expression") from exc
        names = self._name_catalog.names()
        if regexp == "":
            return list(names)
        return [name for name in names if pattern.search(name)]

    def _select(self, selector):
        if selector is None:
            return self.search()
        if isinstance(selector, str):
            return self.search(selector)
        names = list(dict.fromkeys(selector))
        for name in names:
            signal_id(name)
            self._head(name)
        return names

    def info(self, name):
        self._open()
        manifest, _ = self._head(name)
        index = self._index(manifest)
        summaries = {}
        for d in index.pages():
            key = canonical(d["schema"])
            s = summaries.setdefault(
                key, dict(d["schema"], count=0, min=None, max=None, nan_count=0)
            )
            s["count"] += d["count"]
            s["nan_count"] += d["statistics"]["nan_count"]
            for what, op in (("min", min), ("max", max)):
                value = d["statistics"][what]
                value = float(value) if isinstance(value, str) else value
                if value is not None:
                    s[what] = value if s[what] is None else op(s[what], value)
        root = manifest["root"]
        return SignalInfo(
            name,
            manifest["time_kind"],
            manifest["generation"],
            manifest["max_page_size"],
            root["count"],
            decode_time(root["first"], manifest["time_kind"]),
            decode_time(root["last"], manifest["time_kind"]),
            root["page_count"],
            root["payload_bytes"],
            root["stored_bytes"],
            list(summaries.values()),
        )

    def _query(self, manifest, t1, t2, max_count, skip, timezone):
        skip = integer(skip, "skip")
        if max_count is not None:
            max_count = integer(max_count, "max_count")
        zone = self.timezone if timezone is None else timezone
        resolve_timezone(zone)
        low, high = bound(t1, manifest["time_kind"], zone), bound(
            t2, manifest["time_kind"], zone
        )
        if low is not None and high is not None and low > high:
            raise ValueError("t1 must not be greater than t2")
        return low, high, max_count, skip + 1

    def _read_data(self, descriptor, manifest, *, full=False):
        page = read_page(
            self._backend.path(descriptor["key"]),
            full=full,
            expected=descriptor["sha256"],
        )
        h = page.header
        for key in (
            "page_id",
            "count",
            "first",
            "last",
            "schema",
            "payload_bytes",
            "statistics",
        ):
            if h[key] != descriptor[key]:
                raise CorruptionError(f"Page/catalog disagreement: {key}")
        if (
            h["database_id"] != self._config["database_id"]
            or h["signal_name"] != manifest["signal_name"]
            or h["signal_id"] != manifest["signal_id"]
            or h["time_kind"] != manifest["time_kind"]
            or h["file_length"] != descriptor["size"]
        ):
            raise CorruptionError("Page identity or size disagrees with manifest")
        return page.batch()

    @staticmethod
    def _positions(batch, low, high):
        start = (
            0
            if low is None
            else int(np.searchsorted(batch.timestamps, low, side="left"))
        )
        end = (
            len(batch)
            if high is None
            else int(np.searchsorted(batch.timestamps, high, side="right"))
        )
        return start, end

    def iter_signal(
        self, name, t1=None, t2=None, *, max_count=None, skip=0, timezone=None
    ):
        self._open()
        return _BatchStream(self, name, t1, t2, max_count, skip, timezone)

    def _iterate(self, manifest, query):
        low, high, maximum, step = query
        if maximum == 0:
            return
        seen, emitted = 0, 0
        for descriptor in self._index(manifest).pages(low, high):
            covered = (
                low is None
                or low <= decode_time(descriptor["first"], manifest["time_kind"])
            ) and (
                high is None
                or high >= decode_time(descriptor["last"], manifest["time_kind"])
            )
            if covered and -seen % step >= descriptor["count"]:
                seen += descriptor["count"]
                continue
            batch = self._read_data(descriptor, manifest)
            start, end = self._positions(batch, low, high)
            length = end - start
            first = start + (-seen % step)
            seen += length
            if first >= end:
                continue
            if maximum is not None:
                end = min(end, first + (maximum - emitted) * step)
            selected = batch.take(slice(first, end, step))
            emitted += len(selected)
            yield selected
            if maximum is not None and emitted >= maximum:
                break

    def get_signal(
        self, name, t1=None, t2=None, *, max_count=None, skip=0, timezone=None
    ):
        self._open()
        manifest, _ = self._head(name)
        query = self._query(manifest, t1, t2, max_count, skip, timezone)
        count = self._count_snapshot(manifest, query)
        times = np.empty(count, dtype=KINDS[manifest["time_kind"]].newbyteorder("="))
        if not count:
            schemas = {
                canonical(d["schema"]): d["schema"]
                for d in self._index(manifest).pages()
            }
            if (
                len(schemas) == 1
                and (schema := next(iter(schemas.values())))["layout"] == "dense"
            ):
                values = np.empty(
                    (0, *schema["shape"]),
                    dtype=np.dtype(schema["dtype"]).newbyteorder("="),
                )
            else:
                values = np.empty(0, dtype=object)
            return times, values

        def own_record(record):
            owned = record.astype(record.dtype.newbyteorder("="), copy=True)
            return (
                owned[()] if owned.ndim == 0 and owned.dtype.kind not in "SU" else owned
            )

        values, dense_schema, offset = None, None, 0
        for batch in self._iterate(manifest, query):
            key = schema_key(batch)
            if values is None:
                if isinstance(batch.values, RaggedArray):
                    values = np.empty(count, dtype=object)
                else:
                    dense_schema = key
                    values = np.empty(
                        (count, *batch.values.shape[1:]),
                        dtype=batch.values.dtype.newbyteorder("="),
                    )
            elif values.dtype != object and key != dense_schema:
                mixed = np.empty(count, dtype=object)
                for i in range(offset):
                    mixed[i] = own_record(values[i : i + 1].reshape(values.shape[1:]))
                values = mixed
            times[offset : offset + len(batch)] = batch.timestamps
            if values.dtype != object:
                values[offset : offset + len(batch)] = batch.values
            else:
                for i in range(len(batch)):
                    record = (
                        batch.values.record(i)
                        if isinstance(batch.values, RaggedArray)
                        else batch.values[i : i + 1].reshape(batch.values.shape[1:])
                    )
                    values[offset + i] = own_record(record)
            offset += len(batch)
        if offset != count:
            raise CorruptionError(
                "Page counts disagree with the captured index snapshot"
            )
        return times, values

    def get(
        self, selector=None, t1=None, t2=None, *, max_count=None, skip=0, timezone=None
    ):
        self._open()
        return {
            name: self.get_signal(
                name, t1, t2, max_count=max_count, skip=skip, timezone=timezone
            )
            for name in self._select(selector)
        }

    def count_signal(
        self, name, t1=None, t2=None, *, max_count=None, skip=0, timezone=None
    ):
        self._open()
        manifest, _ = self._head(name)
        return self._count_snapshot(
            manifest, self._query(manifest, t1, t2, max_count, skip, timezone)
        )

    def _count_snapshot(self, manifest, query):
        low, high, maximum, step = query
        if maximum == 0:
            return 0

        def boundary_count(descriptor):
            start, end = self._positions(
                self._read_data(descriptor, manifest), low, high
            )
            return end - start

        count = self._index(manifest).count(low, high, boundary_count)
        count = (count + step - 1) // step
        return min(count, maximum) if maximum is not None else count

    def count(
        self, selector=None, t1=None, t2=None, *, max_count=None, skip=0, timezone=None
    ):
        self._open()
        return {
            name: self.count_signal(
                name, t1, t2, max_count=max_count, skip=skip, timezone=timezone
            )
            for name in self._select(selector)
        }

    def _recovery_record(self, manifest):
        record = {
            "scope": "signal",
            "record_kind": manifest["recovery_kind"],
            "config": self._config,
            "database_id": manifest["database_id"],
            "signal_name": manifest["signal_name"],
            "signal_id": manifest["signal_id"],
            "time_kind": manifest["time_kind"],
            "max_page_size": manifest["max_page_size"],
            "generation": manifest["generation"],
            "commit_id": manifest["commit_id"],
            "parent_commit_id": manifest["parent_commit_id"],
            "parent_recovery_sha256": manifest["parent_recovery_sha256"],
            "next_ordinal": manifest["next_ordinal"],
            "totals": {
                k: manifest["root"][k]
                for k in ("count", "page_count", "stored_bytes", "payload_bytes")
            },
        }
        if record["record_kind"] == "checkpoint":
            record["pages"] = list(self._index(manifest).pages())
        else:
            record["added"] = manifest["added"]
            record["removed"] = manifest["removed"]
        return record

    def _publish_recovery(self, manifest, record=None):
        plan = None
        prefix = (
            signal_prefix(manifest["signal_name"])
            + "/pages/recovery/"
            + manifest["commit_id"]
        )
        for copy in (0, 1):
            key = f"{prefix}.{copy}.pg"
            try:
                stored = read_page(self._backend.path(key), full=True).recovery()
                intact = digest(canonical(stored)) == manifest["recovery_sha256"]
            except (OSError, CorruptionError):
                intact = False
            if not intact:
                if plan is None:
                    record = record or self._recovery_record(manifest)
                    if digest(canonical(record)) != manifest["recovery_sha256"]:
                        raise CorruptionError(
                            "Cannot reproduce the committed recovery record"
                        )
                    plan = recovery_plan(record).to_bytes()
                self._backend.publish(key, [plan], replace=True)
        if len(self._verified_recovery) >= 4096:
            self._verified_recovery.clear()
        self._verified_recovery.add(manifest["commit_id"])

    def _ensure_recovery(self, manifest):
        chain = []
        while manifest and manifest["commit_id"] not in self._verified_recovery:
            chain.append(manifest)
            if manifest["recovery_kind"] == "checkpoint":
                break
            parent = read_ref(self._backend, manifest["parent_ref"])
            if (
                parent["commit_id"] != manifest["parent_commit_id"]
                or parent["recovery_sha256"] != manifest["parent_recovery_sha256"]
            ):
                raise CorruptionError("Broken recovery ancestry")
            manifest = parent
        for item in reversed(chain):
            self._publish_recovery(item)

    def repair_recovery(self, name):
        """Finish recovery redundancy for a visible commit without replaying data."""
        self._open(write=True)
        with self._backend.lock(signal_prefix(name) + "/LOCK"):
            self._verified_recovery.clear()
            manifest, _ = self._head(name)
            self._ensure_recovery(manifest)

    def _commit(
        self,
        name,
        kind,
        index,
        parent,
        parent_ref,
        added,
        removed,
        next_ordinal,
        maximum,
        *,
        checkpoint=False,
        inserted=0,
        replaced=0,
    ):
        root = index.flush()
        commit_id = uuid4().hex
        manifest = {
            "version": 1,
            "database_id": self._config["database_id"],
            "signal_name": name,
            "signal_id": signal_id(name),
            "time_kind": kind,
            "commit_id": commit_id,
            "generation": 1 if parent is None else parent["generation"] + 1,
            "parent_commit_id": None if parent is None else parent["commit_id"],
            "parent_ref": parent_ref,
            "parent_recovery_sha256": (
                None if parent is None else parent["recovery_sha256"]
            ),
            "root": root,
            "max_page_size": maximum,
            "next_ordinal": next_ordinal,
            "added": added,
            "removed": [d["page_id"] for d in removed],
            "recovery_kind": "checkpoint" if checkpoint or parent is None else "delta",
        }
        record = self._recovery_record(manifest)
        manifest["recovery_sha256"] = digest(canonical(record))
        ref = write_immutable(
            self._backend, signal_prefix(name) + "/manifests", manifest
        )
        head_key = signal_prefix(name) + "/HEAD.json"
        head = {"commit_id": commit_id, "manifest": ref}
        registration = (
            self._name_catalog.creating(name, commit_id)
            if parent is None
            else nullcontext()
        )
        with registration:
            self._publish_commit(head_key, head, manifest, record)
        return WriteResult(
            manifest["generation"], inserted, replaced, root["count"], commit_id
        )

    def _publish_commit(self, head_key, head, manifest, record):
        name, commit_id = manifest["signal_name"], manifest["commit_id"]
        try:
            self._backend.publish(head_key, [envelope(head)], replace=True)
        except Exception as exc:
            # A directory fsync can fail after rename. Never report that as rollback.
            try:
                visible = unpack(self._backend.read(head_key)) == head
            except FileNotFoundError:
                visible = False
            except (OSError, CorruptionError) as reconcile_error:
                raise CommitOutcomeUnknownError(name, commit_id) from reconcile_error
            if visible:
                raise RecoveryIncompleteError(name, commit_id) from exc
            raise
        try:
            self._publish_recovery(manifest, record)
        except Exception as exc:
            raise RecoveryIncompleteError(name, commit_id) from exc

    def _write_pages(self, name, kind, batches, maximum, ordinal):
        prefix = signal_prefix(name)
        upload_id = uuid4().hex
        descriptors = []
        for batch in page_batches(batches, maximum):
            start = 0
            while start < len(batch):
                remaining = len(batch) - start
                # A conservative initial estimate, then exact serialized-size checking.
                average = max(1, batch.nbytes / len(batch))
                count = min(remaining, max(1, int(maximum / average)))
                page_id = uuid4().hex
                metadata = {
                    "database_id": self._config["database_id"],
                    "config": self._config,
                    "signal_name": name,
                    "signal_id": signal_id(name),
                    "time_kind": kind,
                    "page_id": page_id,
                    "ordinal": ordinal,
                    "upload_batch_id": upload_id,
                    "max_page_size": maximum,
                    "oversized": False,
                }
                while True:
                    piece = batch.take(slice(start, start + count))
                    estimate = data_plan(piece, metadata, hash_sections=False)
                    if estimate.size <= maximum or count == 1:
                        break
                    overhead = estimate.size - piece.nbytes
                    count = max(
                        1,
                        min(
                            count - 1,
                            int((maximum - overhead) / max(1, piece.nbytes / count)),
                        ),
                    )
                metadata["oversized"] = estimate.size > maximum
                plan = data_plan(piece, metadata)
                key = _page_key(prefix, ordinal, page_id)
                self._backend.publish(key, plan.chunks())
                descriptors.append(
                    {
                        "key": key,
                        "sha256": plan.sha256,
                        "page_id": page_id,
                        "ordinal": ordinal,
                        "size": plan.size,
                        **{
                            k: plan.header[k]
                            for k in (
                                "count",
                                "first",
                                "last",
                                "schema",
                                "statistics",
                                "payload_bytes",
                            )
                        },
                    }
                )
                start += count
                ordinal += 1
        return descriptors, ordinal

    def _write_locked(self, name, batches, kind, maximum=None, on_overlap="replace"):
        parent, parent_ref = self._head(name, missing_ok=True)
        if parent:
            self._ensure_recovery(parent)
            if kind != parent["time_kind"]:
                batches = [
                    Batch(
                        normalize_timestamps(
                            b.timestamps, parent["time_kind"], self.timezone
                        )[0],
                        b.values,
                    )
                    for b in batches
                ]
                kind = parent["time_kind"]
            index = self._index(parent)
            maximum = maximum or parent["max_page_size"]
            ordinal = parent["next_ordinal"]
        else:
            index = PageIndex(self._backend, signal_prefix(name), kind)
            maximum = maximum or self._config["default_max_page_size"]
            ordinal = 0
        previous_count = index.root["count"] if index.root else 0
        incoming_count = sum(len(b) for b in batches)
        # Merge the complete affected interval, including gaps between schema runs.
        # Otherwise a newly coalesced page could straddle an untouched live page.
        removed = list(
            index.pages(batches[0].timestamps[0], batches[-1].timestamps[-1])
        )
        if removed and on_overlap == "error":
            raise OverlapError(f"Incoming interval overlaps existing pages of {name!r}")
        if removed:
            old = [self._read_data(d, parent, full=True) for d in removed]
            batches = merge_batches(old + batches)
        added, ordinal = self._write_pages(name, kind, batches, maximum, ordinal)
        index.update(added, removed)
        inserted = index.root["count"] - previous_count
        return self._commit(
            name,
            kind,
            index,
            parent,
            parent_ref,
            added,
            removed,
            ordinal,
            maximum,
            inserted=inserted,
            replaced=incoming_count - inserted,
        )

    def store(self, data, *, max_page_size=None, timezone=None):
        self._open(write=True)
        if not isinstance(data, Mapping):
            raise TypeError("store requires a mapping of signal names to data")
        zone = self.timezone if timezone is None else timezone
        resolve_timezone(zone)
        if isinstance(max_page_size, Mapping):
            if set(max_page_size) - set(data):
                raise ValueError("Page-size overrides must name input signals")
            sizes = {
                name: integer(value, "max_page_size", 1)
                for name, value in max_page_size.items()
            }
        else:
            maximum = (
                None
                if max_page_size is None
                else integer(max_page_size, "max_page_size", 1)
            )
            sizes = dict.fromkeys(data, maximum)
        prepared = {}
        for name, value in data.items():
            signal_id(name)
            prepared[name] = normalize(value, timezone=zone)
        results = {}
        for name, (batches, kind) in prepared.items():
            if not batches:
                continue
            try:
                with self._backend.lock(signal_prefix(name) + "/LOCK"):
                    results[name] = self._write_locked(
                        name, batches, kind, sizes.get(name)
                    )
            except Exception as exc:
                raise StoreError(results, name, exc) from exc
        return results

    def configure_signal(self, name, *, max_page_size):
        self._open(write=True)
        maximum = integer(max_page_size, "max_page_size", 1)
        with self._backend.lock(signal_prefix(name) + "/LOCK"):
            parent, ref = self._head(name)
            self._ensure_recovery(parent)
            if maximum != parent["max_page_size"]:
                self._commit(
                    name,
                    parent["time_kind"],
                    self._index(parent),
                    parent,
                    ref,
                    [],
                    [],
                    parent["next_ordinal"],
                    maximum,
                )
        return self.info(name)

    def _checkpoint_locked(self, name):
        parent, ref = self._head(name)
        self._ensure_recovery(parent)
        if parent["recovery_kind"] == "checkpoint":
            return WriteResult(
                parent["generation"], 0, 0, parent["root"]["count"], parent["commit_id"]
            )
        return self._commit(
            name,
            parent["time_kind"],
            self._index(parent),
            parent,
            ref,
            [],
            [],
            parent["next_ordinal"],
            parent["max_page_size"],
            checkpoint=True,
        )

    def checkpoint(self, name):
        self._open(write=True)
        with self._backend.lock(signal_prefix(name) + "/LOCK"):
            return self._checkpoint_locked(name)

    def ingest(
        self,
        name,
        batches,
        *,
        max_page_size=None,
        on_overlap="error",
        commit_bytes=64 * 1024**2,
        timezone=None,
    ):
        self._open(write=True)
        signal_id(name)
        maximum = (
            None
            if max_page_size is None
            else integer(max_page_size, "max_page_size", 1)
        )
        commit_bytes = integer(commit_bytes, "commit_bytes", 1)
        if on_overlap not in {"error", "replace"}:
            raise ValueError("on_overlap must be 'error' or 'replace'")
        zone = self.timezone if timezone is None else timezone
        resolve_timezone(zone)
        progress = IngestResult()
        try:
            with self._backend.lock(signal_prefix(name) + "/LOCK"):
                parent, _ = self._head(name, missing_ok=True)
                kind = parent["time_kind"] if parent else None
                target = max(
                    commit_bytes,
                    maximum
                    or (
                        parent["max_page_size"]
                        if parent
                        else self._config["default_max_page_size"]
                    ),
                )
                pending, pending_bytes = [], 0
                cursor = (0, 0)

                def commit():
                    nonlocal pending, pending_bytes
                    ordered = all(
                        a.timestamps[-1] < b.timestamps[0]
                        for a, b in zip(pending, pending[1:])
                    )
                    result = self._write_locked(
                        name,
                        pending if ordered else merge_batches(pending),
                        kind,
                        maximum,
                        on_overlap,
                    )
                    progress.commits += 1
                    progress.inserted += result.inserted
                    progress.replaced += result.replaced
                    progress.total = result.total
                    progress.generation = result.generation
                    progress.commit_id = result.commit_id
                    progress.batch_index, progress.record_offset = cursor
                    pending, pending_bytes = [], 0

                for batch_index, raw in enumerate(batches):
                    normalized, current_kind = normalize(raw, kind, zone)
                    if not normalized:
                        continue
                    kind = current_kind
                    offset = 0
                    for batch in normalized:
                        start = 0
                        while start < len(batch):
                            average = max(1, batch.nbytes / len(batch))
                            count = min(
                                len(batch) - start,
                                max(1, int((target - pending_bytes) / average)),
                            )
                            piece = batch.take(slice(start, start + count))
                            pending.append(piece)
                            pending_bytes += piece.nbytes
                            start += count
                            offset += count
                            cursor = (batch_index, offset)
                            if pending_bytes >= target:
                                commit()
                if pending:
                    commit()
                if progress.commits:
                    checkpoint = self._checkpoint_locked(name)
                    progress.generation, progress.commit_id = (
                        checkpoint.generation,
                        checkpoint.commit_id,
                    )
        except Exception as exc:
            raise IngestError(replace(progress), exc) from exc
        return progress

    def check(self, *, full=False):
        from .maintenance import check

        self._open()
        return check(self, full=full)


class _BatchStream(AbstractContextManager):
    def __init__(self, db, name, t1, t2, maximum, skip, timezone):
        self.db, self.name = db, name
        self.args = t1, t2, maximum, skip, timezone
        self.iterator = None
        self.closed = False

    def __enter__(self):
        if self.closed:
            raise ValueError("Stream is closed")
        if self.iterator is None:
            self.db._open()
            manifest, _ = self.db._head(self.name)
            self.iterator = self.db._iterate(
                manifest, self.db._query(manifest, *self.args)
            )
        return self

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        if self.iterator is None:
            self.__enter__()
        return next(self.iterator)

    def close(self):
        if self.iterator is not None:
            self.iterator.close()
        self.closed = True

    def __exit__(self, *exc):
        self.close()
