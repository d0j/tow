"""The parser of ``tow``: argparse in the command's language with TOW's exit codes, built from the
command table (each command adds its own parser, see ``tow.cli.command.Command``)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from typing import Any, NoReturn, cast

from tow.cli.command import EXIT_USAGE, Command, Subcommands


def launcher() -> str:
    """The launcher of this install as this system writes it (``tow setup`` is its command)."""
    from tow import paths, platform

    return paths.launcher(windows=platform.is_windows())


class _Formatter(argparse.HelpFormatter):
    """argparse's "usage:" in the command's language (argparse itself only knows gettext)."""

    def add_usage(self, usage: Any, actions: Any, groups: Any, prefix: str | None = None) -> None:
        if prefix is None:
            from tow.i18n import t

            prefix = t("cli.argparse.usage") + " "
        super().add_usage(usage, actions, groups, prefix)


class _RawFormatter(_Formatter, argparse.RawDescriptionHelpFormatter):
    """The same, keeping the line breaks of ``tow --help``'s description and epilog."""


# argparse's own messages, by its English text (it looks each one up through its module's
# ``_`` when it says it): the catalog's say them in the command's language, with the same
# %-placeholders argparse fills in afterwards. The type name of "invalid float value" is left
# out: it is Python's word, not the owner's.
_ARGPARSE_TEXTS = {
    "the following arguments are required: %s": "cli.argparse.required",
    "one of the arguments %s is required": "cli.argparse.one_required",
    "unrecognized arguments: %s": "cli.argparse.unrecognized",
    "argument %(argument_name)s: %(message)s": "cli.argparse.argument",
    "invalid choice: %(value)r (choose from %(choices)s)": "cli.argparse.invalid_choice",
    "invalid choice: %(value)r, maybe you meant %(closest)r? (choose from %(choices)s)": "cli.argparse.invalid_choice",
    "invalid %(type)s value: %(value)r": "cli.argparse.invalid_value",
    "expected one argument": "cli.argparse.expected_one",
    "expected at most one argument": "cli.argparse.expected_one",
    "expected at least one argument": "cli.argparse.expected_some",
    "not allowed with argument %s": "cli.argparse.not_allowed_with",
    "ambiguous option: %(option)s could match %(matches)s": "cli.argparse.ambiguous",
    "ignored explicit argument %r": "cli.argparse.ignored",
}


def _argparse_text(message: str) -> str:
    from tow.i18n import t

    key = _ARGPARSE_TEXTS.get(message)
    return t(key) if key else message


class _Parser(argparse.ArgumentParser):
    """argparse with TOW's exit codes: a wrong command or option is 1, not argparse's 2 (2 means
    "done in part" in TOW, see ``tow --help``). Its own words (the section titles, -h) come from
    the catalog, in the command's language; every subcommand is a _Parser too."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        from tow.i18n import t

        kwargs.setdefault("formatter_class", _Formatter)
        add_help = kwargs.get("add_help", True)
        kwargs["add_help"] = False  # added below, with its text from the catalog
        super().__init__(*args, **kwargs)
        self._positionals.title = t("cli.argparse.positionals")
        self._optionals.title = t("cli.argparse.options")
        if add_help:
            self.add_argument("-h", "--help", action="help", default=argparse.SUPPRESS, help=t("cli.argparse.help"))

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: {message}\n")


def add_command(
    parent: Subcommands, name: str, help_key: str, description_key: str | None = None, *, listed: bool = True
) -> argparse.ArgumentParser:
    """A command (or a command's action) with its line in the list of ``parent`` - none when not
    ``listed`` - and its own description (``tow COMMAND --help``): the line's text unless it has
    a longer one."""
    from tow.i18n import t

    description = t(description_key or help_key)
    if not listed:
        return parent.add_parser(name, description=description)
    return parent.add_parser(name, help=t(help_key), description=description)


def json_option(parser: argparse.ArgumentParser) -> None:
    """``--json``: the result as JSON for a program."""
    from tow.i18n import t

    parser.add_argument("--json", action="store_true", help=t("cli.help.json"))


def build_parser(commands: Iterable[Command]) -> argparse.ArgumentParser:
    """``tow`` with the commands in their order (the order ``tow --help`` lists them)."""
    from tow.i18n import t

    # argparse knows only gettext: its error messages come from the catalog (an unknown one
    # stays argparse's own English).
    setattr(argparse, "_", _argparse_text)  # noqa: B010 - argparse's module-level lookup, not an attribute of ours
    parser = _Parser(
        prog="tow",
        description=t("cli.help.description"),
        epilog=t("cli.help.epilog", launcher=launcher()),
        formatter_class=_RawFormatter,
    )
    # Its parsers are _Parsers, which the commands configure as any ArgumentParser.
    sub = cast(Subcommands, parser.add_subparsers(dest="cmd", required=True, metavar="<command>"))
    for command in commands:
        command.configure(sub)
    return parser
