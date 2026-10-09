"""Public errors. Partial writes always carry their acknowledged progress."""


class PageStoreError(Exception):
    """Base exception for PageStore storage, integrity, and commit failures."""


class SignalNotFoundError(KeyError, PageStoreError):
    """An exact signal name is missing or has been fully deleted."""


class CorruptionError(PageStoreError):
    """Stored bytes or metadata fail identity, structural, or integrity checks."""


class UnsupportedBackendError(PageStoreError):
    """A storage transport or requested backend operation is not implemented."""


class LockTimeoutError(TimeoutError, PageStoreError):
    """A writer could not acquire its coordination lock within the timeout."""


class OverlapError(PageStoreError):
    """An ingestion group overlaps existing pages with on_overlap='error'."""


class CommitOutcomeUnknownError(PageStoreError):
    """Publication may still complete; reconcile before retrying or unlocking.

    ``signal`` and ``commit_id`` identify the operation where known, and
    ``committed`` is None. Native remote mutations retain locks and disable writes
    on the affected backend instance until the outstanding outcome is resolved.
    """

    committed = None

    def __init__(self, signal, commit_id):
        self.signal = signal
        self.commit_id = commit_id
        super().__init__(
            f"{signal!r}: cannot reconcile publication of commit {commit_id}; "
            "remote requests may still complete; reconcile outstanding operations "
            "and HEAD before releasing locks or retrying"
        )


class RecoveryIncompleteError(PageStoreError):
    """The identified commit is visible, but its recovery redundancy needs repair.

    ``committed`` is True. Call DB.repair_recovery(signal), including for deleted
    signals, instead of assuming the operation rolled back and replaying it.
    """

    committed = True

    def __init__(self, signal, commit_id):
        self.signal = signal
        self.commit_id = commit_id
        super().__init__(
            f"{signal!r}: commit {commit_id} is visible, but recovery copies need repair"
        )


class CatalogIncompleteError(PageStoreError):
    """A signal commit is recoverable but name-catalog finalization failed.

    ``committed`` is True. Pending catalog intent resolves the signal's current
    visibility; call DB.rebuild_catalog() without replaying the write or deletion.
    """

    committed = True

    def __init__(self, signal, commit_id):
        self.signal = signal
        self.commit_id = commit_id
        super().__init__(
            f"{signal!r}: commit {commit_id} is visible and recoverable; "
            "name catalog finalization failed (search uses the catalog intent); "
            "run rebuild_catalog() without replaying data"
        )


class StoreError(PageStoreError):
    """A multi-signal store stopped, carrying results, failed_name, and cause.

    ``results`` contains acknowledged prior signals. Inspect ``cause`` to learn
    whether the failed signal committed or still has an uncertain outcome.
    """

    def __init__(self, results, failed_name, cause):
        self.results = dict(results)
        self.failed_name = failed_name
        self.cause = cause
        super().__init__(f"Store failed for {failed_name!r}: {cause}")


class IngestError(PageStoreError):
    """Ingestion stopped with an IngestResult progress snapshot and underlying cause.

    Earlier acknowledged groups remain committed. Inspect cause before retrying
    the failing group, which may itself be committed or have an unknown outcome.
    """

    def __init__(self, progress, cause):
        self.progress = progress
        self.cause = cause
        super().__init__(
            f"Ingest failed after {progress.commits} acknowledged commits: {cause}"
        )
