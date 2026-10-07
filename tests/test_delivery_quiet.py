"""No messenger connected: a check neither tries nor logs a delivery."""

from __future__ import annotations

import json

from tow.check.notices import _audited_send
from tow.paths import data_dir


def _kinds() -> list[str]:
    path = data_dir() / "tow.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["kind"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_without_a_messenger_nothing_is_sent_or_logged(monkeypatch):
    monkeypatch.setattr("tow.notifiers.deliver_queued", lambda _s: (_ for _ in ()).throw(AssertionError("sent")))
    assert _audited_send({}, text="x", operation_id="op") is False
    assert not [kind for kind in _kinds() if kind.startswith("bot_delivery")]


def test_with_a_messenger_the_delivery_is_logged(monkeypatch):
    monkeypatch.setattr("tow.notifiers.connected", lambda _s: [("telegram", None, None)])
    monkeypatch.setattr("tow.notifiers.deliver_queued", lambda _s: {"telegram": (True, "")})
    assert _audited_send({}, text="x", operation_id="op") is True
    assert [kind for kind in _kinds() if kind.startswith("bot_delivery")] == [
        "bot_delivery_started",
        "bot_delivery_succeeded",
    ]
