"""Run after installing the package, or with PYTHONPATH=. from the repository."""

from tempfile import TemporaryDirectory

from pagestore import DB

with TemporaryDirectory() as directory:
    with DB(directory) as db:
        db.store({"temperature": ([1, 2, 3], [10.0, 11.0, 12.0])})
        db.store({"temperature": ([3, 4], [12.5, 13.0])})
        print(db.get_signal("temperature", 2, 4))
        db.store({"beam": (["2026-01-01 12:00:00.123456789"], [42.0])}, timezone="cern")
        print(db.get_signal("beam"))
        print(db.check(full=True))
