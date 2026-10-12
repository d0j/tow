"""A site asks for a sign-in: one issue per site, counted over the topics (not paused) whose
last check it refused."""

from __future__ import annotations

from collections.abc import Mapping
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


def _issue(config: Mapping[str, Any], topic_id: str, tracker: GenericHttpTracker | None) -> Issue:
    name = tracker.name if tracker else ""
    message = (
        Message("issues.site_needs_login", {"site": _title(config, name), "count": 1})
        if tracker
        else Message("issues.site_needs_login_unknown", {"count": 1})
    )
    row = f"row-{topic_id}"
    shown = (("f", "problem"), ("t", name)) if name else (("f", "problem"),)
    sign_in = tracker is not None and bool(tracker.spec.get("login_path"))
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
        # A site without a sign-in page has no sign-in to offer: the open row shows the error.
        fix=Fix("sign_in", topic_id, label="home.topic.sign_in") if sign_in else None,
    )


def collect(snapshot: Snapshot) -> list[Issue]:
    trackers = load_trackers(snapshot.config)
    found: list[tuple[tuple[bool, str], Issue]] = []
    for topic in snapshot.topics:
        if topic.get("paused") or topic.get("last_error_class") != CLASS:
            continue
        tracker = match_tracker(trackers, str(topic.get("url") or ""))
        order = (tracker is None, _title(snapshot.config, tracker.name).casefold() if tracker else "")
        found.append((order, _issue(snapshot.config, str(topic.get("id") or ""), tracker)))
    # By the site's name, an unknown site last; collect() makes one issue of each site's topics.
    return [issue for _order, issue in sorted(found, key=lambda item: item[0])]
