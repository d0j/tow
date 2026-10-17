"""What a command prints: JSON for a program (``--json`` keeps every field), sentences for a person."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


def machine(data: Any, *, indent: int | None = 2) -> str:
    """``--json``: ASCII JSON, every other character as JSON's own escape. Windows PowerShell 5.1
    decodes a program's pipe in the console's code page, not UTF-8: a Cyrillic folder came back
    garbled from ``tow.cmd ... --json | ConvertFrom-Json`` (X27); the escapes read the same in any."""
    return json.dumps(data, ensure_ascii=True, indent=indent)


def show(data: Any, as_json: bool, text: Callable[[Any], str] | None = None) -> None:
    """``data`` as JSON for a program, or as sentences for a person: ``text`` writes those of a
    command's result; a refusal or a result with a message says just that."""
    if as_json:
        print(machine(data))
    elif text is not None and not (isinstance(data, dict) and data.get("ok") is False):
        print(text(data))
    else:
        print(human(data))


def human(data: Any) -> str:
    """Readable text for a person (not a Python dict repr); --json stays machine output."""
    import yaml

    from tow.check_outcomes import counts_of, outcome_of
    from tow.i18n import t

    if isinstance(data, dict) and isinstance(data.get("results"), list):
        head = [t("cli.check.client", value=data["qbit"])] if data.get("qbit") else []
        if data.get("preview"):
            head.append(t("cli.check.preview"))
        lines = ["  ".join(head)] if head else []
        for row in data["results"]:
            # The outcomes the web and the log use: waiting for disk space or for the client's
            # file check is neither ok nor a failure, and a skipped or unsaved row is no success.
            outcome = outcome_of(row)
            mark = _MARKS[outcome]
            state = (
                row.get("error") or row.get("skipped") or _check_state(row, outcome, preview=bool(data.get("preview")))
            )
            lines.append(f"{mark} {str(row.get('title') or row.get('id'))[:70]}" + (f" — {state}" if state else ""))
            if row.get("note"):
                lines.append(f"    {row['note']}")
        counts = counts_of(data["results"])
        total = {"n": len(data["results"]), "failed": counts["fail"]}
        if counts["pending"]:
            lines.append(t("cli.check.total_waiting", waiting=counts["pending"], **total))
        else:
            lines.append(t("cli.check.total", **total))
        return "\n".join(lines)
    if isinstance(data, dict) and data.get("ok") is False and data.get("error"):
        return str(data["error"])  # the refusal itself; its technical detail is in --json
    if isinstance(data, dict) and data.get("message"):
        return str(data["message"])
    if isinstance(data, (dict, list)):
        return str(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)).rstrip()
    return str(data)


# A check row's mark by its outcome (tow.check_outcomes): signs, the same in every language.
_MARKS = {"new": "ok ", "same": "ok ", "pending": "…  ", "skipped": "–  ", "fail": "ERR"}


def _check_state(row: dict[str, Any], outcome: str, *, preview: bool) -> str:
    """What a check row without its own error or reason says, in words (never a status code)."""
    from tow.i18n import t

    if outcome == "new":
        if preview:
            return t("cli.check.would_change")
        for flag, key in (("selection_updated", "web.check.selection_updated"), ("started", "web.check.started")):
            if row.get(flag):
                return t(key)
        return t("web.check.added") if row.get("added") else t("web.check.changed")
    if outcome == "pending" and row.get("status") == "verification_pending":
        return t("web.check.verifying")
    if outcome == "fail":
        return t("web.check.not_confirmed")
    return ""  # unchanged, or skipped with its note on the next line


# --- what a command says to a person (its --json keeps every field) -------------------------


def size(value: Any) -> str:
    """A size in the command's language: "1.5 MB", "1,5 МБ"."""
    from tow.i18n import current, format_size

    return format_size(int(value or 0), current())


def _names(items: Any) -> str:
    return ", ".join(str(item) for item in items or []) or "—"


