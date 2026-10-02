import json
import multiprocessing
import os
from pathlib import Path

import pytest
from helpers import reaped

from tow.log import (
    MAX_BYTES,
    error_class,
    export_event_projection,
    format_event,
    log_event,
    log_path,
    read_events,
)


def _multiprocess_log_writer(home: str, marker: str, count: int, ready, start) -> None:
    os.environ["TOW_HOME"] = home
    ready.put(marker)
    start.wait()
    for index in range(count):
        log_event("multiprocess", marker=f"{marker}-{index}", payload="x" * 5_000)


def test_log_roundtrip_no_secrets():
    log_event("check_fail", topic="abc", error="not a torrent", password="secret")
    rows = read_events(limit=10)
    assert rows
    assert rows[0]["kind"] == "check_fail"
    assert rows[0]["password"] == "***"
    fmt = format_event(rows[0])
    assert fmt["label"] == "Проверка не удалась"
    assert "not a torrent" in fmt["detail"]
    assert "." in fmt["at"]


def test_export_event_projection_rejects_hash_suffix_data():
    projected = export_event_projection({"hash": "a" * 64 + "SECRET"})
    assert "hash" not in projected


def test_format_event_scrubs_credential_bearing_urls():
    event = {
        "kind": "check_fail",
        "error": "request failed https://user:pass@example.test/x?token=secret",
        "url": "https://user:pass@example.test/x?token=secret",
    }

    rendered = format_event(event)

    assert "pass" not in rendered["detail"]
    assert "secret" not in rendered["detail"]


def test_format_event_omits_malformed_hash():
    rendered = format_event({"kind": "downloaded", "hash": "a" * 40 + "SECRET"})

    assert "aaaaaaaa" not in rendered["detail"]
    assert "SECRET" not in rendered["detail"]


def test_format_error_human():
    fmt = format_event({"ts": "2026-09-11T18:00:00+00:00", "kind": "check_fail", "error": "frozen", "title": "Show"})
    assert fmt["label"] == "Проверка не удалась"
    assert "Show" in fmt["detail"]
    assert "зеркало на паузе" in fmt["detail"]


def test_format_debug_fields():
    fmt = format_event(
        {
            "ts": "2026-09-11T18:00:00+00:00",
            "kind": "downloaded",
            "title": "Show",
            "tracker": "rutor",
            "hash": "ABCDEF12" + "0" * 32,
            "path": r"M:\TV",
            "url": "http://rutor.info/torrent/1/x",
        }
    )
    assert fmt["label"] == "Передано в клиент"
    assert "Show" in fmt["detail"]
    assert "rutor" in fmt["detail"]
    assert "ABCDEF12" in fmt["detail"]
    assert r"M:\TV" in fmt["detail"]
    assert "rutor.info" in fmt["detail"]
    assert fmt["at"].startswith("11.09.2026 21:00:00 ")


def test_error_class():
    assert error_class("лимит скачиваний на сегодня") == "quota"
    assert error_class("frozen") == "frozen"
    assert error_class("qbit down") == "qbit"
    assert error_class("not a torrent") == "not_torrent"
    assert error_class("nnmclub: no download link on page") == "tracker_auth"
    assert error_class("nnmclub: all hosts failed: не torrent (нужен вход?)") == "tracker_auth"
    assert error_class("tracker_auth") == "tracker_auth"
    assert error_class("http 403: tracker auth") == "tracker_auth"
    assert error_class("http 401") == "auth"


@pytest.mark.parametrize(
    ("message", "cls"),
    [
        # Only the site's daily download limit is "quota" (amber); these are not.
        ("Лимитированная серия: торрент-клиент: нет связи", "qbit"),
        ("OSError: [Errno 122] Disk quota exceeded", "error"),
        ("Безлимитный тариф", "error"),
        # ...and every way TOW writes the real one is.
        ("kinozal: лимит скачиваний на сегодня", "quota"),
        ("kinozal: daily download limit reached", "quota"),
        ("all hosts failed: лимит скачиваний на сегодня", "quota"),
        # Every mirror answers "gone" (410) like "not found" (404): the topic was removed.
        ("rutor: all hosts failed: http 410", "gone"),
        ("rutor: all hosts failed: http 404", "gone"),
        ("rutor: all hosts failed: http 503", "tracker"),
    ],
)
def test_error_class_quota_and_gone_are_exact(message, cls):
    assert error_class(message) == cls


