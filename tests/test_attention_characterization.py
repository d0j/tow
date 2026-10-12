"""What Home's "Needs attention" lists, pinned before its rules move into ``tow.issues``.

Synthetic data reaches every item the banner can show today: the data folder lost, scheduled
checks late, a blocked or failed check, the torrent client down, the download history started
over, and sites that ask for a sign-in (per site, counted over topics that are not paused). The
same data must give the same items afterwards; only the wording that sent the reader elsewhere
may change, and only where this file says so.
"""

from __future__ import annotations

import time

import pytest

from tow.config import load_config
from tow.store import load_state, save_state
from tow.web import services, views

NNM = "https://nnmclub.to/forum/viewtopic.php?t="
BLOCKED = (
    "Последняя плановая проверка заблокирована: сохранённые пароли и токены не открываются. Что делать, "
    "показано в Настройках."
)
HISTORY_REBUILT = (
    "Файл истории загрузок был повреждён: TOW начал историю заново и продолжает проверки (повреждённый файл "
    "сохранён рядом как download_history.json.corrupt-…). Загрузки не затронуты; прежние события не "
    "показываются."
)


def _topic(tid: str, url: str, **extra) -> dict:
    return {"id": tid, "title": tid.upper(), "url": url, **extra}


def _everything_but_the_lost_folder() -> dict:
    return {
        "topics": [
            _topic("a", NNM + "1", last_error_class="tracker_auth", last_error="x"),
            _topic("b", NNM + "2", last_error_class="tracker_auth", last_error="x"),
            _topic("c", NNM + "3", last_error_class="tracker_auth", last_error="x", paused=True),
            _topic("d", "https://unknown.example/t/4", last_error_class="tracker_auth", last_error="x"),
            _topic("e", NNM + "5", last_error_class="qbit", last_error="y"),
        ],
        "mirrors": {},
        "health": {
            "auto_at_ts": int(time.time()) - 10 * 24 * 3600,
            "qbit_ok": False,
            "check_ok": False,
            "check_error": "secrets_migration_required",
            "history_rebuilt_at": int(time.time()) - 3600,
        },
    }


def _items(state: dict) -> list[str]:
    save_state(state)
    return views.attention(load_state(), load_config())


def test_every_item_of_today_in_its_order():
    items = _items(_everything_but_the_lost_folder())

    assert [item.split(" ")[0] for item in items] == [
        "Плановые",
        "Последняя",
        "Торрент-клиент",
        "Файл",
        "Нужен",
        "Нужен",
    ]
    assert items[0].startswith("Плановые проверки не выполнялись с ")
    assert items[1:] == [
        BLOCKED,
        "Торрент-клиент недоступен: новые версии не добавляются.",
        HISTORY_REBUILT,
        "Нужен вход на сайт NNM-Club (раздач: 2): откройте строку раздачи.",
        "Нужен вход на сайт сайт (раздач: 1): откройте строку раздачи.",  # the unknown site, as today
    ]


def test_a_check_that_failed_for_another_reason_is_listed_the_same_way():
    state = {"topics": [], "mirrors": {}, "health": {"auto_at_ts": int(time.time()), "check_ok": False}}

    assert _items(state) == [BLOCKED]


def test_the_lost_data_folder_is_listed_only_while_no_topic_is_watched(monkeypatch):
    monkeypatch.setattr(services, "data_lost", lambda: {"at": 1, "folder": "D:\\TOW\\data"})
    lost = _items({"topics": [], "mirrors": {}, "health": {"auto_at_ts": int(time.time())}})

    assert len(lost) == 1
    assert lost[0].startswith("Папка данных TOW (D:\\TOW\\data)")
    watched = {"topics": [_topic("a", NNM + "1")], "mirrors": {}, "health": {"auto_at_ts": int(time.time())}}
    assert _items(watched) == []


@pytest.mark.parametrize(
    "health",
    [
        {"auto_at_ts": int(time.time())},
        {"auto_at_ts": int(time.time()), "qbit_ok": True, "check_ok": True},
        {},  # never checked: nothing is late yet
    ],
)
def test_nothing_is_listed_when_all_is_well(health):
    assert _items({"topics": [_topic("a", NNM + "1")], "mirrors": {}, "health": health}) == []