def _key_line(source: str, key_file: str | None, *, secrets_saved: bool) -> str:
    """Which master key TOW uses, never the key itself."""
    from tow.i18n import t

    if source == "missing":
        return t("store.no_master_key") if secrets_saved else t("cli.keys.source_missing")
    if source == "env":
        return t("cli.keys.source_env")
    where = {"install": "cli.keys.source_install", "file": "cli.keys.source_file", "legacy": "cli.keys.source_legacy"}
    return t(where.get(source, "cli.keys.source_install"), path=key_file or "—")


def secrets_text(status: dict[str, Any]) -> str:
    from tow.i18n import t
    from tow.store import master_key_file

    storage = status.get("storage")
    lines = []
    if status.get("migrated") is not None:
        lines.append(t("cli.secrets.migrated" if status["migrated"] else "cli.secrets.nothing_to_migrate"))
    lines.append(
        {
            "encrypted": t("cli.secrets.encrypted", file="data/secrets.enc"),
            "uninitialized": t("cli.secrets.none"),
            "legacy_plaintext_migration_required": t(
                "store.migrate_needed", file="data/secrets.json", command="tow secrets migrate"
            ),
            "blocked_legacy_plaintext": t("store.legacy_left", file="data/secrets.json", encrypted="secrets.enc"),
        }.get(str(storage), str(storage))
    )
    in_use = master_key_file()
    lines.append(
        _key_line(str(status.get("key_source")), str(in_use) if in_use else None, secrets_saved=storage == "encrypted")
    )
    return "\n".join(lines)


def keys_text(view: dict[str, Any]) -> str:
    from tow.i18n import t
    from tow.store import encrypted_secrets_path

    source = str(view["key_source"])
    lines = [_key_line(source, view["key_file"], secrets_saved=encrypted_secrets_path().is_file())]
    if source in ("env", "file", "legacy"):  # this install's own place for it, beside the one in use
        own = "cli.keys.install_file" if view["install_key_exists"] else "cli.keys.install_file_missing"
        lines.append(t(own, path=view["install_key_file"]))
    return "\n".join(lines)


def access_text(view: dict[str, Any]) -> str:
    from tow.i18n import t

    state = t("cli.access.status_on", bind=view["bind"]) if view["network"] else t("cli.access.status_off")
    password = t("cli.access.password_set" if view["password_set"] else "cli.access.password_unset")
    return f"{state} {password}"


def export_text(result: dict[str, Any]) -> str:
    from tow.i18n import t

    return t("cli.bundle.exported", path=result.get("output") or "—", size=size(result.get("bytes")))


def import_text(result: dict[str, Any]) -> str:
    from tow.i18n import t

    files = _names(result.get("members"))
    if result.get("preview"):
        lines = [t("cli.bundle.import_preview", files=files)]
    else:
        lines = [t("cli.bundle.imported", files=files, checkpoint=result.get("checkpoint") or "—")]
    lines += [t("cli.bundle.path_mapped", old=item["old"], new=item["new"]) for item in result.get("path_maps") or []]
    # "unmapped topic path: D:\TV" - the folder after the colon, said in the owner's words.
    lines += [t("cli.bundle.path_unmapped", path=str(w).split(": ", 1)[-1]) for w in result.get("path_warnings") or []]
    lines += _security_lines(result.get("security_settings") or [])
    if result.get("local_secrets_unreadable"):
        lines.append(t("cli.bundle.local_secrets_unreadable"))
    lines.append(t("cli.bundle.import_notes"))
    if result.get("log_error"):
        lines.append(t("cli.bundle.log_unrecorded"))
    return "\n".join(lines)


def _setting_value(value: Any) -> str:
    from tow.i18n import t

    if value is None:
        return "—"
    if value == "set":
        return t("cli.bundle.security_set")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _security_lines(changes: list[dict[str, Any]]) -> list[str]:
    """The security settings a transfer file holds otherwise than this TOW, and which stay."""
    from tow.i18n import t

    if not changes:
        return []
    lines = [t("cli.bundle.security_header")]
    for item in changes:
        name = t("cli.bundle.security_password") if item.get("setting") == "lan_auth" else str(item.get("setting"))
        here = item.get("this_computer")
        lines.append(
            t(
                "cli.bundle.security_item",
                name=name,
                file=_setting_value(item.get("file")),
                here=t("cli.bundle.security_unknown") if here == "unknown" else _setting_value(here),
            )
        )
    kept = any(item.get("kept") for item in changes)
    lines.append(t("cli.bundle.security_kept" if kept else "cli.bundle.security_taken"))
    return lines


