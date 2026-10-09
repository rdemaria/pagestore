import errno
import json
import shutil

import numpy as np
import pytest

from pagestore import DB, RaggedArray
from pagestore.maintenance import recover, salvage
from pagestore.page_format import PREFIX, read_page


def data_files(db, name):
    manifest, _ = db._head(name)
    return [db._backend.path(d["key"]) for d in db._index(manifest).pages()]


def flip(path, offset):
    raw = bytearray(path.read_bytes())
    raw[offset] ^= 1
    path.write_bytes(raw)


def flatten_data(db, destination, names):
    destination.mkdir()
    paths = []
    for name in names:
        for source in data_files(db, name):
            path = destination / f"anonymous-{len(paths)}.pg"
            shutil.copyfile(source, path)
            paths.append(path)
    return paths


def test_data_pages_alone_reused_with_new_commits_and_recovery(tmp_path):
    source, flat, dest = (tmp_path / name for name in ("source", "flat", "new"))
    with DB(source, default_max_page_size=3500) as db:
        db.store(
            {
                "numeric/µ": (np.arange(100), np.arange(100, dtype=">f8")),
                "date": (["2024-01-01", "2024-01-02"], np.ones((2, 3), dtype=">i2")),
                "ragged": (
                    [1, 2],
                    RaggedArray.from_arrays([np.arange(2), np.arange(4)]),
                ),
            }
        )
        expected = db.get()
        originals = flatten_data(db, flat, db.search())
    before = {p.name: p.read_bytes() for p in originals}
    # A duplicate physical copy of one page must not become duplicate records.
    shutil.copyfile(originals[0], flat / "duplicate.pg")
    report = salvage(flat, dest)
    assert report.ok and report.commit_status == "unknown"
    assert report.duplicate_pages == 1
    assert set(report.salvaged_signals) == set(expected)
    assert report.salvaged_pages == len(originals)
    assert report.salvaged_records == 104
    with DB(dest, mode="r") as db:
        assert db._config["database_id"] == report.source_database_id
        assert db.check(full=True).ok
        for name, wanted in expected.items():
            times, values = db.get_signal(name)
            np.testing.assert_array_equal(times, wanted[0])
            for value, original in zip(values, wanted[1]):
                np.testing.assert_array_equal(value, original)
        actual_bytes = {
            p.read_bytes() for name in db.search() for p in data_files(db, name)
        }
        assert actual_bytes == set(before.values())
    assert all((flat / name).read_bytes() == raw for name, raw in before.items())
    provenance = [
        json.loads(line)
        for line in (dest / "salvage-pages.jsonl").read_text().splitlines()
    ]
    assert len(provenance) == len(originals)
    assert all(
        entry["action"] == "copy" and entry["destination_commit_id"]
        for entry in provenance
    )
    assert recover(dest, tmp_path / "recovered-again").complete


@pytest.mark.parametrize(
    "damage", ["prefix", "primary", "backup", "digest", "padding", "truncated_tail"]
)
def test_salvage_header_damage_without_any_recovery_record(tmp_path, damage):
    with DB(tmp_path / "source") as db:
        db.store({"x": ([1, 2], [10, 20])})
        paths = flatten_data(db, tmp_path / "flat", ["x"])
    path = paths[0]
    original = path.read_bytes()
    h = read_page(path).header
    if damage == "truncated_tail":
        path.write_bytes(original[: h["backup_offset"]])
    else:
        offset = {
            "prefix": 0,
            "primary": PREFIX.size + 10,
            "backup": h["backup_offset"] + 10,
            "digest": len(original) - 1,
            "padding": h["sections"][0]["offset"] - 1,
        }[damage]
        flip(path, offset)
    damaged = path.read_bytes()
    report = salvage(tmp_path / "flat", tmp_path / "new", reuse="hardlink")
    assert report.ok and report.repaired_pages == [str(path)]
    assert report.copied_pages == 1 and report.linked_pages == 0
    assert bool(report.regenerated_digests) == (damage in {"digest", "truncated_tail"})
    assert path.read_bytes() == damaged
    with DB(tmp_path / "new", mode="r") as db:
        assert data_files(db, "x")[0].read_bytes() == original
        assert db.check(full=True).ok


@pytest.mark.parametrize(
    "damage", ["values", "timestamps", "both_headers", "truncated_values"]
)
def test_unverifiable_pages_rejected_other_signals_salvaged(tmp_path, damage):
    with DB(tmp_path / "source") as db:
        db.store({"bad": ([1, 2], [10, 20]), "good": ([1], [30])})
        paths = flatten_data(db, tmp_path / "flat", ["bad", "good"])
    h = read_page(paths[0]).header
    if damage == "both_headers":
        flip(paths[0], PREFIX.size + 10)
        flip(paths[0], h["backup_offset"] + 10)
    elif damage == "truncated_values":
        paths[0].write_bytes(paths[0].read_bytes()[: h["sections"][-1]["offset"] + 1])
    else:
        section = next(s for s in h["sections"] if s["role"] == damage)
        flip(paths[0], section["offset"])
    report = salvage(tmp_path / "flat", tmp_path / "new")
    assert not report.ok and list(report.rejected_pages) == [str(paths[0])]
    assert report.salvaged_signals == ["good"]
    with DB(tmp_path / "new", mode="r") as db:
        assert db.search() == ["good"]
        assert db.check(full=True).ok


