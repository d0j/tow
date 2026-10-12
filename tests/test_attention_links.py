"""Every "Needs attention" item is a link to the problem itself: its page opens what shows the
problem in full, marks the spot (#anchor) and focuses the control that fixes it (?fix=, also
without a script: autofocus). One test per kind, and one that no kind is left out."""

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

    view = attention_view(load_state(), load_config())
    return {entry["kind"]: entry for entry in view["entries"]}


def _tag(page: str, ref: str) -> str:
    match = re.search(rf'<(a|button)\b[^>]*data-fix="{re.escape(ref)}"[^>]*>(.*?)</\1>', page, re.DOTALL)
    assert match, f"no control data-fix={ref}"
    return match.group(0)


def _land(client: TestClient, entry: dict, *, anchor: str, fix: str, label: str) -> tuple[str, str]:
    """Follow the item's link; the page shows the spot and focuses the fix (when it has one)."""
    href = entry["href"]
    split = urlsplit(href)
    assert split.fragment == anchor
    assert parse_qs(split.query).get("fix", [""]) == [fix]
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
    assert entry["href"] == "/?open=a&fix=sign_in%3Aa#row-a"

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
    assert entry["href"] == "/?f=problem&t=nnmclub&open=a&fix=sign_in%3Aa#row-a"

    page, _ = _land(client, entry, anchor="row-a", fix="sign_in:a", label="Войти на сайт")
    assert 'data-tracker="nnmclub"' in page.split('id="row-a"', 1)[1].split(">", 1)[0]
    assert re.search(r'<details class="row-edit[^"]*">', page.split('id="row-b"', 1)[1])  # the next one closed


def test_the_client_down_opens_its_card_with_the_status_and_check(client):
    _home_state(_topic("z", 9), qbit_ok=False)
    entry = _entries(client)["client_down"]
    assert entry["href"] == "/settings?open=clients&fix=check_client%3Adefault#client-default"

    page, _ = _land(client, entry, anchor="client-default", fix="check_client:default", label="Проверить")
    assert re.search(r'id="acc-clients" open', page)  # the section open without a script
    card = page.split('id="client-default"', 1)[1].split("</article>", 1)[0]
    assert "Не отвечает: новые версии не добавляются." in card


def test_late_checks_open_the_service_card_with_restart(client):
    _home_state(_topic("z", 9), auto_at_ts=int(time.time()) - 10 * 24 * 3600)
    entry = _entries(client)["check_stale"]
    assert entry["href"] == "/settings?open=service&fix=restart_service#acc-service"

    page, _ = _land(client, entry, anchor="acc-service", fix="restart_service", label="Перезапустить")
    assert re.search(r'id="acc-service" open', page)


def test_a_check_blocked_by_the_master_key_lands_on_its_explanation_with_the_command(client):
    _home_state(_topic("z", 9), check_ok=False, check_error="secrets_migration_required")
    entry = _entries(client)["check_blocked"]
    assert entry["href"] == "/settings?fix=copy_keys_status#secret-store"

    page, tag = _land(client, entry, anchor="secret-store", fix="copy_keys_status", label="Скопировать команду")
    assert re.search(r'data-copy-text="[^"]* keys status"', tag)
    assert 'id="copy-command-status"' in page
    note = page.split('id="secret-store"', 1)[1].split("</div>", 1)[0]
    assert "keys status" in note


def test_a_failed_check_lands_on_its_error_in_the_history_with_check_all(client):
    _home_state(_topic("z", 9), check_ok=False, check_error="error")
    log_event("check_blocked", reason="error", how="auto")
    log_event("check_blocked", reason="error", how="auto")
    entry = _entries(client)["check_blocked"]
    assert entry["href"] == "/history?group=errors&fix=check_all#latest-check_blocked"

    page, _ = _land(
        client, entry, anchor="latest-check_blocked", fix="check_all", label=t("base.check_all.title", "ru")
    )
    assert page.count('id="latest-check_blocked"') == 1  # the newest one


def test_a_rebuilt_history_lands_on_its_note_with_nothing_to_focus(client):
    _home_state(_topic("z", 9), history_rebuilt_at=int(time.time()))
    entry = _entries(client)["history_rebuilt"]
    assert entry["href"] == "/history#issue-history_rebuilt"

    page, _ = _land(client, entry, anchor="issue-history_rebuilt", fix="", label="")
    note = page.split('id="issue-history_rebuilt"', 1)[1].split("</p>", 1)[0]
    assert "download_history.json.corrupt" in note
    assert " autofocus" not in page


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
    assert entry["href"] == "/settings?open=transfer&fix=restore_night#backup-night"

    page, _ = _land(client, entry, anchor="backup-night", fix="restore_night", label="Восстановить")
    assert re.search(r'id="acc-transfer" open', page)
    assert '<details class="backup-inventory" open>' in page  # the copies unfolded around the button


# Kind -> the test above that follows its link: a new kind needs one.
LANDINGS = {
    "site_needs_login": test_one_topic_asking_for_a_sign_in_opens_its_row_with_the_sign_in_focused,
    "client_down": test_the_client_down_opens_its_card_with_the_status_and_check,
    "check_stale": test_late_checks_open_the_service_card_with_restart,
    "check_blocked": test_a_failed_check_lands_on_its_error_in_the_history_with_check_all,
    "history_rebuilt": test_a_rebuilt_history_lands_on_its_note_with_nothing_to_focus,
    "data_lost": test_a_lost_data_folder_lands_on_the_night_copies_with_the_newest_restore,
}


def test_every_kind_has_a_landing_test():
    assert set(LANDINGS) == {kind.name for kind in issues.known_kinds()}


def test_the_links_open_nothing_else_without_a_fix(client):
    """A page opened without ?fix= focuses nothing by itself."""
    _home_state(_auth("a", 1))
    assert " autofocus" not in client.get("/").text
    assert " autofocus" not in client.get("/settings").text
