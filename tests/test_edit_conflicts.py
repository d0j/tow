"""Two devices editing the same thing (1.33.0): a save changes only what its form changed since it
was rendered, on what is saved now; the same field changed on both sides to different values is
refused with the owner's draft kept and both values shown, until the owner chooses; a form
without a valid base is refused ("reload this form").

Every test plays two clients in a fixed order of steps (the interleaving is the test): "B opens
the panel", "A saves", "B saves". Forms are read from the rendered page and posted as a browser
without scripts would post them.
"""

from __future__ import annotations

import json
import re
import threading
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from tow.config import load_config, save_config
from tow.store import load_state, persistence_lock, save_state
from tow.web import app

URL = "http://rutor.info/torrent/1234567/show"
URL2 = "http://rutor.info/torrent/7654321/other"
HASH = "AA" * 20


class _Forms(HTMLParser):
    """The fields a browser without scripts sends for each form of a page (by form id)."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: dict[str, list[tuple[str, str]]] = {}
        self._form: str | None = None
        self._textarea: str | None = None
        self._select: str | None = None
        self._selected: str | None = None
        self._first_option: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {key: value or "" for key, value in attrs}
        if tag == "form":
            self._form = a.get("id") or a.get("action") or ""
            self.forms.setdefault(self._form, [])
            return
        if self._form is None:
            return
        fields = self.forms[self._form]
        if tag == "input" and a.get("name") and "disabled" not in a:
            kind = a.get("type", "text")
            if kind in {"checkbox", "radio"}:
                if "checked" in a:
                    fields.append((a["name"], a.get("value", "on")))
            elif kind not in {"submit", "button", "file"}:
                fields.append((a["name"], a.get("value", "")))
        elif tag == "textarea" and a.get("name"):
            self._textarea, self._text = a["name"], []
        elif tag == "select" and a.get("name"):
            self._select, self._selected, self._first_option = a["name"], None, None
        elif tag == "option" and self._select is not None:
            value = a.get("value", "")
            if self._first_option is None:
                self._first_option = value
            if "selected" in a:
                self._selected = value

    def handle_data(self, data):
        if self._textarea is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "textarea" and self._textarea is not None and self._form is not None:
            self.forms[self._form].append((self._textarea, "".join(self._text).removeprefix("\n")))
            self._textarea = None
        elif tag == "select" and self._select is not None and self._form is not None:
            self.forms[self._form].append((self._select, self._selected or self._first_option or ""))
            self._select = None


def form_of(html: str, form_id: str) -> dict[str, str]:
    parser = _Forms()
    parser.feed(html)
    return dict(parser.forms[form_id])


def _english() -> None:
    cfg = load_config()
    cfg["language"] = "en"
    save_config(cfg)


def _topic(**extra) -> dict:
    return {
        "id": "t1",
        "title": "Show A",
        "url": URL,
        "save_path": r"D:\TV",
        "client_id": "default",
        **extra,
    }


@pytest.fixture
def two(monkeypatch):
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    _english()
    save_state({"topics": [_topic()], "mirrors": {}})
    a = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    b = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    return a, b


def panel(client: TestClient, tid: str = "t1") -> dict[str, str]:
    """The edit form as the Home row's panel renders it now."""
    response = client.get(f"/topics/{tid}/edit-panel")
    assert response.status_code == 200
    return form_of(response.text, f"ed-{tid}")


def save(client: TestClient, form: dict[str, str], tid: str = "t1", **headers):
    return client.post(f"/topics/{tid}/edit", data=form, follow_redirects=False, headers=headers)


def topic(tid: str = "t1") -> dict:
    return next(item for item in load_state()["topics"] if item["id"] == tid)


def test_another_devices_rename_survives_a_timer_only_save(two):
    """The reproduced loss (r1324 s14): B opened topic-2's panel, A renamed it, B changed only the
    timer and saved - A's name was gone and B was told the renamed topic was saved."""
    a, b = two
    b_form = panel(b)
    a_form = panel(a)
    assert save(a, {**a_form, "title": "Renamed by A"}).status_code == 303
    response = save(b, {**b_form, "check_interval_min": "45"})
    assert response.status_code == 303
    assert "conflict=" not in response.headers["location"]
    saved = topic()
    assert saved["title"] == "Renamed by A"
    assert saved["check_interval_min"] == 45


def test_independent_edits_of_other_fields_all_merge(two):
    a, b = two
    b_form = panel(b)
    a_form = panel(a)
    save(a, {**a_form, "save_path": r"D:\Shows", "tracking_mode": ""})  # folder + "download once"
    save(b, {**b_form, "selection_mode": "episodes", "selection_value": "1-3"})
    saved = topic()
    assert saved["save_path"] == r"D:\Shows"
    assert saved["tracking_mode"] == "once"
    assert saved["selection"] == {"mode": "episodes", "value": "1-3"}


def test_the_same_field_changed_on_both_sides_is_refused_with_the_draft_kept(two):
    a, b = two
    b_form = panel(b)
    save(a, panel(a) | {"title": "Name from A"})
    response = save(b, {**b_form, "title": "Name from B", "check_interval_min": "30"})
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/topics/t1/edit?conflict=")
    assert "Name from B" not in location  # the draft stays on the server, never in the address
    assert topic()["title"] == "Name from A"
    assert topic().get("check_interval_min") is None  # nothing of the refused save is written

    page = b.get(location).text
    assert "Saved now: Name from A" in page
    assert "Yours: Name from B" in page
    again = form_of(page, "ed-t1")
    assert again["title"] == "Name from B"  # the owner's draft, not what is saved
    assert again["check_interval_min"] == "30"
    assert "resolve_title" not in again  # no choice is made for the owner

    # Saved without a choice: refused again, still nothing written.
    refused = save(b, again)
    assert refused.headers["location"].startswith("/topics/t1/edit?conflict=")
    assert topic()["title"] == "Name from A"

    done = save(b, {**again, "resolve_title": "mine"})
    assert "conflict=" not in done.headers["location"]
    assert (topic()["title"], topic()["check_interval_min"]) == ("Name from B", 30)


def test_a_conflict_resolved_by_keeping_the_saved_value_saves_the_rest(two):
    a, b = two
    b_form = panel(b)
    save(a, panel(a) | {"title": "Name from A"})
    location = save(b, {**b_form, "title": "Name from B", "check_interval_min": "30"}).headers["location"]
    again = form_of(b.get(location).text, "ed-t1")
    save(b, {**again, "resolve_title": "current"})
    assert (topic()["title"], topic()["check_interval_min"]) == ("Name from A", 30)


