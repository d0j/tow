"""``tow``: ``main`` runs one command in its own language, behind one error boundary. The commands
are the table of ``tow.cli.commands``, the parser ``tow.cli.parser`` builds from it, what they
print ``tow.cli.render``. Only ``tow.__main__`` imports this package."""

from __future__ import annotations

import argparse
import contextvars
import os
import sys

from tow.cli import commands, render
from tow.cli.command import EXIT_CANNOT_RUN as EXIT_CANNOT_RUN
from tow.cli.command import EXIT_INTERRUPTED as EXIT_INTERRUPTED
from tow.cli.command import EXIT_OK as EXIT_OK
from tow.cli.command import EXIT_PARTIAL as EXIT_PARTIAL
from tow.cli.command import EXIT_USAGE as EXIT_USAGE
from tow.cli.commands.data import MIN_EXPORT_PASSPHRASE as MIN_EXPORT_PASSPHRASE
from tow.cli.parser import build_parser


def _utf8_console() -> None:
    """Russian texts and the em dash survive a redirected output or a cp1251/cp866 console: such
    a stream writes UTF-8 (an interactive UTF-8 terminal is left as it is). A terminal with
    another legacy encoding (a KOI8-R or ISO-8859 locale on Linux or macOS) keeps it - that is
    what it shows - and a character it lacks becomes "?" instead of ending the command."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "").replace("_", "")
        if encoding == "utf8" or not hasattr(stream, "reconfigure"):
            continue
        try:
            interactive = stream.isatty()
        except AttributeError, OSError, ValueError:
            interactive = False
        if not interactive or encoding.startswith("cp"):
            stream.reconfigure(encoding="utf-8", errors="replace")
        else:
            stream.reconfigure(errors="replace")


def _build_parser() -> argparse.ArgumentParser:
    """The parser of every command in the table."""
    return build_parser(commands.COMMANDS)


def _use_language(argv: list[str] | None) -> None:
    """A command typed in a terminal speaks the language chosen in config.yaml, or with ``auto``
    the system's; texts a scheduled task writes (messages, recorded errors) use the owner's
    message language (the browser's with ``auto``)."""
    from tow import i18n

    words = [word for word in (sys.argv[1:] if argv is None else argv) if not word.startswith("-")]
    try:
        interactive = sys.stdout.isatty()
    except AttributeError, ValueError:
        interactive = False
    # What `tow run` starts as its children: their texts go to messages and logs, not to a person.
    children = {command.name for command in commands.COMMANDS if command.background}
    background = bool(words) and words[0] in children and not interactive
    if not background:
        i18n.leave_language_unread_to_caller()  # a broken config.yaml is said once, by the command
    try:
        i18n.use(i18n.message_language() if background else i18n.terminal_language())
    except Exception:  # noqa: BLE001 - a broken config is reported by the command itself
        i18n.use(i18n.DEFAULT)


def _foreign_code_refusal() -> str:
    """Why this environment must not run TOW for its folder (``tow.paths.foreign_code``), or ""."""
    from tow import platform
    from tow.i18n import t
    from tow.paths import foreign_code, repo_root

    install = foreign_code()
    if install is None:
        return ""
    launcher = install / "app" / "scripts" / ("tow.cmd" if platform.is_windows() else "tow")
    return t("cli.foreign_code", code=repo_root(), root=install, setup=f'"{launcher}" setup')


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    # The command's language lives in its own context and ends with it: a caller in the same
    # thread (an embedding process, a test) keeps the language it had.
    return contextvars.copy_context().run(_main, argv)


def _main(argv: list[str] | None) -> int:
    _use_language(argv)  # first: `tow --help` is in the owner's language too
    args = _build_parser().parse_args(argv)
    command = next(row for row in commands.COMMANDS if row.name == args.cmd)  # the parser knows no other
    refusal = _foreign_code_refusal()
    if refusal:  # before anything is written in the folder (only config.yaml's language was read)
        if getattr(args, "json", False):
            print(render.machine({"ok": False, "error": refusal}, indent=None))
        else:
            print(refusal, file=sys.stderr)
        return EXIT_CANNOT_RUN
    if command.layout:
        from tow.supervisor import layout

        # Started by the OS or by hand without a launcher: find data/ and config.yaml first.
        os.environ.update(layout.child_env())
    # TOW writes only inside its install, for this user only: temporary files go to data/tmp,
    # also those of the processes it starts (TMP/TEMP/TMPDIR); umask 077 on Linux and macOS;
    # keys/ and data/ closed to other accounts (Windows ACLs, POSIX 0700).
    from tow.paths import use_private_temp
    from tow.platform import use_private_files
    from tow.store import protect_install_folders

    try:
        use_private_files()
        use_private_temp()
        if command.protects_folders:  # `tow permissions` reports and repairs them itself
            # Repaired before every command; said only by those that start TOW (not before each
            # `tow status`, nor twice at the first start: setup's `keys ensure`, then `start`).
            protect_install_folders(quiet=not command.warn_open_folders)
        return command.run(args)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except Exception as exc:
        # One boundary for every command: a broken config, an unreadable secret store or a
        # missing file is an exit code and a sentence, not a traceback. Unexpected exception
        # messages may contain a URL, key, or password supplied by a library: never echo them.
        # TOW_DEBUG=1 is the owner's explicit opt-in to the original traceback.
        if os.environ.get("TOW_DEBUG"):
            raise
        from tow.config import ConfigError
        from tow.i18n import t
        from tow.store import SecretStoreError, StateVersionError
        from tow.yaml_guard import YamlLimitError

        if isinstance(exc, (ConfigError, SecretStoreError, StateVersionError, YamlLimitError)):
            reason = str(exc)
        elif isinstance(exc, OSError) and exc.filename:
            # A folder TOW cannot write (data\ read-only, a full disk, a missing drive): named,
            # with the system's own words - a path and an OS message hold no password.
            reason = t("cli.folder_failed", path=exc.filename, error=exc.strerror or type(exc).__name__)
        else:
            reason = t("cli.command_failed")
        if getattr(args, "json", False):  # a script asked for JSON: it gets JSON on failure too
            print(render.machine({"ok": False, "error": reason}, indent=None))
        else:
            print(f"tow {args.cmd}: {reason}", file=sys.stderr)
        return EXIT_CANNOT_RUN
