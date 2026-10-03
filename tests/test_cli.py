import pytest

from tow import cli
from tow.cli import main, secrets_ok
from tow.i18n import t


def test_secrets_ok_accepts_registry_client(monkeypatch):
    monkeypatch.setattr("tow.config.load_config", lambda: {"clients": {"main": {"kind": "qbittorrent"}}})
    monkeypatch.setattr("tow.store.load_secrets", lambda: {"clients": {"main": {"host": "http://qbit"}}})

    assert secrets_ok() is True


def test_import_monitorrent_requires_explicit_db():
    with pytest.raises(SystemExit) as exc:
        main(["import-monitorrent", "--json"])
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


def test_unexpected_cli_error_does_not_echo_secret(monkeypatch, capsys):
    def broken(_args):
        raise RuntimeError("password=must-not-be-displayed")

    monkeypatch.setitem(cli._COMMANDS, "version", broken)
    assert cli.main(["version", "--json"]) == cli.EXIT_CANNOT_RUN
    out = capsys.readouterr().out
    assert "must-not-be-displayed" not in out
    assert "не удалось выполнить команду" in out


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
    answers = iter(["short"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(answers))

    assert cli.main(["export", "--output", str(tmp_path / "x.towx"), "--json"]) == 3
    assert "at least 12 characters" in capsys.readouterr().out
    assert not (tmp_path / "x.towx").exists()