def test_the_same_final_value_on_both_sides_is_not_a_conflict(two):
    a, b = two
    b_form = panel(b)
    save(a, panel(a) | {"title": "Same name", "check_interval_min": "20"})
    response = save(b, {**b_form, "title": "Same name", "check_interval_min": "20"})
    assert "conflict=" not in response.headers["location"]
    assert (topic()["title"], topic()["check_interval_min"]) == ("Same name", 20)


def test_a_conflict_answers_a_scripted_save_with_the_page_that_resolves_it(two):
    """With scripts the panel posts with X-TOW-Fetch; the answer names the same page."""
    a, b = two
    b_form = panel(b)
    save(a, panel(a) | {"title": "Name from A"})
    response = save(b, {**b_form, "title": "Name from B"}, **{"X-TOW-Fetch": "1"})
    target = response.json()["redirect"] if response.status_code == 200 else response.headers["location"]
    assert "/topics/t1/edit?conflict=" in target


def test_a_save_without_a_base_is_refused(two, strict_edit_base):
    """A page of an older TOW (or a hand-made post) would put back what it showed."""
    a, _b = two
    response = a.post("/topics/t1/edit", data={"title": "Old page", "url": URL, "save_path": r"D:\TV"})
    assert topic()["title"] == "Show A"
    assert "this form is out of date" in response.text  # the edit page, saying why


@pytest.mark.parametrize(
    "damage",
    [
        lambda base: "not json",
        lambda base: base.replace('"id":"t1"', '"id":"t2"'),  # another topic's
        lambda base: base.replace('"k":"topic"', '"k":"site"'),  # another form's
        lambda base: base.replace('"v":1', '"v":0'),  # another schema
        lambda base: json.dumps({**json.loads(base), "f": {}}),  # fields missing
        lambda base: "x" * 20000,
    ],
)
def test_a_damaged_base_is_refused(two, strict_edit_base, damage):
    a, _b = two
    form = panel(a)
    a.post("/topics/t1/edit", data={**form, "title": "Changed", "base": damage(form["base"])})
    assert topic()["title"] == "Show A"


def test_a_save_of_a_topic_deleted_meanwhile_does_not_bring_it_back(two):
    a, b = two
    b_form = panel(b)
    assert a.post("/topics/t1/delete", follow_redirects=False).status_code == 303
    response = save(b, {**b_form, "title": "Edited"})
    assert load_state()["topics"] == []
    assert "location" in response.headers


def test_a_save_after_undo_of_a_delete_merges_with_the_topic_put_back(two):
    a, b = two
    save(a, panel(a) | {"title": "Renamed by A"})
    b_form = panel(b)
    a.post("/topics/t1/delete")
    assert a.post("/undo", follow_redirects=False).status_code == 303
    assert topic()["title"] == "Renamed by A"
    save(b, {**b_form, "check_interval_min": "15"})
    assert (topic()["title"], topic()["check_interval_min"]) == ("Renamed by A", 15)


def test_undo_after_a_merged_save_puts_back_the_state_just_before_it(two):
    """Undo is stamped from the topic as saved right before the merged write: A's rename, made
    after B opened the panel, survives B's save and its Undo."""
    a, b = two
    b_form = panel(b)
    save(a, panel(a) | {"title": "Renamed by A"})
    save(b, {**b_form, "check_interval_min": "45"})
    assert b.post("/undo", follow_redirects=False).status_code == 303
    assert topic()["title"] == "Renamed by A"
    assert topic().get("check_interval_min") is None


def test_a_check_finished_meanwhile_is_kept(two):
    _a, b = two
    b_form = panel(b)
    with persistence_lock():
        state = load_state()
        state["topics"][0].update(last_ok=True, last_check="2026-10-17T10:00:00+00:00", hash=HASH)
        save_state(state)
    save(b, {**b_form, "title": "Edited"})
    saved = topic()
    assert (saved["title"], saved["last_ok"], saved["hash"]) == ("Edited", True, HASH)


def test_the_selection_merges_as_one_group(two):
    """Mode and rule are one value: A's rule and B's other mode conflict, never half-merge."""
    a, b = two
    b_form = panel(b)
    save(a, panel(a) | {"selection_mode": "episodes", "selection_value": "1-5"})
    response = save(b, {**b_form, "selection_mode": "files", "selection_value": "*.mkv"})
    assert "conflict=" in response.headers["location"]
    assert topic()["selection"] == {"mode": "episodes", "value": "1-5"}


def test_rendering_the_forms_writes_nothing(two):
    """A base lives in the page: opening panels, pages and settings writes no file."""
    from tow import paths

    a, _b = two
    data = paths.data_dir()

    def snapshot():
        # Lock files are how readers wait for writers, not data.
        return {p: p.read_bytes() for p in data.rglob("*") if p.is_file() and p.suffix != ".lock"}

    before = snapshot()
    for address in ("/topics/t1/edit-panel", "/topics/t1/edit", "/sites", "/settings"):
        assert a.get(address).status_code == 200
    assert snapshot() == before


# --- A folder move in the client, unlocked between two checks ---------------------------------


@pytest.fixture
def mover(monkeypatch):
    """A fake client whose move waits until the test lets it finish."""
    import asyncio

    monkeypatch.setattr("tow.web.middleware._SITE_HTTP_LOCK", asyncio.Lock())
    _english()
    save_state({"topics": [_topic(hash=HASH)], "mirrors": {}})
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []

    class Mover:
        def set_location(self, infohash: str, save_path: str) -> str:
            calls.append(save_path)
            return "ok"

        def inspect_torrent(self, infohash: str) -> dict:
            return {"hash": HASH, "save_path": r"D:\TV", "tags": ["tow"]}

    def slow_move(*_args, **_kwargs):
        entered.set()
        release.wait(10)
        return "done"

    monkeypatch.setattr("tow.web.services.client_from_secrets", lambda *_a, **_k: Mover())
    monkeypatch.setattr("tow.web.services.await_relocation", slow_move)
    return entered, release, calls


def _in_thread(fn) -> tuple[threading.Thread, dict]:
    out: dict = {}
    thread = threading.Thread(target=lambda: out.update(response=fn()), daemon=True)
    thread.start()
    return thread, out


def _write(**fields) -> None:
    with persistence_lock():
        state = load_state()
        state["topics"][0].update(fields)
        save_state(state)


