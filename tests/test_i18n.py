"""Languages: English by default, the browser's language on request, one catalog file per language."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tow import i18n

SRC = Path(__file__).parents[1] / "src" / "tow"


def _set_language(value: str) -> None:
    from tow.config import load_config, save_config

    cfg = load_config()
    cfg["language"] = value
    save_config(cfg)


@pytest.mark.parametrize(
    ("language", "interval", "global_timer", "observer", "server_recovery", "old_promise"),
    [
        (
            "en",
            "21 min",
            "own timer replaces that interval",
            "it does not restart TOW",
            "The running TOW service restarts the web server if it fails",
            "it starts it again and reports the cause",
        ),
        (
            "ru",
            "21 мин",
            "таймер раздачи заменяет для неё общий интервал",
            "сам TOW не перезапускает",
            "Работающий сервис TOW перезапускает веб-сервер при сбое",
            "он запускает его заново и пишет причину",
        ),
    ],
)
def test_rendered_help_separates_topic_timers_server_recovery_and_watchdog_observation(
    language, interval, global_timer, observer, server_recovery, old_promise
):
    from tow.config import load_config, save_config
    from tow.web import create_app

    cfg = load_config()
    cfg.update(language=language, interval_sec=1260)
    save_config(cfg)
    response = TestClient(create_app()).get("/settings/help")
    assert response.status_code == 200
    assert f"<b>{interval}</b>" in response.text
    assert global_timer in response.text
    assert observer in response.text
    assert server_recovery in response.text
    assert old_promise not in response.text


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("ru-RU,ru;q=0.9,en-US;q=0.8", "ru"),
        ("en-US,en;q=0.9", "en"),
        ("de-DE,de;q=0.9,ru;q=0.5", "ru"),  # German is not there yet: the next one the browser accepts
        ("fr-FR", "en"),  # nothing matches: English
        ("ru;q=0", "en"),  # q=0 means "not this one"
        ("", "en"),
        (None, "en"),
    ],
)
def test_browser_language_is_negotiated(header, expected):
    assert i18n.negotiate(header, ["en", "ru"]) == expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("zh-Hant-TW", "zh-Hant"),  # RFC 4647 lookup: zh-hant-tw, zh-hant, zh
        ("zh-Hans-CN", "zh"),
        ("ZH-HANT", "zh-Hant"),  # any case
        ("pt-br", "pt-BR"),
        ("PT-BR;q=0.5, ru;q=0.4", "pt-BR"),
        ("pt-PT, ru;q=0.9", "ru"),  # no "pt" alone: the next one
        ("de-x-private-y, ru;q=0.1", "ru"),
        ("sr-Latn-x-foo", "sr-Latn"),  # a singleton is dropped with the subtag after it
        ("ru;q=abc, pt-BR;q=0.2", "pt-BR"),  # malformed q: that entry is ignored
        ("ru;q=, pt-BR;q=0.2", "pt-BR"),
        ("ru;q=-1, pt-BR;q=0.2", "pt-BR"),
        ("ru;q=5, pt-BR", "ru"),  # q above 1 counts as 1; the first one wins a tie
        ("ru;level=1;q=0.9, pt-BR;q=0.8", "ru"),  # other parameters do not matter
        ("*, ru;q=0.5", "ru"),  # the wildcard says nothing
        ("ru;q=0.000, en;q=0.001", "en"),
        ("😀, ru", "ru"),
    ],
)
def test_regional_codes_follow_rfc_4647(header, expected):
    assert i18n.negotiate(header, ["en", "ru", "pt-BR", "zh", "zh-Hant", "sr-Latn"]) == expected


def test_a_regional_language_file_matches_any_case(monkeypatch, tmp_path):
    for path in (SRC / "locales").glob("*.json"):
        (tmp_path / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "pt-BR.json").write_text(
        json.dumps({"_meta": {"name": "Portuguese (Brazil)", "native": "Português", "plural": "french"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(i18n, "LOCALES_DIR", tmp_path)
    i18n.reload()
    try:
        assert "pt-BR" in i18n.codes()
        for written in ("pt-BR", "pt-br", "PT-BR"):
            assert i18n.setting({"language": written}) == "pt-BR"
            _set_language(written)  # config.yaml takes any case
            assert i18n.message_language() == "pt-BR"
        assert i18n.negotiate("pt-br,pt;q=0.9") == "pt-BR"
        i18n.use("PT-br")
        assert i18n.current() == "pt-BR"
        assert i18n.translate("common.save", "pt-br") == "Save"  # nothing translated: English
    finally:
        monkeypatch.undo()
        i18n.reload()
        i18n._CURRENT.set(None)  # the language of this thread goes back to the owner's


@pytest.mark.parametrize("value", ["EN", "Ru", "pt-BR", "zh-Hant-TW", "auto"])
def test_config_takes_a_language_code_in_any_case(value):
    from tow.config import load_config

    _set_language(value)
    assert load_config()["language"] == value


@pytest.mark.parametrize(
    ("n", "form"),
    [
        (1, "one"),
        (2, "few"),
        (4, "few"),
        (5, "many"),
        (11, "many"),
        (12, "many"),
        (21, "one"),
        (22, "few"),
        (25, "many"),
        (111, "many"),
        (0, "many"),
    ],
)
def test_russian_plurals(n, form):
    assert i18n.PLURAL_RULES["east_slavic"](n) == form


def test_every_plural_has_the_forms_its_language_needs():
    for code in i18n.codes():
        needed = set(i18n.PLURAL_FORMS[i18n.plural_rule(code)])
        for key, value in i18n._load(code)[0].items():
            if isinstance(value, dict):
                assert needed <= set(value), (code, key, sorted(needed - set(value)))
                assert all(form.strip() for form in value.values()), (code, key)


@pytest.mark.parametrize(("n", "expected"), [(1, "1 файл"), (3, "3 файла"), (5, "5 файлов"), (1.5, "1.5 файла")])
def test_a_fraction_takes_the_other_form(monkeypatch, tmp_path, n, expected):
    (tmp_path / "en.json").write_text(
        json.dumps({"_meta": {"name": "English", "native": "English", "plural": "one_other"}}), encoding="utf-8"
    )
    forms = {"one": "{n} файл", "few": "{n} файла", "many": "{n} файлов", "other": "{n} файла"}
    (tmp_path / "ru.json").write_text(
        json.dumps({"_meta": {"name": "Russian", "native": "Русский", "plural": "east_slavic"}, "f": forms}),
        encoding="utf-8",
    )
    monkeypatch.setattr(i18n, "LOCALES_DIR", tmp_path)
    i18n.reload()
    try:
        assert i18n.translate("f", "ru", n=n) == expected
        assert i18n.translate("f", "ru", n="not a number") == "not a number файла"  # never raises
    finally:
        monkeypatch.undo()
        i18n.reload()


def test_a_plural_text_is_always_given_its_number():
    """A key whose text has plural forms is called with ``n=`` (else every count reads as 0)."""
    plural = {key for key, value in i18n._load("en")[0].items() if isinstance(value, dict)}
    missing = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and _call_name(node) in ("t", "translate") and node.args):
                continue
            key = node.args[0]
            given = any(keyword.arg in ("n", None) for keyword in node.keywords)
            if isinstance(key, ast.Constant) and key.value in plural and not given:
                missing.append(f"{path.name}:{node.lineno} {key.value}")
    for path in (SRC / "templates").glob("*.html"):
        for key, args in re.findall(r"""\bt\(\s*'([a-z0-9_.]+)'([^)]*)\)""", path.read_text(encoding="utf-8")):
            if key in plural and not re.search(r"\bn\s*=", args):
                missing.append(f"{path.name} {key}")
    assert missing == []


def test_lookup_falls_back_to_english_then_to_the_key(monkeypatch, tmp_path):
    (tmp_path / "en.json").write_text(
        json.dumps(
            {"_meta": {"name": "English"}, "a": {"b": "Hello {name}", "c": {"one": "{n} file", "other": "{n} files"}}}
        ),
        encoding="utf-8",
    )
    (tmp_path / "ru.json").write_text(
        json.dumps(
            {
                "_meta": {"name": "Russian", "native": "Русский", "plural": "east_slavic"},
                "a": {"c": {"one": "{n} файл", "few": "{n} файла", "many": "{n} файлов", "other": "{n} файла"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(i18n, "LOCALES_DIR", tmp_path)
    i18n.reload()
    try:
        assert i18n.translate("a.b", "ru", name="TOW") == "Hello TOW"  # missing in Russian: English
        assert i18n.translate("a.c", "ru", n=3) == "3 файла"
        assert i18n.translate("a.c", "ru", n=5) == "5 файлов"
        assert i18n.translate("a.c", "en", n=1) == "1 file"
        assert i18n.translate("no.such.key", "ru") == "no.such.key"
        assert i18n.translate("a.b", "ru") == "Hello {name}"  # a missing value stays visible, never crashes
        assert [code for code, _n, _native in i18n.available()] == ["en", "ru"]
    finally:
        monkeypatch.undo()
        i18n.reload()


def test_a_new_language_is_one_file(monkeypatch, tmp_path):
    for path in (SRC / "locales").glob("*.json"):
        (tmp_path / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "de.json").write_text(
        json.dumps({"_meta": {"name": "German", "native": "Deutsch"}, "common": {"save": "Speichern"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(i18n, "LOCALES_DIR", tmp_path)
    i18n.reload()
    try:
        assert "de" in i18n.codes()
        assert i18n.translate("common.save", "de") == "Speichern"
        assert i18n.negotiate("de-AT,de;q=0.9") == "de"
    finally:
        monkeypatch.undo()
        i18n.reload()


def test_english_is_the_default_and_the_browser_decides(monkeypatch):
    from tow.web import app

    _set_language("auto")
    page = TestClient(app).get("/settings", headers={"Accept-Language": "en-US,en;q=0.9"}).text
    assert '<html lang="en">' in page
    assert _selected_language(page) == ("auto", i18n.translate("settings.language.auto", "en", name="English"))
    ru = TestClient(app).get("/settings", headers={"Accept-Language": "ru-RU,ru;q=0.9"}).text
    assert '<html lang="ru">' in ru
    assert _selected_language(ru) == ("auto", i18n.translate("settings.language.auto", "ru", name="Русский"))
    plain = TestClient(app).get("/settings").text  # no header: English
    assert '<html lang="en">' in plain


def _language_select(page: str) -> str:
    return page.split('id="language-choice"', 1)[1].split("</select>", 1)[0]


def _selected_language(page: str) -> tuple[str, str]:
    import html

    value, label = re.search(r'<option value="([^"]+)" selected>([^<]*)</option>', _language_select(page)).groups()
    return value, html.unescape(label)


def test_language_is_one_list_with_automatic_first():
    import html

    from tow.web import app

    _set_language("auto")
    page = TestClient(app).get("/settings", headers={"Accept-Language": "ru"}).text
    values = re.findall(r'<option value="([^"]+)"', _language_select(page))
    assert values == ["auto", *i18n.codes()]
    assert 'type="checkbox"' not in page.split('id="acc-language"', 1)[1].split("</details>", 1)[0]
    assert i18n.translate("settings.language.messages_note_auto", "ru") in html.unescape(page)
    _set_language("ru")
    page = TestClient(app).get("/settings", headers={"Accept-Language": "en"}).text
    assert _selected_language(page)[0] == "ru"
    assert i18n.translate("settings.language.messages_note", "ru") in html.unescape(page)
    search = re.search(r'id="acc-language" data-q="([^"]*)"', page).group(1).split()
    assert {"language", "язык", "english", "русский", "ru"} <= set(search)


@pytest.mark.parametrize(
    ("form", "saved"),
    [
        ({"language": "ru"}, "ru"),
        ({"language": "RU"}, "ru"),
        ({"language": "auto"}, "auto"),
        ({"language": "xx"}, "auto"),  # not a language TOW has: automatic
        ({"auto": "0", "language": "ru"}, "ru"),  # the older form (a checkbox and a list)
        ({"auto": "1", "language": "ru"}, "auto"),
    ],
)
def test_saving_the_language(form, saved):
    from tow.config import load_config
    from tow.web import app

    _set_language("en")
    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/settings/language", data=form, follow_redirects=False
    )
    assert response.status_code == 303
    assert load_config()["language"] == saved


def test_a_chosen_language_wins_over_the_browser():
    from tow.web import app

    c = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    c.post("/settings/language", data={"language": "ru"})
    assert '<html lang="ru">' in c.get("/settings", headers={"Accept-Language": "en"}).text
    c.post("/settings/language", data={"language": "auto"})
    assert '<html lang="en">' in c.get("/settings", headers={"Accept-Language": "en"}).text


def test_messages_follow_the_owners_browser_when_auto():
    from tow.web import app

    _set_language("auto")
    TestClient(app).get("/", headers={"Accept-Language": "ru-RU,ru;q=0.9", "Accept": "text/html"})
    assert i18n.message_language() == "ru"
    _set_language("en")
    assert i18n.message_language() == "en"


def test_the_remembered_browser_language_is_rewritten_when_its_file_is_gone(monkeypatch, tmp_path):
    from tow.web import app

    _set_language("auto")
    visit = {"Accept-Language": "ru-RU,ru;q=0.9", "Accept": "text/html"}
    TestClient(app).get("/", headers=visit)
    other_home = tmp_path / "other"
    other_home.mkdir()
    monkeypatch.setenv("TOW_HOME", str(other_home))  # another install in the same process
    TestClient(app).get("/", headers=visit)
    assert i18n.message_language() == "ru"


def test_texts_outside_a_request_read_the_config_only_when_it_changes(monkeypatch):
    import tow.config

    calls = []
    original = tow.config.load_config
    monkeypatch.setattr(tow.config, "load_config", lambda: calls.append(1) or original())
    _set_language("ru")
    calls.clear()
    for _ in range(50):
        i18n.translate("common.save")
    assert i18n.message_language() == "ru"
    assert len(calls) <= 1
    _set_language("en")  # an edit is seen at once
    assert i18n.translate("common.save") == "Save"


def test_app_js_texts_are_built_once_per_language():
    from tow.web.templating import js_texts

    i18n.reload()
    first = js_texts()
    assert first
    assert all(key.startswith("js.") or key == "_datetime" for key in first)
    misses = i18n._prefixed.cache_info().misses
    first["js.extra"] = "a caller's change"
    assert js_texts() == {key: value for key, value in first.items() if key != "js.extra"}
    assert i18n._prefixed.cache_info().misses == misses
    assert i18n.texts_with_prefix("js.", "en") != i18n.texts_with_prefix("js.", "ru")


def test_page_scripts_write_dates_and_sizes_like_the_server():
    from tow.web.templating import content_texts, js_texts

    root = Path(__file__).resolve().parents[1] / "src" / "tow" / "static"
    _set_language("ru")
    assert js_texts()["_datetime"] == i18n.datetime_pattern("ru") == "%d.%m.%Y %H:%M:%S"
    assert content_texts()["web.bytes.gb"] == "{size} ГБ"
    _set_language("en")
    assert js_texts()["_datetime"] == i18n.datetime_pattern("en")
    assert content_texts()["web.bytes.kb"] == "{size} KB"
    content_js = (root / "content.js").read_text(encoding="utf-8")
    updates_js = (root / "updates.js").read_text(encoding="utf-8")
    assert "KiB" not in content_js
    assert '"web.bytes.gb"' in content_js
    assert "toLocaleString" not in updates_js
    assert "I18N._datetime" in updates_js  # tests/js/update_page_version.mjs checks the result


def test_bad_language_setting_is_refused():
    from tow.config import ConfigError, load_config
    from tow.paths import config_path

    config_path().write_text("language: 42\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="language"):
        load_config()


def test_an_unread_language_setting_is_told_once_in_the_owners_words(monkeypatch, caplog, capsys):
    # QA: every command with a broken config.yaml printed "language setting not read (automatic
    # is used): ConfigError" - English, a class name, and again whenever the file changed.
    from tow import platform
    from tow.cli import main
    from tow.config import ConfigError, load_config
    from tow.paths import config_path

    monkeypatch.setattr(i18n, "_TOLD", set())
    monkeypatch.setattr(i18n, "_CURRENT", i18n.ContextVar("test_language", default=None))
    monkeypatch.setattr(platform.current(), "ui_language", lambda: "ru-RU")  # the system's: Russian
    i18n._configured_language_of.cache_clear()
    config_path().write_text("language: ru\nport: [broken\n", encoding="utf-8")
    with pytest.raises(ConfigError) as broken:
        load_config()
    assert main(["version"]) == 0
    config_path().write_text("language: ru\nport: [still broken\n", encoding="utf-8")
    assert i18n._configured_language() == i18n.AUTO
    told = [entry.getMessage() for entry in caplog.records if entry.name == "tow.i18n"]
    assert told == [i18n.translate("config_error.language_unread", "ru", error=broken.value.text("ru"))]
    assert "ConfigError" not in capsys.readouterr().err


# --- a damaged language file never breaks TOW ------------------------------------------------


@pytest.fixture
def locales(monkeypatch, tmp_path):
    """A copy of the shipped catalogs TOW reads instead of its own (edit files, then reload)."""
    folder = tmp_path / "locales"
    folder.mkdir()
    for path in (SRC / "locales").glob("*.json"):
        (folder / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(i18n, "LOCALES_DIR", folder)
    i18n.reload()
    yield folder
    monkeypatch.setattr(i18n, "LOCALES_DIR", SRC / "locales")
    i18n.reload()
    i18n._CURRENT.set(None)  # a CLI run in the test set this thread's language


@pytest.mark.parametrize(
    "damage",
    [
        '{"_meta": {"name": "Russian", "native": "Русский", "plural": "east_slavic"}, "common": {',  # syntax
        '{"_meta": "Russian", "common": {"save": "Сохранить"}}',  # _meta is not an object
        '{"_meta": {"native": "Русский"}, "common": {"save": "Сохранить"}}',  # no name
        '["not", "an", "object"]',
        "",
    ],
)
def test_a_damaged_language_file_is_skipped_not_fatal(locales, caplog, damage):
    from tow.web import app

    (locales / "ru.json").write_text(damage, encoding="utf-8")
    i18n.reload()
    assert i18n.codes() == ["en"]
    assert "ru.json" in caplog.text
    assert i18n.translate("common.save", "ru") == "Save"
    page = TestClient(app).get("/settings", headers={"Accept-Language": "ru"})
    assert page.status_code == 200
    assert '<html lang="en">' in page.text


def test_a_damaged_language_file_does_not_break_the_cli(locales):
    from tow.cli import main

    (locales / "ru.json").write_text("{oops", encoding="utf-8")
    i18n.reload()
    assert main(["version"]) == 0


def test_damaged_english_shows_the_keys_and_never_crashes(locales):
    from tow.web import app

    (locales / "en.json").write_text("{", encoding="utf-8")
    i18n.reload()
    assert "en" in i18n.codes()
    assert i18n.translate("common.save", "en") == "common.save"
    assert i18n.translate("common.save", "ru") == "Сохранить"
    _set_language("en")
    assert TestClient(app).get("/settings").status_code == 200


def test_meta_gaps_fall_back_with_a_warning(locales, caplog):
    (locales / "de.json").write_text(
        json.dumps({"_meta": {"name": "German", "plural": "celtic"}, "common": {"save": "Speichern"}}),
        encoding="utf-8",
    )
    i18n.reload()
    assert ("de", "German", "German") in i18n.available()  # no native name: the English one
    assert i18n.plural_rule("de") == "one_other"  # an unknown rule: the English one
    assert "de.json" in caplog.text
    assert "plural" in caplog.text


def test_a_wrong_value_costs_that_entry_only(locales, caplog):
    (locales / "de.json").write_text(
        json.dumps(
            {
                "_meta": {"name": "German", "native": "Deutsch", "plural": "one_other"},
                "common": {"save": "Speichern", "check": 42, "remove": ["x"], "cancel": {"one": 1, "other": "x"}},
            }
        ),
        encoding="utf-8",
    )
    i18n.reload()
    assert i18n.translate("common.save", "de") == "Speichern"
    assert i18n.translate("common.check", "de") == i18n.translate("common.check", "en")  # English instead
    assert "common.check" in caplog.text


def test_dates_and_decimals_follow_the_language():
    from tow.clock import format_ui_timestamp
    from tow.web.text import format_bytes as _format_bytes

    stamp = "2026-10-01T15:50:00+00:00"
    assert format_ui_timestamp(stamp, "en") == "2026-10-01 18:50:00 IDT UTC+03:00"
    assert format_ui_timestamp(stamp, "ru") == "01.10.2026 18:50:00 IDT UTC+03:00"
    assert i18n.format_decimal(1.5, 1, "en") == "1.5"
    assert i18n.format_decimal(1.5, 1, "ru") == "1,5"
    token = i18n._CURRENT.set("en")
    try:
        assert _format_bytes(1536 * 1024**2) == i18n.translate("web.bytes.gb", "en", size="1.5")
    finally:
        i18n._CURRENT.reset(token)
    assert _format_bytes(1536 * 1024**2) == i18n.translate("web.bytes.gb", "ru", size="1,5")  # the tests' language


def test_short_times_follow_the_language():
    """Status lines ("last night copy: …") wrote dd.mm HH:MM in every language."""
    from datetime import UTC, datetime

    from tow.pulse import clock

    moment = datetime(2026, 10, 1, 15, 50, tzinfo=UTC).timestamp()
    assert clock(moment, "en") == "2026-10-01 18:50"
    assert clock(moment, "ru") == "01.10 18:50"
    assert i18n.meta_issues({"name": "X", "native": "X", "plural": "none", "datetime_short": "%A %H"})[1]


def test_a_date_pattern_with_names_falls_back(locales, caplog):
    tree = json.loads((locales / "ru.json").read_text(encoding="utf-8"))
    tree["_meta"]["datetime"] = "%d %B %Y"  # month names would come in the machine's language
    (locales / "ru.json").write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")
    i18n.reload()
    from datetime import UTC, datetime

    assert i18n.format_datetime(datetime(2026, 10, 1, 18, 50, tzinfo=UTC), "ru") == "2026-10-01 18:50:00"
    assert "datetime" in caplog.text


@pytest.mark.parametrize("title", ["Show (1x01-05 из 10)", "Show S01E01-E05 of 10"])
def test_the_episode_total_is_read_from_the_title_in_either_language(title):
    from tow.notify import _episode_text

    for lang in i18n.codes():
        assert _episode_text(title, lang) == i18n.translate("notify.episode_of", lang, episodes="S01E01–05", total=10)


def test_every_shipped_language_file_has_a_complete_meta():
    for path in sorted((SRC / "locales").glob("*.json")):
        meta = json.loads(path.read_text(encoding="utf-8")).get("_meta")
        assert i18n.meta_issues(meta) == (None, []), path.name


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{name} is {state}", "TOW is {state}"),  # a value not given stays visible
        ("a stray { brace", "a stray { brace"),
        ("a stray } brace {name}", "a stray } brace TOW"),
        ("{0} and {name.upper} and {name[0]}", "{0} and {name.upper} and {name[0]}"),
        ("{name!r} {name:>10}", "{name!r} {name:>10}"),
        ("{{name}}", "{TOW}"),
        ("{Name} {name}", "{Name} TOW"),
    ],
)
def test_filling_placeholders_never_raises(text, expected):
    assert i18n.fill(text, {"name": "TOW"}) == expected


def test_a_stray_brace_in_a_translation_does_not_break_the_page(locales):
    tree = json.loads((locales / "ru.json").read_text(encoding="utf-8"))
    tree["settings"]["page"]["title"] = "Настройки {0} {x.attr} {"
    (locales / "ru.json").write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")
    i18n.reload()
    assert i18n.translate("settings.page.title", "ru", n=1, x="y") == "Настройки {0} {x.attr} {"


def test_every_catalog_value_fills_with_any_values():
    """Every text of every language takes its values without an error and leaves none unfilled."""
    names = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
    for code in i18n.codes():
        for key in i18n.keys(code):
            raw = json.dumps(i18n._load(code)[0][key], ensure_ascii=False)
            params = dict.fromkeys(names.findall(raw), "<v>")
            for n in (0, 1, 2, 5, 21, 1.5):
                text = i18n.translate(key, code, **{**params, "n": n})
                assert not names.search(text), (code, key, text)


def test_reload_changes_the_version_caches_are_keyed_by():
    before = i18n.version()
    i18n.reload()
    assert i18n.version() != before


def test_the_colour_of_an_error_never_comes_from_a_language_file(monkeypatch, tmp_path):
    """Classification reads codes, never catalog texts: a reworded file changes no colour."""
    from tow import errors, log

    shipped = {code: errors.TowError(code).error_class for code in ("check.daily_limit", "client.managed.missing")}
    locales = tmp_path / "locales"
    locales.mkdir()
    for path in i18n.LOCALES_DIR.glob("*.json"):
        tree = json.loads(path.read_text(encoding="utf-8"))
        tree["check"]["daily_limit"] = "folder qbit cloudflare frozen"  # every legacy marker at once
        (locales / path.name).write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(i18n, "LOCALES_DIR", locales)
    i18n.reload()
    try:
        for code, cls in shipped.items():
            error = errors.TowError(code)
            assert log.error_class(error) == cls
            assert log.error_class(error.record()) == cls
            assert log.error_class(str(error), code=code) == cls
    finally:
        monkeypatch.undo()
        i18n.reload()


# --- the catalogs stay complete -------------------------------------------------------------

_KEY_RE = re.compile(r"""\bt\(\s*['"]([a-z0-9_]+(?:\.[a-z0-9_]+)+)['"]""")


def _used_keys() -> set[str]:
    used: set[str] = set()
    for path in [*SRC.rglob("*.py"), *(SRC / "templates").glob("*.html"), *(SRC / "static").glob("*.js")]:
        if path.name == "i18n.py":  # its docstring shows example keys
            continue
        used.update(_KEY_RE.findall(path.read_text(encoding="utf-8")))
    return used


def test_every_key_in_code_and_templates_exists_in_english():
    missing = sorted(key for key in _used_keys() if not i18n.has(key, "en"))
    assert missing == [], f"add these keys to src/tow/locales/en.json: {missing}"


def test_every_language_has_the_same_keys_as_english():
    english = i18n.keys("en")
    for code in i18n.codes():
        if code == "en":
            continue
        assert sorted(english - i18n.keys(code)) == [], f"{code}.json lacks keys"
        assert sorted(i18n.keys(code) - english) == [], f"{code}.json has keys English does not"


# Russian values that rightly have no Cyrillic or equal the English: names and formats.
_RU_AS_IN_ENGLISH = frozenset({"log.cls.cloudflare", "notifier.whatsapp.phone_placeholder", "check.two_errors"})


def test_every_russian_text_is_translated():
    cyrillic = re.compile("[а-яё]", re.IGNORECASE)
    english = i18n._load("en")[0]
    untranslated = []
    for key, value in sorted(i18n._load("ru")[0].items()):
        if key in _RU_AS_IN_ENGLISH:
            continue
        raw = json.dumps(value, ensure_ascii=False)
        if not cyrillic.search(raw) or value == english.get(key):
            untranslated.append(f"{key}: {raw}")
    assert untranslated == [], "translate these in src/tow/locales/ru.json:\n" + "\n".join(untranslated)
    assert all(key in english for key in _RU_AS_IN_ENGLISH)  # the allowlist stays current


_RU_JARGON = re.compile(
    r"\b(tracker|session|secrets?|read-back|scheduler|redirect|browser-auth|cross-origin|allowlist|origin)\b"
    r"|браузер-auth|\bэпизод",
    re.IGNORECASE,
)
# The owner is addressed formally ("укажите"), never "укажи".
_RU_INFORMAL = re.compile(
    r"(?<![а-яё])(укажи|нажми|выбери|проверь|введи|вставь|скопируй|сохрани|открой|замени|оставь|перезапусти"
    r"|задай|смотри|попробуй|добавь|удали|включи|выключи|отрежь|пиши|напиши|запусти|отправь|подключи|обнови"
    r"|сделай|создай|храни|войди|останови|заполни|верни|ты|тебе|твой|твои)(?![а-яё])",
    re.IGNORECASE,
)


def test_russian_texts_speak_plain_formal_russian():
    found = []
    for key, value in sorted(i18n._load("ru")[0].items()):
        text = re.sub(r"\{[a-z_]+\}", "", json.dumps(value, ensure_ascii=False))
        if _RU_JARGON.search(text) or _RU_INFORMAL.search(text):
            found.append(f"{key}: {text}")
    assert found == [], "\n".join(found)


def test_no_russian_verb_is_said_both_formally_and_informally():
    """Every imperative the catalog uses formally ("отрежьте", "пишите") must never appear in its
    informal form ("отрежь", "пиши") in any Russian value."""
    texts = {key: json.dumps(value, ensure_ascii=False) for key, value in i18n._load("ru")[0].items()}
    words = {word.lower() for text in texts.values() for word in re.findall(r"[а-яё]+", text, re.IGNORECASE)}
    formal = {word for word in words if re.search(r"(?:ите|йте|ьте)$", word)}
    assert {"укажите", "отрежьте", "пишите"} <= formal  # the scan sees them
    informal = {word[:-2] for word in formal}
    found = sorted(
        f"{key}: {word}"
        for key, text in texts.items()
        for word in re.findall(r"[а-яё]+", text, re.IGNORECASE)
        if word.lower() in informal
    )
    assert found == [], "\n".join(found)


@pytest.mark.parametrize(
    ("key", "cls"),
    [
        ("mirrors.redirect_cross_origin", "tracker"),
        ("check.add_unconfirmed", "qbit"),
        ("client.managed.wrong_folder", "no_path"),
        ("mirrors.tracker_daily_limit", "quota"),
        ("mirrors.all_paused", "tracker"),
    ],
)
def test_a_reworded_message_keeps_its_colour_in_every_language(key, cls):
    from tow.errors import TowError
    from tow.log import error_class

    error = TowError(key, tracker="rutor", seconds=5)
    for code in i18n.codes():
        i18n.use(code)
        assert error_class(error) == cls, code
        assert error_class(str(error), code=key) == cls, code


def test_placeholders_match_between_languages():
    """Both ways: a translation neither invents a value nor drops one (``{n}`` may be spelled out)."""
    pattern = re.compile(r"\{([a-z_]+)\}")
    for code in i18n.codes():
        for key in i18n.keys("en"):
            en = i18n._load("en")[0][key]
            other = i18n._load(code)[0].get(key)
            if other is None:
                continue
            en_names = set(pattern.findall(json.dumps(en, ensure_ascii=False)))
            other_names = set(pattern.findall(json.dumps(other, ensure_ascii=False)))
            assert other_names <= en_names | {"n"}, (code, key, other_names - en_names)
            assert en_names - {"n"} <= other_names, (code, key, en_names - other_names)


def test_no_stray_braces_in_any_language():
    """Only ``{name}`` placeholders: a lone brace or ``{0}`` shows up literally on the page."""
    for code in i18n.codes():
        for key, value in i18n._load(code)[0].items():
            for text in value.values() if isinstance(value, dict) else [value]:
                rest = re.sub(r"\{[a-z_][a-z0-9_]*\}", "", text)
                assert "{" not in rest, (code, key, text)
                assert "}" not in rest, (code, key, text)


# --- keys the code holds as data, and keys it builds -----------------------------------------

_KEY_SHAPE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+")
_FILE_NAME = re.compile(r"\.(py|json|jsonl|yaml|yml|html|js|css|txt|enc|key|towx|lock|exe|cmd|ps1|md|log|xml)$")
# Strings shaped like keys that are not texts: Deluge Web RPC method names.
_NOT_KEYS = frozenset({"auth.login", "web.connect", "web.connected", "web.get_host_status", "web.get_hosts"})


def _data_keys() -> dict[str, str]:
    """Every string in the code shaped like a key of a catalog section - label tables
    (_EVENT_LABELS, _ERROR_WORDS, ...), Field labels/placeholders, plugin STEPS/NOTE - wherever
    it is passed to t() later."""
    sections = {key.split(".", 1)[0] for key in i18n.keys("en")}
    found: dict[str, str] = {}
    for path in SRC.rglob("*.py"):
        if path.name == "i18n.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        in_fstrings = {id(part) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for part in node.values}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or id(node) in in_fstrings:
                continue
            value = node.value
            if _KEY_SHAPE.fullmatch(value) and value.split(".", 1)[0] in sections and not _FILE_NAME.search(value):
                found.setdefault(value, f"{path.name}:{node.lineno}")
    return {key: where for key, where in found.items() if key not in _NOT_KEYS}


def test_every_key_held_as_data_exists():
    keys = _data_keys()
    assert len(keys) > 400  # the scan sees the label tables and the plugins
    for key in ("notifier.whatsapp.phone_placeholder", "client.fields.host", "log.err.frozen"):
        assert key in keys
    missing = sorted(f"{key} ({where})" for key, where in keys.items() if not i18n.has(key))
    assert missing == [], f"add these keys to src/tow/locales/en.json: {missing}"


def _built_keys() -> dict[str, str]:
    """Every key the code builds with an f-string (``t(f"notify.action.{kind}")``) as its template."""
    found: dict[str, str] = {}
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.JoinedStr) and node.values):
                continue
            first = node.values[0]
            if not (isinstance(first, ast.Constant) and _KEY_SHAPE.match(str(first.value))):
                continue
            template = "".join(
                str(part.value) if isinstance(part, ast.Constant) else "{" + ast.unparse(part.value) + "}"
                for part in node.values
            )
            if template.split(".", 1)[0] in {key.split(".", 1)[0] for key in i18n.keys("en")}:
                found.setdefault(template, f"{path.name}:{node.lineno}")
    return found


