"""The job table of ``tow run`` (tow.supervisor.jobs) against the commands its children run.

The supervisor starts ``python -m tow <argv>`` for each child job; the scope flags that tell a
check which part to do (``--global-only``, ``--timer-only``, ``--progress-only``,
``--space-only``) are hidden from ``--help``, so nothing but this test notices a row whose
arguments ``tow`` no longer accepts, or that names the wrong scope.
"""

from __future__ import annotations

import pytest

from tow import cli
from tow.supervisor import core
from tow.supervisor.jobs import BY_NAME, JOBS

SCOPES = ("global_only", "timer_only", "progress_only", "space_only", "site_retry_only")
# What each child job must be: its command and the one scope flag it sets (None: not a check).
EXPECTED = {
    "check": ("check", "global_only"),
    "timer": ("check", "timer_only"),
    "progress": ("check", "progress_only"),
    "space": ("check", "space_only"),
    "backup": ("backup", None),
    "site_retry": ("check", "site_retry_only"),
}
CHILDREN = [job for job in JOBS if job.argv is not None]


def test_every_child_job_is_expected():
    assert sorted(job.name for job in CHILDREN) == sorted(EXPECTED)


@pytest.mark.parametrize("job", CHILDREN, ids=lambda job: job.name)
def test_the_arguments_of_a_child_job_parse_and_set_its_scope(job):
    assert job.argv is not None
    command, scope = EXPECTED[job.name]
    args = cli._build_parser().parse_args(list(job.argv))
    assert args.cmd == command
    assert args.json is True  # the supervisor reads the child's result as JSON
    if command == "check":
        assert {name for name in SCOPES if getattr(args, name)} == {scope}
        # A scheduled child applies and notifies, and is never counted as run by hand.
        assert (args.apply, args.notify, args.dry_run, args.manual) == (True, True, False, False)


def test_the_supervisor_runs_the_table():
    assert len(BY_NAME) == len(JOBS)  # one row per name
    argv = {job.name: list(job.argv) for job in CHILDREN if job.argv is not None}
    timeouts = {job.name: job.timeout_sec for job in CHILDREN}
    assert (argv, timeouts) == (core.JOB_ARGS, core.JOB_TIMEOUT_SEC)
    assert all(timeout and timeout > 0 for timeout in core.JOB_TIMEOUT_SEC.values())


def test_the_watchdog_runs_inside_the_supervisor():
    watchdog = BY_NAME["watchdog"]
    assert watchdog.argv is None
    assert watchdog.timeout_sec is None
    assert "watchdog" not in core.JOB_ARGS
    assert "watchdog" not in core.JOB_TIMEOUT_SEC


def test_the_priority_and_the_saved_keys():
    # The first due job starts: a check before a night copy, before the passes.
    assert [job.name for job in JOBS] == ["check", "backup", "progress", "watchdog", "timer", "space", "site_retry"]
    # data/run/schedule.json of a running install keeps these keys: renaming one loses the cadence.
    saved = {job.name: job.persisted_key for job in JOBS if job.persisted_key}
    assert saved == {"check": "check_started_at", "backup": "backup_attempt_at"}
