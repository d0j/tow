"""What an issue is: the problem, how bad it is, where it is shown and what fixes it.

Everything here is plain data. A kind module (``tow.issues.kinds``) makes issues from a
``Snapshot``; the web turns a ``Target`` into a link and a ``Fix`` into the control it focuses,
the CLI counts them. Nothing here reads a file or knows a page's address.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# The tones of tow.status an issue can have, worst first: red is an actionable failure, amber
# is trouble that may pass or needs a look (AGENTS.md, the status-colour contract).
SEVERITIES = ("bad", "warn")


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What the kinds read, taken by the caller: the state, the config and the facts that live in
    files of their own (the next scheduled check, the note of a lost data folder)."""

    state: Mapping[str, Any]
    config: Mapping[str, Any]
    now: float
    next_check_at: float | None = None
    data_lost: Mapping[str, Any] | None = None

    @property
    def health(self) -> Mapping[str, Any]:
        health = self.state.get("health")
        return health if isinstance(health, Mapping) else {}

    @property
    def topics(self) -> list[Mapping[str, Any]]:
        topics = self.state.get("topics")
        return [topic for topic in topics if isinstance(topic, Mapping)] if isinstance(topics, list) else []


@dataclass(frozen=True, slots=True)
class Message:
    """A catalog key and its values. A value named ``at`` is a time (epoch seconds), shown in
    the reader's way of writing times; every other value is shown as it is."""

    key: str
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Target:
    """Where the issue is shown in full. ``page`` is a page's name (``home``, ``settings``,
    ``history``); ``open`` what that page opens (a Settings section, a Home topic); ``filter`` the
    page's own filters ((name, value) pairs); ``anchor`` the element that shows the problem.
    An empty anchor is the issue's own note on that page (``issue-<id>``). ``note``: the element
    does not say what is wrong by itself (a card, a settings row): the page shows the issue's
    text inside it."""

    page: str
    open: str = ""
    filter: tuple[tuple[str, str], ...] = ()
    anchor: str = ""
    note: bool = False


@dataclass(frozen=True, slots=True)
class Fix:
    """The control that resolves the issue: an action (``sign_in``, ``check_client``…), the
    thing it acts on, and the catalog key of the control's label."""

    action: str
    subject: str = ""
    label: str = ""

    @property
    def ref(self) -> str:
        """How the page names the control (``data-fix``): the action, ``:subject`` when it has one."""
        return f"{self.action}:{self.subject}" if self.subject else self.action


@dataclass(frozen=True, slots=True)
class Issue:
    """One thing that needs the owner now.

    ``id`` is stable while the problem lasts (the kind, ``:subject`` for one per subject); two
    issues with the same id are one problem seen twice and are merged by ``collect`` (counts
    added, subjects joined, ``target_many`` taken when it has one). ``subjects`` name what the
    problem is about: topic ids, a site, a client.
    """

    kind: str
    id: str
    severity: str
    message: Message
    target: Target
    fix: Fix | None = None
    count: int = 1
    subjects: tuple[str, ...] = ()
    target_many: Target | None = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"issue {self.id}: severity {self.severity!r} is not one of {SEVERITIES}")


def worst(issues: Iterable[Issue]) -> str:
    """The worst severity among ``issues`` ("" for none)."""
    found = {issue.severity for issue in issues}
    return next((severity for severity in SEVERITIES if severity in found), "")


def fingerprint(issues: Iterable[Issue]) -> str:
    """Names this set of problems: the same problems give the same value (in any order, in any
    language), a new or a different one another. Empty when there is none."""
    rows = sorted(
        json.dumps(
            [
                issue.id,
                issue.severity,
                issue.count,
                list(issue.subjects),
                issue.message.key,
                dict(issue.message.params),
            ],
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        for issue in issues
    )
    if not rows:
        return ""
    data = "\n".join(rows)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]
