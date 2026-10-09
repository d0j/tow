"""What the topic forms on Home do, for ``routes_topics``: add a topic - refused at once with the
form kept, or saved and checked - and edit one, with a client-side move of its folder.

The record comes from ``tow.topic_form``; every store, client, site and check is reached through
``tow.web.services``.
"""

from __future__ import annotations

import copy
from typing import Any, NamedTuple

from fastapi.responses import RedirectResponse

from tow import undo
from tow.clients.factory import client_configuration
from tow.clock import iso_now
from tow.config import as_bool
from tow.errors import TowError
from tow.folders import paths_equal, remember_save_root, resolve_save_path
from tow.guess import canon_watch_url
from tow.log import error_fields
from tow.records import shown_title
from tow.store import CheckBusyError, SecretStoreError
from tow.title import title_is_placeholder
from tow.topic_form import (
    TopicForm,
    apply_selection,
    bind_content,
    new_topic,
    pending_move,
    previous_selection,
    save_path_refusal,
    selection_policy,
)
from tow.topic_timers import parse_interval, set_interval
from tow.torrent import TorrentMetadata, parse_torrent_metadata
from tow.trackers import GenericHttpTracker, load_trackers, match_tracker
from tow.web import services
from tow.web.text import t
from tow.web.views import add_refused_redirect, flash_redirect, manual_check_flash, topic_check_row


class Refused(Exception):
    """Nothing is saved: Home says ``message`` as ``kind`` - or, for an add with ``field`` set,
    the add form opens again with what was typed and the reason at that field."""

    def __init__(self, message: Any, kind: str = "err", *, field: str | None = None) -> None:
        super().__init__(message)
        self.message, self.kind, self.field = message, kind, field

    def response(self, form: TopicForm) -> RedirectResponse:
        if self.field is None:
            return flash_redirect("/", self.message, self.kind)
        return add_refused_redirect(str(self.message), form.draft(), kind=self.field)


class _Added(NamedTuple):
    topic: dict[str, Any]
    tracker: GenericHttpTracker
    policy: dict[str, Any]
    interval: int | None


def _prepared(token: str, url: str, client_id: str) -> TorrentMetadata | None:
    """The form's prepared torrent, read and decrypted once per request."""
    return parse_torrent_metadata(services.read_content(token, url, client_id)) if token else None


def _folder_refusal(dest: str, *, current: str = "") -> str | None:
    allow_unc = as_bool(services.load_config().get("allow_unc_save_paths"))
    return save_path_refusal(dest, allow_unc=allow_unc, current=current)


def add_topic(form: TopicForm) -> RedirectResponse:
    """A new topic, checked at once. A refused add never happened: the form says why, History
    logs no failed check for it."""
    try:
        added = _new_topic(form)
        _save_new_topic(added.topic)
    except Refused as refusal:
        return refusal.response(form)
    new, policy = added.topic, added.policy
    services.log_event(
        "topic_add",
        topic=new["id"],
        title=new["title"],
        url=new["url"],
        path=new["save_path"],
        client_id=new["client_id"],
        selection_mode=policy["mode"],
        selection_value=policy["value"],
        tracking_mode=policy["tracking_mode"],
        check_interval_min=added.interval,
        how="manual",
    )
    return _first_check(new, added.tracker)


def _new_topic(form: TopicForm) -> _Added:
    """The new topic's record; refused at once - before the site is asked for a title - when
    the form cannot be saved."""
    try:
        interval = parse_interval(form.check_interval_min or "")
    except TowError as exc:
        raise Refused(str(exc), field="timer") from exc
    url = canon_watch_url(form.url)
    state = services.load_state()
    cfg = services.load_config()
    tracker = match_tracker(load_trackers(cfg), url)
    if not tracker:
        raise Refused(t("web.topics.unknown_link"), field="no_site")
    if any(item.get("url") == url for item in state.setdefault("topics", [])):
        raise Refused("web.topics.already_watched", "warn")
    dest = resolve_save_path(form.save_path, state)
    if not dest:
        raise Refused(t("web.topics.need_folder"), field="folder")
    if problem := _folder_refusal(dest):
        raise Refused(problem, field="folder")
    try:
        selected_client = client_configuration(cfg, form.client_id or None)
    except (RuntimeError, ValueError) as exc:
        raise Refused(t("web.topics.choose_client"), field="client") from exc
    if not selected_client.get("enabled", True):
        raise Refused(t("web.topics.client_disabled"), field="client")
    client_id = str(selected_client["id"])
    try:
        prepared = _prepared(form.content_token, url, client_id)
        policy = selection_policy(
            form.selection_mode, form.selection_value, form.tracking_mode, prepared, form.selection_indices
        )
    except (ValueError, RuntimeError) as exc:
        raise Refused(str(exc), field="selection") from exc
    # Only now the site's page for a title (up to the site's timeouts): a refusal above is at once.
    name = form.title.strip()
    if title_is_placeholder(name, url):
        name = services.guess_topic_title(url) or name or url
    topic = new_topic(
        form,
        title=name,
        url=url,
        save_path=dest,
        client_id=client_id,
        policy=policy,
        prepared=prepared,
        interval=interval,
    )
    return _Added(topic, tracker, policy, interval)


