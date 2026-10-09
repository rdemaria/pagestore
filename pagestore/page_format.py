"""Version 1 self-describing, little-endian pages with redundant headers.

The last 32 bytes are SHA256(all preceding bytes). Page payloads never use pickle.
"""

from dataclasses import dataclass
import hashlib
import json
import math
import mmap
from pathlib import Path
import struct

import numpy as np

from .catalog import canonical, digest
from .errors import CorruptionError
from .model import Batch, RaggedArray, statistics, value_dtype
from .timestamps import decode_time, encode_time

PREFIX = struct.Struct("<8sHHI")
TRAILER = struct.Struct("<8sHHIQQ32s32s32s")
MAGIC, END_MAGIC = b"PGSTORE\0", b"PGEND\0\0\0"
VERSION = 1
MAX_HEADER = 64 * 1024


def align(value):
    return (value + 63) // 64 * 64


def _array(values):
    a = np.asarray(values)
    dtype = value_dtype(a.dtype)
    if a.dtype.kind == "b" and np.any(a.view("u1") > 1):
        a = np.not_equal(a, False)
    return np.ascontiguousarray(a, dtype=dtype)


def _view(array):
    return memoryview(array.reshape(-1)).cast("B")


class PagePlan:
    """Own canonical buffers until a streaming publication has finished."""

    def __init__(self, metadata, arrays, *, hash_sections=True):
        self.arrays = [(name, _array(array)) for name, array in arrays]
        self.header = dict(metadata, format_version=VERSION, flags=0)
        sections = []
        for name, array in self.arrays:
            sections.append(
                {
                    "role": name,
                    "dtype": array.dtype.str,
                    "byte_order": (
                        "not_applicable" if array.dtype.byteorder == "|" else "little"
                    ),
                    "order": "C",
                    "shape": list(array.shape),
                    "offset": 0,
                    "length": array.nbytes,
                    "sha256": digest(_view(array)) if hash_sections else "0" * 64,
                }
            )
        self.header.update(sections=sections, backup_offset=0, file_length=0)
        previous = None
        for _ in range(20):
            raw = canonical(self.header)
            offset = align(PREFIX.size + len(raw) + 32)
            for section in sections:
                section["offset"] = offset
                offset = align(offset + section["length"])
            self.header["backup_offset"] = offset
            self.header["file_length"] = offset + len(raw) + 32 + TRAILER.size
            if raw == previous:
                break
            previous = raw
        else:
            raise ValueError("Page header layout did not converge")
        self.raw_header = raw
        if len(raw) > MAX_HEADER:
            raise ValueError("Page header exceeds the 64 KiB format limit")
        self.size = self.header["file_length"]
        self.sha256 = None

    def chunks(self):
        hasher = hashlib.sha256()
        position = 0
        hh = hashlib.sha256(self.raw_header).digest()

        def emit(chunk):
            nonlocal position
            position += len(chunk)
            hasher.update(chunk)
            return chunk

        yield emit(PREFIX.pack(MAGIC, VERSION, 0, len(self.raw_header)))
        yield emit(self.raw_header)
        yield emit(hh)
        for section, (_, array) in zip(self.header["sections"], self.arrays):
            yield emit(b"\0" * (section["offset"] - position))
            yield emit(_view(array))
        yield emit(b"\0" * (self.header["backup_offset"] - position))
        yield emit(self.raw_header)
        yield emit(hh)
        trailer = TRAILER.pack(
            END_MAGIC,
            VERSION,
            0,
            len(self.raw_header),
            self.header["backup_offset"],
            self.size,
            b"\0" * 32,
            hh,
            b"\0" * 32,
        )
        yield emit(trailer[:-32])
        self.sha256 = hasher.hexdigest()
        yield hasher.digest()

    def to_bytes(self):
        return b"".join(self.chunks())


def data_plan(batch, metadata, *, hash_sections=True):
    kind = metadata["time_kind"]
    times = (
        batch.timestamps.view("i8") if kind == "datetime64[ns]" else batch.timestamps
    )
    arrays = [("timestamps", times)]
    if isinstance(batch.values, RaggedArray):
        arrays.extend(
            [("offsets", batch.values.offsets), ("values", batch.values.values)]
        )
    else:
        arrays.append(("values", batch.values))
    header = dict(
        metadata,
        page_kind="data",
        count=len(batch),
        schema=batch.schema,
        first=encode_time(batch.timestamps[0], kind),
        last=encode_time(batch.timestamps[-1], kind),
        time_unit="ns" if kind == "datetime64[ns]" else "caller_defined",
        time_epoch="1970-01-01T00:00:00Z" if kind == "datetime64[ns]" else None,
        statistics=statistics(batch),
        payload_bytes=batch.nbytes,
    )
    return PagePlan(header, arrays, hash_sections=hash_sections)


