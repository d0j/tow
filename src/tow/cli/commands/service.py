"""The commands of the service: what runs, starts, stops and reports on TOW, and how it is installed
and updated (``version``, ``status``, ``serve``, ``watchdog``, ``run``, ``start``, ``stop``,
``restart``, ``setup``, ``autostart``, ``update``)."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

from tow import __version__
from tow.cli import render
from tow.cli.command import EXIT_CANNOT_RUN, EXIT_OK, EXIT_USAGE, Command, Subcommands
from tow.cli.commands.security import first_run_key
from tow.cli.parser import add_command, json_option, launcher


def _add_version(sub: Subcommands) -> None:
    json_option(add_command(sub, "version", "cli.help.version"))


def _version(args: argparse.Namespace) -> int:
    render.show({"version": __version__}, getattr(args, "json", False), lambda data: f"TOW {data['version']}")
    return 0


VERSION = Command("version", _add_version, _version)


def _add_status(sub: Subcommands) -> None:
    json_option(add_command(sub, "status", "cli.help.status"))


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
    stale: str | None = None  # the autostart's name when it starts a folder that is gone
    try:
        from tow.autostart import backend
        from tow.doctor import stale_autostart

        found = backend().status()
        autostart: bool | None = bool(found.get("on"))
        if stale_autostart(found):
            stale = str(found.get("where") or "TOW")
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
        # Written from the number beside "at" (an older TOW stored "at" already written in the
        # page's language of that moment), in the language this command speaks, like next_check.
        "last_check": (
            format_ui_timestamp(datetime.fromtimestamp(health["at_ts"], UTC).isoformat())
            if isinstance(health.get("at_ts"), int) and health["at_ts"] > 0
            else health.get("at") or None
        ),
        "last_check_ok": bool(health.get("check_ok", True)) if health else None,
        "next_check": format_ui_timestamp(datetime.fromtimestamp(next_at, UTC).isoformat()) if next_at else None,
        "network": as_bool(cfg.get("allow_lan")),
        "autostart": autostart,
        "autostart_stale": stale is not None,
        "autostart_where": stale,
        "update_interrupted": running is None and layout.interrupted_update(),
    }


def _status(args: argparse.Namespace) -> int:
    """One line on TOW for a terminal or a monitoring script (--json)."""
    from tow.i18n import t

    view = _status_view()
    if args.json:
        render.show(view, True)
        return EXIT_OK
    yes_no = {True: t("cli.status.yes"), False: t("cli.status.no"), None: "?"}
    last_check = view["last_check"]
    client = view["client"] or t("cli.status.none")
    if view["client_ok"] is not None:
        client += " " + (t("cli.status.answers") if view["client_ok"] else t("cli.status.not_answering"))
    if view["running"]:
        state = t("cli.status.running", pid=view["pid"] or "?")
    else:
        state = t("cli.status.update_interrupted" if view["update_interrupted"] else "cli.status.stopped")
    parts = [
        f"TOW {view['version']}",
        state,
        client,
        t("cli.status.sites", n=view["sites"]),
        t("cli.status.topics", n=view["topics"]),
        t("cli.status.last", at=last_check or t("cli.status.never"))
        + ("" if view["last_check_ok"] is not False else " " + t("cli.status.failed")),
        t("cli.status.next", at=view["next_check"] or "—"),
        t("cli.status.autostart", value=yes_no[view["autostart"]]),
    ]
    print(" · ".join(parts))
    if view["autostart_stale"]:  # the OS keeps starting a TOW folder that was moved or deleted
        print(t("doctor_report.autostart_stale", where=view["autostart_where"]))
    return EXIT_OK


STATUS = Command("status", _add_status, _status)


def _add_serve(sub: Subcommands) -> None:
    from tow.i18n import t

    # The web page alone (`tow run` serves it together with the schedule): not listed.
    s = add_command(sub, "serve", "cli.help.serve", listed=False)
    s.add_argument("--host", default=None, help=t("cli.help.serve_host"))
    s.add_argument("--port", type=int, default=None, help=t("cli.help.serve_port"))
    s.add_argument("--log-file", type=Path, default=None, help=t("cli.help.serve_log"))
    s.add_argument("--parent-pid", type=int, default=None, help=argparse.SUPPRESS)  # `tow run`: end with it


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from tow.bind import validate_bind
    from tow.config import as_bool, load_config, port_of
    from tow.store import check_state_version

    check_state_version()  # a newer TOW's state.json: refused, never served or rewritten
    first_run_key()
    cfg = load_config()
    try:
        host = validate_bind(args.host or cfg.get("bind"), allow_lan=as_bool(cfg.get("allow_lan")))
    except ValueError as exc:
        from tow.i18n import t

        print(t("cli.serve_blocked", error=exc))
        return 3
    port = args.port or port_of(cfg)
    log_config = _serve_log_config(args.log_file) if getattr(args, "log_file", None) else None
    # No X-Forwarded-For/-Proto from anyone: TOW tells this computer from the network by the
    # connection's own address, which a header (or FORWARDED_ALLOW_IPS=*) must never replace.
    config = uvicorn.Config(
        "tow.web:app",
        host=host,
        port=port,
        reload=False,
        log_config=log_config,
        proxy_headers=False,
        forwarded_allow_ips="",
    )
    server = uvicorn.Server(config)
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
        "class": "tow.log.SafeRotatingFileHandler",  # a log held by a terminal keeps its older files
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


SERVE = Command("serve", _add_serve, _serve, background=True, warn_open_folders=True)


def _add_watchdog(sub: Subcommands) -> None:
    json_option(add_command(sub, "watchdog", "cli.help.watchdog"))


def _watchdog(args: argparse.Namespace) -> int:
    from tow.i18n import t
    from tow.store import load_state
    from tow.supervisor import data_lost
    from tow.watchdog import run_watchdog

    report = run_watchdog()
    # Only `tow run` notices a deleted data folder (it knows the one it started with); by hand
    # its note says it, as Home does, until topics are watched again.
    if (lost := data_lost()) is not None and not load_state().get("topics"):
        report["data_lost"] = True
        report["alerts"].append(t("watchdog.alert.data_lost", folder=lost["folder"]))
    render.show(report, args.json, render.watchdog_text)
    return 0 if report["service_ok"] and report["checks_ok"] and not report.get("data_lost") else 2


WATCHDOG = Command("watchdog", _add_watchdog, _watchdog, background=True)


def _interrupted_update() -> bool:
    """An update cut off mid-switch: the code in app/ may be half new; the update puts it right."""
    from tow.i18n import t
    from tow.supervisor import layout

    if not layout.interrupted_update():
        return False
    print(t("cli.run.update_interrupted", root=layout.install_root()), flush=True)
    return True


def _add_run(sub: Subcommands) -> None:
    add_command(sub, "run", "cli.help.run")


def _run(_args: argparse.Namespace) -> int:
    from tow.store import check_state_version
    from tow.supervisor import run_supervisor

    if _interrupted_update():
        return EXIT_CANNOT_RUN
    check_state_version()  # a newer TOW's state.json: refused, never served or rewritten
    first_run_key()
    return run_supervisor()


RUN = Command("run", _add_run, _run, layout=True, background=True, warn_open_folders=True)


def _add_start(sub: Subcommands) -> None:
    from tow.i18n import t

    start = add_command(sub, "start", "cli.help.start")
    start.add_argument("--no-browser", action="store_true", help=t("cli.help.start_no_browser"))
    start.add_argument("--wait", type=float, default=120.0, help=t("cli.help.start_wait"))


def _says_port_busy(line: str, port: int) -> bool:
    """``line`` is `tow run`'s refusal of a taken port, in any language it may have used."""
    from tow import i18n

    return any(
        i18n.translate(key, lang, port=port) in line
        for key in ("supervisor.port_busy", "supervisor.port_busy_tow")
        for lang in i18n.codes()
    )


