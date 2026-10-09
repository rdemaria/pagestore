"""Checksummed immutable JSON metadata and atomic HEAD pointers."""

import hashlib
import json
from uuid import uuid4

from .errors import CorruptionError

STORE_FORMAT_VERSION = 3
DIRECTORY_FANOUT = 128


def canonical(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def envelope(value):
    return canonical({"body": value, "sha256": digest(canonical(value))})


def unpack(data):
    try:
        wrapped = json.loads(data)
        body = wrapped["body"]
        if digest(canonical(body)) != wrapped["sha256"]:
            raise ValueError("Checksum mismatch")
        return body
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise CorruptionError("Invalid checksummed catalog JSON") from exc


def signal_id(name):
    if not isinstance(name, str) or not name:
        raise ValueError("Signal names must be nonempty strings")
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def signal_prefix(name, ordinal):
    return f"signals/{ordinal_path(ordinal)}-{signal_id(name)}"


def signal_location(prefix):
    """Decode a canonical adaptive signal directory into (ordinal, name hash)."""
    parts = prefix.split("/")
    if len(parts) < 2 or parts[0] != "signals":
        raise CorruptionError("Invalid signal directory")
    leaf, separator, sid = parts[-1].partition("-")
    digits = parts[1:-1] + [leaf]
    if (
        not separator
        or len(sid) != 64
        or any(c not in "0123456789abcdef" for c in sid)
        or any(not radix_component(d) for d in digits)
        or (len(digits) > 1 and digits[0] == "00")
    ):
        raise CorruptionError("Invalid signal directory")
    ordinal = 0
    for digit in digits:
        ordinal = ordinal * DIRECTORY_FANOUT + int(digit, 16)
    return ordinal, sid


def radix_component(name):
    return len(name) == 2 and name[0] in "01234567" and name[1] in "0123456789abcdef"


def signal_directories(backend):
    """Walk radix branches only; never list a signal's data/index subtrees."""
    stack = ["signals"]
    while stack:
        prefix = stack.pop()
        try:
            names = backend.listdir(prefix)
        except FileNotFoundError:
            continue
        for name in sorted(names):
            key = prefix + "/" + name
            if radix_component(name):
                stack.append(key)
            elif "-" in name:
                signal_location(key)
                yield key


def ordinal_path(ordinal):
    """Base-128 components; small collections need no extra directories."""
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
        raise ValueError("Ordinal must be a nonnegative integer")
    parts = [f"{ordinal % DIRECTORY_FANOUT:02x}"]
    while ordinal := ordinal // DIRECTORY_FANOUT:
        parts.append(f"{ordinal % DIRECTORY_FANOUT:02x}")
    return "/".join(reversed(parts))


def ordinal_directory(ordinal):
    return ordinal_path(ordinal).rpartition("/")[0]


def write_immutable(backend, prefix, value, *, ordinal=None):
    raw = envelope(value)
    if ordinal is not None and (directory := ordinal_directory(ordinal)):
        prefix += "/" + directory
    key = f"{prefix}/{uuid4().hex}.json"
    backend.publish(key, [raw])
    return {"key": key, "sha256": digest(raw)}


def read_ref(backend, ref):
    raw = backend.read(ref["key"], expected=ref["sha256"])
    if digest(raw) != ref["sha256"]:
        raise CorruptionError(f"Catalog checksum mismatch: {ref['key']}")
    return unpack(raw)