def test_an_edit_made_while_the_client_moves_the_folder_survives(mover):
    entered, release, _calls = mover
    with TestClient(app, headers={"Origin": "http://127.0.0.1"}) as b:
        form = panel(b)
        thread, out = _in_thread(lambda: save(b, {**form, "save_path": r"D:\New"}))
        try:
            assert entered.wait(5)
            _write(title="Renamed by A", check_interval_min=None)
        finally:
            release.set()
            thread.join(10)
    assert "conflict=" not in out["response"].headers["location"]
    saved = topic()
    assert (saved["title"], saved["save_path"]) == ("Renamed by A", r"D:\New")


def test_a_conflict_found_after_the_move_keeps_the_moved_folder(mover):
    """The client moved the torrent: refusing the save cannot undo that, so the folder is
    recorded and the field changed on both sides waits for the owner's choice."""
    entered, release, _calls = mover
    with TestClient(app, headers={"Origin": "http://127.0.0.1"}) as b:
        form = panel(b)
        thread, out = _in_thread(lambda: save(b, {**form, "save_path": r"D:\New", "title": "Name from B"}))
        try:
            assert entered.wait(5)
            _write(title="Name from A", title_set=True)
        finally:
            release.set()
            thread.join(10)
    location = out["response"].headers["location"]
    assert location.startswith("/topics/t1/edit?conflict=")
    saved = topic()
    assert (saved["title"], saved["save_path"]) == ("Name from A", r"D:\New")


def test_two_moves_of_one_topic_never_run_together(mover):
    entered, release, calls = mover
    with TestClient(app, headers={"Origin": "http://127.0.0.1"}) as b:
        first_form = panel(b)
        second_form = panel(b)
        thread, _out = _in_thread(lambda: save(b, {**first_form, "save_path": r"D:\First"}))
        try:
            assert entered.wait(5)
            second = save(b, {**second_form, "save_path": r"D:\Second"})
            assert parse_qs(urlsplit(second.headers["location"]).query).get("flash")
        finally:
            release.set()
            thread.join(10)
    assert calls == [r"D:\First"]
    assert topic()["save_path"] == r"D:\First"


def test_undo_never_moves_a_torrent_whose_folder_a_save_is_moving(monkeypatch):
    """Final review 1.33.0 #5: an earlier folder edit was still undoable; while another save's
    unlocked client move waited, Undo sent its own move of the same torrent beside it. Undo now
    shares the save's per-topic reservation: it is refused (nothing written, the Undo stays) and
    works once the move has ended."""
    import asyncio

    monkeypatch.setattr("tow.web.middleware._SITE_HTTP_LOCK", asyncio.Lock())
    _english()
    save_state({"topics": [_topic(hash=HASH)], "mirrors": {}})
    calls: list[str] = []
    where = {"path": r"D:\TV"}
    entered, release = threading.Event(), threading.Event()
    release.set()  # the first save's move ends at once

    class Client:
        def set_location(self, infohash: str, save_path: str) -> str:
            calls.append(save_path)
            where["path"] = save_path
            return "ok"

        def inspect_torrent(self, infohash: str) -> dict:
            return {"hash": HASH, "save_path": where["path"], "tags": ["tow"]}

    def slow_move(*_args, **_kwargs):
        entered.set()
        release.wait(10)
        return "done"

    monkeypatch.setattr("tow.web.services.client_from_secrets", lambda *_a, **_k: Client())
    monkeypatch.setattr("tow.clients.factory.from_secrets", lambda *_a, **_k: Client())
    monkeypatch.setattr("tow.web.services.await_relocation", slow_move)
    monkeypatch.setattr("tow.check.await_relocation", lambda *_a, **_k: "done")
    with TestClient(app, headers={"Origin": "http://127.0.0.1"}) as b:
        save(b, {**panel(b), "save_path": r"D:\First"})
        assert topic()["save_path"] == r"D:\First"
        entered.clear()
        release.clear()
        form = panel(b)
        thread, _out = _in_thread(lambda: save(b, {**form, "save_path": r"D:\Second"}))
        try:
            assert entered.wait(5)
            refused = b.post("/undo", follow_redirects=False)
            assert refused.status_code == 303
            assert calls == [r"D:\First", r"D:\Second"]  # no move back beside the running one
            assert load_state().get("undo")  # the Undo stays for another try
            assert "not undone: a save is moving this topic" in b.get(refused.headers["location"]).text
        finally:
            release.set()
            thread.join(10)
        assert topic()["save_path"] == r"D:\Second"
        assert b.post("/undo", follow_redirects=False).status_code == 303
    assert calls == [r"D:\First", r"D:\Second", r"D:\First"]
    assert topic()["save_path"] == r"D:\First"


# --- Sites ------------------------------------------------------------------------------------


@pytest.fixture
def sites(monkeypatch):
    from tow.store import save_secrets

    monkeypatch.setattr("tow.web.site_form._resolve_addresses", lambda _name: ["93.184.216.34"])
    cfg = load_config()
    cfg["language"] = "en"
    cfg["trackers"] = {
        "rutor": {
            "title": "rutor",
            "url_regex": r"rutor/(\d+)",
            "fetch_hosts": ["http://rutor.example"],
            "login_hosts": ["http://rutor.example"],
            "download_path": "/download/{id}",
            "login_path": "",
        }
    }
    save_config(cfg)
    save_state({"topics": [], "mirrors": {}})
    save_secrets({"trackers": {"rutor": {"username": "fixture-user", "password": "fixture-secret"}}})
    a = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    b = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    return a, b


def card(client: TestClient, name: str = "rutor") -> dict[str, str]:
    return form_of(client.get("/sites").text, f"ed-site-{name}")


def save_card(client: TestClient, form: dict[str, str], name: str = "rutor"):
    return client.post(f"/sites/{name}", data=form, follow_redirects=False)


def site(name: str = "rutor") -> dict:
    return load_config()["trackers"][name]


def site_login(name: str = "rutor") -> dict:
    from tow.store import load_secrets

    return load_secrets()["trackers"][name]


def test_another_devices_mirror_survives_a_save_of_another_field(sites):
    a, b = sites
    b_form = card(b)
    a_form = card(a)
    hosts = "http://rutor.example\nhttp://mirror.example"
    assert save_card(a, {**a_form, "fetch_hosts": hosts}).status_code == 303
    assert site()["fetch_hosts"] == ["http://rutor.example", "http://mirror.example"]
    response = save_card(b, {**b_form, "topic_path": "/torrent/{id}"})
    assert "conflict=" not in response.headers["location"]
    assert site()["fetch_hosts"] == ["http://rutor.example", "http://mirror.example"]
    assert site()["topic_path"] == "/torrent/{id}"


