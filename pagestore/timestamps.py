"""Exact numeric axes and timezone-aware conversion to UTC nanoseconds."""

from datetime import datetime, timezone as dt_timezone
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np

UTC = dt_timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MIN_NS, MAX_NS = -(2**63) + 1, 2**63 - 1
KINDS = {
    "int64": np.dtype("<i8"),
    "float64": np.dtype("<f8"),
    "datetime64[ns]": np.dtype("datetime64[ns]"),
}


def resolve_timezone(timezone="utc"):
    """Resolve UTC, CERN, local, or an IANA timezone without a fixed-offset guess."""
    if not isinstance(timezone, str):
        raise TypeError("timezone must be 'utc', 'cern', 'local', or an IANA name")
    key = timezone.lower()
    if key == "utc":
        return UTC
    if key == "cern":
        return ZoneInfo("Europe/Zurich")
    if key == "local":
        setting = os.environ.get("TZ")
        if setting:
            setting = setting.removeprefix(":")
            if Path(setting).is_absolute():
                with open(setting, "rb") as f:
                    return ZoneInfo.from_file(f)
            if setting.upper() in {"UTC", "UTC0", "GMT", "GMT0"}:
                return UTC
            try:
                return ZoneInfo(setting)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(
                    "Local TZ must name an IANA timezone or a zoneinfo file"
                ) from exc
        try:
            with open("/etc/localtime", "rb") as f:
                return ZoneInfo.from_file(f)
        except OSError as exc:
            raise ValueError(
                "Cannot determine local timezone; use an IANA timezone explicitly"
            ) from exc
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone!r}") from exc


def _localize(value, zone):
    # Round-trip both folds to reject nonexistent and ambiguous wall times.
    candidates = []
    for fold in (0, 1):
        aware = value.replace(tzinfo=zone, fold=fold)
        if aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == value:
            candidates.append(aware)
    if not candidates:
        raise ValueError(
            f"Nonexistent local timestamp: {value}; specify a valid time with an offset"
        )
    if len(candidates) == 2 and candidates[0].utcoffset() != candidates[1].utcoffset():
        raise ValueError(
            f"Ambiguous local timestamp: {value}; specify an explicit UTC offset"
        )
    return candidates[0]


