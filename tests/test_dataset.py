import numpy as np

from pagestore import Data,DataSet


def mk_data(a,b,n,name):
    idx=np.linspace(a,b,n)
    rec=idx**3
    return Data(idx,rec,name)


def test_store():
    data1=mk_data(1.,2.,10,'a')
    data2=mk_data(1.,2.,10,'b')
    ds=DataSet()
    ds.store_data(data1)
    ds.store_data(data2)
    assert ds.search()==['a','b']


def test_add():
    data1=mk_data(1.,2.,10,'a')
    data2=mk_data(1.,2.,10,'b')
    ds=data1+data2
    assert ds.search()==['a','b']


