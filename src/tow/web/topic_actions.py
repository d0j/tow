"""What the topic forms on Home do, for ``routes_topics``: add a topic - refused at once with the
form kept, or saved and checked - and edit one, with a client-side move of its folder.

The record comes from ``tow.topic_form``; every store, client, site and check is reached through
``tow.web.services``.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from typing import Any, NamedTuple
from urllib.parse import quote

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
from tow.web import check_jobs, edit_base, services, topic_merge
from tow.web.check_actions import enqueue_check
from tow.web.messages import flash_redirect, topic_subject, use_subject
from tow.web.text import t
from tow.web.views import add_refused_redirect, failed_check_feedback, feedback_redirect, topic_check_row


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
    use_subject(topic_subject(shown_title(new)))  # the messages from here on are about it
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
    # Typed or changed by the owner: not empty, not a link, and not the title TOW guessed
    # into the field (no guess at all: what is there was typed).
    owner_named = bool(name) and not title_is_placeholder(name, url) and name != form.guessed_title.strip()
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
    if owner_named:
        topic["title_set"] = True
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
    the check found. Another applying check runs or waits: the first check waits in the queue,
    and its result is said as here ("added to TOW, ...", a failure logged and notified)."""
    follow_up = _first_check_follow_up(new, tracker)
    if check_jobs.has_pending(apply=True):
        return enqueue_check(topic_id=new["id"], apply=True, follow_up=follow_up)
    try:
        out = services.run_check(apply=True, notify=True, ids=[new["id"]], ignore_cool=True, how="manual", wait=False)
    except CheckBusyError:
        # Waiting here held this request - and every page behind it - for the whole other check.
        return enqueue_check(topic_id=new["id"], apply=True, follow_up=follow_up)
    except SecretStoreError as e:
        services.record_check_failure(e, how="manual")
        return feedback_redirect(follow_up(None, e))
    except Exception as e:  # noqa: BLE001 - the topic is saved; its first check's failure is logged and notified
        return feedback_redirect(follow_up(None, e))
    return feedback_redirect(follow_up(out, None))


def _first_check_follow_up(
    new: dict[str, Any], tracker: GenericHttpTracker
) -> Callable[[dict[str, Any] | None, BaseException | None], dict[str, str]]:
    """What the new topic's first check says (``check_jobs.FOLLOW_UPS``), at once or queued."""

    def said(key: str, kind: str = "ok") -> dict[str, str]:
        return {"text": t(key), "kind": kind, "credential_topic": ""}

    def follow_up(out: dict[str, Any] | None, error: BaseException | None) -> dict[str, str]:
        if isinstance(error, SecretStoreError):
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
            return said("web.topics.added_blocked", "warn")
        if error is not None or out is None:
            _first_check_failed(new, error if isinstance(error, Exception) else RuntimeError("no result"), tracker.name)
            return said("web.topics.added_check_failed", "warn")
        row = topic_check_row(new["id"], out)
        if row is None:
            return said("web.topics.added_no_result", "warn")
        tracker_name = tracker.name if tracker.spec.get("login_path") else ""
        if (
            failed := failed_check_feedback(row, topic_id=new["id"], tracker_name=tracker_name, new_topic=True)
        ) is not None:
            return failed
        return said("web.topics.added_confirmed" if row.get("added") else "web.topics.added_nothing_new")

    return follow_up


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


class _Edit(NamedTuple):
    """What an edit decided under the first lock: the topic before it, the edited copy, the
    folder, and the form it applied (the owner's changes on the topic as it was then)."""

    before: dict[str, Any]
    candidate: dict[str, Any]
    dest: str
    form: TopicForm
    edited: dict[str, Any]  # the topic_edit event, logged by _save_edit once the edit is saved


# The topics whose folder a save is moving in the client now: a second move of the same topic
# waits for the first to end (it is refused, never run beside it).
_RELOCATING: set[str] = set()
_RELOCATING_LOCK = threading.Lock()


