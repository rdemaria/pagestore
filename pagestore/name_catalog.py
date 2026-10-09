"""Cached names and stable signal locations, with serialized rare allocations.

A small HEAD references up to 128 immutable columnar shards. Location reservations
survive deletion; a per-signal identity record makes the catalog rebuildable.
"""

from bisect import bisect_left
from collections import OrderedDict
from contextlib import contextmanager

from .catalog import (
    DIRECTORY_FANOUT,
    envelope,
    read_ref,
    signal_id,
    signal_prefix,
    unpack,
    write_immutable,
)
from .errors import (
    CatalogIncompleteError,
    CommitOutcomeUnknownError,
    CorruptionError,
    PageStoreError,
)


def name_shard(name):
    return f"{int(signal_id(name)[:2], 16) // 2:02x}"


class NameCatalog:
    HEAD = "catalog/HEAD.json"
    LOCK = "catalog/LOCK"

    def __init__(self, db):
        self.db = db
        self.backend = db._backend
        self._raw = None
        self._head = None
        self._shards = {}
        self._names = []
        self._locations = OrderedDict()

    def _empty(self):
        return {
            "version": 2,
            "database_id": self.db._config["database_id"],
            "next_signal_ordinal": 0,
            "shards": {},
            "pending": [],
        }

    def _decode(self, raw):
        try:
            head = unpack(raw)
            if (
                head["version"] != 2
                or head["database_id"] != self.db._config["database_id"]
                or not isinstance(head["shards"], dict)
                or len(head["shards"]) > DIRECTORY_FANOUT
                or not isinstance(head["pending"], list)
                or len(set(head["pending"])) != len(head["pending"])
                or type(head["next_signal_ordinal"]) is not int
                or head["next_signal_ordinal"] < 0
            ):
                raise ValueError("Invalid catalog identity or allocation state")
            for shard, ref in head["shards"].items():
                if (
                    len(shard) != 2
                    or shard[0] not in "01234567"
                    or shard[1] not in "0123456789abcdef"
                    or not ref["key"].startswith("catalog/shards/")
                ):
                    raise ValueError("Invalid catalog shard reference")
            for name in head["pending"]:
                signal_id(name)
            return head
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptionError("Invalid name catalog HEAD") from exc

    def _read_shard(self, shard, ref):
        try:
            body = read_ref(self.backend, ref)
            names, ordinals, live = body["names"], body["ordinals"], body["live"]
            if (
                body["database_id"] != self.db._config["database_id"]
                or body["shard"] != shard
                or not all(isinstance(v, list) for v in (names, ordinals, live))
                or not len(names) == len(ordinals) == len(live)
                or any(not isinstance(n, str) or not n for n in names)
                or any(a >= b for a, b in zip(names, names[1:]))
                or any(type(o) is not int or o < 0 for o in ordinals)
                or len(set(ordinals)) != len(ordinals)
                or any(type(v) is not bool for v in live)
                or any(name_shard(n) != shard for n in names)
            ):
                raise ValueError("Invalid signal location shard")
            return body
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptionError("Invalid name catalog shard") from exc

    def _blank_shard(self, shard):
        return {
            "database_id": self.db._config["database_id"],
            "shard": shard,
            "names": [],
            "ordinals": [],
            "live": [],
        }

    def _write_shard(self, body):
        # At most 128 active files; obsolete generations are reclaimed below.
        return write_immutable(self.backend, "catalog/shards", body)

    def _publish(self, head):
        self.backend.publish(self.HEAD, [envelope(head)], replace=True)

    def _retire(self, refs):
        for ref in refs:
            try:
                self.backend.remove(ref["key"])
            except OSError:
                pass

    def _remember(self, name, ordinal):
        previous = self._locations.get(name)
        if previous is not None and previous != ordinal:
            raise CorruptionError("A signal's reserved location changed")
        self._locations[name] = ordinal
        self._locations.move_to_end(name)
        if len(self._locations) > 4096:
            self._locations.popitem(last=False)
        return ordinal

    @staticmethod
    def _position(body, name):
        i = bisect_left(body["names"], name)
        return i if i < len(body["names"]) and body["names"][i] == name else None

    def _cached_shard(self, shard, ref):
        previous = self._shards.get(shard)
        if previous is None or previous[0] != ref:
            previous = ref, self._read_shard(shard, ref)
            self._shards[shard] = previous
        return previous[1]

    def _anchored_location(self, name, ordinal):
        # A reservation can fail before its identity is published, and a rebuild
        # may drop it. Cache only locations with a durable, immutable anchor.
        self.db._verify_signal_identity(name, ordinal)
        self.db._remember_identity(name)
        return self._remember(name, ordinal)

    def location(self, name):
        """Return a stable ordinal, or None; normal exact reads load one shard."""
        signal_id(name)
        if name in self._locations:
            self._locations.move_to_end(name)
            return self._locations[name]
        shard = name_shard(name)
        cached = self._shards.get(shard)
        if cached is not None:
            i = self._position(cached[1], name)
            if i is not None:
                try:
                    return self._anchored_location(name, cached[1]["ordinals"][i])
                except FileNotFoundError:
                    pass
        for _ in range(8):
            try:
                raw = self.backend.read(self.HEAD)
                head = self._decode(raw)
                ref = head["shards"].get(shard)
                if ref is None:
                    return None
                try:
                    body = self._cached_shard(shard, ref)
                except FileNotFoundError:
                    if self.backend.read(self.HEAD) != raw:
                        continue
                    raise CorruptionError("Missing name catalog shard") from None
                i = self._position(body, name)
                if i is None:
                    return None
                ordinal = body["ordinals"][i]
                if ordinal >= head["next_signal_ordinal"]:
                    raise CorruptionError("Location exceeds catalog allocation counter")
                try:
                    return self._anchored_location(name, ordinal)
                except FileNotFoundError:
                    return ordinal
            except (FileNotFoundError, CorruptionError):
                # Read-only fallback for missing/damaged derived metadata. Never
                # silently replace a corrupt catalog as part of an ordinary read.
                for entry in self.db._scan_signal_entries():
                    if entry["name"] == name:
                        return self._remember(name, entry["ordinal"])
                return None
        raise PageStoreError("Name catalog changed repeatedly; retry lookup")

    def reserve(self, name):
        """Allocate once under the catalog lock, then release it before signal I/O."""
        ordinal = self.location(name)
        if ordinal is not None:
            # Existence of the durable identity makes cached locations independent
            # of catalog rebuilds. Hot paths use the DB's bounded identity cache.
            if name in self.db._known_identities:
                return ordinal
            try:
                self.db._verify_signal_identity(name, ordinal)
            except FileNotFoundError:
                pass
            else:
                self.db._remember_identity(name)
                return ordinal
        with self.backend.lock(self.LOCK):
            head = self._ensure_locked()
            shard = name_shard(name)
            previous = head["shards"].get(shard)
            body = (
                self._read_shard(shard, previous)
                if previous
                else self._blank_shard(shard)
            )
            if any(o >= head["next_signal_ordinal"] for o in body["ordinals"]):
                raise CorruptionError("Location exceeds catalog allocation counter")
            i = self._position(body, name)
            if i is None:
                ordinal = head["next_signal_ordinal"]
                i = bisect_left(body["names"], name)
                body["names"].insert(i, name)
                body["ordinals"].insert(i, ordinal)
                body["live"].insert(i, False)
                head["next_signal_ordinal"] += 1
                head["shards"][shard] = self._write_shard(body)
                # Reserve the counter and location atomically before anchoring it.
                self._publish(head)
            else:
                ordinal = body["ordinals"][i]
            self.db._ensure_signal_identity(name, ordinal)
            self.db._remember_identity(name)
            self._remember(name, ordinal)
            if previous and head["shards"][shard] != previous:
                self._retire([previous])
        return ordinal

    def _rebuild_locked(self):
        head = self._empty()
        shards, seen_names, seen_ordinals = {}, set(), set()
        count = 0
        for entry in self.db._scan_signal_entries():
            name, ordinal = entry["name"], entry["ordinal"]
            if name in seen_names or ordinal in seen_ordinals:
                raise CorruptionError("Duplicate signal name or allocation ordinal")
            seen_names.add(name)
            seen_ordinals.add(ordinal)
            shard = name_shard(name)
            body = shards.setdefault(shard, self._blank_shard(shard))
            body["names"].append(name)
            body["ordinals"].append(ordinal)
            body["live"].append(entry["live"])
            head["next_signal_ordinal"] = max(head["next_signal_ordinal"], ordinal + 1)
            count += entry["live"]
        for shard, body in shards.items():
            order = sorted(range(len(body["names"])), key=body["names"].__getitem__)
            for column in ("names", "ordinals", "live"):
                body[column] = [body[column][i] for i in order]
            head["shards"][shard] = self._write_shard(body)
        self._publish(head)
        live = {ref["key"] for ref in head["shards"].values()}
        self._retire(
            {"key": key}
            for key in self.backend.glob("catalog/shards/*.json")
            if key not in live
        )
        return head, count

    def rebuild(self):
        with self.backend.lock(self.LOCK):
            _, count = self._rebuild_locked()
        self._raw, self._head, self._shards, self._names = None, None, {}, []
        return count

    def _ensure_locked(self):
        try:
            return self._decode(self.backend.read(self.HEAD))
        except FileNotFoundError:
            return self._rebuild_locked()[0]

    def creating(self, name, commit_id):
        return self._changing(name, commit_id, present=True)

    def removing(self, name, commit_id):
        return self._changing(name, commit_id, present=False)

    @contextmanager
    def _changing(self, name, commit_id, *, present):
        # Allocation releases the catalog lock before the signal lock is acquired.
        # Publication lock ordering remains signal -> catalog; rebuild takes only
        # the catalog lock and reads stable identity records plus committed HEADs.
        with self.backend.lock(self.LOCK):
            head = self._ensure_locked()
            if name not in head["pending"]:
                head["pending"].append(name)
            self._publish(head)
            yield
            try:
                shard = name_shard(name)
                previous = head["shards"][shard]
                body = self._read_shard(shard, previous)
                i = self._position(body, name)
                if i is None:
                    raise CorruptionError("Unreserved signal in catalog intent")
                body["live"][i] = present
                head["shards"][shard] = self._write_shard(body)
                head["pending"].remove(name)
                self._publish(head)
            except CommitOutcomeUnknownError:
                raise
            except Exception as exc:
                raise CatalogIncompleteError(name, commit_id) from exc
            self._retire([previous])

    def names(self):
        for _ in range(8):
            try:
                raw = self.backend.read(self.HEAD)
            except FileNotFoundError:
                if not self.backend.writable:
                    return self.db._scan_signal_names()
                with self.backend.lock(self.LOCK):
                    self._ensure_locked()
                continue
            if raw != self._raw:
                head = self._decode(raw)
                shards = {}
                try:
                    for shard, ref in head["shards"].items():
                        body = self._cached_shard(shard, ref)
                        if any(
                            o >= head["next_signal_ordinal"] for o in body["ordinals"]
                        ):
                            raise CorruptionError(
                                "Location exceeds catalog allocation counter"
                            )
                        shards[shard] = ref, body
                except FileNotFoundError as exc:
                    if self.backend.read(self.HEAD) != raw:
                        continue
                    raise CorruptionError("Missing name catalog shard") from exc
                if self._head is None or head["shards"] != self._head["shards"]:
                    self._names = sorted(
                        name
                        for _, body in shards.values()
                        for name, live in zip(body["names"], body["live"])
                        if live
                    )
                self._raw, self._head, self._shards = raw, head, shards
            if self._head["pending"]:
                present = []
                for name in self._head["pending"]:
                    shard = self._shards[name_shard(name)][1]
                    i = self._position(shard, name)
                    if i is None:
                        raise CorruptionError("Pending name has no reserved location")
                    prefix = signal_prefix(name, shard["ordinals"][i])
                    if (
                        self.db._head(name, missing_ok=True, prefix=prefix)[0]
                        is not None
                    ):
                        present.append(name)
                return sorted(
                    (set(self._names) - set(self._head["pending"])) | set(present)
                )
            return self._names
        raise PageStoreError(
            "Name catalog changed repeatedly during loading; retry search"
        )