def test_a_site_field_changed_on_both_sides_is_refused_with_the_draft_kept(sites):
    a, b = sites
    b_form = card(b)
    save_card(a, {**card(a), "login_path": "/login-a.php"})
    response = save_card(b, {**b_form, "login_path": "/login-b.php", "topic_path": "/t/{id}"})
    location = response.headers["location"]
    assert "conflict=" in location
    assert "login-b" not in location
    assert (site()["login_path"], site().get("topic_path")) == ("/login-a.php", None)
    page = b.get(location).text
    assert "Saved now: /login-a.php" in page
    assert "Yours: /login-b.php" in page
    again = form_of(page, "ed-site-rutor")
    assert (again["login_path"], again["topic_path"]) == ("/login-b.php", "/t/{id}")
    save_card(b, {**again, "resolve_login_path": "mine"})
    assert (site()["login_path"], site()["topic_path"]) == ("/login-b.php", "/t/{id}")


def test_mirrors_and_sign_in_mirrors_merge_as_one_group(sites):
    a, b = sites
    b_form = card(b)
    save_card(a, {**card(a), "login_hosts": ""})  # A: no sign-in mirror
    response = save_card(b, {**b_form, "fetch_hosts": "http://rutor.example\nhttp://second.example"})
    assert "conflict=" in response.headers["location"]
    assert site()["fetch_hosts"] == ["http://rutor.example"]


def test_a_site_save_without_a_base_is_refused(sites, strict_edit_base):
    a, _b = sites
    form = {key: value for key, value in card(a).items() if key != "base"}
    save_card(a, {**form, "login_path": "/old-page.php"})
    assert site()["login_path"] == ""


def test_the_password_is_never_in_the_page_and_a_kept_one_survives(sites):
    """The base holds an opaque token, never the password or a digest of it to guess against."""
    import hashlib

    a, b = sites
    page = b.get("/sites").text
    assert "fixture-secret" not in page
    for algorithm in ("md5", "sha1", "sha256"):
        assert hashlib.new(algorithm, b"fixture-secret").hexdigest()[:16] not in page
    b_form = card(b)
    assert b_form["password"] == ""
    save_card(a, {**card(a), "password": "changed-by-a"})
    save_card(b, {**b_form, "topic_path": "/t/{id}"})  # B keeps (empty field)
    assert site_login()["password"] == "changed-by-a"


def test_a_password_replaced_on_both_sides_is_a_conflict_and_never_kept_in_the_draft(sites):
    a, b = sites
    b_form = card(b)
    save_card(a, {**card(a), "password": "changed-by-a"})
    response = save_card(b, {**b_form, "password": "typed-by-b"})
    location = response.headers["location"]
    assert "conflict=" in location
    assert site_login()["password"] == "changed-by-a"
    page = b.get(location).text
    assert "typed-by-b" not in page
    assert "changed-by-a" not in page
    assert "Type the password or token again" in page


def test_the_same_new_password_on_both_sides_is_not_a_conflict(sites):
    a, b = sites
    b_form = card(b)
    save_card(a, {**card(a), "password": "same-new"})
    response = save_card(b, {**b_form, "password": "same-new"})
    assert "conflict=" not in response.headers["location"]
    assert site_login()["password"] == "same-new"


def test_a_password_can_be_removed_by_intent(sites):
    a, _b = sites
    save_card(a, {**card(a), "password_intent": "remove"})
    assert "password" not in site_login()
    assert site_login()["username"] == "fixture-user"


def test_a_site_base_expires_with_the_server_key(sites, monkeypatch, strict_edit_base):
    """A secret's token is keyed by this server process: a card from before a restart is
    refused ("reload this form"), never compared against a key it was not made with."""
    from tow.web import edit_base

    a, _b = sites
    form = card(a)
    monkeypatch.setattr(edit_base, "_KEY_ID", "restart1")
    save_card(a, {**form, "login_path": "/after-restart.php"})
    assert site()["login_path"] == ""


# --- Settings ---------------------------------------------------------------------------------


TOKEN_A = "111111:" + "a" * 30
TOKEN_B = "222222:" + "b" * 30


@pytest.fixture
def settings_two():
    from tow.store import save_secrets

    cfg = load_config()
    cfg["language"] = "en"
    cfg["clients"] = [{"id": "main", "kind": "qbittorrent", "title": "Living room", "default": True}]
    save_config(cfg)
    save_secrets(
        {
            "clients": {"main": {"host": "192.0.2.10", "port": 8080, "username": "admin", "password": "pw-1"}},
            "telegram": {"token": "123456:fake-token", "chat_ids": ["1001"]},
        }
    )
    a = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    b = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    return a, b


def settings_form(client: TestClient, form_id: str) -> dict[str, str]:
    return form_of(client.get("/settings").text, form_id)


def post(client: TestClient, action: str, form: dict[str, str]):
    return client.post(action, data=form, follow_redirects=False)


def test_another_devices_check_interval_survives_a_save_of_the_undo_time(settings_two):
    a, b = settings_two
    b_form = settings_form(b, "/settings/interval")
    post(a, "/settings/interval", {**settings_form(a, "/settings/interval"), "interval_min": "30"})
    assert load_config()["interval_sec"] == 1800
    response = post(b, "/settings/interval", {**b_form, "flash_ttl_min": "3"})
    assert "conflict=" not in response.headers["location"]
    assert (load_config()["interval_sec"], load_config()["flash_ttl_sec"]) == (1800, 180)


def test_a_check_interval_changed_on_both_sides_is_refused_with_the_draft_kept(settings_two):
    a, b = settings_two
    b_form = settings_form(b, "/settings/interval")
    post(a, "/settings/interval", {**settings_form(a, "/settings/interval"), "interval_min": "30"})
    location = post(b, "/settings/interval", {**b_form, "interval_min": "90"}).headers["location"]
    assert "conflict=" in location
    assert load_config()["interval_sec"] == 1800
    page = b.get(location).text
    assert "Saved now: 30" in page
    assert "Yours: 90" in page
    again = form_of(page, "/settings/interval")
    assert again["interval_min"] == "90"
    post(b, "/settings/interval", {**again, "resolve_interval": "mine"})
    assert load_config()["interval_sec"] == 5400


