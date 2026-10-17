"""Words any signed-in device may read: one policy for Recent messages (tow.messages_feed) and
every page or answer a network device gets (tow.web reaches it through tow.web.services).
A link keeps only its host, a folder or file path goes, control characters go; a message's own
values that name a folder or a file are "…" before its words are formatted. The PC running TOW
keeps every word for itself."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

# A folder or file path, also one with spaces in its folders ("D:\TV\Show A\e01.mkv"), a drive's
# root ("D:\"), a share ("\\nas", "//nas/media") and a folder at the root ("/media", "~/x"):
# every part up to its last separator, then the name up to a space (a POSIX name without the
# mark that ends a sentence after it). A slash between words ("and/or", "1/2", "Show / Сериал")
# is no path. Scrubbing a little more is harmless.
_POSIX_NAME = r"[^\s\\/\"'<>·—]*[^\s\\/\"'<>·—;,.:!?)\]]"
PATH_RES = (
    re.compile(r"\b[A-Za-z]:\\(?:[^\\/\n\"'<>·—:]{0,60}\\)*[^\s\\/\"'<>·—]*"),
    re.compile(r"\b[A-Za-z]:/(?:[^\\/\n\"'<>·—:]{0,60}/)*[^\s\\/\"'<>·—]*"),
    re.compile(r"\\\\(?:[^\\/\n\"'<>·—:]{0,60}\\)*[^\s\\/\"'<>·—]*"),
    re.compile(rf"(?<![\w.:/])~?/(?:(?:[^\\/\n\"'<>·—:]{{0,60}}/)+(?:{_POSIX_NAME})?|{_POSIX_NAME})"),
)
# Message values that name a folder or a file, whatever they hold.
PATH_PARAMS = frozenset(
    (
        "path",
        "folder",
        "file",
        "root",
        "where",
        "dir",
        "directory",
        "save_path",
        "log",
        "stderr",
        "snapshot",
        "checkpoint",
        "launcher",
    )
)
_MESSAGE = "$msg"  # a stored value that is itself a message (tow.errors)
_NESTED_MAX = 32  # messages inside messages: as deep as tow.errors renders them
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+", re.IGNORECASE)


def scrub_display(text: str, limit: int = 600) -> str:
    """Words a message may show on any device, applied after formatting: a link keeps only its
    host, a folder or file path goes, control characters go."""

    def host(match: re.Match[str]) -> str:
        try:
            return urlsplit(match.group(0)).hostname or "…"
        except ValueError:
            return "…"

    value = _URL_RE.sub(host, str(text or ""))
    for pattern in PATH_RES:
        value = pattern.sub("…", value)
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    return " ".join(value.split())[:limit]


def without_paths(params: Any, depth: int = 0) -> Any:
    """A stored message's values (and those of the messages among them) without a folder or a
    file: a value named as one (``PATH_PARAMS``) or holding one is "…", before the words are
    formatted - the sentence stays whole, with no path in it for any device."""
    if not isinstance(params, Mapping):
        return params
    kept: dict[str, Any] = {}
    for name, value in params.items():
        inner = value.get(_MESSAGE) if isinstance(value, Mapping) else None
        if isinstance(inner, Mapping):
            if depth >= _NESTED_MAX:
                kept[name] = "…"
                continue
            kept[name] = {_MESSAGE: {**inner, "params": without_paths(inner.get("params"), depth + 1)}}
        elif isinstance(value, str) and (name in PATH_PARAMS or any(path.search(value) for path in PATH_RES)):
            kept[name] = "…"
        else:
            kept[name] = value
    return kept
