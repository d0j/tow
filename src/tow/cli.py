from __future__ import annotations

import argparse
import contextvars
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

from tow import __version__


def secrets_ok() -> bool:
    from tow.clients.factory import client_configurations, client_secret_block
    from tow.config import load_config
    from tow.store import SecretStoreError, load_secrets

    try:
        cfg = load_config()
        secrets = load_secrets()
        for configuration in client_configurations(cfg):
            if configuration.get("enabled", True) and client_secret_block(cfg, secrets, str(configuration["id"])).get(
                "host"
            ):
                return True
    except SecretStoreError, TypeError, ValueError:
        return False
    return False


# The export file protects every secret with this passphrase alone (PBKDF2): a short one
# is guessable offline, so the CLI refuses it.
MIN_EXPORT_PASSPHRASE = 12

# Exit codes, the same for every command (`tow --help` lists them).
EXIT_OK = 0
EXIT_USAGE = 1  # a wrong command or option
EXIT_PARTIAL = 2  # done in part: a topic, a mirror or a step failed
EXIT_CANNOT_RUN = 3  # cannot run: config, secrets, the lock, a missing file
EXIT_INTERRUPTED = 130


def _launcher() -> str:
    """The launcher of this install as this system writes it (``tow setup`` is its command)."""
    from tow import platform

    return "scripts\\tow.cmd" if platform.is_windows() else "scripts/tow"


def _utf8_console() -> None:
    """Russian texts and the em dash survive a redirected output or a cp1251/cp866 console: such
    a stream writes UTF-8 (an interactive UTF-8 terminal is left as it is)."""
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


def _print(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(_human(data))


def _human(data: Any) -> str:
    """Readable text for a person (not a Python dict repr); --json stays machine output."""
    import yaml

    from tow.i18n import t

    if isinstance(data, dict) and isinstance(data.get("results"), list):
        lines = [
            t("cli.check.client", value=data.get("qbit"))
            + ("  " + t("cli.check.preview") if data.get("preview") else "")
        ]
        for row in data["results"]:
            mark = "ok " if row.get("ok") else "ERR"
            state = (
                row.get("error")
                or row.get("skipped")
                or row.get("status")
                or (t("web.check.changed") if row.get("changed") else "")
            )
            lines.append(f"{mark} {str(row.get('title') or row.get('id'))[:70]}" + (f" — {state}" if state else ""))
        failed = sum(1 for row in data["results"] if not row.get("ok"))
        lines.append(t("cli.check.total", n=len(data["results"]), failed=failed))
        return "\n".join(lines)
    if isinstance(data, (dict, list)):
        return str(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)).rstrip()
    return str(data)


