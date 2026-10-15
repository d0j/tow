"""Shared view helpers: the add form's draft, the topic rows of Home with their status and
attention lines, the messages of a manual check, whether the first-start page is due, and the
service and backup cards of Settings. Messages after an action are ``tow.web.messages``'s (its
names stay importable from here)."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse

from tow import access, i18n
from tow.adopt import unmarked_hash
from tow.check import PREVIOUS_REVISION_ACTIVE, blocked_by_previous_revision
from tow.clock import format_ui_timestamp
from tow.config import as_bool, flash_ttl, interval_sec_of
from tow.episodes import expected_for_topic
from tow.errors import render_stored
from tow.issues import Issue, Snapshot, fingerprint, worst
from tow.jsonish import as_dict
from tow.log import error_class, owner_language
from tow.net_errors import humanize
from tow.progress import progress_summary
from tow.records import CheckRow, shown_title
from tow.status import TRACKER_WARNING_CLASSES, project_home_status
from tow.trackers import GenericHttpTracker, load_trackers, match_tracker
from tow.web import _context, services
from tow.web.messages import (  # noqa: F401 - the message helpers, importable from here as before
    FLASH_KINDS,
    flash_at,
    flash_location,
    flash_redirect,
    flash_text,
    home_redirect,
    message_text,
    record,
    request_flash,
    resolve_subject,
    topic_subject,
    use_subject,
)
from tow.web.messages import FLASHES as _FLASHES  # noqa: F401 - read by tests as views._FLASHES
from tow.web.messages import TokenStore as _TokenStore
from tow.web.text import format_bytes, t


def flash_ttl_sec() -> int:
    try:
        n = int(_context.config().get("flash_ttl_sec") or 60)
    except TypeError, ValueError:
        n = 60
    return flash_ttl(n)


def first_run(cfg: dict[str, Any]) -> bool:
    """No password yet and the owner has not put it off: Home opens the first-start page."""
    if as_bool(cfg.get("setup_done")):
        return False
    secrets = _context.secrets_or_none()
    return secrets is not None and not access.password_is_set(secrets)


# A refused add's draft and reason (D2/H2): in the memory of the web process only, like a message
# after an action (tow.web.messages) - it matters for the next page, a few seconds later. A
# restart forgets it: the page then opens an empty add form. Nothing of the owner's input touches
# the disk.
_ADD_DRAFTS = _TokenStore(ttl=600, limit=64)


def add_refused_redirect(problem: str, draft: dict[str, str], *, kind: str = "", page: str = "/") -> RedirectResponse:
    """D2/H2: back to the page (Home, or Sites for a new site) with the add form open, the draft
    and the reason shown once (no flash). The draft never holds a password."""
    record = {key: str(value) for key, value in draft.items() if value and key != "password"}
    record.update(error=problem, kind=kind)
    token = _ADD_DRAFTS.put(record)
    # No #new fragment: the browser's fragment scroll after load would take the focus away from
    # the field app.js puts it in (the open form is at the top of the page anyway).
    return RedirectResponse(page + "?add=" + quote(token), status_code=303)


def add_draft(request: Request) -> dict[str, str] | None:
    return _ADD_DRAFTS.get(request.query_params.get("add") or "")


def credential_prompt(request: Request) -> dict[str, Any] | None:
    topic_id = (request.query_params.get("credential_topic") or "").strip()
    if not topic_id or len(topic_id) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", topic_id):
        return None
    state = _context.state()
    topic = next((item for item in state.get("topics") or [] if str(item.get("id")) == topic_id), None)
    if not isinstance(topic, dict):
        return None
    tracker = match_tracker(load_trackers(_context.config()), str(topic.get("url") or ""))
    if tracker is None or not tracker.spec.get("login_path"):
        return None
    credentials = ((_context.secrets_or_none() or {}).get("trackers") or {}).get(tracker.name) or {}
    username = str(credentials.get("username") or "")
    browser_enabled = bool(tracker.spec.get("browser_auth"))
    operation_id = (request.query_params.get("browser_auth_id") or "").strip()
    browser_status = (
        services.browser_auth.status(topic_id, operation_id or None) if browser_enabled else {"status": "idle"}
    )
    return {
        "id": topic_id,
        "title": shown_title(topic),
        "tracker": tracker.name,
        "username": username,
        "browser_auth": browser_enabled,
        "browser_auth_status": browser_status,
    }


def row_class(row: CheckRow) -> str:
    """The class of a check row's error: the typed error's own, the text's for an old one."""
    return str(row.get("error_class") or error_class(str(row.get("error") or "")))


