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
    index_event_titles,
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


def test_pause_then_resume_reads_as_paused_then_resumed():
    labels = [
        format_event({"kind": "topic_pause", "paused": True})["label"],
        format_event({"kind": "topic_pause", "paused": False})["label"],
        format_event({"kind": "site_pause", "status": "paused"})["label"],
        format_event({"kind": "site_pause", "status": "resumed"})["label"],
    ]

    assert labels == ["Пауза", "Возобновлена", "Сайт на паузе", "Сайт возобновлён"]


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


def test_event_log_scrubs_secret_assignments_and_auth_headers():
    log_event(
        "check_fail",
        error="password=hunter2 token='abc123' Authorization: Bearer long-secret-value\nnext line",
    )
    row = read_events(limit=1)[0]
    assert "hunter2" not in str(row)
    assert "abc123" not in str(row)
    assert "long-secret-value" not in str(row)
    assert "next line" in str(row)


def test_format_event_omits_malformed_hash():
    rendered = format_event({"kind": "downloaded", "hash": "a" * 40 + "SECRET"})

    assert "aaaaaaaa" not in rendered["detail"]
    assert "SECRET" not in rendered["detail"]


def test_a_long_folder_keeps_its_own_name_and_says_it_was_cut():
    """Round-3 audit: paths were cut at 80 characters with nothing to say so."""
    short = "M:\\Series\\Show"
    assert short in format_event({"kind": "topic_edit", "path": short})["detail"]
    long = "M:\\" + "\\".join(["folder"] * 15) + "\\The Show S01"
    detail = format_event({"kind": "topic_edit", "path": long})["detail"]
    assert "…" in detail
    assert detail.endswith("\\The Show S01")
    assert len(detail) == 80


def test_event_titles_resolve_from_topic_or_revision_without_rewriting_log():
    current_hash = "A" * 40
    old_hash = "B" * 40
    index = index_event_titles(
        [{"id": "topic-1", "title": "Series One", "hash": current_hash, "previous_hashes": [old_hash]}]
    )
    assert "Series One" in format_event({"kind": "file_completed", "topic": "topic-1"}, title_index=index)["detail"]
    assert "Series One" in format_event({"kind": "file_completed", "hash": old_hash}, title_index=index)["detail"]
    assert (
        "Original"
        in format_event({"kind": "file_completed", "topic": "topic-1", "title": "Original"}, title_index=index)[
            "detail"
        ]
    )
    assert (
        "Series One"
        not in format_event({"kind": "file_completed", "topic": "deleted", "hash": "C" * 40}, title_index=index)[
            "detail"
        ]
    )


def test_event_titles_do_not_guess_when_hash_or_id_is_shared():
    shared = "D" * 40
    index = index_event_titles(
        [
            {"id": "same", "title": "First", "hash": shared},
            {"id": "same", "title": "Second", "hash": shared},
        ]
    )
    detail = format_event({"kind": "file_completed", "hash": shared, "topic": "same"}, title_index=index)["detail"]
    assert "First" not in detail
    assert "Second" not in detail
    assert shared[:8] in detail


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