class _Parser(argparse.ArgumentParser):
    """argparse with TOW's exit codes: a wrong command or option is 1, not argparse's 2 (2 means
    "done in part" in TOW, see ``tow --help``)."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: {message}\n")


def _build_parser() -> argparse.ArgumentParser:
    from tow.i18n import t

    p = _Parser(
        prog="tow",
        description=t("cli.help.description"),
        epilog=t("cli.help.epilog", launcher=_launcher()),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>")
    as_json = t("cli.help.json")

    v = sub.add_parser("version", help=t("cli.help.version"), description=t("cli.help.version"))
    v.add_argument("--json", action="store_true", help=as_json)
    st = sub.add_parser("status", help=t("cli.help.status"), description=t("cli.help.status"))
    st.add_argument("--json", action="store_true", help=as_json)
    d = sub.add_parser("doctor", help=t("cli.help.doctor"), description=t("cli.help.doctor"))
    d.add_argument("--json", action="store_true", help=as_json)
    d.add_argument("--notify", action="store_true", help=t("cli.help.doctor_notify"))
    im = sub.add_parser("import-monitorrent", help=t("cli.help.import_monitorrent"))
    im.add_argument("--json", action="store_true", help=as_json)
    im.add_argument("--db", required=True, type=Path, help=t("cli.help.monitorrent_db"))
    im.add_argument("--client", help=t("cli.help.monitorrent_client"))
    im.add_argument("--apply", action="store_true", help=t("cli.help.monitorrent_apply"))
    c = sub.add_parser("check", help=t("cli.help.check"), description=t("cli.help.check"))
    c.add_argument("--json", action="store_true", help=as_json)
    c.add_argument("--apply", action="store_true", help=t("cli.help.check_apply"))
    c.add_argument("--dry-run", action="store_true", help=t("cli.help.check_dry_run"))
    c.add_argument("--notify", action="store_true", help=t("cli.help.check_notify"))
    # A person running the CLI by hand; the scheduled task omits it (the header countdown follows auto runs).
    c.add_argument("--manual", action="store_true", help=argparse.SUPPRESS)
    # G2: the progress pass of `tow run` - completions from the torrent client only, no tracker requests.
    c.add_argument("--progress-only", action="store_true", help=argparse.SUPPRESS)
    scope = c.add_mutually_exclusive_group()
    scope.add_argument("--global-only", action="store_true", help=argparse.SUPPRESS)
    scope.add_argument("--timer-only", action="store_true", help=argparse.SUPPRESS)
    # The web page alone (`tow run` serves it together with the schedule): not listed.
    s = sub.add_parser("serve", description=t("cli.help.serve"))
    s.add_argument("--host", default=None, help=t("cli.help.serve_host"))
    s.add_argument("--port", type=int, default=None, help=t("cli.help.serve_port"))
    s.add_argument("--log-file", type=Path, default=None, help=t("cli.help.serve_log"))
    s.add_argument("--parent-pid", type=int, default=None, help=argparse.SUPPRESS)  # `tow run`: end with it
    access_cmd = sub.add_parser("access", help=t("cli.help.access"), description=t("cli.help.access_long"))
    access_cmd.add_argument("access_action", choices=("on", "off", "status"), help=t("cli.help.access_action"))
    access_cmd.add_argument("--password", action="store_true", help=t("cli.help.access_password"))
    access_cmd.add_argument("--json", action="store_true", help=as_json)
    secrets_cmd = sub.add_parser("secrets", help=t("cli.help.secrets"))
    secrets_sub = secrets_cmd.add_subparsers(dest="secrets_action", required=True, metavar="<action>")
    secrets_status = secrets_sub.add_parser("status", help=t("cli.help.secrets_status"))
    secrets_status.add_argument("--json", action="store_true", help=as_json)
    secrets_migrate = secrets_sub.add_parser("migrate", help=t("cli.help.secrets_migrate"))
    secrets_migrate.add_argument("--json", action="store_true", help=as_json)
    secrets_generate = secrets_sub.add_parser("generate-key", help=t("cli.help.secrets_generate"))
    secrets_generate.add_argument("--key-file", type=Path, default=None, help=t("cli.help.key_file"))
    secrets_generate.add_argument("--json", action="store_true", help=as_json)
    keys_cmd = sub.add_parser("keys", help=t("cli.help.keys"))
    keys_sub = keys_cmd.add_subparsers(dest="keys_action", required=True, metavar="<action>")
    keys_status = keys_sub.add_parser("status", help=t("cli.help.keys_status"))
    keys_status.add_argument("--json", action="store_true", help=as_json)
    keys_adopt = keys_sub.add_parser("adopt", help=t("cli.help.keys_adopt"))
    keys_adopt.add_argument("--from", dest="source", type=Path, default=None, help=t("cli.help.keys_from"))
    keys_adopt.add_argument("--json", action="store_true", help=as_json)
    keys_ensure = keys_sub.add_parser("ensure", help=t("cli.help.keys_ensure"))
    keys_ensure.add_argument("--json", action="store_true", help=as_json)
    export_cmd = sub.add_parser("export", help=t("cli.help.export"), description=t("cli.help.export"))
    export_cmd.add_argument("--output", required=True, type=Path, help=t("cli.help.export_output"))
    export_cmd.add_argument("--include-log", action="store_true", help=t("cli.help.export_log"))
    export_cmd.add_argument("--force", action="store_true", help=t("cli.help.export_force"))
    export_cmd.add_argument("--json", action="store_true", help=as_json)
    import_cmd = sub.add_parser("import", help=t("cli.help.import"), description=t("cli.help.import"))
    import_cmd.add_argument("--input", required=True, type=Path, help=t("cli.help.import_input"))
    import_cmd.add_argument("--path-map", action="append", default=[], help=t("cli.help.import_path_map"))
    import_cmd.add_argument("--apply", action="store_true", help=t("cli.help.import_apply"))
    import_cmd.add_argument("--json", action="store_true", help=as_json)
    import_rollback = sub.add_parser("import-rollback", help=t("cli.help.import_rollback"))
    import_rollback.add_argument("--checkpoint", required=True, type=Path, help=t("cli.help.rollback_checkpoint"))
    import_rollback.add_argument("--apply", action="store_true", help=t("cli.help.rollback_apply"))
    import_rollback.add_argument("--json", action="store_true", help=as_json)
    backup = sub.add_parser("backup", help=t("cli.help.backup"))
    backup.add_argument("--json", action="store_true", help=as_json)
    restore = sub.add_parser("restore-snapshot", help=t("cli.help.restore_snapshot"))
    restore.add_argument("--path", required=True, type=Path, help=t("cli.help.restore_path"))
    restore.add_argument("--apply", action="store_true", help=t("cli.help.restore_apply"))
    restore.add_argument("--json", action="store_true", help=as_json)
    watchdog = sub.add_parser("watchdog", help=t("cli.help.watchdog"), description=t("cli.help.watchdog"))
    watchdog.add_argument("--json", action="store_true", help=as_json)
    sub.add_parser("password", help=t("cli.help.password"))
    sub.add_parser("run", help=t("cli.help.run"))
    start = sub.add_parser("start", help=t("cli.help.start"), description=t("cli.help.start"))
    start.add_argument("--no-browser", action="store_true", help=t("cli.help.start_no_browser"))
    start.add_argument("--wait", type=float, default=120.0, help=t("cli.help.start_wait"))
    stop = sub.add_parser("stop", help=t("cli.help.stop"))
    stop.add_argument("--wait", type=float, default=60.0, help=t("cli.help.stop_wait"))
    sub.add_parser("restart", help=t("cli.help.restart"))
    sub.add_parser("setup", help=t("cli.help.setup"), description=t("cli.help.setup"))
    autostart = sub.add_parser("autostart", help=t("cli.help.autostart"), description=t("cli.help.autostart"))
    # "migrate" (1.18-1.20) only points to the documentation now: accepted, not offered.
    autostart.add_argument(
        "autostart_action", choices=("on", "off", "status", "migrate"), help=t("cli.help.autostart_action")
    )
    autostart.add_argument("--without-login", action="store_true", help=t("cli.help.autostart_without_login"))
    autostart.add_argument("--apply", action="store_true", help=argparse.SUPPRESS)  # migrate (1.18-1.20)
    autostart.add_argument("--json", action="store_true", help=as_json)
    update = sub.add_parser("update", help=t("cli.help.update"))
    update.add_argument("--ref", required=True, help=t("cli.help.update_ref"))
    return p


def _guarded(args: argparse.Namespace, action: Callable[[], dict[str, Any]], failure: str) -> int:
    """Print the result of a portable export/import step; never leak an unexpected error."""
    from tow.bundle import ExportImportError

    try:
        _print(action(), args.json)
        return 0
    except ExportImportError as exc:
        _print({"ok": False, "error": str(exc)}, getattr(args, "json", False))
        return 3
    except Exception:  # noqa: BLE001 - the CLI boundary: a fixed sentence, never a traceback that may hold a secret
        _print({"ok": False, "error": failure}, getattr(args, "json", False))
        return 3


def _cmd_version(args: argparse.Namespace) -> int:
    _print({"version": __version__}, getattr(args, "json", False))
    return 0


def _cmd_secrets(args: argparse.Namespace) -> int:
    from tow.store import (
        SecretStoreError,
        generate_master_key,
        migrate_legacy_secrets,
        secret_store_status,
    )

    try:
        if args.secrets_action == "status":
            _print(secret_store_status(), args.json)
        elif args.secrets_action == "migrate":
            migrated = migrate_legacy_secrets()
            _print({"ok": True, "migrated": migrated, **secret_store_status()}, args.json)
        elif args.secrets_action == "generate-key":
            from tow.i18n import t

            path = generate_master_key(args.key_file)
            _print({"ok": True, "key_file_created": True, "message": t("cli.keys.generated", path=path)}, args.json)
        return 0
    except SecretStoreError as exc:
        _print({"ok": False, "error": _key_error(exc)}, getattr(args, "json", False))
        return 3


def _key_error(exc: Exception) -> str:
    """A master key failure in the owner's words (never the key itself)."""
    from tow.i18n import t
    from tow.store import MasterKeyError

    return t(f"cli.keys.error.{exc.kind}") if isinstance(exc, MasterKeyError) else str(exc)