def _row_error(row: CheckRow) -> str:
    """A check row's error as Home's row says it: from its code, in the page's language, with the
    site by its title ("Fake Site B: no mirror answered", not "fakebee: ..."); the stored text
    for an error without a code."""
    text = str(row.get("error") or t("web.check.not_confirmed")).strip()
    record = row.get("error_record")
    if not isinstance(record, Mapping) or not record.get("code"):
        return text
    params = record.get("params")
    params = dict(params) if isinstance(params, Mapping) else {}
    if isinstance(params.get("tracker"), str) and params["tracker"]:
        params["tracker"] = site_title_of(params["tracker"])
    return render_stored(record["code"], params, text).strip() or text


def _feedback(text: str, kind: str = "ok", *, credential_topic: str = "") -> dict[str, str]:
    return {"text": text, "kind": kind, "credential_topic": credential_topic}


def feedback_redirect(feedback: Mapping[str, str]) -> RedirectResponse:
    """Home with what a manual check said (``check_feedback``): its message, and the sign-in
    prompt when the site asked for one. The message names the request's topic; a failure is
    shown as any message is, also while "Needs attention" lists the topic (the message is the
    click's result, the triangle what is still wrong)."""
    return home_redirect(feedback["text"], feedback["kind"], credential_topic=feedback["credential_topic"] or None)


def check_feedback(
    row: CheckRow | None, *, topic_id: str, adopted: bool = False, tracker_name: str = ""
) -> dict[str, str]:
    """What a manual check of one topic says, as data - ``text``, its ``kind`` and the
    ``credential_topic`` whose sign-in to ask for ("" for none) - the same for a check run at
    once and a queued one. The words never name the topic: the message's subject does.
    ``row``: the topic's result, None for a topic that is gone;
    ``adopted``: the owner has just adopted its torrent into TOW, which a check that changes
    nothing else confirms; ``tracker_name``: its site when it signs in."""
    if row is None:
        return _feedback(t("web.topics.not_found"), "err")
    if row.get("note"):  # its link or client changed meanwhile: nothing of this check was saved
        return _feedback(t("web.check.no_changes_reason", reason=row["note"]), "warn")
    failed = failed_check_feedback(row, topic_id=topic_id, tracker_name=tracker_name)
    if failed is not None:
        return failed
    if not row.get("ok"):  # an empty row: nothing says the check worked
        return _feedback(t("web.check.failed"), "err")
    if row.get("added"):
        return _feedback(t("web.check.added"))
    if row.get("started"):  # a torrent that waited for disk space, started now
        return _feedback(t("web.check.started"))
    if row.get("selection_updated"):
        return _feedback(t("web.check.selection_updated"))
    if row.get("changed"):
        return _feedback(t("web.check.changed"))
    if adopted:
        return _feedback(t("web.check.adopted"))
    if row.get("skipped"):
        return _feedback(t("web.check.no_changes_reason", reason=row["skipped"]))
    return _feedback(t("web.common.no_changes"))


def manual_check_flash(
    row: CheckRow | None, *, topic_id: str, tracker_name: str = "", new_topic: bool = False
) -> RedirectResponse | None:
    """Home after a failed check by hand (``failed_check_feedback``); None for a row that is ok."""
    feedback = failed_check_feedback(row, topic_id=topic_id, tracker_name=tracker_name, new_topic=new_topic)
    return feedback_redirect(feedback) if feedback is not None else None


def failed_check_feedback(
    row: CheckRow | None, *, topic_id: str, tracker_name: str = "", new_topic: bool = False
) -> dict[str, str] | None:
    """A failed check by hand, in words: "added to TOW, ..." only for the first check of a
    topic just added (``new_topic``); a check of a topic TOW already had says the check failed."""
    if not row or row.get("ok"):
        return None
    if row.get("status") == "verification_pending":
        return _feedback(t("web.check.verifying"), "info")  # deferred: TOW confirms it later
    error = humanize(_row_error(row))
    if row.get("status") == "waiting_space":
        # Not a failed check: the torrent is in the client, stopped, and TOW starts it once its
        # files fit (the error says so). Just added now, or still waiting from an earlier check.
        return _feedback(t("web.check.added_waiting_space", error=error) if row.get("added") else error, "warn")
    cls = row_class(row)
    record = row.get("error_record")
    code = str(record.get("code") or "") if isinstance(record, Mapping) else ""
    # The previous version still seeding is TOW's own guard, not something the client refused
    # ("The torrent client did not confirm: the previous version … is still active").
    if cls == "qbit" and code != PREVIOUS_REVISION_ACTIVE:
        flash = t("web.check.added_client_refused" if new_topic else "web.check.client_refused", error=error)
    else:
        flash = t("web.check.added_check_failed" if new_topic else "web.check.check_failed", error=error)
    # The status-colour contract (AGENTS.md): a site that does not answer is amber, not an error.
    kind = "warn" if cls in TRACKER_WARNING_CLASSES else "err"
    return _feedback(flash, kind, credential_topic=topic_id if cls == "tracker_auth" and tracker_name else "")


def topic_check_row(topic_id: str, out: dict[str, Any]) -> CheckRow | None:
    return next((r for r in out.get("results") or [] if str(r.get("id")) == str(topic_id)), None)


