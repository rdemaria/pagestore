"""Storage adapters. Filesystem paths never imply a local locking protocol."""

from .filesystem import BackendInfo, FileBackend
from .xrootd import XRootDBackend


def require_filesystem_paths(*paths):
    """Offline reconstruction is not yet a native remote operation."""
    import os
    from ..errors import UnsupportedBackendError

    if any("://" in os.fspath(path) for path in paths):
        raise UnsupportedBackendError(
            "Offline recover/salvage require filesystem directories, not remote URLs"
        )


def open_backend(
    url,
    *,
    writable=True,
    lock_timeout=30.0,
    xrootd_url=None,
    io_timeout=30,
    visibility_timeout=30.0,
):
    """Select a transport; weak mount aliases use XRootD for all I/O."""
    import os
    from urllib.parse import urlsplit

    raw = os.fspath(url)
    if xrootd_url is not None:
        if "://" in raw and urlsplit(raw).scheme != "file":
            raise ValueError(
                "xrootd_url is only valid with a filesystem path or file URL"
            )
        raw = os.fspath(xrootd_url)
        if urlsplit(raw).scheme not in {"root", "roots"}:
            raise ValueError("xrootd_url must be a root:// or roots:// URL")
    if "://" in raw and urlsplit(raw).scheme in {"root", "roots"}:
        return XRootDBackend(
            raw,
            writable=writable,
            lock_timeout=lock_timeout,
            io_timeout=io_timeout,
            visibility_timeout=visibility_timeout,
        )
    return FileBackend(
        url,
        writable=writable,
        lock_timeout=lock_timeout,
        visibility_timeout=visibility_timeout,
    )


__all__ = ["BackendInfo", "FileBackend", "XRootDBackend", "open_backend"]
