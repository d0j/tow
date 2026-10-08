"""One check run: what it checks, its clients and messages, the commit of its result, the
health record of the header and the sites' daily download limits."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from tow import check_transaction, download_history
from tow.check import rows, space
from tow.check.client_ops import ClientPool, remote_clients
from tow.check.notices import RunNotifications, flush_notifications
from tow.check.reconcile import reconcile_and_commit, reconcile_preview
from tow.check.topic import CheckRun, check_topic, read_secrets
from tow.check_steps import changed_owner_fields, merge_check_results, owner_fields
from tow.clients import factory as client_factory
from tow.clock import iso_now, machine_now
from tow.config import load_config
from tow.i18n import t
from tow.jsonish import as_dict
from tow.log import error_class, log_event, owner_language
from tow.notify import NotificationBatch
from tow.records import CheckRow, Health, Topic, health_of, topics_of
from tow.store import SecretStoreError, check_run_lock, load_state, persistence_lock, save_state
from tow.trackers import load_trackers


def _check_failure_code(error: str | BaseException) -> str:
    """The header clock's reason: by the error's type (its text is in the owner's language);
    only a plain text (from an older caller) is matched by its words."""
    if isinstance(error, SecretStoreError):
        return "secrets_migration_required"
    if isinstance(error, BaseException):
        return error_class(error)
    message = str(error or "").lower()
    if "legacy plaintext" in message or "master key" in message or "secret" in message:
        return "secrets_migration_required"
    return error_class(error)


def _last_good_scheduled_check(health: Health) -> int | None:
    """When the last scheduled check completed (state written by an older TOW: its last
    scheduled attempt, unless that attempt was itself a recorded failure)."""
    if "auto_ok_at_ts" in health:
        value = health.get("auto_ok_at_ts")
        return int(value) if isinstance(value, (int, float)) else None
    value = health.get("auto_at_ts")
    return int(value) if isinstance(value, (int, float)) and health.get("check_ok") is not False else None


def record_check_failure(error: str | BaseException, *, how: str = "auto") -> Health:
    """Persist a failed apply attempt so the UI clock has a fresh attempt time."""
    code = _check_failure_code(error)
    now = machine_now()
    with persistence_lock():  # read-modify-write: never lose a concurrent edit
        state = load_state()
        previous = health_of(state)
        # A blocked check learned nothing about qBit or the bot: keep what the last real
        # check saw. Writing qbit_ok=False showed "связи нет" and swallowed the next
        # real "торрент-клиент недоступен" notification.
        health = Health()
        if "qbit" in previous:
            health["qbit"] = previous["qbit"]
        if "client" in previous:
            health["client"] = previous["client"]
        if "qbit_ok" in previous:
            health["qbit_ok"] = previous["qbit_ok"]
        if "clients_ok" in previous:
            health["clients_ok"] = previous["clients_ok"]
        health.update(
            {
                "check_ok": False,
                "check_error": code,
                "at": now.isoformat(timespec="seconds"),
                "at_ts": int(now.timestamp()),
                # The header countdown runs from the last scheduled *attempt*...
                "auto_at_ts": int(now.timestamp()) if how == "auto" else previous.get("auto_at_ts"),
                # ...the watchdog from the last scheduled check that really ran (M6): a check
                # failing on every run must not look like a healthy schedule.
                "auto_ok_at_ts": _last_good_scheduled_check(previous),
                "check_failures": int(previous.get("check_failures") or 0) + (1 if how == "auto" else 0),
            }
        )
        state["health"] = health
        save_state(state)
    log_event("check_blocked", reason=code, how=how)
    return health


def _progress_only_row(topic: Topic) -> dict[str, Any]:
    return {
        "id": topic.get("id"),
        "title": topic.get("title"),
        "url": topic.get("url"),
        "ok": True,
        "hash": topic.get("hash"),
        "changed": False,
        "status": "skipped",
        "skipped": t("check.progress_only", owner_language()),
    }


# How long the Home page says that the download history was started over.
HISTORY_REBUILT_NOTICE_SEC = 7 * 24 * 3600


def _note_history_rebuilt(health: Health, previous: Health, *, rebuilt: bool) -> None:
    if rebuilt:
        health["history_rebuilt_at"] = health["at_ts"]
        return
    earlier = previous.get("history_rebuilt_at")
    if isinstance(earlier, int) and health["at_ts"] - earlier < HISTORY_REBUILT_NOTICE_SEC:
        health["history_rebuilt_at"] = earlier


def run_check(
    *,
    apply: bool,
    notify: bool,
    ids: list[str] | None = None,
    ignore_cool: bool = False,
    how: str = "auto",
    progress_only: bool = False,
    wait: bool = True,
    scheduled_scope: str = "",
    space_only: bool = False,
) -> dict[str, Any]:
    """Run one check; applying checks never overlap (scheduled, progress, web, CLI).

    ``space_only``: the supervisor's space pass - only the topics whose revision waits in the
    client for disk space, asked of the client alone (``tow.check.space``), no site traffic.

    ``wait=False`` (web requests): raise ``CheckBusyError`` at once when another applying
    check runs, instead of holding the request - and every page behind it - for minutes.

    ``check_run_lock`` serializes applying checks across processes for the whole run. The
    shared persistence lock is taken only to read the state and to commit the result, so
    edits in the web UI, the watchdog and the night copy are not held up by network time;
    edits made meanwhile are kept (``merge_check_results``).
    """
    if scheduled_scope not in {"", "global", "timer"} or ((progress_only or space_only) and scheduled_scope):
        raise ValueError("invalid scheduled check scope")
    if not apply:
        return _run_check(
            apply=False,
            notify=notify,
            ids=ids,
            ignore_cool=ignore_cool,
            how=how,
            progress_only=progress_only,
            scheduled_scope=scheduled_scope,
            space_only=space_only,
        )
    with check_run_lock(wait=wait):
        return _run_check(
            apply=True,
            notify=notify,
            ids=ids,
            ignore_cool=ignore_cool,
            how=how,
            progress_only=progress_only,
            scheduled_scope=scheduled_scope,
            space_only=space_only,
        )


def _today() -> str:
    """The local date (YYYY-MM-DD) a daily download limit belongs to."""
    return iso_now()[:10]


def _daily_limits(
    limited_today: set[str], quota: set[str], results: list[dict[str, Any]], today: str
) -> dict[str, str]:
    """{site: today} for the sites under their daily download limit after this run.

    A manual check that reached a limited site without hitting the limit ends it early.
    """
    tried = {str(row.get("tracker")) for row in results if row.get("tracker")}
    kept = {name for name in limited_today if name in quota or name not in tried}
    return dict.fromkeys(sorted(kept | quota), today)


def _store_daily_limits(state: dict[str, Any], limits: dict[str, str]) -> None:
    if limits:
        state["daily_limit"] = limits  # yesterday's entries are dropped
    else:
        state.pop("daily_limit", None)


def _load_run_state(*, apply: bool) -> dict[str, Any]:
    """The state a run works on: an apply first finishes an interrupted commit; a dry run
    works on a copy (it never writes)."""
    if apply:
        with persistence_lock():
            check_transaction.recover_check_transaction()
            return load_state(quarantine=True)
    return copy.deepcopy(load_state(quarantine=False))


@dataclass
class _RunResult:
    """What a run hands to its commit."""

    state: dict[str, Any]
    results: list[CheckRow]
    started: dict[str, tuple[Any, ...]]  # owner fields per topic when the run started
    pool: ClientPool
    notifications: NotificationBatch
    cfg: dict[str, Any]
    how: str
    limited_today: set[str]
    quota: set[str]
    today: str


def _commit_run_state(result: _RunResult, history_rebuilt: bool = False) -> None:
    """Write the run's result onto the state as it is on disk now (the owner's edits made
    meanwhile are kept), with the health record and the staged messages, in one write."""
    state, pool, how = result.state, result.pool, result.how
    health = Health(
        qbit=pool.ping,
        client=pool.ping,
        qbit_ok=pool.default_ok,
        at=rows.now(),
        at_ts=int(machine_now().timestamp()),
    )
    state["health"] = health
    # The header countdown runs from the last *scheduled* check; a manual check of
    # one topic must not move it.
    previous_health = health_of(load_state(quarantine=False))
    _note_history_rebuilt(health, previous_health, rebuilt=history_rebuilt)
    health["auto_at_ts"] = health["at_ts"] if how == "auto" else previous_health.get("auto_at_ts")
    health["auto_ok_at_ts"] = health["at_ts"] if how == "auto" else _last_good_scheduled_check(previous_health)
    health["clients_ok"] = pool.health()
    if not pool.asked:  # no client was asked: keep what the last check or the diagnostics saw
        del health["qbit"], health["client"], health["qbit_ok"], health["clients_ok"]
        if "qbit" in previous_health:
            health["qbit"] = previous_health["qbit"]
        if "client" in previous_health:
            health["client"] = previous_health["client"]
        if "qbit_ok" in previous_health:
            health["qbit_ok"] = previous_health["qbit_ok"]
        if "clients_ok" in previous_health:
            health["clients_ok"] = previous_health["clients_ok"]
    if empty := pool.empty_counts():
        health["clients_empty"] = empty
    disk = load_state()
    edited = {
        str(topic.get("id")): changed
        for topic in topics_of(disk)
        if (changed := changed_owner_fields(result.started.get(str(topic.get("id"))), topic))
    }
    merge_check_results(disk, state, edited=edited)
    disk["health"] = health
    _store_daily_limits(disk, _daily_limits(result.limited_today, result.quota, result.results, result.today))
    # Staged in the same write as the result: a TOW stopped before sending loses nothing.
    from tow import delivery

    delivery.stage(disk, list(result.notifications), cfg=result.cfg)
    save_state(disk)


def _wanted_ids(
    state: dict[str, Any], ids: list[str] | None, scheduled_scope: str, *, space_only: bool = False
) -> set[str] | None:
    """The topics this run checks (None: all of them)."""
    want = {str(x) for x in ids} if ids is not None else None
    if space_only:
        waiting = {str(topic.get("id")) for topic in space.waiting_topics(state)}
        want = waiting if want is None else want & waiting
    if scheduled_scope:
        from tow.supervisor import layout
        from tow.topic_timers import batch_ids, interval_of

        selected = (
            set(batch_ids(state, layout.read_json(layout.schedule_path())))
            if scheduled_scope == "timer"
            else {str(topic.get("id")) for topic in topics_of(state) if interval_of(topic) is None}
        )
        want = selected if want is None else want & selected
    return want


def _clients_expected_to_list(state: dict[str, Any], want: set[str] | None, default_id: str) -> frozenset[str]:
    """The clients that had TOW's torrents at the last check: a topic this run checks was fine
    then. A paused or finished one-time topic is not checked (its last result stays as it
    was), nor is one whose own timer is not due: such a topic never says the client is
    expected to list anything."""
    return frozenset(
        str(topic.get("client_id") or default_id)
        for topic in topics_of(state)
        if (want is None or str(topic.get("id")) in want)
        and topic.get("hash")
        and topic.get("last_ok") is True
        and not topic.get("paused")
        and not topic.get("once_done")
    )


def _run_check(
    *,
    apply: bool,
    notify: bool,
    ids: list[str] | None = None,
    ignore_cool: bool = False,
    how: str = "auto",
    progress_only: bool = False,
    scheduled_scope: str = "",
    space_only: bool = False,
) -> dict[str, Any]:
    cfg = load_config()
    secrets, secrets_stamp = read_secrets()
    state = _load_run_state(apply=apply)
    started = {str(topic.get("id")): owner_fields(topic) for topic in topics_of(state)}
    notify = bool(notify and apply)
    trackers = load_trackers(cfg)
    ua = cfg.get("user_agent")
    batch = NotificationBatch()
    pending_reconcile_records: list[tuple[str, dict[str, Any]]] = []
    previous_health = health_of(state)

    def _record(kind: str, **fields: Any) -> None:
        if apply:
            log_event(kind, **fields)

    want = _wanted_ids(state, ids, scheduled_scope, space_only=space_only)
    default_id = client_factory.default_client_id(cfg)
    pool = ClientPool(
        cfg=cfg,
        apply=apply,
        notify=notify,
        how=how,
        record=_record,
        batch=batch,
        default_id=default_id,
        previous_qbit_ok=previous_health.get("qbit_ok"),
        previous_clients_ok=dict(previous_health.get("clients_ok") or {}),
        expect_torrents=_clients_expected_to_list(state, want, default_id),
        previous_empty={str(k): int(v) for k, v in as_dict(previous_health.get("clients_empty")).items()},
    )
    notifications = RunNotifications(
        enabled=notify,
        batch=batch,
        previous_error_class={
            str(topic.get("id")): (str(topic.get("last_error_class") or "error") if topic.get("last_error") else "")
            for topic in topics_of(state)
        },
        client_errors=pool.errors,
        default_client_id=pool.default_id,
    )
    # Nothing to check (a new install, or no topic in this run): no client is asked, so one not
    # set up yet is not logged as unreachable; the header keeps what was seen before.
    if any(want is None or str(topic.get("id")) in want for topic in topics_of(state)):
        pool.open_default(secrets)
    else:
        pool.asked = False
    # A site that said "download limit for today" is left alone until the next local day: the
    # limit is kept in the state, so the next scheduled runs do not ask it again. A manual
    # check (the owner pressed the button) still tries.
    today = _today()
    limited_today = {str(name) for name, day in as_dict(state.get("daily_limit")).items() if day == today}
    quota: set[str] = set(limited_today) if how in {"auto", "timer"} else set()
    run = CheckRun(
        apply=apply,
        notify=notify,
        how=how,
        ignore_cool=ignore_cool,
        ua=ua,
        state=state,
        trackers=trackers,
        quota=quota,
        secrets=secrets,
        client_errors=pool.errors,
        get_client_for=lambda topic: pool.get(topic, run.secrets),
        record=_record,
        queue_notification=notifications.queue,
        history_retention=download_history.retention(cfg),
        remote_clients=remote_clients(cfg, secrets),
        secrets_stamp=secrets_stamp,
        # A row's check (the owner chose these topics) puts a removed torrent back; Check all
        # and the scheduled checks only report it.
        readd_removed=how == "manual" and ids is not None,
    )
    results: list[CheckRow] = []
    for topic in topics_of(state):
        if want is not None and str(topic.get("id")) not in want:
            continue
        # G2: a progress-only pass asks qBittorrent alone - no tracker traffic at all.
        if space_only:
            row = _progress_only_row(topic)
            space.space_pass_row(topic, run, row)
            results.append(row)
        else:
            results.append(_progress_only_row(topic) if progress_only else check_topic(topic, run))
    secrets = run.secrets  # the per-topic step may have re-read tracker cookies
    pool.queue_recovered()
    if not apply:
        reconcile_preview(state, results, run, pending_reconcile_records, want)
        return {"qbit": pool.ping, "results": results, "preview": True}

    outcome = _RunResult(state, results, started, pool, batch, cfg, how, limited_today, quota, today)
    cleanup_pending = reconcile_and_commit(
        state,
        results,
        run,
        pending_reconcile_records,
        want,
        lambda rebuilt: _commit_run_state(outcome, rebuilt),
    )
    for event_kind, fields in pending_reconcile_records:
        _record(event_kind, **fields)
    flush_notifications(cfg, secrets, how=how, notify=notify)
    if cleanup_pending:
        _record("check_persistence_cleanup_pending", how=how)
    _record("check", apply=apply, ok=sum(1 for r in results if r.get("ok")), n=len(results), how=how)
    return {"qbit": pool.ping, "results": results}
