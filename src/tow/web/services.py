"""What the web pages call outside the web package: the stores, the check, the service lifecycle,
restore points and portable bundles, diagnostics, the browser sign-in and the event log.

This is the web layer's one seam. Route modules and view helpers call these through this
module - ``services.load_config()``, ``services.run_check(...)`` - never by importing them from
their home modules, so a test replaces one in one place and every page sees it::

    monkeypatch.setattr("tow.web.services.run_check", fake_check)

Pure helpers (formatting, parsing, validation) are imported from their own modules as usual;
anything that reads or writes the install's data, talks to a client or a site, or changes the
service belongs here.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps

from tow.browser_auth import browser_auth
from tow.check import record_check_failure, run_check
from tow.config import load_config, save_config
from tow.doctor import doctor_report
from tow.lifecycle import request_restart, service_status, set_autostart
from tow.log import log_event
from tow.ratelimit import LoginThrottle
from tow.releases import release_status
from tow.restore_points import (
    check_portable_bundle,
    create_restore_point,
    export_portable_bundle,
    list_restore_points,
    restore_from_point,
    restore_portable_bundle,
)
from tow.restore_points import cleanup_status as restore_point_cleanup_status
from tow.snapshots import cleanup_status as night_cleanup_status
from tow.store import (
    load_download_history,
    load_secrets,
    load_state,
    persistence_lock,
    save_secrets,
    save_state,
)
from tow.store_transaction import recover as recover_store_transaction
from tow.supervisor.layout import next_check_at
from tow.undo import cleanup as cleanup_secret_undo
from tow.web_update import log_tail as web_update_log
from tow.web_update import start as start_web_update
from tow.web_update import status as web_update_status

__all__ = [
    "browser_auth",
    "check_portable_bundle",
    "cleanup_secret_undo",
    "create_restore_point",
    "doctor_report",
    "export_portable_bundle",
    "list_restore_points",
    "load_config",
    "load_download_history",
    "load_secrets",
    "load_state",
    "locked_state_mutation",
    "log_event",
    "login_throttle",
    "next_check_at",
    "night_cleanup_status",
    "persistence_lock",
    "record_check_failure",
    "recover_store_transaction",
    "release_status",
    "request_restart",
    "restore_from_point",
    "restore_point_cleanup_status",
    "restore_portable_bundle",
    "run_check",
    "save_config",
    "save_secrets",
    "save_state",
    "service_status",
    "set_autostart",
    "start_web_update",
    "web_update_log",
    "web_update_status",
]

# Failed sign-ins and password checks, per address and in total: one budget for the sign-in
# page and the password card (a test gets a fresh one).
login_throttle = LoginThrottle()


def locked_state_mutation[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """A route that reads, changes and writes the stores holds the persistence lock throughout,
    so a check finishing meanwhile cannot be lost (or lose the owner's change)."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with persistence_lock():
            return function(*args, **kwargs)

    return wrapped
