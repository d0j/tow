"""The supervisor's schedule on a fake clock, pinned as text: ``tests/golden/schedule-grid.txt``.

Each scenario builds a ``Schedule``, then ticks through its times. At every tick the line shows
what ``due()`` returns (in its order, with how late each job is) and ``next_due()`` (in its key
order); every due job is then started as the supervisor starts it, and its facts are recorded
(a night copy that the scenario fails is an attempt without a good copy). Times are local, in
the owner's zone. The scenarios cross both DST switches of 2026 in Israel (a slot in the skipped
hour, a slot in the repeated hour), a machine asleep over the night slot, a failed copy and its
retry, personal timers, the disk-space pass, and night copies turned off.

A change of the schedule's behaviour changes this text. If intended, regenerate it and review
the diff:

    uv run --frozen python tests/test_golden_schedule.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tow.supervisor.schedule import Schedule

GOLDEN = Path(__file__).resolve().parent / "golden" / "schedule-grid.txt"
ZONE = ZoneInfo("Asia/Jerusalem")
MINUTE = 60


def at(text: str) -> float:
    """Epoch seconds of a local wall time "YYYY-MM-DD HH:MM" (the first one, if repeated)."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=ZONE).timestamp()


def shown(ts: float) -> str:
    return datetime.fromtimestamp(ts, ZONE).strftime("%m-%d %H:%M:%S%z")


@dataclass
class Scenario:
    name: str
    what: str
    start: str
    ticks: list[float]
    interval_sec: int = 3600
    backup_at: tuple[int, int] = (3, 30)
    backup_enabled: bool = True
    timers: dict[str, dict[str, Any]] = field(default_factory=dict)
    space_until: float = 0.0  # a topic waits for disk space until then
    failed_copies: int = 0  # the first this many night copies fail


