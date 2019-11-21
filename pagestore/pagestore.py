import os, sqlite3
from shutil import rmtree

import numpy as np

from .page import Page
from .data import Data, DataSet

sqlite3.register_adapter(np.int64, int)


class PageStore:
    def __init__(self, pagedir, dbfile=None, max_page_size=10000000):
        self.pagedir = pagedir
        os.makedirs(pagedir, exist_ok=True)
        if dbfile is None:
            dbfile = os.path.join(pagedir, "pagestore.db")
        self.dbfile = dbfile
        self.db = sqlite3.connect(self.dbfile)
        self.create_db()
        self.max_page_size = max_page_size

    def __repr__(self):
        recall = self.count_records_all()
        pages = self.count_pages_all()
        return (
            f"<Pagestore serving '{self.pagedir}': {recall} records in {pages} pages >"
        )

    def delete(self):
        os.unlink(self.dbfile)
        rmtree(self.pagedir)

    #  Database operations
    def create_db(self):
        sql = """
        CREATE TABLE IF NOT EXISTS pages(
              pageid INTEGER PRIMARY KEY,
              name  STRING,
              begin   NUMERIC,
              end     NUMERIC,
              count   INTEGER,
              size    INTEGER,
              idx_type STRING);
        CREATE INDEX IF NOT EXISTS page_index ON pages(pageid);
        """
        self.db.executescript(sql)
        self.db.commit()

    def new_pageid(self):
        sql = """SELECT max(pageid)+1 FROM pages"""
        res = self.db.execute(sql).fetchone()[0]
        if res is None:
            return 0
        else:
            return res

    def insert_page(self, page):
        sql = """INSERT OR REPLACE INTO pages VALUES
               (?,?,?,?,?,?,?)"""
        self.db.execute(sql, page.to_list())
        self.db.commit()

    def remove_page(self, pageid):
        """remove page from database"""
        sql = """DELETE FROM pages WHERE pageid=?"""
        self.db.execute(sql, (pageid,))
        self.db.commit()

    def delete_page(self, page):
        """remove page from database and deleta page data"""
        self.remove_page(page.pageid)
        page.delete(self.pagedir)
        self.db.commit()

    def get_pages(self, name):
        sql = """select * FROM pages WHERE name = ?
               ORDER BY begin"""
        return (Page(*res) for res in self.db.execute(sql, (name,)))

    def get_page_before(self, name, idx):
        """get pages with begin before ii included"""
        sql = """select * FROM pages WHERE  name = ? AND begin <= ?
               ORDER BY begin DESC LIMIT 1"""
        res = self.db.execute(sql, (name, idx)).fetchone()
        if res is not None:
            return Page(*res)

    def get_page_after(self, name, idx):
        """get pages with begin after ii excluded"""
        sql = """select * FROM pages WHERE  name = ? AND begin > ?
               ORDER BY begin LIMIT 1"""
        res = self.db.execute(sql, (name, idx)).fetchone()
        if res is not None:
            return Page(*res)

    def get_pages_between(self, name, idx1, idx2):
        """get pages with begin before ii included"""
        sql = """select * FROM pages
               WHERE  name = ? AND end >= ? AND begin <= ?
               ORDER BY begin"""
        return [Page(*res) for res in self.db.execute(sql, (name, idx1, idx2))]

    def count_pages(self, name):
        sql = """select count(*) FROM pages WHERE name = ?"""
        return self.db.execute(sql, (name,)).fetchone()[0]

    def count_records(self, name):
        sql = """select sum(count) FROM pages WHERE name = ?"""
        res = self.db.execute(sql, (name,)).fetchone()[0]
        return 0 if res is None else res

    def get_page(self, pageid):
        sql = """select * FROM pages WHERE pageid = ?"""
        res = self.db.execute(sql, (pageid,)).fetchone()
        if res is not None:
            return Page(*res)

    def count_pages_all(self):
        sql = """select count(*) FROM pages"""
        return self.db.execute(sql).fetchone()[0]

    def count_records_all(self):
        sql = """select sum(count) FROM pages"""
        res = self.db.execute(sql).fetchone()[0]
        return 0 if res is None else res

    def search(self, pattern="%"):
        sql = """SELECT DISTINCT name FROM pages WHERE name LIKE ? ORDER BY name"""
        return [row[0] for row in self.db.execute(sql, (pattern,))]

    #  End Database operations

    def new_page(self, pageid, data):
        """store data in pageid"""
        page = Page.from_data(pageid, data)
        page.write(data, self.pagedir)
        self.insert_page(page)

    def merge_page_data(self, page, data):
        data, replace = data.merge(page.read(self.pagedir))
        # pageid is recycled
        page = Page.from_data(page.pageid, data)
        page.write(data, self.pagedir)
        self.insert_page(page)
        return page.pageid

    def merge_page_page(self, page1, page2):
        data1 = page1.read(self.pagedir)
        data2 = page2.read(self.pagedir)
        data, replace = data1.merge(data2)
        # pageid of page1 is recycled, page2 dropped
        page = Page.from_data(page1.pageid, data)
        page.write(data, self.pagedir)
        self.insert_page(page)
        self.delete_page(page2)
        return page.pageid

    def split_pages(self, name):
        for page in self.get_pages(name):
            if page.size > self.max_page_size:
                data = page.read(self.pagedir)
                left, right = data.cut_nbytes(self.max_page_size)
                # recycle page.pageid
                self.new_page(page.pageid, left)
                # get new pageid
                self.new_page(self.new_pageid(), right)

    def join_pages(self, name):
        pages = list(self.get_pages(name))
        for ii in range(len(pages)):
            for jj in range(1, len(pages) - ii):
                if pages[ii].size + pages[ii + jj].size < self.max_page_size:
                    self.merge_page_page(pages[ii], pages[ii + jj])
                else:
                    break

    def rebalance(self, name):
        self.split_pages(name)
        self.join_pages(name)

    def store_data(self, data):
        # make sure it is sorted
        data.sort()
        name = data.name
        if self.count_pages(name) == 0:
            # first page in db
            self.new_page(self.new_pageid(), data)
            return
        # find relevant pages
        # each page convers the regions from the beginning of the page to
        # the beginning of the next page excluded
        # with the exception of the first page that covers everything before and
        # and the last page that covers after
        while len(data) > 0:
            # find the page before and after the beginnig of data
            # there must at least be one page
            page_before = self.get_page_before(name, data.begin())
            page_after = self.get_page_after(name, data.begin())
            if page_before is None:
                # page_after must exists
                # data before page.begin is pre-pended to page and the rest re-submitted
                curr, data = data.cut_lt(page_after.begin)
                self.merge_page(page_after, curr)
            else:
                # a page before exists
                # if a page after exists data belonging to page_before is used and
                # the rest resubmitted otherwise everything is used
                if page_after is not None:
                    curr, data = data.cut_lt(page_after.begin)
                else:
                    curr = data
                    data = []
                self.merge_page_data(page_before, curr)
        # by merging some pages might have grown too much,
        # rebalance split and rejoin pages
        self.rebalance(name)

    def store(self, dataset):
        for name, data in dataset.items():
            if Data.is_data(data):
                self.store_data(data)
            else:
                idx, rec = data
                self.store_data(Data(idx, rec, name))

    # Extraction methods
    def get_data(self, name, idx1=-np.infty, idx2=np.infty):
        pages = self.get_pages_between(name, idx1, idx2)

        data = None
        for page in pages:
            if data is None:
                data = page.read(self.pagedir)
            else:
                data.append(page.read(self.pagedir))

        return data.trim(idx1, idx2)

    def get_names(self, pattern_or_list=""):
        if type(pattern_or_list) is str:
            pattern = pattern_or_list
            return [k for k in self.search(pattern) if pattern in k]
        else:
            lst = pattern_or_list
            return [k for k in lst if self.count_pages(k) > 0]

    def get(self, pattern_or_list, idx1=-np.infty, idx2=np.infty):
        res = {
            name: self.get_data(name, idx1, idx2)
            for name in self.get_names(pattern_or_list)
        }
        return DataSet(res)

    # Consistency check
    def check(self):
        last = -np.infty
        for page in self.pages:
            if page.begin < last:
                raise ValueError("page {page} collide")
            last = page.end