def _save_new_topic(new: dict[str, Any]) -> None:
    """The request does not hold the site lock (the title was fetched, a check follows): what
    it decided on - the site, the client, the folder - is read again under the persistence lock
    before the topic is saved."""
    with services.persistence_lock():
        cfg = services.load_config()
        if match_tracker(load_trackers(cfg), new["url"]) is None:
            raise Refused(t("web.topics.unknown_link"), field="no_site")
        try:
            selected_client = client_configuration(cfg, new["client_id"])
        except (RuntimeError, ValueError) as exc:
            raise Refused(t("web.topics.choose_client"), field="client") from exc
        if not selected_client.get("enabled", True):
            raise Refused(t("web.topics.client_disabled"), field="client")
        if problem := _folder_refusal(new["save_path"]):
            raise Refused(problem, field="folder")
        state = services.load_state()
        topics = state.setdefault("topics", [])
        if any(item.get("url") == new["url"] for item in topics):
            raise Refused("web.common.already_exists", "warn")
        topics.append(new)
        remember_save_root(state, new["save_path"])
        undo.stamp(state, "topic_add", id=new["id"])
        services.save_state(state)
        services.cleanup_secret_undo()


def _first_check(new: dict[str, Any], tracker: GenericHttpTracker) -> RedirectResponse:
    """The new topic's first check, and what Home says about it: the topic stays saved whatever
    the check found."""
    try:
        out = services.run_check(apply=True, notify=True, ids=[new["id"]], ignore_cool=True, how="manual", wait=False)
        row = topic_check_row(new["id"], out)
        if row is None:
            return flash_redirect("/", "web.topics.added_no_result", "warn")
        if row and not row.get("ok"):
            refused = manual_check_flash(
                row,
                topic_id=new["id"],
                tracker_name=tracker.name if tracker.spec.get("login_path") else "",
                new_topic=True,
            )
            if refused is not None:  # always, for a row that is not ok
                return refused
    except CheckBusyError:
        # Waiting here held this request - and every page behind it - for the whole other check.
        return flash_redirect("/", "web.topics.added_check_busy", "warn")
    except SecretStoreError as e:
        services.record_check_failure(e, how="manual")
        services.log_event(
            "add_check_blocked",
            topic=new["id"],
            title=new["title"],
            url=new["url"],
            path=new["save_path"],
            error="secrets_migration_required",
            cls="secret_gate",
            how="manual",
        )
        return flash_redirect("/", "web.topics.added_blocked", "warn")
    except Exception as e:  # noqa: BLE001 - the topic is saved; its first check's failure is logged and notified
        _first_check_failed(new, e, tracker.name)
        return flash_redirect("/", "web.topics.added_check_failed", "warn")
    return flash_redirect("/", "web.topics.added_confirmed" if row.get("added") else "web.topics.added_nothing_new")


def _first_check_failed(new: dict[str, Any], error: Exception, tracker: str) -> None:
    services.log_event(
        "add_check_fail",
        topic=new["id"],
        title=new["title"],
        url=new["url"],
        path=new["save_path"],
        **error_fields(error),
        how="manual",
    )
    try:
        services.notify_topic_error(new, error, tracker=tracker, how="manual")
    except Exception as notify_exc:  # noqa: BLE001 - a lost message about the failure is logged, never a 500
        services.log_event("add_check_notify_fail", topic=new["id"], error=str(notify_exc), how="manual")


