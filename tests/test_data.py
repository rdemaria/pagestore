import numpy as np

from pagestore import Data


def mk_data(a, b, n, name):
    idx = np.linspace(a, b, n)
    rec = idx ** 3
    return Data(idx, rec, name)


def test_sort():
    idx = np.arange(0, 100.0)
    data = Data(idx, idx ** 2, "test")
    data.idx[:10] += 101
    data.sort()
    assert len(data) == 100
    assert all(np.diff(data.idx) > 0)
    assert all(data.rec[:90] == data.idx[:90] ** 2)


def test_cut_idx():
    idx = np.arange(0, 100.0)
    data = Data(idx, idx ** 2, "test")
    left, right = data.cut_idx(50)
    assert len(left) == 50
    assert len(right) == 50
    data2, data3 = left.merge(right)
    assert len(data3) == 0
    assert data2.compare(data)


def test_cut_lt():
    idx = np.arange(0, 100.0)
    data = Data(idx, idx ** 2, "test")
    left, right = data.cut_lt(30.0)
    assert left.idx[-1] == 29.0
    assert right.idx[0] == 30.0


def test_cut_nbytes():
    idx = np.arange(0, 100.0)
    data = Data(idx, idx ** 2, "test")
    left, right = data.cut_nbytes(280)
    assert left.get_size() <= 280
    assert right.get_size() > 280


def test_merge():
    idx = np.arange(0, 100.0)
    data = Data(idx, idx ** 2, "test")
    idx = np.array([0, 1, 2.5, 3.7, 4, 7, 8])
    data2 = Data(idx, idx ** 3)
    data3, data4 = data.merge(data2)
    for tt in idx:
        ii = data3.idx == tt
        assert data3.rec[ii] == data3.idx[ii] ** 3
    for tt in [0, 1, 4, 7, 8]:
        ii = data4.idx == tt
        assert data4.rec[ii] == data4.idx[ii] ** 2


def test_simplemerge():
    data1 = mk_data(0.0, 1.0, 100, "a")
    data2 = mk_data(2.0, 3.0, 100, "a")
    data3, _ = data1.merge(data2)
    data4, _ = data2.merge(data1)

    assert np.all(data3.idx == data3.idx)


def test_overlapmerge():
    data1 = mk_data(0, 10, 11, "a")
    data2 = mk_data(5, 15, 11, "a")
    data3, data4 = data1.merge(data2)
    assert np.all(data3.idx == np.linspace(0, 15, 16))


def test_trim():
    idx = np.arange(0, 100.0)
    data = Data(idx, idx ** 2, "test")
    data1 = data.trim(13, 15)
    assert all(data1.idx == [13, 14, 15])
    assert all(data1.rec == data1.idx ** 2)


def test_append():
    idx = np.arange(0, 100.0)
    data = Data(idx, idx ** 2, "test")
    left, right = data.cut_idx(50)
    data2 = left.append(right)
    assert data2.compare(data)


def test_filter():
    idx = np.arange(0, 100.0)
    data = Data(idx, idx ** 2, "test")
    data1 = data.filter(data.rec % 2 == 0)
    assert all(data1.rec % 2 == 0)
    assert all(data1.rec == data1.idx ** 2)
