"""Sentences with markup are one catalog value each (``tm``): a translation can reorder them, and
only a short list of tags comes back from the escaped text."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from tow import i18n
from tow.config import load_config, save_config
from tow.web import app
from tow.web.text import tm

SRC = Path(__file__).parents[1] / "src" / "tow"
TAG = re.compile(r"</?[a-z]+\b[^>]*>")


def _markup_keys() -> set[str]:
    return {key for key in i18n.keys("en") if "<" in json.dumps(i18n._load("en")[0][key], ensure_ascii=False)}


def test_tags_from_the_catalog_come_back_and_values_are_escaped():
    i18n.use("en")
    html = str(tm("settings.access.no_password"))
    assert html.startswith("<b>No password set</b> — ")
    link = str(tm("settings.page.secret_store", help="/settings/help"))
    assert '<a href="/settings/help">guide</a>' in link
    value = str(tm("sites.help.p2", field='<script>alert("x")</script>'))
    assert "<script>" not in value
    assert "&lt;script&gt;" in value


def test_a_link_to_another_site_or_a_script_becomes_harmless():
    i18n.use("en")
    for href in ("javascript:alert(1)", "//evil.example/", "http://plain.example/", 'x" onclick="y'):
        assert '<a href="#">' in str(tm("settings.page.secret_store", help=href)), href
    assert '<a href="https://example.org/a">' in str(tm("settings.page.secret_store", help="https://example.org/a"))


def test_a_translation_cannot_bring_other_markup(monkeypatch, tmp_path):
    locales = tmp_path / "locales"
    locales.mkdir()
    for path in i18n.LOCALES_DIR.glob("*.json"):
        tree = json.loads(path.read_text(encoding="utf-8"))
        tree["help"]["intro"]["text"] = '<img src=x onerror=alert(1)><b onclick="x">bold</b> <b>ok</b>'
        (locales / path.name).write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(i18n, "LOCALES_DIR", locales)
    i18n.reload()
    try:
        html = str(tm("help.intro.text"))
        assert "<img" not in html
        assert "<b onclick" not in html
        assert "<b>ok</b>" in html
    finally:
        monkeypatch.undo()
        i18n.reload()


def test_every_language_has_the_same_tags_as_english():
    for code in i18n.codes():
        for key in _markup_keys():
            english = TAG.findall(i18n._load("en")[0][key])
            other = TAG.findall(i18n._load(code)[0].get(key, i18n._load("en")[0][key]))
            assert sorted(other) == sorted(english), (code, key)


def test_a_text_with_markup_is_never_shown_with_plain_t():
    """``t()`` would show the tags as text; a key with markup goes through ``tm()``."""
    keys = _markup_keys()
    assert len(keys) >= 20
    plain = re.compile(r"""\bt\(\s*['"]([a-z0-9_.]+)['"]""")
    for path in [*(SRC / "templates").glob("*.html"), *SRC.rglob("*.py")]:
        used = set(plain.findall(path.read_text(encoding="utf-8"))) & keys
        assert not used, (path.name, used)


def test_no_template_glues_sentence_pieces_any_more():
    pieces = re.compile(r"_(?:a|b|c|before|after|bold|rest)'\)")
    for path in (SRC / "templates").glob("*.html"):
        assert not pieces.search(path.read_text(encoding="utf-8")), path.name


def test_the_help_and_sites_pages_render_their_markup_in_either_language():
    cfg = load_config()
    cfg["language"] = "auto"
    save_config(cfg)
    english = TestClient(app, headers={"Accept-Language": "en"})
    russian = TestClient(app, headers={"Accept-Language": "ru"})
    assert "<li><b>Home</b> — the topics TOW watches" in english.get("/settings/help").text
    assert "<li><b>Главная</b> — раздачи, за которыми следит TOW" in russian.get("/settings/help").text
    sites = english.get("/sites").text
    assert "Replace the number with <code>(\\d+)</code>" in sites
    assert "<code>/forum/dl.php?t={id}</code> or <code>/download.php?id={id}</code>" in sites
