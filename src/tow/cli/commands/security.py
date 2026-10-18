"""The commands that guard the install: its folders, network access, the password, the saved
passwords and tokens and their master key (``permissions``, ``access``, ``secrets``, ``keys``,
``password``)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from tow.cli import render
from tow.cli.command import EXIT_CANNOT_RUN, EXIT_OK, Command, Subcommands
from tow.cli.parser import add_command, json_option


def _add_permissions(sub: Subcommands) -> None:
    from tow.i18n import t

    perm = add_command(sub, "permissions", "cli.help.permissions", "cli.help.permissions_long")
    perm.add_argument(
        "permissions_action",
        nargs="?",
        choices=("status", "fix"),
        default="status",
        metavar="{status,fix}",
        help=t("cli.help.permissions_action"),
    )
    perm.add_argument("--owner", metavar="ACCOUNT", help=t("cli.help.permissions_owner"))
    json_option(perm)


def _permissions(args: argparse.Namespace) -> int:
    from tow import permissions

    result = permissions.fix(args.owner) if args.permissions_action == "fix" else permissions.status()
    if args.json:
        render.show(result, True)
    else:
        print(permissions.text(result))
    if args.permissions_action == "status":
        return EXIT_OK
    return EXIT_OK if result.get("ok") else EXIT_CANNOT_RUN


PERMISSIONS = Command("permissions", _add_permissions, _permissions, protects_folders=False)


def _ask(prompt: str, *, quiet_stdout: bool) -> str:
    """A line typed after ``prompt``; with ``quiet_stdout`` (--json) the prompt goes to stderr, so
    standard output holds the JSON alone."""
    if not quiet_stdout:
        return input(prompt)
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return input()


def _ask_password(*, quiet_stdout: bool = False) -> dict[str, Any]:
    """A new password typed twice, then its reminder, stored as the first-start page does it. A
    mistyped or too short password is refused before the reminder: no point typing one for it."""
    from getpass import getpass

    from tow import access
    from tow.auth import AuthConfigurationError, require_password_length
    from tow.i18n import t

    first = getpass(t("cli.password.prompt"))
    second = getpass(t("cli.password.repeat"))
    if first != second:
        raise AuthConfigurationError("web.password.differ")
    require_password_length(first)
    return access.set_password(first, second, _ask(t("cli.password.hint_prompt"), quiet_stdout=quiet_stdout))


def _add_access(sub: Subcommands) -> None:
    from tow.i18n import t

    access_cmd = add_command(sub, "access", "cli.help.access", "cli.help.access_long")
    access_cmd.add_argument(
        "access_action", choices=("on", "off", "status"), metavar="{on,off,status}", help=t("cli.help.access_action")
    )
    access_cmd.add_argument("--password", action="store_true", help=t("cli.help.access_password"))
    json_option(access_cmd)


def _access(args: argparse.Namespace) -> int:
    """Network access from other devices, for an install without a screen (this computer is
    the one running TOW, as on the Settings card). On needs a password: asked here when none
    is set (or with --password)."""
    import copy

    from tow import access, store_transaction
    from tow.auth import AuthConfigurationError
    from tow.bind import network_bind
    from tow.config import as_bool, load_config
    from tow.i18n import t
    from tow.log import log_event
    from tow.store import SecretStoreError, load_secrets, persistence_lock

    if args.access_action == "status":
        cfg = load_config()
        view = {"network": as_bool(cfg.get("allow_lan")), "bind": str(cfg.get("bind") or "127.0.0.1")}
        view["password_set"] = render.flag(access.password_is_set(load_secrets()))
        render.show(view, args.json, render.access_text)
        return EXIT_OK
    enabled = args.access_action == "on"
    try:
        if enabled and (args.password or not access.password_is_set(load_secrets())):
            # Checked, stored and every device signed out, as `tow password` does it - before the
            # network opens: never a network without its password.
            record = _ask_password(quiet_stdout=args.json)
            log_event("settings_password", changed="password", hint=bool(record.get("hint")), where="cli", how="manual")
        with persistence_lock():
            cfg = copy.deepcopy(load_config())
            bind = network_bind(cfg.get("bind")) if enabled else "127.0.0.1"
            cfg.update(allow_lan=enabled, bind=bind, setup_done=True)
            store_transaction.commit(config=cfg)
    except (AuthConfigurationError, SecretStoreError, store_transaction.TransactionError) as exc:
        render.show({"ok": False, "error": str(exc)}, args.json)
        return EXIT_CANNOT_RUN
    log_event("settings_access", allow_lan=enabled, bind=cfg["bind"], where="cli", how="manual")
    # Off works at once (every request reads the config); on needs the server to listen anew.
    message = t("cli.access.on") + " " + t("cli.access.restart") if enabled else t("cli.access.off")
    render.show({"ok": True, "network": enabled, "message": message}, args.json)
    return EXIT_OK


ACCESS = Command("access", _add_access, _access)


def _key_error(exc: Exception) -> str:
    """A master key failure in the owner's words (never the key itself)."""
    from tow.i18n import t
    from tow.store import MasterKeyError

    return t(f"cli.keys.error.{exc.kind}") if isinstance(exc, MasterKeyError) else str(exc)


def _store_view(status: dict[str, Any]) -> dict[str, Any]:
    """What ``secrets`` and ``keys`` say of the store: its state and where the master key comes
    from, each one of the store's fixed words (None for any other), never a value it holds."""
    from tow.store import KEY_SOURCES, STORAGE_STATES

    return {
        "storage": render.one_of(status.get("storage"), STORAGE_STATES),
        "key_source": render.one_of(status.get("key_source"), KEY_SOURCES),
    }


