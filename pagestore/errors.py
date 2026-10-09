"""Public errors. Partial writes always carry their acknowledged progress."""


class PageStoreError(Exception):
    pass


class SignalNotFoundError(KeyError, PageStoreError):
    pass


class CorruptionError(PageStoreError):
    pass


class UnsupportedBackendError(PageStoreError):
    pass


class LockTimeoutError(TimeoutError, PageStoreError):
    pass


class OverlapError(PageStoreError):
    pass


class CommitOutcomeUnknownError(PageStoreError):
    committed = None

    def __init__(self, signal, commit_id):
        self.signal = signal
        self.commit_id = commit_id
        super().__init__(
            f"{signal!r}: cannot reconcile publication of commit {commit_id}; inspect HEAD before retrying"
        )


class RecoveryIncompleteError(PageStoreError):
    committed = True

    def __init__(self, signal, commit_id):
        self.signal = signal
        self.commit_id = commit_id
        super().__init__(
            f"{signal!r}: commit {commit_id} is visible, but recovery copies need repair"
        )


class StoreError(PageStoreError):
    def __init__(self, results, failed_name, cause):
        self.results = dict(results)
        self.failed_name = failed_name
        self.cause = cause
        super().__init__(f"Store failed for {failed_name!r}: {cause}")


class IngestError(PageStoreError):
    def __init__(self, progress, cause):
        self.progress = progress
        self.cause = cause
        super().__init__(
            f"Ingest failed after {progress.commits} acknowledged commits: {cause}"
        )