# A row of a check's report in the status-colour contract (AGENTS.md): a failure of the site's
# transport is amber, any other failure red, waiting amber, new data blue, a skip grey.
_REPORT_TONES = {"pending": "warn", "skipped": "mut", "new": "new", "same": "ok"}


def _report_tone(outcome: str, row: CheckRow) -> str:
    if outcome == "fail":
        return "warn" if row_class(row) in TRACKER_WARNING_CLASSES else "bad"
    return _REPORT_TONES[outcome]


def check_report(out: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    """Mutually exclusive outcomes: waiting/skipped/unsaved are not failed or unchanged. Each row
    has its colour (``tone``); the report's ``kind`` is its worst result's (a red failure stays
    red however many rows passed); a reason that only repeats its row's label is left out."""
    counts = services.check_outcome_counts(out.get("results") or [])
    details: list[dict[str, str]] = []
    tones: set[str] = set()
    labels = {
        "new": "web.check_all.outcome_new",
        "same": "web.check_all.outcome_same",
        "pending": "web.check_all.outcome_pending",
        "skipped": "web.check_all.outcome_skipped",
        "fail": "web.check_all.outcome_fail",
    }
    priority = {"fail": 0, "pending": 1, "skipped": 2, "new": 3, "same": 4}
    for row in sorted(out.get("results") or [], key=lambda item: priority[services.check_outcome(item)]):
        outcome = services.check_outcome(row)
        if outcome == "skipped":
            reason = str(row.get("note") or row.get("skipped") or t("web.common.no_changes"))
        elif outcome == "pending":
            reason = t("web.check.verifying") if row["status"] == "verification_pending" else _row_error(row)
        elif outcome == "fail":
            record = row.get("error_record")
            reason = (
                services.site_error_summary(record, fallback=_row_error(row))
                if isinstance(record, Mapping)
                else _row_error(row)
            )
        elif outcome == "new":
            reason = t(
                "web.check.selection_updated"
                if row.get("selection_updated")
                else "web.check.started"
                if row.get("started")
                else "web.check.added"
                if row.get("added")
                else "web.check.changed"
            )
        else:
            reason = t("web.common.no_changes")
        tone = _report_tone(outcome, row)
        tones.add(tone)
        label = t("web.check_all.outcome_preview" if not apply and outcome == "new" else labels[outcome])
        if outcome == "same" or reason.strip().casefold() in {label.casefold(), t("web.common.no_changes").casefold()}:
            reason = ""  # "Unchanged ... no changes" said it twice
        if len(details) < 100:
            topic_id = str(row.get("id") or "")
            details.append(
                {
                    "topic_id": topic_id if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic_id) else "",
                    "title": services.scrub_text(shown_title(row))[:200],
                    "outcome": outcome,
                    "tone": tone,
                    "label": label,
                    "reason": humanize(services.scrub_text(reason))[:1000],
                }
            )
    prefix = t("web.check_all.applied" if apply else "web.check_all.preview")
    count_keys = {
        "new": "web.check_all.updated_count",
        "same": "web.check_all.unchanged_count",
        "pending": "web.check_all.waiting_count",
        "skipped": "web.check_all.skipped_count",
        "fail": "web.check_all.failed_count",
    }
    summary = prefix + (
        ", ".join(
            t("web.check_all.would_update_count" if not apply and key == "new" else count_keys[key], count=value)
            for key, value in counts.items()
            if value
        )
        or t("web.check_all.empty")
    )
    kind = "err" if "bad" in tones else "warn" if "warn" in tones else "ok"
    return {"summary": summary, "counts": counts, "rows": details, "total": sum(counts.values()), "kind": kind}


def check_progress(job: Mapping[str, Any]) -> str:
    """Where a manual check that waits or runs is, in words (Home's note and its polling)."""
    if job.get("status") == "running":
        since = job.get("running_since")
        return t("web.check_all.progress_running", at=_epoch_time(since) if isinstance(since, (int, float)) else "—")
    if isinstance(job.get("waiting_since"), (int, float)):
        return t("web.check_all.progress_waiting", at=_epoch_time(job["waiting_since"]))
    return t("web.check_all.progress_queued", position=job.get("position") or 1)


def result_location(job: Mapping[str, Any]) -> str:
    """The one address of a manual check's result, shown from the job whenever it opens (for
    the job's hour - longer than a message token lives), with the sign-in prompt the check
    asked for."""
    feedback = job.get("feedback")
    credential = feedback.get("credential_topic") if isinstance(feedback, Mapping) else ""
    query = "check_result=" + quote(str(job["id"]))
    if credential:
        query += "&credential_topic=" + quote(str(credential))
    # A topic's check ends in one message, not a result card to scroll to.
    return f"/?{query}" if job.get("topic_id") else f"/?{query}#check-result"