def test_a_resolved_interval_conflict_keeps_another_devices_undo_time(settings_two):
    """Final review 1.33.0 #1: only the interval conflicts; the recovery form brought back B's
    whole old draft, so choosing "mine" for the interval also put the undo time back to 1."""
    a, b = settings_two
    b_form = settings_form(b, "/settings/interval")
    a_form = settings_form(a, "/settings/interval")
    post(a, "/settings/interval", {**a_form, "interval_min": "90", "flash_ttl_min": "2"})
    assert (load_config()["interval_sec"], load_config()["flash_ttl_sec"]) == (5400, 120)
    location = post(b, "/settings/interval", {**b_form, "interval_min": "120"}).headers["location"]
    assert "conflict=" in location
    again = form_of(b.get(location).text, "/settings/interval")
    assert (again["interval_min"], again["flash_ttl_min"]) == ("120", "2")  # untouched: saved now
    response = post(b, "/settings/interval", {**again, "resolve_interval": "mine"})
    assert "conflict=" not in response.headers["location"]
    assert (load_config()["interval_sec"], load_config()["flash_ttl_sec"]) == (7200, 120)


def test_a_topic_recovery_form_shows_untouched_fields_as_saved_now(two):
    """The same rule on every form: a recovery form keeps the owner's draft only for what they
    changed or what conflicts."""
    a, b = two
    b_form = panel(b)
    save(a, panel(a) | {"title": "Name from A", "check_interval_min": "20"})
    location = save(b, {**b_form, "title": "Name from B"}).headers["location"]
    again = form_of(b.get(location).text, "ed-t1")
    assert (again["title"], again["check_interval_min"]) == ("Name from B", "20")
    save(b, {**again, "resolve_title": "mine"})
    assert (topic()["title"], topic()["check_interval_min"]) == ("Name from B", 20)


def test_a_site_recovery_form_shows_untouched_fields_as_saved_now(sites):
    a, b = sites
    b_card = card(b)
    save_card(a, {**card(a), "login_path": "/login-a.php", "topic_path": "/a/{id}"})
    location = save_card(b, {**b_card, "login_path": "/login-b.php"}).headers["location"]
    again = form_of(b.get(location).text, "ed-site-rutor")
    assert (again["login_path"], again["topic_path"]) == ("/login-b.php", "/a/{id}")
    save_card(b, {**again, "resolve_login_path": "mine"})
    assert (site()["login_path"], site()["topic_path"]) == ("/login-b.php", "/a/{id}")


def test_the_language_chosen_on_both_sides_is_a_conflict(settings_two):
    a, b = settings_two
    b_form = settings_form(b, "/settings/language")
    post(a, "/settings/language", {**settings_form(a, "/settings/language"), "language": "ru"})
    response = post(b, "/settings/language", {**b_form, "language": "auto"})
    assert "conflict=" in response.headers["location"]
    assert load_config()["language"] == "ru"


def test_an_unchanged_client_card_does_not_put_back_another_devices_address(settings_two):
    from tow.store import load_secrets

    a, b = settings_two
    b_form = settings_form(b, "client-form-client-0")
    # A new address needs the password typed again (it never goes where it was not typed for).
    post(a, "/settings/client", {**settings_form(a, "client-form-client-0"), "host": "192.0.2.20", "password": "pw-1"})
    assert load_secrets()["clients"]["main"]["host"] == "192.0.2.20"
    post(b, "/settings/client", b_form)  # B's card saved as it was shown
    assert load_secrets()["clients"]["main"]["host"] == "192.0.2.20"


def test_a_client_card_changed_on_both_sides_is_a_credential_conflict(settings_two):
    from tow.store import load_secrets

    a, b = settings_two
    b_form = settings_form(b, "client-form-client-0")
    post(a, "/settings/client", {**settings_form(a, "client-form-client-0"), "password": "pw-from-a"})
    response = post(b, "/settings/client", {**b_form, "port": "8081", "password": "pw-from-b"})
    location = response.headers["location"]
    assert "conflict=" in location
    saved = load_secrets()["clients"]["main"]
    assert (saved["port"], saved["password"]) == (8080, "pw-from-a")
    page = b.get(location).text
    assert "pw-from-b" not in page
    assert "pw-from-a" not in page
    assert "pw-1" not in page
    again = form_of(page, "client-form-client-0")
    assert again["port"] == "8081"
    assert again["password"] == ""


def test_an_unchanged_messenger_card_does_not_put_back_another_devices_chat(settings_two):
    from tow.store import load_secrets

    a, b = settings_two
    b_form = settings_form(b, "notifier-form-telegram")
    post(a, "/settings/notifier/telegram", {**settings_form(a, "notifier-form-telegram"), "chat_ids": "2002"})
    assert load_secrets()["telegram"]["chat_ids"] == ["2002"]
    post(b, "/settings/notifier/telegram", b_form)
    assert load_secrets()["telegram"]["chat_ids"] == ["2002"]
    assert load_secrets()["telegram"]["token"] == "123456:fake-token"


def test_a_messenger_token_typed_on_both_sides_is_a_conflict_never_kept(settings_two):
    from tow.store import load_secrets

    a, b = settings_two
    b_form = settings_form(b, "notifier-form-telegram")
    post(a, "/settings/notifier/telegram", {**settings_form(a, "notifier-form-telegram"), "token": TOKEN_A})
    location = post(b, "/settings/notifier/telegram", {**b_form, "token": TOKEN_B}).headers["location"]
    assert "conflict=" in location
    assert load_secrets()["telegram"]["token"] == TOKEN_A
    page = b.get(location).text
    assert TOKEN_B not in page
    assert TOKEN_A not in page


def test_a_settings_save_without_a_base_is_refused(settings_two, strict_edit_base):
    a, _b = settings_two
    response = a.post("/settings/interval", data={"interval_min": "30", "flash_ttl_min": "1"})
    assert load_config().get("interval_sec") != 1800
    assert "this form is out of date" in response.text


# --- The other Settings forms (final review 1.33.0 #7) -----------------------------------------


def form_items(html: str, action: str) -> dict:
    """A form's fields as a browser sends them, repeated names as lists (the protection cards)."""
    parser = _Forms()
    parser.feed(html)
    out: dict[str, list[str]] = {}
    for name, value in parser.forms[action]:
        out.setdefault(name, []).append(value)
    return {name: values if name.startswith("provider_") else values[0] for name, values in out.items()}


PROTECTION = {
    "enabled": "1",
    "timeout_sec": "30",
    "min_interval_sec": "1800",
    "max_attempts": "2",
    "provider_id": ["first", "second"],
    "provider_kind": ["flaresolverr", "byparr"],
    "provider_url": ["http://127.0.0.1:8191", "http://127.0.0.1:8192"],
    "provider_order": ["1", "2"],
    "provider_enabled": ["first", "second"],
}


