from .pagestore import PageStore
from .page import Page
from .data import Data, DataSet

__all__=['PageStore','Page','Data','DataSet']



"""

TODO:
    unit tests

FEATURES:
    no page id recycling
    read-only
    concurent usage (db locking)
    history
    settings in the db (per variable)
    buffer
    consistency check
    hashing data
    xrootd support


OPTIMIZATION
    shortcut merge if no overlap
    specialized record savings
"""
