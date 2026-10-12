"""The commands that check the topics, the install and the torrents in the client (``doctor``,
``adopt``, ``check``)."""

from __future__ import annotations

import argparse
import json
from typing import Any

from tow.cli import render
from tow.cli.command import EXIT_CANNOT_RUN, EXIT_OK, EXIT_PARTIAL, EXIT_USAGE, Command, Subcommands
from tow.cli.parser import add_command, json_option


def _add_doctor(sub: Subcommands) -> None:
    from tow.i18n import t

    d = add_command(sub, "doctor", "cli.help.doctor")
    json_option(d)
    d.add_argument("--notify", action="store_true", help=t("cli.help.doctor_notify"))


def _doctor(args: argparse.Namespace) -> int:
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
    # The diagnostics ran: a finding (no client set up, a client or site not answering) is
    # "done in part", never "cannot run" (an unreadable config or secret store is that).
    return EXIT_OK if report.get("ok") else EXIT_PARTIAL


DOCTOR = Command("doctor", _add_doctor, _doctor)


def _add_adopt(sub: Subcommands) -> None:
    from tow.i18n import t

    ad = add_command(sub, "adopt", "cli.help.adopt")
    ad.add_argument("topics", nargs="*", metavar="TOPIC_ID", help=t("cli.help.adopt_ids"))
    ad.add_argument("--all-unmarked", action="store_true", help=t("cli.help.adopt_all"))
    ad.add_argument("--yes", action="store_true", help=t("cli.help.adopt_yes"))
    ad.add_argument("--replace-label", action="store_true", help=t("cli.help.adopt_replace_label"))
    json_option(ad)


def _adopt(args: argparse.Namespace) -> int:
    """Adopt into TOW (tow.adopt): list the topics whose torrent is in the client without TOW's
    mark, ask, and mark them. Never without the owner's yes (or --yes)."""
    from tow.adopt import adopt_topic, unmarked_topics
    from tow.i18n import t
    from tow.store import CheckBusyError

    def say(data: dict[str, Any]) -> None:
        # A person reads the message itself; --json is the whole record and nothing else.
        if args.json:
            render.show(data, True)
        else:
            print(data.get("error") or data.get("message") or "")

    if not args.topics and not args.all_unmarked:
        say({"ok": False, "error": t("cli.adopt.nothing_asked")})
        return EXIT_USAGE
    report = unmarked_topics(ids=None if args.all_unmarked else [str(tid) for tid in args.topics])
    found: list[dict[str, Any]] = report["found"]
    # An unknown id or a client that did not answer is said: it is not "nothing to adopt".
    problems = [t("cli.adopt.unknown", id=tid) for tid in report["unknown"]] + [
        t("cli.adopt.unreachable", client=client, error=error) for client, error in report["unreachable"].items()
    ]
    problem_fields = {"unknown": report["unknown"], "unreachable": report["unreachable"]}
    if not found:
        if problems:
            say({"ok": False, "adopted": [], **problem_fields, "error": "\n".join(problems)})
            return EXIT_CANNOT_RUN
        say({"ok": True, "adopted": [], "message": t("cli.adopt.none")})
        return EXIT_OK
    if args.json and not args.yes:
        # No list and no question before the JSON: it lists what --yes would adopt.
        say({"ok": True, "adopted": [], "candidates": found, **problem_fields, "message": t("cli.adopt.list_only")})
        return EXIT_PARTIAL if problems else EXIT_OK
    if not args.yes:
        for line in problems:
            print(line)
        for item in found:
            print(f"{item['id']}  {item['hash'][:12]}  {item['title']}")
        try:
            answer = input(t("cli.adopt.confirm")).strip().casefold()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes", "д", "да"}:
            say({"ok": True, "adopted": [], "message": t("cli.adopt.cancelled")})
            return EXIT_OK
    adopted: list[str] = []
    lines: list[str] = list(problems) if args.yes else []
    failed = 0
    for item in found:
        try:
            result = adopt_topic(item["id"], how="manual", replace_label=args.replace_label)
        except CheckBusyError:
            say({"ok": False, "adopted": adopted, "error": t("cli.adopt.busy")})
            return EXIT_CANNOT_RUN
        except Exception as exc:  # noqa: BLE001 - one topic's failure is reported (and logged); the others go on
            failed += 1
            lines.append(t("cli.adopt.failed", id=item["id"], error=str(exc)))
            continue
        adopted.append(item["id"])
        lines.append(t("cli.adopt.already" if result["already"] else "cli.adopt.done", id=item["id"]))
    ok = not failed and not problems
    say({"ok": ok, "adopted": adopted, **problem_fields, "message": "\n".join(lines)})
    return EXIT_OK if ok else EXIT_PARTIAL


ADOPT = Command("adopt", _add_adopt, _adopt)


def _add_check(sub: Subcommands) -> None:
    from tow.i18n import t

    c = add_command(sub, "check", "cli.help.check")
    json_option(c)
    c.add_argument("--apply", action="store_true", help=t("cli.help.check_apply"))
    c.add_argument("--dry-run", action="store_true", help=t("cli.help.check_dry_run"))
    c.add_argument("--notify", action="store_true", help=t("cli.help.check_notify"))
    # A person running the CLI by hand; the scheduled task omits it (the header countdown follows auto runs).
    c.add_argument("--manual", action="store_true", help=argparse.SUPPRESS)
    # G2: the progress pass of `tow run` - completions from the torrent client only, no tracker requests.
    c.add_argument("--progress-only", action="store_true", help=argparse.SUPPRESS)
    # The space pass of `tow run`: topics waiting in the client for disk space, client only.
    c.add_argument("--space-only", action="store_true", help=argparse.SUPPRESS)
    scope = c.add_mutually_exclusive_group()
    scope.add_argument("--global-only", action="store_true", help=argparse.SUPPRESS)
    scope.add_argument("--timer-only", action="store_true", help=argparse.SUPPRESS)


def _check(args: argparse.Namespace) -> int:
    from tow.check import record_check_failure, run_check
    from tow.store import SecretStoreError, check_state_version

    check_state_version()  # a newer TOW's state.json: refused before anything is recorded
    apply = bool(args.apply) and not args.dry_run
    background = args.progress_only or args.space_only
    how = "manual" if args.manual else "progress" if background else "timer" if args.timer_only else "auto"
    try:
        out = run_check(
            apply=apply,
            notify=args.notify,
            how=how,
            progress_only=args.progress_only,
            scheduled_scope="global" if args.global_only else "timer" if args.timer_only else "",
            space_only=args.space_only,
        )
    except SecretStoreError as exc:
        if apply:
            record_check_failure(exc, how=how)
        render.show({"ok": False, "error": str(exc), "blocked": True}, args.json)
        return 3
    except Exception as exc:  # noqa: BLE001 - store, transaction, client library: recorded instead of a bare traceback
        if apply:
            record_check_failure(exc, how=how)
        from tow.i18n import t

        render.show({"ok": False, "error": t("cli.command_failed")}, args.json)
        return EXIT_CANNOT_RUN
    render.show(out, args.json)
    fails = [r for r in out["results"] if not r.get("ok")]
    return 2 if fails else 0


CHECK = Command("check", _add_check, _check, background=True)