def _datetime_ns(value, zone):
    remainder = 0
    if isinstance(value, (str, np.str_)):
        value = str(value).strip()
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d{1,9})?)?(?:[Zz]|[+-]\d{2}:\d{2})?)?",
            value,
        ):
            raise ValueError(
                f"Invalid ISO timestamp or precision finer than nanoseconds: {value!r}"
            )
        # datetime retains six fractional digits; preserve the remaining nanoseconds.
        match = re.search(r"([Tt ]\d{2}:\d{2}:\d{2})[.,](\d+)", value)
        if match:
            fraction = match.group(2)
            if len(fraction) > 9:
                raise ValueError(
                    "Timestamp precision finer than nanoseconds is unsupported"
                )
            remainder = int(fraction.ljust(9, "0")) % 1000
        try:
            value = datetime.fromisoformat(
                value.replace("Z", "+00:00").replace("z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError(f"Invalid ISO timestamp: {value!r}") from exc
    if not isinstance(value, datetime):
        raise TypeError(
            "Datetime axes require datetime, ISO strings, or numpy datetime64"
        )
    if value.tzinfo is None or value.utcoffset() is None:
        value = _localize(value, zone)
    delta = value.astimezone(UTC) - EPOCH
    ns = (
        (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    ) * 1000 + remainder
    if not MIN_NS <= ns <= MAX_NS:
        raise ValueError("Datetime is outside the datetime64[ns] range")
    return ns


def _numpy_datetimes(values):
    if np.isnat(values).any():
        raise ValueError("NaT timestamps are not allowed")
    unit, step = np.datetime_data(values.dtype)
    if unit in {"Y", "M"}:
        # Check before the day conversion, which otherwise can wrap huge years.
        lo = np.datetime64("1677-09-21").astype(values.dtype)
        hi = np.datetime64("2262-04-12").astype(values.dtype)
        if np.any(values < lo) or np.any(values > hi):
            raise ValueError("Datetime is outside the datetime64[ns] range")
        values = values.astype("datetime64[D]")
        unit, step = "D", 1
    raw = values.astype(values.dtype.newbyteorder("="), copy=False).view("i8")
    factors = {
        "W": 604800_000_000_000,
        "D": 86400_000_000_000,
        "h": 3600_000_000_000,
        "m": 60_000_000_000,
        "s": 1_000_000_000,
        "ms": 1_000_000,
        "us": 1000,
        "ns": 1,
    }
    if unit in factors:
        factor = factors[unit] * step
        if raw.size and (
            int(raw.min()) * factor < MIN_NS or int(raw.max()) * factor > MAX_NS
        ):
            raise ValueError("Datetime conversion would overflow")
        result = raw * factor
    elif unit in {"ps", "fs", "as"}:
        divisor = {"ps": 1000, "fs": 1_000_000, "as": 1_000_000_000}[unit]
        # Uncommon sub-nanosecond units, including multi-unit dtypes, need exact arithmetic.
        ints = [int(x) * step for x in raw]
        if any(x % divisor or not MIN_NS <= x // divisor <= MAX_NS for x in ints):
            raise ValueError("Datetime conversion would lose precision or overflow")
        result = np.array([x // divisor for x in ints], dtype="i8")
    else:
        raise ValueError(f"Unsupported datetime unit: {unit}")
    return np.asarray(result, dtype="i8").view("datetime64[ns]")


def normalize_timestamps(values, kind=None, timezone="utc"):
    arr = np.asarray(values)
    if arr.ndim != 1:
        raise ValueError("timestamps must be one-dimensional")
    if arr.size == 0 and kind is not None:
        if kind not in KINDS:
            raise ValueError(f"Unknown timestamp kind: {kind}")
        return np.empty(0, dtype=KINDS[kind].newbyteorder("=")), kind
    if arr.dtype.kind == "M":
        result = _numpy_datetimes(arr)
        inferred = "datetime64[ns]"
    elif arr.dtype.kind in "US" or (arr.dtype.kind == "O" and len(arr)):
        zone = resolve_timezone(timezone)
        result = np.array([_datetime_ns(x, zone) for x in arr], dtype="i8").view(
            "datetime64[ns]"
        )
        inferred = "datetime64[ns]"
    elif arr.dtype.kind in "iu":
        if arr.dtype.kind == "u" and arr.size and int(arr.max()) > MAX_NS:
            raise ValueError("Unsigned timestamps must fit int64")
        result = arr.astype("i8", copy=False)
        inferred = "int64"
    elif arr.dtype.kind == "f" and arr.dtype.itemsize <= 8:
        result = arr.astype("f8", copy=False)
        if not np.isfinite(result).all():
            raise ValueError("Timestamps must be finite")
        if np.signbit(result[result == 0]).any():
            result = result.copy()
            result[result == 0] = 0.0
        inferred = "float64"
    else:
        raise TypeError(f"Unsupported timestamp dtype: {arr.dtype}")
    if kind is None or kind == inferred:
        return result, inferred
    if "datetime64[ns]" in (kind, inferred):
        raise TypeError(
            "Cannot mix numeric and datetime timestamps; convert epoch units explicitly"
        )
    if kind == "int64":
        if (
            np.any(result < -(2**63))
            or np.any(result >= 2**63)
            or np.any(result != np.floor(result))
        ):
            raise ValueError("Timestamps are not exactly representable as int64")
        return result.astype("i8"), kind
    if kind == "float64":
        converted = result.astype("f8")
        if result.size and (int(result.min()) < -(2**53) or int(result.max()) > 2**53):
            if any(int(f) != int(i) for f, i in zip(converted, result)):
                raise ValueError("Timestamps are not exactly representable as float64")
        return converted, kind
    raise ValueError(f"Unknown timestamp kind: {kind}")


def bound(value, kind, timezone="utc"):
    if value is None:
        return None
    result, _ = normalize_timestamps([value], kind, timezone)
    return result[0]


def encode_time(value, kind):
    if kind == "datetime64[ns]":
        return str(int(np.asarray(value, dtype="datetime64[ns]").view("i8")))
    return float(value).hex() if kind == "float64" else str(int(value))


def decode_time(value, kind):
    if kind == "datetime64[ns]":
        return np.datetime64(int(value), "ns")
    return float.fromhex(value) if kind == "float64" else int(value)
