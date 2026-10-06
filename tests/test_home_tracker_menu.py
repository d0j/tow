"""Home offers every tracker without making the toolbar grow with the site list."""

from copy import deepcopy
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tow.config import load_config, save_config
from tow.store import save_state
from tow.web import app

STATIC = Path(__file__).parents[1] / "src" / "tow" / "static"


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("count", [0, 1, 35, 100])
def test_menu_includes_all_configured_sites_even_without_topics(language, count):
    config = load_config()
    preset = deepcopy(config["trackers"]["rutor"])
    names = [f"site-{number:03}" for number in reversed(range(count))]
    config["trackers"] = {name: deepcopy(preset) for name in names}
    config["language"] = language
    save_config(config)
    page = BeautifulSoup(TestClient(app).get("/").text, "html.parser")
    menu = page.select_one("#tracker-menu")
    choices = menu.select('[role="menuitemradio"]')
    assert [choice["data-choice-value"] for choice in choices] == ["", *sorted(names)]
    assert choices[0]["aria-checked"] == "true"
    assert all(choice["aria-checked"] == "false" for choice in choices[1:])
    assert choices[0].get_text(strip=True) == ("Все сайты" if language == "ru" else "All sites")
    assert [option["value"] for option in page.select("#list-tracker option")] == ["", *sorted(names)]
    assert page.select_one("[data-tracker-chips]") is None
    assert len(page.select("[data-filter]")) == 4


def test_menu_deduplicates_sites_and_retains_unknown_watched_site():
    save_state(
        {
            "topics": [
                {"id": "a", "title": "Show A", "url": "https://rutor.info/torrent/1234567", "save_path": "M:/TV"},
                {"id": "b", "title": "Show B", "url": "https://rutor.info/torrent/1234568", "save_path": "M:/TV"},
                {"id": "c", "title": "Show C", "url": "https://unknown.invalid/topic/1234569", "save_path": "M:/TV"},
            ]
        }
    )
    page = BeautifulSoup(TestClient(app).get("/").text, "html.parser")
    values = [choice["data-choice-value"] for choice in page.select("#tracker-menu [role=menuitemradio]")]
    assert values.count("rutor") == 1
    assert {row["data-tracker"] for row in page.select(".row-wrap")} <= set(values)


def test_sites_are_named_by_their_title_as_on_the_sites_page():
    config = load_config()
    config["trackers"] = {"nnmclub": deepcopy(config["trackers"]["nnmclub"]), "my_site": {"url_regex": r"x/(\d+)"}}
    save_config(config)
    page = BeautifulSoup(TestClient(app).get("/").text, "html.parser")
    choices = page.select("#tracker-menu [role=menuitemradio]")
    assert [(c["data-choice-value"], c.get_text(strip=True)) for c in choices[1:]] == [
        ("my_site", "my_site"),
        ("nnmclub", "NNM-Club"),
    ]


def test_long_and_markup_site_names_are_text_not_html():
    config = load_config()
    name = '<img src=x onerror="alert(1)"> &' + "very-long-site-name-" * 30
    spec = deepcopy(config["trackers"]["rutor"])
    spec.pop("title", None)  # a site of the owner's: named by its key
    config["trackers"] = {name: spec}
    save_config(config)
    page = BeautifulSoup(TestClient(app).get("/").text, "html.parser")
    choice = page.select("#tracker-menu [role=menuitemradio]")[1]
    assert choice["data-choice-value"] == name
    assert choice["title"] == name
    assert choice.select_one("span").get_text() == name
    assert choice.select_one("img") is None
    assert page.select("#list-tracker option")[1].get_text() == name


def test_tracker_menu_has_labelled_opener_and_is_hidden_before_enhancement():
    page = BeautifulSoup(TestClient(app).get("/").text, "html.parser")
    picker = page.select_one("#tracker-picker")
    assert picker.has_attr("hidden")
    toggle = picker.select_one("button")
    assert toggle["aria-haspopup"] == "menu"
    assert toggle["aria-expanded"] == "false"
    assert toggle["aria-controls"] == "tracker-menu"
    assert toggle["aria-label"]
    assert picker.select_one("#tracker-menu").has_attr("hidden")