def protection_form(client: TestClient, address: str = "/settings") -> dict:
    return form_items(client.get(address).text, "/settings/protection")


def providers() -> list[str]:
    return [item["id"] for item in load_config()["protection"]["providers"]]


@pytest.fixture
def protection_two(settings_two):
    a, _b = settings_two
    assert post(a, "/settings/protection", PROTECTION).status_code == 303
    assert providers() == ["first", "second"]
    return settings_two


def test_two_tabs_editing_different_protection_fields_both_keep_their_change(protection_two):
    """The second save replaced the whole configuration and lost the first change."""
    a, b = protection_two
    b_form = protection_form(b)
    post(a, "/settings/protection", {**protection_form(a), "timeout_sec": "45"})
    assert load_config()["protection"]["timeout_sec"] == 45
    response = post(b, "/settings/protection", {**b_form, "max_attempts": "3"})
    assert "conflict=" not in response.headers["location"]
    saved = load_config()["protection"]
    assert (saved["timeout_sec"], saved["max_attempts"]) == (45, 3)


def test_a_stale_protection_form_never_restores_a_connection_removed_elsewhere(protection_two):
    a, b = protection_two
    b_form = protection_form(b)
    post(a, "/settings/protection/remove", {"provider_id": "second"})
    assert providers() == ["first"]
    post(b, "/settings/protection", {**b_form, "timeout_sec": "60"})  # connections untouched
    assert providers() == ["first"]
    assert load_config()["protection"]["timeout_sec"] == 60
    # Its connection edited in the stale form: refused, the removal stays.
    urls = list(b_form["provider_url"])
    urls[1] = "http://127.0.0.1:8193"
    response = post(b, "/settings/protection", {**b_form, "provider_url": urls})
    assert "conflict=" in response.headers["location"]
    assert providers() == ["first"]
    assert "Saved now:" in b.get(response.headers["location"]).text


def _with_new_connection(form: dict, kind: str, url: str) -> dict:
    """The form with its blank suggestion (the last card) filled in and switched on."""
    ident = form["provider_id"][-1]
    kinds, urls = list(form["provider_kind"]), list(form["provider_url"])
    kinds[-1], urls[-1] = kind, url
    return {
        **form,
        "provider_kind": kinds,
        "provider_url": urls,
        "provider_enabled": [*form["provider_enabled"], ident],
    }


def test_two_tabs_adding_different_services_both_keep_their_connection(protection_two):
    """1.33.1 A03: every render suggests its own new connection (128 random bits), and two tabs
    that only add one each are both saved, in the order they were saved, without a conflict: each
    list is provably the base followed by additions (a prefix of each has the base's digest)."""
    a, b = protection_two
    a_form, b_form = protection_form(a), protection_form(b)
    a_id, b_id = a_form["provider_id"][-1], b_form["provider_id"][-1]
    assert a_id != b_id  # each render its own suggestion
    assert re.fullmatch(r"new-connection-[0-9a-f]{32}", a_id)
    post(a, "/settings/protection", _with_new_connection(a_form, "flaresolverr", "http://127.0.0.1:8201"))
    response = post(b, "/settings/protection", _with_new_connection(b_form, "byparr", "http://127.0.0.1:8202"))
    assert "conflict=" not in response.headers["location"]
    saved = [(item["id"], item["kind"], item["url"]) for item in load_config()["protection"]["providers"]]
    assert saved == [
        ("first", "flaresolverr", "http://127.0.0.1:8191/v1"),
        ("second", "byparr", "http://127.0.0.1:8192/v1"),
        (a_id, "flaresolverr", "http://127.0.0.1:8201/v1"),
        (b_id, "byparr", "http://127.0.0.1:8202/v1"),
    ]


def test_an_added_connection_with_an_id_saved_meanwhile_is_a_conflict(protection_two):
    """A suffix whose id is already saved (a forged or replayed form) is no addition: the union is
    refused and the conflict stays."""
    a, b = protection_two
    a_form, b_form = protection_form(a), protection_form(b)
    post(a, "/settings/protection", _with_new_connection(a_form, "flaresolverr", "http://127.0.0.1:8201"))
    forged = _with_new_connection(b_form, "byparr", "http://127.0.0.1:8202")
    forged["provider_id"] = [*forged["provider_id"][:-1], a_form["provider_id"][-1]]
    forged["provider_enabled"] = [*forged["provider_enabled"][:-1], a_form["provider_id"][-1]]
    response = post(b, "/settings/protection", forged)
    assert "conflict=" in response.headers["location"]
    assert len(providers()) == 3


def test_a_union_past_the_connection_limit_is_a_conflict(protection_two):
    """At 15 saved connections two tabs adding one each would make 17: the second is refused as
    a conflict, the first stays."""
    a, b = protection_two
    form = protection_form(a)
    for number in range(13):
        form = _with_new_connection(form, "byparr", f"http://127.0.0.1:{8300 + number}")
        assert "conflict=" not in post(a, "/settings/protection", form).headers["location"]
        form = protection_form(a)
    assert len(providers()) == 15
    a_form, b_form = protection_form(a), protection_form(b)
    post(a, "/settings/protection", _with_new_connection(a_form, "flaresolverr", "http://127.0.0.1:8201"))
    assert len(providers()) == 16
    response = post(b, "/settings/protection", _with_new_connection(b_form, "byparr", "http://127.0.0.1:8202"))
    assert "conflict=" in response.headers["location"]
    assert len(providers()) == 16


def test_two_tabs_adding_different_services_never_save_a_hybrid_connection(protection_two):
    """Re-check 1.33.0 #1: after A saved, B's recovery form drew A's saved card with B's address,
    and "mine" saved a connection neither tab configured. B also edited a saved connection here,
    so its list is no mere addition and the save is a conflict: the recovery form brings back B's
    whole collection."""
    a, b = protection_two
    a_form, b_form = protection_form(a), protection_form(b)
    post(a, "/settings/protection", _with_new_connection(a_form, "flaresolverr", "http://127.0.0.1:8201"))
    assert len(providers()) == 3
    b_sent = _with_new_connection(b_form, "byparr", "http://127.0.0.1:8202")
    b_sent["provider_url"] = [b_sent["provider_url"][0], "http://127.0.0.1:8193", *b_sent["provider_url"][2:]]
    location = post(b, "/settings/protection", b_sent).headers["location"]
    assert "conflict=" in location
    again = protection_form(b, location)
    shown = list(zip(again["provider_kind"], again["provider_url"], strict=True))
    assert ("byparr", "http://127.0.0.1:8202") in shown
    assert ("flaresolverr", "http://127.0.0.1:8202") not in shown
    response = post(b, "/settings/protection", {**again, "resolve_providers": "mine"})
    assert "conflict=" not in response.headers["location"]
    saved = [(item["kind"], item["url"]) for item in load_config()["protection"]["providers"]]
    assert saved == [
        ("flaresolverr", "http://127.0.0.1:8191/v1"),
        ("byparr", "http://127.0.0.1:8193/v1"),
        ("byparr", "http://127.0.0.1:8202/v1"),
    ]


