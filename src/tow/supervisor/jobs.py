"""The jobs of ``tow run``, one row each: what runs, for how long, how often, what it keeps.

- ``argv``: the arguments of its child process (``python -m tow ...``); None for the watchdog,
  a pass inside the supervisor;
- ``timeout_sec``: a child still running after this long is stopped;
- ``every_sec`` / ``first_delay_sec``: a fixed cadence, and the first run after the start.
  None where the time comes from elsewhere: the check every ``interval_sec`` of the config,
  the night copy at its daily slot, the personal timers at each topic's own minutes;
- ``persisted_key``: the key in ``data/run/schedule.json`` that keeps its last scheduled start
  across a restart.

This table holds data only and imports nothing from TOW.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class JobSpec:
    name: str
    argv: tuple[str, ...] | None
    timeout_sec: float | None
    every_sec: int | None
    first_delay_sec: int | None
    persisted_key: str | None = None


JOBS: tuple[JobSpec, ...] = (
    JobSpec(
        "check",
        ("check", "--apply", "--notify", "--global-only", "--json"),
        timeout_sec=3600.0,
        every_sec=None,  # interval_sec of the config
        first_delay_sec=120,
        persisted_key="check_started_at",
    ),
    JobSpec(
        "backup",
        ("backup", "--json"),
        timeout_sec=1800.0,
        every_sec=None,  # once per local day at backup_time
        first_delay_sec=None,
        persisted_key="backup_attempt_at",
    ),
    JobSpec(
        "progress",
        ("check", "--apply", "--notify", "--progress-only", "--json"),
        timeout_sec=600.0,
        every_sec=30 * 60,
        first_delay_sec=5 * 60,
    ),
    JobSpec("watchdog", None, timeout_sec=None, every_sec=10 * 60, first_delay_sec=60),
    JobSpec(
        "timer",
        ("check", "--apply", "--notify", "--timer-only", "--json"),
        timeout_sec=3600.0,
        every_sec=None,  # each topic's own minutes
        first_delay_sec=None,
    ),
    JobSpec(
        "space",
        ("check", "--apply", "--notify", "--space-only", "--json"),
        timeout_sec=600.0,
        every_sec=5 * 60,
        first_delay_sec=5 * 60,
    ),
)

BY_NAME: Mapping[str, JobSpec] = MappingProxyType({job.name: job for job in JOBS})
