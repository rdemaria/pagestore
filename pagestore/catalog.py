"""Checksummed immutable JSON metadata and atomic HEAD pointers."""

import hashlib
import json
from uuid import uuid4

from .errors import CorruptionError


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


def signal_prefix(name):
    sid = signal_id(name)
    return f"signals/{sid[:2]}/{sid}"


def write_immutable(backend, prefix, value):
    raw = envelope(value)
    key = f"{prefix}/{uuid4().hex}.json"
    backend.publish(key, [raw])
    return {"key": key, "sha256": digest(raw)}


def read_ref(backend, ref):
    raw = backend.read(ref["key"])
    if digest(raw) != ref["sha256"]:
        raise CorruptionError(f"Catalog checksum mismatch: {ref['key']}")
    return unpack(raw)
