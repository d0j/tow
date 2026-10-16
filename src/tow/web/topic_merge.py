"""A topic's edit form as the three-way merge sees it (``tow.web.edit_base``): the values the
panel showed, what a save asks for, the form ``topic_actions`` applies - the owner's changes on
the topic as it is now - and the rows a conflict shows.

Groups: the link, the name, the folder, the client, the selection (mode, rule and an exact
list's identity: one value), following, and the personal timer. Nothing a check writes is part
of the form, so a save never puts back what a check found meanwhile.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from dataclasses import replace
from typing import Any

from tow.clients.factory import client_configuration, client_name
from tow.errors import TowError
from tow.folders import paths_equal, resolve_save_path
from tow.guess import canon_watch_url
from tow.records import shown_title
from tow.selection import normalize_policy
from tow.topic_form import TopicForm, previous_selection
from tow.topic_timers import parse_interval
from tow.web.edit_base import Merge, Row, Spec, digest
from tow.web.text import t


def _same_path(left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str) and left.strip() and right.strip():
        return paths_equal(left, right)
    return bool(left == right)


TOPIC = Spec(
    "topic",
    {
        "url": ("url",),
        "title": ("title",),
        "save_path": ("save_path",),
        "client": ("client_id",),
        "selection": ("selection",),
        "tracking": ("tracking_mode",),
        "timer": ("check_interval_min",),
    },
    same={"save_path": _same_path},
)

# The form's fields of each group: a refused save's draft keeps those of the groups it changed.
FORM_FIELDS = {
    "url": ("url",),
    "title": ("title",),
    "save_path": ("save_path",),
    "client": ("client_id",),
    "selection": ("selection_mode", "selection_value", "content_token", "selection_indices"),
    "tracking": ("tracking_mode",),
    "timer": ("check_interval_min",),
}


def _selection_key(mode: str, value: str) -> str:
    """A non-exact selection as one comparable value (its rule normalized)."""
    try:
        policy = normalize_policy(mode, value)
    except TowError, ValueError:
        return f"{mode.strip().lower()}:{value.strip()}"
    return f"{policy['mode']}:{policy['value']}"


def _stored_key(topic: Mapping[str, Any]) -> str:
    selection = previous_selection(dict(topic))
    mode = str(selection.get("mode") or "all")
    if mode == "exact":
        return "exact:" + digest([selection.get("files"), selection.get("source_hash")])
    return _selection_key(mode, str(selection.get("value") or ""))


def _interval(topic: Mapping[str, Any]) -> int | None:
    value = topic.get("check_interval_min")
    return value if type(value) is int else None


def current(topic: Mapping[str, Any], default_client: str) -> dict[str, Any]:
    """The form's values of ``topic`` as saved now (and as its panel shows them)."""
    return {
        "url": str(topic.get("url") or ""),
        "title": shown_title(dict(topic)).strip(),
        "save_path": str(topic.get("save_path") or ""),
        "client_id": str(topic.get("client_id") or default_client),
        "selection": _stored_key(topic),
        "tracking_mode": "once" if topic.get("tracking_mode") == "once" else "watch",
        "check_interval_min": _interval(topic),
    }


def submitted(form: TopicForm, base: Mapping[str, Any], state: dict[str, Any], default_client: str) -> dict[str, Any]:
    """What the save asks for, as the form's values (a field the form did not send is absent)."""
    out: dict[str, Any] = {
        "title": form.title.strip(),
        "save_path": resolve_save_path(form.save_path, state),
        "client_id": form.client_id.strip() or default_client,
        "tracking_mode": "once" if form.tracking_mode == "once" else "watch",
    }
    if form.url.strip():
        out["url"] = canon_watch_url(form.url.strip())
    if form.selection_mode == "exact":
        if form.content_token or form.selection_indices:
            out["selection"] = "exact:new:" + digest([form.content_token, form.selection_indices])
        else:  # the exact list the panel showed, kept
            out["selection"] = base["selection"] if str(base["selection"]).startswith("exact:") else "exact:?"
    else:
        out["selection"] = _selection_key(form.selection_mode, form.selection_value)
    if form.check_interval_min is not None:
        try:
            out["check_interval_min"] = parse_interval(form.check_interval_min)
        except TowError:
            out["check_interval_min"] = f"invalid:{form.check_interval_min.strip()[:32]}"
    return out


