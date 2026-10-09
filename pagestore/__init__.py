"""Immutable time-series pages. The original API lives in pagestore.legacy."""

from .version import __version__
from .db import DB
from .model import (
    Batch,
    RaggedArray,
    WriteResult,
    IngestResult,
    SignalInfo,
    CheckReport,
    RecoveryReport,
)
from .errors import (
    PageStoreError,
    SignalNotFoundError,
    CorruptionError,
    UnsupportedBackendError,
    LockTimeoutError,
    OverlapError,
    RecoveryIncompleteError,
    CommitOutcomeUnknownError,
    StoreError,
    IngestError,
)

__all__ = [
    "DB",
    "Batch",
    "RaggedArray",
    "WriteResult",
    "IngestResult",
    "SignalInfo",
    "CheckReport",
    "RecoveryReport",
    "PageStoreError",
    "SignalNotFoundError",
    "CorruptionError",
    "UnsupportedBackendError",
    "LockTimeoutError",
    "OverlapError",
    "RecoveryIncompleteError",
    "CommitOutcomeUnknownError",
    "StoreError",
    "IngestError",
    "__version__",
]
