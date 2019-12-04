import numpy as np

from pagestore import Data, DataSet


def mk_data(a, b, n, name):
    idx = np.linspace(a, b, n)
    rec = idx ** 3
    return Data(idx, rec, name)


def mk_dataset():
    data1 = mk_data(1.0, 2.0, 7, "a")
    data2 = mk_data(1.0, 2.0, 10, "b")
    ds = data1 + data2
    return ds


def test_store():
    data1 = mk_data(1.0, 2.0, 10, "a")
    data2 = mk_data(1.0, 2.0, 10, "b")
    ds = DataSet()
    ds.store_data(data1)
    ds.store_data(data2)
    assert ds.search() == ["a", "b"]


def test_add():
    data1 = mk_data(1.0, 2.0, 7, "a")
    data2 = mk_data(1.0, 2.0, 10, "b")
    ds = data1 + data2
    assert ds.search() == ["a", "b"]


def test_count():
    ds = mk_dataset()
    assert ds.count()["a"] == 7
    assert ds.count()["b"] == 10