@pytest.mark.parametrize(
    ("message", "cls"),
    [
        # B5: owner content in the tail must not pick the class (and the Home colour).
        (
            (
                "previous torrent revision is still active on an overlapping file: Frozen.Planet.II.S01E02.mkv"
                " — остановите прежнюю раздачу в клиенте"
            ),
            "qbit",
        ),
        ("reconcile: previous torrent revision is still active on an overlapping file: Room.401.mkv", "qbit"),
        ("torrent hash is already claimed by an incompatible topic: Безлимитный сезон", "error"),
        ("cannot read file Room.401.2026.mkv", "error"),
        (r"cannot write D:\Frozen\Planet\ep1.mkv", "error"),
        ("mirror frozen until 12:00", "frozen"),
    ],
)
def test_error_class_ignores_owner_content(message, cls):
    assert error_class(message) == cls


def test_format_nnmclub_auth_failure_is_explicit():
    fmt = format_event(
        {
            "kind": "check_fail",
            "cls": "tracker_auth",
            "tracker": "nnmclub",
            "error": "nnmclub: no download link on page",
        }
    )

    assert "вход на сайт" in fmt["detail"]
    assert "ссылка .torrent не выдана" in fmt["detail"]


def test_log_rotates(monkeypatch):
    import tow.log as logmod

    monkeypatch.setattr(logmod, "MAX_BYTES", 80)
    p = log_path()
    p.write_text("x" * 120 + "\n", encoding="utf-8")
    log_event("check", ok=1, n=1)
    assert p.is_file()
    assert p.with_name("tow.jsonl.1").is_file()
    assert "check" in p.read_text(encoding="utf-8")


def test_rotation_and_append_are_serialized_between_processes(tmp_path):
    p = tmp_path / "tow.jsonl"
    p.write_bytes(b"x" * (MAX_BYTES + 1))
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    count = 40
    markers = [f"writer-{index}" for index in range(6)]
    processes = [
        context.Process(target=_multiprocess_log_writer, args=(str(tmp_path), marker, count, ready, start))
        for marker in markers
    ]
    with reaped(*processes):
        for process in processes:
            process.start()
        assert {ready.get(timeout=10) for _ in processes} == set(markers)
        start.set()
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0

    rows = []
    for candidate in (p, *(p.with_name(f"tow.jsonl.{index}") for index in range(1, 5))):
        if not candidate.is_file():
            continue
        lines = candidate.read_text(encoding="utf-8").splitlines()
        rows.extend(json.loads(line) for line in lines if line.startswith("{"))
    expected = {f"{marker}-{index}" for marker in markers for index in range(count)}
    assert {row["marker"] for row in rows} == expected


def test_log_write_failure_does_not_raise(monkeypatch, capsys):
    from pathlib import Path

    from tow import log as tow_log

    real_open = Path.open

    def failing_open(self, *args, **kwargs):
        if self.name == "tow.jsonl":
            raise OSError(28, "No space left on device")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    tow_log.log_event("client_added", topic="t1")  # must not raise

    assert "TOW log write failed (client_added)" in capsys.readouterr().err


def test_log_rotation_failure_still_appends(monkeypatch):
    from tow import log as tow_log

    def failing_rotate(_path):
        raise PermissionError("rotation target in use")

    monkeypatch.setattr(tow_log, "_rotate_if_needed", failing_rotate)
    tow_log.log_event("check_ok", topic="t2")

    assert any(e.get("kind") == "check_ok" for e in tow_log.read_events(limit=10))


def test_events_page_reads_only_the_tail_of_a_large_log(monkeypatch):
    from tow import log as logmod

    path = logmod.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    old = json.dumps({"kind": "old", "pad": "x" * 200}) + "\n"
    new = json.dumps({"kind": "check", "n": 1}) + "\n"
    path.write_text(old * 5000 + new, encoding="utf-8")  # ~1 MiB, most of it never read
    reads = []
    real_open = Path.open

    def counting_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self == path:
            real_read = handle.read
            handle.read = lambda *a: reads.append(len(chunk := real_read(*a))) or chunk
        return handle

    monkeypatch.setattr(Path, "open", counting_open)

    events = read_events(limit=5)

    assert events[0]["kind"] == "check"
    assert sum(reads) <= logmod._TAIL_BYTES


def test_log_keeps_five_files_of_five_mib():
    from tow import log as logmod

    assert (logmod.MAX_BYTES, logmod.BACKUPS) == (5 * 1024 * 1024, 4)


@pytest.mark.parametrize(
    ("message", "cls"),
    [
        ("rutor: Cloudflare challenge", "cloudflare"),
        ("kinozal: Just a moment...", "cloudflare"),
        ("rutor: all hosts failed: http 404", "gone"),
        ("rutor: all hosts failed: timeout", "tracker"),
    ],
)
def test_error_classes_reachable(message, cls):
    from tow.log import error_class

    assert error_class(message) == cls