def _first_run_key() -> Path | None:
    """First start: no master key anywhere and no secrets yet - create keys/master.key (through
    the store's own key writer) and say once, on the console and in the log, to back it up.
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
    print(t("cli.keys.first_run", path=path))
    log_event("master_key_created", how="auto")
    return path


def _cmd_keys(args: argparse.Namespace) -> int:
    from tow.i18n import t
    from tow.paths import key_file
    from tow.store import SecretStoreError, adopt_master_key, master_key_file, secret_store_status

    if args.keys_action == "ensure":
        created = _first_run_key()
        if created is None and secret_store_status()["key_source"] == "missing":
            from tow.store import MissingMasterKeyError

            _print({"ok": False, "error": str(MissingMasterKeyError("store.no_master_key"))}, args.json)
            return EXIT_CANNOT_RUN
        if args.json:
            _print({"ok": True, "key_file_created": created is not None}, True)
        elif created is None:
            print(t("cli.keys.in_use"))
        return EXIT_OK

    if args.keys_action == "status":
        in_use = master_key_file()
        _print(
            {
                "key_source": secret_store_status()["key_source"],
                "key_file": str(in_use) if in_use else None,
                "install_key_file": str(key_file()),
                "install_key_exists": key_file().is_file(),
            },
            args.json,
        )
        return 0
    try:
        result = adopt_master_key(args.source)
    except SecretStoreError as exc:
        _print({"ok": False, "error": _key_error(exc)}, args.json)
        return 3
    key = "cli.keys.already" if result["already"] else "cli.keys.adopted"
    _print({**result, "message": t(key, path=result["key_file"], count=result["secrets_checked"])}, args.json)
    return 0


def _cmd_password(_args: argparse.Namespace) -> int:
    """Reset a forgotten password on the computer running TOW (whoever runs this has the PC)."""
    from getpass import getpass

    from tow import access
    from tow.auth import AuthConfigurationError
    from tow.i18n import t
    from tow.log import log_event
    from tow.store import SecretStoreError

    try:
        first = getpass(t("cli.password.prompt"))
        second = getpass(t("cli.password.repeat"))
        if first != second:  # asked before the reminder: no point typing one for a mistyped password
            raise AuthConfigurationError("web.password.differ")
        # The same path as the first-start page and Settings: checked, stored, every device signed out.
        record = access.set_password(first, second, input(t("cli.password.hint_prompt")))
    except (AuthConfigurationError, SecretStoreError) as exc:
        _print({"ok": False, "error": str(exc)}, False)
        return 3
    log_event("settings_password", changed="password", hint=bool(record.get("hint")), where="cli", how="manual")
    _print({"ok": True, "message": t("web.password.saved")}, False)
    return 0


def _cmd_import_monitorrent(args: argparse.Namespace) -> int:
    from tow.import_monitorrent import MonitorrentImportError, import_monitorrent

    try:
        _print(import_monitorrent(args.db, apply=args.apply, client_id=args.client), args.json)
    except MonitorrentImportError as exc:
        _print({"ok": False, "error": str(exc)}, args.json)
        return 3
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    from getpass import getpass

    from tow.bundle import ExportImportError, export_bundle

    def run() -> dict[str, Any]:
        first = getpass("TOW export passphrase: ")
        if len(first) < MIN_EXPORT_PASSPHRASE:
            raise ExportImportError(f"export passphrase must have at least {MIN_EXPORT_PASSPHRASE} characters")
        second = getpass("Repeat TOW export passphrase: ")
        if first != second:
            raise ExportImportError("export passphrases do not match")
        return export_bundle(args.output, first, include_log=args.include_log, overwrite=args.force)

    return _guarded(args, run, "export failed safely")


def _cmd_import(args: argparse.Namespace) -> int:
    from getpass import getpass

    from tow.bundle import import_bundle

    def run() -> dict[str, Any]:
        passphrase = getpass("TOW export passphrase: ")
        return import_bundle(args.input, passphrase, apply=args.apply, path_maps=args.path_map)

    return _guarded(args, run, "import failed safely")


def _cmd_import_rollback(args: argparse.Namespace) -> int:
    from tow.bundle import rollback_import

    return _guarded(args, lambda: rollback_import(args.checkpoint, apply=args.apply), "import rollback failed safely")


def _cmd_doctor(args: argparse.Namespace) -> int:
    from tow.doctor import doctor_report, doctor_text
    from tow.notify import send as notify_send
    from tow.store import load_secrets

    report = doctor_report(probe=True)
    text = doctor_text(report)
    if args.notify:
        notify_send(load_secrets(), "tow doctor: " + text.replace("\n", " | ")[:3500])
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(text)
    if not secrets_ok():
        return 3
    return 0 if report.get("ok") else 2


def _cmd_check(args: argparse.Namespace) -> int:
    from tow.check import record_check_failure, run_check
    from tow.store import SecretStoreError, check_state_version

    check_state_version()  # a newer TOW's state.json: refused before anything is recorded
    apply = bool(args.apply) and not args.dry_run
    how = "manual" if args.manual else "progress" if args.progress_only else "timer" if args.timer_only else "auto"
    try:
        out = run_check(
            apply=apply,
            notify=args.notify,
            how=how,
            progress_only=args.progress_only,
            scheduled_scope="global" if args.global_only else "timer" if args.timer_only else "",
        )
    except SecretStoreError as exc:
        if apply:
            record_check_failure(exc, how=how)
        _print({"ok": False, "error": str(exc), "blocked": True}, args.json)
        return 3
    except Exception as exc:  # noqa: BLE001 - store, transaction, client library: recorded instead of a bare traceback
        if apply:
            record_check_failure(exc, how=how)
        from tow.i18n import t

        _print({"ok": False, "error": t("cli.command_failed")}, args.json)
        return EXIT_CANNOT_RUN
    _print(out, args.json)
    fails = [r for r in out["results"] if not r.get("ok")]
    return 2 if fails else 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from tow.bind import validate_bind
    from tow.config import as_bool, load_config, port_of
    from tow.store import check_state_version

    check_state_version()  # a newer TOW's state.json: refused, never served or rewritten
    _first_run_key()
    cfg = load_config()
    try:
        host = validate_bind(args.host or cfg.get("bind"), allow_lan=as_bool(cfg.get("allow_lan")))
    except ValueError as exc:
        from tow.i18n import t

        print(t("cli.serve_blocked", error=exc))
        return 3
    port = args.port or port_of(cfg)
    log_config = _serve_log_config(args.log_file) if getattr(args, "log_file", None) else None
    server = uvicorn.Server(uvicorn.Config("tow.web:app", host=host, port=port, reload=False, log_config=log_config))
    parent = getattr(args, "parent_pid", None)
    if parent:
        from tow.supervisor.parent import end_with_parent

        end_with_parent(int(parent), server)  # started by `tow run`: never outlives it
    server.run()
    return 0


def _serve_log_config(path: Path) -> dict[str, Any]:
    """uvicorn's log to a rotated file: the task runs hidden, its console goes nowhere."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": str(path),
        "maxBytes": 5 * 1024 * 1024,
        "backupCount": 3,
        "encoding": "utf-8",
        "formatter": "plain",
    }
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
        "handlers": {"file": handler},
        "loggers": {
            name: {"handlers": ["file"], "level": "INFO", "propagate": False}
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
        },
    }


