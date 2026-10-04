"""Compact Home tools keep labelled choices and the shared control sizes."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tow.config import load_config, save_config
from tow.web import app

STATIC = Path(__file__).parents[1] / "src" / "tow" / "static"


@pytest.mark.parametrize("language", ["en", "ru"])
def test_sort_menu_has_labelled_radio_choices_and_native_backing(language):
    config = load_config()
    config["language"] = language
    save_config(config)
    page = TestClient(app).get("/").text
    assert 'class="home-page"' in page
    assert '<select id="list-sort">' in page
    assert 'id="sort-picker" hidden' in page
    assert 'aria-haspopup="menu" aria-expanded="false" aria-controls="sort-menu"' in page
    assert 'id="sort-menu" role="menu"' in page
    assert page.count('role="menuitemradio"') == 4
    assert page.count('aria-checked="true"') == 1
    for mode in ("", "name", "event", "status"):
        assert f'data-sort-value="{mode}"' in page
    for icon in ("added", "name", "event", "status"):
        assert f'href="#i-sort-{icon}"' in page
    assert "Сортировка" in page if language == "ru" else "Sort" in page


def test_home_spacing_does_not_change_other_pages():
    assert 'class="home-page"' not in TestClient(app).get("/settings").text
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".home-page { padding-top: .45rem; }" in css
    assert "grid-template-columns: minmax(0, 1fr) auto;" in css
    assert ".list-filters { display: flex; flex-wrap: wrap;" in css
    assert ".sort-picker[hidden], .sort-menu[hidden] { display: none; }" in css


def test_sort_choices_do_not_toggle_tracker_filters():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '!chip.hasAttribute("data-filter") && !trackerChips?.contains(chip)' in js
    assert 'select.dispatchEvent(new Event("change"))' in js
    assert 'select.addEventListener("change", sync)' in js
    assert 'sortSelect?.addEventListener("change", () => { sortRows(); remember(); })' in js


def test_menu_restores_known_url_modes_and_rejects_unknown_values():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert '[...sortSelect.options].some((option) => option.value === mode) ? mode : ""' in js
    assert 'set("s", sortSelect?.value || "")' in js
    assert 'item.setAttribute("aria-checked", String(item.dataset.sortValue === select.value))' in js


def test_menu_keyboard_navigation_focus_return_and_outside_dismissal():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for key in ("ArrowDown", "ArrowUp", "Home", "End", 'event.key === "Escape"', 'event.key === "Tab"'):
        assert key in js
    assert 'toggle.setAttribute("aria-expanded", "true")' in js
    assert 'toggle.setAttribute("aria-expanded", "false")' in js
    assert "if (restoreFocus) toggle.focus()" in js
    assert 'document.addEventListener("pointerdown"' in js
    assert 'picker.addEventListener("focusout"' in js


def test_enhancement_is_home_only_and_keeps_the_native_select_until_ready():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "if (!select || !picker || !toggle || !menu) return" in js
    assert "sync();\n  select.hidden = true;\n  picker.hidden = false;" in js
    assert "tools.hidden = originalOrder.length < 2" in js
