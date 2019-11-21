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


TODO FEATURES:
    read-only
    replace pickle
    no page id recycling
    settings in the db (per variable)
    hashing data
    consistency check

    concurent usage (db locking)
    history
    buffer

    xrootd support
    mysql support

TODO OPTIMIZATION:
    shortcut merge if no overlap
    specialized record savings