def check_result_flash(job: Mapping[str, Any]) -> dict[str, str] | None:
    """The one message a finished check of a topic ends in, shown on its result's page as a
    check at once shows it (``check_feedback``) - also when it ended without words of its own
    (cancelled, never given the check lock, stopped by an error of TOW). None for a check of
    every topic (its report is the page's card) and for one that still waits or runs."""
    if not job.get("topic_id") or job.get("status") not in {"done", "failed"}:
        return None
    feedback = job.get("feedback")
    said = feedback if isinstance(feedback, Mapping) and feedback.get("text") else {}
    stopped = job.get("cancelled") or job.get("lock_timeout")
    ended = job.get("ended_at")
    return record(
        str(said.get("text") or check_job_message(job)),
        str(said.get("kind") or job.get("kind") or ("info" if job.get("cancelled") else "warn" if stopped else "err")),
        # Named when it was asked for: a topic deleted meanwhile keeps its name.
        subject=str(job.get("subject") or ""),
        # One identity per job: the result page opened again is not a new message.
        message_id="check-" + str(job.get("id") or ""),
        at=ended if isinstance(ended, (int, float)) else None,
    )


def check_status_message(job: Mapping[str, Any] | None, *, key: str = "") -> dict[str, str]:
    """The one message of a manual check a page follows (``/check/status``, D10), by the same id
    ("check-<job>") while it waits or runs and when it has ended, so the page updates it in place:
    where it is (info); a topic's result, with its sign-in when the site asked for one; Check all's
    "finished", coloured by its worst result, with its report; or, for a check this server does
    not know (``key``: the page's action id), that its result is unavailable."""
    if job is None:
        return record(
            t("web.check.result_unavailable"),
            "warn",
            message_id="check-unknown-" + re.sub(r"[^A-Za-z0-9_-]", "", key)[:64],
            href="/history",
            link=t("web.check.history_link"),
        )
    message_id = "check-" + str(job.get("id") or "")
    subject = str(job.get("subject") or "")
    if job.get("status") in {"queued", "running"}:
        return record(check_progress(job), "info", subject=subject, message_id=message_id)
    topic = check_result_flash(job)
    if topic is not None:
        feedback = job.get("feedback")
        credential = feedback.get("credential_topic") if isinstance(feedback, Mapping) else ""
        if credential:
            topic.update(href="/?credential_topic=" + quote(str(credential)), link=t("web.check.sign_in"))
        return topic
    report = job.get("report")
    ended = job.get("ended_at")
    if not isinstance(report, Mapping):
        return record(check_job_message(job), str(job.get("kind") or "err"), message_id=message_id)
    return record(
        t("web.check_all.done_short"),
        str(report.get("kind") or job.get("kind") or "ok"),
        message_id=message_id,
        at=ended if isinstance(ended, (int, float)) else None,
        href=f"/?check_result={quote(str(job['id']))}#check-result",
        link=t("web.check_all.view_results"),
    )


def check_job_message(job: Mapping[str, Any]) -> str:
    """What a finished manual check says; for one that ended without a word of its own
    (cancelled, never given the check lock, stopped by an error of TOW itself), why."""
    if job.get("flash"):
        return str(job["flash"])
    if job.get("cancelled"):
        return t("web.check_all.cancelled")
    if job.get("lock_timeout"):
        return t("web.check_all.lock_timeout")
    return t("web.check_all.internal_error")


def ui_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        return format_ui_timestamp(value)
    except TypeError, ValueError:
        return "—"


_STORED_ZONE = re.compile(r"(?P<when>.+?) (?:[A-Z]{2,5} )?UTC(?:(?P<sign>[+-])(?P<hours>\d{2}):(?P<minutes>\d{2}))?")


def stored_ui_time(value: Any) -> str:
    """A time a check stored - an ISO time, or (TOW 1.24 and before) a text written in the page
    language of that moment ("07.10.2026 12:16:13 UTC+03:00") - written in this page's language.
    A text that is neither is shown as it is."""
    text = str(value or "").strip()
    if not text:
        return ""
    with suppress(TypeError, ValueError):
        return format_ui_timestamp(text)
    match = _STORED_ZONE.fullmatch(text)
    if match is None:
        return text
    minutes = int(match["hours"] or 0) * 60 + int(match["minutes"] or 0)
    zone = timezone(timedelta(minutes=-minutes if match["sign"] == "-" else minutes))
    for code in i18n.codes():
        with suppress(ValueError):
            moment = datetime.strptime(match["when"], i18n.datetime_pattern(code)).replace(tzinfo=zone)
            return format_ui_timestamp(moment)
    return text


# Event kind -> catalog key of its short label.
_EVENT_LABELS = {
    "torrent_completed": "web.event.file",
    "client_added": "web.event.file",
    "new_file": "web.event.file",
    "episode_completed": "web.event.file",
    "file_completed": "web.event.file",
    "client_removed": "web.event.file",
}


