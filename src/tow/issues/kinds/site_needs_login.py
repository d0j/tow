"""A site asks for a sign-in: one issue per site, counted over the topics (not paused) whose
last check it refused.

A site with a sign-in page asks the owner to sign in (Home, the topic's row). A site without one
has no sign-in to offer: its Sites row opens at the user name, where a sign-in is added.

Once the owner's sign-in to a site is confirmed (``signed_in_at`` of its ``mirrors`` entry, written
by Home's sign-in), the topics its last check refused before that are said to wait for their next
check instead: their error stays as the check left it, and nothing checks them before the owner
or the schedule does (each check may download a .torrent, and a site may limit those per day)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from tow.issues.model import Fix, Issue, Message, Snapshot, Target
from tow.status import error_tone
from tow.trackers import GenericHttpTracker, load_trackers, match_tracker

KIND = "site_needs_login"
ORDER = 60
CLASS = "tracker_auth"


def _title(config: Mapping[str, Any], name: str) -> str:
    """The site as the pages name it ("NNM-Club"), not its settings key ("nnmclub")."""
    site = (config.get("trackers") or {}).get(name)
    return str((site.get("title") if isinstance(site, Mapping) else "") or name)


def _no_sign_in(config: Mapping[str, Any], topic_id: str, tracker: GenericHttpTracker) -> Issue:
    """The site asks for a sign-in TOW has no way to make: its row on Sites, the user name
    focused (the row itself says "No sign-in needed", so the page shows why it is open)."""
    name = tracker.name
    spot = Target("sites", open=name, anchor=f"site-row-{name}", note=True)
    return Issue(
        kind=KIND,
        id=f"{KIND}:{name}",
        severity=error_tone(CLASS),
        message=Message("issues.site_login_missing", {"site": _title(config, name), "count": 1}),
        subjects=(topic_id,),
        target=spot,
        fix=Fix("site_login", name, label="sites.login"),
    )


def _signed_in_at(state: Mapping[str, Any], name: str) -> float | None:
    """When the owner's sign-in to the site was last confirmed (epoch seconds), if ever."""
    mirrors = state.get("mirrors")
    bucket = mirrors.get(name) if isinstance(mirrors, Mapping) else None
    at = bucket.get("signed_in_at") if isinstance(bucket, Mapping) else None
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return None
    return float(at)


def _checked_before(topic: Mapping[str, Any], at: float) -> bool:
    """The topic's last check ran before ``at`` (a check time TOW 1.24 and older wrote as text
    certainly did). Check times are kept to the second: one in the sign-in's second counts as
    before, as the sign-in's own check is."""
    try:
        checked = datetime.fromisoformat(str(topic.get("last_check") or "")).timestamp()
    except ValueError:
        return True
    return checked <= at


def _waits(config: Mapping[str, Any], topic_id: str, tracker: GenericHttpTracker) -> Issue:
    """Signed in since this topic was refused: its next check tells; "Check all" asks now."""
    name = tracker.name
    row = f"row-{topic_id}"
    return Issue(
        kind=KIND,
        id=f"{KIND}:{name}:signed_in",
        severity="warn",
        message=Message("issues.site_signed_in", {"site": _title(config, name), "count": 1}),
        subjects=(topic_id,),
        target=Target("home", open=topic_id, anchor=row),
        target_many=Target("home", open=topic_id, filter=(("f", "problem"), ("t", name)), anchor=row),
        fix=Fix("check_all", label="base.check_all.title"),
    )


def _issue(
    config: Mapping[str, Any], topic_id: str, tracker: GenericHttpTracker | None, *, signed_in: bool = False
) -> Issue:
    if tracker is not None and not tracker.spec.get("login_path"):
        return _no_sign_in(config, topic_id, tracker)
    if tracker is not None and signed_in:
        return _waits(config, topic_id, tracker)
    name = tracker.name if tracker else ""
    message = (
        Message("issues.site_needs_login", {"site": _title(config, name), "count": 1})
        if tracker
        else Message("issues.site_needs_login_unknown", {"count": 1})
    )
    row = f"row-{topic_id}"
    shown = (("f", "problem"), ("t", name)) if name else (("f", "problem"),)
    return Issue(
        kind=KIND,
        id=f"{KIND}:{name}",
        severity=error_tone(CLASS),
        message=message,
        subjects=(topic_id,),
        # One topic: its row, open. Several: Home shows the site's topics with a problem, the
        # first one open.
        target=Target("home", open=topic_id, anchor=row),
        target_many=Target("home", open=topic_id, filter=shown, anchor=row),
        # A topic of a site TOW does not know: the open row shows the error.
        fix=Fix("sign_in", topic_id, label="home.topic.sign_in") if tracker is not None else None,
    )


def collect(snapshot: Snapshot) -> list[Issue]:
    trackers = load_trackers(snapshot.config)
    found: list[tuple[tuple[bool, str], Issue]] = []
    for topic in snapshot.topics:
        if topic.get("paused") or topic.get("last_error_class") != CLASS or not topic.get("last_error"):
            continue
        tracker = match_tracker(trackers, str(topic.get("url") or ""))
        order = (tracker is None, _title(snapshot.config, tracker.name).casefold() if tracker else "")
        at = _signed_in_at(snapshot.state, tracker.name) if tracker else None
        signed_in = at is not None and _checked_before(topic, at)
        found.append((order, _issue(snapshot.config, str(topic.get("id") or ""), tracker, signed_in=signed_in)))
    # By the site's name, an unknown site last; collect() makes one issue of each site's topics.
    return [issue for _order, issue in sorted(found, key=lambda item: item[0])]
