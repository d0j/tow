from types import SimpleNamespace

import pytest

from tow import cli
from tow.cli import main
from tow.i18n import t


def test_the_removed_monitorrent_import_is_an_unknown_command():
    with pytest.raises(SystemExit) as exc:
        main(["import-monitorrent", "--db", "monitorrent.db"])
    assert exc.value.code == 1  # a usage error (2 means "done in part" in TOW)


def test_version_json(capsys):
    import tomllib
    from importlib.metadata import version
    from pathlib import Path

    assert main(["version", "--json"]) == 0
    out = capsys.readouterr().out
    assert version("tow") in out
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"].count(".") == 2


def test_check_help_has_dry_run(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["check", "--help"])
    assert exc.value.code == 0
    assert "--dry-run" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("language", "expected", "incorrect"),
    [
        ("en", "one supervised service", "one process"),
        ("ru", "единым сервисом", "одном процессе"),
    ],
)
def test_run_help_describes_a_supervised_service(capsys, language, expected, incorrect):
    from tow.config import load_config, save_config

    config = load_config()
    config["language"] = language
    save_config(config)
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    output = " ".join(capsys.readouterr().out.split())
    assert expected in output
    assert incorrect not in output


@pytest.mark.parametrize(
    ("language", "own", "foreign"),
    [
        ("en", ["usage: tow backup", "options:", "show this help message and exit"], ["использование"]),
        (
            "ru",
            ["использование: tow backup", "параметры:", "показать эту справку и выйти"],
            ["usage:", "options:", "show this help"],
        ),
    ],
)
def test_a_command_help_speaks_one_language_with_its_description(capsys, language, own, foreign):
    # In Russian argparse's own words ("usage:", "options", -h) stayed English, and `backup`
    # printed no description of its own.
    from tow.config import load_config, save_config

    config = load_config()
    config["language"] = language
    save_config(config)
    with pytest.raises(SystemExit) as exc:
        main(["backup", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for text in own:
        assert text in out
    for text in foreign:
        assert text not in out
    assert " ".join(t("cli.help.backup_long", language).split()[:4]) in " ".join(out.split())


def test_a_wrong_option_shows_the_usage_in_the_command_language(capsys):
    from tow.config import load_config, save_config

    config = load_config()
    config["language"] = "ru"
    save_config(config)
    with pytest.raises(SystemExit) as exc:
        main(["restore-snapshot"])  # --path is required: refused by the command itself
    assert exc.value.code == cli.EXIT_USAGE
    assert capsys.readouterr().err.startswith("использование: tow restore-snapshot")


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["restore-snapshot"], "не указано: --path"),
        (["access"], "не указано: {on,off,status}"),
        (["access", "maybe"], "'maybe' — не одно из 'on', 'off', 'status'"),
        (["autostart"], "не указано: {on,off,status}"),
        (["permissions", "x"], "'x' — не одно из 'status', 'fix'"),
        (["stop", "--wait", "abc"], "--wait: 'abc' — недопустимое значение"),
        (["status", "extra"], "неизвестные аргументы: extra"),
        (["serve", "--port"], "--port: нужно значение"),
        (["check", "--global-only", "--timer-only"], "нельзя указывать вместе с --global-only"),
    ],
)
def test_a_refused_command_line_is_said_in_the_command_language(capsys, argv, expected):
    # argparse's own messages stayed English ("the following arguments are required") and
    # named TOW's variables (access_action, autostart_action) and Python's types ("float").
    from tow.config import load_config, save_config

    config = load_config()
    config["language"] = "ru"
    save_config(config)
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert expected in err
    for word in ("required", "invalid", "unrecognized", "expected", "_action", "float", "int value"):
        assert word not in err


def test_an_english_refusal_names_no_variable_or_python_type(capsys):
    from tow.config import load_config, save_config

    config = load_config()
    config["language"] = "en"
    save_config(config)
    for argv, expected in ((["access"], "missing: {on,off,status}"), (["stop", "--wait", "x"], "'x' is not a valid")):
        with pytest.raises(SystemExit):
            main(argv)
        err = capsys.readouterr().err
        assert expected in err
        assert "access_action" not in err
        assert "float" not in err


def test_the_five_task_commands_are_gone(capsys):
    # 1.21: no `install-task`; `autostart migrate` only says the switch belongs to 1.18-1.20.
    with pytest.raises(SystemExit):
        main(["install-task"])
    capsys.readouterr()
    assert main(["autostart", "migrate", "--apply"]) == 0
    assert "v1.20.0" in capsys.readouterr().out


def test_a_broken_config_is_an_exit_code_not_a_traceback(capsys):
    # N13: every command used to crash with a traceback on a config/secret problem.
    from tow.paths import config_path

    config_path().write_text("port: http\n", encoding="utf-8")

    assert cli.main(["doctor"]) == 3
    err = capsys.readouterr().err
    assert "tow doctor: config.yaml: port" in err
    assert "Traceback" not in err


