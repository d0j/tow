"""Sites, Settings and History follow another device by themselves (tow.web.page_sync,
routes_sync ``/page/fragments``): the page area's revision is what the page draws - a routine check
changes none of it, a change made in another browser does, and only in the fragment it is in. A
page opens knowing its own revision and digests; the fragments are those of exactly that revision,
only for who may see the page, never stored, refused as JSON. History follows its own filters and
keys its lines by their events."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import open_network

from tow.config import load_config, save_config
from tow.log import log_event
from tow.store import load_secrets, load_state, save_secrets, save_state
from tow.web import app, page_sync, services, sync

KEY = "k" * 40
LAN = ("192.168.1.7", 50000)
ORIGIN = {"Origin": "http://127.0.0.1"}
MARKS = re.compile(r"<!--frag:([A-Za-z0-9_-]+):([0-9a-f]{32})-->")
LIST = re.compile(r"<!--frags:(/[a-z]+):([0-9a-f]{32})-->")
NODE = shutil.which("node")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float = sync.SAMPLE_SEC) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    ticking = Clock()
    monkeypatch.setattr(sync, "SAMPLER", sync.Sampler(clock=ticking))
    monkeypatch.setattr(sync, "AREAS_MEMO", sync.Areas())
    with sync._BUCKETS_LOCK:
        sync._BUCKETS.clear()
    yield ticking
    with sync._BUCKETS_LOCK:
        sync._BUCKETS.clear()


def _topic(tid: str, title: str, number: int, **fields):
    return {"id": tid, "title": title, "url": f"http://rutor.info/torrent/{number}/x", "save_path": "M:/a", **fields}


def _seed() -> None:
    save_state(
        {
            "topics": [
                _topic("t1", "Show A", 101, last_ok=True, last_check="2026-10-16T10:00:00+00:00"),
                _topic("t2", "Show B", 102, last_ok=True, last_check="2026-10-16T10:00:00+00:00"),
            ],
            "health": {"qbit_ok": True, "at": "2026-10-16T10:00:00+00:00"},
        }
    )


def _routine_check(at: str) -> None:
    """What a scheduled check that found nothing new writes: the topics' and the header's times,
    and the supervisor's plan for the next one."""
    state = load_state()
    for topic in state["topics"]:
        topic["last_check"] = at
    state["health"]["at"] = at
    save_state(state)
    status = services.sync_source_paths()["status"]
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(json.dumps({"last_check_at": at, "next_at": at}), encoding="utf-8")


def _page_rev(client: TestClient, page: str, **params) -> str:
    answer = client.get("/sync.json", params={"page": page, **params})
    assert answer.status_code == 200, answer.text
    return answer.json()["rev"]["page"]


def _marks(html: str) -> tuple[str, dict[str, str]]:
    found = LIST.search(html)
    assert found is not None
    return found.group(2), dict(MARKS.findall(html))


def _lan(monkeypatch) -> TestClient:
    open_network(monkeypatch, token=KEY)
    lan = TestClient(app, client=LAN, headers=ORIGIN)
    assert lan.post("/login", data={"password": KEY}, follow_redirects=False).status_code == 303
    return lan


class _Fields(HTMLParser):
    """The fields of the forms in a piece of a page, as its markup draws them."""

    def __init__(self) -> None:
        super().__init__()
        self.fields: list[dict] = []
        self._forms = 0
        self._select: dict | None = None
        self._textarea: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = {key: value or "" for key, value in attrs}
        if tag == "form":
            self._forms += 1
            return
        if not self._forms:
            return
        common = {"tag": tag, "name": a.get("name") or a.get("id", ""), "disabled": "disabled" in a}
        if tag == "input":
            kind = a.get("type", "text")
            self.fields.append({**common, "type": kind, "value": a.get("value", ""), "checked": "checked" in a})
        elif tag == "select":
            self._select = {**common, "multiple": "multiple" in a, "options": []}
            self.fields.append(self._select)
        elif tag == "option" and self._select is not None:
            option = {"value": a.get("value", ""), "defaultSelected": "selected" in a, "disabled": "disabled" in a}
            self._select["options"].append(option)
        elif tag == "textarea":
            self._textarea = {**common, "type": "textarea", "value": "", "checked": False}
            self.fields.append(self._textarea)

    def handle_data(self, data):
        if self._textarea is not None:
            self._textarea["value"] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self._forms -= 1
        elif tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None


