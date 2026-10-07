"""Network errors in the owner's words, at display time.

A failed request leaves texts like ``[WinError 10061] No connection could be made…`` or
``ConnectionError(MaxRetryError("HTTPConnectionPool(host=…): Max retries exceeded…"))`` in the
state and the log. They stay there as they are (the evidence); pages show the words of the
catalog instead - refused, no answer in time, address not found, certificate problem, connection
reset - and keep the raw text under a "Details" toggle.
"""

from __future__ import annotations

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


def humanize(text: str, lang: str | None = None) -> str:
    """``text`` with its technical part said in words ("torrent client: connection refused");
    a text without one comes back unchanged, but for a known reason in brackets."""
    value = str(text or "")
    starts = [at for marker in _MARKERS if (at := value.find(marker)) != -1]
    if not starts:
        return _BRACKETED.sub(lambda match: _bracketed_words(match, lang), value)
    at = min(starts)
    head = value[:at]
    for english, name in _LIBRARY_HEADS:
        head = head.replace(english, name)
    parts = [part for part in head.rstrip(" :(—–-,;.").split(": ") if part]
    head = ": ".join(part for index, part in enumerate(parts) if not index or part != parts[index - 1])
    kind = reason(value[at:])
    phrase = t(_KEYS[kind] if kind else "doctor.reason.network", lang)
    return f"{head}: {phrase}" if head else phrase
