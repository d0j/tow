"""Every write the app serves refuses a request from another site (CSRF), whoever sends it.

The rule lives in one place (``tow.web.middleware``): a write needs an ``Origin`` of the very
address it was sent to. This test does not trust that: it takes every route the app has, the
ones added later included (the torrent clients' section too), and sends each write from another
origin, without one and with a near miss, from this computer and from a signed-in device on the
network. Every other write method on each path, the static files and a path no route has are
refused the same way: the check comes before any routing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from helpers import open_network
from test_routes import ROUTES

from tow.auth import SESSION_COOKIE, issue_session, lan_password_record, lan_password_session_key
from tow.i18n import t
from tow.store import save_secrets
from tow.web import app, routes_clients

WRITES = ("POST", "PUT", "PATCH", "DELETE")
SAMPLE = {
    "tid": "1234567",
    "name": "fakesite",
    "point_id": "20260101-000000-abcdef",
    "kind": "ntfy",
    "job": "abcdef1234567890",
}
LOCAL = "http://127.0.0.1:8787"
LAN = "http://192.168.1.2:8787"
FOREIGN = [
    None,  # no Origin at all
    "null",  # a sandboxed frame, a file: page, a redirect across sites
    "http://evil.example",
    "http://evil.example:8787",
    "http://127.0.0.1:8788",  # another program on this computer
    "https://127.0.0.1:8787",  # another scheme
    "http://localhost:8787",  # another origin, though the same computer
    "http://192.168.1.2:8787",  # the network address is another origin than loopback
    "http://127.0.0.1:8787/",  # an Origin is never a URL with a path
    "http://user@127.0.0.1:8787",
]
# What no route declares: refused before routing, so two origins are proof enough.
UNROUTED_ORIGINS = [None, "http://evil.example"]
UNROUTED_PATHS = ["/static/app.js", "/no-such-page"]


def _concrete(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: SAMPLE[m.group(1)], path)


def _declared_writes() -> list[tuple[str, str]]:
    """(method, concrete path) of every write route the app serves."""
    declared = {
        (method, route.path)
        for route in iter_route_contexts(app.routes)
        for method in getattr(route, "methods", None) or ()
        if method in WRITES
    }
    # The enumeration really sees the routers' routes: every recorded write, and nothing else.
    assert declared == {(method, path) for method, path in ROUTES if method in WRITES}
    # The torrent clients' section is among them (a router of its own since the refactor).
    clients = {route.path for route in routes_clients.router.routes}
    assert clients
    assert clients <= {path for _method, path in declared}
    return [(method, _concrete(path)) for method, path in sorted(declared)]


def _unrouted_writes() -> list[tuple[str, str]]:
    """Every write method no route declares on a served path, and writes to paths no route has."""
    served = {
        (method, route.path)
        for route in iter_route_contexts(app.routes)
        for method in getattr(route, "methods", None) or ()
    }
    paths = sorted({path for _method, path in served})
    return [(method, _concrete(path)) for path in paths for method in WRITES if (method, path) not in served] + [
        (method, path) for path in UNROUTED_PATHS for method in WRITES
    ]


def _files(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def _assert_refused(client: TestClient, origins: list[str | None], base: str) -> None:
    forbidden = t("web.refused.forbidden", "ru")
    plan = [(write, origins) for write in _declared_writes()]
    plan += [(write, [o for o in UNROUTED_ORIGINS if o in origins]) for write in _unrouted_writes()]
    for (method, path), tried in plan:
        for origin in tried:
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            if origin is not None:
                headers["Origin"] = origin
            answer = client.request(method, base + path, content=b"a=1", headers=headers, follow_redirects=False)
            assert (answer.status_code, answer.text) == (403, forbidden), (method, path, origin)
            assert SESSION_COOKIE not in answer.cookies


def test_every_write_refuses_another_origin_from_this_computer(tmp_path):
    client = TestClient(app, client=("127.0.0.1", 50000))
    client.get(LOCAL + "/")  # whatever a first page writes is written before the snapshot
    before = _files(tmp_path)

    _assert_refused(client, FOREIGN, LOCAL)

    assert _files(tmp_path) == before  # no route ran: nothing was written


def test_every_write_refuses_another_origin_from_a_signed_in_device(tmp_path):
    open_network()
    record = lan_password_record("a-long-password")
    save_secrets({"lan_auth": record})
    cookie = issue_session(lan_password_session_key(record))
    client = TestClient(app, client=("192.168.1.7", 50000), cookies={SESSION_COOKIE: cookie})
    assert client.get(LAN + "/settings", follow_redirects=False).status_code == 200  # really signed in
    before = _files(tmp_path)
    origins = [origin for origin in FOREIGN if origin != LAN] + [LOCAL]

    _assert_refused(client, origins, LAN)

    assert _files(tmp_path) == before
    assert client.get(LAN + "/settings", follow_redirects=False).status_code == 200  # and still signed in


@pytest.mark.parametrize(("peer", "base"), [("127.0.0.1", LOCAL), ("192.168.1.7", LAN)])
def test_the_same_write_from_tows_own_page_passes_the_check(peer, base):
    """The control: the refusal above is the origin's, not something else about the request."""
    if peer != "127.0.0.1":
        open_network()
        record = lan_password_record("a-long-password")
        save_secrets({"lan_auth": record})
        cookies = {SESSION_COOKIE: issue_session(lan_password_session_key(record))}
    else:
        cookies = {}
    client = TestClient(app, client=(peer, 50000), cookies=cookies)

    answer = client.post(
        base + "/settings/theme", data={"theme": "dark"}, headers={"Origin": base}, follow_redirects=False
    )

    assert answer.status_code == 303