def _fields(html: str) -> list[dict]:
    parser = _Fields()
    parser.feed(html)
    return parser.fields


def _form(html: str, action: str) -> dict[str, str]:
    """What a form posts as drawn (its boxes ticked, its selects' marked options)."""
    found = re.search(rf'<form[^>]*action="{re.escape(action)}"[^>]*>.*?</form>', html, re.DOTALL)
    assert found is not None, action
    sent: dict[str, str] = {}
    for field in _fields(found.group(0)):
        if field["disabled"] or not field["name"] or field.get("type") in {"submit", "button"}:
            continue
        if field["tag"] == "select":
            sent[field["name"]] = next((o["value"] for o in field["options"] if o["defaultSelected"]), "")
        elif field.get("type") not in {"checkbox", "radio"} or field["checked"]:
            sent[field["name"]] = field["value"]
    return sent


def _changed(fields: list[dict], tmp_path: Path) -> list[str]:
    """The fields static/sync.js's one rule (pages.js's too) counts as changed on a page that has
    just loaded them (tests/js/drawn_fields.mjs)."""
    path = tmp_path / "fields.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    script = Path(__file__).parent / "js" / "drawn_fields.mjs"
    result = subprocess.run(
        [NODE, str(script), str(path)], capture_output=True, text=True, encoding="utf-8", timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])["changed"]


# --------------------------------------------------------------------------- revisions


@pytest.mark.parametrize("page", ["/settings", "/sites"])
def test_a_routine_check_changes_no_revision_of_sites_or_settings(page, clock):
    """Regression (1.33 P1): the revision followed the whole state and the supervisor's status,
    so every scheduled check redrew Settings and Sites in every open tab."""
    _seed()
    client = TestClient(app)
    before = _page_rev(client, page)
    _routine_check("2026-10-16T10:05:00+00:00")
    clock.tick()
    assert _page_rev(client, page) == before


def test_a_messenger_connected_in_another_browser_changes_only_its_section(monkeypatch, clock):
    _seed()
    local = TestClient(app, headers=ORIGIN)
    html = local.get("/settings").text
    rev, digests = _marks(html)
    assert _page_rev(local, "/settings") == rev  # the page knows the revision it shows
    secrets = load_secrets()
    secrets["telegram"] = {"token": "123456:fake-token", "chat_ids": ["1001"]}
    save_secrets(secrets)  # what another browser's save writes
    clock.tick()
    now = _page_rev(local, "/settings")
    assert now != rev
    found = local.get("/page/fragments", params={"page": "/settings", "rev": now}).json()
    assert found["rev"] == now
    assert found["stale"] is False
    changed = {item["id"] for item in found["fragments"] if digests.get(item["id"]) != item["hash"]}
    assert changed == {"acc-bots"}
    bots = next(item["html"] for item in found["fragments"] if item["id"] == "acc-bots")
    assert 'id="acc-bots"' in bots
    assert "notifier-test-telegram" in bots


def test_the_secret_store_warning_appears_and_goes_live(clock):
    """The red "passwords and tokens cannot be opened" note is a fragment of its own
    (``settings-alerts``, present and empty while nothing is wrong): another device's breakage
    or ``tow keys`` fix shows on an open Settings page without a reload. Other sections may
    change with it (an unreadable store changes the messenger cards too)."""
    from tow.storage.secrets import encrypted_secrets_path

    _seed()
    secrets = load_secrets()
    secrets["telegram"] = {"token": "123456:fake-token", "chat_ids": ["1001"]}
    save_secrets(secrets)
    local = TestClient(app, headers=ORIGIN)
    html = local.get("/settings").text
    rev, digests = _marks(html)
    assert next(iter(digests)) == "settings-alerts"
    assert 'id="secret-store"' not in html
    store = encrypted_secrets_path()
    readable = store.read_bytes()
    store.write_bytes(b"not a secrets file")  # what a broken store or a foreign key reads as
    clock.tick()
    broken = _page_rev(local, "/settings")
    assert broken != rev
    found = local.get("/page/fragments", params={"page": "/settings", "rev": broken}).json()
    alerts = next(item for item in found["fragments"] if item["id"] == "settings-alerts")
    assert alerts["hash"] != digests["settings-alerts"]
    assert 'id="secret-store"' in alerts["html"]
    assert " keys status" in alerts["html"]
    store.write_bytes(readable)  # fixed elsewhere
    clock.tick()
    fixed = _page_rev(local, "/settings")
    assert fixed == rev
    found = local.get("/page/fragments", params={"page": "/settings", "rev": fixed}).json()
    alerts = next(item for item in found["fragments"] if item["id"] == "settings-alerts")
    assert alerts["hash"] == digests["settings-alerts"]
    assert "secret-store" not in alerts["html"]