def _add_secrets(sub: Subcommands) -> None:
    from tow.i18n import t

    secrets_cmd = add_command(sub, "secrets", "cli.help.secrets", "cli.help.secrets_long")
    secrets_sub = secrets_cmd.add_subparsers(dest="secrets_action", required=True, metavar="<action>")
    json_option(add_command(secrets_sub, "status", "cli.help.secrets_status"))
    json_option(add_command(secrets_sub, "migrate", "cli.help.secrets_migrate"))
    secrets_generate = add_command(secrets_sub, "generate-key", "cli.help.secrets_generate")
    secrets_generate.add_argument("--key-file", type=Path, default=None, help=t("cli.help.key_file"))
    json_option(secrets_generate)


def _secrets(args: argparse.Namespace) -> int:
    from tow.store import (
        SecretStoreError,
        generate_master_key,
        migrate_legacy_secrets,
        secret_store_status,
    )

    try:
        if args.secrets_action == "status":
            render.show(_store_view(secret_store_status()), args.json, render.secrets_text)
        elif args.secrets_action == "migrate":
            migrated = render.flag(migrate_legacy_secrets())
            view = {"ok": True, "migrated": migrated, **_store_view(secret_store_status())}
            render.show(view, args.json, render.secrets_text)
        elif args.secrets_action == "generate-key":
            from tow.i18n import t

            # A relative --key-file is the terminal's folder: the message names the whole path.
            path = generate_master_key(args.key_file.absolute() if args.key_file else None)
            render.show(
                {"ok": True, "key_file_created": True, "message": t("cli.keys.generated", path=path)}, args.json
            )
        return 0
    except SecretStoreError as exc:
        render.show({"ok": False, "error": _key_error(exc)}, getattr(args, "json", False))
        return 3


SECRETS = Command("secrets", _add_secrets, _secrets)


def first_run_key(*, quiet_stdout: bool = False) -> Path | None:
    """First start: no master key anywhere and no secrets yet - create keys/master.key (through
    the store's own key writer) and say once, on the console and in the log, to back it up
    (on stderr with ``quiet_stdout``: --json keeps standard output for the JSON).
    None when a key is in use already, or when secrets exist (their key must be brought back,
    never replaced: the command that needs it says how)."""
    from tow.i18n import t
    from tow.log import log_event
    from tow.store import SecretStoreError, encrypted_secrets_path, generate_master_key, secret_store_status

    if secret_store_status()["key_source"] != "missing" or encrypted_secrets_path().is_file():
        return None
    try:
        path = generate_master_key()
    except SecretStoreError:
        return None
    print(t("cli.keys.first_run", path=path), file=sys.stderr if quiet_stdout else sys.stdout)
    log_event("master_key_created", how="auto")
    return path


def _add_keys(sub: Subcommands) -> None:
    from tow.i18n import t

    keys_cmd = add_command(sub, "keys", "cli.help.keys", "cli.help.keys_long")
    keys_sub = keys_cmd.add_subparsers(dest="keys_action", required=True, metavar="<action>")
    json_option(add_command(keys_sub, "status", "cli.help.keys_status"))
    keys_adopt = add_command(keys_sub, "adopt", "cli.help.keys_adopt")
    keys_adopt.add_argument("--from", dest="source", type=Path, default=None, help=t("cli.help.keys_from"))
    json_option(keys_adopt)
    json_option(add_command(keys_sub, "ensure", "cli.help.keys_ensure"))


def _keys(args: argparse.Namespace) -> int:
    from tow.i18n import t
    from tow.paths import key_file
    from tow.store import SecretStoreError, adopt_master_key, master_key_file, secret_store_status

    if args.keys_action == "ensure":
        created = first_run_key(quiet_stdout=args.json)
        if created is None and secret_store_status()["key_source"] == "missing":
            from tow.store import MissingMasterKeyError

            render.show({"ok": False, "error": str(MissingMasterKeyError("store.no_master_key"))}, args.json)
            return EXIT_CANNOT_RUN
        if args.json:
            render.show({"ok": True, "key_file_created": created is not None}, True)
        elif created is None:
            print(t("cli.keys.in_use"))
        return EXIT_OK

    if args.keys_action == "status":
        in_use = master_key_file()
        render.show(
            {
                "key_source": _store_view(secret_store_status())["key_source"],
                "key_file": str(in_use) if in_use else None,
                "install_key_file": str(key_file()),
                "install_key_exists": key_file().is_file(),
            },
            args.json,
            render.keys_text,
        )
        return 0
    try:
        result = adopt_master_key(args.source)
    except SecretStoreError as exc:
        render.show({"ok": False, "error": _key_error(exc)}, args.json)
        return 3
    key = "cli.keys.already" if result["already"] else "cli.keys.adopted"
    render.show({**result, "message": t(key, path=result["key_file"], count=result["secrets_checked"])}, args.json)
    return 0


KEYS = Command("keys", _add_keys, _keys)


def _add_password(sub: Subcommands) -> None:
    add_command(sub, "password", "cli.help.password", "cli.help.password_long")


def _password(_args: argparse.Namespace) -> int:
    """Reset a forgotten password on the computer running TOW (whoever runs this has the PC)."""
    from tow.auth import AuthConfigurationError
    from tow.i18n import t
    from tow.log import log_event
    from tow.store import SecretStoreError

    try:
        # The same path as the first-start page and Settings: checked, stored, every device signed out.
        record = _ask_password()
    except (AuthConfigurationError, SecretStoreError) as exc:
        render.show({"ok": False, "error": str(exc)}, False)
        return 3
    log_event("settings_password", changed="password", hint=bool(record.get("hint")), where="cli", how="manual")
    render.show({"ok": True, "message": t("web.password.saved")}, False)
    return 0


PASSWORD = Command("password", _add_password, _password)
