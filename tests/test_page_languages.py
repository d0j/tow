"""Every page, filled with data, speaks one language: English shows no Russian, Russian no stray English.

The rest of the suite pins ``language: ru`` (tests/conftest.py). Here each page is rendered with
seeded topics (a site error, a client error, a site login needed, paused, new data, episodes in
progress), two torrent clients, connected messengers (one failing), history events of every group,
a stale check, a flash message and a live undo bar: in English (``language: auto`` and an English
browser) and in Russian. Seeded values are removed first: only what TOW itself writes must be in
the page's language.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tow import i18n
from tow.config import load_config, save_config
from tow.log import log_event
from tow.paths import data_dir
from tow.store import save_secrets, save_state

CYRILLIC = re.compile(r"[\u0400-\u04FF]+")
LATIN_WORD = re.compile(r"\b[A-Za-z][A-Za-z]{2,}\b")

# What the owner typed or a site/client returned: shown as it is, in any language.
TITLES = {
    "error": "Слово пацана / Word of the Boy [S01E01-08 из 08] (2023)",
    "client": "Northern Lights / Северное сияние [S02E01-05 из 10]",
    "paused": "Paused Show / Пауза",
    "new": "Done Film (2024)",
    "login": "Login Needed",
    "history": "Seeded History Title",
    "undo": "Old Topic",
}
ERRORS = {
    "tracker": "rutor: all hosts failed",
    "client": "qBittorrent: Connection refused",
    "login": "login required",
    "history": "timeout",
    "delivery": "HTTP 401",
}
CLIENT_IDS = ("main", "nas", "NAS")
SEEDED = (
    *TITLES.values(),
    *(title.split(" / ")[0] for title in TITLES.values()),
    *ERRORS.values(),
    *CLIENT_IDS,
    "Вход",  # config.example.yaml: the value of a site's login button, sent to the site as is
)

# English words a Russian page shows on purpose. Each is a name, a command, a code or an example,
# not an untranslated text.
RU_ALLOWED = {
    # products and services
    *("TOW", "Torrent", "Watcher", "qBittorrent", "qBit", "Transmission", "Deluge", "WebUI", "Python"),
    *("Telegram", "BotFather", "WhatsApp", "Discord", "ntfy", "Tailscale", "Cloudflare", "Magnet", "magnet"),
    *("Google", "Play", "App", "Store", "Droid", "Windows", "Linux", "macOS"),
    "Update",  # the update file in the TOW folder: "Update TOW.cmd", "Update TOW.command"
    # client kinds as TOW names them (the "kind" in the card's technical details)
    *("qbittorrent", "transmission", "deluge"),
    # site names and titles from config.example.yaml (data)
    *("rutor", "kinozal", "nnmclub", "rutracker", "tapochek", "unionpeer", "fast_torrent", "nnm"),
    *("Kinozal", "NNM", "Club", "RuTracker", "Tapochek", "UnionPeer", "Rutor", "Fast"),
    # abbreviations Russian uses as they are
    *("LAN", "URL", "API", "PID"),
    *("CallMeBot", "apikey"),  # the WhatsApp gateway and its word for its key
    "tow",  # the tag TOW puts on its torrents in the client
    # what the owner must type or press elsewhere, exactly like this
    "Start",  # Telegram's button
    "newbot",  # the /newbot command
    *("permissions", "fix", "sudo"),  # `tow permissions fix` in the guide, with sudo on Linux and macOS
    *("allow", "callmebot", "send", "messages"),  # CallMeBot's activation phrase
    # keyboard keys named in the help
    *("Enter", "Esc", "Tab", "Ctrl", "Shift"),
    # time-zone abbreviations next to times (IDT UTC+03:00)
    *("UTC", "IDT", "IST", "GMT", "CET", "CEST", "MSK", "EET", "EEST"),
    # file name examples in the selection help (*.mkv, Subs/*.srt)
    *("Subs", "mkv", "srt"),
    "regex",
    # the language picker names each language in English too
    *("Language", "English", "Russian"),
    # the technical summary of a check run in the journal (ok 3/4 apply=True)
    *("apply", "True", "False"),
    # scheduled task names (TOW-check, TOW-serve, ...)
    *("check", "serve", "backup", "watchdog", "progress"),
    # CLI commands the help names (tow export / tow import)
    *("export", "import"),
    # Literal updater command shown in the manual alternative, not translated prose.
    *("update", "ref", "latest"),
}

# Russian catalog entries that still carry an English word (ru.json, 1.17 i18n item "untranslated
# Russian entries"): checked apart below, so the main check guards everything else meanwhile.
RU_KNOWN_LEAKS = {
    "secrets": "js.clock.secrets_migration_required",
    "tracker": "log.cls.tracker_auth",
    "torrent": "home.add.check_hint",
}


def _when(minutes_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


def _topics() -> list[dict]:
    return [
        {
            "id": "t-error",
            "title": TITLES["error"],
            "url": "http://rutor.info/torrent/1/x",
            "save_path": "D:/Video",
            "last_ok": False,
            "last_error": ERRORS["tracker"],
            "last_error_class": "tracker",
        },
        {
            "id": "t-client",
            "title": TITLES["client"],
            "url": "https://rutracker.org/forum/viewtopic.php?t=2",
            "save_path": "D:/Video",
            "client_id": "nas",
            "last_ok": False,
            "last_error": ERRORS["client"],
            "last_error_class": "qbit",
        },
        {
            "id": "t-login",
            "title": TITLES["login"],
            "url": "https://nnmclub.to/forum/viewtopic.php?t=5",
            "save_path": "D:/Video",
            "last_ok": False,
            "last_error": ERRORS["login"],
            "last_error_class": "tracker_auth",
        },
        {
            "id": "t-paused",
            "title": TITLES["paused"],
            "url": "https://kinozal.guru/details.php?id=3",
            "save_path": "D:/Video",
            "paused": True,
            "last_ok": True,
        },
        {
            "id": "t-new",
            "title": TITLES["new"],
            "url": "http://rutor.info/torrent/4/y",
            "save_path": "D:/Films",
            "last_ok": True,
            "last_changed": True,
        },
    ]


def _history() -> dict:
    items = {
        f"episode:s02e0{n}": {
            "identity": f"episode:s02e0{n}",
            "kind": "episode",
            "label": f"S02E0{n}",
            "status": "completed" if n < 3 else "downloading",
            "progress": 1.0 if n < 3 else 0.4,
        }
        for n in range(1, 6)
    }
    last_event = {"kind": "episode_completed", "label": "S02E02", "at": _when(30)}
    return {"schema_version": 1, "topics": {"t-client": {"items": items, "last_event": last_event}}}


_EVENTS = (
    ("client_added", {"title": TITLES["history"], "how": "auto"}),
    ("file_completed", {"title": TITLES["history"], "path": "D:/Video/e01.mkv"}),
    ("episode_completed", {"title": TITLES["history"]}),
    ("check_fail", {"title": TITLES["history"], "error": ERRORS["history"], "cls": "tracker", "how": "auto"}),
    ("client_add_failed", {"title": TITLES["history"], "error": "qbit down", "cls": "qbit"}),
    ("topic_add", {"title": TITLES["history"], "how": "manual"}),
    ("topic_delete", {"title": TITLES["history"], "how": "manual"}),
    ("undo", {"how": "manual"}),
    ("site_add", {"tracker": "rutor", "how": "manual"}),
    ("settings_portable_restore", {"how": "manual"}),
    ("bot_delivery_succeeded", {"title": TITLES["history"]}),
    ("bot_delivery_failed", {"title": TITLES["history"], "error": ERRORS["delivery"]}),
    ("check", {"ok": 3, "n": 4, "apply": True, "how": "auto"}),
)


def _seed(language: str, *, first_start: bool = False) -> None:
    cfg = load_config()
    cfg["language"] = language
    cfg["setup_done"] = not first_start
    cfg.pop("client", None)
    cfg["clients"] = [
        {"id": "main", "kind": "qbittorrent", "default": True},
        {"id": "nas", "kind": "transmission", "title": "NAS"},
    ]
    save_config(cfg)
    secrets: dict = {
        "clients": {
            "main": {"host": "127.0.0.1", "port": 8080, "username": "admin", "password": "fixture"},
            "nas": {"host": "nas.example", "port": 9091},
        },
        "telegram": {"token": "123:fixture", "chat_ids": ["42"]},
        "notifiers": {"ntfy": {"topic": "tow-fixture"}},
    }
    if not first_start:
        secrets["lan_auth"] = {"salt": "fixture", "hash": "fixture"}
    save_secrets(secrets)
    now = datetime.now(UTC)
    three_days_ago = int(now.timestamp()) - 3 * 86400
    save_state(
        {
            "topics": _topics(),
            "mirrors": {"rutor": {"active": "http://rutor.info"}},
            "health": {"at_ts": three_days_ago, "auto_at_ts": three_days_ago, "qbit_ok": False},
            "notify_status": {"telegram:42": {"ok": False, "error": ERRORS["delivery"], "at": int(now.timestamp())}},
            "undo": {"kind": "topic", "item": {"id": "t-old", "title": TITLES["undo"]}, "ts": now.isoformat()},
        }
    )
    (data_dir() / "download_history.json").write_text(json.dumps(_history()), encoding="utf-8")
    for kind, fields in _EVENTS:
        log_event(kind, **fields)


def _client(language: str) -> TestClient:
    from tow.web import app

    accept = "en-US,en;q=0.9" if language == "en" else "ru-RU,ru;q=0.9"
    return TestClient(app, headers={"Origin": "http://127.0.0.1", "Accept-Language": accept, "Accept": "text/html"})


PAGES = ("/", "/sites", "/settings", "/settings/help", "/doctor", "/history", "/history?group=errors", "/login")


def _pages(language: str) -> dict[str, str]:
    from tow.web.views import flash_location

    client = _client(language)
    i18n.use(language)  # the message is rendered for this page's reader
    done = flash_location("/", "web.settings.restored", "ok")  # a message
    failed = flash_location("/settings?open=transfer", "web.settings.rollback_critical", "err")  # red, stays
    warned = flash_location("/sites", "web.sites.password_again", "warn")  # amber, stays
    out = {}
    for path in (*PAGES, done, failed, warned):
        response = client.get(path)
        assert response.status_code == 200, (path, response.status_code)
        out[path] = response.text
    return out


def _visible_text(html: str) -> str:
    """Text a person reads: the page text, tooltips, placeholders, labels and the app.js texts."""
    soup = BeautifulSoup(html, "html.parser")
    parts = []
    for script in soup.find_all("script"):
        if script.get("type") == "application/json" and script.string:
            data = json.loads(script.string)
            if isinstance(data, dict):
                parts.extend(str(value) for value in data.values())
        script.decompose()
    for style in soup.find_all("style"):
        style.decompose()
    for tag in soup.find_all(True):
        attributes = ("title", "placeholder", "aria-label", "alt", "data-confirm")
        parts.extend(str(tag[name]) for name in attributes if tag.get(name))
        if tag.name == "input" and tag.get("type") in ("submit", "button") and tag.get("value"):
            parts.append(str(tag["value"]))
    parts.append(soup.get_text(" "))
    text = " ".join(parts)
    for value in sorted(SEEDED, key=len, reverse=True):
        text = text.replace(value, " ")
    return text


def _russian_words(html: str) -> list[str]:
    natives = {native for _code, _name, native in i18n.available()}  # the language picker
    return sorted(set(CYRILLIC.findall(_visible_text(html))) - natives)


def _all_english_words(html: str) -> set[str]:
    text = _visible_text(html)
    # technical tokens: links, paths, host and file names, regexes, @bots, {placeholders}, .towx
    text = " ".join(token for token in text.split() if not re.search(r"[/\\@{}^$?=*]|\w\.\w|^\.\w", token))
    text = re.sub(r"\bTOW-\w+", " ", text)  # scheduled task names
    return {word for word in LATIN_WORD.findall(text) if word not in RU_ALLOWED}


def _english_words(html: str) -> list[str]:
    return sorted(_all_english_words(html) - set(RU_KNOWN_LEAKS))


def _assert_seeded_data_is_shown(pages: dict[str, str]) -> None:
    """The check means something only when the pages really show the data."""
    home = pages["/"]
    assert all(title.split(" / ")[0] in home for title in (TITLES["error"], TITLES["client"], TITLES["new"]))
    assert 'id="undo-form"' in home
    assert 'class="attention card"' in home  # the stale check and the client down
    assert any('id="flash"' in html for path, html in pages.items() if "flash=" in path)
    assert TITLES["history"] in pages["/history"]
    assert "NAS" in pages["/settings"]
    assert "notifier-card" in pages["/settings"]


def test_every_page_in_english_has_no_russian():
    _seed("auto")
    pages = _pages("en")
    assert all('<html lang="en">' in html for html in pages.values())
    _assert_seeded_data_is_shown(pages)
    found = {path: words for path, html in pages.items() if (words := _russian_words(html))}
    assert found == {}


def test_every_page_in_russian_has_no_stray_english():
    _seed("ru")
    pages = _pages("ru")
    assert all('<html lang="ru">' in html for html in pages.values())
    _assert_seeded_data_is_shown(pages)
    found = {path: words for path, html in pages.items() if (words := _english_words(html))}
    assert found == {}


def test_russian_catalog_entries_have_no_english_words():
    assert all(i18n.has(key, "ru") for key in RU_KNOWN_LEAKS.values())  # the guard is about real keys
    _seed("ru")
    leaks = set().union(*(_all_english_words(html) for html in _pages("ru").values()))
    assert sorted(leaks & set(RU_KNOWN_LEAKS)) == []


@pytest.mark.parametrize("language", ["en", "ru"])
def test_every_history_event_has_a_label(language):
    from tow.log import HISTORY_GROUPS

    _seed("auto" if language == "en" else language)
    kinds = sorted(set().union(*HISTORY_GROUPS.values()))
    for kind in kinds:
        log_event(kind, how="auto")
    html = _client(language).get("/history").text
    assert sorted(kind for kind in kinds if re.search(rf">\s*{kind}\s*<", html)) == []


@pytest.mark.parametrize(("language", "setting"), [("en", "auto"), ("ru", "ru")])
def test_first_start_page_speaks_one_language(language, setting):
    _seed(setting, first_start=True)
    html = _client(language).get("/setup").text
    assert f'<html lang="{language}">' in html
    assert (_russian_words(html) if language == "en" else _english_words(html)) == []


def test_css_shows_no_russian_labels():
    css = _client("en").get("/static/app.css").text
    labels = re.findall(r'content:\s*"([^"]*)"', css)
    assert [label for label in labels if CYRILLIC.search(label)] == []