def _reserve_move(tid: str) -> bool:
    with _RELOCATING_LOCK:
        if tid in _RELOCATING:
            return False
        _RELOCATING.add(tid)
        return True


def _release_move(tid: str) -> None:
    with _RELOCATING_LOCK:
        _RELOCATING.discard(tid)


def _default_client() -> str:
    try:
        return str(client_configuration(services.load_config(), None)["id"])
    except RuntimeError, ValueError, KeyError:
        return ""


def _reload_form(tid: str) -> RedirectResponse:
    """A save without a valid base: nothing is saved; the form opens again as saved now."""
    return flash_redirect(f"/topics/{quote(tid, safe='')}/edit", "web.edit.reload", "err")


def _conflict(
    tid: str, topic: dict[str, Any], form: TopicForm, merged: edit_base.Merge, *, said: str = "", kind: str = "warn"
) -> RedirectResponse:
    """A save refused for a conflict: the form opens again with the owner's draft, both values of
    each conflicting group and a new base (saved now, those groups waiting for a choice)."""
    default = _default_client()
    now = topic_merge.current(topic, default)
    base = edit_base.encode(topic_merge.TOPIC, tid, now, merged.conflicts)
    token = edit_base.keep_conflict(
        topic_merge.TOPIC,
        tid,
        base=base,
        draft=topic_merge.draft(form, merged.changed | set(merged.conflicts)),
        rows=topic_merge.rows(merged.conflicts, topic, form, services.load_config()),
    )
    message = t("web.edit.conflict") + said
    return flash_redirect(f"/topics/{quote(tid, safe='')}/edit?conflict={quote(token)}", message, kind)


def edit_base_of(topic: dict[str, Any]) -> str:
    """The hidden ``base`` of ``topic``'s edit form, as it is saved now."""
    tid = str(topic.get("id") or "")
    return edit_base.encode(topic_merge.TOPIC, tid, topic_merge.current(topic, _default_client()))


def edit_topic(
    tid: str, form: TopicForm, base_raw: str = "", resolutions: dict[str, str] | None = None
) -> RedirectResponse:
    """The topic as the edit form says, merged with the topic as it is now: only what the owner
    changed since the form was rendered (``base_raw``) is saved, on the current topic; a field
    another device changed to something else meanwhile is a conflict, and nothing is saved. A
    changed folder of a topic with a revision is moved in its client too. The persistence lock is
    held to read and to save, never while the client moves the folder (up to
    ``RELOCATION_WAIT_SEC``): the save then reads the topic again, checks the merge again and
    applies only what the form changed, so a check that finished meanwhile is kept."""
    with services.persistence_lock():
        state = services.load_state()
        topic = _find(state, tid)
        if topic is None:  # deleted meanwhile (another tab, undo): nothing to save, never "Saved"
            return flash_redirect("/", "web.topics.not_found", "err")
        default = _default_client()
        now = topic_merge.current(topic, default)
        base = edit_base.read(topic_merge.TOPIC, tid, base_raw, now)
        if base is None:
            return _reload_form(tid)
        wanted = topic_merge.submitted(form, base.values, state, default)
        merged = edit_base.merge(topic_merge.TOPIC, base, now, wanted, resolutions)
        if merged.conflicts:
            return _conflict(tid, topic, form, merged)
        applied = topic_merge.effective_form(form, topic, merged)
        try:
            edit = _decide(state, topic, applied)
        except Refused as refusal:
            return refusal.response(form)
        old_dest = str(topic.get("save_path") or "")
        old_hash = str(topic.get("hash") or "")
        if not old_hash or paths_equal(old_dest, edit.dest):
            return _save_edit(state, topic, edit.before, edit.candidate, edit.dest, None, edited=edit.edited)
        if not _reserve_move(tid):  # another save moves this topic's folder now
            return flash_redirect("/", "web.edit.move_busy", "warn")
    try:
        moved = _move_in_client(edit.candidate, tid, old_hash, edit.dest)
        return _after_move(tid, edit, moved, old_dest, old_hash, base, wanted, resolutions)
    finally:
        _release_move(tid)


