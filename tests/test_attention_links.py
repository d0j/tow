"""Every "Needs attention" item is a link to the problem itself: its page opens what shows the
problem in full, marks the spot (?spot=, or #anchor when there is nothing to focus) and focuses
the control that fixes it (?fix=, also without a script: autofocus). One test per kind, and one
that no kind is left out."""

from __future__ import annotations

import html
import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from tow import issues
from tow.i18n import t
from tow.log import log_event
from tow.store import save_secrets, save_state
from tow.web import app, services

NNM = "https://nnmclub.to/forum/viewtopic.php?t="


def _topic(tid: str, n: int, **extra) -> dict:
    return {"id": tid, "title": f"Show {tid}", "url": NNM + str(n), **extra}


def _auth(tid: str, n: int) -> dict:
    return _topic(tid, n, last_error_class="tracker_auth", last_error="The site asks to sign in again.")


def _entries(client: TestClient) -> dict[str, dict]:
    from tow.config import load_config
    from tow.store import load_state
    from tow.web.views import attention_view

    view = attention_view(load_state(), load_config(), local=True)
    return {entry["kind"]: entry for entry in view["entries"]}


def _tag(page: str, ref: str) -> str:
    match = re.search(rf'<(a|button)\b[^>]*data-fix="{re.escape(ref)}"[^>]*>(.*?)</\1>', page, re.DOTALL)
    assert match, f"no control data-fix={ref}"
    return match.group(0)


def _land(client: TestClient, entry: dict, *, anchor: str, fix: str, label: str) -> tuple[str, str]:
    """Follow the item's link; the page shows the spot and focuses the fix (when it has one)."""
    href = entry["href"]
    split = urlsplit(href)
    query = parse_qs(split.query)
    assert query.get("fix", [""]) == [fix]
    if fix:
        # No #fragment: a browser ignores autofocus on such an address (Chromium), and without a
        # script that focus is all there is. app.js finds the spot by ?spot=.
        assert split.fragment == ""
        assert query["spot"] == [anchor]
    else:  # nothing to focus: the browser scrolls to the spot and the CSS lights it up
        assert split.fragment == anchor
        assert "spot" not in query
    assert entry["fix"] == fix
    assert entry["fix_label"] == label
    response = client.get(f"{split.path}?{split.query}" if split.query else split.path)
    assert response.status_code == 200
    page = response.text
    assert f'id="{anchor}"' in page, f"no #{anchor} on {split.path}"
    tag = ""
    if fix:
        tag = _tag(page, fix)
        assert " autofocus" in tag.split(">", 1)[0]  # focused without a script too
        assert " disabled" not in tag.split(">", 1)[0]
        words = html.unescape(re.sub(r"<[^>]+>", "", tag)).strip()
        if not words:  # an icon button is named by its aria-label
            words = html.unescape(re.search(r'aria-label="([^"]*)"', tag).group(1))
        assert words == label
        assert page.count(" autofocus") == 1
    return page, tag


@pytest.fixture
def client():
    save_secrets({"qbittorrent": {"host": "127.0.0.1", "port": 8080, "username": "admin", "password": "pw"}})
    return TestClient(app)


def _home_state(*topics, **health) -> None:
    save_state({"topics": list(topics), "mirrors": {}, "health": {"auto_at_ts": int(time.time()), **health}})


def test_one_topic_asking_for_a_sign_in_opens_its_row_with_the_sign_in_focused(client):
    _home_state(_auth("a", 1), _topic("z", 9))
    entry = _entries(client)["site_needs_login"]
    assert entry["text"] == "Войти на NNM-Club (раздач: 1)"
    assert entry["href"] == "/?open=a&fix=sign_in%3Aa&spot=row-a"

    page, tag = _land(client, entry, anchor="row-a", fix="sign_in:a", label="Войти на сайт")
    row = page.split('id="row-a"', 1)[1].split('class="row-wrap', 1)[0]
    assert re.search(r'<details class="row-edit[^"]*" open>', row)  # open, its panel drawn in
    assert "data-edit-loaded" in row
    assert "The site asks to sign in again." in row  # the full error
    assert 'href="/?credential_topic=a"' in tag
    other = page.split('id="row-z"', 1)[1]
    assert re.search(r'<details class="row-edit[^"]*">', other)  # the others stay closed
    assert 'data-edit-src="/topics/z/edit-panel"' in other


