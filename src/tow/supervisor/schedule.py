"""The supervisor's own schedule: what is due now, from facts that survive a restart.

Pure logic with the time passed in, so a fake clock tests it (days, sleeps and DST switches
in microseconds):

- ``check``    every ``interval_sec`` after the last scheduled check (the supervisor's own last
  start, kept in ``data/run/schedule.json``; ``health.auto_at_ts`` until there is one), the
  first two minutes after start;
- ``progress`` every 30 minutes (completions from the torrent clients, no site traffic);
- ``backup``   once per local calendar day at ``backup_time`` (default 03:30); a slot that
  passed without a good copy (the machine was off or asleep, or the copy failed) is caught up
  at once, a failed copy is tried again six hours later (or at the next slot, if sooner);
- ``watchdog`` every 10 minutes (lateness, night copies, change alerts, heartbeat, messages).

Times are epoch seconds (no DST in them); only the daily slot is a local wall time, built
for each calendar day in the owner's zone (PEP 495: a time skipped by the spring switch maps an
hour later, a time the autumn switch repeats is its first occurrence), so a DST switch neither
skips nor repeats a night: a copy is due only when the newest good one is older than the latest
slot, never because 24 hours have passed (the autumn day has 25). A wall clock moved backwards
never stalls a job: a "last run" in the future counts as the first observation of that
correction, not the continually advancing current tick.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo

from tow.config import DEFAULT_BACKUP_TIME, parse_backup_time

__all__ = ["DEFAULT_BACKUP_TIME", "Schedule", "WakeDetector", "local_day", "local_slot", "parse_backup_time"]

CHECK_FIRST_DELAY_SEC = 120
PROGRESS_EVERY_SEC = 30 * 60
PROGRESS_FIRST_DELAY_SEC = 5 * 60
WATCHDOG_EVERY_SEC = 10 * 60
WATCHDOG_FIRST_DELAY_SEC = 60
# A failed night copy is tried again after this long (not at every start or tick).
BACKUP_RETRY_SEC = 6 * 3600


def local_slot(day: date, at: tuple[int, int], zone: tzinfo | None) -> float:
    """The epoch moment of ``at`` local time on ``day``.

    PEP 495 rules (``fold=0``) for the owner's zone or, with ``zone=None``, this machine's: a
    time the spring switch skips maps an hour later, one the autumn switch repeats is the first.
    """
    return datetime(day.year, day.month, day.day, at[0], at[1], tzinfo=zone, fold=0).timestamp()


def local_day(ts: float, zone: tzinfo | None) -> date:
    moment = datetime.fromtimestamp(ts, UTC)
    return (moment.astimezone() if zone is None else moment.astimezone(zone)).date()


@dataclass
class Schedule:
    """Decides which jobs are due; the supervisor runs them one at a time."""

    started_at: float
    interval_sec: int = 3600
    backup_at: tuple[int, int] = DEFAULT_BACKUP_TIME
    zone: tzinfo | None = None
    last: dict[str, float] = field(default_factory=dict)  # this process's last start of each job
    _corrected: dict[str, tuple[float, float]] = field(default_factory=dict, init=False, repr=False)

    def _clamp(self, ts: float, now: float, *, source: str) -> float:
        # Pin a future fact once. Repeated min(ts, now) moves the deadline on every
        # tick and starves the job until the machine catches up with the old clock.
        previous = self._corrected.get(source)
        if ts and previous is not None and previous[0] == ts:
            anchor = min(previous[1], now)  # another backward correction
            self._corrected[source] = (ts, anchor)
            return anchor
        if ts > now:
            self._corrected[source] = (ts, now)
            return now
        self._corrected.pop(source, None)
        return ts if ts else 0.0

    def check_due_at(self, now: float, last_scheduled_check: float) -> float:
        last = max(
            self._clamp(last_scheduled_check, now, source="check:persisted"),
            self._clamp(self.last.get("check", 0.0), now, source="check:own"),
        )
        if not last:
            return self._clamp(self.started_at, now, source="startup") + CHECK_FIRST_DELAY_SEC
        return last + self.interval_sec

    def _every_due_at(self, name: str, every: float, first_delay: float, now: float) -> float:
        last = self._clamp(self.last.get(name, 0.0), now, source=f"{name}:own")
        return last + every if last else self._clamp(self.started_at, now, source="startup") + first_delay

    def _slot(self, day: date) -> float:
        return local_slot(day, self.backup_at, self.zone)

    def previous_slot(self, now: float) -> float:
        """The latest daily slot at or before ``now``."""
        today = local_day(now, self.zone)
        slot = self._slot(today)
        return slot if slot <= now else self._slot(today - timedelta(days=1))

    def next_slot(self, now: float) -> float:
        """The first daily slot after ``now``."""
        today = local_day(now, self.zone)
        slot = self._slot(today)
        return slot if slot > now else self._slot(today + timedelta(days=1))

    def backup_due_at(self, now: float, last_ok: float, last_attempt: float) -> float:
        """When the next night copy is due (``now`` or earlier means: run it)."""
        last_ok = self._clamp(last_ok, now, source="backup:ok")
        tried = max(
            self._clamp(last_attempt, now, source="backup:attempt"),
            self._clamp(self.last.get("backup", 0.0), now, source="backup:own"),
        )
        previous = self.previous_slot(now)
        if last_ok >= previous:
            return self.next_slot(now)  # the latest slot has its good copy
        if tried < previous:
            return previous  # the slot passed without an attempt (off, asleep): at once
        # The copy for the latest slot was tried and failed: again six hours later, or at the
        # next slot when that comes first. status.json shows this very time.
        return min(tried + BACKUP_RETRY_SEC, self.next_slot(now))

    def due(
        self, now: float, *, last_scheduled_check: float, last_backup_ok: float, last_backup_attempt: float
    ) -> list[tuple[str, float]]:
        """The due jobs, most important first, each with how late it is (seconds)."""
        due_at = {
            "check": self.check_due_at(now, last_scheduled_check),
            "backup": self.backup_due_at(now, last_backup_ok, last_backup_attempt),
            "progress": self._every_due_at("progress", PROGRESS_EVERY_SEC, PROGRESS_FIRST_DELAY_SEC, now),
            "watchdog": self._every_due_at("watchdog", WATCHDOG_EVERY_SEC, WATCHDOG_FIRST_DELAY_SEC, now),
        }
        return [(name, now - at) for name, at in due_at.items() if at <= now]

    def started(self, name: str, now: float) -> None:
        self._corrected.pop(f"{name}:own", None)
        if name == "check":
            self._corrected.pop("check:persisted", None)
        elif name == "backup":
            self._corrected.pop("backup:attempt", None)
        self.last[name] = now

    def next_due(self, now: float, **facts: float) -> dict[str, float]:
        """When each job is due next (for status.json and Settings)."""
        return {
            "check": self.check_due_at(now, facts.get("last_scheduled_check", 0.0)),
            "backup": self.backup_due_at(now, facts.get("last_backup_ok", 0.0), facts.get("last_backup_attempt", 0.0)),
            "progress": self._every_due_at("progress", PROGRESS_EVERY_SEC, PROGRESS_FIRST_DELAY_SEC, now),
            "watchdog": self._every_due_at("watchdog", WATCHDOG_EVERY_SEC, WATCHDOG_FIRST_DELAY_SEC, now),
        }


@dataclass
class WakeDetector:
    """A wall clock that jumped further than the monotonic one: the machine slept.

    The monotonic clock stops while the machine sleeps (Linux, macOS; on Windows the
    supervisor's loop simply does not run), the wall clock does not. ``woke_at`` is the last
    wake seen; watchdog lateness is measured from it.
    """

    tick_slack_sec: float = 90.0
    _last_wall: float | None = None
    _last_mono: float | None = None
    woke_at: float | None = None
    slept_sec: float = 0.0

    def observe(self, wall: float, mono: float) -> bool:
        woke = False
        if self._last_wall is not None and self._last_mono is not None:
            gap = (wall - self._last_wall) - (mono - self._last_mono)
            # The loop itself standing still for long (Windows keeps the monotonic clock
            # running in sleep) also shows as a long tick.
            stalled = mono - self._last_mono
            if gap > self.tick_slack_sec or stalled > max(self.tick_slack_sec, 300.0):
                self.woke_at = wall
                self.slept_sec = max(gap, stalled)
                woke = True
        self._last_wall, self._last_mono = wall, mono
        return woke
