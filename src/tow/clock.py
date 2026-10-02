from __future__ import annotations

import re
from datetime import UTC, datetime, tzinfo
from typing import Any

# Zone names that already say "UTC"; the label must not repeat it ("UTC UTC+00:00").
_UTC_NAMES = frozenset({"UTC", "GMT", "Z", "Coordinated Universal Time", "Greenwich Mean Time"})
# A zone's short name ("EDT", "CEST", "MSK"); "+03" style names and long Windows names are not.
_ABBREVIATION = re.compile(r"[A-Z]{2,5}")


def local_zone() -> tzinfo | None:
    """The zone TOW shows and stamps local times in; None means this machine's zone.

    The single seam for the wall-clock zone: tests pin it so their results never
    depend on where the suite runs.
    """
    return None


def machine_now() -> datetime:
    """Return the real local wall-clock time of this machine."""
    return datetime.now(UTC).astimezone(local_zone())


def iso_now() -> str:
    return machine_now().isoformat(timespec="seconds")


def iso_from_epoch(value: Any) -> str | None:
    try:
        timestamp = float(value)
    except TypeError, ValueError:
        return None
    if timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, UTC).astimezone(local_zone()).isoformat(timespec="seconds")
    except OSError, OverflowError, ValueError:
        return None


def parse_timestamp(value: datetime | str) -> datetime:
    """An aware datetime. A naive value (written by an old TOW) is local wall-clock time in
    the zone of that moment - not today's offset, which is an hour off across a DST change."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        zone = local_zone()
        dt = dt.replace(tzinfo=zone) if zone is not None else dt.astimezone()
    return dt


def machine_timezone_label(value: datetime | None = None) -> str:
    """The zone of a local time: its offset from UTC, after the zone's own short name when it
    has one ("EDT UTC-04:00", "UTC+05:30", "UTC"); a long Windows name is left out."""
    dt = (value or machine_now()).astimezone(local_zone())
    offset = dt.utcoffset()
    seconds = int(offset.total_seconds()) if offset is not None else 0
    sign = "+" if seconds >= 0 else "-"
    hours, remainder = divmod(abs(seconds), 3600)
    utc = "UTC" if seconds == 0 else f"UTC{sign}{hours:02d}:{remainder // 60:02d}"
    name = dt.tzname() or ""
    if name in _UTC_NAMES or not _ABBREVIATION.fullmatch(name):
        return utc
    return f"{name} {utc}"


def format_ui_timestamp(value: datetime | str, lang: str | None = None) -> str:
    """A local time as the owner's language writes dates (``_meta.datetime``), with its zone."""
    from tow import i18n

    dt = parse_timestamp(value).astimezone(local_zone())
    return f"{i18n.format_datetime(dt, lang)} {machine_timezone_label(dt)}"