def _start(args: argparse.Namespace) -> int:
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
    written = starter.output_size()
    result = starter.start(port, wait=args.wait, browser=browser)
    logs = layout.logs_dir()
    if result["state"] == "exited":
        # The reason itself, here: the start window says "the reason is above".
        lines = starter.output_since(written)
        for line in lines:
            print(line)
        if not any(_says_port_busy(line, port) for line in lines):  # that one says all there is
            print(t("cli.start.exited", log=logs / "run.log", stderr=starter.stderr_log()))
        return EXIT_CANNOT_RUN
    if result["state"] == "timeout":
        print(t("cli.start.timeout", seconds=int(args.wait), log=logs / "run.log"))
        return EXIT_CANNOT_RUN
    print(t("cli.start.ready", url=result["url"]))
    if browser and not result["browser"]:
        print(t("cli.start.open_by_hand", url=result["url"]))
    return EXIT_OK


START = Command("start", _add_start, _start, layout=True, warn_open_folders=True)


def _add_stop(sub: Subcommands) -> None:
    from tow.i18n import t

    stop = add_command(sub, "stop", "cli.help.stop")
    stop.add_argument("--wait", type=float, default=60.0, help=t("cli.help.stop_wait"))


def _stop(args: argparse.Namespace) -> int:
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


