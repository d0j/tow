"""The owner's opt-in to G1 by itself (``replace_previous_revision`` in Settings, a topic's own
``replace_previous``): an applying check whose new revision shares a file with a previous
revision of the same topic still running in its client stops that previous revision - only
TOW's own, its files kept, each stop read back - and adds the new one in the same check.

- A stop that fails leaves the topic waiting as before (``check.previous_revision_active``:
  nothing is added); the log has why the stop failed.
- An add that fails or is not confirmed after the stop starts the stopped revisions again, so
  the owner is not left with nothing seeding (best effort, logged).
- A previous revision another topic follows, one without TOW's mark, a client that cannot add a
  file selection or a torrent another topic claims: nothing is stopped, the check refuses as
  without the opt-in.
- A dry run only says it would stop the previous one and add it (``would_stop_previous``).

Off (the default), nothing here runs: the owner's one click on Home
(``POST /topics/{id}/replace-revision``) does it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tow.check.client_ops import (
    PREVIOUS_REVISION_ACTIVE,
    blocked_revision,
    can_add_selected,
    claimed_by_other_topic,
    client_owned_by_tow,
    conflicting_topic_claim,
    live_previous_overlaps,
)
from tow.check.space import is_stopped
from tow.errors import TowError
from tow.log import error_fields
from tow.records import Topic

if TYPE_CHECKING:
    from tow.check.topic import TopicCheck

REASON = "replace_revision_auto"


def by_itself(topic: Topic, default: bool) -> bool:
    """Whether a check stops this topic's previous revision itself: its own choice, else
    Settings' (``default``)."""
    choice = str(topic.get("replace_previous") or "")
    return choice == "on" if choice in {"on", "off"} else bool(default)


def _revisions(work: TopicCheck, h: str) -> list[str]:
    """The topic's own earlier revisions (never the new one)."""
    hashes = [work.old, *map(str, work.topic.get("previous_hashes") or [])]
    return [value for value in dict.fromkeys(hashes) if value and value.upper() != h.upper()]


def _stoppable(work: TopicCheck, metadata: Any, *, h: str, dest: str, plan: Any) -> list[tuple[str, str]]:
    """The previous revisions to stop before the add: each still running on a file of the new
    one, TOW's own and no other topic's. [] when there is none, or when any of them may not be
    stopped (then the add is refused as without the opt-in, and nothing is stopped)."""
    topic, run, client = work.topic, work.run, work.client
    if client is None or not can_add_selected(client) or not callable(getattr(client, "stop_owned_torrent", None)):
        return []
    if conflicting_topic_claim(run.state, topic, h, dest, plan):
        return []  # the add is refused for that, before anything is stopped
    found = live_previous_overlaps(client, _revisions(work, h), metadata.files)
    if any(claimed_by_other_topic(run.state, topic, revision) for revision, _file in found):
        return []
    if not all(client_owned_by_tow(client, revision) for revision, _file in found):
        return []
    return found


def would_stop(work: TopicCheck, metadata: Any, *, h: str, plan: Any) -> list[str]:
    """A dry run: the previous revisions an applying check would stop before it adds ``h``
    (read-only: the client is only asked)."""
    dest = str(work.topic.get("save_path") or "").strip()
    return [revision for revision, _file in _stoppable(work, metadata, h=h, dest=dest, plan=plan)]


def stop_previous(work: TopicCheck, metadata: Any, *, h: str, dest: str, plan: Any) -> list[str]:
    """Stop the topic's previous revisions that still run on a file of ``h`` (``_stoppable``),
    each read back; returns their hashes ([] when none was stopped). A stop that fails starts
    those stopped before it again and refuses the add: ``check.previous_revision_active``, its
    cause logged once per waiting revision."""
    topic, run, row, client = work.topic, work.run, work.row, work.client
    found = _stoppable(work, metadata, h=h, dest=dest, plan=plan)
    if not found or client is None:
        return []
    stopped: list[str] = []
    for revision, file in found:
        try:
            client.stop_owned_torrent(revision)
            if not is_stopped(client.inspect_torrent(revision)):
                raise TowError("check.stop_unconfirmed")
        except Exception as error:  # any failure to stop is the refusal it was without the opt-in
            start_again(work, stopped, h=h)
            if blocked_revision(topic) != h.upper():  # the wait itself is logged once (topic._still_waiting)
                run.record(
                    "client_stop_failed",
                    component="client",
                    integration_id=work.client_id,
                    client_id=work.client_id,
                    topic_id=topic.get("id"),
                    topic=topic.get("id"),
                    title=topic.get("title"),
                    hash=revision,
                    reason=REASON,
                    **error_fields(error),
                    how=run.how,
                )
            raise TowError(PREVIOUS_REVISION_ACTIVE, file=file, hash=h.upper()) from error
        stopped.append(revision)
    run.record(
        "client_stopped",
        component="client",
        integration_id=work.client_id,
        client_id=work.client_id,
        topic_id=topic.get("id"),
        topic=topic.get("id"),
        title=topic.get("title"),
        hashes=stopped,
        reason=REASON,
        status="succeeded",
        how=run.how,
    )
    row["stopped_previous"] = stopped
    return stopped


def start_again(work: TopicCheck, stopped: list[str], *, h: str) -> None:
    """The add after the stop did not happen: start the stopped revisions again (best effort,
    each read back and logged). Not while the new revision ``h`` runs in the client: two
    torrents would write the same file."""
    if not stopped or work.client is None:
        return
    topic, run, client = work.topic, work.run, work.client
    work.row.pop("stopped_previous", None)
    fields = {
        "component": "client",
        "integration_id": work.client_id,
        "client_id": work.client_id,
        "topic_id": topic.get("id"),
        "topic": topic.get("id"),
        "title": topic.get("title"),
        "reason": REASON,
        "how": run.how,
    }
    try:
        new = client.inspect_torrent(h)
    except Exception as error:  # noqa: BLE001 - not known whether the new one runs: nothing is started
        run.record("client_start_failed", hashes=list(stopped), **error_fields(error), **fields)
        return
    if new is not None and not is_stopped(new):
        run.record("client_start_failed", hashes=list(stopped), status="new_revision_running", **fields)
        return
    started: list[str] = []
    for revision in stopped:
        try:
            client.start_owned_torrent(revision)
            if is_stopped(client.inspect_torrent(revision)):
                raise TowError("check.restart_unconfirmed")
        except Exception as error:  # noqa: BLE001 - best effort: each failure is logged, the add's own error stands
            run.record("client_start_failed", hash=revision, **error_fields(error), **fields)
            continue
        started.append(revision)
    if started:
        run.record("client_started", hashes=started, status="succeeded", **fields)
