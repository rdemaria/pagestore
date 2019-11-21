import sqlite3

import numpy as np

from .page import Page
from .data import Data


class PageStore:

    def __init__(self,basedir,max_page_size=10000000):
        self.basedir=basedir
        self.db=sqlite3.connect(":memory:")
        self.create_db()
        self.max_page_size=max_page_size

    def create_db(self):
        sql="""
        CREATE TABLE IF NOT EXISTS pages(
              pageid INTEGER,
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

    def insert_page(self,page):
        sql="""INSERT INTO pages VALUES
               (?,?,?,?,?,?,?)"""
        self.db.execute(sql,page.to_list())

    def delete_page(self,page):
        sql="""DELETE FROM pages WHERE pageid==?"""
        self.db.execute(sql,(page.pageid,))
        page.delete()

    def get_pages(self,name):
        sql="""select * FROM pages WHERE name = ?
               ORDER BY begin"""
        return ( Page(*res) for res in self.db.execute(sql,(name,)) )

    def get_page_before(self,name,ii):
        sql="""select * FROM pages WHERE  name = ? AND begin <= ?
               ORDER BY begin DESC LIMIT 1"""
        return  self.db.execute(sql,(name,ii)).fetchone()

    def count_pages(self,name):
        sql="""select count(*) FROM pages WHERE name = ?"""
        return self.db.execute(sql).fetchone()[0]


    def new_pageid(self):
        sql="""SELECT max(pageid)+1 FROM pages"""
        return self.db.execute(sql).fetchone()

    def check(self):
        last=-np.infty
        for page in self.pages:
            if page.begin < last:
                raise ValueError("page {page} collide")
            last=page.end

    def store(self, name, idx, rec=None):
        data=Data(idx,rec,name)
        data.sort()
        if self.count_pages(name)==0:
            self.new_page(0,data)
        # find relevant pages
        while len(data)>0:
            page_before=self.get_page_before(data.begin())
            page_after=self.get_page_after(data.begin())
            if page_before is None:
                # page_after must exists
                curr,data=data.cut_lt(page_after.begin)
                stage.append(self.merge_page(page_after,curr))
            else:
                if page_after is not None:
                    curr,data=data.cut_lt(page_after.begin)
                self.merge_page_data(page_before,idx)
        self.rebalance(name)


    def new_page(self,pageid,data):
        page=Page.from_data(pageid,data)
        page.write(data,self.basedir)
        self.insert_page(page)

    def merge_page_data(self,page,data):
        data,replace=data.merge(page.read(self.basedir))
        page=Page.from_data(page.pageid,data)
        page.write(data,self.basedir)
        self.insert_page(page)

    def merge_page_page(self,page1,page2):
        data1=page1.read(self.basedir)
        data2=page2.read(self.basedir)
        data,replace=data.merge(data1,data2)
        page=Page.from_data(page1.pageid,data)
        page.write(data,self.basedir)
        self.insert_page(page)
        self.delete_page(page2)

    def split_pages(self,name):
        for page in self.get_pages(name):
            if page.size >self.max_page_size:
                data=page.read(self.basedir)
                left,right=data.cut_nbytes(self.max_page_size)
                self.new_page(pageid,left)
                self.new_page(self.new_pageid(),right)

    def join_pages(self,name):
        pages=list(self.get_pages(name))
        for ii in range(len(pages)):
            for jj in range(1,len(pages)-ii):
                if pages[ii].size+pages[ii+jj].size< self.max_page_size:
                    self.merge_page_page(pages[ii],pages[ii+jj])
                else:
                    break

    def rebalance(self,name):
        self.split_pages(name)
        self.join_pages(name)


    def get(self,name):
        data=None

        for page in self.get_pages(name):
            if data is None:
                data=page.read(self.basedir)
            else:
                data.append(page.read(self.basedir))

        return data