def _held(monkeypatch, *held: Path) -> None:
    """Windows refuses to rename a file another program holds open: so does os.replace here."""
    real = os.replace

    def replace(source, target, *args, **kwargs):
        if Path(source) in held:
            raise PermissionError(32, "The process cannot access the file", str(source))
        return real(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


def _rotated(path: Path) -> dict[str, str]:
    return {
        candidate.name: candidate.read_text(encoding="utf-8")
        for candidate in sorted(path.parent.glob(path.name + "*"))
        if candidate != path
    }


def test_a_held_event_log_keeps_every_rotated_file(monkeypatch):
    # The live log held open (History reading it, a terminal following it): the rotation used to
    # delete .4 and shift the others before the last rename failed - at every new event, until
    # the whole history was gone.
    import tow.log as logmod

    monkeypatch.setattr(logmod, "MAX_BYTES", 80)
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("live " * 30 + "\n", encoding="utf-8")
    for index in range(1, 5):
        path.with_name(f"tow.jsonl.{index}").write_text(f"older {index}\n", encoding="utf-8")
    before = _rotated(path)
    with monkeypatch.context() as held:
        _held(held, path)
        for _ in range(6):
            assert log_event("check", ok=1, n=1) is True
        assert _rotated(path) == before  # nothing lost, nothing renamed
        assert path.read_text(encoding="utf-8").count('"kind": "check"') == 6
    live = path.read_text(encoding="utf-8")
    assert log_event("check", ok=1, n=2) is True  # free again: rotated as always
    assert _rotated(path) == {
        "tow.jsonl.1": live,
        "tow.jsonl.2": "older 1\n",
        "tow.jsonl.3": "older 2\n",
        "tow.jsonl.4": "older 3\n",
    }


def test_a_held_rotated_file_stops_the_rotation_without_a_loss(monkeypatch):
    import tow.log as logmod

    monkeypatch.setattr(logmod, "MAX_BYTES", 80)
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("live " * 30 + "\n", encoding="utf-8")
    for index in range(1, 5):
        path.with_name(f"tow.jsonl.{index}").write_text(f"older {index}\n", encoding="utf-8")
    before = _rotated(path)
    _held(monkeypatch, path.with_name("tow.jsonl.2"))  # the History page reads an older file
    assert log_event("check", ok=1, n=1) is True
    assert _rotated(path) == before


def test_run_and_serve_logs_keep_their_older_files_while_held(monkeypatch, tmp_path):
    import logging

    from tow.log import SafeRotatingFileHandler

    path = tmp_path / "serve.log"
    handler = SafeRotatingFileHandler(path, maxBytes=60, backupCount=3, encoding="utf-8")
    try:
        for index in range(1, 4):
            path.with_name(f"serve.log.{index}").write_text(f"older {index}\n", encoding="utf-8")
        record = logging.LogRecord("uvicorn", logging.INFO, __file__, 1, "x" * 40, None, None)
        with monkeypatch.context() as held:
            _held(held, path)
            for _ in range(5):
                handler.emit(record)
            assert _rotated(path) == {f"serve.log.{index}": f"older {index}\n" for index in range(1, 4)}
        handler.emit(record)  # free again: the grown log is rotated as one
        assert path.with_name("serve.log.1").read_text(encoding="utf-8").count("x" * 40) == 5
        assert path.with_name("serve.log.2").read_text(encoding="utf-8") == "older 1\n"
        assert path.with_name("serve.log.3").read_text(encoding="utf-8") == "older 2\n"
        assert not path.with_name("serve.log.3.old").exists()
    finally:
        handler.close()


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
        # Generous waits: under a parallel suite, six spawned interpreters may take long to start.
        assert {ready.get(timeout=120) for _ in processes} == set(markers)
        start.set()
        for process in processes:
            process.join(timeout=120)
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
    assert tow_log.log_event("client_added", topic="t1") is False  # must not raise

    assert "TOW log write failed (client_added)" in capsys.readouterr().err


def test_log_rotation_failure_still_appends(monkeypatch):
    from tow import log as tow_log

    def failing_rotate(_path):
        raise PermissionError("rotation target in use")

    monkeypatch.setattr(tow_log, "_rotate_if_needed", failing_rotate)
    assert tow_log.log_event("check_ok", topic="t2") is True

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


def _history_line(n, **fields):
    return json.dumps({"kind": "file_completed", "event_id": f"e{n}", **fields}, ensure_ascii=False) + "\n"


def test_history_reads_the_log_from_its_end_across_blocks_and_files(monkeypatch):
    from tow import log as logmod

    monkeypatch.setattr(logmod, "_BLOCK_BYTES", 97)  # lines cross block boundaries everywhere
    path = logmod.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    older = [_history_line(n, title="Сериал " * (n % 7)) for n in range(40)]
    newer = [_history_line(n, title="Show " * (n % 3)) for n in range(40, 90)]
    path.with_name(path.name + ".1").write_text("".join(older), encoding="utf-8")
    path.write_text("".join(newer) + "\n", encoding="utf-8")

    events = logmod.history_events(limit=1000)

    assert [event["event_id"] for event in events] == [f"e{n}" for n in reversed(range(90))]
    assert [event["event_id"] for event in logmod.history_events(limit=3)] == ["e89", "e88", "e87"]


def test_history_stops_reading_once_it_has_enough_events(monkeypatch):
    from tow import log as logmod

    path = logmod.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(_history_line(n, pad="x" * 200) for n in range(20_000)), encoding="utf-8")  # ~5 MiB
    reads = []
    real_open = Path.open

    def counting_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self == path:
            real_read = handle.read
            handle.read = lambda *a: reads.append(len(chunk := real_read(*a))) or chunk
        return handle

    monkeypatch.setattr(Path, "open", counting_open)

    assert len(logmod.history_events(limit=300)) == 300
    assert sum(reads) <= logmod._BLOCK_BYTES


