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
   * contains an sorted array (index) and array of the same lengths (record)
   * data has a name
   * two Data objects can be merged

 DataSet:
   * contais a set of Data objects

* Page:
   * can read and write a Data objects from a source `basedir` and numerical `pageid`
   * stores also begin, end, count, size

* PageStore:
   * manages a set of pages belonging to the same name
   * keeps pages not overlapping and within a given pagesize
   * it uses an sqlite database to store page information and a set of file in `pagedir` to store the data


Temporary API
--------------

- `search(pattern_or_list)`
- `select(pattern_or_list, idx1, idx2, idx_test, rec_test, limit, skip, offset)`
- `count(pattern_or_list, idx1, idx2, idx_test, rec_test, limit, skip, offset)`
- `iter(variable, idx1, idx2)



Todo
---------

- [ ]  review extraction/search api (collect, count, iter first)
- [ ]  delete records
- [ ]  date time functions

- [ ]  read-only modes
- [ ]  optional copy on write mode
- [ ]  protect private api
- [ ]  test different datatypes for index and records

- [ ]  (per variable) settings in the db
- [ ]  add hashing of pages
- [ ]  add consistency check and recovery

- [ ]  concurent usage (db locking)
- [ ]  history
- [ ]  buffer

- [ ] xrootd support
- [ ] mysql support

- [ ]  replace pickle with specialized record savings