class _Moved(NamedTuple):
    """How the client-side move of a topic's folder went: ``outcome`` as ``await_relocation``
    says it ("done", "moving", "failed"), "absent" when the client does not have the torrent
    (nothing to move), or "error" when the client refused or did not answer (the topic keeps its
    old folder); ``suffix`` and ``kind`` for the save message."""

    outcome: str
    suffix: str
    kind: str


def _find(state: dict[str, Any], tid: str) -> dict[str, Any] | None:
    return next((item for item in state.get("topics") or [] if str(item.get("id")) == tid), None)


def edit_topic(tid: str, form: TopicForm) -> RedirectResponse:
    """The topic as the edit form says; a changed folder of a topic with a revision is moved in
    its client too. The persistence lock is held to read and to save, never while the client
    moves the folder (up to ``RELOCATION_WAIT_SEC``): the save then reads the topic again and
    applies only what the form changed, so a check that finished meanwhile is kept."""
    with services.persistence_lock():
        state = services.load_state()
        topic = _find(state, tid)
        if topic is None:  # deleted meanwhile (another tab, undo): nothing to save, never "Saved"
            return flash_redirect("/", "web.topics.not_found", "err")
        try:
            candidate, policy, selection_changed = _edited(topic, form)
            dest = resolve_save_path(form.save_path, state)
            if not dest:
                raise Refused("web.topics.need_folder_short")
            if problem := _folder_refusal(dest, current=str(topic.get("save_path") or "")):
                raise Refused(problem)
        except Refused as refusal:
            return refusal.response(form)
        before = copy.deepcopy(topic)
        services.log_event(
            "topic_edit",
            topic=tid,
            title=candidate.get("title"),
            url=candidate.get("url"),
            path=dest,
            client_id=candidate["client_id"],
            selection_mode=policy["mode"],
            selection_value=policy["value"],
            tracking_mode=policy["tracking_mode"],
            selection_changed=selection_changed,
            check_interval_min=candidate.get("check_interval_min"),
            how="manual",
        )
        old_dest = str(topic.get("save_path") or "")
        old_hash = str(topic.get("hash") or "")
        if not old_hash or paths_equal(old_dest, dest):
            return _save_edit(state, topic, before, candidate, dest, None)
    moved = _move_in_client(candidate, tid, old_hash, dest)
    with services.persistence_lock():
        state = services.load_state()
        topic = _find(state, tid)
        if topic is None:  # deleted while its folder moved: the History has the move
            return flash_redirect("/", "web.topics.not_found", "err")
        return _save_edit(state, topic, before, candidate, dest, moved, old_dest=old_dest)


def _save_edit(
    state: dict[str, Any],
    topic: dict[str, Any],
    before: dict[str, Any],
    candidate: dict[str, Any],
    dest: str,
    moved: _Moved | None,
    *,
    old_dest: str = "",
) -> RedirectResponse:
    """The form's changes (``before`` -> ``candidate``) on ``topic`` as it is now, with the new
    folder unless its move failed; the caller holds the persistence lock."""
    undo.stamp(state, "topic_put", item=copy.deepcopy(topic))
    for key in before.keys() | candidate.keys():
        if key not in candidate:
            topic.pop(key, None)
        elif before.get(key) != candidate[key] or key not in before:
            topic[key] = copy.deepcopy(candidate[key])
    if moved is None or moved.outcome != "error":
        topic["save_path"] = dest
        remember_save_root(state, dest)
    if moved is not None and moved.outcome != "error":
        if pending := pending_move(moved.outcome, old_dest, dest, iso_now()):
            topic["move_pending"] = pending
        else:
            topic.pop("move_pending", None)
    services.save_state(state)
    services.cleanup_secret_undo()
    suffix, kind = (moved.suffix, moved.kind) if moved is not None else ("", "ok")
    return flash_redirect("/", t("web.common.saved") + suffix, kind)


def _rename(topic: dict[str, Any], candidate: dict[str, Any], name: str) -> None:
    """The edit panel's name on ``candidate``. The name as the panel showed it changes nothing; a
    name the owner typed is shown instead of the site's title (``title_set``); the site's own
    title or an empty field shows the site's title again."""
    site_title = str(topic.get("tracker_title") or "").strip()
    if name and name == shown_title(topic).strip():
        return
    if not name or name == site_title:
        candidate.pop("title_set", None)
        candidate["title"] = name or site_title or str(topic.get("title") or "")
    else:
        candidate["title"] = name
        candidate["title_set"] = True