def test_history_search_by_a_topics_current_name_parses_only_its_events(monkeypatch):
    from tow import log as logmod

    path = logmod.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [_history_line(n, topic_id=f"t{n % 50}") for n in range(500)]
    lines.append(_history_line(500, hash="AB" * 20))
    path.write_text("".join(lines), encoding="utf-8")
    titles = index_event_titles(
        [{"id": f"t{n}", "title": "Unique Show" if n == 7 else f"Other {n}"} for n in range(50)]
        + [{"id": "h", "title": "Hashed Unique", "hash": "ab" * 20}]
    )
    parsed = []
    real_loads = logmod.json.loads
    monkeypatch.setattr(logmod.json, "loads", lambda text, *a, **k: parsed.append(text) or real_loads(text, *a, **k))

    found = logmod.history_events(text="unique", title_index=titles)

    assert [event["event_id"] for event in found] == ["e500", *(f"e{n}" for n in reversed(range(7, 500, 50)))]
    assert len(parsed) == len(found)
    parsed.clear()
    by_text = logmod.history_events(text="E10", title_index=titles)  # in the line itself
    assert [event["event_id"] for event in by_text] == [*(f"e{n}" for n in range(109, 99, -1)), "e10"]
    assert len(parsed) == len(by_text)


@pytest.mark.parametrize("block", [97, 4096, 256 * 1024])
def test_history_search_skips_whole_blocks_without_changing_what_it_finds(monkeypatch, block):
    """A search reads only the lines of blocks that can hold the text: the results are those of
    the line-by-line search, also for text that only appears once folded (ß -> ss, the Kelvin
    sign -> k, the ligature ﬁ -> fi), escaped text, other letters' cases and broken bytes."""
    from tow import log as logmod

    monkeypatch.setattr(logmod, "_BLOCK_BYTES", block)
    path = logmod.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    titles = [
        "Plain Show",
        "STRASSE",
        "Straße",
        "Größe ẞ",
        "Kelvin",
        "ﬁle",
        "Сериал Сезон",
        "İstanbul",
        "mixed CaSe",
    ]
    lines = [_history_line(n, title=titles[n % len(titles)], pad="y" * (n % 13)) for n in range(400)]
    lines.insert(37, json.dumps({"kind": "file_completed", "event_id": "esc", "title": "Straße"}) + "\n")
    raw = "".join(lines).encode("utf-8")
    raw = raw.replace(b'"pad": "yyy"', b'"pad": "\xff\xfeyy"', 3)  # bytes that are not UTF-8
    path.write_bytes(raw)
    needles = ["zzz", "ss", "strasse", "STRASSE", "k", "kelvin", "fi", "FILE", "i", "сезон", "case", "y\xff", "a b"]
    found = {needle: logmod.history_events(text=needle, limit=1000) for needle in needles}
    monkeypatch.setattr(logmod, "_block_without", lambda needle, refs: None)  # line by line, as before

    for needle in needles:
        assert found[needle] == logmod.history_events(text=needle, limit=1000), needle
    assert found["zzz"] == []
    assert len(found["ss"]) > len(found["strasse"]) > 0
    assert {event["title"] for event in found["kelvin"]} == {"Kelvin"}
    assert {event["title"] for event in found["fi"]} >= {"ﬁle"}
    assert any(event["event_id"] == "esc" for event in found["strasse"])


def test_the_characters_that_fold_to_ascii_letters_are_all_known():
    from tow import log as logmod

    folds = logmod._ascii_folds()
    expected: dict[str, set[bytes]] = {}
    for code in range(0x80, 0x110000):
        char = chr(code)
        for folded in char.casefold():
            if folded.isascii():
                expected.setdefault(folded, set()).add(char.encode("utf-8"))
    assert folds == {key: frozenset(value) for key, value in expected.items()}
    assert "ß".encode() in folds["s"]
    assert "K".encode() in folds["k"]


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
