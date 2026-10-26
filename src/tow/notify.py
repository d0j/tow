from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from tow import errors as tow_errors
from tow import i18n
from tow.i18n import t
from tow.log import scrub_text
from tow.records import Topic

_EPISODE_RANGE = re.compile(
    r"(?ix)\b(?:s?(?P<season>\d{1,2})[xх])(?P<start>\d{1,4})"
    r"(?:\s*[-–]\s*(?P<end>\d{1,4}))?"
    # Not "MVO (2x2)": a voice-over studio's name in brackets is no season and episode.
    r"(?!\s*[)\]])"
)
# "Серии: 1-12 из 12" / "Episodes 1-8 of 10": the episodes of a season the title names elsewhere.
_EPISODE_SPAN = re.compile(
    r"(?i)(?<![\dxх])(?P<start>\d{1,4})(?:\s*[-–—]\s*(?P<end>\d{1,4}))?"
    r"\s*(?:сери[яий]|эпизод\w*|episodes?)?\s*(?:из|of)\s*(?P<total>\d{1,4})\b"
)
_EPISODE_SE = re.compile(r"(?ix)\bS(?P<season>\d{1,2})E(?P<start>\d{1,4})(?:\s*[-–]\s*E?(?P<end>\d{1,4}))?\b")
_EPISODE_TOTAL = re.compile(r"(?ix)\b(?:из|of)\s*(?P<total>\d{1,4})\b")
_NOTIFICATION_PRIORITY = {
    "qbit_down": 70,
    "qbit_up": 70,
    "recovered": 10,
    "updated": 20,
    "added": 20,
    "removed": 30,
    "restored": 30,
    "new_file": 40,
    "revision": 40,
    "completed": 50,
    "season_complete": 55,
    "error": 60,
}
_CLIENT_MUTATIONS = frozenset({"added", "updated"})
_ACTIONS = frozenset({"added", "updated", "completed", "new_file", "revision", "removed", "restored"})
# Events the owner must hear about even when the same topic also failed in this run:
# the message keeps the event and carries the error.
_EVENTS = frozenset({*_CLIENT_MUTATIONS, "completed", "season_complete", "new_file", "revision", "removed", "restored"})


def error_parts(error: Any) -> tuple[Any, ...]:
    """The errors a notification carries: a text, a typed error, a stored record, or several."""
    if isinstance(error, tuple):
        return error
    return (error,) if error else ()


def _joined(*errors: Any) -> Any:
    """Every error once, in order (one error as it is, several as a tuple); rendered and joined
    with "; " when the message is composed, in the owner's language."""
    found: dict[str, Any] = {}
    for error in errors:
        for part in error_parts(error):
            found.setdefault(tow_errors.text_of(part, "en") if not isinstance(part, str) else part, part)
    values = tuple(found.values())
    return values[0] if len(values) == 1 else values or ""


def error_text(error: Any, lang: str) -> str:
    """The errors of a notification in ``lang``."""
    texts = (scrub_text(tow_errors.summary_of(part, lang)) for part in error_parts(error))
    return "; ".join(dict.fromkeys(text for text in texts if text))


@dataclass(frozen=True, slots=True)
class PendingNotification:
    kind: str
    operation_id: str
    topic: Topic
    tracker: str = ""
    error: Any = ""  # a text, a typed error (rendered in the owner's language) or a tuple of them
    episodes: str = ""
    recovered: bool = False  # an earlier error of this topic is gone (said in the same message)
    # The add stopped the topic's previous version first (tow.check.replace): said with it.
    previous_stopped: bool = False