def _cmd_backup(args: argparse.Namespace) -> int:
    from tow.snapshots import SnapshotError, create_snapshot

    try:
        _print(create_snapshot(), args.json)
    except SnapshotError as exc:
        _print({"ok": False, "error": str(exc)}, args.json)
        return 3
    return 0


def _cmd_restore_snapshot(args: argparse.Namespace) -> int:
    from tow.snapshots import SnapshotError, restore_snapshot

    try:
        result = restore_snapshot(args.path, apply=args.apply)
    except SnapshotError as exc:
        _print({"ok": False, "error": str(exc)}, args.json)
        return 3
    _print(result, args.json)
    return 0


def _cmd_watchdog(args: argparse.Namespace) -> int:
    from tow.watchdog import run_watchdog

    report = run_watchdog()
    _print(report, args.json)
    return 0 if report["service_ok"] and report["checks_ok"] else 2


def _interrupted_update() -> bool:
    """An update cut off mid-switch: the code in app/ may be half new; the update puts it right."""
    from tow.i18n import t
    from tow.supervisor import layout

    if not layout.interrupted_update():
        return False
    print(t("cli.run.update_interrupted", root=layout.install_root()), flush=True)
    return True


def _cmd_run(_args: argparse.Namespace) -> int:
    from tow.store import check_state_version
    from tow.supervisor import run_supervisor

    if _interrupted_update():
        return EXIT_CANNOT_RUN
    check_state_version()  # a newer TOW's state.json: refused, never served or rewritten
    _first_run_key()
    return run_supervisor()


