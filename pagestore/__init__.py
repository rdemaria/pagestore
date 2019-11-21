"""
SUMMARY:

    * Data:
        * store a time series in memory base on an sorted index and record array
        * data has a name

    * DataSet
        * store a set of Data

    * Page:
         * page can read and write a Data object from a  source `basedir` and numerical `pageid`
         * page stores also begin, end, count, size

    * PageStore:
         * Manages a set of pages belonging to the same name
         * keeps pages not overlapping and within a given pagesize
         * it uses a .db to store page information and basedir to locate the data


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
    mysql support

OPTIMIZATION
    shortcut merge if no overlap
    specialized record savings
"""



from .pagestore import PageStore
from .page import Page
from .data import Data, DataSet

__all__=['PageStore','Page','Data','DataSet']



