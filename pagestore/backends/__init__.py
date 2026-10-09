"""Storage adapters. Filesystem paths never imply a local locking protocol."""

from .filesystem import BackendInfo, FileBackend

__all__ = ["BackendInfo", "FileBackend"]