def _cmd_start(args: argparse.Namespace) -> int:
    """`tow run` in the background (unless it runs) and its page in the browser: the start files
    of every install run this. TOW_NO_BROWSER=1 (tests, a server) never opens a browser."""
    from tow.config import load_config, port_of
    from tow.i18n import t
    from tow.supervisor import layout, starter

    if _interrupted_update():
        return EXIT_CANNOT_RUN
    port = port_of(load_config())
    browser = not args.no_browser and os.environ.get("TOW_NO_BROWSER", "").strip().lower() not in ("1", "true", "yes")
    if layout.running() is None:
        print(t("cli.start.starting"), flush=True)
    result = starter.start(port, wait=args.wait, browser=browser)
    logs = layout.logs_dir()
    if result["state"] == "exited":
        print(t("cli.start.exited", log=logs / "run.log", stderr=starter.stderr_log()))
        return EXIT_CANNOT_RUN
    if result["state"] == "timeout":
        print(t("cli.start.timeout", seconds=int(args.wait), log=logs / "run.log"))
        return EXIT_CANNOT_RUN
    print(t("cli.start.ready", url=result["url"]))
    if browser and not result["browser"]:
        print(t("cli.start.open_by_hand", url=result["url"]))
    return EXIT_OK


def _cmd_stop(args: argparse.Namespace) -> int:
    from tow.i18n import t
    from tow.supervisor import request_stop, wait_stopped

    if request_stop(by="cli") is None:
        print(t("cli.run.not_running"))
        return 0
    if args.wait <= 0:
        print(t("cli.run.stop_requested"))
        return 0
    if wait_stopped(args.wait):
        print(t("cli.run.stopped"))
        return 0
    print(t("cli.run.still_stopping"))
    return 2


