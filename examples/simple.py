from numpy import array

from pagestore import PageStore

db = PageStore("./localdb")
db.store({"a": ([1, 2, 3], [0.1, 0.2, 0.3])})
db.store({"a": ([3, 4, 5], [0.35, 0.45, 0.55])})

res = db.get("a", 2, 4).to_dict()

res == {"a": (array([2, 3, 4]), array([0.2, 0.3, 0.45]))}