def test_a_site_saved_in_another_browser_is_a_new_row_of_the_sites_list(clock):
    _seed()
    local = TestClient(app, headers=ORIGIN)
    rev, digests = _marks(local.get("/sites").text)
    assert next(iter(digests)) == "sites-head"
    assert list(digests)[-1] == "sites-empty"
    cfg = load_config()
    cfg.setdefault("trackers", {})["newsite"] = {
        "title": "New Site",
        "url_regex": r"^https?://new\.example/t/(\d+)",
        "fetch_hosts": ["https://new.example"],
        "download_path": "/dl/{id}",
    }
    save_config(cfg)
    clock.tick()
    now = _page_rev(local, "/sites")
    assert now != rev
    found = local.get("/page/fragments", params={"page": "/sites", "rev": now}).json()
    assert "site-row-newsite" in found["order"]
    assert "site-row-newsite" not in digests
    assert found["order"].index("site-row-newsite") < found["order"].index("sites-empty")
    changed = {item["id"] for item in found["fragments"] if digests.get(item["id"]) != item["hash"]}
    assert changed == {"site-row-newsite"}


def test_what_only_the_address_chose_is_not_a_change(clock):
    """A page opened on a section (?open=), a card (?card=) or a fix (?fix=) shows the same
    fragments as any other: its revision is the poll's."""
    _seed()
    local = TestClient(app, headers=ORIGIN)
    plain, _ = _marks(local.get("/settings").text)
    opened, _ = _marks(local.get("/settings", params={"open": "bots", "card": "telegram", "fix": "x"}).text)
    assert plain == opened == _page_rev(local, "/settings")
    assert _marks(local.get("/sites", params={"open": "rutor"}).text)[0] == _page_rev(local, "/sites")


def test_a_random_suggestion_and_the_free_space_are_no_change():
    html = (
        '<!--frag:a--><input value="tow-1" data-sync-volatile>'
        '<span class="nowrap">· <span data-sync-volatile>3 GB free</span></span>'
        '<details class="x" open data-q="open"><summary>s</summary></details><!--/frag:a-->'
    )
    other = (
        '<!--frag:a--><input value="tow-2" data-sync-volatile>'
        '<span class="nowrap">· <span data-sync-volatile>2 GB free</span></span>'
        '<details class="x" data-q="open"><summary>s</summary></details><!--/frag:a-->'
    )
    assert page_sync.fragments(html)["a"][0] == page_sync.fragments(other)["a"][0]
    assert 'data-q="open"' in page_sync.normalized(html)  # a word "open" in a value stays


def test_each_render_suggests_its_own_connection_without_changing_the_digest(clock):
    """1.33.1 A03: the blank new-connection card gets a random id per render (two tabs never share
    one), and only that card's own references to it are left out of the digest."""
    _seed()
    cfg = load_config()
    cfg["protection"] = {
        "enabled": True,
        "providers": [{"id": "first", "kind": "flaresolverr", "url": "http://127.0.0.1:8191", "enabled": True}],
    }
    save_config(cfg)
    local = TestClient(app, headers=ORIGIN)
    pages = [local.get("/settings").text for _ in range(2)]
    ids = [re.findall(r'name="provider_id" value="(new-connection-[0-9a-f]{32})"', page) for page in pages]
    assert len(ids[0]) == len(ids[1]) == 1
    assert ids[0] != ids[1]
    assert _marks(pages[0]) == _marks(pages[1]) == (_page_rev(local, "/settings"), _marks(pages[0])[1])


def test_only_the_blank_suggestions_own_references_leave_the_digest():
    """The same string elsewhere - a saved connection's id, a typed address - is kept: it may be a
    real change."""
    card = '<details class="c" data-sync-suggestion="{0}"><input name="provider_id" value="{0}"></details>'
    one, two = "new-connection-" + "a" * 32, "new-connection-" + "b" * 32
    assert page_sync.digest(card.format(one)) == page_sync.digest(card.format(two))
    saved = '<input name="provider_url" value="http://x/{0}">'
    assert page_sync.digest(card.format(one) + saved.format(one)) != page_sync.digest(
        card.format(two) + saved.format(two)
    )
    nested = '<details data-sync-suggestion="{0}"><details><p>{0}</p></details><p>{0}</p></details><p>{0}</p>'
    assert page_sync.normalized(nested.format(one)).count(one) == 1  # only the one after the card


