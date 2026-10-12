"""``tow --help`` and the ``--help`` of every command, in English and Russian, pinned as text:
``tests/golden/cli-help.en.txt`` and ``cli-help.ru.txt``.

The pages are rendered as a terminal 100 columns wide without colour, on the Windows backend
(the launcher named in ``tow --help`` is the system's), with trailing spaces removed. A change
of a command, an option or a help text changes this text. If intended, regenerate it and review
the diff:

    uv run --frozen python tests/test_golden_help.py
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"
LANGUAGES = ("en", "ru")
# The terminal the pages are rendered for: its width, and no colour (Python 3.14's argparse
# colours a terminal that allows it).
ENVIRONMENT = {"COLUMNS": "100", "LINES": "50", "NO_COLOR": "1", "PYTHON_COLORS": "0"}
UNSET = ("FORCE_COLOR",)


def commands() -> list[list[str]]:
    """Every command path of the parser (["check"], ["keys", "ensure"]...), listed or not."""
    from tow import cli

    found: list[list[str]] = []
    seen: set[int] = set()

    def walk(path: list[str], parser: argparse.ArgumentParser) -> None:
        if id(parser) in seen:  # an alias of a command already walked
            return
        seen.add(id(parser))
        found.append(path)
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    walk([*path, name], sub)

    walk([], cli._build_parser())
    return found


def page(path: list[str]) -> str:
    from tow import cli

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            code = cli.main([*path, "--help"])
        except SystemExit as exc:
            code = exc.code
    assert code == 0, (path, code)
    return "\n".join(line.rstrip() for line in out.getvalue().rstrip().splitlines())


def pages() -> str:
    """Every help page in the language of the current config.yaml, on the Windows backend."""
    from tow import platform

    with platform.use(platform.backend_for("windows")):
        return "\n\n".join(f"$ {' '.join(['tow', *path, '--help'])}\n{page(path)}" for path in commands()) + "\n"


@contextlib.contextmanager
def language_config(folder: Path, language: str) -> Iterator[None]:
    """config.yaml of the example with the language (TOW_CONFIG), for the duration."""
    config = folder / f"config-{language}.yaml"
    template = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    config.write_text(f"{template}\nlanguage: {language}\nsetup_done: true\n", encoding="utf-8")
    previous = os.environ.get("TOW_CONFIG")
    os.environ["TOW_CONFIG"] = str(config)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TOW_CONFIG", None)
        else:
            os.environ["TOW_CONFIG"] = previous


@pytest.mark.parametrize("language", LANGUAGES)
def test_help_pages_are_pinned(language, tmp_path, monkeypatch):
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    for name in UNSET:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TOW_CONFIG", str(tmp_path / "config.yaml"))  # restored after the test
    with language_config(tmp_path, language):
        actual = pages()
    expected = (GOLDEN / f"cli-help.{language}.txt").read_text(encoding="utf-8")
    if actual != expected:
        changed = [
            f"- {old}\n+ {new}"
            for old, new in zip(expected.splitlines(), actual.splitlines(), strict=False)
            if old != new
        ][:10]
        raise AssertionError(
            f"tow --help ({language}) changed; if intended, regenerate and review the diff "
            "(uv run --frozen python tests/test_golden_help.py):\n" + "\n".join(changed or ["(more or fewer lines)"])
        )


def test_every_command_has_a_page():
    names = {" ".join(path) for path in commands()}
    assert {"", "check", "run", "serve", "backup", "keys", "update", "autostart"} <= names
    for language in LANGUAGES:
        text = (GOLDEN / f"cli-help.{language}.txt").read_text(encoding="utf-8")
        assert [line for line in text.splitlines() if line.startswith("$ tow ")] == [
            f"$ {' '.join(['tow', *path, '--help'])}" for path in commands()
        ]


def _regenerate() -> None:
    """Render into a throwaway install (the test's environment), never the checkout's data."""
    for name, value in ENVIRONMENT.items():
        os.environ[name] = value
    for name in UNSET:
        os.environ.pop(name, None)
    with tempfile.TemporaryDirectory(prefix="tow-golden-help-") as folder:
        os.environ["TOW_ROOT"] = os.environ["TOW_HOME"] = folder
        os.environ.pop("TOW_MASTER_KEY_FILE", None)
        for language in LANGUAGES:
            with language_config(Path(folder), language):
                text = pages()
            target = GOLDEN / f"cli-help.{language}.txt"
            target.write_text(text, encoding="utf-8", newline="\n")
            print(f"wrote {target}", file=sys.__stdout__)


if __name__ == "__main__":
    _regenerate()
