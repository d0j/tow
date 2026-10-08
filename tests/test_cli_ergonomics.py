"""1.21 CLI: every command and flag explains itself, one table of exit codes, `tow status`,
`tow access`, JSON on failure and a console that survives the em dash."""

from __future__ import annotations

import argparse
import io
import json
import re
from datetime import UTC, datetime

import pytest

from tow import cli


def _parsers() -> list[tuple[str, argparse.ArgumentParser]]:
    found = []

    def walk(name: str, parser: argparse.ArgumentParser) -> None:
        found.append((name, parser))
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for sub_name, sub in action.choices.items():
                    walk(f"{name} {sub_name}".strip(), sub)

    walk("", cli._build_parser())
    return found


# Every command explains every flag (the five-task layout and its commands are gone since 1.21).
_LEGACY: set[str] = set()


def test_every_listed_command_and_every_flag_has_a_description():
    root = cli._build_parser()
    subactions = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction))
    listed = {choice.dest: choice.help for choice in subactions._choices_actions}
    for name in ("version", "status", "doctor", "check", "access", "run", "stop", "restart", "setup", "export"):
        assert listed.get(name), name
    for name, parser in _parsers():
        if name.split(" ")[0] in _LEGACY:
            continue
        for action in parser._actions:
            if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
                continue
            assert action.help, (name, action.dest)


def test_every_command_describes_itself():
    # `tow --help` says "every command explains itself": backup, keys, update... printed only usage.
    for name, parser in _parsers():
        assert parser.description, name or "tow"


def test_task_only_commands_are_not_listed():
    root = cli._build_parser()
    subactions = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction))
    listed = {choice.dest for choice in subactions._choices_actions}
    assert "serve" not in listed  # `tow run` serves the page
    assert "install-task" not in subactions.choices  # the five-task layout is gone
    help_text = root.format_help()
    assert "--progress-only" not in cli._build_parser().format_help()
    assert "0    " in help_text
    assert "130" in help_text
    assert "setup" in help_text


def test_a_wrong_command_is_exit_1(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["no-such-command"])
    assert caught.value.code == cli.EXIT_USAGE == 1


def test_setup_says_to_use_the_launcher(capsys):
    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "setup" in out
    assert "tow" in out


def test_json_is_honoured_when_a_command_fails(capsys):
    from tow.paths import config_path

    config_path().write_text("port: http\n", encoding="utf-8")
    assert cli.main(["doctor", "--json"]) == 3
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["ok"] is False
    assert "port" in data["error"]


def test_status_is_one_line_and_json(monkeypatch, capsys):
    from tow.store import save_state

    monkeypatch.setattr("tow.autostart.backend", lambda: type("B", (), {"status": lambda self: {"on": True}})())
    save_state({"topics": [{"id": "t1", "title": "Show", "url": "http://rutor.info/torrent/1/x"}]})
    assert cli.main(["status"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.count("\n") == 0
    assert line.startswith("TOW ")
    assert "qBittorrent" in line
    assert cli.main(["status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["topics"] == 1
    assert data["running"] is False
    assert data["autostart"] is True
    assert data["client"] == "qBittorrent"


def test_status_writes_both_check_times_in_its_own_language(monkeypatch, capsys):
    # The check stored "at" in Russian; the terminal speaks English: both times read the English way.
    from tow.config import load_config, save_config
    from tow.store import save_state

    cfg = load_config()
    cfg["language"] = "en"
    save_config(cfg)
    monkeypatch.setattr("tow.autostart.backend", lambda: type("B", (), {"status": lambda self: {"on": False}})())
    at = datetime(2026, 10, 7, 1, 36, 49, tzinfo=UTC)
    save_state({"topics": [], "health": {"at": "07.10.2026 04:36:49 UTC+03:00", "at_ts": int(at.timestamp())}})
    assert cli.main(["status"]) == 0
    line = capsys.readouterr().out
    assert "07.10.2026" not in line
    assert re.search(r"last check: 2026-10-07 \d\d:36:49", line), line


@pytest.mark.parametrize(
    ("configured", "system", "expected"),
    [("auto", "ru_RU.UTF-8", "ru"), ("auto", "C", "en"), ("en", "ru_RU.UTF-8", "en")],
)
def test_a_typed_command_speaks_the_configured_or_the_system_language(
    monkeypatch, capsys, configured, system, expected
):
    # The browser last asked for English; a terminal follows config.yaml, else the system.
    from tow import i18n, platform
    from tow.config import load_config, save_config
    from tow.platform.posix import PosixBackend

    cfg = load_config()
    cfg["language"] = configured
    save_config(cfg)
    i18n.remember_browser_language("en" if expected == "ru" else "ru")
    for name in ("LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", system)
    monkeypatch.setattr("tow.autostart.backend", lambda: type("B", (), {"status": lambda self: {"on": True}})())

    with platform.use(PosixBackend("linux")):
        assert cli.main(["status"]) == 0
    line = capsys.readouterr().out

    assert i18n.translate("cli.status.stopped", expected) in line


def test_status_writes_the_last_and_the_next_check_the_same_way(monkeypatch, capsys):
    from tow.clock import parse_timestamp
    from tow.i18n import format_datetime
    from tow.store import save_state

    monkeypatch.setattr("tow.autostart.backend", lambda: type("B", (), {"status": lambda self: {"on": True}})())
    at_ts = int(parse_timestamp("2026-10-06 22:30:56").timestamp())  # a check stores both, the same moment
    save_state({"topics": [], "health": {"at": "2026-10-06 22:30:56", "at_ts": at_ts, "check_ok": True}})
    assert cli.main(["status"]) == 0
    line = capsys.readouterr().out

    assert "2026-10-06 22:30:56" not in line
    assert format_datetime(datetime.fromisoformat("2026-10-06T22:30:56")) in line


def test_access_on_asks_for_a_password_when_none_is_set(monkeypatch, capsys):
    from tow import access
    from tow.config import load_config
    from tow.store import load_secrets

    answers = iter(["correct horse battery", "correct horse battery"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(answers))
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert cli.main(["access", "on"]) == 0
    cfg = load_config()
    assert (cfg["allow_lan"], cfg["bind"]) == (True, "0.0.0.0")
    assert access.password_is_set(load_secrets())
    assert cli.main(["access", "off"]) == 0
    cfg = load_config()
    assert (cfg["allow_lan"], cfg["bind"]) == (False, "127.0.0.1")
    assert access.password_is_set(load_secrets())  # off keeps the password


def test_access_on_refuses_mismatched_passwords_and_keeps_the_network_closed(monkeypatch, capsys):
    from tow.config import load_config

    answers = iter(["correct horse battery", "something else entirely"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(answers))
    assert cli.main(["access", "on", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert not load_config().get("allow_lan")


class _Stream(io.TextIOWrapper):
    def __init__(self, encoding: str, tty: bool) -> None:
        super().__init__(io.BytesIO(), encoding=encoding)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.mark.parametrize(
    ("encoding", "tty", "becomes"),
    [("cp1251", True, "utf-8"), ("cp1252", False, "utf-8"), ("utf-8", False, "utf-8"), ("latin-1", True, "latin-1")],
)
def test_the_console_writes_utf8_when_redirected_or_on_a_code_page(monkeypatch, encoding, tty, becomes):
    out, err = _Stream(encoding, tty), _Stream(encoding, tty)
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)
    cli._utf8_console()
    assert out.encoding.lower() == becomes
    if becomes == "utf-8":
        out.write("TOW — проверка")  # the em dash and Russian no longer fail
