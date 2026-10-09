"""Rebuildable signal discovery, independent of measurement manifests.

A small atomic HEAD references up to 256 immutable sorted name shards. Readers
check HEAD on every search and load only changed shards. Only creation of a new
signal (and explicit rebuilds) takes the catalog lock; ordinary commits do not.
"""

from contextlib import contextmanager

from .catalog import envelope, read_ref, signal_id, unpack, write_immutable
from .errors import CatalogIncompleteError, CorruptionError, PageStoreError


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

    def _empty(self):
        return {
            "version": 1,
            "database_id": self.db._config["database_id"],
            "shards": {},
            "pending": [],
        }

    def _decode(self, raw):
        try:
            head = unpack(raw)
            if (
                head["version"] != 1
                or head["database_id"] != self.db._config["database_id"]
                or not isinstance(head["shards"], dict)
                or not isinstance(head["pending"], list)
                or len(set(head["pending"])) != len(head["pending"])
            ):
                raise ValueError("Invalid name catalog identity or structure")
            for shard, ref in head["shards"].items():
                if (
                    len(shard) != 2
                    or any(c not in "0123456789abcdef" for c in shard)
                    or not ref["key"].startswith(f"catalog/shards/{shard}/")
                ):
                    raise ValueError("Invalid name shard reference")
            for name in head["pending"]:
                signal_id(name)
            return head
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptionError("Invalid name catalog HEAD") from exc

    def _read_shard(self, shard, ref):
        try:
            body = read_ref(self.backend, ref)
            names = body["names"]
            if (
                body["database_id"] != self.db._config["database_id"]
                or body["shard"] != shard
                or not isinstance(names, list)
                or any(not isinstance(n, str) or not n for n in names)
                or any(a >= b for a, b in zip(names, names[1:]))
            ):
                raise ValueError("Invalid sorted name shard")
            return names
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptionError("Invalid name catalog shard") from exc

    def _write_shard(self, shard, names):
        return write_immutable(
            self.backend,
            f"catalog/shards/{shard}",
            {
                "database_id": self.db._config["database_id"],
                "shard": shard,
                "names": names,
            },
        )

    def _publish(self, head):
        self.backend.publish(self.HEAD, [envelope(head)], replace=True)

    def _retire(self, refs):
        # Only derived shards are removed, after their replacement HEAD is durable.
        # Readers with older HEADs retry if a retired shard is no longer present.
        for ref in refs:
            try:
                self.backend.remove(ref["key"])
            except OSError:
                pass  # An unreferenced cache file is harmless; rebuild cleans it up.

    def _rebuild_locked(self):
        head = self._empty()
        shards = {}
        for name in self.db._scan_signal_names():
            shards.setdefault(signal_id(name)[:2], []).append(name)
        for shard, names in shards.items():
            head["shards"][shard] = self._write_shard(shard, names)
        self._publish(head)
        live = {ref["key"] for ref in head["shards"].values()}
        self._retire(
            {"key": p.relative_to(self.backend.root).as_posix()}
            for p in self.backend.path("catalog/shards").glob("*/*.json")
            if p.relative_to(self.backend.root).as_posix() not in live
        )
        return head, sum(map(len, shards.values()))

    def rebuild(self):
        with self.backend.lock(self.LOCK):
            _, count = self._rebuild_locked()
        # Do not retain the old million-name snapshot until another search.
        self._raw, self._head, self._shards, self._names = None, None, {}, []
        return count

    def _ensure_locked(self):
        try:
            return self._decode(self.backend.read(self.HEAD))
        except FileNotFoundError:
            return self._rebuild_locked()[0]

    @contextmanager
    def creating(self, name, commit_id):
        # Lock ordering is always signal -> catalog. Rebuild never takes signal
        # locks: existing-signal commits do not change the name roster.
        with self.backend.lock(self.LOCK):
            head = self._ensure_locked()
            if name not in head["pending"]:
                head["pending"].append(name)
            # Durable intent BEFORE signal HEAD. A crash cannot leave a committed
            # signal undiscoverable, nor make an uncommitted signal appear in search.
            self._publish(head)
            yield
            try:
                shard = signal_id(name)[:2]
                previous = head["shards"].get(shard)
                names = self._read_shard(shard, previous) if previous else []
                names = sorted(set(names) | {name})
                head["shards"][shard] = self._write_shard(shard, names)
                head["pending"].remove(name)
                self._publish(head)
            except Exception as exc:
                raise CatalogIncompleteError(name, commit_id) from exc
            if previous:
                self._retire([previous])

    def names(self):
        # No reader locks or writes, except lazy initialization by a writable DB
        # when opening an older store without a name catalog.
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
                        old = self._shards.get(shard)
                        shards[shard] = (
                            old
                            if old is not None and old[0] == ref
                            else (ref, self._read_shard(shard, ref))
                        )
                except FileNotFoundError as exc:
                    if self.backend.read(self.HEAD) != raw:
                        continue
                    raise CorruptionError("Missing name catalog shard") from exc
                if self._head is None or head["shards"] != self._head["shards"]:
                    self._names = sorted(
                        n for _, names in shards.values() for n in names
                    )
                self._raw, self._head, self._shards = raw, head, shards
            if self._head["pending"]:
                pending = [
                    name
                    for name in self._head["pending"]
                    if self.db._head(name, missing_ok=True)[0] is not None
                ]
                return sorted(set(self._names).union(pending))
            return self._names
        raise PageStoreError(
            "Name catalog changed repeatedly during loading; retry search"
        )