def test_overlap_requires_explicit_selection(tmp_path):
    with DB(tmp_path / "source") as db:
        db.store({"x": ([1, 2], [10, 20]), "other": ([1], [50])})
        old = data_files(db, "x")[0]
        db.store({"x": ([2], [99])})
        new = data_files(db, "x")[0]
    source = tmp_path / "source"
    report = salvage(source, tmp_path / "conflicted")
    assert not report.ok and report.salvaged_signals == ["other"]
    assert set(report.conflicts["x"]) == {str(old), str(new)}
    assert report.ignored_recovery_pages > 0
    selected = salvage(source, tmp_path / "selected", pages=[new.relative_to(source)])
    assert selected.ok and selected.salvaged_signals == ["x"]
    with DB(tmp_path / "selected", mode="r") as db:
        assert db.get_signal("x")[1].tolist() == [10, 99]


def test_damaged_middle_page_leaves_verified_data_and_reports_gap(tmp_path):
    with DB(tmp_path / "source", default_max_page_size=1) as db:
        db.store({"x": ([1, 2, 3], [10, 20, 30])})
        paths = flatten_data(db, tmp_path / "flat", ["x"])
    header = read_page(paths[1]).header
    flip(paths[1], header["sections"][-1]["offset"])
    report = salvage(tmp_path / "flat", tmp_path / "new")
    assert not report.ok and report.salvaged_records == 2
    assert list(report.rejected_pages) == [str(paths[1])]
    with DB(tmp_path / "new", mode="r") as db:
        assert db.get_signal("x")[0].tolist() == [1, 3]
        assert db.get_signal("x")[1].tolist() == [10, 30]
        assert db.check(full=True).ok


def test_conflicting_page_identity_is_reported_even_without_time_overlap(tmp_path):
    from pagestore.model import Batch
    from pagestore.page_format import data_plan

    with DB(tmp_path / "source") as db:
        db.store({"x": ([1], [10])})
        paths = flatten_data(db, tmp_path / "flat", ["x"])
    header = read_page(paths[0]).header
    changed = data_plan(Batch(np.array([2]), np.array([20])), header).to_bytes()
    (tmp_path / "flat/conflicting.pg").write_bytes(changed)
    report = salvage(tmp_path / "flat", tmp_path / "new")
    assert not report.ok and report.salvaged_pages == 0
    assert len(report.conflicts["x"]) == 2


def test_hardlink_reuses_intact_files_without_copy(tmp_path):
    with DB(tmp_path / "source") as db:
        db.store({"x": ([1, 2], [10, 20])})
        path = data_files(db, "x")[0]
    report = salvage(tmp_path / "source", tmp_path / "new", reuse="hardlink")
    assert report.ok and report.linked_pages == 1 and report.copied_pages == 0
    with DB(tmp_path / "new") as db:
        linked = data_files(db, "x")[0]
        assert path.samefile(linked)
        db.store({"x": ([2], [99])})
        assert db.get_signal("x")[1].tolist() == [10, 99]
    # Copy-on-write in the destination must leave linked source measurements intact.
    assert read_page(path, full=True).batch().values.tolist() == [10, 20]


def test_hardlink_failure_has_no_implicit_copy(tmp_path, monkeypatch):
    from pagestore.backends import FileBackend

    with DB(tmp_path / "source") as db:
        db.store({"x": ([1], [2])})

    def reject(*args):
        raise OSError(errno.EXDEV, "different filesystems")

    monkeypatch.setattr(FileBackend, "link", reject)
    report = salvage(tmp_path / "source", tmp_path / "new", reuse="hardlink")
    assert not report.ok and report.salvaged_pages == report.copied_pages == 0
    assert "different filesystems" in report.errors[0]


def test_uncommitted_intact_upload_is_explicit_salvage(tmp_path):
    from pagestore.catalog import signal_id
    from pagestore.model import Batch
    from pagestore.page_format import data_plan
    from uuid import uuid4

    flat = tmp_path / "flat"
    flat.mkdir()
    plan = data_plan(
        Batch(np.array([1, 2]), np.array([3, 4])),
        {
            "database_id": uuid4().hex,
            "signal_name": "orphan",
            "signal_id": signal_id("orphan"),
            "time_kind": "int64",
            "page_id": uuid4().hex,
            "ordinal": 0,
            "max_page_size": 8192,
        },
    )
    (flat / "orphan.pg").write_bytes(plan.to_bytes())
    assert not recover(flat, tmp_path / "strict").complete
    report = salvage(flat, tmp_path / "new")
    assert report.ok and report.commit_status == "unknown"
    assert report.salvaged_signals == ["orphan"]


def test_multiple_database_ids_are_not_merged_and_sources_protected(tmp_path):
    flat = tmp_path / "flat"
    flat.mkdir()
    for i in range(2):
        with DB(tmp_path / str(i)) as db:
            db.store({"x": ([i], [i])})
            shutil.copyfile(data_files(db, "x")[0], flat / f"{i}.pg")
    report = salvage(flat, tmp_path / "new")
    assert not report.ok and "multiple databases" in report.errors[0]
    assert not (tmp_path / "new/store.json").exists()
    assert salvage(flat, tmp_path / "one", pages=["0.pg"]).ok
    with pytest.raises(FileExistsError):
        salvage(flat, flat / "nested")
    with pytest.raises(FileExistsError):
        salvage(flat, tmp_path / "one")
    with pytest.raises(ValueError):
        salvage(flat, tmp_path / "escape", pages=["../one/store.json"])
