"""Check all's report stays the page's card: coloured by the status contract (a site that did not
answer is amber, an actionable failure red), closed in place by its x (only its own parameter
leaves the address), without a reason that repeats its row's label or help for an empty list."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from helpers import manual_check_consumers

from tow.config import load_config, save_config
from tow.store import save_state
from tow.web import app
from tow.web.views import check_report

TITLE = "Harbour Clock / Часовая гавань"


@pytest.fixture
def client():
    cfg = load_config()
    cfg["language"] = "en"
    save_config(cfg)
    save_state(
        {
            "topics": [
                {"id": "t1", "title": TITLE, "url": "http://rutor.info/torrent/1/x", "save_path": "M:/a"},
                {"id": "t2", "title": "Second Show", "url": "http://rutor.info/torrent/2/x", "save_path": "M:/a"},
            ]
        }
    )
    return TestClient(app, headers={"Origin": "http://127.0.0.1"})


def _report_page(client, monkeypatch, rows, path="/?check_result={job}"):
    consumers = manual_check_consumers(monkeypatch)
    titles = {"t1": TITLE, "t2": "Second Show"}
    monkeypatch.setattr(
        "tow.web.services.run_check",
        lambda **_kw: {"results": [{"id": tid, "title": titles[tid], **row} for tid, row in rows.items()]},
    )
    response = client.post("/check", data={"mode": "apply"}, follow_redirects=False)
    job = parse_qs(urlsplit(response.headers["location"]).query)["check_job"][0]
    consumers[0].run()
    return job, BeautifulSoup(client.get(path.format(job=job)).text, "html.parser").select_one("#check-result")


@pytest.mark.parametrize(
    ("rows", "kind"),
    [
        ({"t1": {"ok": True}, "t2": {"ok": True, "changed": True}}, "ok"),
        ({"t1": {"ok": True}, "t2": {"ok": False, "error": "timeout", "error_class": "tracker"}}, "warn"),
        (
            {
                "t1": {"ok": False, "error": "timeout", "error_class": "tracker"},
                "t2": {"ok": False, "error": "refused", "error_class": "qbit"},
            },
            "err",
        ),
    ],
)
def test_the_report_has_the_colour_of_its_worst_result(rows, kind):
    report = check_report({"results": [{"id": tid, **row} for tid, row in rows.items()]}, apply=True)
    assert report["kind"] == kind  # a red failure is not made amber by the rows that passed


def test_the_report_card_colours_by_the_contract_and_closes_in_place(client, monkeypatch):
    rows = {
        "t1": {"ok": False, "error": "timeout", "error_class": "tracker"},
        "t2": {"ok": False, "error": "refused", "error_class": "qbit"},
    }
    _job, card = _report_page(client, monkeypatch, rows, "/?s=name&check_result={job}&open=t1#check-result")
    pills = {
        row.select_one("a").get_text(): row.select_one(".pill")["class"] for row in card.select(".check-result-row")
    }
    assert pills[TITLE] == ["pill", "warn"]  # a site that did not answer
    assert pills["Second Show"] == ["pill", "bad"]  # the client refused: actionable
    close = card.select_one("a.flash-x")
    assert close["href"] == "/?s=name&open=t1"  # only its own parameter goes
    assert close["aria-label"]
    assert not [a for a in card.select("a") if a.get_text(strip=True) in {"Close", "Закрыть"}]


def test_a_reason_that_repeats_its_pill_and_the_help_of_an_empty_list_are_left_out(client, monkeypatch):
    _job, card = _report_page(client, monkeypatch, {"t1": {"ok": True}})
    row = card.select_one(".check-result-row")
    assert row.get_text(" ", strip=True) == f"Unchanged {TITLE}"  # not "Unchanged … no changes"
    assert "Results from this check" in card.get_text()
    _job, empty = _report_page(client, monkeypatch, {})
    assert not empty.select(".check-result-row")
    assert "Results from this check" not in empty.get_text()  # nothing to open
