"""Immutable time-series pages. The original API lives in pagestore.legacy."""

from .version import __version__
from .db import DB
from .inspection import read_page
from .model import (
    Batch,
    RaggedArray,
    WriteResult,
    IngestResult,
    StoreInfo,
    SignalInfo,
    CheckReport,
    RecoveryReport,
    SalvageReport,
)
from .errors import (
    PageStoreError,
    SignalNotFoundError,
    CorruptionError,
    UnsupportedBackendError,
    LockTimeoutError,
    OverlapError,
    RecoveryIncompleteError,
    CatalogIncompleteError,
    CommitOutcomeUnknownError,
    StoreError,
    IngestError,
)

__all__ = [
    "DB",
    "read_page",
    "Batch",
    "RaggedArray",
    "WriteResult",
    "IngestResult",
    "StoreInfo",
    "SignalInfo",
    "CheckReport",
    "RecoveryReport",
    "SalvageReport",
    "PageStoreError",
    "SignalNotFoundError",
    "CorruptionError",
    "UnsupportedBackendError",
    "LockTimeoutError",
    "OverlapError",
    "RecoveryIncompleteError",
    "CatalogIncompleteError",
    "CommitOutcomeUnknownError",
    "StoreError",
    "IngestError",
    "__version__",
]