@pytest.mark.parametrize(
    ("text", "code", "values"),
    [("port: 8787\nbind: [x\n", "syntax", {"line": 3, "column": 1}), ("- a\n- b\n", "mapping", {})],
)
def test_starting_with_unreadable_yaml_says_where_in_the_owners_words(monkeypatch, capsys, text, code, values):
    # `tow start` is what every start file runs; a YAML error used to be only "the command could
    # not be completed".
    from tow.paths import config_path

    config_path().write_text(text, encoding="utf-8")
    monkeypatch.setenv("TOW_NO_BROWSER", "1")

    assert cli.main(["start"]) == cli.EXIT_CANNOT_RUN
    captured = capsys.readouterr()
    assert captured.out == ""  # refused before anything was started
    # The terminal speaks the system's language when config.yaml cannot say which one.
    expected = {f"tow start: {t(f'config_error.{code}', lang, **values)}" for lang in ("en", "ru")}
    assert captured.err.strip() in expected, captured.err


def test_unexpected_cli_error_does_not_echo_secret(monkeypatch, capsys):
    def broken(_args):
        raise RuntimeError("password=must-not-be-displayed")

    monkeypatch.setitem(cli._COMMANDS, "version", broken)
    assert cli.main(["version", "--json"]) == cli.EXIT_CANNOT_RUN
    out = capsys.readouterr().out
    assert "must-not-be-displayed" not in out
    assert "не удалось выполнить команду" in out


def test_a_folder_tow_cannot_write_is_named_with_its_cause(monkeypatch, capsys):
    # QA: with data\ read-only every command said "could not be completed; check the settings".
    def denied(_args):
        raise PermissionError(13, "Access is denied", "C:\\TOW\\data\\state.json")

    monkeypatch.setitem(cli._COMMANDS, "version", denied)
    assert cli.main(["version", "--json"]) == cli.EXIT_CANNOT_RUN
    out = capsys.readouterr().out
    assert "C:\\\\TOW\\\\data\\\\state.json" in out  # JSON-escaped
    assert "Access is denied" in out
    assert "не удалось выполнить команду" not in out
    monkeypatch.setitem(cli._COMMANDS, "version", lambda _args: (_ for _ in ()).throw(ConnectionError("x")))
    assert cli.main(["version", "--json"]) == cli.EXIT_CANNOT_RUN  # no file: the general sentence
    assert "не удалось выполнить команду" in capsys.readouterr().out


def test_a_restart_without_a_supervisor_is_said_in_the_owners_language(monkeypatch):
    from tow import i18n, lifecycle

    monkeypatch.setattr("tow.supervisor.layout.running", lambda: None)
    result = lifecycle.request_restart()
    assert result == {"ok": False, "error": i18n.t("settings.service.not_supervised")}
    assert "tow run" in result["error"]
    assert result["error"] != i18n.t("settings.service.not_supervised", "en")  # the tests speak Russian


def test_failed_check_does_not_echo_exception_secret(monkeypatch, capsys):
    def broken(**_kwargs):
        raise RuntimeError("token=must-not-be-displayed")

    monkeypatch.setattr("tow.check.run_check", broken)
    assert cli.main(["check", "--json"]) == cli.EXIT_CANNOT_RUN
    out = capsys.readouterr().out
    assert "must-not-be-displayed" not in out
    assert "не удалось выполнить команду" in out


def test_debug_env_shows_the_traceback(monkeypatch):
    from tow.paths import config_path

    config_path().write_text("port: http\n", encoding="utf-8")
    monkeypatch.setenv("TOW_DEBUG", "1")

    with pytest.raises(ValueError, match="port"):
        cli.main(["doctor"])


def _sentences_only(out):
    """Words for a person: no "field: value" line of a dict, no true/false/null, no field name."""
    import re

    assert out.strip()
    for line in out.splitlines():
        assert not re.match(r"^\s*-?\s*'?[a-z_]+'?:(\s|$)", line), line
    for word in ("true", "false", "null", "key_source", "storage", "password_set", "service_ok", "ok:"):
        assert not re.search(rf"(?<![\w/.]){word}(?![\w.])", out), (word, out)