def recovery_plan(record):
    header = {
        "page_kind": "recovery",
        "scope": record["scope"],
        "database_id": record["config"]["database_id"],
        "config": record["config"],
        "signal_name": record.get("signal_name"),
        "signal_id": record.get("signal_id"),
        "page_id": record["commit_id"],
        "count": 0,
    }
    return PagePlan(
        header, [("recovery_json", np.frombuffer(canonical(record), dtype="u1"))]
    )


@dataclass
class DecodedPage:
    header: dict
    arrays: dict
    buffer: object
    sha256: str
    damaged_envelope: bool = False

    def batch(self):
        times = self.arrays["timestamps"]
        if self.header["time_kind"] == "datetime64[ns]":
            times = times.view("<M8[ns]")
        values = self.arrays["values"]
        if self.header["schema"]["layout"] == "ragged":
            values = RaggedArray(values, self.arrays["offsets"])
        return Batch(times, values)

    def recovery(self):
        try:
            return json.loads(self.arrays["recovery_json"].tobytes())
        except (ValueError, UnicodeError) as exc:
            raise CorruptionError("Invalid recovery JSON") from exc


def _header_copy(buf, offset, length):
    if not 0 < length <= MAX_HEADER or offset < 0 or offset + length + 32 > len(buf):
        return None
    raw = bytes(buf[offset : offset + length])
    if hashlib.sha256(raw).digest() != buf[offset + length : offset + length + 32]:
        return None
    try:
        return raw, json.loads(raw)
    except (ValueError, UnicodeError):
        return None


def decode(buf, *, full=False, expected=None, allow_damaged_envelope=False):
    try:
        return _decode(
            buf,
            full=full,
            expected=expected,
            allow_damaged_envelope=allow_damaged_envelope,
        )
    except CorruptionError:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        struct.error,
        IndexError,
    ) as exc:
        raise CorruptionError(f"Invalid page structure: {exc}") from exc


