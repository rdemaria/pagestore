PageStore
==========

Summary
-------
PageStore store a collection of named and indexed array organized in multiple page files.

Usage
------

```
from pagestore import PageStore
db=PageStore("./localdb")
db.store({'a': ([1,2,3], [.1,.2.,.3]) })
db.store({'a': ([3,4,5], [.35,.45.,.55]) })

d.data('a',2,4).to_dict() == \
     {'a':  ([2,3,4], [.2.,.35,.45]) })

```

Structure
---------


* Data:
   * store a time series in memory base on an sorted index and record array
   * data has a name

 DataSet:
   * store a set of Data

* Page:
   * can read and write a Data objects from a  source `basedir` and numerical `pageid`
   * stores also begin, end, count, size

* PageStore:
   * manages a set of pages belonging to the same name
   * keeps pages not overlapping and within a given pagesize
   * it uses an sqlite database to store page information and a set of file in `pagedir` to store the data


Todo
---------

- [ ]  read-only modes
- [ ]  optional copy on write mode
 
- [ ]  (per variable) settings in the db 
- [ ]  add hashing of pages
- [ ]  add consistency check and recovery

- [ ]  concurent usage (db locking)
- [ ]  history
- [ ]  buffer

- [ ] xrootd support
- [ ] mysql support

- [ ]  replace pickle with specialized record savings
