"""What the header and the settings page show about messengers (no network)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tow.errors import render_stored
from tow.i18n import t
from tow.log import scrub_text
from tow.notifiers.base import text, ui_language
from tow.notifiers.registry import connected, kinds, settings_of, targets, title
from tow.scrub import PATH_PARAMS, scrub_display

_NESTED_MAX = 32  # messages inside messages: as deep as tow.errors renders them


def _shown_values(value: Any, *, local: bool, name: str = "", depth: int = 0) -> Any:
    """A stored message's values as a reader may see them, messages among them too: never a
    link's credentials (``tow.log.scrub_text``: a link keeps its scheme, host and port); for a
    network device (``local`` false) also no folder or file (a value named as one is "…", the
    others scrubbed as Recent messages are)."""
    if depth > _NESTED_MAX:
        return "…"
    if isinstance(value, str):
        if not local and name in PATH_PARAMS:
            return "…"
        shown = scrub_text(value)
        return shown if local else scrub_display(shown, limit=8192)
    if isinstance(value, Mapping):
        return {key: _shown_values(item, local=local, name=str(key), depth=depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_shown_values(item, local=local, name=name, depth=depth + 1) for item in value]
    return value


def _status_error(row: dict[str, Any], *, local: bool) -> str:
    """Why the last delivery failed, in the reader's language (the stored text for an old status).
    Its values are scrubbed (``_shown_values``) before the sentence is formatted or cut, then the
    sentence itself. What is stored stays as it is."""
    shown = render_stored(
        row.get("error_code"),
        _shown_values(row.get("error_params"), local=local),
        _shown_values(str(row.get("error") or ""), local=local),
        ui_language(),
    )
    return str(_shown_values(shown, local=local))


def _kind_status(secrets: dict[str, Any], state: dict[str, Any], kind: str, *, local: bool = True) -> dict[str, Any]:
    """One status per messenger from its recipients' statuses (any failing one -> not ok)."""
    status = state.get("notify_status") or {}
    outbox = state.get("notify_outbox") or {}
    keys = [key for key, target_kind, _m, _s in targets(secrets) if target_kind == kind] or [kind]
    rows = [status.get(key) or {} for key in keys]
    failing = [row for row in rows if row.get("ok") is False]
    seen = [row for row in rows if "ok" in row]
    queued = 0
    dropped = 0
    for key in keys:
        box = outbox.get(key)
        if isinstance(box, dict):
            queued += len(box.get("items") or [])
        elif isinstance(box, list):
            queued += len(box)
        dropped += int((state.get("notify_dropped") or {}).get(key) or 0)
    return {
        "ok": (not failing) if seen else None,
        "error": _status_error(failing[0], local=local) if failing else "",
        "at": max((int(row.get("at") or 0) for row in rows), default=0) or None,
        "queued": queued,
        "dropped": dropped,
    }


def health(secrets: dict[str, Any], state: dict[str, Any]) -> bool | None:
    """None: nothing connected; True: every channel's last delivery worked (or none yet)."""
    channels = connected(secrets)
    if not channels:
        return None
    return all(_kind_status(secrets, state, kind)["ok"] is not False for kind, _, _ in channels)


def summary(secrets: dict[str, Any], state: dict[str, Any]) -> str:
    lang = ui_language()
    parts = []
    for kind, module, _ in connected(secrets):
        ok = _kind_status(secrets, state, kind)["ok"] is not False
        parts.append(f"{title(module, lang).split(' ')[0]} {'✓' if ok else '✗'}")
    if not parts:
        return t("notifier.common.summary_none", lang)
    return t("notifier.common.summary", lang, channels=", ".join(parts))


def cards(secrets: dict[str, Any], state: dict[str, Any], *, local: bool = True) -> list[dict[str, Any]]:
    """Everything the settings page shows for each messenger, in the owner's language; ``local``:
    for this PC (every word of an error but a link's credentials), else for a network device
    (scrubbed as Recent messages are)."""
    lang = ui_language()
    view = []
    for kind, module in kinds().items():
        settings = settings_of(secrets, kind)
        suggest = getattr(module, "suggestion", None)
        fields = []
        for field in module.FIELDS:
            value = (settings or {}).get(field.name)
            fields.append(
                {
                    "name": field.name,
                    "label": text(field.label, lang),
                    "kind": field.kind,
                    "placeholder": text(field.placeholder, lang),
                    "required": field.required,
                    "width": field.width,
                    "saved": bool(value),
                    "value": ""
                    if field.kind == "secret"
                    else (", ".join(value) if isinstance(value, list) else str(value or field.default or "")),
                    "suggestion": suggest(field.name) if (callable(suggest) and not settings) else "",
                }
            )
        status = (
            _kind_status(secrets, state, kind, local=local)
            if settings is not None
            else {"ok": None, "error": "", "at": None, "queued": 0, "dropped": 0}
        )
        view.append(
            {
                "kind": kind,
                "title": title(module, lang),
                # The product's own name, as the section's pill lists the connected ones.
                "name": title(module, lang).split(" ")[0],
                "steps": [text(step, lang) for step in module.STEPS],
                "note": text(str(getattr(module, "NOTE", "")), lang),
                "fields": fields,
                "connected": settings is not None,
                **status,
            }
        )
    return view
