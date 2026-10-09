"""Standalone, fully verified measurement-page extraction."""

import numpy as np

from .catalog import signal_id
from .errors import CorruptionError
from .model import RaggedArray
from .page_format import read_page as _read_page


def read_page(path):
    """Return (signal_name, timestamps, records) from one verified data page.

    No DB, index, or recovery record is required. Both header copies, every array
    section, the whole-page SHA-256, and record consistency are checked before any
    data is returned. Corrupt pages raise CorruptionError; intact recovery pages
    raise ValueError because they contain no measurement records. No repairs or
    writes are performed.

    Arrays own writable, native-endian memory, matching DB.get_signal(). Ragged
    records are a one-dimensional object array of owned NumPy arrays. Datetime
    timestamps use datetime64[ns] with UTC semantics; numeric axes remain numeric.
    Verification establishes page integrity, not its former commit status.
    """
    page = _read_page(path, full=True)
    if page.header["page_kind"] != "data":
        raise ValueError("read_page() requires a measurement page, not a recovery page")
    try:
        name = page.header["signal_name"]
        if signal_id(name) != page.header["signal_id"]:
            raise CorruptionError("Data-page signal identity mismatch")
    except (KeyError, TypeError, ValueError) as exc:
        raise CorruptionError("Invalid data-page signal identity") from exc
    batch = page.batch()
    if page.header.get("payload_bytes") != batch.nbytes:
        raise CorruptionError("Data-page payload size mismatch")

    def owned(array):
        return array.astype(array.dtype.newbyteorder("="), copy=True)

    timestamps = owned(batch.timestamps)
    if isinstance(batch.values, RaggedArray):
        records = np.empty(len(batch), dtype=object)
        for i in range(len(batch)):
            records[i] = owned(batch.values.record(i))
    else:
        records = owned(batch.values)
    return name, timestamps, records