@pytest.mark.parametrize("language", ["en", "ru"])
def test_a_command_tells_a_person_its_result_in_sentences(monkeypatch, capsys, tmp_path, language):
    # `tow keys status`, `secrets status`, `access status`, `backup`, `export`, `import`,
    # `watchdog`, `autostart status` printed their result as YAML: internal field names
    # (key_source, storage, service_ok…), true/false, English values in Russian.
    import re

    from tow.autostart import windows
    from tow.config import load_config, save_config

    monkeypatch.setenv("TOW_HOME", str(tmp_path / "data"))
    config = load_config()
    config.update(language=language, backup_dir=str(tmp_path / "night"))
    save_config(config)
    passphrase = "a long passphrase"
    monkeypatch.setattr("getpass.getpass", lambda prompt="": passphrase)
    monkeypatch.setattr(
        "tow.watchdog.run_watchdog",
        lambda: {
            "port": 8787,
            "service_ok": True,
            "checks_ok": True,
            "checks_late": False,
            "backup_ok": None,
            "alerts": [],
            "monitoring_ok": True,
        },
    )
    monkeypatch.setattr(
        "tow.autostart.backend",
        lambda: SimpleNamespace(
            status=lambda: {
                "backend": "windows",
                "on": False,
                "ours": False,
                "stale": False,
                "state": "absent",
                "where": windows.TASK_NAME,
                "error": "",
            }
        ),
    )
    output = tmp_path / "export.towx"
    commands = [
        ["version"],
        ["keys", "status"],
        ["secrets", "status"],
        ["secrets", "migrate"],
        ["access", "status"],
        ["backup"],
        ["export", "--output", str(output)],
        ["import", "--input", str(output)],
        ["watchdog"],
        ["autostart", "status"],
    ]
    for argv in commands:
        assert main(argv) == 0, argv
        out = capsys.readouterr().out
        _sentences_only(out)
        if language == "ru" and argv != ["version"]:
            assert re.search("[а-яё]", out, re.IGNORECASE), (argv, out)
    from tow.snapshots import list_snapshots, snapshot_path

    folder = snapshot_path(list_snapshots(limit=1)[0]["name"])
    assert main(["restore-snapshot", "--path", str(folder)]) == 0
    _sentences_only(capsys.readouterr().out)


def test_a_refusal_says_the_reason_alone(capsys, tmp_path):
    cli._print({"ok": False, "error": "The reason.", "detail": "a technical cause"}, False)
    assert capsys.readouterr().out == "The reason.\n"
    cli._print({"ok": True, "message": "Done."}, False, lambda data: "never this")
    assert capsys.readouterr().out == "never this\n"
    cli._print({"ok": True, "message": "Done."}, False)
    assert capsys.readouterr().out == "Done.\n"


def test_a_check_without_a_client_answer_shows_no_empty_client(capsys):
    cli._print({"qbit": "", "results": [], "preview": True}, False)
    out = capsys.readouterr().out
    assert t("cli.check.client", value="") not in out
    assert out.splitlines()[0] == t("cli.check.preview")


def test_text_output_is_readable_not_a_dict_repr(capsys):
    cli._print({"ok": True, "tasks": [{"name": "TOW-check", "rc": 0}]}, False)

    out = capsys.readouterr().out
    assert "{'ok'" not in out
    assert "ok: true" in out
    assert "name: TOW-check" in out


def test_check_text_output_follows_the_owners_language(monkeypatch, capsys):
    import re

    from tow import check
    from tow.config import load_config, save_config

    cfg = load_config()
    cfg["language"] = "en"
    save_config(cfg)
    result = {"qbit": "5.2", "preview": True, "results": [{"title": "Show", "ok": True, "changed": True}]}
    monkeypatch.setattr(check, "run_check", lambda **_kw: result)
    try:
        assert cli.main(["check"]) == 0
    finally:
        from tow import i18n

        i18n._CURRENT.set(None)  # main() set this thread's language
    out = capsys.readouterr().out
    assert not re.search("[а-яё]", out, re.IGNORECASE), out
    assert t("cli.check.total", "en", n=1, failed=0) in out
    assert t("web.check.changed", "en") in out


def test_check_text_output_is_one_line_per_topic(monkeypatch, capsys):
    from tow import check

    monkeypatch.setattr(
        check,
        "run_check",
        lambda **_kw: {
            "qbit": "5.2",
            "preview": True,
            "results": [{"title": "Show", "ok": True}, {"id": "t2", "ok": False, "error": "boom"}],
        },
    )

    assert cli.main(["check"]) == 2
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(t("cli.check.client", value="5.2"))
    assert out[1] == "ok  Show"
    assert out[2] == "ERR t2 — boom"
    assert out[3] == t("cli.check.total", n=2, failed=1)


def test_export_refuses_a_short_passphrase(monkeypatch, capsys, tmp_path):
    import json

    from tow.config import load_config, save_config

    cfg = load_config()
    cfg["language"] = "en"  # the owner chose English: prompt and refusal follow
    save_config(cfg)
    prompts = []
    monkeypatch.setattr("getpass.getpass", lambda prompt: prompts.append(prompt) or "short")

    assert cli.main(["export", "--output", str(tmp_path / "x.towx"), "--json"]) == 3
    assert prompts == [t("cli.bundle.passphrase", "en")]
    refusal = json.loads(capsys.readouterr().out)
    assert refusal == {"ok": False, "error": t("cli.bundle.passphrase_short", "en", length=12)}
    assert "at least 12 characters" in refusal["error"]
    assert not (tmp_path / "x.towx").exists()
