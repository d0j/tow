"""The commands that copy and bring back TOW's data: the ``.towx`` transfer file and the night
copies (``export``, ``import``, ``import-rollback``, ``backup``, ``restore-snapshot``)."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tow.cli import render
from tow.cli.command import Command, Subcommands
from tow.cli.parser import add_command, json_option

# The export file protects every secret with this passphrase alone (PBKDF2): a short one
# is guessable offline, so the CLI refuses it.
MIN_EXPORT_PASSPHRASE = 12

# Why a file was not imported, in the owner's words (tow.bundle.ExportImportError.reason). A
# `tow export` file is protected by a passphrase, not by the master key.
_IMPORT_REASONS = {
    "not_tow": "backup.restore_point.reason_not_tow",
    "other_key": "cli.bundle.wrong_passphrase",
    "too_large": "backup.restore_point.reason_too_large",
    "damaged": "backup.restore_point.reason_damaged",
}


def _guarded(
    args: argparse.Namespace,
    action: Callable[[], dict[str, Any]],
    failure: str,
    *,
    importing: bool = False,
    text: Callable[[Any], str] | None = None,
) -> int:
    """Print the result of a portable export/import step in the owner's words (``failure``: the
    catalog key said when nothing more precise is known); never leak an unexpected error.
    A refusal keeps its technical cause as ``detail``."""
    from tow.bundle import ExportImportError
    from tow.i18n import t

    try:
        render.show(action(), args.json, text)
        return 0
    except ExportImportError as exc:
        if exc.owner_text is not None:
            refusal = {"ok": False, "error": exc.owner_text.text()}
        else:
            why = t(_IMPORT_REASONS.get(exc.reason, _IMPORT_REASONS["damaged"]))
            error = t("cli.bundle.import_refused", reason=why) if importing else t(failure)
            refusal = {"ok": False, "error": error, "detail": str(exc)}
        render.show(refusal, getattr(args, "json", False))
        return 3
    except Exception:  # noqa: BLE001 - the CLI boundary: a fixed sentence, never a traceback that may hold a secret
        render.show({"ok": False, "error": t(failure)}, getattr(args, "json", False))
        return 3


def _add_export(sub: Subcommands) -> None:
    from tow.i18n import t

    export_cmd = add_command(sub, "export", "cli.help.export")
    export_cmd.add_argument("--output", required=True, type=Path, help=t("cli.help.export_output"))
    export_cmd.add_argument("--include-log", action="store_true", help=t("cli.help.export_log"))
    export_cmd.add_argument("--force", action="store_true", help=t("cli.help.export_force"))
    json_option(export_cmd)


def _export(args: argparse.Namespace) -> int:
    from getpass import getpass

    from tow.bundle import ExportImportError, export_bundle
    from tow.errors import Msg
    from tow.i18n import t

    output = args.output.absolute()  # a relative name is the terminal's folder: said in full

    def run() -> dict[str, Any]:
        if output.exists() and not args.force:  # said before the passphrase is typed twice
            raise ExportImportError(
                "export output already exists", owner_text=Msg("cli.bundle.output_exists", path=str(output))
            )
        first = getpass(t("cli.bundle.passphrase"))
        if len(first) < MIN_EXPORT_PASSPHRASE:
            raise ExportImportError(
                f"export passphrase must have at least {MIN_EXPORT_PASSPHRASE} characters",
                owner_text=Msg("cli.bundle.passphrase_short", length=MIN_EXPORT_PASSPHRASE),
            )
        second = getpass(t("cli.bundle.passphrase_repeat"))
        if first != second:
            raise ExportImportError("export passphrases do not match", owner_text=Msg("cli.bundle.passphrase_mismatch"))
        return export_bundle(output, first, include_log=args.include_log, overwrite=args.force)

    return _guarded(args, run, "cli.bundle.export_failed", text=render.export_text)


EXPORT = Command("export", _add_export, _export)


def _add_import(sub: Subcommands) -> None:
    from tow.i18n import t

    import_cmd = add_command(sub, "import", "cli.help.import")
    import_cmd.add_argument("--input", required=True, type=Path, help=t("cli.help.import_input"))
    import_cmd.add_argument("--path-map", action="append", default=[], help=t("cli.help.import_path_map"))
    import_cmd.add_argument("--apply", action="store_true", help=t("cli.help.import_apply"))
    import_cmd.add_argument(
        "--accept-security-settings", action="store_true", help=t("cli.help.import_accept_security")
    )
    json_option(import_cmd)


def _import(args: argparse.Namespace) -> int:
    from getpass import getpass

    from tow.bundle import ExportImportError, import_bundle, parse_path_maps, this_install_settings
    from tow.errors import Msg
    from tow.i18n import t

    def run() -> dict[str, Any]:
        # A missing file or a wrong --path-map is said before the passphrase is asked for.
        if not args.input.is_file():
            raise ExportImportError("import input missing", owner_text=Msg("cli.bundle.no_input", path=str(args.input)))
        parse_path_maps(args.path_map)
        passphrase = getpass(t("cli.bundle.passphrase"))
        # A file someone handed over must not open this TOW to the network with their password,
        # point its backups at their share or turn the home-network opt-ins on: this computer's
        # security settings stay unless the owner takes the file's by name.
        if args.accept_security_settings:
            return import_bundle(args.input, passphrase, apply=args.apply, path_maps=args.path_map)
        return import_bundle(
            args.input,
            passphrase,
            apply=args.apply,
            path_maps=args.path_map,
            config_overrides=this_install_settings(),
            preserve_secret_keys={"lan_auth"},
        )

    return _guarded(args, run, "cli.bundle.import_failed", importing=True, text=render.import_text)


IMPORT = Command("import", _add_import, _import)


def _add_import_rollback(sub: Subcommands) -> None:
    from tow.i18n import t

    rollback = add_command(sub, "import-rollback", "cli.help.import_rollback", "cli.help.import_rollback_long")
    rollback.add_argument("--checkpoint", required=True, type=Path, help=t("cli.help.rollback_checkpoint"))
    rollback.add_argument("--apply", action="store_true", help=t("cli.help.rollback_apply"))
    json_option(rollback)


def _import_rollback(args: argparse.Namespace) -> int:
    from tow.bundle import rollback_import

    return _guarded(
        args,
        lambda: rollback_import(args.checkpoint, apply=args.apply),
        "cli.bundle.rollback_failed",
        text=render.rollback_text,
    )


IMPORT_ROLLBACK = Command("import-rollback", _add_import_rollback, _import_rollback)


def _add_backup(sub: Subcommands) -> None:
    json_option(add_command(sub, "backup", "cli.help.backup", "cli.help.backup_long"))


def _backup(args: argparse.Namespace) -> int:
    from tow.snapshots import SnapshotError, create_snapshot

    try:
        render.show(create_snapshot(), args.json, render.backup_text)
    except SnapshotError as exc:
        render.show({"ok": False, "error": str(exc)}, args.json)
        return 3
    return 0


BACKUP = Command("backup", _add_backup, _backup, background=True)


def _add_restore_snapshot(sub: Subcommands) -> None:
    from tow.i18n import t

    restore = add_command(sub, "restore-snapshot", "cli.help.restore_snapshot", "cli.help.restore_snapshot_long")
    restore.add_argument("--path", required=True, type=Path, help=t("cli.help.restore_path"))
    restore.add_argument("--apply", action="store_true", help=t("cli.help.restore_apply"))
    json_option(restore)


def _restore_snapshot(args: argparse.Namespace) -> int:
    from tow.snapshots import SnapshotError, restore_snapshot

    try:
        result = restore_snapshot(args.path, apply=args.apply)
    except SnapshotError as exc:
        render.show({"ok": False, "error": str(exc)}, args.json)
        return 3
    render.show(result, args.json, render.restore_text)
    return 0


RESTORE_SNAPSHOT = Command("restore-snapshot", _add_restore_snapshot, _restore_snapshot)