def _decide(state: dict[str, Any], topic: dict[str, Any], form: TopicForm) -> _Edit:
    """The edit of ``topic`` the form asks for (``Refused``: it cannot be saved), logged."""
    candidate, policy, selection_changed = _edited(topic, form)
    dest = resolve_save_path(form.save_path, state)
    if not dest:
        raise Refused("web.topics.need_folder_short")
    if problem := _folder_refusal(dest, current=str(topic.get("save_path") or "")):
        raise Refused(problem)
    # Logged by _save_edit once the edit is saved: a save refused or failed is no "edited".
    edited = {
        "topic": str(topic.get("id") or ""),
        "title": candidate.get("title"),
        "url": candidate.get("url"),
        "path": dest,
        "client_id": candidate["client_id"],
        "selection_mode": policy["mode"],
        "selection_value": policy["value"],
        "tracking_mode": policy["tracking_mode"],
        "selection_changed": selection_changed,
        "check_interval_min": candidate.get("check_interval_min"),
        "how": "manual",
    }
    return _Edit(copy.deepcopy(topic), candidate, dest, form, edited)


def _after_move(
    tid: str,
    edit: _Edit,
    moved: _Moved,
    old_dest: str,
    old_hash: str,
    base: edit_base.Base,
    wanted: dict[str, Any],
    resolutions: dict[str, str] | None,
) -> RedirectResponse:
    """The second check after the client moved the folder (without the lock): the topic is read
    again and the merge checked again. The move happened whatever comes now, so its folder is
    recorded while the topic still has the torrent that moved; a field another device changed
    meanwhile is a conflict, kept as saved now and shown with the owner's value."""
    with services.persistence_lock():
        state = services.load_state()
        topic = _find(state, tid)
        if topic is None:  # deleted while its folder moved: the History has the move
            return flash_redirect("/", "web.topics.not_found", "err")
        if str(topic.get("hash") or "") != old_hash or not paths_equal(str(topic.get("save_path") or ""), old_dest):
            # A check bound another torrent, or the folder changed meanwhile: what moved is not
            # what the topic follows now, and the topic is left as it is.
            return flash_redirect("/", t("web.edit.changed_while_moving") + moved.suffix, "warn")
        now = topic_merge.current(topic, _default_client())
        again = edit_base.merge(topic_merge.TOPIC, base, now, wanted, resolutions)
        if again.conflicts:
            if moved.outcome != "error":  # the folder the client moved the torrent to
                folder_only = {**copy.deepcopy(topic), "save_path": edit.dest}
                _save_edit(
                    state,
                    topic,
                    copy.deepcopy(topic),
                    folder_only,
                    edit.dest,
                    moved,
                    old_dest=old_dest,
                    edited={"topic": tid, "how": "manual"},  # only the folder was saved
                )
            return _conflict(tid, topic, edit.form, again, said=moved.suffix)
        return _save_edit(
            state, topic, edit.before, edit.candidate, edit.dest, moved, old_dest=old_dest, edited=edit.edited
        )


def _save_edit(
    state: dict[str, Any],
    topic: dict[str, Any],
    before: dict[str, Any],
    candidate: dict[str, Any],
    dest: str,
    moved: _Moved | None,
    *,
    old_dest: str = "",
    edited: dict[str, Any] | None = None,
) -> RedirectResponse:
    """The form's changes (``before`` -> ``candidate``) on ``topic`` as it is now, with the new
    folder unless its move failed; the caller holds the persistence lock. ``edited``: the
    ``topic_edit`` event, logged after the save with the folder the topic really has."""
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
    if edited is not None:
        services.log_event("topic_edit", **{**edited, "path": topic.get("save_path")})
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