def _every(first: str, last: str, minutes: int) -> list[float]:
    begin, end = at(first), at(last)
    return [begin + step * minutes * MINUTE for step in range(int((end - begin) // (minutes * MINUTE)) + 1)]


SCENARIOS = [
    Scenario(
        "dst-spring",
        "the night of 27 March 2026: 02:00 jumps to 03:00; the 02:30 slot maps to 03:30",
        start="2026-03-26 23:50",
        ticks=_every("2026-03-26 23:50", "2026-03-27 04:50", 20),
        backup_at=(2, 30),
    ),
    Scenario(
        "dst-autumn",
        "the night of 25 October 2026: 02:00 goes back to 01:00; the 01:30 slot is the first one",
        start="2026-10-24 23:50",
        ticks=_every("2026-10-24 23:50", "2026-10-25 03:50", 20),
        backup_at=(1, 30),
    ),
    Scenario(
        "asleep-over-the-slot",
        "asleep from 23:15 to 06:10: the missed copy and the overdue jobs run at the wake; then a day",
        start="2026-06-10 22:00",
        ticks=[
            *_every("2026-06-10 22:00", "2026-06-10 23:15", 15),
            *_every("2026-06-11 06:10", "2026-06-11 07:10", 15),
        ],
    ),
    Scenario(
        "failed-copy-retry",
        "the 03:30 copy fails: tried again six hours later, at 09:30",
        start="2026-06-11 03:00",
        ticks=_every("2026-06-11 03:00", "2026-06-11 10:00", 30),
        interval_sec=4 * 3600,
        failed_copies=1,
    ),
    Scenario(
        "personal-timers",
        "two personal timers (20 and 45 minutes) beside the hourly check",
        start="2026-06-10 12:00",
        ticks=_every("2026-06-10 12:00", "2026-06-10 13:40", 5),
        timers={
            "t1": {"minutes": 20, "revision": "r1", "set_at": at("2026-06-10 11:50")},
            "t2": {"minutes": 45, "revision": "r2", "set_at": at("2026-06-10 12:00")},
        },
    ),
    Scenario(
        "space-waiting",
        "a topic waits for disk space until 12:30: the space pass every 5 minutes, then none",
        start="2026-06-10 12:00",
        ticks=_every("2026-06-10 12:00", "2026-06-10 12:45", 5),
        space_until=at("2026-06-10 12:30"),
    ),
    Scenario(
        "backup-off",
        "night copies turned off: no backup key, nothing due at 03:30",
        start="2026-06-11 02:50",
        ticks=_every("2026-06-11 02:50", "2026-06-11 04:10", 20),
        backup_enabled=False,
    ),
]


def run(scenario: Scenario) -> list[str]:
    started = at(scenario.start)
    schedule = Schedule(
        started_at=started,
        interval_sec=scenario.interval_sec,
        backup_at=scenario.backup_at,
        zone=ZONE,
        backup_enabled=scenario.backup_enabled,
        topic_timers={tid: dict(item) for tid, item in scenario.timers.items()},
    )
    # The night copy of the slot before the start is there.
    facts = {
        "last_scheduled_check": 0.0,
        "last_backup_ok": schedule.previous_slot(started),
        "last_backup_attempt": schedule.previous_slot(started),
    }
    failures = scenario.failed_copies
    lines = [f"# {scenario.name}: {scenario.what}"]
    for now in scenario.ticks:
        schedule.space_waiting = now < scenario.space_until
        upcoming = schedule.next_due(now, **facts)
        due = schedule.due(now, **facts)
        jobs = " ".join(f"{name}+{late:.0f}s" for name, late in due) or "-"
        planned = " ".join(f"{name}={shown(when)}" for name, when in upcoming.items())
        lines.append(f"{shown(now)}  due: {jobs}  |  next: {planned}")
        for name, late in due:
            if name == "timer":
                for tid, when in schedule.timer_due(now).items():
                    if when <= now:
                        item = schedule.topic_timers[tid]
                        schedule.timer_attempts[tid] = {
                            "revision": item["revision"],
                            "minutes": item["minutes"],
                            "started_at": now,
                        }
            schedule.started(name, now, late)
            if name == "check":
                facts["last_scheduled_check"] = schedule.last["check"]
            elif name == "backup":
                facts["last_backup_attempt"] = now
                if failures:
                    failures -= 1
                else:
                    facts["last_backup_ok"] = now
    return lines


def grid() -> str:
    return "\n\n".join("\n".join(run(scenario)) for scenario in SCENARIOS) + "\n"


def test_the_schedule_grid_is_pinned():
    expected = GOLDEN.read_text(encoding="utf-8")
    actual = grid()
    if actual != expected:
        changed = [
            f"- {old}\n+ {new}"
            for old, new in zip(expected.splitlines(), actual.splitlines(), strict=False)
            if old != new
        ][:10]
        raise AssertionError(
            "the schedule changed; if intended, regenerate and review the diff "
            "(uv run --frozen python tests/test_golden_schedule.py):\n" + "\n".join(changed)
        )


def test_the_grid_crosses_what_it_says():
    text = grid()
    assert "03-27 03:30:00+0300  due: backup" in text  # the skipped 02:30 slot, an hour later
    assert "10-25 01:30:00+0300  due: backup" in text  # the repeated 01:30: its first occurrence
    assert "+0200" in text.split("# dst-autumn")[1]  # the clock really went back
    assert "06-11 06:10:00+0300  due: check" in text  # the wake runs the overdue jobs
    assert "timer=" in text
    assert "space=" in text
    backup_off = text.split("# backup-off")[1].split("\n", 1)[1]  # its ticks, without the title
    assert backup_off.count("next: check=") == 5
    assert "backup" not in backup_off


if __name__ == "__main__":
    GOLDEN.parent.mkdir(exist_ok=True)
    GOLDEN.write_text(grid(), encoding="utf-8", newline="\n")
    print(f"wrote {GOLDEN}")