def effective_form(form: TopicForm, topic: Mapping[str, Any], merged: Merge) -> TopicForm:
    """The form ``topic_actions`` applies: the owner's values for the groups they changed, the
    topic's own for every other (so a save puts back nothing another device changed)."""
    changed = merged.changed
    selection = previous_selection(dict(topic))
    keep: dict[str, Any] = {}
    if "url" not in changed:
        keep["url"] = str(topic.get("url") or "")
    if "title" not in changed:
        keep["title"] = shown_title(dict(topic))
    if "save_path" not in changed:
        keep["save_path"] = str(topic.get("save_path") or "")
    if "client" not in changed:
        keep["client_id"] = str(topic.get("client_id") or "")
    if "selection" not in changed:
        keep.update(
            selection_mode=str(selection.get("mode") or "all"),
            selection_value=str(selection.get("value") or ""),
            content_token="",
            selection_indices="",
        )
    if "tracking" not in changed:
        keep["tracking_mode"] = "once" if topic.get("tracking_mode") == "once" else "watch"
    if "timer" not in changed:
        keep["check_interval_min"] = None  # keeps the topic's timer
    return replace(form, **keep)


def draft(form: TopicForm, groups: frozenset[str] | set[str]) -> dict[str, str]:
    """The fields of the groups the owner changed, as typed: a conflict brings them back."""
    values = form.draft()
    if form.check_interval_min is None:
        values.pop("check_interval_min", None)
    return {name: values[name] for group in groups for name in FORM_FIELDS[group] if name in values}


_LABELS = {
    "url": "home.add.url",
    "title": "home.add.name",
    "save_path": "home.add.folder",
    "client": "home.add.client",
    "selection": "home.add.what",
    "tracking": "home.add.follow",
    "timer": "timer.field",
}
_MODES = {"all": "home.add.mode_all", "episodes": "home.add.mode_episodes", "files": "home.add.mode_files"}


def _client_text(cfg: dict[str, Any], client_id: str) -> str:
    try:
        return client_name(client_configuration(cfg, client_id or None))
    except RuntimeError, ValueError, KeyError, TypeError:
        return client_id


def _selection_text(mode: str, value: str) -> str:
    if mode == "exact":
        return t("content.manual")
    words = t(_MODES.get(mode, "home.add.mode_all"))
    return f"{words}: {value.strip()}" if value.strip() and mode != "all" else words


def _text(group: str, values: Mapping[str, Any], cfg: dict[str, Any]) -> str:
    if group == "client":
        return _client_text(cfg, str(values.get("client_id") or ""))
    if group == "selection":
        return _selection_text(str(values.get("selection_mode") or "all"), str(values.get("selection_value") or ""))
    if group == "tracking":
        return t("web.edit.value.watch" if values.get("tracking_mode") != "once" else "web.edit.value.once")
    if group == "timer":
        raw = str(values.get("check_interval_min") or "").strip()
        return t("web.edit.value.minutes", count=int(raw)) if raw.isdigit() else t("web.edit.value.global_timer")
    text = str(values.get(FORM_FIELDS[group][0]) or "").strip()
    return text or t("web.edit.value.empty")


def _shown(topic: Mapping[str, Any]) -> dict[str, Any]:
    """The topic's values as its form's fields."""
    selection = previous_selection(dict(topic))
    interval = _interval(topic)
    return {
        "url": str(topic.get("url") or ""),
        "title": shown_title(dict(topic)),
        "save_path": str(topic.get("save_path") or ""),
        "client_id": str(topic.get("client_id") or ""),
        "selection_mode": str(selection.get("mode") or "all"),
        "selection_value": str(selection.get("value") or ""),
        "tracking_mode": "once" if topic.get("tracking_mode") == "once" else "watch",
        "check_interval_min": "" if interval is None else str(interval),
    }


def rows(groups: tuple[str, ...], topic: Mapping[str, Any], form: TopicForm, cfg: dict[str, Any]) -> list[Row]:
    """What a conflict shows for each group: saved now, and the owner's value."""
    now = _shown(topic)
    if not now["client_id"]:
        with suppress(RuntimeError, ValueError, KeyError):
            now["client_id"] = str(client_configuration(cfg, None)["id"])
    mine = form.draft()
    return [Row(group, t(_LABELS[group]), _text(group, now, cfg), _text(group, mine, cfg)) for group in groups]