def _families() -> dict[str, set[str]]:
    """Every value each built key can take, from the code that picks it."""
    from tow import log, notify, store

    return {
        "cli.keys.error.{exc.kind}": set(store.MASTER_KEY_KINDS),
        "log.kind.{kind}": set(),  # optional by design: kind_label() falls back to words
        "log.cls.{cls}": set(log._CLASSES),
        "notify.action.{kind}": set(notify._ACTIONS),
        "notify.client_{state}": {"down", "up"},
        "notify.client_{state}_named": {"down", "up"},
        "pulse.shutdown.{action}": {"restarted", "powered_off"},
        "settings.{section}.q": {"clients", "notify"},
        "notifier.{module.KIND}.title": set(),  # optional by design: the TITLE is used without it
        "backup.restore_point.{name}": {
            "rollback_failed",
            "validation_failed",
            "portable_invalid",
            "portable_check_failed",
            "cannot_create",
            "cannot_create_dir",
        },
    }


def test_every_key_the_code_builds_exists_in_every_language():
    families = _families()
    unknown = sorted(f"{template} ({where})" for template, where in _built_keys().items() if template not in families)
    assert unknown == [], f"list the values of these built keys in _families(): {unknown}"
    for template, values in families.items():
        for value in values:
            key = re.sub(r"\{[^}]+\}", value, template)
            for code in i18n.codes():
                assert i18n.has(key, code), (code, key)