def rollback_text(result: dict[str, Any]) -> str:
    from tow.i18n import t

    if result.get("preview"):
        return t("cli.bundle.rollback_preview", files=_names(result.get("targets")))
    return t("cli.bundle.rolled_back", files=_names(result.get("restored")))


def backup_text(result: dict[str, Any]) -> str:
    from tow.i18n import t

    lines = [
        t(
            "cli.backup.made",
            path=result.get("snapshot") or "—",
            n=result.get("files", 0),
            size=size(result.get("bytes")),
        )
    ]
    if result.get("missing"):
        lines.append(t("cli.backup.missing", files=_names(result["missing"])))
    if result.get("pruned"):
        lines.append(t("cli.backup.pruned", n=len(result["pruned"])))
    if result.get("cleanup_warning"):
        lines.append(str(result["cleanup_warning"]))
    if result.get("budget_warning"):
        lines.append(str(result["budget_warning"]))
    return "\n".join(lines)


def restore_text(result: dict[str, Any]) -> str:
    from tow.clock import format_ui_timestamp
    from tow.i18n import t

    try:
        made = format_ui_timestamp(str(result.get("created_at") or ""))
    except TypeError, ValueError:
        made = str(result.get("created_at") or "—")
    if not result.get("applied"):
        lines = [
            t(
                "cli.restore.preview",
                path=result.get("snapshot") or "—",
                at=made,
                version=result.get("version") or "—",
                files=_names(result.get("files")),
            )
        ]
        if not result.get("signed"):
            lines.append(t("backup.snapshot.unsigned"))
        return "\n".join(lines)
    lines = [
        t("cli.restore.done", path=result.get("snapshot") or "—", at=made, safety=result.get("safety_copy") or "—")
    ]
    if isinstance(changed := result.get("interval_changed"), dict):
        lines.append(t("cli.restore.interval", minutes=int(changed.get("to") or 0) // 60))
    if result.get("cleanup_warning"):
        lines.append(str(result["cleanup_warning"]))
    return "\n".join(lines)


def watchdog_text(report: dict[str, Any]) -> str:
    from tow.i18n import t

    if report.get("service_ok"):
        lines = [t("cli.watchdog.up", port=report.get("port"))]
    else:
        lines = [t("cli.watchdog.down", port=report.get("port"), cause=report.get("cause") or "—")]
    if report.get("checks_failing"):
        lines.append(t("cli.watchdog.checks_failing", n=report["checks_failing"]))
    elif report.get("checks_late"):
        lines.append(t("cli.watchdog.checks_late"))
    else:
        lines.append(t("cli.watchdog.checks_ok"))
    backup = report.get("backup_ok")
    lines.append(
        t(
            "cli.watchdog.backup_off"
            if backup is None
            else "cli.watchdog.backup_ok"
            if backup
            else "cli.watchdog.backup_bad"
        )
    )
    lines += [str(alert) for alert in report.get("alerts") or []]  # what this pass told the messengers too
    return "\n".join(lines)


def autostart_text(status: dict[str, Any]) -> str:
    from tow.doctor import stale_autostart
    from tow.i18n import t

    where = str(status.get("where") or "TOW")
    if status.get("error"):
        return t("cli.autostart.unread", error=status["error"])
    if stale_autostart(status):
        return t("doctor.autostart_stale", where=where)
    if status.get("ours") is False and status.get("state", "present") == "present":
        return t("doctor.autostart_foreign", where=where)
    if status.get("on"):
        line = t("cli.autostart.on", where=where)
        return f"{line} {t('cli.autostart.without_login')}" if status.get("without_login") else line
    if status.get("ours") and status.get("state", "present") == "present" and status.get("command"):
        return t("cli.autostart.differs", where=where)  # TOW's, but turned off or not as TOW writes it
    return t("cli.autostart.off")
