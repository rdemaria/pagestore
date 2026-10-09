"""The PageStore database API and filesystem commit protocol."""

from collections import OrderedDict
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
import re
from uuid import uuid4

import numpy as np

from .backends import open_backend
from .catalog import (
    STORE_FORMAT_VERSION,
    canonical,
    digest,
    envelope,
    ordinal_directory,
    ordinal_path,
    read_ref,
    signal_id,
    signal_prefix,
    signal_location,
    signal_directories,
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
    StoreInfo,
    SignalInfo,
    WriteResult,
    integer,
    merge_batches,
    normalize,
    page_batches,
    schema_key,
)
from .page_format import data_plan, recovery_plan
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
    return f"{prefix}/pages/{ordinal_path(ordinal)}-{page_id}.pg"


class DB(AbstractContextManager):
    """Open an immutable-page time-series store, preferably in a ``with`` block.

    Parameters
    ----------
    url : str or path-like
        A filesystem path, file URL, or native root:// / roots:// store URL.
    mode : {"r", "a", "x"}, default "r"
        Read an existing store, open/create a writable store, or exclusively
        create a new store. Opening is read-only by default and never initializes
        missing metadata; writes require explicit mode="a" or mode="x".
    default_max_page_size : int or None
        Serialized page-size default for new signals, in bytes. None uses 8 MiB
        for a new store or its persisted setting when reopening. An explicit
        value must match an existing store's default. An indivisible oversized
        record is stored on its own page.
    lock_timeout : float, default 30
        Maximum seconds spent waiting for a writer lock; locks are never stolen.
    timezone : str, default "utc"
        Timezone for naive datetime strings: "utc", "cern", "local", or an
        IANA name. Explicit datetime offsets take precedence; numeric axes retain
        their own units. Datetime output is UTC numpy.datetime64[ns].
    xrootd_url : str or None
        Authoritative XRootD store URL for a mounted-path alias. When supplied,
        all I/O and locks use this URL. EOS/SSHFS mounts require it for writes.
    io_timeout : int, default 30
        Timeout in seconds for an individual native XRootD request.
    visibility_timeout : float, default 30
        Retry window in seconds for incomplete remote or weak-mounted files.

    Notes
    -----
    Instances are thread-confined. Workers open separate DBs and use the ordinary
    methods; coordination is automatic. Different signals progress independently,
    and same-signal writes serialize through the backend. Writes commit per signal;
    multi-signal operations provide no transaction spanning all signals. Closing does not
    delete the store. See individual methods for partial-commit exceptions.
    """

    def __init__(
        self,
        url,
        *,
        mode="r",
        default_max_page_size=None,
        lock_timeout=30.0,
        timezone="utc",
        xrootd_url=None,
        io_timeout=30,
        visibility_timeout=30.0,
    ):
        if mode not in {"r", "a", "x"}:
            raise ValueError("mode must be 'r', 'a', or 'x'")
        resolve_timezone(timezone)
        self.timezone = timezone
        self._mode = mode
        self._closed = False
        self._backend = open_backend(
            url,
            writable=mode != "r",
            lock_timeout=lock_timeout,
            xrootd_url=xrootd_url,
            io_timeout=io_timeout,
            visibility_timeout=visibility_timeout,
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
            with self._backend.lock(".INIT", family=self._backend.init_coordination):
                if self._backend.exists("store.json"):
                    if mode == "x":
                        raise FileExistsError(self._backend.root)
                    self._config = unpack(self._backend.read("store.json"))
                else:
                    if any(name != ".INIT.LOCK" for name in self._backend.listdir()):
                        raise CorruptionError(
                            "Directory is not an empty/new database; recover missing store.json instead of recreating it"
                        )
                    self._config = {
                        "format_version": STORE_FORMAT_VERSION,
                        "database_id": uuid4().hex,
                        "config_id": uuid4().hex,
                        "default_max_page_size": requested or DEFAULT_MAX_PAGE_SIZE,
                        "coordination": self._backend.info.coordination,
                        "lock_namespace": "per-signal-v1",
                    }
                    self._backend.publish("store.json", [envelope(self._config)])
        self._validate_config(requested)
        self._backend.bind_coordination(self._config["coordination"])
        self._known_identities = OrderedDict()
        self._name_catalog = NameCatalog(self)
        if mode != "r":
            # Existing workers need no database-wide writer lock. Root recovery
            # repair is idempotent: competing repairs publish identical bytes.
            self._ensure_root_recovery()

    def _validate_config(self, requested):
        if self._config.get("format_version") != STORE_FORMAT_VERSION:
            raise CorruptionError(
                f"Unsupported database format {self._config.get('format_version')!r}; "
                f"expected {STORE_FORMAT_VERSION}. Experimental layouts are not "
                "migrated automatically."
            )
        if self._config.get("lock_namespace") != "per-signal-v1":
            raise CorruptionError("Unsupported coordination namespace")
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
                intact = self._backend.read_page(key, full=True).recovery() == record
            except (OSError, CorruptionError):
                intact = False
            if not intact:
                if raw is None:
                    raw = recovery_plan(record).to_bytes()
                self._backend.publish(key, [raw], replace=True)

    @property
    def backend_info(self):
        """Return the selected storage profile, capabilities, and coordination family."""
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

    def refresh(self):
        """Clear cached metadata so subsequent operations reload it from storage.

        Drops cached names, shards, signal locations/identities, and recovery
        verification results. Returns None. Reloading is lazy: this call performs
        no storage I/O or writes and works in read-only mode. Normal reads already
        reread signal HEADs; searches already check for catalog changes.

        Existing arrays and entered iterators keep their captured data/snapshots.
        This does not flush external filesystem/mount caches, wait for writers,
        or provide a database-wide snapshot. Native XRootD reads continue to use
        REFRESH opens; weak mounts can still return valid older metadata. Writer
        locks and any ambiguous-commit protection remain intact. A closed DB
        raises ValueError; refresh does not reopen it.
        """
        self._open()
        self._name_catalog = NameCatalog(self)
        self._known_identities.clear()
        self._verified_recovery.clear()

    def close(self):
        """Release this instance's catalog cache and reject subsequent operations.

        Safe to call repeatedly. Previously returned owned arrays and captured
        immutable-page streams remain valid; closing does not erase stored data.
        """
        self._closed = True
        self._name_catalog = None
        self._known_identities.clear()

    def __exit__(self, *exc):
        self.close()

    def __repr__(self):
        """Describe the location, mode, profile, and state without storage I/O."""
        return (
            f"{type(self).__name__}({str(self._backend.root)!r}, "
            f"mode={self._mode!r}, profile={self._backend.info.profile!r}, "
            f"closed={self._closed!r})"
        )

    def _signal_prefix(self, name, *, create=False):
        ordinal = (
            self._name_catalog.reserve(name)
            if create
            else self._name_catalog.location(name)
        )
        if ordinal is None:
            raise SignalNotFoundError(name)
        return signal_prefix(name, ordinal)

    def _identity(self, name, ordinal):
        return {
            "database_id": self._config["database_id"],
            "signal_name": name,
            "signal_id": signal_id(name),
            "signal_ordinal": ordinal,
        }

    def _verify_signal_identity(self, name, ordinal):
        key = signal_prefix(name, ordinal) + "/SIGNAL.json"
        if unpack(self._backend.read(key)) != self._identity(name, ordinal):
            raise CorruptionError("Signal allocation identity mismatch")

    def _ensure_signal_identity(self, name, ordinal):
        try:
            self._verify_signal_identity(name, ordinal)
        except FileNotFoundError:
            self._backend.publish(
                signal_prefix(name, ordinal) + "/SIGNAL.json",
                [envelope(self._identity(name, ordinal))],
            )

    def _remember_identity(self, name):
        self._known_identities[name] = None
        self._known_identities.move_to_end(name)
        if len(self._known_identities) > 4096:
            self._known_identities.popitem(last=False)

    def _head(self, name, *, missing_ok=False, include_deleted=False, prefix=None):
        try:
            prefix = prefix or self._signal_prefix(name)
            head = unpack(self._backend.read(prefix + "/HEAD.json"))
        except (FileNotFoundError, SignalNotFoundError):
            if missing_ok:
                return None, None
            raise SignalNotFoundError(name) from None
        manifest = read_ref(self._backend, head["manifest"])
        if (
            manifest["signal_name"] != name
            or manifest["signal_id"] != signal_id(name)
            or manifest["database_id"] != self._config["database_id"]
            or manifest["commit_id"] != head["commit_id"]
            or prefix != signal_prefix(name, manifest["signal_ordinal"])
        ):
            raise CorruptionError("Signal identity disagrees with HEAD/database")
        if manifest["root"] is None and not include_deleted:
            if missing_ok:
                return None, None
            raise SignalNotFoundError(name)
        return manifest, head["manifest"]

    def _index(self, manifest):
        return PageIndex(
            self._backend,
            signal_prefix(manifest["signal_name"], manifest["signal_ordinal"]),
            manifest["time_kind"],
            manifest["root"],
            next_ordinal=integer(
                manifest["next_index_ordinal"], "next_index_ordinal", 0
            ),
        )

    def _new_index(self, name, kind):
        return PageIndex(
            self._backend,
            self._signal_prefix(name),
            kind,
        )

    def _recovery_key(self, manifest, copy):
        prefix = (
            signal_prefix(manifest["signal_name"], manifest["signal_ordinal"])
            + "/pages/recovery"
        )
        directory = ordinal_directory(manifest["generation"] - 1)
        if directory:
            prefix += "/" + directory
        return f"{prefix}/{manifest['commit_id']}.{copy}.pg"

    def _scan_signal_entries(self):
        """Enumerate stable reservations and committed HEADs at arbitrary depth."""
        for prefix in signal_directories(self._backend):
            ordinal, sid = signal_location(prefix)
            try:
                identity = unpack(self._backend.read(prefix + "/SIGNAL.json"))
            except FileNotFoundError:
                try:
                    head = unpack(self._backend.read(prefix + "/HEAD.json"))
                except FileNotFoundError:
                    continue  # No published identity or signal at this location.
                manifest = read_ref(self._backend, head["manifest"])
                name = manifest["signal_name"]
            else:
                name = identity["signal_name"]
                if identity != self._identity(name, ordinal):
                    raise CorruptionError("Signal allocation identity mismatch")
            if sid != signal_id(name) or prefix != signal_prefix(name, ordinal):
                raise CorruptionError("Signal directory disagrees with identity")
            manifest, _ = self._head(
                name, prefix=prefix, missing_ok=True, include_deleted=True
            )
            root = None if manifest is None else manifest["root"]
            yield {
                "name": name,
                "ordinal": ordinal,
                "committed": manifest is not None,
                "live": root is not None,
                "record_count": 0 if root is None else root["count"],
            }

    def _scan_signal_names(self, *, include_deleted=False):
        """Authoritative, expensive enumeration for rebuilds and verification."""
        return sorted(
            e["name"]
            for e in self._scan_signal_entries()
            if e["committed"] and (include_deleted or e["live"])
        )

    def rebuild_catalog(self):
        """Rebuild locations and name discovery; return the live signal count.

        Reads identities, HEADs, and manifests only. Preserves reserved/deleted
        locations, repairs missing or corrupt derived catalogs, and clears name
        intents and retired shards. Other writers may remain active; measurement
        pages are unaffected.
        """
        self._open(write=True)
        return self._name_catalog.rebuild()

    def search(self, regexp=""):
        """Search sorted names, loading the compact catalog on first use.

        Uses re.search; an empty pattern lists all live names. Invalid patterns
        raise ValueError. Each call checks for other workers' creations/deletions.
        Measurement updates do not invalidate the cache. If the derived catalog
        is missing, read-only searches scan signal HEADs; a writable first search
        rebuilds the catalog.
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

    def info(self):
        """Return StoreInfo with total size, live signal count, and record count.

        Scans the complete store namespace, including metadata, recovery copies,
        retired pages, locks, and pending files. ``size_basis`` distinguishes
        allocated disk bytes from logical file lengths; see StoreInfo. Reads
        current signal manifests for counts, never measurement payloads or page
        indexes. Deleted signals and uncommitted reservations have zero counts.
        Enumeration is independent of the derived name catalog and repairs no
        metadata, even when the name catalog is missing or damaged.

        This operation is proportional to the store's files/signals and can be
        slow on large or remote stores. It takes no writer locks and gives no
        atomic snapshot across signals/files; quiesce writers for stable totals.
        """
        self._open()
        signal_count = record_count = 0
        for entry in self._scan_signal_entries():
            if entry["live"]:
                signal_count += 1
                record_count += entry["record_count"]
        size_bytes, size_basis = self._backend.disk_usage()
        return StoreInfo(size_bytes, signal_count, record_count, size_basis)

    def info_signal(self, name):
        """Return SignalInfo for one exact name, using page-index metadata.

        Includes timestamp kind, bounds, count, generation, page-size policy,
        active byte totals, and per-schema statistics. Does not read measurement
        payloads. Missing or deleted names raise SignalNotFoundError.
        """
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
        page = self._backend.read_page(
            descriptor["key"],
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
        """Return a context-managed iterator of Batch objects for one exact name.

        Bounds are inclusive; None is unbounded. Select every ``skip + 1`` record
        across page/schema boundaries, then limit output to ``max_count`` records.
        Datetime parsing uses ``timezone`` or the DB default. The snapshot is
        captured on entering the stream or requesting its first batch; subsequent
        commits, including deletion, do not change that snapshot.

        Batches retain stored schemas and use RaggedArray for ragged records.
        Arrays may share read-only memory mappings. Use a ``with`` block or call
        the stream's close() when stopping early. Missing/deleted names raise
        SignalNotFoundError when the snapshot is captured.
        """
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
        """Return owned ``(timestamps, values)`` arrays for one exact signal name.

        Bounds are inclusive; None is unbounded. After filtering, select every
        ``skip + 1`` record and then apply ``max_count``. A zero maximum or an empty
        matching interval returns empty arrays. Reversed bounds raise ValueError;
        missing/deleted names raise SignalNotFoundError.

        ``timezone`` overrides the DB setting for naive datetime bounds. Numeric
        axes remain numeric; datetime output is UTC datetime64[ns]. Arrays own
        writable native-endian memory. Compatible dense records form one NumPy
        array; mixed dtypes/shapes or ragged records use a one-dimensional object
        array containing independent NumPy records. Reads capture one signal's
        committed snapshot, with no lock or transaction across other signals.
        """
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
        """Return ``{name: (timestamps, values)}`` for selected signals.

        None selects all names; a string is a regular expression used with
        re.search; an iterable supplies exact names, deduplicated in input order.
        Regex results are sorted. Use ``[name]`` for a literal name containing
        regex characters. Unknown exact names raise SignalNotFoundError; a regex
        with no matches returns an empty dict. Bounds, sampling, ownership, and
        timezone follow get_signal(), independently for each selected signal.
        """
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
        """Count selected records in one exact signal's committed snapshot.

        Uses the same inclusive bounds, timezone, ``skip``, and ``max_count`` as
        get_signal(), without materializing its result. Fully covered index
        subtrees use aggregate counts; partial boundary pages may be read.
        Missing/deleted names raise SignalNotFoundError.
        """
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
        """Return ``{name: count}`` using get() selectors and count_signal() bounds.

        Sampling and count limits apply separately to each signal. Each count
        uses its own committed snapshot; this is not a multi-signal transaction.
        """
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
            "signal_ordinal": manifest["signal_ordinal"],
            "time_kind": manifest["time_kind"],
            "max_page_size": manifest["max_page_size"],
            "generation": manifest["generation"],
            "commit_id": manifest["commit_id"],
            "parent_commit_id": manifest["parent_commit_id"],
            "parent_recovery_sha256": manifest["parent_recovery_sha256"],
            "next_ordinal": manifest["next_ordinal"],
            "totals": {
                k: manifest["root"][k] if manifest["root"] is not None else 0
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
        for copy in (0, 1):
            key = self._recovery_key(manifest, copy)
            try:
                stored = self._backend.read_page(key, full=True).recovery()
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
        """Repair paired recovery records for an exact name without replaying data.

        Includes a deleted signal's retained tombstone and required predecessors.
        Use after RecoveryIncompleteError; a name with no retained HEAD raises
        SignalNotFoundError. Catalog repair is separate: call rebuild_catalog().
        """
        self._open(write=True)
        with self._backend.lock(self._signal_prefix(name) + "/LOCK"):
            self._verified_recovery.clear()
            manifest, _ = self._head(name, include_deleted=True)
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
        reactivating = parent_ref is not None and parent["root"] is None
        checkpoint = checkpoint or root is None or parent is None or reactivating
        manifest = {
            "version": 1,
            "database_id": self._config["database_id"],
            "signal_name": name,
            "signal_id": signal_id(name),
            "signal_ordinal": signal_location(index.prefix)[0],
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
            "next_index_ordinal": index.next_ordinal,
            "added": added,
            "removed": [d["page_id"] for d in removed],
            "recovery_kind": "checkpoint" if checkpoint else "delta",
        }
        record = self._recovery_record(manifest)
        manifest["recovery_sha256"] = digest(canonical(record))
        ref = write_immutable(
            self._backend,
            index.prefix + "/manifests",
            manifest,
            ordinal=manifest["generation"] - 1,
        )
        head_key = index.prefix + "/HEAD.json"
        head = {"commit_id": commit_id, "manifest": ref}
        registration = nullcontext()
        if root is not None and (parent is None or reactivating):
            registration = self._name_catalog.creating(name, commit_id)
        elif root is None and parent_ref is not None and parent["root"] is not None:
            registration = self._name_catalog.removing(name, commit_id)
        with registration:
            self._publish_commit(head_key, head, manifest, record)
        return WriteResult(
            manifest["generation"],
            inserted,
            replaced,
            root["count"] if root is not None else 0,
            commit_id,
        )

    def _publish_commit(self, head_key, head, manifest, record):
        name, commit_id = manifest["signal_name"], manifest["commit_id"]
        try:
            self._backend.publish(head_key, [envelope(head)], replace=True)
        except CommitOutcomeUnknownError as exc:
            # An old/missing HEAD cannot disprove a still-running remote rename.
            raise CommitOutcomeUnknownError(name, commit_id) from exc
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
        except CommitOutcomeUnknownError:
            raise
        except Exception as exc:
            raise RecoveryIncompleteError(name, commit_id) from exc

    def _write_pages(self, name, kind, batches, maximum, ordinal):
        prefix = self._signal_prefix(name)
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
        parent, parent_ref = self._head(name, missing_ok=True, include_deleted=True)
        if parent:
            self._ensure_recovery(parent)
        if parent and parent["root"] is not None:
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
            index = self._index(parent) if parent else self._new_index(name, kind)
            index.kind = kind
            maximum = maximum or self._config["default_max_page_size"]
            ordinal = parent["next_ordinal"] if parent else 0
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
        """Upsert a mapping of exact signal names to measurements.

        Values may be ``(timestamps, values)``, a Batch, or a sequence of Batches.
        Records are sorted by timestamp; the last incoming duplicate wins, and
        incoming records replace existing records at equal timestamps. Empty
        input for a signal is a no-op. A deleted name is recreated as a new
        logical signal while preserving its commit history.

        Concurrent workers call this same method through independent DB instances.
        Writer coordination is automatic: different signals progress independently,
        and same-signal upserts serialize without losing disjoint records. There is
        no caller-held lock, worker registration, or parallel mode to configure.

        ``max_page_size`` is a positive byte limit or a mapping of input names to
        limits. None preserves existing settings and uses the DB default for new
        signals. ``timezone`` overrides naive datetime parsing for this call.

        Return ``{name: WriteResult}`` for nonempty inputs. Input normalization
        occurs before writes; each signal then commits independently. StoreError
        carries acknowledged ``results``, ``failed_name``, and the underlying
        ``cause``. Inspect that cause for committed or unknown-outcome errors
        before retrying. This operation does not reclaim retired page files.
        """
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
                with self._backend.lock(
                    self._signal_prefix(name, create=True) + "/LOCK"
                ):
                    results[name] = self._write_locked(
                        name, batches, kind, sizes.get(name)
                    )
            except Exception as exc:
                raise StoreError(results, name, exc) from exc
        return results

    def delete_signal(self, name, t1=None, t2=None, *, timezone=None):
        """Atomically remove records in the inclusive interval [t1, t2].

        ``name`` is exact. Either bound may be None (unbounded); omitting both
        removes all records. Numeric bounds retain the signal's measurement-axis
        semantics. Datetime strings use ``timezone`` or the DB's timezone (UTC by
        default), with the same parsing rules as get_signal(). Reversed bounds
        raise ValueError for an existing signal.

        Return the number of removed records; missing signals and intervals with
        no matching records return 0 without committing. Removing the last record
        hides the signal from search and exact reads; a later store/ingest can
        recreate it with a new timestamp kind and default page-size setting.

        Whole pages are retired without reading their payload; only partially
        covered pages are verified and rewritten. Old files remain for readers
        and recovery, so deletion does not immediately reclaim disk space. A
        durable empty checkpoint records full deletion. RecoveryIncompleteError
        and CatalogIncompleteError indicate an already committed deletion;
        CommitOutcomeUnknownError requires reconciliation before retrying.
        """
        self._open(write=True)
        signal_id(name)
        resolve_timezone(self.timezone if timezone is None else timezone)
        try:
            prefix = self._signal_prefix(name)
        except SignalNotFoundError:
            return 0
        with self._backend.lock(prefix + "/LOCK"):
            parent, ref = self._head(name, missing_ok=True)
            if parent is None:
                return 0
            low, high, _, _ = self._query(parent, t1, t2, None, 0, timezone)
            kind = parent["time_kind"]
            index = self._index(parent)

            def covered(item):
                return (low is None or low <= decode_time(item["first"], kind)) and (
                    high is None or high >= decode_time(item["last"], kind)
                )

            removed, survivors, count = [], [], 0
            if covered(index.root):
                # The empty checkpoint needs no inventory of retired data pages.
                count = index.root["count"]
                index.root = None
            else:
                for descriptor in index.pages(low, high):
                    if covered(descriptor):
                        count += descriptor["count"]
                    else:
                        batch = self._read_data(descriptor, parent, full=True)
                        start, end = self._positions(batch, low, high)
                        if start == end:
                            continue
                        count += end - start
                        if start:
                            survivors.append(batch.take(slice(0, start)))
                        if end < len(batch):
                            survivors.append(batch.take(slice(end, None)))
                    removed.append(descriptor)
                if not count:
                    return 0
            self._ensure_recovery(parent)
            added, ordinal = self._write_pages(
                name, kind, survivors, parent["max_page_size"], parent["next_ordinal"]
            )
            index.update(added, removed)
            self._commit(
                name,
                kind,
                index,
                parent,
                ref,
                added,
                removed,
                ordinal,
                parent["max_page_size"],
            )
            return count

    def configure_signal(self, name, *, max_page_size):
        """Commit a new page-size limit in bytes and return updated SignalInfo.

        The positive integer limit applies to future pages; existing pages are
        unchanged. Repeating the same limit is a no-op. Missing/deleted names
        raise SignalNotFoundError. Publication errors may report a committed or
        unknown result, as for other single-signal writes.
        """
        self._open(write=True)
        maximum = integer(max_page_size, "max_page_size", 1)
        with self._backend.lock(self._signal_prefix(name) + "/LOCK"):
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
        return self.info_signal(name)

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
        """Publish a full active-page recovery checkpoint and return WriteResult.

        The checkpoint contains metadata, not a second measurement payload copy.
        If the current commit is already a checkpoint, repair its recovery copies
        if needed and return it without another commit. Missing/deleted names
        raise SignalNotFoundError; repair_recovery() also accepts deleted names.
        """
        self._open(write=True)
        with self._backend.lock(self._signal_prefix(name) + "/LOCK"):
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
        """Stream batches into one exact signal, returning acknowledged IngestResult.

        ``batches`` yields Batch objects or ``(timestamps, values)`` pairs. Each
        input is normalized and buffered into bounded commit groups. This method
        adds streaming to the same concurrency protocol used by store(). ``commit_bytes``
        is a positive byte target (64 MiB by default); a group may exceed it to
        accommodate a page or indivisible record. ``max_page_size`` and ``timezone``
        follow store(). A new or deleted name is created on its first nonempty group.

        ``on_overlap='error'`` rejects groups intersecting existing page ranges;
        ``'replace'`` permits ordinary upserts. Different signal workers proceed
        independently. A final recovery checkpoint completes a nonempty ingestion.
        Empty input returns zero progress without creating a signal.

        IngestError carries ``progress`` for acknowledged groups and its ``cause``;
        earlier commits survive later failures. Progress's batch_index and
        record_offset identify a cursor in sorted/deduplicated input, not original
        unsorted row positions. Its commit count excludes the final checkpoint,
        while generation/commit_id include it. Reconcile committed or ambiguous
        failures before replaying any group.
        """
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
            with self._backend.lock(self._signal_prefix(name, create=True) + "/LOCK"):
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
        """Return a CheckReport for catalogs, active pages, and recovery records.

        Includes deletion tombstones and checks names against authoritative signal
        HEADs rather than trusting the name cache. ``full=True`` additionally
        verifies measurement payload hashes, ordering, and statistics; remote and
        weak-mounted page reads always verify hashes. No repairs or reclamation
        are performed. Run with writers quiescent for a consistent whole-store
        assessment; this scan is not a multi-signal transactional snapshot.
        """
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