# Keys nothing names literally, by design. Kept small: a new entry needs a reason.
_DYNAMIC_PREFIXES = (
    "log.kind.",  # the label of an event kind (kind_label); old kinds stay readable in the history
    "update.",  # read by scripts/update.py from its TEXTS keys (checked below)
)


def _update_script_texts() -> set[str]:
    tree = ast.parse((SRC.parents[1] / "scripts" / "update.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and [getattr(t, "id", "") for t in node.targets] == ["TEXTS"]:
            assert isinstance(node.value, ast.Dict)
            return {key.value for key in node.value.keys if isinstance(key, ast.Constant)}
    raise AssertionError("scripts/update.py has no TEXTS")


def test_every_english_key_is_used():
    """A catalog key nobody reads is a text nobody translates for a reason: it goes."""
    sources = [*SRC.rglob("*.py"), *(SRC / "templates").glob("*.html"), *(SRC / "static").glob("*.js")]
    quoted = set(re.findall(r"""['"]([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)['"]""", _read_all(sources)))
    built = {re.sub(r"\{[^}]+\}", value, template) for template, values in _families().items() for value in values}
    update_texts = _update_script_texts()
    unused = sorted(
        key
        for key in i18n.keys("en")
        if key not in quoted
        and key not in built
        and not key.startswith(_DYNAMIC_PREFIXES)
        and not (key.startswith("update.") and key.removeprefix("update.") in update_texts)
    )
    assert unused == [], f"remove these keys from every language file (or name them in the code): {unused}"
    stale = sorted(k for k in i18n.keys("en") if k.startswith("update.") and k[7:] not in update_texts)
    assert stale == [], f"scripts/update.py no longer says these: {stale}"


def _read_all(paths: list[Path]) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in paths if path.name != "i18n.py")


# --- every event the code logs has a label --------------------------------------------------


def _strings(node: ast.AST) -> set[str]:
    """The texts an expression can be: a literal, both sides of ``a if c else b``, ``"x_" + ...``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _strings(node.body) | _strings(node.orelse)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return {a + b for a in _strings(node.left) for b in _strings(node.right)}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return set().union(*(_strings(item) for item in node.elts))
    return set()


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _logged_kinds() -> dict[str, str]:
    """Every event kind the code can write to the log, with where: literal ``log_event`` /
    ``_record`` / ``run.record`` kinds, the kinds a loop logs, ``_change_clients`` events and the
    reconcile events progress.py collects."""
    found: dict[str, str] = {}
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            where = f"{path.name}:{getattr(node, 'lineno', 0)}"
            kinds: set[str] = set()
            if isinstance(node, ast.Call) and node.args:
                name = _call_name(node)
                if name in ("log_event", "_record", "record"):
                    kinds = _strings(node.args[0])
                elif name == "_change_clients" and len(node.args) > 2:
                    kinds = _strings(node.args[2])
                elif name == "append" and ast.unparse(node.func) == "events.append":
                    kinds = _strings(node.args[0])
            elif isinstance(node, ast.For) and ast.unparse(node.target) in ("kind", "event_kind"):
                kinds = _strings(node.iter)
            elif isinstance(node, ast.Assign) and any(ast.unparse(t) == "event_name" for t in node.targets):
                kinds = _strings(node.value)
            for kind in kinds:
                found.setdefault(kind, where)
    return found


def test_the_kind_scanner_sees_the_known_kinds():
    kinds = _logged_kinds()
    for kind in ("check", "backup_created", "watchdog_alert", "browser_auth_failed", "browser_auth_succeeded"):
        assert kind in kinds
    for kind in ("bot_delivery_succeeded", "settings_client_add", "revision_updated", "client_restored"):
        assert kind in kinds
    assert len(kinds) > 80


def test_every_logged_event_kind_has_a_label():
    from tow.log import HISTORY_GROUPS

    kinds = {**_logged_kinds(), **{kind: "log.HISTORY_GROUPS" for group in HISTORY_GROUPS.values() for kind in group}}
    missing = sorted(f"{kind} ({where})" for kind, where in kinds.items() if not i18n.has(f"log.kind.{kind}"))
    assert missing == [], f"add log.kind.<kind> to src/tow/locales/en.json: {missing}"


def test_history_shows_labels_not_identifiers():
    from tow.log import format_event, kind_label

    assert kind_label("backup_created", "en") == "nightly backup made"
    assert kind_label("some_future_kind", "en") == "some future kind"  # an older/newer log: words
    row = format_event({"kind": "check", "ok": 3, "n": 4, "apply": False, "ts": "2026-10-01T18:50:00+03:00"})
    assert row["label"] == i18n.translate("log.kind.check").capitalize()
    assert i18n.translate("log.check_result", ok=3, n=4) in row["detail"]
    assert i18n.translate("log.check_preview") in row["detail"]
    assert "apply=" not in row["detail"]
