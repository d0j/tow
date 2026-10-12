"""Home's "Needs attention" banner: it folds into a warning button with the number of problems and
opens again from it (attention.js), the folded choice belongs to one set of problems (the
fingerprint), and without the script the banner stays open."""

from __future__ import annotations

import re
import time
from pathlib import Path

from fastapi.testclient import TestClient

from tow.config import load_config, save_config
from tow.store import save_state
from tow.web import app
from tow.web.templating import _static_asset_version

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
    health = {"auto_at_ts": int(time.time()), "qbit_ok": not client_down, "history_rebuilt_at": int(time.time())}
    return {"topics": topics, "mirrors": {}, "health": health}


def _home(state: dict, language: str = "ru") -> str:
    cfg = load_config()
    cfg["language"] = language
    save_config(cfg)
    save_state(state)
    return TestClient(app).get("/").text


def _fingerprint(page: str) -> str:
    match = re.search(r'<script src="/static/attention\.js\?v=[^"]+" data-fingerprint="(\w*)"></script>', page)
    assert match, "attention.js is loaded in <head>, before the page is drawn"
    return match.group(1)


def test_the_banner_folds_into_a_warning_button_with_the_count():
    page = _home(_state(signs_in=2))
    head = page.split("</head>", 1)[0]

    assert "/static/attention.js" in head
    assert " defer" not in head.split("attention.js", 1)[1].split(">", 1)[0]
    assert '<section class="attention card" id="attention" role="status" aria-labelledby="attention-title">' in page
    fold = re.search(r"<button[^>]*data-attention-fold[^>]*>", page).group(0)
    assert 'class="ico attention-fold"' in fold
    assert 'aria-expanded="true"' in fold
    assert 'aria-controls="attention"' in fold
    assert 'aria-label="Свернуть «Требует внимания»"' in fold
    unfold = re.search(r"<button[^>]*data-attention-unfold[^>]*>", page).group(0)
    assert 'class="attention-unfold bad"' in unfold  # the worst problem is red
    assert 'aria-expanded="false"' in unfold
    assert 'aria-controls="attention"' in unfold
    assert 'aria-label="Требует внимания: 3. Показать"' in unfold  # the client, a site, the history
    assert '<use href="#i-alert"/>' in page.split("data-attention-unfold", 1)[1].split("</button>", 1)[0]
    assert '<span class="attention-count" aria-hidden="true">3</span>' in page
    assert '<symbol id="i-alert"' in page


def test_the_names_are_in_english_too():
    page = _home(_state(), "en")

    assert 'aria-label="Collapse “Needs attention”"' in page
    assert 'aria-label="Needs attention: 2. Show"' in page


def test_an_amber_set_folds_into_an_amber_button():
    page = _home(_state(client_down=False))  # only the history notice

    assert 'class="attention-unfold warn"' in page


def test_the_fingerprint_names_the_set_of_problems():
    one = _fingerprint(_home(_state(signs_in=1)))
    assert re.fullmatch(r"[0-9a-f]{16}", one)
    assert _fingerprint(_home(_state(signs_in=1))) == one
    assert _fingerprint(_home(_state(signs_in=1), "en")) == one  # not the words: the problems
    assert _fingerprint(_home(_state(signs_in=2))) != one  # another topic asks for a sign-in
    assert _fingerprint(_home(_state(client_down=False, signs_in=1))) != one  # the client answers again


def test_without_problems_no_banner_and_an_empty_fingerprint():
    page = _home({"topics": [], "mirrors": {}, "health": {"auto_at_ts": int(time.time()), "qbit_ok": True}})

    assert _fingerprint(page) == ""
    assert 'id="attention"' not in page
    assert "data-attention-unfold" not in page


def test_without_the_script_the_banner_is_open_and_the_buttons_hidden():
    css = (STATIC / "app.css").read_text(encoding="utf-8")

    assert ".attention-fold.ico, .attention-unfold { display: none; }" in css
    shown = [rule.split("{", 1)[0].strip() for rule in re.findall(r"[^{}]+\{[^{}]*display: inline-flex[^{}]*\}", css)]
    assert ".attention-js .attention-fold.ico" in shown
    assert ".attention-js.attention-folded .attention-unfold" in shown
    assert re.search(r"\.attention-folded \.attention \{ display: none; \}", css)


def test_the_script_is_part_of_the_static_version():
    assert '"attention.js"' in (STATIC.parent / "web" / "templating.py").read_text(encoding="utf-8")
    assert _static_asset_version()