@pytest.mark.parametrize("path", ["/settings", "/", "/sites", "/topics/t1/edit"])
def test_every_select_of_a_form_draws_one_option_chosen(path):
    """Final QA 1.33.0 F1: a select whose markup marks no option shows its first one, which a
    script comparing with the marked default counted as the owner's change. Every select a form
    draws marks the option it shows: the kind of client to add, a new Site access connection's
    service, and a topic whose torrent client is gone (Home's Add form, its edit panel)."""
    cfg = load_config()
    cfg["clients"] = [
        {"id": "main", "kind": "qbittorrent", "title": "Living room"},
        {"id": "second", "kind": "transmission", "title": "Office"},
    ]
    save_config(cfg)
    _seed()
    state = load_state()
    state["topics"][0]["client_id"] = "gone"
    save_state(state)
    html = TestClient(app, headers=ORIGIN).get(path).text
    selects = [field for field in _fields(html) if field["tag"] == "select" and not field["multiple"]]
    assert selects
    unmarked = [field["name"] for field in selects if sum(o["defaultSelected"] for o in field["options"]) != 1]
    assert unmarked == []


@pytest.mark.allow_system  # runs node on a local script; no network, no system changes
@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_an_idle_settings_page_follows_another_devices_clients_and_site_access(clock, tmp_path):
    """Final QA 1.33.0 F1: on a fresh Settings page "Torrent clients" and "Site access" counted as
    being edited (a select without a marked option), so they kept "shows once you leave it" and
    never followed another device. Drawn as the page has them, their fields are no draft; another
    device's save changes exactly what the page then puts in, and that is no draft either."""
    _seed()
    local = TestClient(app, headers=ORIGIN)
    html = local.get("/settings").text
    rev, digests = _marks(html)
    drawn = dict(re.findall(r"<!--frag:([A-Za-z0-9_-]+):[0-9a-f]{32}-->(.*?)<!--/frag:\1-->", html, re.DOTALL))
    for section in ("acc-clients", "acc-protection"):
        fields = _fields(drawn[section])
        assert any(field["tag"] == "select" for field in fields), section
        assert _changed(fields, tmp_path) == [], section
    other = TestClient(app, headers=ORIGIN)  # another device
    added = other.post("/settings/client/add", data={"kind": "transmission"}, follow_redirects=False)
    assert added.status_code == 303
    access = {
        "enabled": "1",
        "timeout_sec": "47",
        "min_interval_sec": "1800",
        "max_attempts": "2",
        "provider_id": "first",
        "provider_kind": "flaresolverr",
        "provider_url": "http://127.0.0.1:8191",
        "provider_order": "1",
        "provider_enabled": "first",
    }
    assert other.post("/settings/protection", data=access, follow_redirects=False).status_code == 303
    clock.tick()
    now = _page_rev(local, "/settings")
    assert now != rev
    found = local.get("/page/fragments", params={"page": "/settings", "rev": now}).json()
    changed = {item["id"]: item["html"] for item in found["fragments"] if digests.get(item["id"]) != item["hash"]}
    assert set(changed) == {"acc-clients", "acc-protection"}
    assert 'value="47"' in changed["acc-protection"]
    for section, fragment in changed.items():
        assert _changed(_fields(fragment), tmp_path) == [], section


def test_a_settings_conflict_page_knows_the_revision_the_poll_serves(clock):
    """Final QA 1.33.0 F2: a refused save's page (?conflict=) took its revision and digests from
    what it drew (the owner's draft in the section), while the poll draws the plain page: the
    section looked changed at once and said "Changed on another device or by TOW". Nothing changed
    since: the page's revision and digests are what /page/fragments serves."""
    _seed()
    a = TestClient(app, headers=ORIGIN)
    b = TestClient(app, headers=ORIGIN)
    b_form = _form(b.get("/settings").text, "/settings/interval")
    a_form = _form(a.get("/settings").text, "/settings/interval")
    saved = a.post("/settings/interval", data={**a_form, "interval_min": "30"}, follow_redirects=False)
    assert saved.status_code == 303
    refused = b.post("/settings/interval", data={**b_form, "interval_min": "90"}, follow_redirects=False)
    location = refused.headers["location"]
    assert "conflict=" in location
    clock.tick()
    page = b.get(location).text
    assert "edit-conflict" in page
    assert 'value="90"' in page  # the owner's draft is what the page shows
    rev, digests = _marks(page)
    assert rev == _page_rev(b, "/settings")
    found = b.get("/page/fragments", params={"page": "/settings", "rev": rev}).json()
    assert found["rev"] == rev
    assert found["stale"] is False
    assert {item["id"]: item["hash"] for item in found["fragments"]} == digests