def _edited(topic: dict[str, Any], form: TopicForm) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """A copy of ``topic`` with the form's changes but the folder, the selection rule and whether
    the selection changed; ``Refused`` when the form cannot be saved."""
    candidate = copy.deepcopy(topic)
    try:
        if form.check_interval_min is not None:
            set_interval(candidate, parse_interval(form.check_interval_min))
    except TowError as exc:
        raise Refused(exc) from exc
    _rename(topic, candidate, form.title.strip())
    if form.url.strip():
        url = canon_watch_url(form.url.strip())
        if not match_tracker(load_trackers(services.load_config()), url):
            raise Refused("web.topics.unknown_link_short")
        if topic.get("hash") and url != str(topic.get("url") or ""):
            raise Refused("web.topics.new_link", "warn")
        candidate["url"] = url
    try:
        selected_client = client_configuration(services.load_config(), form.client_id or None)
    except (RuntimeError, ValueError) as exc:
        raise Refused("web.topics.choose_client_short") from exc
    client_id = str(selected_client["id"])
    if topic.get("hash") and client_id != str(topic.get("client_id") or client_id):
        raise Refused("web.topics.client_locked")
    if candidate["url"] != topic.get("url") or client_id != str(topic.get("client_id") or client_id):
        # What the last check found (an unmarked torrent to adopt) was about the old link or
        # client: its message stays until the next check, its offer does not.
        candidate.pop("last_error_code", None)
        candidate.pop("last_error_params", None)
    try:
        prepared = _prepared(form.content_token, str(candidate["url"]), client_id)
        policy = selection_policy(
            form.selection_mode,
            form.selection_value,
            form.tracking_mode,
            prepared,
            form.selection_indices,
            previous_selection(topic),
        )
        bind_content(topic, candidate, client_id, form.content_token, prepared, form.selection_mode)
    except (ValueError, RuntimeError) as exc:
        raise Refused(exc) from exc
    return candidate, policy, apply_selection(topic, candidate, client_id, policy)


def _move_in_client(topic: dict[str, Any], tid: str, old_hash: str, dest: str) -> _Moved:
    """The topic's torrent moves to ``dest`` in its client (only one TOW added; the owner's edit
    asked for it), without the persistence lock. A failure is logged and said, never raised -
    the edit itself is kept."""
    move_client_id = str(topic.get("client_id") or "") or None
    try:
        adapter = services.client_from_secrets(services.load_config(), services.load_secrets(), move_client_id)
        client_kind = str(getattr(adapter, "client_kind", getattr(adapter, "kind", "client")))
        if adapter.inspect_torrent(old_hash) is None:
            # Nothing to move: the torrent is gone from its client (Home says so). The new folder
            # is where "Check" adds it again; refusing it kept the old one for that add.
            return _Moved("absent", t("web.topics.moved_absent"), "ok")
        if not services.client_owned_by_tow(adapter, old_hash):
            raise RuntimeError(t("web.topics.not_owned"))
        adapter.set_location(old_hash, dest)
        outcome = services.await_relocation(adapter, old_hash, dest)
        services.log_event(
            "qbit_move",
            topic=tid,
            title=topic.get("title"),
            path=dest,
            hash=old_hash,
            client_id=move_client_id,
            client_kind=client_kind,
            status={"done": "succeeded", "moving": "moving"}.get(outcome, "unconfirmed"),
            how="manual",
        )
        suffix = {
            "done": t("web.topics.moved_done"),
            "moving": t("web.topics.moved_moving"),
        }.get(outcome, t("web.topics.moved_unconfirmed"))
        return _Moved(outcome, suffix, "ok" if outcome in ("done", "moving") else "warn")
    except Exception as e:  # noqa: BLE001 - any client failure of the move is logged and said; the edit is kept
        services.log_event(
            "qbit_move_fail",
            topic=tid,
            title=topic.get("title"),
            path=dest,
            hash=old_hash,
            client_id=move_client_id,
            **error_fields(e),
            how="manual",
        )
        return _Moved("error", t("web.topics.move_failed"), "warn")