def _cmd_restart(_args: argparse.Namespace) -> int:
    from tow.i18n import t
    from tow.supervisor import request_restart

    if request_restart(by="cli") is None:
        print(t("cli.run.not_running"))
        return EXIT_CANNOT_RUN
    print(t("cli.run.restart_requested"))
    return 0


def _cmd_autostart(args: argparse.Namespace) -> int:
    from tow.autostart import backend
    from tow.i18n import t

    if args.autostart_action == "migrate":
        # The switch from the five Windows tasks of 1.17 was in 1.18-1.20; this version has none.
        print(t("autostart.migrate.gone"))
        return 0
    chosen = backend()
    if args.autostart_action == "status":
        _print(chosen.status(), args.json)
        return 0
    result = chosen.enable(without_login=args.without_login) if args.autostart_action == "on" else chosen.disable()
    if args.json:
        _print(result, True)
    elif result.get("ok"):
        print(t("autostart.done_on") if args.autostart_action == "on" else t("autostart.done_off"))
        if result.get("hint"):
            print(result["hint"])
    else:
        print(t("autostart.failed", error=result.get("error") or t("autostart.not_confirmed")))
    return 0 if result.get("ok") else 2


def _cmd_update(args: argparse.Namespace) -> int:
    """The update replaces this very environment, so it cannot run from it: say how to run it."""
    from tow import platform
    from tow.i18n import t
    from tow.paths import repo_root

    script = repo_root() / "scripts" / "update.py"
    python = getattr(sys, "_base_executable", None) or sys.executable  # the venv's base interpreter
    print(t("cli.update.how"))
    print(f'  "{python}" "{script}" --ref {args.ref}')
    if not (repo_root() / ".git").exists():  # the Windows bundle, install.ps1, install.sh
        print(t("cli.update.archive"))
    if platform.is_windows():
        print(t("cli.update.or_windows"))
        print(f'  pwsh -File "{repo_root() / "scripts" / "deploy.ps1"}" -Ref {args.ref}')
    return 0


