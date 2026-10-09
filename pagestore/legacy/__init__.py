"""The original SQLite/pickle implementation, retained for existing databases.

Legacy files are trusted-input only. They are not the new PageStore format.
"""

from .data import Data, DataSet
from .page import Page
from .pagestore import PageStore

__all__ = ["Data", "DataSet", "Page", "PageStore"]
