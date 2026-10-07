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
from tow.notify import event_text
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
    it decided on is read again under the persistence lock before the topic is saved."""
    with services.persistence_lock():
        if match_tracker(load_trackers(services.load_config()), new["url"]) is None:
            raise Refused(t("web.topics.unknown_link"), field="no_site")
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
        _first_check_failed(new, e)
        return flash_redirect("/", "web.topics.added_check_failed", "warn")
    return flash_redirect("/", "web.topics.added_confirmed" if row.get("added") else "web.topics.added_nothing_new")


def _first_check_failed(new: dict[str, Any], error: Exception) -> None:
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
        services.notify_send(services.load_secrets(), event_text(title=new["title"], kind="error", error=error))
    except Exception as notify_exc:  # noqa: BLE001 - a lost message about the failure is logged, never a 500
        services.log_event("add_check_notify_fail", topic=new["id"], error=str(notify_exc), how="manual")


def edit_topic(tid: str, form: TopicForm) -> RedirectResponse:
    """The topic as the edit form says (the caller holds the persistence lock); a changed folder
    of a topic with a revision is moved in its client too."""
    state = services.load_state()
    topic = next((item for item in state.get("topics") or [] if str(item.get("id")) == tid), None)
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
    undo.stamp(state, "topic_put", item=copy.deepcopy(topic))
    topic.update(candidate)
    old_dest = str(topic.get("save_path") or "")
    old_hash = str(topic.get("hash") or "")
    if not old_hash or paths_equal(old_dest, dest):
        topic["save_path"] = dest
        remember_save_root(state, dest)
    services.log_event(
        "topic_edit",
        topic=tid,
        title=topic.get("title"),
        url=topic.get("url"),
        path=dest,
        client_id=candidate["client_id"],
        selection_mode=policy["mode"],
        selection_value=policy["value"],
        tracking_mode=policy["tracking_mode"],
        selection_changed=selection_changed,
        check_interval_min=topic.get("check_interval_min"),
        how="manual",
    )
    moved, moved_kind = "", "ok"
    if old_hash and not paths_equal(old_dest, dest):
        moved, moved_kind = _move_in_client(state, topic, tid, old_hash, old_dest, dest)
    services.save_state(state)
    services.cleanup_secret_undo()
    return flash_redirect("/", t("web.common.saved") + moved, moved_kind)


def _edited(topic: dict[str, Any], form: TopicForm) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """A copy of ``topic`` with the form's changes but the folder, the selection rule and whether
    the selection changed; ``Refused`` when the form cannot be saved."""
    candidate = copy.deepcopy(topic)
    try:
        if form.check_interval_min is not None:
            set_interval(candidate, parse_interval(form.check_interval_min))
    except TowError as exc:
        raise Refused(exc) from exc
    if form.title.strip():
        candidate["title"] = form.title.strip()
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


def _move_in_client(
    state: dict[str, Any], topic: dict[str, Any], tid: str, old_hash: str, old_dest: str, dest: str
) -> tuple[str, str]:
    """The topic's torrent moves to ``dest`` in its client (only one TOW added; the owner's edit
    asked for it): the save message's suffix and its kind. A failure is logged and said, never
    raised - the edit itself is kept."""
    move_client_id = str(topic.get("client_id") or "") or None
    try:
        adapter = services.client_from_secrets(services.load_config(), services.load_secrets(), move_client_id)
        client_kind = str(getattr(adapter, "client_kind", getattr(adapter, "kind", "client")))
        if not services.client_owned_by_tow(adapter, old_hash):
            raise RuntimeError(t("web.topics.not_owned"))
        adapter.set_location(old_hash, dest)
        outcome = services.await_relocation(adapter, old_hash, dest)
        topic["save_path"] = dest
        if pending := pending_move(outcome, old_dest, dest, iso_now()):
            topic["move_pending"] = pending
        else:
            topic.pop("move_pending", None)
        remember_save_root(state, dest)
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
        moved = {
            "done": t("web.topics.moved_done"),
            "moving": t("web.topics.moved_moving"),
        }.get(outcome, t("web.topics.moved_unconfirmed"))
        return moved, "ok" if outcome in ("done", "moving") else "warn"
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
        return t("web.topics.move_failed"), "warn"