# --------------------------------------------------------------------------- the fragments' answer


def test_fragments_are_never_stored_and_bound_to_their_revision(clock):
    _seed()
    local = TestClient(app, headers=ORIGIN)
    old = _page_rev(local, "/sites")
    answer = local.get("/page/fragments", params={"page": "/sites", "rev": old})
    assert answer.status_code == 200
    assert answer.headers["cache-control"] == "no-store"
    assert answer.json()["stale"] is False
    save_state({**load_state(), "mirrors": {"rutor": {"frozen": True}}})  # paused elsewhere
    clock.tick()
    stale = local.get("/page/fragments", params={"page": "/sites", "rev": old}).json()
    assert stale["stale"] is True
    assert stale["rev"] != old


@pytest.mark.parametrize(
    "params",
    [{"page": "/"}, {"page": "/elsewhere"}, {"page": "/sites", "rev": "nothex"}, {"page": "/history", "group": "bad"}],
)
def test_a_malformed_fragment_question_is_refused(params):
    _seed()
    answer = TestClient(app).get("/page/fragments", params=params)
    assert answer.status_code == 400
    assert answer.json() == {"ok": False, "error": "bad_request"}


def test_fragments_need_the_session_and_say_so_as_json(monkeypatch):
    _seed()
    lan = _lan(monkeypatch)
    assert lan.get("/page/fragments", params={"page": "/settings"}).status_code == 200
    stranger = TestClient(app, client=LAN, headers={**ORIGIN, "Accept": "application/json"})
    refused = stranger.get("/page/fragments", params={"page": "/settings"})
    assert refused.status_code == 401
    assert refused.headers["content-type"].startswith("application/json")  # never the sign-in page
    foreign = TestClient(app, base_url="http://evil.example").get("/page/fragments", params={"page": "/sites"})
    assert foreign.status_code == 403
    assert foreign.json()["ok"] is False


def test_the_network_never_gets_this_computers_settings_fragments(monkeypatch):
    """Local and remote are separate variants: the network's Settings never gets the form that
    opens or closes network access, which only this computer has."""
    _seed()
    lan = _lan(monkeypatch)
    local = TestClient(app, headers=ORIGIN)
    remote_access = next(
        f
        for f in lan.get("/page/fragments", params={"page": "/settings"}).json()["fragments"]
        if f["id"] == "acc-access"
    )
    local_access = next(
        f
        for f in local.get("/page/fragments", params={"page": "/settings"}).json()["fragments"]
        if f["id"] == "acc-access"
    )
    assert remote_access["hash"] != local_access["hash"]
    assert 'action="/settings/access"' in local_access["html"]
    assert 'action="/settings/access"' not in remote_access["html"]


CARD_ERROR = (
    r"cannot write D:\Copies\night\x.part (see /home/owner/tow/state.json)"
    " via https://api.telegram.org/bot123456:SECRETTOKEN/sendMessage?key=SECRETKEY"
)
CREDENTIALS = ("SECRETTOKEN", "SECRETKEY", "123456:")
LOCAL_WORDS = (r"D:\Copies\night\x.part", "/home/owner/tow/state.json")