def test_several_topics_show_the_sites_problems_with_the_first_open(client):
    _home_state(_auth("a", 1), _auth("b", 2), _topic("z", 9))
    entry = _entries(client)["site_needs_login"]
    assert entry["text"] == "Войти на NNM-Club (раздач: 2)"
    assert entry["href"] == "/?f=problem&t=nnmclub&open=a&fix=sign_in%3Aa&spot=row-a"

    page, _ = _land(client, entry, anchor="row-a", fix="sign_in:a", label="Войти на сайт")
    assert 'data-tracker="nnmclub"' in page.split('id="row-a"', 1)[1].split(">", 1)[0]
    assert re.search(r'<details class="row-edit[^"]*">', page.split('id="row-b"', 1)[1])  # the next one closed


def test_a_site_without_a_sign_in_opens_its_row_on_sites_at_the_user_name(client):
    from tow.config import load_config, save_config

    cfg = load_config()
    cfg["trackers"] = {
        "fakeone": {
            "title": "Site One",
            "url_regex": r"^https://one\.example/t/(\d+)",
            "fetch_hosts": ["https://one.example"],
        },
        "faketwo": {"url_regex": r"^https://two\.example/t/(\d+)", "fetch_hosts": ["https://two.example"]},
    }
    save_config(cfg)
    asks = {"last_error_class": "tracker_auth", "last_error": "The site asks to sign in again."}
    _home_state(
        {"id": "a", "url": "https://one.example/t/1", **asks},
        {"id": "b", "url": "https://one.example/t/2", **asks},
        {"id": "z", "url": "https://two.example/t/9"},
    )
    entry = _entries(client)["site_needs_login"]
    # Not "Sign in to …": the site has no sign-in TOW could make.
    assert entry["text"] == "Сайт Site One просит вход (раздач: 2)"
    assert entry["fix"] == "site_login:fakeone"
    assert entry["fix_label"] == "Логин"
    split = urlsplit(entry["href"])
    assert split.path == "/sites"
    assert parse_qs(split.query) == {"open": ["fakeone"], "fix": ["site_login:fakeone"], "spot": ["site-row-fakeone"]}
    assert split.fragment == ""

    page = client.get(f"{split.path}?{split.query}").text
    row = page.split('id="site-row-fakeone"', 1)[1].split('id="site-row-faketwo"', 1)[0]
    assert re.search(r'<details class="row-edit sites" open>', row)  # open without a script
    assert html.escape(entry["text"], quote=False) in row  # the row says why it is open
    field = re.search(r'<input id="site-user-fakeone"[^>]*>', row).group(0)
    assert 'data-fix="site_login:fakeone"' in field
    assert " autofocus" in field
    assert page.count(" autofocus") == 1
    other = page.split('id="site-row-faketwo"', 1)[1]
    assert re.search(r'<details class="row-edit sites">', other)  # the other site stays closed
    assert "data-issue-id" not in other


KZ = "https://kinozal.guru/details.php?id="


def _signed_in_check(monkeypatch, *, ok: bool) -> None:
    """The check a sign-in runs: it checks the one topic, which passes (``ok``) or is refused."""
    from tow.store import load_state

    def check(*, ids, **_kw):
        state = load_state()
        for topic in state["topics"]:
            if topic["id"] in ids and ok:
                for key in ("last_error", "last_error_class"):
                    topic.pop(key)
                topic.update(last_ok=True, last_check="2099-01-01T00:00:00+00:00")
        save_state(state)
        row = {"id": ids[0], "ok": ok} if ok else {"id": ids[0], "ok": False, "error": "tracker auth required"}
        return {"results": [row]}

    monkeypatch.setattr(services, "run_check", check)