class NotificationBatch:
    """Keep one highest-priority Telegram notification per topic per check."""

    def __init__(self) -> None:
        self._items: dict[str, PendingNotification] = {}

    def queue(
        self,
        topic: Topic,
        *,
        kind: str,
        operation_id: str,
        tracker: str = "",
        error: Any = "",
        episodes: str = "",
        previous_stopped: bool = False,
    ) -> None:
        topic_id = str(topic.get("id") or "")
        if not topic_id:
            return
        current = self._items.get(topic_id)
        if current and current.kind == "recovered" and kind not in {"recovered", "error"}:
            # "снова работает" must not disappear behind an add or a completion of the same topic.
            self._items[topic_id] = PendingNotification(
                kind, operation_id, topic, tracker, error, episodes, recovered=True, previous_stopped=previous_stopped
            )
            return
        if current and kind == "recovered" and current.kind not in {"recovered", "error"}:
            self._items[topic_id] = PendingNotification(
                current.kind,
                current.operation_id,
                current.topic,
                current.tracker,
                current.error,
                current.episodes,
                recovered=True,
                previous_stopped=current.previous_stopped,
            )
            return
        if current and kind == "error" and current.kind in _EVENTS:
            # An add, a completion or a removal really happened: a later error of the
            # same topic must not hide it; report both in one message.
            self._items[topic_id] = PendingNotification(
                current.kind,
                current.operation_id,
                current.topic,
                current.tracker,
                _joined(current.error, error),
                current.episodes,
                previous_stopped=current.previous_stopped,
            )
            return
        if current and current.kind == "error" and kind in _EVENTS:
            # The same, in the other order (reconcile events come after the check's error).
            self._items[topic_id] = PendingNotification(
                kind,
                operation_id,
                topic,
                tracker or current.tracker,
                _joined(current.error, error),
                episodes,
                previous_stopped=previous_stopped,
            )
            return
        if current:
            new_priority = _NOTIFICATION_PRIORITY.get(kind, 0)
            current_priority = _NOTIFICATION_PRIORITY.get(current.kind, 0)
            if new_priority < current_priority:
                if error and current.kind in _EVENTS:
                    self._items[topic_id] = PendingNotification(
                        current.kind,
                        current.operation_id,
                        current.topic,
                        current.tracker,
                        _joined(current.error, error),
                        current.episodes,
                        recovered=current.recovered,
                        previous_stopped=current.previous_stopped,
                    )
                return
            if new_priority == current_priority:
                if kind == "error" and error and _joined(error) != _joined(current.error):
                    error = _joined(current.error, error)
                elif not episodes:
                    return
                if current.episodes:
                    labels = list(dict.fromkeys((current.episodes, episodes)))
                    episodes = ", ".join(labels)
            if current.kind in _EVENTS and kind in _EVENTS:
                error = _joined(current.error, error)
        # A later event of the same topic keeps "works again" said with an earlier one.
        recovered = bool(current and current.recovered and kind in _EVENTS)
        self._items[topic_id] = PendingNotification(
            kind, operation_id, topic, tracker, error, episodes, recovered, previous_stopped
        )

    def __iter__(self) -> Iterator[PendingNotification]:
        return iter(self._items.values())


def short_series_title(title: str, lang: str | None = None) -> str:
    value = " ".join((title or "")[:4096].split()).strip()[:512]
    value = re.split(r"\s*\[[^\]]*\]", value, maxsplit=1)[0].strip()
    value = re.split(r"\s+[|/]\s+", value, maxsplit=1)[0].strip()
    value = re.sub(r"\s+\(\d{4}(?:-\d{4})?\).*$", "", value).strip()
    return value.strip(" -_|")[:100] or t("notify.topic_fallback", lang or i18n.message_language())


def _episode_text(title: str, lang: str) -> str:
    """The episodes a topic's title names, as "S01E01–08 из 10" ("" when it names none)."""
    source = (title or "")[:4096]
    match = _EPISODE_SE.search(source) or _EPISODE_RANGE.search(source)
    found_total = _EPISODE_TOTAL.search(source)  # "of 10" / "из 10" in the site's title
    total = int(found_total.group("total")) if found_total else None
    if match:
        season, start, end = int(match.group("season")), int(match.group("start")), match.group("end")
    else:
        # "Сезон: 2 / Серии: 1-12 из 12": the season and the episode count as tow.episodes reads
        # them for the topic's progress, the episodes from that count's own "a-b из c".
        from tow.episodes import parse_expected_count, parse_season_hint

        expected = parse_expected_count(source)
        hinted = parse_season_hint(source) if expected else None
        span = next(
            (
                found
                for found in _EPISODE_SPAN.finditer(source)
                if expected and int(found.group("total")) == int(expected["episode_max"])
            ),
            None,
        )
        if hinted is None or span is None:
            return ""
        season, start, end, total = hinted, int(span.group("start")), span.group("end"), int(span.group("total"))
    prefix = f"S{season:02d}E{start:02d}"
    value = f"{prefix}–{int(end):02d}" if end and int(end) != start else prefix
    return t("notify.episode_of", lang, episodes=value, total=total) if total else value