@pytest.mark.parametrize("coded", [True, False])
def test_settings_card_errors_are_scrubbed_for_the_network_and_credentials_for_everyone(monkeypatch, coded, tmp_path):
    """A messenger card's last delivery error and the night copy's failure: a network device
    sees no folder, file or link secret in them (the values are scrubbed before the sentence is
    formatted, then the sentence); this PC keeps the paths, and nobody gets a link's
    credentials. The configured folder, a setting the device may edit, stays; nothing stored
    changes."""
    from tow.snapshots.record import record_status

    _seed()
    secrets = load_secrets()
    secrets["telegram"] = {"token": "123456:fake-token", "chat_ids": ["1001"]}
    save_secrets(secrets)
    folder = str(tmp_path / "Copies")
    cfg = load_config()
    cfg["backup_dir"] = folder
    save_config(cfg)
    state = load_state()
    status = {"ok": False, "at": 1800000000, "error": CARD_ERROR}
    if coded:
        status.update(
            error="Telegram: no answer",
            error_code="notifier.common.delivery_unknown",
            error_params={"what": "Telegram", "error": CARD_ERROR},
        )
    state["notify_status"] = {"telegram:1001": status}
    save_state(state)
    record_status(last_error=CARD_ERROR, last_error_at=1800000000.0)
    stored = load_state()["notify_status"]
    lan = _lan(monkeypatch)
    local = TestClient(app, headers=ORIGIN)

    def sections(client: TestClient) -> list[str]:
        page = dict(
            re.findall(
                r"<!--frag:([A-Za-z0-9_-]+):[0-9a-f]{32}-->(.*?)<!--/frag:\1-->",
                client.get("/settings").text,
                re.DOTALL,
            )
        )
        found = {
            f["id"]: f["html"] for f in client.get("/page/fragments", params={"page": "/settings"}).json()["fragments"]
        }
        return [page["acc-bots"], page["acc-transfer"], found["acc-bots"], found["acc-transfer"]]

    for html in sections(lan):
        # The link's host stays, as the whole word after "via" (the link itself is gone).
        assert set(re.findall(r" via ([\w.-]+)", html)) == {"api.telegram.org"}
        assert not [word for word in (*CREDENTIALS, *LOCAL_WORDS) if word in html]
    remote = sections(lan)
    assert folder in remote[1]  # the configured folder: a setting, not an error
    for html in sections(local):
        assert not [word for word in CREDENTIALS if word in html]
        assert [word for word in LOCAL_WORDS if word in html]
    assert load_state()["notify_status"] == stored


# --------------------------------------------------------------------------- History


def test_historys_filters_are_part_of_the_request_variant(clock):
    _seed()
    client = TestClient(app)
    every = client.get("/sync.json", params={"page": "/history"})
    errors = client.get("/sync.json", params={"page": "/history", "group": "errors"})
    searched = client.get("/sync.json", params={"page": "/history", "q": "Show A"})
    revs = {every.json()["rev"]["page"], errors.json()["rev"]["page"], searched.json()["rev"]["page"]}
    assert len(revs) == 3
    assert len({every.headers["etag"], errors.headers["etag"], searched.headers["etag"]}) == 3
    again = client.get(
        "/sync.json", params={"page": "/history", "group": "errors"}, headers={"If-None-Match": errors.headers["etag"]}
    )
    assert again.status_code == 304
    assert client.get("/sync.json", params={"page": "/history", "group": "nope"}).status_code == 400
    assert client.get("/sync.json", params={"page": "/history", "q": "x" * 201}).status_code == 400


def test_history_lines_are_keyed_by_their_events_and_follow_the_filters(clock):
    _seed()
    log_event("topic_pause", topic_id="t1")
    log_event("check_fail", topic_id="t2", error="qBittorrent: connection refused", error_class="qbit")
    client = TestClient(app, headers=ORIGIN)
    page = client.get("/history", params={"group": "errors"}).text
    shown = re.findall(r'<div class="log-line" data-event="([^"]+)"', page)
    assert len(shown) == 1
    before = _page_rev(client, "/history", group="errors")
    log_event("check_fail", topic_id="t1", error="qBittorrent: connection refused", error_class="qbit")
    clock.tick()
    now = _page_rev(client, "/history", group="errors")
    assert now != before
    found = client.get("/page/fragments", params={"page": "/history", "group": "errors", "rev": now}).json()
    assert found["rev"] == now
    assert found["limit"] == page_sync.HISTORY_LIMIT
    keys = [entry["id"] for entry in found["entries"]]
    assert len(keys) == 2
    assert keys[1] == shown[0]
    assert all(f'data-event="{entry["id"]}"' in entry["html"] for entry in found["entries"])
    assert "Show A" in found["entries"][0]["html"]
    assert 'id="latest-check_fail"' in found["entries"][0]["html"]
    assert 'id="latest-check_fail"' not in found["entries"][1]["html"]  # the newest line has the address
    every = client.get("/page/fragments", params={"page": "/history"}).json()
    assert len(every["entries"]) == 3