def _cmd_setup(_args: argparse.Namespace) -> int:
    """`setup` belongs to the launcher: it rebuilds this very environment, so it cannot run from it."""
    from tow.i18n import t

    print(t("cli.setup.use_launcher", launcher=_launcher()))
    return EXIT_OK


def _status_view() -> dict[str, Any]:
    """What `tow status` reports: read from the files only (no client, no site is asked)."""
    from datetime import UTC, datetime

    from tow.clients.factory import client_configuration, client_name
    from tow.clock import format_ui_timestamp
    from tow.config import as_bool, interval_sec_of, load_config
    from tow.store import load_state
    from tow.supervisor import layout

    cfg = load_config()
    state = load_state(quarantine=False)
    raw_health = state.get("health")
    health: dict[str, Any] = raw_health if isinstance(raw_health, dict) else {}
    next_at = layout.next_check_at(interval_sec_of(cfg), health)  # the countdown of the header
    try:
        client_title = client_name(client_configuration(cfg))
    except RuntimeError, ValueError:
        client_title = ""
    try:
        from tow.autostart import backend

        autostart: bool | None = bool(backend().status().get("on"))
    except Exception:  # noqa: BLE001 - autostart is read from the system; its failure is "unknown"
        autostart = None
    running = layout.running()
    return {
        "version": __version__,
        "running": running is not None,
        "pid": (running or {}).get("pid"),
        "client": client_title,
        "client_ok": health.get("qbit_ok") if "qbit_ok" in health else None,
        "sites": len(cfg.get("trackers") or {}),
        "topics": len(state.get("topics") or []),
        "last_check": health.get("at") or None,
        "last_check_ok": bool(health.get("check_ok", True)) if health else None,
        "next_check": format_ui_timestamp(datetime.fromtimestamp(next_at, UTC).isoformat()) if next_at else None,
        "network": as_bool(cfg.get("allow_lan")),
        "autostart": autostart,
    }


def _cmd_status(args: argparse.Namespace) -> int:
    """One line on TOW for a terminal or a monitoring script (--json)."""
    from tow.i18n import t

    view = _status_view()
    if args.json:
        _print(view, True)
        return EXIT_OK
    yes_no = {True: t("cli.status.yes"), False: t("cli.status.no"), None: "?"}
    client = view["client"] or t("cli.status.none")
    if view["client_ok"] is not None:
        client += " " + (t("cli.status.answers") if view["client_ok"] else t("cli.status.not_answering"))
    parts = [
        f"TOW {view['version']}",
        t("cli.status.running", pid=view["pid"] or "?") if view["running"] else t("cli.status.stopped"),
        client,
        t("cli.status.sites", n=view["sites"]),
        t("cli.status.topics", n=view["topics"]),
        t("cli.status.last", at=view["last_check"] or t("cli.status.never"))
        + ("" if view["last_check_ok"] is not False else " " + t("cli.status.failed")),
        t("cli.status.next", at=view["next_check"] or "—"),
        t("cli.status.autostart", value=yes_no[view["autostart"]]),
    ]
    print(" · ".join(parts))
    return EXIT_OK


