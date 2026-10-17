"""Sites, Settings and History, live: what of them another device's change redraws.

A page is drawn once by its own route; what may change while it is open is marked in its template
as *fragments* - a Sites row, a Settings section, the empty list - between two comments::

    <!--frags:/sites-->                       the page's list (its revision is written into it)
    <!--frag:site-row-rutor--> ... <!--/frag:site-row-rutor-->

A fragment's digest is the SHA-256 of what it draws, without what only the address chose (a
section opened by ``?open=``, a field focused by ``?fix=``, a refused field) and without what
changes by itself (``data-sync-volatile``: the free disk space, a random topic name offered). The page area's
revision (``tow.web.sync``) is the digest of the fragments in their order: a routine check that
changes nothing a page draws changes no revision, and a page opens knowing its own (``annotate``),
so it asks for nothing until something it shows changes. ``GET /page/fragments`` then hands the
fragments of exactly that revision (``routes_sync``); the page replaces only those whose digest
differs, never one the owner is working in (``static/pages.js``).

History is a list of events, newest first, as its filters (``group``, ``q``) choose them: each line
is keyed by its event's id, so the page merges the newest window into what it shows - a line
once, in the server's order, at most ``HISTORY_LIMIT`` of them.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

FRAGMENT_PAGES = ("/sites", "/settings")
# The newest events History shows; the page says so only when the list stops there.
HISTORY_LIMIT = 300
FRAG = re.compile(r"<!--frag:([A-Za-z0-9_-]{1,160})-->(.*?)<!--/frag:\1-->", re.DOTALL)
LIST = re.compile(r"<!--frags:(/[a-z]+)-->")
HASH_HEX = 32
MAX_QUERY_CHARS = 200
_TAG = re.compile(r"<([a-zA-Z][a-zA-Z0-9-]*)((?:\s+[^\s=>/]+(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?)*)\s*(/?)>")
_ATTR = re.compile(r"\s+([^\s=>/]+)(?:\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+))?")
_VOLATILE = re.compile(r"<span\b[^>]*\bdata-sync-volatile\b[^>]*>[^<]*</span>")
# What only the address chose: a section or card opened for a link, a field focused or marked.
_ADDRESS_ONLY = {"autofocus", "aria-invalid"}


def _drop_attributes(match: re.Match[str]) -> str:
    tag, attrs, slash = match.group(1), match.group(2), match.group(3)
    names = _ADDRESS_ONLY | ({"open"} if tag.lower() == "details" else set())
    if "data-sync-volatile" in attrs:
        names = names | {"value"}  # a ready value made up anew each time (a random topic name)
    if not any(name in attrs for name in names):
        return match.group(0)
    kept = "".join(part.group(0) for part in _ATTR.finditer(attrs) if part.group(1).lower() not in names)
    return f"<{tag}{kept}{' /' if slash else ''}>"


def normalized(html: str) -> str:
    """What a fragment draws, without what the address alone chose or what changes by itself."""
    return _TAG.sub(_drop_attributes, _VOLATILE.sub("", html))


def digest(html: str) -> str:
    return hashlib.sha256(normalized(html).encode()).hexdigest()[:HASH_HEX]


def fragments(html: str) -> dict[str, tuple[str, str]]:
    """The page's fragments in their order: ``{id: (digest, html)}``."""
    found: dict[str, tuple[str, str]] = {}
    for match in FRAG.finditer(html):
        found.setdefault(match.group(1), (digest(match.group(2)), match.group(2)))
    return found


def projection(found: Mapping[str, tuple[str, str]]) -> dict[str, Any]:
    """What the page area's revision is the digest of."""
    return {"order": list(found), "hashes": {fid: item[0] for fid, item in found.items()}}


def annotate(html: str, rev: str, found: Mapping[str, tuple[str, str]]) -> str:
    """The page with its revision in its list marker and each fragment's digest in its opening
    comment: the page knows what it shows."""

    def opening(match: re.Match[str]) -> str:
        fid = match.group(1)
        item = found.get(fid)
        return f"<!--frag:{fid}:{item[0]}-->{match.group(2)}<!--/frag:{fid}-->" if item else match.group(0)

    return LIST.sub(lambda match: f"<!--frags:{match.group(1)}:{rev}-->", FRAG.sub(opening, html), count=1)


def page_request(request: Request, path: str) -> Request:
    """``request`` as a plain open of ``path``: the same viewer (address, cookies, language), no
    query - a page drawn for the poll shows what any open of it shows."""
    scope = {**request.scope, "path": path, "raw_path": path.encode(), "query_string": b""}
    return Request(scope)


def render(request: Request, page: str) -> str:
    """The page as its route draws it for ``request`` (read through the request's readers)."""
    if page == "/sites":
        from tow.web.routes_sites import sites_view

        response = sites_view(page_request(request, page))
    elif page == "/settings":
        from tow.web.routes_settings import settings_view

        response = settings_view(page_request(request, page), live=True)
    else:
        raise ValueError(page)
    return bytes(response.body).decode("utf-8")


def annotated_response(request: Request, page: str, response: Response) -> Response:
    """The page a route drew, with its revision and its fragments' digests (``annotate``)."""
    from tow.web.sync import annotated_page

    html = annotated_page(request, page, bytes(response.body).decode("utf-8"))
    return HTMLResponse(html, status_code=response.status_code)


# --------------------------------------------------------------------------- History


def filters(group: str, q: str) -> dict[str, str]:
    """History's filters as the page applies them: a known group, a search of bounded length.
    Raises ValueError for anything else."""
    from tow.log import HISTORY_GROUPS

    if group and group not in HISTORY_GROUPS:
        raise ValueError("unknown group")
    if len(q) > MAX_QUERY_CHARS:
        raise ValueError("search too long")
    return {"group": group, "q": q}


def event_key(event: Mapping[str, Any]) -> str:
    """A History line's identity: its event's id; an event of an older TOW without one, the digest
    of the record itself (the same line every time it is read)."""
    found = str(event.get("event_id") or "")
    if re.fullmatch(r"[0-9a-f]{8,64}", found):
        return found
    canonical = json.dumps(event, sort_keys=True, ensure_ascii=False, default=str)
    return "h" + hashlib.sha256(canonical.encode()).hexdigest()[: HASH_HEX - 1]


def history_rows(
    events: list[dict[str, Any]], state: Mapping[str, Any], cfg: Mapping[str, Any], *, local: bool
) -> list[dict[str, Any]]:
    """History's lines for ``events`` (newest first): their words (for this PC, ``local``, or a
    network device: ``views.log_line``), the newest line of each kind's address
    (``latest-<kind>``: "Needs attention" links there) and their key."""
    from tow.log import format_event, index_event_titles, index_site_titles
    from tow.web.views import log_line

    title_index = index_event_titles(state.get("topics") or [])
    site_titles = index_site_titles(cfg)
    rows = []
    seen: set[str] = set()
    for event in events:
        row = log_line(format_event(event, title_index=title_index, site_titles=site_titles), local=local)
        kind = str(event.get("kind") or "")
        if kind and kind not in seen:
            seen.add(kind)
            row["anchor"] = f"latest-{kind}"
        row["key"] = event_key(event)
        rows.append(row)
    return rows


def history_entries(request: Request, rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Each line drawn as the page draws it, with its key and digest."""
    from tow.web.templating import TEMPLATES

    template = TEMPLATES.env.get_template("_history_line.html")
    out = []
    for row in rows:
        html = template.render(e=row, request=request).strip()
        out.append({"id": row["key"], "hash": digest(html), "html": html})
    return out
