from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from pagestore import DB
from pagestore.timestamps import normalize_timestamps


def test_utc_default_and_nanoseconds(tmp_path):
    with DB(tmp_path / "db") as db:
        db.store(
            {
                "x": (
                    [
                        "2024-01-01 12:00:00.123456789",
                        "2024-01-01T13:00:00.123456790+01:00",
                    ],
                    [1, 2],
                )
            }
        )
        times, values = db.get_signal("x", "2024-01-01T13:00:00+01:00")
        np.testing.assert_array_equal(
            times,
            np.array(
                ["2024-01-01T12:00:00.123456789", "2024-01-01T12:00:00.123456790"],
                dtype="datetime64[ns]",
            ),
        )
        np.testing.assert_array_equal(values, [1, 2])
        assert db.count_signal("x", "2024-01-01T12:00:00.123456790Z") == 1


@pytest.mark.parametrize(
    "date,expected",
    [
        ("2024-01-01 12:00:00", "2024-01-01T11:00:00"),
        ("2024-07-01 12:00:00", "2024-07-01T10:00:00"),
    ],
)
def test_cern_and_local(tmp_path, monkeypatch, date, expected):
    monkeypatch.setenv("TZ", "Europe/Zurich")
    for zone in ("cern", "local", "Europe/Zurich"):
        result, kind = normalize_timestamps([date], timezone=zone)
        assert kind == "datetime64[ns]"
        assert result[0] == np.datetime64(expected, "ns")
    with DB(tmp_path / "db", timezone="cern") as db:
        db.store({"x": ([date], [1])})
        assert db.count_signal("x", date, date) == 1
        assert db.count_signal("x", expected, expected, timezone="utc") == 1


@pytest.mark.parametrize(
    "date,reason",
    [("2024-03-31 02:30:00", "Nonexistent"), ("2024-10-27 02:30:00", "Ambiguous")],
)
def test_dst_invalid_wall_times(date, reason):
    with pytest.raises(ValueError, match=reason):
        normalize_timestamps([date], timezone="cern")


def test_dst_explicit_offsets_and_aware_fold():
    times, _ = normalize_timestamps(
        ["2024-10-27T02:30:00+02:00", "2024-10-27T02:30:00+01:00"], timezone="cern"
    )
    assert times[1] - times[0] == np.timedelta64(1, "h")
    aware = [
        datetime(2024, 10, 27, 2, 30, tzinfo=ZoneInfo("Europe/Zurich"), fold=i)
        for i in (0, 1)
    ]
    np.testing.assert_array_equal(normalize_timestamps(aware)[0], times)


@pytest.mark.parametrize(
    "values",
    [
        np.array([2**63], dtype="u8"),
        [float("nan")],
        [float("inf")],
        np.array(["NaT"], dtype="datetime64[ns]"),
        np.array(["2500-01-01"], dtype="datetime64[D]"),
        np.array([1], dtype="datetime64[ps]"),
        ["2024-01-01T00:00:00.1234567891"],
        ["2500-01-01"],
    ],
)
def test_invalid_timestamps(values):
    with pytest.raises((ValueError, TypeError)):
        normalize_timestamps(values)


def test_exact_numeric_and_datetime_conversions():
    assert normalize_timestamps([1.0, 2.0], "int64")[0].dtype == np.dtype("i8")
    with pytest.raises(ValueError):
        normalize_timestamps([1.5], "int64")
    with pytest.raises(ValueError):
        normalize_timestamps([2**53 + 1], "float64")
    with pytest.raises(TypeError):
        normalize_timestamps([1], "datetime64[ns]")
    for dtype in ("datetime64[us]", ">M8[us]", "datetime64[2s]"):
        original = np.array(["2020-01-01"], dtype=dtype)
        assert normalize_timestamps(original)[0][0] == np.datetime64("2020-01-01", "ns")
    assert not np.signbit(normalize_timestamps([-0.0])[0][0])


def test_invalid_timezone_even_for_empty_input(tmp_path):
    with pytest.raises(ValueError, match="timezone"):
        DB(tmp_path / "db", timezone="not/a/timezone")