@pytest.mark.parametrize("name", [0, 123, False, None])
def test_yaml_scalar_site_names_are_displayed_as_text_without_changing_config(name):
    config = load_config()
    config["trackers"][name] = deepcopy(config["trackers"]["rutor"])
    save_config(config)
    page = BeautifulSoup(TestClient(app).get("/").text, "html.parser")
    values = [choice["data-choice-value"] for choice in page.select("#tracker-menu [role=menuitemradio]")]
    assert str(name) in values
    assert values == sorted(set(values))
    assert name in load_config()["trackers"]


def test_empty_site_name_and_scalar_text_collisions_do_not_duplicate_all_or_choices():
    config = load_config()
    for name in ("", 123, "123"):
        config["trackers"][name] = deepcopy(config["trackers"]["rutor"])
    save_config(config)
    page = BeautifulSoup(TestClient(app).get("/").text, "html.parser")
    values = [choice["data-choice-value"] for choice in page.select("#tracker-menu [role=menuitemradio]")]
    assert values.count("") == 1
    assert values.count("123") == 1
    assert len(page.select('#tracker-menu [aria-checked="true"]')) == 1


def test_tracker_url_restoration_and_reset_use_the_same_menu_value():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '[...trackerSelect.options].some((option) => option.value === tracker) ? tracker : ""' in js
    assert 'initChoiceMenu(trackerSelect, "tracker")' in js
    assert 'initChoiceMenu(sortSelect, "sort")' in js
    assert 'trackerSelect?.addEventListener("change", () => {' in js
    assert "tracker = trackerSelect.value;\n    announce();\n    remember();" in js
    assert 'set("t", tracker)' in js
    assert "el.dataset.tracker === tracker" in js
    # An active URL filter stays resettable even after all but one watch is removed.
    assert "tools.hidden = originalOrder.length < 2 && !tracker" in js


def test_a_filter_that_matches_nothing_says_so_and_offers_a_reset():
    save_state({"topics": [{"id": "a", "title": "Show A", "url": "https://rutor.info/torrent/1234567"}]})
    page = BeautifulSoup(TestClient(app).get("/?f=paused").text, "html.parser")
    empty = page.select_one("#topics #list-empty")
    assert empty.has_attr("hidden")  # app.js shows it when a filter leaves no row
    assert empty.select_one("a")["href"] == "/"
    assert "Под фильтр ничего не подходит" in empty.get_text()
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "listEmpty.hidden = !(narrowed && !shown)" in js


def test_on_a_phone_rows_wrap_and_the_version_does_not_float_over_text():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    phone = css[css.index("The row icons sit top right") :]
    phone = phone[: phone.index("\n}\n")]
    assert ".topic-path .clip-start { white-space: normal;" in phone
    assert ".topic-event.clip, .topic-event .row-error { white-space: normal;" in phone
    assert "body > .app-version { position: absolute; }" in phone
    assert "body { position: relative; padding-bottom:" in phone


def test_large_lists_scroll_and_long_names_do_not_resize_the_toolbar():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".tracker-toggle { max-width: 10rem; }" in css
    assert "text-overflow: ellipsis; white-space: nowrap;" in css
    assert ".tracker-picker[hidden] { display: none; }" in css
    assert ".tracker-menu { left: 0; right: auto;" in css
    # Parent width excludes the page scrollbar, unlike a 100vw-based limit.
    assert "max-width: min(19rem, 100%); max-height: min(20rem, 45dvh);" in css
    assert "max-height: min(20rem, 45dvh); overflow-y: auto;" in css
    assert "max-width: min(19rem, calc(100vw - 1.5rem))" in css