def _cmd_access(args: argparse.Namespace) -> int:
    """Network access from other devices, for an install without a screen (this computer is
    the one running TOW, as on the Settings card). On needs a password: asked here when none
    is set (or with --password)."""
    import copy
    from getpass import getpass

    from tow import access, store_transaction
    from tow.auth import AuthConfigurationError
    from tow.config import as_bool, load_config
    from tow.i18n import t
    from tow.log import log_event
    from tow.store import SecretStoreError, load_secrets, persistence_lock

    if args.access_action == "status":
        cfg = load_config()
        view = {"network": as_bool(cfg.get("allow_lan")), "bind": str(cfg.get("bind") or "127.0.0.1")}
        view["password_set"] = access.password_is_set(load_secrets())
        _print(view, args.json)
        return EXIT_OK
    enabled = args.access_action == "on"
    try:
        if enabled and (args.password or not access.password_is_set(load_secrets())):
            first = getpass(t("cli.password.prompt"))
            second = getpass(t("cli.password.repeat"))
            if first != second:
                raise AuthConfigurationError("web.password.differ")
            # Checked, stored and every device signed out, as `tow password` does it - before the
            # network opens: never a network without its password.
            record = access.set_password(first, second, input(t("cli.password.hint_prompt")))
            log_event("settings_password", changed="password", hint=bool(record.get("hint")), where="cli", how="manual")
        with persistence_lock():
            cfg = copy.deepcopy(load_config())
            cfg.update(allow_lan=enabled, bind="0.0.0.0" if enabled else "127.0.0.1", setup_done=True)
            store_transaction.commit(config=cfg)
    except (AuthConfigurationError, SecretStoreError, store_transaction.TransactionError) as exc:
        _print({"ok": False, "error": str(exc)}, args.json)
        return EXIT_CANNOT_RUN
    log_event("settings_access", allow_lan=enabled, bind=cfg["bind"], where="cli", how="manual")
    message = t("cli.access.on") if enabled else t("cli.access.off")
    _print({"ok": True, "network": enabled, "message": message + " " + t("cli.access.restart")}, args.json)
    return EXIT_OK


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "access": _cmd_access,
    "setup": _cmd_setup,
    "status": _cmd_status,
    "autostart": _cmd_autostart,
    "update": _cmd_update,
    "run": _cmd_run,
    "start": _cmd_start,
    "stop": _cmd_stop,
    "restart": _cmd_restart,
    "version": _cmd_version,
    "secrets": _cmd_secrets,
    "keys": _cmd_keys,
    "import-monitorrent": _cmd_import_monitorrent,
    "export": _cmd_export,
    "import": _cmd_import,
    "import-rollback": _cmd_import_rollback,
    "doctor": _cmd_doctor,
    "check": _cmd_check,
    "serve": _cmd_serve,
    "watchdog": _cmd_watchdog,
    "backup": _cmd_backup,
    "restore-snapshot": _cmd_restore_snapshot,
    "password": _cmd_password,
}


# Commands that run without tow.cmd/tow-env.cmd (autostart, an update, the owner in a terminal).
_LAYOUT_COMMANDS = frozenset({"run", "start", "stop", "restart", "autostart"})


def _use_message_language() -> None:
    """Texts a scheduled task writes (messages, recorded errors) use the owner's language."""
    from tow import i18n

    try:
        i18n.use(i18n.message_language())
    except Exception:  # noqa: BLE001 - a broken config is reported by the command itself
        i18n.use(i18n.DEFAULT)


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    # The command's language lives in its own context and ends with it: a caller in the same
    # thread (an embedding process, a test) keeps the language it had.
    return contextvars.copy_context().run(_main, argv)


def _main(argv: list[str] | None) -> int:
    _use_message_language()  # first: `tow --help` is in the owner's language too
    args = _build_parser().parse_args(argv)
    command = _COMMANDS.get(args.cmd)
    if command is None:
        return EXIT_USAGE
    if args.cmd in _LAYOUT_COMMANDS:
        from tow.supervisor import layout

        # Started by the OS or by hand without a launcher: find data/ and config.yaml first.
        os.environ.update(layout.child_env())
    # TOW writes only inside its install, for this user only: temporary files go to data/tmp,
    # also those of the processes it starts (TMP/TEMP/TMPDIR); umask 077 on Linux and macOS.
    from tow.paths import use_private_temp
    from tow.platform import use_private_files

    use_private_files()
    use_private_temp()
    try:
        return command(args)
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

        reason = (
            str(exc)
            if isinstance(exc, (ConfigError, SecretStoreError, StateVersionError, YamlLimitError))
            else t("cli.command_failed")
        )
        if getattr(args, "json", False):  # a script asked for JSON: it gets JSON on failure too
            print(json.dumps({"ok": False, "error": reason}, ensure_ascii=False))
        else:
            print(f"tow {args.cmd}: {reason}", file=sys.stderr)
        return EXIT_CANNOT_RUN


if __name__ == "__main__":
    raise SystemExit(main())
