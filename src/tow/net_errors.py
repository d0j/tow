"""Network errors in the owner's words, at display time.

A failed request leaves texts like ``[WinError 10061] No connection could be made…`` or
``ConnectionError(MaxRetryError("HTTPConnectionPool(host=…): Max retries exceeded…"))`` in the
state and the log. They stay there as they are (the evidence); pages show the words of the
catalog instead - refused, no answer in time, address not found, certificate problem, connection
reset - and keep the raw text under a "Details" toggle.
"""

from __future__ import annotations

import errno
import re

from tow.i18n import t

# Where the technical part of a message starts: an OS error, a requests/urllib3 repr.
_MARKERS = (
    "[WinError",
    "[Errno",
    "HTTPConnectionPool(",
    "HTTPSConnectionPool(",
    "ConnectionError(",
    "MaxRetryError(",
    "NewConnectionError(",
    "NameResolutionError(",
    "ConnectTimeoutError(",
    "ReadTimeoutError(",
    "ConnectTimeout(",
    "ReadTimeout(",
    "SSLError(",
    "SSLCertVerificationError(",
    "ProxyError(",
    "ProtocolError(",
    "RemoteDisconnected(",
    "ConnectionRefusedError(",
    "ConnectionResetError(",
    "ConnectionAbortedError(",
    "<urllib3.",
    "socket.gaierror",
)
# The first group whose phrase is in the lower-cased technical part names it.
_REASONS = (
    (("10061", "errno 111", "connection refused", "actively refused", "connectionrefusederror"), "refused"),
    (
        (
            "getaddrinfo",
            "11001",
            "nameresolutionerror",
            "name or service not known",
            "nodename nor servname",
            "temporary failure in name resolution",
            "failed to resolve",
            "no address associated",
        ),
        "no_address",
    ),
    (("certificate", "sslerror", "ssl:", "[ssl", "tlsv1"), "certificate"),
    (("timed out", "timeout", "10060"), "timeout"),
    (
        ("10054", "10053", "connection reset", "connectionreseterror", "remotedisconnected", "connection aborted"),
        "reset",
    ),
)


_KEYS = {
    "refused": "doctor.reason.refused",
    "no_address": "doctor.reason.no_address",
    "certificate": "doctor.reason.certificate",
    "timeout": "doctor.reason.timeout",
    "reset": "doctor.reason.reset",
}


def reason(raw: str) -> str | None:
    """The kind of a network failure (refused, no_address, certificate, timeout, reset), or None."""
    low = str(raw or "").lower()
    for phrases, kind in _REASONS:
        if any(phrase in low for phrase in phrases):
            return kind
    return None


# English sentences a client library puts before its technical part: said by the client's name
# (a Russian page showed "Failed to connect to qBittorrent. Connection Error: …").
_LIBRARY_HEADS = (
    ("Failed to connect to qBittorrent. Connection Error", "qBittorrent"),
    ("Failed to connect to qBittorrent", "qBittorrent"),
)
# A library's own words in brackets inside a catalog text: "no connection (timed out)".
_BRACKETED = re.compile(r"\(([\x20-\x7e]{1,160})\)")


def _bracketed_words(match: re.Match[str], lang: str | None) -> str:
    kind = reason(match.group(1))
    return f"({t(_KEYS[kind], lang)})" if kind else match.group(0)


# OS errors of the network (a socket's), by their errno names on this system; Winsock's
# (10000-11999) and the resolver's (negative) are all network ones.
_NETWORK_ERRNO_NAMES = frozenset(
    {
        "EADDRINUSE", "EADDRNOTAVAIL", "EAFNOSUPPORT", "EALREADY", "ECONNABORTED", "ECONNREFUSED",
        "ECONNRESET", "EHOSTDOWN", "EHOSTUNREACH", "EINPROGRESS", "EISCONN", "ENETDOWN", "ENETRESET",
        "ENETUNREACH", "ENOBUFS", "ENOTCONN", "EPIPE", "EPROTO", "EPROTONOSUPPORT", "ESHUTDOWN",
        "ETIMEDOUT", "ETOOMANYREFS",
    }
)  # fmt: skip
_NETWORK_ERRNOS = frozenset(code for code, name in errno.errorcode.items() if name in _NETWORK_ERRNO_NAMES)
_OS_ERROR = re.compile(r"\[(Errno|WinError) (-?\d+)\]")


def _other_os_error(technical: str) -> bool:
    """The technical part is an OS error that is not the network's ("[Errno 28] No space left on
    device", "[WinError 32] The process cannot access the file…"): its own words say what it is,
    and "network error" would not."""
    match = _OS_ERROR.match(technical)
    if not match:
        return False
    code = int(match.group(2))
    if match.group(1) == "WinError":  # a Windows error code, not an errno: only Winsock's are the network's
        return not 10000 <= code < 12000
    return not (code < 0 or 10000 <= code < 12000 or code in _NETWORK_ERRNOS)


def humanize(text: str, lang: str | None = None) -> str:
    """``text`` with its technical part said in words ("torrent client: connection refused");
    a text without one comes back unchanged, but for a known reason in brackets."""
    value = str(text or "")
    starts = [at for marker in _MARKERS if (at := value.find(marker)) != -1]
    if not starts:
        return _BRACKETED.sub(lambda match: _bracketed_words(match, lang), value)
    at = min(starts)
    if reason(value[at:]) is None and _other_os_error(value[at:]):
        return value
    head = value[:at]
    for english, name in _LIBRARY_HEADS:
        head = head.replace(english, name)
    parts = [part for part in head.rstrip(" :(—–-,;.").split(": ") if part]
    head = ": ".join(part for index, part in enumerate(parts) if not index or part != parts[index - 1])
    kind = reason(value[at:])
    phrase = t(_KEYS[kind] if kind else "doctor.reason.network", lang)
    return f"{head}: {phrase}" if head else phrase
