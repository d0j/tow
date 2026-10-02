"""download_history.json no longer grows forever (tow.download_history)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tow.download_history import DEFAULT_KEEP_DAYS, DEFAULT_MAX_ITEMS, prune, retention

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _item(days_ago: float, *, superseded: bool, completed: float | None = None) -> dict:
    item = {"first_seen_at": _at(days_ago), "superseded": superseded, "status": "completed"}
    if completed is not None:
        item["completed_observed_at"] = _at(completed)
    return item


def test_defaults_are_generous_and_the_config_can_change_them():
    assert retention({}) == (DEFAULT_KEEP_DAYS, DEFAULT_MAX_ITEMS) == (730, 5000)
    assert retention({"history_keep_days": 30, "history_max_items": 100}) == (30, 100)
    assert retention({"history_keep_days": 0}) == (0, DEFAULT_MAX_ITEMS)  # 0 turns the limit off
    for wrong in (-1, "30", True, None, 1.5):
        assert retention({"history_keep_days": wrong}) == (DEFAULT_KEEP_DAYS, DEFAULT_MAX_ITEMS)


def test_old_revisions_go_after_the_keep_time_current_files_never():
    history = {
        "topics": {
            "t": {
                "last_scan_at": _at(0),
                "items": {
                    "old-revision": _item(800, superseded=True),
                    "old-but-completed-lately": _item(800, superseded=True, completed=10),
                    "recent-revision": _item(10, superseded=True),
                    "current-ancient": _item(3000, superseded=False),
                    "no-date": {"superseded": True},
                },
            }
        }
    }
    assert prune(history, watched={"t"}, now=NOW) is True
    assert set(history["topics"]["t"]["items"]) == {
        "old-but-completed-lately",
        "recent-revision",
        "current-ancient",
        "no-date",
    }
    assert prune(history, watched={"t"}, now=NOW) is False  # nothing more to drop


def test_a_topic_no_longer_watched_keeps_its_record_for_the_keep_time():
    history = {
        "topics": {
            "deleted-long-ago": {"last_scan_at": _at(800), "items": {}},
            "deleted-lately": {"last_scan_at": _at(5), "items": {}},
            "watched-but-old-scan": {"last_scan_at": _at(800), "items": {}},
            "never-scanned": {"items": {}},
        }
    }
    assert prune(history, watched={"watched-but-old-scan"}, now=NOW) is True
    assert set(history["topics"]) == {"deleted-lately", "watched-but-old-scan", "never-scanned"}


def test_beyond_the_item_limit_the_oldest_superseded_go_first():
    items = {f"old-{n}": _item(100 + n, superseded=True) for n in range(5)}
    items.update({f"live-{n}": _item(1, superseded=False) for n in range(3)})
    history = {"topics": {"t": {"items": items}}}
    assert prune(history, watched={"t"}, now=NOW, max_items=5) is True
    kept = set(history["topics"]["t"]["items"])
    assert kept == {"live-0", "live-1", "live-2", "old-0", "old-1"}  # old-4..old-2 were the oldest


def test_live_items_are_kept_even_above_the_limit():
    history = {"topics": {"t": {"items": {f"live-{n}": _item(1, superseded=False) for n in range(4)}}}}
    assert prune(history, watched={"t"}, now=NOW, max_items=2) is False
    assert len(history["topics"]["t"]["items"]) == 4


def test_limits_turned_off_keep_everything():
    history = {
        "topics": {
            "gone": {"last_scan_at": _at(9000), "items": {"x": _item(9000, superseded=True)}},
        }
    }
    assert prune(history, watched=set(), now=NOW, keep_days=0, max_items=0) is False
    assert "x" in history["topics"]["gone"]["items"]


def test_a_broken_history_is_left_alone():
    assert prune({"topics": []}, watched=set(), now=NOW) is False
    history = {"topics": {"t": "broken", "u": {"items": "broken"}}}
    assert prune(history, watched=set(), now=NOW) is False