def _sign_in(client: TestClient, topic: str) -> None:
    """Home's sign-in prompt of ``topic``: a login and password, then the check of that topic."""
    response = client.post(
        f"/topics/{topic}/tracker-login",
        data={"username": "u", "password": "p"},
        headers={"Origin": "http://127.0.0.1"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_after_a_sign_in_the_sites_other_topics_wait_for_their_next_check(client, monkeypatch):
    refused = {"last_error_class": "tracker_auth", "last_error": "asks", "last_check": "2026-01-01T00:00:00+00:00"}
    _home_state({"id": "a", "url": KZ + "1", **refused}, {"id": "b", "url": KZ + "2", **refused})
    assert _entries(client)["site_needs_login"]["text"] == "Войти на Kinozal (раздач: 2)"
    _signed_in_check(monkeypatch, ok=True)

    _sign_in(client, "a")

    entry = _entries(client)["site_needs_login"]
    assert entry["text"] == "Вход на Kinozal выполнен: остальные раздачи проверятся при следующей проверке (раздач: 1)"
    assert entry["severity"] == "warn"
    assert entry["href"] == "/?open=b&fix=check_all&spot=row-b"
    _land(client, entry, anchor="row-b", fix="check_all", label=t("base.check_all.title", "ru"))
    from tow.store import load_state

    other = next(topic for topic in load_state()["topics"] if topic["id"] == "b")
    assert other["last_error_class"] == "tracker_auth"  # nothing cleared without a check
    assert other["last_check"] == refused["last_check"]  # and nothing checked by itself


def test_a_sign_in_the_check_refused_still_asks_to_sign_in(client, monkeypatch):
    refused = {"last_error_class": "tracker_auth", "last_error": "asks", "last_check": "2026-01-01T00:00:00+00:00"}
    _home_state({"id": "a", "url": KZ + "1", **refused}, {"id": "b", "url": KZ + "2", **refused})
    _signed_in_check(monkeypatch, ok=False)

    _sign_in(client, "a")

    assert _entries(client)["site_needs_login"]["text"] == "Войти на Kinozal (раздач: 2)"


def test_the_client_down_opens_its_card_with_the_status_and_check(client):
    _home_state(_topic("z", 9), qbit_ok=False)
    entry = _entries(client)["client_down"]
    assert entry["href"] == "/settings?open=clients&fix=check_client%3Adefault&spot=client-default"

    page, _ = _land(client, entry, anchor="client-default", fix="check_client:default", label="Проверить")
    assert re.search(r'id="acc-clients" open', page)  # the section open without a script
    card = page.split('id="client-default"', 1)[1].split("</article>", 1)[0]
    assert "Не отвечает: новые версии не добавляются." in card


def test_client_down_reads_the_current_client_factory_on_each_call(client, monkeypatch):
    from tow.clients import factory

    _home_state(_topic("z", 9), qbit_ok=False)
    # Load the issue module before changing its dependency; a captured function
    # would ignore the change, or retain it after discovery during another test.
    assert _entries(client)["client_down"]["fix"] == "check_client:default"
    with monkeypatch.context() as change:
        change.setattr(factory, "default_client_id", lambda cfg: "main")
        assert _entries(client)["client_down"]["fix"] == "check_client:main"
    assert _entries(client)["client_down"]["fix"] == "check_client:default"


def test_a_down_clients_topics_fold_by_the_current_client_factory(client, monkeypatch):
    # topic_error read the main client through a function captured at import, issues.clients
    # through the module: after a test changed the main client while the kinds were first loaded,
    # a dead client's failed topic was listed beside "client not answering" again.
    from tow.clients import factory

    topic = {**_topic("z", 9), "last_error": "client refused", "last_error_class": "qbit"}
    topic.pop("client_id", None)
    _home_state(topic, qbit_ok=False)
    with monkeypatch.context() as change:
        change.setattr(factory, "default_client_id", lambda cfg: "main")
        assert set(_entries(client)) == {"client_down"}
    assert set(_entries(client)) == {"client_down"}


def test_late_checks_open_the_service_card_with_restart(client):
    _home_state(_topic("z", 9), auto_at_ts=int(time.time()) - 10 * 24 * 3600)
    entry = _entries(client)["check_stale"]
    assert entry["href"] == "/settings?open=service&fix=restart_service&spot=acc-service"

    page, _ = _land(client, entry, anchor="acc-service", fix="restart_service", label="Перезапустить")
    assert re.search(r'id="acc-service" open', page)
    # The card says what is wrong: the service card alone does not.
    card = page.split('id="acc-service"', 1)[1].split("</details>", 1)[0]
    assert html.escape(entry["text"], quote=False) in card
    assert card.count('data-issue-id="check_stale"') == 1  # one note, named by its issue


def test_a_check_blocked_by_the_master_key_lands_on_its_explanation_with_the_command(client):
    _home_state(_topic("z", 9), check_ok=False, check_error="secrets_migration_required")
    entry = _entries(client)["check_blocked"]
    assert entry["href"] == "/settings?fix=copy_keys_status&spot=secret-store"

    page, tag = _land(client, entry, anchor="secret-store", fix="copy_keys_status", label="Скопировать команду")
    assert re.search(r'data-copy-text="[^"]* keys status"', tag)
    assert 'id="copy-command-status"' not in page  # its outcome is a message
    note = page.split('id="secret-store"', 1)[1].split("</div>", 1)[0]
    assert "keys status" in note


def test_a_failed_check_lands_on_its_error_in_the_history_with_check_all(client):
    _home_state(_topic("z", 9), check_ok=False, check_error="error")
    log_event("check_blocked", reason="error", how="auto")
    log_event("check_blocked", reason="error", how="auto")
    entry = _entries(client)["check_blocked"]
    assert entry["href"] == "/history?group=errors&fix=check_all&spot=latest-check_blocked"

    page, _ = _land(
        client, entry, anchor="latest-check_blocked", fix="check_all", label=t("base.check_all.title", "ru")
    )
    assert page.count('id="latest-check_blocked"') == 1  # the newest one
    # The line says why, with the banner's verb (this event, as TOW 1.28 wrote it, has only a
    # class: the line says where the details are).
    line = html.unescape(re.sub(r"<[^>]+>", "", page.split('id="latest-check_blocked"', 1)[1].split("</div>", 1)[0]))
    assert "Проверка не завершилась · авто · причина не записана" in line
    assert "не завершилась" in entry["text"]


def test_a_failed_check_says_its_error_on_the_landing(client):
    from tow.check import record_check_failure

    _home_state(_topic("z", 9))
    record_check_failure(OSError(28, "No space left on device"))
    entry = _entries(client)["check_blocked"]
    page = client.get(entry["href"]).text
    line = html.unescape(re.sub(r"<[^>]+>", "", page.split('id="latest-check_blocked"', 1)[1].split("</div>", 1)[0]))
    assert "Проверка не завершилась · авто" in line
    assert "No space left on device" in line


def test_a_rebuilt_history_lands_on_its_note_with_nothing_to_focus(client):
    _home_state(_topic("z", 9), history_rebuilt_at=int(time.time()))
    assert "history_rebuilt" not in _entries(client)
    page = client.get("/history#issue-history_rebuilt").text
    note = page.split('id="issue-history_rebuilt"', 1)[1].split("</p>", 1)[0]
    assert "download_history.json.corrupt" in note
    assert " autofocus" not in page


def test_a_trimmed_history_is_said_on_home_and_lands_on_its_note_in_the_history(client):
    _home_state(_topic("z", 9), history_trimmed_at=int(time.time()))
    assert "history_trimmed" in _entries(client)  # unlike a rebuilt one: it goes on while there are so many files
    page = client.get("/history#issue-history_trimmed").text
    note = page.split('id="issue-history_trimmed"', 1)[1].split("</p>", 1)[0]
    assert "точку восстановления" in note
    assert " autofocus" not in page


def test_a_topic_error_opens_its_row_with_the_current_problem(client):
    _home_state(_topic("a", 1, last_error="Synthetic connection refused", last_error_class="tracker"))
    entry = _entries(client)["topic_error"]
    assert entry["text"] == "Show a — Synthetic connection refused"
    page, _ = _land(client, entry, anchor="row-a", fix="", label="")
    assert "Synthetic connection refused" in page
    assert re.search(r'<details class="row-edit[^\"]*" open>', page.split('id="row-a"', 1)[1])


def test_a_previous_revision_still_running_lands_on_its_stop_and_add(client):
    # 26.10.2026: the entry only told the owner to stop the torrent in the client; the topic's
    # panel has the one click that does it (POST /topics/<id>/replace-revision).
    blocked = _topic(
        "a",
        1,
        hash="AB" * 20,
        last_error="the previous version is still active",
        last_error_class="qbit",
        last_error_code="check.previous_revision_active",
        last_error_params={"file": "Show.S01E01.mkv", "hash": "CD" * 20},
    )
    _home_state(blocked)
    entry = _entries(client)["topic_error"]
    page, _ = _land(client, entry, anchor="row-a", fix="replace_revision:a", label=t("home.topic.replace"))
    assert "/topics/a/replace-revision" in page


def test_a_lost_data_folder_lands_on_the_night_copies_with_the_newest_restore(client, monkeypatch, tmp_path_factory):
    from tow.config import load_config, save_config
    from tow.snapshots import create_snapshot

    cfg = load_config()
    cfg["backup_dir"] = str(tmp_path_factory.mktemp("other-drive") / "TOW-backup")  # outside the data folder
    save_config(cfg)
    _home_state()
    create_snapshot()
    monkeypatch.setattr(services, "data_lost", lambda: {"at": 1, "folder": "D:\\TOW\\data"})
    entry = _entries(client)["data_lost"]
    assert entry["href"] == "/settings?open=transfer&fix=restore_night&spot=backup-night"

    page, _ = _land(client, entry, anchor="backup-night", fix="restore_night", label="Восстановить")
    assert re.search(r'id="acc-transfer" open', page)
    assert '<details class="backup-inventory" open>' in page  # the copies unfolded around the button
    card = page.split('id="backup-night"', 1)[1].split("</article>", 1)[0]
    assert html.escape(entry["text"], quote=False) in card  # what was lost, in the card


# Kind -> the test above that follows its link: a new kind needs one.
def test_a_folder_off_its_disk_lands_on_the_volumes_page_with_its_accept(client, tmp_path):
    folder = tmp_path / "media"
    _home_state(_topic("a", 1, volume_unseen={"folder": str(folder), "at": int(time.time())}))
    entry = _entries(client)["volume_missing"]
    assert entry["text"] == f"Папка раздачи «Show a» на другом диске или диск не подключён ({folder})"
    page, _ = _land(
        client, entry, anchor="volume-topic-a", fix="accept_volume:topic:a", label="Это тот же диск — принять"
    )
    assert str(folder) in page


LANDINGS = {
    "site_needs_login": test_one_topic_asking_for_a_sign_in_opens_its_row_with_the_sign_in_focused,
    "client_down": test_the_client_down_opens_its_card_with_the_status_and_check,
    "check_stale": test_late_checks_open_the_service_card_with_restart,
    "check_blocked": test_a_failed_check_lands_on_its_error_in_the_history_with_check_all,
    "history_rebuilt": test_a_rebuilt_history_lands_on_its_note_with_nothing_to_focus,
    "history_trimmed": test_a_trimmed_history_is_said_on_home_and_lands_on_its_note_in_the_history,
    "data_lost": test_a_lost_data_folder_lands_on_the_night_copies_with_the_newest_restore,
    "topic_error": test_a_topic_error_opens_its_row_with_the_current_problem,
    "volume_missing": test_a_folder_off_its_disk_lands_on_the_volumes_page_with_its_accept,
}


def test_every_kind_has_a_landing_test():
    assert set(LANDINGS) == {kind.name for kind in issues.known_kinds()}


def test_a_card_says_nothing_of_a_problem_that_is_gone(client):
    _home_state(_topic("z", 9))
    assert "data-issue-id" not in client.get("/settings").text


def test_the_links_open_nothing_else_without_a_fix(client):
    """A page opened without ?fix= focuses nothing by itself."""
    _home_state(_auth("a", 1))
    assert " autofocus" not in client.get("/").text
    assert " autofocus" not in client.get("/settings").text
    assert " autofocus" not in client.get("/sites").text