def _decode(buf, *, full, expected, allow_damaged_envelope):
    if len(buf) < PREFIX.size + TRAILER.size:
        raise CorruptionError("Truncated page")
    pmagic, pversion, pflags, plen = PREFIX.unpack_from(buf)
    trailer = TRAILER.unpack_from(buf, len(buf) - TRAILER.size)
    tmagic, tversion, tflags, tlen, backup, flen, reserved, thash, stored_hash = trailer
    primary = (
        _header_copy(buf, PREFIX.size, plen)
        if pmagic == MAGIC and pversion == VERSION and pflags == 0
        else None
    )
    secondary = (
        _header_copy(buf, backup, tlen)
        if tmagic == END_MAGIC
        and tversion == VERSION
        and tflags == 0
        and flen == len(buf)
        else None
    )
    if primary is None and secondary is None:
        raise CorruptionError("Neither page header is intact")
    if primary and secondary and primary[0] != secondary[0]:
        raise CorruptionError("Valid header copies disagree")
    raw, header = primary or secondary
    hh = hashlib.sha256(raw).digest()
    if (
        header["format_version"] != VERSION
        or header["flags"] != 0
        or header["file_length"] != len(buf)
    ):
        raise CorruptionError("Unknown page version/flags or wrong file length")
    boff = header["backup_offset"]
    if boff + len(raw) + 32 + TRAILER.size != len(buf):
        raise CorruptionError("Invalid backup header location")
    good_trailer = (
        tmagic == END_MAGIC
        and tversion == VERSION
        and tflags == 0
        and tlen == len(raw)
        and backup == boff
        and flen == len(buf)
        and reserved == b"\0" * 32
        and thash == hh
    )
    damaged = not (primary and secondary and good_trailer)
    if damaged and not allow_damaged_envelope:
        raise CorruptionError(
            "Damaged page envelope; recover using the surviving header"
        )
    arrays = {}
    end = align(PREFIX.size + len(raw) + 32)
    for section in header["sections"]:
        dtype_text = section["dtype"]
        if not isinstance(dtype_text, str) or dtype_text[:1] not in ("<", "|"):
            raise CorruptionError("Array storage byte order must be explicit")
        dtype = value_dtype(np.dtype(dtype_text))
        expected_order = "not_applicable" if dtype.str.startswith("|") else "little"
        if (
            dtype.str != dtype_text
            or section["byte_order"] != expected_order
            or section["order"] != "C"
        ):
            raise CorruptionError("Contradictory array encoding")
        shape = section["shape"]
        if not isinstance(shape, list) or any(
            type(n) is not int or n < 0 for n in shape
        ):
            raise CorruptionError("Invalid array shape")
        length = math.prod(shape) * dtype.itemsize
        offset = section["offset"]
        if (
            length != section["length"]
            or offset % 64
            or offset < end
            or offset + length > boff
        ):
            raise CorruptionError(
                "Array section outside payload or overlapping another section"
            )
        role = section["role"]
        if role in arrays:
            raise CorruptionError("Duplicate array role")
        arrays[role] = np.ndarray(tuple(shape), dtype=dtype, buffer=buf, offset=offset)
        arrays[role].flags.writeable = False
        end = offset + length
        if full and digest(memoryview(buf)[offset:end]) != section["sha256"]:
            raise CorruptionError(f"Corrupt {role} section")
    if header["page_kind"] == "data":
        kind = header["time_kind"]
        expected_dtype = "<f8" if kind == "float64" else "<i8"
        if (
            kind not in {"int64", "float64", "datetime64[ns]"}
            or arrays["timestamps"].dtype.str != expected_dtype
        ):
            raise CorruptionError("Invalid timestamp representation")
        if header["time_unit"] != (
            "ns" if kind == "datetime64[ns]" else "caller_defined"
        ) or header["time_epoch"] != (
            "1970-01-01T00:00:00Z" if kind == "datetime64[ns]" else None
        ):
            raise CorruptionError("Contradictory timestamp semantics")
        if arrays["timestamps"].shape != (header["count"],) or header["count"] < 1:
            raise CorruptionError("Invalid timestamp count")
        expected_roles = (
            {"timestamps", "values", "offsets"}
            if header["schema"]["layout"] == "ragged"
            else {"timestamps", "values"}
        )
        if set(arrays) != expected_roles:
            raise CorruptionError("Array roles disagree with schema")
    elif header["page_kind"] == "recovery":
        if (
            set(arrays) != {"recovery_json"}
            or arrays["recovery_json"].dtype.str != "|u1"
        ):
            raise CorruptionError("Invalid recovery payload")
    else:
        raise CorruptionError("Unknown page kind")
    actual_hash = stored_hash.hex()
    if expected is not None and not damaged and actual_hash != expected:
        if (
            not allow_damaged_envelope
            or not full
            or digest(memoryview(buf)[:-32]) != expected
        ):
            raise CorruptionError("Page digest disagrees with catalog")
        damaged = True
    if full and not damaged and digest(memoryview(buf)[:-32]) != actual_hash:
        if not allow_damaged_envelope:
            raise CorruptionError("Full page checksum mismatch")
        # Sections passed independent checks. A receipt may later identify a
        # damaged digest/padding field; no claim of intactness is made here.
        damaged = True
    page = DecodedPage(header, arrays, buf, actual_hash, damaged)
    if header["page_kind"] == "data":
        batch = page.batch()
        if len(batch.values) != len(batch) or batch.schema != header["schema"]:
            raise CorruptionError("Record schema disagrees with payload")
        if full:
            times = batch.timestamps
            if (
                len(times) > 1
                and np.any(times[1:] <= times[:-1])
                or kind == "float64"
                and not np.isfinite(times).all()
                or kind == "datetime64[ns]"
                and np.isnat(times).any()
            ):
                raise CorruptionError("Invalid timestamp ordering or values")
            if times[0] != decode_time(header["first"], kind) or times[
                -1
            ] != decode_time(header["last"], kind):
                raise CorruptionError("Page timestamp bounds disagree")
            if statistics(batch) != header["statistics"]:
                raise CorruptionError("Page statistics disagree")
            values = (
                batch.values.values
                if isinstance(batch.values, RaggedArray)
                else batch.values
            )
            if values.dtype.kind == "b" and np.any(values.view("u1") > 1):
                raise CorruptionError("Noncanonical boolean encoding")
    return page