def test_a_connection_removed_elsewhere_keeps_its_draft_in_the_recovery_form(protection_two):
    """Re-check 1.33.0 #1: the recovery form drew only the connections saved now, so the draft of
    one removed on another device vanished and "mine" could not bring it back."""
    a, b = protection_two
    b_form = protection_form(b)
    post(a, "/settings/protection/remove", {"provider_id": "second"})
    urls = list(b_form["provider_url"])
    urls[1] = "http://127.0.0.1:8193"
    location = post(b, "/settings/protection", {**b_form, "provider_url": urls}).headers["location"]
    assert "conflict=" in location
    again = protection_form(b, location)
    index = again["provider_id"].index("second")
    assert (again["provider_kind"][index], again["provider_url"][index]) == ("byparr", "http://127.0.0.1:8193")
    assert "second" in again["provider_enabled"]
    post(b, "/settings/protection", {**again, "resolve_providers": "mine"})
    saved = load_config()["protection"]["providers"]
    assert [(item["id"], item["kind"], item["url"]) for item in saved] == [
        ("first", "flaresolverr", "http://127.0.0.1:8191/v1"),
        ("second", "byparr", "http://127.0.0.1:8193/v1"),
    ]


def test_a_protection_conflict_resolved_as_saved_keeps_the_connections_saved_now(protection_two):
    a, b = protection_two
    a_form, b_form = protection_form(a), protection_form(b)
    post(a, "/settings/protection", _with_new_connection(a_form, "flaresolverr", "http://127.0.0.1:8201"))
    before = load_config()["protection"]["providers"]
    b_sent = _with_new_connection(b_form, "byparr", "http://127.0.0.1:8202")
    b_sent["provider_url"] = [b_sent["provider_url"][0], "http://127.0.0.1:8193", *b_sent["provider_url"][2:]]
    response = post(b, "/settings/protection", b_sent)  # an edit of a saved one too: no mere addition
    assert "conflict=" in response.headers["location"]
    again = protection_form(b, response.headers["location"])
    post(b, "/settings/protection", {**again, "resolve_providers": "current"})
    assert load_config()["protection"]["providers"] == before


def test_protection_fields_changed_on_both_sides_are_a_conflict_with_the_draft_kept(protection_two):
    a, b = protection_two
    b_form = protection_form(b)
    post(a, "/settings/protection", {**protection_form(a), "timeout_sec": "45", "max_attempts": "4"})
    location = post(b, "/settings/protection", {**b_form, "timeout_sec": "60"}).headers["location"]
    assert "conflict=" in location
    assert load_config()["protection"]["timeout_sec"] == 45
    again = protection_form(b, location)
    assert (again["timeout_sec"], again["max_attempts"]) == ("60", "4")
    post(b, "/settings/protection", {**again, "resolve_timeout": "mine"})
    saved = load_config()["protection"]
    assert (saved["timeout_sec"], saved["max_attempts"]) == (60, 4)


def test_backup_retention_changed_on_both_sides_is_a_conflict(settings_two):
    a, b = settings_two
    b_form = settings_form(b, "/settings/backup/retention")
    post(a, "/settings/backup/retention", {**settings_form(a, "/settings/backup/retention"), "days": "10"})
    assert load_config()["backup_days"] == 10
    response = post(b, "/settings/backup/retention", {**b_form, "days": "20"})
    assert "conflict=" in response.headers["location"]
    assert load_config()["backup_days"] == 10


def test_a_stale_backup_folder_form_does_not_put_back_its_folder(settings_two, tmp_path):
    a, b = settings_two
    # Outside the data folder (night copies never go there), as tests/test_backup_locations does.
    first, second = tmp_path.parent / f"{tmp_path.name}-a", tmp_path.parent / f"{tmp_path.name}-b"
    first.mkdir()
    second.mkdir()
    b_form = form_items(b.get("/settings").text, "/settings/backup/location")
    a_form = form_items(a.get("/settings").text, "/settings/backup/location")
    post(a, "/settings/backup/location", {**a_form, "path": str(first), "action": "save"})
    key = next(key for key, value in load_config().items() if value == str(first))
    response = post(b, "/settings/backup/location", {**b_form, "path": str(second), "action": "save"})
    assert "conflict=" in response.headers["location"]
    assert load_config()[key] == str(first)
    # "Use the default" from a page that showed another folder is a change of the folder too.
    b_form = form_items(b.get("/settings").text, "/settings/backup/location")  # shows `first`
    post(
        a,
        "/settings/backup/location",
        {**form_items(a.get("/settings").text, "/settings/backup/location"), "path": str(second), "action": "save"},
    )
    assert load_config()[key] == str(second)
    response = post(b, "/settings/backup/location", {**b_form, "action": "default"})
    assert "conflict=" in response.headers["location"]
    assert load_config()[key] == str(second)
    # A page that showed the default folder, unchanged: "Save" keeps the folder saved now.
    post(
        b,
        "/settings/backup/location",
        {
            **form_items(b.get(response.headers["location"]).text, "/settings/backup/location"),
            "resolve_folder": "current",
            "action": "save",
        },
    )
    assert load_config()[key] == str(second)


def test_automatic_backups_saved_on_both_sides_to_one_value_is_no_conflict(settings_two):
    a, b = settings_two
    b_form = settings_form(b, "/settings/backup/automatic")
    flipped = "" if b_form.get("enabled") else "1"
    post(a, "/settings/backup/automatic", {**settings_form(a, "/settings/backup/automatic"), "enabled": flipped})
    response = post(b, "/settings/backup/automatic", {**b_form, "enabled": flipped})
    assert "conflict=" not in response.headers["location"]
    assert "base" in b_form  # the box carries its base too


