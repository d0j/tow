"""Current problems use one native header disclosure, never a page-sized banner."""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tow.config import load_config, save_config
from tow.store import load_state, save_state
from tow.web import app
from tow.web.templating import _static_asset_version
from tow.web.views import flash_location

STATIC = Path(__file__).resolve().parents[1] / "src" / "tow" / "static"
NNM = "https://nnmclub.to/forum/viewtopic.php?t="


def _state(*, client_down: bool = True, signs_in: int = 0) -> dict:
    topics = [
        {
            "id": f"t{n}",
            "title": f"Show {n}",
            "url": NNM + str(n),
            "last_error_class": "tracker_auth",
            "last_error": "x",
        }
        for n in range(1, signs_in + 1)
    ]
    return {
        "topics": topics,
        "mirrors": {},
        "health": {
            "auto_at_ts": int(time.time()),
            "qbit_ok": not client_down,
            "history_rebuilt_at": int(time.time()),
        },
    }


def _home(state: dict, language: str = "ru", path: str = "/") -> BeautifulSoup:
    cfg = load_config()
    cfg["language"] = language
    save_config(cfg)
    save_state(state)
    response = TestClient(app).get(path)
    assert response.status_code == 200
    return BeautifulSoup(response.text, "html.parser")


def _fingerprint(page: BeautifulSoup) -> str:
    return page.select_one("[data-attention]")["data-fingerprint"]


def test_only_a_triangle_and_count_in_the_header_default_closed():
    page = _home(_state(signs_in=2))
    menu = page.select_one("header [data-attention]")
    assert "hidden" not in menu.attrs
    assert "open" not in menu.attrs
    assert "bad" in menu["class"]
    summary = menu.select_one("summary")
    assert summary["aria-label"] == "Текущие проблемы: 2"
    assert summary.select_one('use[href="#i-alert"]')
    assert summary.get_text(strip=True) == "2"
    assert summary.select_one("[data-attention-count]")["aria-hidden"] == "true"
    assert len(menu.select("[data-issue-id]")) == 2
    assert not page.select("main [data-attention], [data-attention-fold], [data-attention-unfold], [data-flash-fold]")


@pytest.mark.parametrize("path", ["/", "/sites", "/history", "/settings", "/settings/help"])
def test_indicator_available_on_all_pages_without_duplicate_ids(path):
    page = _home(_state(), "en", path)
    assert page.select_one("header [data-attention-toggle]")["aria-label"] == "Current problems: 1"
    identities = [element["id"] for element in page.select("[id]")]
    assert len(identities) == len(set(identities))


def test_tracker_failure_is_amber_and_escaped():
    state = _state(client_down=False)
    state["topics"] = [
        {"id": "a", "title": "<Show A>", "url": NNM + "1", "last_error": "<timeout>", "last_error_class": "tracker"}
    ]
    menu = _home(state).select_one("[data-attention]")
    assert "warn" in menu["class"]
    assert menu.select_one("[data-attention-count]").get_text() == "1"
    assert "<Show A> — <timeout>" in menu.get_text()
    assert "&lt;timeout&gt;" in str(menu)
    assert menu.select_one("a")["href"] == "/?open=a#row-a"


def test_fingerprint_is_stable_across_languages_and_changes_with_observations():
    one = _fingerprint(_home(_state(signs_in=1)))
    assert re.fullmatch(r"[0-9a-f]{16}", one)
    assert _fingerprint(_home(_state(signs_in=1))) == one
    assert _fingerprint(_home(_state(signs_in=1), "en")) == one
    assert _fingerprint(_home(_state(signs_in=2))) != one
    assert _fingerprint(_home(_state(client_down=False, signs_in=1))) != one


def test_past_history_only_leaves_no_visible_indicator():
    page = _home(_state(client_down=False))
    menu = page.select_one("[data-attention]")
    assert "hidden" in menu.attrs
    assert _fingerprint(page) == ""
    assert not menu.select("[data-issue-id]")


def test_native_disclosure_and_hidden_rule_work_without_scripts():
    page = _home(_state())
    assert page.select_one("details > summary[data-attention-toggle]")
    assert not page.select_one("[data-attention]").has_attr("open")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert re.search(r"\.attention-menu\[hidden\]\s*\{\s*display:\s*none", css)
    assert re.search(r"\.attention-panel\s*\{[^}]*position:\s*absolute", css)


def test_health_snapshot_recovery_and_recurrence_do_not_mutate_state():
    state = _state(client_down=False)
    state["topics"] = [
        {"id": "a", "title": "Show A", "url": NNM + "1", "last_error": "timeout", "last_error_class": "tracker"}
    ]
    save_state(state)
    client = TestClient(app)
    before = load_state()
    first = client.get("/health.json").json()["attention"]
    assert first["count"] == 1
    assert load_state() == before
    before["topics"][0].update(last_error=None, last_ok=True)
    save_state(before)
    recovered = client.get("/health.json").json()["attention"]
    assert (recovered["count"], recovered["fingerprint"], recovered["entries"]) == (0, "", [])
    before["topics"][0].update(last_error="timeout", last_ok=False)
    save_state(before)
    assert client.get("/health.json").json()["attention"]["count"] == 1


# The result of a click is a message, "Needs attention" what is still wrong: a failed check is
# shown as a message also while the triangle lists its topic (it used to be folded into a hidden
# text only a screen reader read - sighted owners saw nothing confirm the click).
@pytest.mark.parametrize("kind", ["warn", "err"])
def test_a_failed_check_is_a_visible_message_beside_the_triangle(kind):
    url = flash_location("/", "synthetic check result", kind, subject="Show A")
    state = _state(client_down=False)
    state["topics"] = [{"id": "a", "title": "Show A", "last_error": "timeout", "last_error_class": "tracker"}]
    page = _home(state, path=url)
    assert page.select_one("#flash").get_text(" ", strip=True) == "Show A — synthetic check result"
    assert page.select_one("[data-attention-count]").get_text() == "1"
    assert not page.select("[data-problem-result]")


@pytest.mark.parametrize("topics", [[], [{"id": "a", "paused": True, "last_error": "timeout"}]])
def test_an_unrepresented_failed_action_keeps_visible_feedback(topics):
    url = flash_location("/", "synthetic failure", "err")
    state = _state(client_down=False)
    state["topics"] = topics
    assert _home(state, path=url).select_one("#flash")


def test_a_recovered_topic_leaves_the_triangle_and_its_message_says_what_the_click_found():
    url = flash_location("/", "old failure", "warn")
    state = _state(client_down=False)
    state["topics"] = [{"id": "a", "last_error": None, "last_ok": True}]
    page = _home(state, path=url)
    assert page.select_one("#flash")
    assert page.select_one("[data-attention]").has_attr("hidden")


def test_dead_default_client_has_one_triangle_entry_and_one_message():
    url = flash_location("/", "client refusal", "err")
    state = _state()
    state["topics"] = [{"id": "a", "title": "Show A", "last_error": "client refused", "last_error_class": "qbit"}]
    page = _home(state, path=url)
    assert len(page.select("#flash")) == 1
    assert page.select_one("[data-attention-count]").get_text() == "1"
    assert len(page.select("[data-issue-id]")) == 1


def test_script_is_versioned_and_uses_shared_poll():
    page = _home(_state())
    script = page.select_one('script[src^="/static/attention.js"]')
    assert script.has_attr("defer")
    assert _static_asset_version() in script["src"]
    source = (STATIC / "attention.js").read_text(encoding="utf-8")
    assert "healthPoll.add(" in source
    assert "fetch(" not in source
    assert "localStorage" not in source
