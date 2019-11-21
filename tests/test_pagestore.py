import os

import numpy as np

from pagestore import Page, Data, PageStore


def mk_data(a, b, n, name):
    idx = np.linspace(a, b, n)
    rec = idx ** 3
    return Data(idx, rec, name)


def test_pagestore(tmp_path):
    basedir = os.path.join(tmp_path, "mydb")
    db = PageStore(basedir, max_page_size=100)
    assert db.new_pageid() == 0
    db.delete()


def test_dbop(tmp_path):
    basedir = os.path.join(tmp_path, "mydb")
    db = PageStore(basedir, max_page_size=100)
    data = mk_data(0.0, 1.0, 100, "a")
    page = Page.from_data(123, data)
    db.insert_page(page)
    assert db.new_pageid() == 124
    assert db.count_pages("a") == 1

    pages = list(db.get_pages("a"))
    assert len(pages) == 1
    assert page.compare(pages[0])

    page1 = db.get_page_before("a", 0)
    assert page.compare(page1)

    page1 = db.get_page(123)
    assert page.compare(page1)

    page1 = db.get_page_before("a", -1)
    assert page1 is None

    db.remove_page(page.pageid)
    assert db.new_pageid() == 0

    db.delete()


def test_prestore(tmp_path):
    basedir = os.path.join(tmp_path, "mydb")
    db = PageStore(basedir, max_page_size=100)
    data = mk_data(0.0, 1.0, 100, "a")

    db.new_page(123, data)
    page = db.get_page(123)
    data1 = page.read(db.pagedir)
    data.compare(data1)

    data = mk_data(1.0, 2.0, 100, "a")
    pageid = db.merge_page_data(page, data)

    data = db.get_page(pageid).read(db.pagedir)

    assert all(data.rec == data.idx ** 3)
    assert len(data) == 199
    assert data.idx[0] == 0.0
    assert data.idx[-1] == 2.0

    data = mk_data(3.0, 4.0, 100, "a")
    db.new_page(124, data)
    page1 = db.get_page(123)
    page2 = db.get_page(124)
    pageid = db.merge_page_page(page1, page2)

    assert page1.pageid == pageid
    data = db.get_page(pageid).read(db.pagedir)
    assert len(data) == 299

    db.delete()


def test_getpage(tmp_path):
    basedir = os.path.join(tmp_path, "mydb")
    db = PageStore(basedir, max_page_size=100)

    # no page
    assert db.get_page_before("a", 1.5) is None
    assert db.get_page_after("a", 1.5) is None

    #  [0. 1.]
    db.new_page(123, mk_data(0.0, 1.0, 100, "a"))
    assert db.get_page_before("a", 1.5).pageid == 123
    assert db.get_page_before("a", 0.0).pageid == 123
    assert db.get_page_before("a", -0.1) is None
    assert db.get_page_before("a", 2.0).pageid == 123

    assert db.get_page_after("a", 1.5) is None
    assert db.get_page_after("a", 0.0) is None
    assert db.get_page_after("a", -0.1).pageid == 123

    #  [0. 1.]  [2. 3.]
    db.new_page(124, mk_data(2.0, 3.0, 100, "a"))
    assert db.get_page_before("a", 1.5).pageid == 123
    assert db.get_page_before("a", 0.0).pageid == 123
    assert db.get_page_before("a", -0.1) is None
    assert db.get_page_before("a", 2.0).pageid == 124

    assert db.get_page_after("a", 0.0).pageid == 124
    assert db.get_page_after("a", 1.9).pageid == 124
    assert db.get_page_after("a", 2.0) is None

    assert len(db.get_pages_between("a", -0.5, -0.1)) == 0
    assert len(db.get_pages_between("a", 0.5, 1.5)) == 1
    assert len(db.get_pages_between("a", 0.5, 2.5)) == 2
    assert len(db.get_pages_between("a", 2.5, 3.5)) == 1
    assert len(db.get_pages_between("a", 3.1, 3.5)) == 0


def test_store(tmp_path):
    basedir = os.path.join(tmp_path, "mydb")
    db = PageStore(basedir, max_page_size=10000)
    db.store_data(mk_data(0, 10, 11, "a"))
    db.store_data(mk_data(5, 15, 11, "a"))

    assert len(list(db.get_pages("a"))) == 1

    data = db.get_data("a")

    assert data.begin() == 0
    assert data.end() == 15
    assert len(data.idx) == 16


def test_get_data(tmp_path):
    basedir = os.path.join(tmp_path, "mydb")
    db = PageStore(basedir, max_page_size=10000)
    db.store({"a": ([1, 2, 3], [0.1, 0.2, 0.3])})
    db.store({"a": ([3, 4, 5], [0.35, 0.45, 0.55])})

    assert db.get_pages_between("a", 2, 4)[0].pageid == 0

    data = db.get_data("a", 2, 4)


def test_rebalance(tmp_path):
    basedir = os.path.join(tmp_path, "mydb")
    db = PageStore(basedir, max_page_size=100)
    db.store_data(mk_data(0, 10, 11, "a"))
    db.store_data(mk_data(5, 15, 11, "a"))
    assert len(list(db.get_pages("a"))) == 2


def test_search(tmp_path):
    basedir = os.path.join(tmp_path, "mydb")
    db = PageStore(basedir, max_page_size=100)
    db.store_data(mk_data(0, 10, 11, "a"))
    db.store_data(mk_data(5, 15, 11, "b"))
    db.store_data(mk_data(5, 15, 11, "c"))
    db.store_data(mk_data(5, 15, 11, "aa"))

    assert db.search("a%") == ["a", "aa"]
    assert db.search() == ["a", "aa", "b", "c"]
