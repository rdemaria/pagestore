import numpy as np

from pagestore import Data, PageStore


def mk_random_data(idx_a, idx_b, count_a, count_b, name_n):
    count = np.random.randint(count_a, count_b)
    idx = np.random.uniform(low=idx_a, high=idx_b, size=count)
    np.sort(idx)
    rec = idx ** 3
    name = "v" + str(np.random.randint(name_n))
    print(f"new data {name}: count={count} a={idx[0]:5.3g} b={idx[1]:5.3g}")
    return Data(idx, rec, name)


def mk_random_name(nmax):
    return str(np.random.randint(nmax))


def check_non_overlapping_pages(db, name):
    pages = db.get_pages(name)
    if len(pages) > 1:
        bb = np.array([page.begin for page in pages])
        ee = np.array([page.end for page in pages])
        assert np.all(np.diff(np.c_[bb, ee].flatten()))


def check_data(db):
    records = db.count_records_all()
    pages = db.count_pages_all()
    print(f"db records:{records:9} pages:{pages:9}")
    for name in db.search():
        check_non_overlapping_pages(db, name)


def benchmark(idx_a, idx_b, count_a, count_b, name_n, insert_number, max_page_size):
    db = PageStore("./localdb", max_page_size=max_page_size)

    for n in range(insert_number):
        data = mk_random_data(idx_a, idx_b, count_a, count_b, name_n)
        db.store_data(data)
        check_data(db)
        for ii, vv in zip(data.idx, data.rec):
            ww = db.get_data(data.name, ii, ii).rec[0]
            assert ww == vv

    return db


db = benchmark(0, 100, 5, 10, 3, 100, 1000)

# db.delete()