def _event_display(event: Mapping[str, Any]) -> str:
    kind = str(event.get("kind") or "")
    if event.get("label_code"):  # TOW's own wording ("Back in the client"): the reader's language
        # A label of its own, so a capital letter: "files" stood in bold beside "S02E03".
        text = render_stored(event["label_code"], event.get("label_params"), str(event.get("label") or ""))
        return text[:1].upper() + text[1:]
    if event.get("label"):
        return str(event["label"])
    label = _EVENT_LABELS.get(kind)
    return t(label) if label else kind


def event_output(event: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(event)
    if row.get("at"):
        row["event_at"] = ui_time(row["at"])
    row["display"] = _event_display(row)
    return row


def issues_now(state: Mapping[str, Any], cfg: Mapping[str, Any]) -> list[Issue]:
    """G3: what needs the owner now (tow.issues), from this request's state and config."""
    raw_health = state.get("health")
    health: dict[str, Any] = raw_health if isinstance(raw_health, dict) else {}
    snapshot = Snapshot(
        state=state,
        config=cfg,
        now=time.time(),
        next_check_at=services.next_check_at(interval_sec_of(cfg), health),
        # The note of a lost data folder matters only while no topic is watched: not read then.
        data_lost=None if state.get("topics") else services.data_lost(),
    )
    return services.collect_issues(snapshot)


def issue_text(issue: Issue) -> str:
    """The issue's message in the page's language; a time in the page's way of writing times."""
    params = dict(issue.message.params)
    if isinstance(params.get("at"), (int, float)):
        params["at"] = ui_time(datetime.fromtimestamp(params["at"], UTC).isoformat())
    if issue.kind == "topic_error":
        params["title"] = services.short_series_title(str(params.get("title") or ""), owner_language())
    rendered = render_stored(issue.message.key, params, t(issue.message.key, **params))
    return services.scrub_text(humanize(rendered)) if issue.kind == "topic_error" else rendered


# The page a Target names -> its address.
ISSUE_PAGES = {"home": "/", "settings": "/settings", "history": "/history", "sites": "/sites"}


def issue_note_id(issue: Issue) -> str:
    """The id of the issue's own note on its page (a Target without an anchor lands there)."""
    return "issue-" + re.sub(r"[^A-Za-z0-9_-]+", "-", issue.id)


def issue_href(issue: Issue) -> str:
    """Where the issue's link goes: its page with the page's own filters, what that page opens
    (``open``), the control that fixes it (``fix``, focused there) and the spot that shows the
    problem (highlighted there).

    With a fix the spot is ``spot=``, not ``#anchor``: a browser does not honour ``autofocus`` on
    a page whose address has a fragment (Chromium), and without a script the focus on the fix is
    all there is. app.js brings the spot into view and lights it up. Without a fix the spot is the
    fragment: the browser scrolls to it and the CSS lights it up, with or without a script."""
    from urllib.parse import urlencode

    target = issue.target
    spot = target.anchor or issue_note_id(issue)
    params = [*target.filter]
    if target.open:
        params.append(("open", target.open))
    if issue.fix is None:
        query = urlencode(params)
        return f"{ISSUE_PAGES[target.page]}{'?' + query if query else ''}#{spot}"
    params += [("fix", issue.fix.ref), ("spot", spot)]
    return f"{ISSUE_PAGES[target.page]}?{urlencode(params)}"


def attention_view(state: Mapping[str, Any], cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Current header problems, worst first, with stable identities and links to their fixes."""
    # Past history loss remains a note on History, not an active failure counter.
    found = [issue for issue in issues_now(state, cfg) if issue.kind != "history_rebuilt"]
    return {
        "entries": [
            {
                "id": issue.id,
                "kind": issue.kind,
                "severity": issue.severity,
                "subjects": list(issue.subjects),
                "text": issue_text(issue),
                "href": issue_href(issue),
                "fix": issue.fix.ref if issue.fix else "",
                "fix_label": t(issue.fix.label) if issue.fix and issue.fix.label else "",
            }
            for issue in found
        ],
        "fingerprint": fingerprint(found),
        "tone": worst(found),
        "count": len(found),
        "subjects": sorted({subject for issue in found for subject in issue.subjects}),
        "label": t("home.attention_show", count=len(found)),
    }


def issue_notes(state: Mapping[str, Any], cfg: Mapping[str, Any], page: str) -> list[dict[str, str]]:
    """The issues ``page`` says in words: those whose link lands on their own note (a Target
    without an anchor: ``at`` is "", ``id`` the note's) and those whose spot does not say what is
    wrong by itself (``Target.note``: ``at`` is that spot's id, the note goes inside it). Each
    note keeps its issue's ``severity`` (its colour) and id (``issue``, stable while it lasts)."""
    return [
        {
            "id": issue_note_id(issue),
            "issue": issue.id,
            "severity": issue.severity,
            "at": issue.target.anchor,
            "text": issue_text(issue),
        }
        for issue in issues_now(state, cfg)
        if issue.target.page == page and (issue.target.note or not issue.target.anchor)
    ]


def client_notes(state: Mapping[str, Any], cfg: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """The clients that do not answer now, by id: their card's one note is their ``client_down``
    issue (``issue``: its id, ``severity``), on the same evidence as the header and Needs
    attention. The kind names the client first among its subjects."""
    return {
        issue.subjects[0]: {"issue": issue.id, "severity": issue.severity}
        for issue in issues_now(state, cfg)
        if issue.kind == "client_down" and issue.subjects
    }


def attention(state: Mapping[str, Any], cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The entries of ``attention_view``."""
    items: list[dict[str, Any]] = attention_view(state, cfg)["entries"]
    return items


def _search_href(tracker: GenericHttpTracker | None, state: Mapping[str, Any], query: str) -> str:
    """E2: the site's search link for ``query`` (the site's "search_path", else its preset's).
    A link only - TOW never fetches or scrapes search results."""
    from urllib.parse import quote

    from tow.trackers import presets

    if tracker is None or not query.strip():
        return ""
    spec = getattr(tracker, "spec", {}) or {}
    path = str(spec.get("search_path") or presets.search_path(tracker.name))
    hosts = [str(h).rstrip("/") for h in spec.get("fetch_hosts") or [] if h]
    mirror = ((state.get("mirrors") or {}).get(tracker.name) or {}).get("active")
    host = str(mirror or (hosts[0] if hosts else "")).rstrip("/")
    if not path or not host:
        return ""
    return host + path.replace("{q}", quote(query.strip()))


def _next_season_query(title: str, series: str, summary: Mapping[str, Any]) -> str:
    """For a complete season: "<name> <season + 1>" (E2)."""
    from tow.episodes import parse_season_hint

    if not summary.get("is_complete"):
        return ""
    season = parse_season_hint(title)
    return f"{series} {season + 1}" if season else ""


def _error_line(error: str, cls: str, site: str = "") -> str:
    """One short line for the row (D4): the error's class in words, then its advice
    (the part after " — ") or its start; the full text stays in the edit panel. The row has
    its own Site column, so a leading "<site>: " is left out."""
    from tow.log import CLS_RU

    text = " ".join(str(error or "").split())
    if site and text.casefold().startswith(f"{site}: ".casefold()):
        text = text[len(site) + 2 :]
    if not text:
        return ""
    lang = owner_language()
    if cls == "gone":
        # Every mirror said 404/410: "removed from the site" is the reason; the transport words
        # ("no mirror answered: … error 404") contradict it and stay in the tooltip.
        label = CLS_RU.label(cls, lang, cls)
        return label[:1].upper() + label[1:]
    detail = text.split(" — ", 1)[1] if " — " in text else text
    for internal, key in _ERROR_WORDS:
        detail = detail.replace(internal, i18n.t(key, lang) if key else "")
    if len(detail) > 90:
        detail = detail[:89].rstrip() + "…"
    label = CLS_RU.label(cls, lang, i18n.t("web.error_line.error", lang))
    # A paused mirror's own words already say what happened ("all mirrors are paused"), and so do
    # a disk's ("waiting for disk space: …"): no "Mirror paused:" or "Low disk space:" in front.
    same = detail.lower().startswith(label.lower()) or cls in ("frozen", "disk")
    line = detail if same else f"{label}: {detail}"
    return line[:1].upper() + line[1:]


# Frequent internal phrases in the owner's words for the row (catalog keys); the raw text stays
# in the tooltip.
_ERROR_WORDS = (
    ("all hosts failed: ", ""),
    ("all hosts failed", "web.error_words.all_hosts_failed"),
    ("previous torrent revision is still active on an overlapping file: ", "web.error_words.previous_revision"),
    ("no download link on page", "web.error_words.no_download_link"),
    ("network unavailable", "web.error_words.network"),
    ("timeout", "web.error_words.timeout"),
    ("http 503", "web.error_words.http_503"),
    ("http 502", "web.error_words.http_502"),
    ("http 404", "web.error_words.http_404"),
    ("not a torrent", "web.error_words.not_torrent"),
)


def _site_title(cfg: Mapping[str, Any], name: str) -> str:
    """A site as the pages name it ("NNM-Club"), not its settings key ("nnmclub")."""
    site = (cfg.get("trackers") or {}).get(name)
    return str((site.get("title") if isinstance(site, Mapping) else "") or name)


def site_title_of(name: str) -> str:
    """A site by the title the pages show (`_site_title` of the current settings)."""
    return _site_title(_context.config(), name)


def _with_site_title(params: Any, tracker: Any, title: str) -> Any:
    """Stored error values with the site's key replaced by its title (the row and the edit panel
    said "nnmclub: all mirrors are paused")."""
    if not isinstance(params, Mapping) or tracker is None or params.get("tracker") != tracker.name:
        return params
    return {**params, "tracker": title}


def topic_rows(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    cfg = _context.config()
    _context.trackers()  # a site whose link pattern is broken fails the page, as it always did
    history = services.load_download_history()
    timers = services.topic_timer_status(dict(state))
    retries = services.site_retry_status(dict(state), cfg)
    rows = []
    for topic in state.get("topics") or []:
        url = topic.get("url") or ""
        tr = _context.tracker_of(url)
        # The site's title finds the next season and the topic on the site; the row shows the
        # owner's own name once set.
        title = topic.get("tracker_title") or topic.get("title") or ""
        summary = topic_progress_summary(topic, history)
        record = (history.get("topics") or {}).get(str(topic.get("id") or "")) or {}
        event = record.get("last_event") or {}
        status = project_home_status(topic, record)
        series = title.split(" / ")[0]
        next_season = _next_season_query(title, series, summary)
        # The error in the reader's language (from its code); an old record shows its stored text.
        site = _site_title(cfg, tr.name) if tr else ""
        original_error = render_stored(
            topic.get("last_error_code"),
            _with_site_title(topic.get("last_error_params"), tr, site),
            str(topic.get("last_error") or ""),
        )
        last_error = services.site_error_summary(
            {
                "code": topic.get("last_error_code"),
                "params": _with_site_title(topic.get("last_error_params"), tr, site),
            },
            fallback=str(topic.get("last_error") or ""),
        )
        rows.append(
            {
                **topic,
                "timer": timers.get(str(topic.get("id")), {}),
                "site_retry_at": stored_ui_time(retries.get(str(topic.get("id"))))
                if retries.get(str(topic.get("id")))
                else "",
                # Stored as an ISO time (by TOW 1.24 and before: as text in the language of the
                # check): shown in this page's language.
                "last_check": stored_ui_time(topic.get("last_check")),
                "last_ok_at": stored_ui_time(topic.get("last_ok_at")),
                "last_error": last_error,
                "last_error_details": services.diagnostic_details(
                    {"code": topic.get("last_error_code"), "params": topic.get("last_error_params")}
                )
                or (services.scrub_text(original_error)[:8192] if original_error != last_error else ""),
                "replaces_revision": blocked_by_previous_revision(topic),
                "adoptable": bool(unmarked_hash(topic)),
                "tracker": tr.name if tr else t("web.site_unknown"),
                "name": shown_title(topic),
                "series": shown_title(topic).split(" / ")[0],
                "download_summary": summary,
                "last_event_label": _event_display(event) if event else "",
                "last_event_at": ui_time(event.get("at")) if event else "",
                "last_event_iso": str(event.get("at") or "") if event else "",
                "last_error_class": (
                    cls := str(topic.get("last_error_class") or "")
                    or (
                        error_class(str(topic.get("last_error") or ""), topic.get("last_error_code"))
                        if last_error
                        else ""
                    )
                ),
                # The site asks for a sign-in: the row opens the prompt ("Needs attention" sends
                # the owner to the row, and the row had no way to sign in).
                "sign_in_href": (
                    "/?credential_topic=" + quote(str(topic.get("id") or ""))
                    if cls == "tracker_auth" and tr is not None and tr.spec.get("login_path")
                    else ""
                ),
                "last_error_human": humanize(last_error),
                "error_line": t(
                    "site_access.row_no_connection"
                    if services.site_incident(
                        {"code": topic.get("last_error_code"), "params": topic.get("last_error_params")}
                    )
                    == "mirrors.connection_retry"
                    else "site_access.row_limited"
                )
                if original_error != last_error and cls in {"tracker", "cloudflare"}
                else _error_line(humanize(last_error), cls, site),
                "search_href": _search_href(tr, state, series),
                "next_season_href": _search_href(tr, state, next_season) if next_season else "",
                "torrent_tone": status.torrent_tone,
                "torrent_label": status.torrent_label,
                "tracker_tone": status.tracker_tone,
                "tracker_label": status.tracker_label,
            }
        )
    return rows


def topic_progress_summary(topic: Mapping[str, Any], history: Mapping[str, Any]) -> dict[str, Any]:
    summary = progress_summary(str(topic.get("id") or ""), history)
    pending = as_dict(topic.get("verification_pending"))
    if pending:
        summary.update(is_complete=False, completion_known=False)
    if pending and not pending.get("blocked"):
        summary.update(verifying=not bool(topic.get("last_error")), is_complete=False, completion_known=False)
    if summary.get("expected") is not None:
        return summary
    expected = expected_for_topic(topic)
    if not expected or not expected.get("total"):
        return summary
    return {
        **summary,
        "expected": int(expected["total"]),
        "completion_known": False,
        "is_complete": False,
    }


def service_view() -> dict[str, Any]:
    """The service card of Settings: the status, the watchdog's last problem, autostart and the
    last requested restart, in words."""
    service = services.service_status()
    from tow.diagnostic_json import epoch
    from tow.pulse import clock

    problem = services.watchdog_problem()
    problem_at = epoch(problem.get("at")) if problem else None
    service["watchdog_problem"] = (
        {"at": clock(problem_at) if problem_at else "—", "lines": str(problem["text"]).splitlines()}
        if problem
        else None
    )
    # The same finding as Diagnostics: this account's autostart starts another TOW folder, so
    # the unchecked box here is not "nothing is set up".
    detail = service.get("autostart_detail")
    service["autostart_foreign"] = (
        isinstance(detail, dict)
        and detail.get("ours") is False
        and not detail.get("stale")
        and detail.get("state", "present") == "present"
    )
    # Its program is gone (that folder was moved or deleted): the OS keeps trying in vain, and
    # turning autostart on here takes it over.
    service["autostart_stale"] = services.stale_autostart(detail)
    supervisor = service.get("supervisor")
    server = supervisor.get("server") if isinstance(supervisor, dict) else None
    up_since = server.get("up_since") if isinstance(server, dict) else None
    try:
        service["running_since"] = format_ui_timestamp(str(up_since)) if up_since else None
    except TypeError, ValueError:
        service["running_since"] = None
    restart = service.get("restart")
    if not isinstance(restart, dict):
        service["restart_view"] = None
        return service
    status = str(restart.get("status") or "unknown")
    reason = {
        "settings": t("web.service.reason.settings"),
        "restore": t("web.service.reason.restore"),
        "manual": t("web.service.reason.manual"),
    }.get(str(restart.get("reason") or "settings"), t("web.service.reason.unknown"))
    state = {
        "ready": t("web.service.state.ready"),
        "queued": t("web.service.state.queued"),
        "stopping": t("web.service.state.stopping"),
        "starting": t("web.service.state.starting"),
        "started": t("web.service.state.started"),
        "failed": t("web.service.state.failed"),
    }.get(status, t("web.service.state.unknown"))
    created_at = restart.get("created_at")
    try:
        at = format_ui_timestamp(str(created_at)) if created_at else t("web.service.date_unknown")
    except TypeError, ValueError:
        at = t("web.service.date_unknown")
    service["restart_view"] = {
        "at": at,
        "reason": reason,
        "state": state,
        "failed": status == "failed",
    }
    return service


def _epoch_time(value: float) -> str:
    return format_ui_timestamp(datetime.fromtimestamp(value, UTC).isoformat())


def backup_view(cfg: dict[str, Any], request: Request) -> dict[str, Any]:
    """Night copies: folder, last result, the newest copies; folders of both kinds of copies."""
    from tow.backup_retention import retention_settings
    from tow.locations import LOCATIONS, is_default, resolve
    from tow.snapshots import status_failed

    folders = {}
    for kind, location in LOCATIONS.items():
        raw = str(cfg.get(location.key) or "")
        path = resolve(raw, location)
        free = services.free_bytes(path)
        folders[kind] = {
            "kind": kind,
            "title": location.title,
            "value": "" if is_default(raw, location) else raw,
            "path": str(path),
            "default_path": str(resolve("", location)),
            "is_default": is_default(raw, location),
            "free": format_bytes(free) if free is not None else "",
        }
    st = services.snapshot_status()
    copies = services.list_snapshots(limit=None)
    snapshots = [
        {"name": row["name"], "at": format_ui_timestamp(str(row["created_at"])), "size": format_bytes(row["bytes"])}
        for row in copies
    ]
    last_ok = st.get("last_ok_at")
    failed_at = st.get("last_error_at")
    cleanup = services.restore_point_cleanup_status(cfg=cfg)
    night_cleanup = services.night_cleanup_status(cfg=cfg)
    retention = retention_settings(cfg)
    return {
        "folders": folders,
        "snapshots": snapshots,
        "total_size": format_bytes(sum(row["bytes"] for row in copies)),
        "automatic": as_bool(cfg.get("backup_enabled"), True),
        "retention": retention,
        # Written as the list of copies writes them (the year too, in every language).
        "last_ok": _epoch_time(last_ok)
        if isinstance(last_ok, (int, float))
        else (snapshots[0]["at"] if snapshots else ""),
        "failed": status_failed(st),
        "read_error": st.get("read_error") is True,
        "error": str(st.get("last_error") or ""),
        "error_at": _epoch_time(failed_at) if isinstance(failed_at, (int, float)) else "",
        "cleanup_pending": night_cleanup["pending"] is True,
        "cleanup_read_error": night_cleanup["read_error"],
        "cleanup_unrecorded": night_cleanup["pending"] is None,
        "cleanup_legacy": night_cleanup.get("legacy") is True and night_cleanup["pending"] is None,
        "point_cleanup_pending": cleanup["pending"] is True,
        "point_cleanup_read_error": cleanup["read_error"],
        "point_cleanup_unrecorded": cleanup["pending"] is None,
        "point_cleanup_legacy": cleanup.get("legacy") is True and cleanup["pending"] is None,
    }
