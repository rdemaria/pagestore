import os

import numpy as np

from pagestore import Page, Data
from pagestore.page import num2path


def check_with_data(self,data):
    assert self.begin==data.idx[0]
    assert self.end==data.idx[-1]
    assert self.count==len(data)
    assert self.size==data.get_size()
    assert self.idx_type==data.idx.dtype.str
    assert self.name==data.name

def check_same(page1,page2):
    for k in page1.__dict__:
        assert getattr(page1,k)==getattr(page2,k)


def test_num2path():
    assert num2path(0)=='00'
    assert num2path(99)=='99'
    assert num2path(123)=='01/23'
    assert num2path(12345)=='01/23/45'


def test_page():
    idx=np.arange(0,100.)
    data=Data(idx,idx**2,'test')
    page=Page.from_data(123,data)
    page.check_with_data(data)

def test_to_list():
    idx=np.arange(0,100.)
    data=Data(idx,idx**2,'test')
    page=Page.from_data(123,data)
    page2=Page(*page.to_list())
    check_same(page,page2)


def test_read_write(tmp_path):
    idx=np.arange(0,100.)
    data=Data(idx,idx**2,'test')
    page=Page.from_data(123,data)
    basedir=tmp_path
    page.write(data,basedir)
    page.check(basedir)