def read_page(path, *, full=False, expected=None, allow_damaged_envelope=False):
    with Path(path).open("rb") as stream:
        if stream.seek(0, 2) == 0:
            raise CorruptionError("Empty page")
        buf = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
    # NumPy arrays retain their mmap base. Never explicitly close an exported buffer.
    return decode(
        buf, full=full, expected=expected, allow_damaged_envelope=allow_damaged_envelope
    )


def read_salvage_page(path):
    """Fully validate standalone arrays, tolerating a missing metadata-only tail.

    No ordinary read or strict recovery uses this fallback. A truncated payload
    cannot pass: every array byte must exist and match its intact header's hash.
    """
    try:
        return read_page(path, full=True, allow_damaged_envelope=True)
    except CorruptionError as original:
        # Only rebuild a truncated backup/trailer after validating the primary
        # header. Missing array bytes or a lost primary header remain errors.
        with Path(path).open("rb") as stream:
            if stream.seek(0, 2) < PREFIX.size:
                raise original
            buf = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            magic, version, flags, length = PREFIX.unpack_from(buf)
            primary = _header_copy(buf, PREFIX.size, length)
            if (magic, version, flags) != (MAGIC, VERSION, 0) or primary is None:
                raise original
            raw, h = primary
            end = max(s["offset"] + s["length"] for s in h["sections"])
            backup = h["backup_offset"]
            flen = h["file_length"]
            if (
                flen <= len(buf)
                or end > len(buf)
                or backup != align(end)
                or flen != backup + len(raw) + 32 + TRAILER.size
            ):
                raise original
            repaired = bytearray(flen)
            repaired[: len(buf)] = buf
            hh = hashlib.sha256(raw).digest()
            repaired[backup : backup + len(raw) + 32] = raw + hh
            repaired[-TRAILER.size :] = TRAILER.pack(
                END_MAGIC,
                VERSION,
                0,
                len(raw),
                backup,
                flen,
                b"\0" * 32,
                hh,
                b"\0" * 32,
            )
            # The missing whole-page digest remains explicitly untrusted. Full
            # decoding verifies sections and semantics before repair can proceed.
            return decode(repaired, full=True, allow_damaged_envelope=True)
        except (ValueError, TypeError, KeyError, OverflowError, struct.error) as exc:
            raise CorruptionError("Cannot salvage truncated page envelope") from exc


def repair_bytes(page, expected=None, *, allow_new_digest=False):
    """Repair metadata, normally requiring the original whole-page hash.

    Explicit salvage may allow a new digest after validating every section against
    an intact header. It cannot then claim the original envelope was recovered.
    Strict committed-state recovery never enables this option.
    """
    if allow_new_digest and expected is not None:
        raise ValueError("Cannot replace a trusted expected page digest")
    if allow_new_digest:
        # Do not trust a caller to have used full=True before authorizing salvage.
        page = decode(page.buffer, full=True, allow_damaged_envelope=True)
    if not page.damaged_envelope:
        return bytes(page.buffer)
    h = page.header
    raw = canonical(h)
    hh = hashlib.sha256(raw).digest()
    repaired = bytearray(page.buffer)
    repaired[: PREFIX.size] = PREFIX.pack(MAGIC, VERSION, h["flags"], len(raw))
    repaired[PREFIX.size : PREFIX.size + len(raw) + 32] = raw + hh
    end = PREFIX.size + len(raw) + 32
    for section in h["sections"]:
        repaired[end : section["offset"]] = b"\0" * (section["offset"] - end)
        end = section["offset"] + section["length"]
    repaired[end : h["backup_offset"]] = b"\0" * (h["backup_offset"] - end)
    offset = h["backup_offset"]
    repaired[offset : offset + len(raw) + 32] = raw + hh
    trailer = TRAILER.pack(
        END_MAGIC,
        VERSION,
        h["flags"],
        len(raw),
        offset,
        len(repaired),
        b"\0" * 32,
        hh,
        b"\0" * 32,
    )
    repaired[-TRAILER.size : -32] = trailer[:-32]
    candidate = digest(memoryview(repaired)[:-32])
    if not allow_new_digest and candidate != (expected or page.sha256):
        raise CorruptionError(
            "Metadata repair cannot reproduce the trusted page digest"
        )
    repaired[-32:] = bytes.fromhex(candidate)
    decode(repaired, full=True, expected=candidate)
    return bytes(repaired)