STOP = Command("stop", _add_stop, _stop, layout=True)


def _add_restart(sub: Subcommands) -> None:
    add_command(sub, "restart", "cli.help.restart")


def _restart(_args: argparse.Namespace) -> int:
    from tow.i18n import t
    from tow.supervisor import request_restart

    if request_restart(by="cli") is None:
        print(t("cli.run.not_running"))
        return EXIT_CANNOT_RUN
    print(t("cli.run.restart_requested"))
    return 0


RESTART = Command("restart", _add_restart, _restart, layout=True)


def _add_setup(sub: Subcommands) -> None:
    add_command(sub, "setup", "cli.help.setup")


def _setup(_args: argparse.Namespace) -> int:
    """`setup` belongs to the launcher: it rebuilds this very environment, so it cannot run from it."""
    from tow.i18n import t

    print(t("cli.setup.use_launcher", launcher=launcher()))
    return EXIT_OK


SETUP = Command("setup", _add_setup, _setup)


def _add_autostart(sub: Subcommands) -> None:
    from tow.i18n import t

    autostart = add_command(sub, "autostart", "cli.help.autostart")
    # "migrate" (1.18-1.20) only points to the documentation now: accepted, not offered.
    autostart.add_argument(
        "autostart_action",
        choices=("on", "off", "status", "migrate"),
        metavar="{on,off,status}",
        help=t("cli.help.autostart_action"),
    )
    autostart.add_argument("--without-login", action="store_true", help=t("cli.help.autostart_without_login"))
    autostart.add_argument("--apply", action="store_true", help=argparse.SUPPRESS)  # migrate (1.18-1.20)
    json_option(autostart)


def _autostart(args: argparse.Namespace) -> int:
    from tow.autostart import backend
    from tow.i18n import t

    if args.autostart_action == "migrate":
        # The switch from the five Windows tasks of 1.17 was in 1.18-1.20; this version has none.
        print(t("autostart.migrate.gone"))
        return 0
    chosen = backend()
    if args.autostart_action == "status":
        render.show(chosen.status(), args.json, render.autostart_text)
        return 0
    result = chosen.enable(without_login=args.without_login) if args.autostart_action == "on" else chosen.disable()
    if args.json:
        render.show(result, True)
    elif result.get("ok"):
        print(t("autostart.done_on") if args.autostart_action == "on" else t("autostart.done_off"))
        if result.get("hint"):
            print(result["hint"])
    else:
        print(t("autostart.failed", error=result.get("error") or t("autostart.not_confirmed")))
    return 0 if result.get("ok") else 2


AUTOSTART = Command("autostart", _add_autostart, _autostart, layout=True)


def _add_update(sub: Subcommands) -> None:
    from tow.i18n import t

    update = add_command(sub, "update", "cli.help.update", "cli.help.update_long")
    update.add_argument("--ref", required=True, help=t("cli.help.update_ref"))


def _update(args: argparse.Namespace) -> int:
    """The update replaces this very environment, so it cannot run from it: say how to run it."""
    from tow import platform
    from tow.i18n import t
    from tow.paths import repo_root

    script = repo_root() / "scripts" / "update.py"
    python = getattr(sys, "_base_executable", None) or sys.executable  # the venv's base interpreter
    archive = not (repo_root() / ".git").exists()  # the Windows bundle, install.ps1, install.sh
    found = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", args.ref)
    if archive and found and tuple(int(part) for part in found.groups()) < (1, 22, 0):
        # update.py refuses it: no command that would only be refused.
        print(t("cli.update.archive_too_old", ref=args.ref))
        return EXIT_USAGE
    print(t("cli.update.how"))
    print(f'  "{python}" "{script}" --ref {args.ref}')
    if archive:
        print(t("cli.update.archive"))
        # Their own update file in the TOW folder; deploy.ps1 needs a git checkout.
        name = {"windows": "Update TOW.cmd", "macos": "Update TOW.command"}.get(platform.this_os(), "update-tow")
        print(t("cli.update.archive_file", file=name, ref=args.ref))
    elif platform.is_windows():
        print(t("cli.update.or_windows"))
        # Windows PowerShell 5.1 is on every Windows (pwsh may not be); Bypass for this one run,
        # so an execution policy that refuses scripts does not stop it.
        deploy = repo_root() / "scripts" / "deploy.ps1"
        print(f'  powershell -NoProfile -ExecutionPolicy Bypass -File "{deploy}" -Ref {args.ref}')
    return 0


UPDATE = Command("update", _add_update, _update)
