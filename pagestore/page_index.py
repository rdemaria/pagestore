"""Immutable ordered page index with batched copy-on-write updates.

Leaves hold up to 128 descriptors; internal nodes hold up to 64 references.
Only dirty nodes are written, once, at the end of a commit.
"""

from collections import OrderedDict

from .catalog import canonical, read_ref, write_immutable
from .errors import CorruptionError
from .timestamps import decode_time

MAX_LEAF = 128
MAX_CHILDREN = 64
MAX_NODE_BYTES = 256 * 1024
AGGREGATES = ("count", "page_count", "stored_bytes", "payload_bytes")


def _descriptor_meta(d):
    return {
        "first": d["first"],
        "last": d["last"],
        "count": d["count"],
        "page_count": 1,
        "stored_bytes": d["size"],
        "payload_bytes": d["payload_bytes"],
    }


class PageIndex:
    def __init__(self, backend, prefix, kind, root=None, *, next_ordinal=0):
        self.backend, self.prefix, self.kind = backend, prefix, kind
        self.root = root
        self.next_ordinal = next_ordinal
        self.cache = OrderedDict()

    def _time(self, value):
        return decode_time(value, self.kind)

    def _load(self, node):
        if "leaf" in node:
            return node
        key = node["key"]
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        loaded = read_ref(self.backend, node)
        if loaded.get("version") != 1 or not loaded.get("items"):
            raise CorruptionError(f"Invalid page index node: {key}")
        calculated = self._node(loaded["leaf"], loaded["items"])
        if any(
            calculated[field] != node[field] for field in ("first", "last", *AGGREGATES)
        ):
            raise CorruptionError(f"Index aggregates disagree: {key}")
        items = loaded["items"]
        if any(
            self._time(a["last"]) >= self._time(b["first"])
            for a, b in zip(items, items[1:])
        ):
            raise CorruptionError(f"Overlapping index intervals: {key}")
        self.cache[key] = loaded
        if len(self.cache) > 256:
            self.cache.popitem(last=False)
        return loaded

    @staticmethod
    def _node(leaf, items):
        metas = [_descriptor_meta(d) for d in items] if leaf else items
        result = {
            "version": 1,
            "leaf": leaf,
            "items": items,
            "first": metas[0]["first"],
            "last": metas[-1]["last"],
        }
        result.update({key: sum(m[key] for m in metas) for key in AGGREGATES})
        return result

    def _split(self, leaf, items):
        if not items:
            return []
        node = self._node(leaf, items)
        maximum = MAX_LEAF if leaf else MAX_CHILDREN
        # Count is the common fast check. Size is checked on flush as well.
        if len(items) <= maximum and (
            not leaf or len(canonical(node)) <= MAX_NODE_BYTES
        ):
            return [node]
        if len(items) == 1:
            raise ValueError("A single index descriptor exceeds the node size limit")
        half = len(items) // 2
        return self._split(leaf, items[:half]) + self._split(leaf, items[half:])

    def _edit(self, reference, first, descriptor):
        if reference is None:
            return [] if descriptor is None else [self._node(True, [descriptor])]
        node = self._load(reference)
        items = list(node["items"])
        target = self._time(first)
        lo, hi = 0, len(items)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._time(items[mid]["first"]) < target:
                lo = mid + 1
            else:
                hi = mid
        if node["leaf"]:
            if lo < len(items) and items[lo]["first"] == first:
                items.pop(lo)
            elif descriptor is None:
                raise CorruptionError("Retired page absent from index")
            if descriptor is not None:
                items.insert(lo, descriptor)
        else:
            index = min(lo, len(items) - 1)
            if self._time(items[index]["first"]) > target:
                index = max(0, index - 1)
            items[index : index + 1] = self._edit(items[index], first, descriptor)
        return self._split(node["leaf"], items)

    def update(self, added, removed=()):
        for descriptor, insert in [(d, False) for d in removed] + [
            (d, True) for d in added
        ]:
            roots = self._edit(
                self.root, descriptor["first"], descriptor if insert else None
            )
            self.root = (
                self._node(False, roots)
                if len(roots) > 1
                else roots[0] if roots else None
            )
        while (
            self.root
            and "leaf" in self.root
            and not self.root["leaf"]
            and len(self.root["items"]) == 1
        ):
            self.root = self.root["items"][0]

    def _flush(self, node):
        if "leaf" not in node:
            return node
        if not node["leaf"]:
            node = self._node(False, [self._flush(child) for child in node["items"]])
        if len(canonical(node)) > MAX_NODE_BYTES:
            raise ValueError("Index node exceeds its serialized size limit")
        ref = write_immutable(
            self.backend, self.prefix + "/index", node, ordinal=self.next_ordinal
        )
        self.next_ordinal += 1
        ref.update({key: node[key] for key in ("first", "last", *AGGREGATES)})
        return ref

    def flush(self):
        if self.root:
            self.root = self._flush(self.root)
        return self.root

    def _intersects(self, node, low, high):
        return (low is None or self._time(node["last"]) >= low) and (
            high is None or self._time(node["first"]) <= high
        )

    def pages(self, low=None, high=None):
        def visit(ref):
            if not self._intersects(ref, low, high):
                return
            node = self._load(ref)
            if node["leaf"]:
                for descriptor in node["items"]:
                    if self._intersects(descriptor, low, high):
                        yield descriptor
            else:
                for child in node["items"]:
                    yield from visit(child)

        if self.root:
            yield from visit(self.root)

    def count(self, low, high, boundary_count):
        def visit(ref):
            if not self._intersects(ref, low, high):
                return 0
            if (low is None or low <= self._time(ref["first"])) and (
                high is None or high >= self._time(ref["last"])
            ):
                return ref["count"]
            node = self._load(ref)
            if node["leaf"]:
                count = 0
                for d in node["items"]:
                    if self._intersects(d, low, high):
                        if (low is None or low <= self._time(d["first"])) and (
                            high is None or high >= self._time(d["last"])
                        ):
                            count += d["count"]
                        else:
                            count += boundary_count(d)
                return count
            return sum(visit(child) for child in node["items"])

        return visit(self.root) if self.root else 0