@pytest.mark.parametrize("group", ["", "errors"])
def test_a_log_line_no_history_filter_shows_changes_no_history_revision(group, clock):
    """The History revision is the digest of the window its filters draw: a routine line that
    no group shows (a site checked) or that this filter does not take leaves it as it was, so no
    open History tab asks for its lines again; a line it shows changes it."""
    _seed()
    log_event("check_fail", topic_id="t2", error="qBittorrent: connection refused", error_class="qbit")
    client = TestClient(app, headers=ORIGIN)
    params = {"group": group} if group else {}
    before = _page_rev(client, "/history", **params)
    log_event("tracker_checked", tracker="rutor")  # in no History group
    if group:
        log_event("topic_pause", topic_id="t1")  # History shows it, this filter does not
    clock.tick()
    assert _page_rev(client, "/history", **params) == before
    log_event("check_fail", topic_id="t1", error="qBittorrent: connection refused", error_class="qbit")
    clock.tick()
    now = _page_rev(client, "/history", **params)
    assert now != before
    found = client.get("/page/fragments", params={"page": "/history", "rev": now, **params}).json()
    assert found["rev"] == now
    assert len(found["entries"]) == 2


def test_the_history_window_is_read_once_per_change_for_every_tab_of_its_filters(monkeypatch, clock):
    """Every open History tab with the same filters shares one reading of the log per change:
    the poll's revision and the lines /page/fragments hands out are that one window."""
    _seed()
    log_event("check_fail", topic_id="t2", error="qBittorrent: connection refused", error_class="qbit")
    calls: list[str] = []
    plain = services.history_events

    def counted(**kwargs):
        calls.append(kwargs.get("group", ""))
        return plain(**kwargs)

    monkeypatch.setattr(services, "history_events", counted)
    tabs = [TestClient(app, headers=ORIGIN) for _ in range(3)]
    revs = {_page_rev(tab, "/history", group="errors") for tab in tabs}
    assert len(revs) == 1
    (rev,) = revs
    for tab in tabs:
        found = tab.get("/page/fragments", params={"page": "/history", "group": "errors", "rev": rev}).json()
        assert found["rev"] == rev
    assert calls == ["errors"]
    log_event("check_fail", topic_id="t1", error="qBittorrent: connection refused", error_class="qbit")
    clock.tick()
    for tab in tabs:
        assert _page_rev(tab, "/history", group="errors") != rev
    assert calls == ["errors", "errors"]


def test_the_history_windows_kept_are_bounded(monkeypatch, clock):
    """Each search is a variant of its own: the windows kept for them are bounded in number."""
    _seed()
    log_event("check_fail", topic_id="t2", error="qBittorrent: connection refused", error_class="qbit")
    client = TestClient(app, headers=ORIGIN)
    for index in range(sync.HISTORY_VARIANTS + 5):
        _page_rev(client, "/history", q=f"Show {index}")
    assert sync.AREAS_MEMO.history_variants() <= sync.HISTORY_VARIANTS


def test_the_history_limit_note_is_always_drawn_and_hidden_below_the_limit(monkeypatch):
    """pages.js shows the "newest N" note once live merges fill the list to its limit and hides it
    below: the page must carry it from the start, hidden while fewer lines are shown."""
    from tow.web import routes_history

    _seed()
    monkeypatch.setattr(routes_history, "HISTORY_LIMIT", 3)
    log_event("topic_pause", topic_id="t1")
    client = TestClient(app)
    notes = re.findall(r"<p [^>]*data-history-note[^>]*>", client.get("/history").text)
    assert len(notes) == 1
    assert "hidden" in notes[0]
    for _ in range(3):
        log_event("topic_pause", topic_id="t2")
    notes = re.findall(r"<p [^>]*data-history-note[^>]*>", client.get("/history").text)
    assert len(notes) == 1
    assert "hidden" not in notes[0]


def test_an_event_of_an_older_tow_without_an_id_keeps_one_key():
    record = {"ts": "2026-10-01T18:50:00+03:00", "kind": "check", "n": 3}
    assert page_sync.event_key(record) == page_sync.event_key(dict(record))
    assert (
        page_sync.event_key({**record, "event_id": "0123456789abcdef0123456789abcdef"})
        == "0123456789abcdef0123456789abcdef"
    )
