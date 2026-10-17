"""Words any signed-in device may read: one policy for Recent messages (tow.messages_feed) and
every page or answer a network device gets (tow.web reaches it through tow.web.services).
A link keeps only its host, a folder or file path goes, control characters go; a message's own
values that name a folder or a file are "…" before its words are formatted.

Credentials go for every device, the PC running TOW included (``scrub_credentials``): a link's
sign-in (``user:password@``), the values of its secret query names (``uk``, ``pk``, ``passkey``,
``token``...), a tracker's announce address (its passkey may be in the path) and a magnet's
trackers, a header's or a ``name=value`` secret. The PC keeps every other word: folders, and
links otherwise whole."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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


# Credentials, for every device. A query name that holds a secret: its value is "***".
_SECRET_QUERY = re.compile(
    r"uk|pk|passkey|pass_key|torrent_pass|authkey|auth|token|access_token|api[_-]?key|access[_-]?key|key|secret"
    r"|client[_-]?secret|password|passwd|pass|pwd|sid|session|sig|signature|cf_clearance",
    re.IGNORECASE,
)
_ANNOUNCE = re.compile(r"/(?:announce|scrape)(?:\.php)?\b", re.IGNORECASE)
_MAGNET_RE = re.compile(r"magnet:\?[^\s'\"<>]*", re.IGNORECASE)
_MAGNET_XT_RE = re.compile(r"(?:^|&)xt=([^&]*)", re.IGNORECASE)
_SECRET_HEADER_RE = re.compile(r"\b(authorization|cookie|set-cookie)\s*[:=][^\r\n]*", re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(
    r"\b(password|passphrase|passkey|token|secret|cf_clearance|api[_-]?key|access[_-]?key|client[_-]?secret|uk|pk)"
    r"\s*([:=])\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|(?!\*\*\*)[^\s,;&)]+)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bbearer\s+[^\s,;]+", re.IGNORECASE)


def _magnet(match: re.Match[str]) -> str:
    """A magnet link keeps only its info-hash (xt): trackers (tr=), peers and names go."""
    raw = match.group(0)
    stripped = raw.rstrip(".,;:)]}")
    xt = _MAGNET_XT_RE.search(stripped[len("magnet:?") :])
    return (f"magnet:?xt={xt.group(1)}" if xt else "magnet:?") + raw[len(stripped) :]


def _query_without_secrets(query: str) -> str:
    kept = []
    for item in query.split("&"):
        name, sep, value = item.partition("=")
        kept.append(f"{name}=***" if sep and value and _SECRET_QUERY.fullmatch(name) else item)
    return "&".join(item for item in kept if item)


def _link_without_credentials(match: re.Match[str]) -> str:
    raw = match.group(0)
    stripped = raw.rstrip(".,;:)]}")
    tail = raw[len(stripped) :]
    try:
        parts = urlsplit(stripped)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "…" + tail
    if not host:
        return "…" + tail
    netloc = (f"[{host}]" if ":" in host else host) + port
    if parts.scheme.lower() in {"udp", "ws", "wss"} or _ANNOUNCE.search(parts.path):
        return f"{parts.scheme}://{netloc}" + tail  # a tracker's address: its passkey may be anywhere
    return urlunsplit((parts.scheme, netloc, parts.path, _query_without_secrets(parts.query), "")) + tail


def scrub_credentials(text: str) -> str:
    """``text`` without credentials, for every device (see the module); every other word stays."""
    value = _MAGNET_RE.sub(_magnet, str(text or ""))
    value = _URL_RE.sub(_link_without_credentials, value)
    value = _SECRET_HEADER_RE.sub(lambda match: f"{match.group(1)}: ***", value)
    value = _SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}***", value)
    return _BEARER_RE.sub("Bearer ***", value)


def credentials_out(params: Any, depth: int = 0) -> Any:
    """Stored values (a message's, and those of the messages among them) without credentials."""
    if isinstance(params, str):
        return scrub_credentials(params)
    if depth >= _NESTED_MAX:
        return params if isinstance(params, int | float | bool) or params is None else "…"
    if isinstance(params, list | tuple):
        return [credentials_out(item, depth + 1) for item in params]
    if isinstance(params, Mapping):
        return {name: credentials_out(value, depth + 1) for name, value in params.items()}
    return params


def scrub_display(text: str, limit: int = 600) -> str:
    """Words a message may show on any device, applied after formatting: no credentials, a link
    keeps only its host, a folder or file path goes, control characters go."""

    def host(match: re.Match[str]) -> str:
        try:
            return urlsplit(match.group(0)).hostname or "…"
        except ValueError:
            return "…"

    value = _URL_RE.sub(host, scrub_credentials(str(text or "")))
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