def test_network_access_is_refused_when_the_access_card_changed_meanwhile(settings_two, monkeypatch):
    """Network access and the password are one card guarded as a whole: a form opened before
    another device changed it saves nothing."""
    from tow.store import load_secrets

    monkeypatch.setattr("tow.web.services.sign_out_everywhere", lambda *a, **k: None)
    a, b = settings_two
    b_access = settings_form(b, "/settings/access")
    a_access = settings_form(a, "/settings/access")
    first = {"allow_lan": "1", "lan_password": "first-pass-1", "lan_password2": "first-pass-1"}
    post(a, "/settings/access", {**a_access, **first})
    assert load_config()["allow_lan"] is True
    record = load_secrets()["lan_auth"]
    second = {"allow_lan": "1", "lan_password": "second-pass-2", "lan_password2": "second-pass-2"}
    response = post(b, "/settings/access", {**b_access, **second})
    assert load_secrets()["lan_auth"] == record  # B's password is not written over A's
    assert "flash=" in response.headers["location"]
    b_password = settings_form(b, "/settings/password")
    fourth = {"lan_password": "fourth-pass-4", "lan_password2": "fourth-pass-4"}
    post(a, "/settings/password", {**settings_form(a, "/settings/password"), **fourth})
    assert load_secrets()["lan_auth"] != record
    record = load_secrets()["lan_auth"]
    post(b, "/settings/password", {**b_password, "lan_password": "third-pass-3", "lan_password2": "third-pass-3"})
    assert load_secrets()["lan_auth"] == record
    b_box = settings_form(b, "/settings/access")
    post(a, "/settings/access", {**settings_form(a, "/settings/access"), "allow_lan": ""})
    assert load_config()["allow_lan"] is False
    post(b, "/settings/access", {**b_box, "allow_lan": "1"})  # B's page still showed it on
    assert load_config()["allow_lan"] is False


def test_a_stale_autostart_box_does_not_put_back_without_signing_in(settings_two, monkeypatch):
    status = {"autostart": True, "without_login": True, "supports_without_login": True}

    def set_autostart(enabled, *, without_login=False):
        status.update(autostart=enabled, without_login=without_login)
        return {"ok": True}

    monkeypatch.setattr("tow.web.services.set_autostart", set_autostart)
    monkeypatch.setattr("tow.web.services.service_status", lambda: dict(status))
    a, b = settings_two
    b_form = settings_form(b, "/settings/service/autostart")
    a_form = settings_form(a, "/settings/service/autostart")
    post(a, "/settings/service/autostart", {**a_form, "enabled": "1", "without_login": "0"})  # A: sign-in needed
    assert status["without_login"] is False
    post(b, "/settings/service/autostart", {**b_form, "enabled": ""})  # B (stale): autostart off
    assert status["autostart"] is False
    post(b, "/settings/service/autostart", {**b_form, "enabled": "1"})  # and on again, same page
    assert status["without_login"] is False  # the hidden stale "1" is not put back


# --- the check stops the previous version itself (tow.check.replace) ---------------------------


def test_the_checks_setting_saves_renders_and_undoes(settings_two):
    from tow.config import config_path
    from tow.log import read_events

    a, _b = settings_two
    form = settings_form(a, "/settings/interval")
    assert "replace_previous_revision" not in form  # off by default: the box is clear
    page = a.get("/settings").text
    assert "Stop the previous version of a topic by itself" in page

    response = post(a, "/settings/interval", {**form, "replace_previous_revision": "1"})

    assert "conflict=" not in response.headers["location"]
    assert load_config()["replace_previous_revision"] is True
    assert "replace_previous_revision: true" in config_path().read_text(encoding="utf-8")
    assert settings_form(a, "/settings/interval")["replace_previous_revision"] == "1"  # ticked now
    assert [e["enabled"] for e in read_events(limit=50) if e.get("kind") == "settings_replace_previous"] == [True]
    post(a, "/undo", {})
    assert load_config()["replace_previous_revision"] is False


def test_another_devices_checks_setting_survives_a_save_of_the_interval(settings_two):
    a, b = settings_two
    b_form = settings_form(b, "/settings/interval")
    post(a, "/settings/interval", {**settings_form(a, "/settings/interval"), "replace_previous_revision": "1"})
    response = post(b, "/settings/interval", {**b_form, "interval_min": "30"})  # B's box still clear
    assert "conflict=" not in response.headers["location"]
    assert (load_config()["interval_sec"], load_config()["replace_previous_revision"]) == (1800, True)


def test_a_topics_own_choice_saves_renders_and_goes_back_to_settings(two):
    a, _b = two
    html = a.get("/topics/t1/edit-panel").text
    select = re.search(r'<select id="edit-replace-previous-t1" class="(?P<cls>[^"]+)" name="replace_previous">', html)
    assert select is not None
    assert select["cls"] == "w-m"  # the standard size of every select
    assert "As in Settings (off)" in html
    form = panel(a)
    assert form["replace_previous"] == ""

    save(a, {**form, "replace_previous": "off"})
    assert topic()["replace_previous"] == "off"
    assert panel(a)["replace_previous"] == "off"
    save(a, {**panel(a), "replace_previous": "on"})
    assert topic()["replace_previous"] == "on"
    save(a, {**panel(a), "replace_previous": ""})
    assert "replace_previous" not in topic()  # as Settings says again


def test_a_topics_choice_outside_the_list_is_refused(two):
    a, _b = two
    from helpers import flash_of

    response = save(a, {**panel(a), "replace_previous": "always", "title": "Renamed"})
    assert response.status_code == 303
    assert "Choose from the list whether the previous version is stopped by itself" in flash_of(
        response.headers["location"]
    )
    assert "replace_previous" not in topic()
    assert topic()["title"] == "Show A"  # nothing of the refused save is written


def test_another_devices_topic_choice_survives_a_rename(two):
    a, b = two
    b_form = panel(b)
    save(a, {**panel(a), "replace_previous": "on"})
    response = save(b, {**b_form, "title": "Renamed by B"})
    assert "conflict=" not in response.headers["location"]
    assert (topic()["title"], topic()["replace_previous"]) == ("Renamed by B", "on")


def test_a_topics_choice_changed_on_both_sides_is_a_conflict_that_names_both(two):
    a, b = two
    b_form = panel(b)
    save(a, {**panel(a), "replace_previous": "on"})
    location = save(b, {**b_form, "replace_previous": "off"}).headers["location"]
    assert "conflict=" in location
    page = b.get(location).text
    assert "Saved now: Yes" in page
    assert "Yours: No" in page
    assert topic()["replace_previous"] == "on"
