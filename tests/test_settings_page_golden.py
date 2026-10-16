"""The settings page with two torrent clients and one messenger, pinned as HTML:
``tests/golden/settings-page.html``.

The page is rendered from fixed stores (the service, the copies of the data and the log are
replaced in ``tow.web.services``); what changes from run to run or from one computer to another
- the install folder, the version, the asset stamp, the times, the free disk space, a random
suggestion - is replaced by a placeholder. Moving how the page is built (which module makes a card, which helper reads
what) must not change a byte of it. A change to the page itself changes this text: if intended,
regenerate it and review the diff:

    uv run --frozen python tests/test_settings_page_golden.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

GOLDEN = Path(__file__).resolve().parent / "golden" / "settings-page.html"
RENDERED: list[str] = []

CONFIG = {
    "language": "en",
    "clients": [
        {"id": "main", "kind": "qbittorrent", "title": "Living room", "default": True},
        {"id": "nas", "kind": "transmission"},
    ],
}
SECRETS = {
    "clients": {
        "main": {"host": "192.0.2.10", "port": 8080, "username": "admin", "password": "pw"},
        "nas": {"host": "http://nas.example:9091/transmission", "port": 9091, "username": "", "password": ""},
    },
    "telegram": {"token": "123456:fake-token", "chat_ids": ["1001"]},
}
TOPICS = [
    {"id": "1234567", "url": "https://tracker.example/topic/1234567", "title": "Show A", "save_path": "D:/TV"},
    {"id": "7654321", "url": "https://tracker.example/topic/7654321", "title": "Show B", "client_id": "nas"},
    {"id": "1111111", "url": "https://tracker.example/topic/1111111", "title": "Show C", "client_id": "main"},
]
SERVICE = {
    "pid": 4321,
    "autostart": True,
    "supports_without_login": False,
    "task": {"status": "Ready", "last_run_result": "0"},
    "supervisor": {"server": {"up_since": "2026-10-02T19:59:53+00:00"}},
    "restart": {"status": "started", "operation_id": "restart-test", "created_at": "2026-09-24T09:15:24+00:00"},
}
EVENTS = [
    {"ts": "2026-10-01T18:50:00+03:00", "kind": "settings_client_add", "client_kind": "transmission", "how": "manual"},
    {"ts": "2026-10-01T18:40:00+03:00", "kind": "check", "ok": 3, "n": 3, "apply": True},
]
RESTORE_POINTS = [{"id": "rp-1", "name": "rp-1", "created_at": "2026-10-01T10:00:00+00:00", "bytes": 2048}]
CLEANUP = {"pending": False, "read_error": False}


def _normalised(page: str) -> str:
    from tow import __version__, paths
    from tow.web.templating import STATIC_VERSION

    root = str(paths.root())
    for form in (root, root.replace("\\", "\\\\"), root.replace("\\", "/")):
        page = page.replace(form, "<root>")
    page = page.replace(STATIC_VERSION, "<static>").replace(__version__, "<version>")
    # A time in the computer's own zone; the free space of its disk; ntfy's suggested random topic.
    page = re.sub(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?( [A-Z]{2,5})?( UTC([+-]\d{2}:\d{2})?)?", "<time>", page)
    # A time by the one rule (short_time: "20:31" today, "15 Oct, 20:31" another day); the page
    # script's TOW zone, whose offset changes with summer time.
    page = re.sub(r'<time datetime="[^"]*" title="[^"]*" data-short>[^<]*</time>', "<short-time>", page)
    page = re.sub(r'"_tz_now": "-?\d+"', '"_tz_now": "<offset>"', page)
    page = re.sub(r'"_tz_name": "[^"]*"', '"_tz_name": "<zone>"', page)  # the computer's own zone
    page = re.sub(r"[\d.,]+ (B|KB|MB|GB|TB) free", "<size> free", page)
    page = re.sub(r'value="tow-[0-9a-f]{12}"', 'value="tow-<random>"', page)
    # A saved password's comparison token in a form's base: keyed by the server process.
    page = re.sub(r"&#34;[0-9a-f]{8}\.[0-9a-f]{32}&#34;", "&#34;<secret-token>&#34;", page)
    # Where the tab keeps its recent messages: a hash of this install's folders.
    page = re.sub(r'data-namespace="[0-9a-f]{16}"', 'data-namespace="<install>"', page)
    page = re.sub(r"<root>[^\"<]*", lambda m: m.group(0).replace("\\", "/"), page)
    # Without the spaces the templates leave at line ends (the repository refuses trailing spaces).
    return "\n".join(line.rstrip() for line in page.strip().splitlines()) + "\n"


def render(monkeypatch) -> str:
    from fastapi.testclient import TestClient

    from tow.config import load_config, save_config
    from tow.store import load_state, save_secrets, save_state
    from tow.web import app

    cfg = load_config()
    cfg.update(CONFIG)
    save_config(cfg)
    save_secrets(SECRETS)
    state = load_state()
    state["topics"] = TOPICS
    save_state(state)
    monkeypatch.setattr("tow.platform.is_windows", lambda: False)
    monkeypatch.setattr("tow.web.services.service_status", lambda: dict(SERVICE))
    monkeypatch.setattr("tow.web.services.watchdog_problem", lambda: None)
    monkeypatch.setattr("tow.web.services.stale_autostart", lambda _detail: False)
    monkeypatch.setattr("tow.web.services.read_events", lambda limit=200: [dict(e) for e in EVENTS])
    monkeypatch.setattr("tow.web.services.list_restore_points", lambda: [dict(p) for p in RESTORE_POINTS])
    monkeypatch.setattr("tow.web.services.restore_point_cleanup_status", lambda cfg=None: dict(CLEANUP))
    monkeypatch.setattr("tow.web.services.night_cleanup_status", lambda cfg=None: dict(CLEANUP))
    page = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/settings")
    assert page.status_code == 200
    return _normalised(page.text)


def test_the_settings_page_is_built_as_before(monkeypatch):
    page = render(monkeypatch)
    assert page.count('action="/settings/client"') == 2  # one form per client
    assert 'id="notifier-telegram"' in page
    RENDERED[:] = [page]  # for the regeneration below; the test itself writes only in its own folder
    expected = GOLDEN.read_text(encoding="utf-8") if GOLDEN.is_file() else ""
    if page != expected:
        changed = [
            f"- {old}\n+ {new}"
            for old, new in zip(expected.splitlines(), page.splitlines(), strict=False)
            if old != new
        ][:10]
        raise AssertionError(
            "the settings page changed; if intended, regenerate and review the diff "
            "(uv run --frozen python tests/test_settings_page_golden.py):\n"
            + "\n".join(changed or ["(more or fewer lines)"])
        )


if __name__ == "__main__":
    import pytest

    # Rendered by the test, in its own environment (a throwaway install), never the checkout's data.
    pytest.main([f"{__file__}::test_the_settings_page_is_built_as_before", "-q", "-p", "no:cacheprovider"])
    tested = next(m for m in list(sys.modules.values()) if getattr(m, "__file__", None) == __file__ and m.RENDERED)
    GOLDEN.write_text(tested.RENDERED[0], encoding="utf-8", newline="\n")
    print(f"wrote {GOLDEN}")