def _tracker_text(tracker: str) -> str:
    """The site as messages name it: its preset's name ("RuTracker"), else its own, capitalized."""
    from tow.trackers import presets

    value = " ".join((tracker or "").split()).strip()
    if not value:
        return ""
    return presets.label(value.lower()) or value[:40].capitalize()


def event_text(
    *,
    title: str,
    kind: str,
    error: Any = "",
    tracker: str = "",
    episodes: str = "",
    selected_files: int | None = None,
    total_files: int | None = None,
    recovered: bool = False,
    episode_source: str = "",
    previous_stopped: bool = False,
) -> str:
    """A message about a topic; ``episode_source`` is the site's title the episodes are read
    from when ``title`` is the owner's own name (by default ``title`` itself);
    ``previous_stopped``: an add that stopped the topic's previous version first says so."""
    lang = i18n.message_language()
    text = _event_text(
        lang,
        title=title,
        kind=kind,
        error=error,
        tracker=tracker,
        episodes=episodes,
        episode_source=episode_source,
        selected_files=selected_files,
        total_files=total_files,
        previous_stopped=previous_stopped,
    )
    if recovered and kind not in {"recovered", "error"}:
        return t("notify.recovered_suffix", lang, text=text)
    return text


# A failure's words in a message, at most this long and cut between words, so the step it ends
# with is not cut off mid-word (every messenger takes 1000 characters or more: tow.notifiers).
ERROR_CHARS = 400


def _brief(error: str | None) -> str:
    text = " ".join((error or "").split())
    if len(text) <= ERROR_CHARS:
        return text
    cut = text[: ERROR_CHARS - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > ERROR_CHARS // 2 else cut).rstrip(" ,;:—-") + "…"


def _event_text(
    lang: str,
    *,
    title: str,
    kind: str,
    error: Any = "",
    tracker: str = "",
    episodes: str = "",
    selected_files: int | None = None,
    total_files: int | None = None,
    episode_source: str = "",
    previous_stopped: bool = False,
) -> str:
    error = error_text(error, lang)
    if kind in {"qbit_down", "qbit_up"}:
        state = "down" if kind == "qbit_down" else "up"
        return t(f"notify.client_{state}_named", lang, name=title) if title else t(f"notify.client_{state}", lang)
    if kind == "recovered":
        return t("notify.works_again", lang, title=short_series_title(title, lang))
    if kind == "season_complete":
        text = t("notify.season_complete", lang, title=short_series_title(title, lang), episodes=episodes).rstrip()
        err = _brief(error)
        return text + (t("notify.failure_suffix", lang, error=err) if err else "")
    if kind == "error":
        err = _brief(error)
        name = short_series_title(title, lang)
        return (
            t("notify.failure_with_error", lang, title=name, error=err)
            if err
            else t("notify.failure", lang, title=name)
        )

    parts = [short_series_title(title, lang)]
    if _tracker_text(tracker):
        parts.append(_tracker_text(tracker))
    episode = " ".join((episodes or "").split()).strip() or _episode_text(episode_source or title, lang)
    if episode:
        parts.append(episode)
    action = t(f"notify.action.{kind}", lang) if kind in _ACTIONS else ""
    if action:
        if kind in {"added", "updated"} and selected_files is not None and total_files is not None:
            action += " " + t("notify.files_selected", lang, selected=selected_files, total=total_files)
        if kind in _CLIENT_MUTATIONS and previous_stopped:
            action += t("notify.previous_stopped_suffix", lang)
        parts.append(action)
    text = " — ".join(parts)
    err = _brief(error)
    if err and kind in _CLIENT_MUTATIONS:
        text += t("notify.then_failure_suffix", lang, error=err)
    elif err and kind in _EVENTS:
        text += t("notify.failure_suffix", lang, error=err)
    return text


def send(secrets: dict[str, Any], text: str) -> bool:
    """Send to every connected messenger; True when all of them delivered it.

    What a channel could not take is queued and retried (tow.notifiers).
    """
    from tow.notifiers import send_all

    results = send_all(secrets, text)
    return bool(results) and all(ok for ok, _ in results.values())
