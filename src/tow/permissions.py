"""``tow permissions``: who can get into the install, and a one-time repair of its owner.

Every start closes the install root, keys/ and data/ when this account owns them
(``tow.store.protect_install_folders``). A root created from an administrator terminal is owned
by Administrators: no start may change it (it could be another account's), so it stays open and
every start warns. ``tow permissions fix``, run once in an administrator terminal, makes the
owner's account the owner of these folders and closes them as a start would: that account,
SYSTEM and Administrators, nothing inherited, what they hold inheriting it. The owner's account
is the one named with ``--owner``, else the install's owner when that is a person, else the one
the autostart task runs as, else the one that opened the terminal - never Administrators, and
never the administrator whose password a standard account typed to open it (then it has to be
named). Nothing outside the install root is touched, never a drive root or a link. Without an
administrator terminal it does what a start does and prints the command to run in one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tow import platform
from tow.i18n import t


def _folders() -> list[tuple[str, Path]]:
    from tow.paths import data_dir, keys_dir, root

    return [("root", root()), ("keys", keys_dir()), ("data", data_dir(create=False))]


def _plain_dir(path: Path) -> bool:
    try:
        return platform.is_plain_dir(path.lstat())
    except OSError:
        return False


def _inside(path: Path, root: Path) -> bool:
    inner, outer = (os.path.normcase(os.path.abspath(value)) for value in (path, root))
    return inner == outer or inner.startswith(outer.rstrip(os.sep) + os.sep)


def _open(backend: platform.Backend, kind: str, path: Path, account: str | None) -> bool | None:
    """Accounts other than ``account`` (and SYSTEM, Administrators) can get into ``path``."""
    if account is None:
        return None
    try:
        return backend.shared_for(path, account, root=kind == "root")
    except AttributeError, OSError, ValueError:
        return None


def status(account: str | None = None) -> dict[str, Any]:
    """The install root, keys/ and data/: open to other accounts or not, and who owns them.
    ``mine`` compares the owner with ``account`` (by default this process's); ``outside``: a
    folder elsewhere (TOW_HOME, ``--key-file``) that ``fix`` never changes - an open one is
    reported, never with the advice to run ``fix``."""
    from tow.paths import root

    backend = platform.current()
    me = account if account is not None else backend.current_account()
    install = root()
    rows = []
    for kind, path in _folders():
        if not _plain_dir(path):
            continue
        owner = backend.folder_owner(path)
        rows.append(
            {
                "folder": kind,
                "path": str(path),
                "open": _open(backend, kind, path, me),
                "owner": backend.account_name(owner) if owner else "",
                "mine": owner is not None and owner == me,
                "outside": not _inside(path, install),
            }
        )
    inside = [row for row in rows if not row["outside"]]
    result: dict[str, Any] = {
        "ok": not any(row["open"] for row in inside),
        "folders": rows,
        "account": backend.account_name(me) if me else "",
        "elevated": backend.elevated(),
    }
    if not result["ok"]:
        # Another account's folder: only an administrator can hand it over.
        result["needs_admin"] = any(row["open"] and not row["mine"] for row in inside)
        result["fix_command"] = fix_command(admin=result["needs_admin"])
    return result


def fix_command(*, admin: bool) -> str:
    """``tow permissions fix`` as typed in this install; with ``admin`` for an administrator
    terminal (Windows) or with sudo (Linux, macOS)."""
    from tow.paths import launcher, root

    command = f'"{root() / launcher(windows=platform.is_windows())}" permissions fix'
    return f"sudo {command}" if admin and not platform.is_windows() else command


def _autostart_user() -> str:
    """The account the autostart task of this install runs as ("" without one)."""
    from tow.autostart import backend

    try:
        task = backend().status()
    except Exception:  # noqa: BLE001 - no task, or one that cannot be read: the terminal's account
        return ""
    return str(task.get("user") or "") if task.get("ours") else ""


def owner_account(requested: str | None = None) -> tuple[str | None, str]:
    """(account, where it comes from): the one named with ``--owner``; else the install's own
    owner when that is a person's account; else the autostart task's; else the one that opened
    this terminal - unless the terminal runs as another account than the one signed in (an
    administrator's password typed from a standard account), which is refused ("session"): the
    owner has to be named then. A group, SYSTEM or a service account is never the answer."""
    from tow.paths import root

    backend = platform.current()
    if requested:
        named_account = backend.account_of(requested)
        if named_account and backend.personal_account(named_account):
            return named_account, "option"
        return None, "option"
    current = backend.folder_owner(root())
    if current and backend.personal_account(current):
        return current, "owner"
    named = _autostart_user()
    task_account = backend.account_of(named) if named else None
    if task_account and backend.personal_account(task_account):
        return task_account, "autostart"
    account = backend.invoking_account()
    session = backend.session_account()
    if account and session and session != account:
        return None, "session"
    if account and backend.personal_account(account):
        return account, "terminal"
    return None, ""


def _no_owner(backend: platform.Backend, source: str, requested: str | None) -> str:
    """Why there is no account to give the folders to, and how to name one."""
    if source == "option":
        return t("permissions.owner_not_person", name=requested or "")
    command = f"{fix_command(admin=False)} --owner"
    if source == "session":
        terminal, session = backend.invoking_account() or "", backend.session_account() or ""
        return t(
            "permissions.owner_ambiguous",
            terminal=backend.account_name(terminal),
            session=backend.account_name(session),
            command=command,
        )
    return t("permissions.no_account", command=command)


def fix(owner: str | None = None) -> dict[str, Any]:
    """Make the install's folders the owner's and close them; read back (see the module).
    ``owner``: the account to give them to (``--owner``), instead of finding it."""
    from tow.paths import root
    from tow.store import protect_install_folders

    backend = platform.current()
    install = root()
    if install.parent == install or not _plain_dir(install):
        return {"ok": False, "error": t("permissions.refused_root", path=str(install))}
    if not backend.elevated():
        was_open = not status()["ok"]
        protect_install_folders(quiet=True)  # what every start does: this account's folders
        after = status()
        if after["ok"] or after["needs_admin"]:
            return {**after, "changed": was_open and after["ok"]}
        return {**after, "error": t("permissions.still_open")}
    account, source = owner_account(owner)
    if account is None:
        return {"ok": False, "error": _no_owner(backend, source, owner)}
    changed = False
    for kind, path in _folders():
        if not _plain_dir(path) or not _inside(path, install):
            continue
        if backend.folder_owner(path) == account and _open(backend, kind, path, account) is False:
            continue
        if not backend.hand_over(path, account):
            return {**status(account), "ok": False, "error": t("permissions.failed", path=str(path))}
        changed = True
    after = status(account)
    if not changed:
        return {**after, "changed": False}
    return {**after, "changed": True, "owner_account": backend.account_name(account), "owner_from": source}


_FOLDER_NAMES = {
    "root": "permissions.folder_root",
    "keys": "permissions.folder_keys",
    "data": "permissions.folder_data",
}
_OWNER_FROM = {
    "option": "permissions.owner_from_option",
    "owner": "permissions.owner_from_owner",
    "autostart": "permissions.owner_from_autostart",
    "terminal": "permissions.owner_from_terminal",
}


def text(result: dict[str, Any]) -> str:
    """``status`` or ``fix`` in words."""
    lines = []
    for row in result.get("folders") or []:
        state = {True: "permissions.state_open", False: "permissions.state_closed"}.get(
            row["open"], "permissions.state_unknown"
        )
        lines.append(
            t(
                "permissions.folder_line",
                folder=t(_FOLDER_NAMES[row["folder"]]),
                path=row["path"],
                state=t(state),
                owner=row["owner"] or t("permissions.owner_unknown"),
            )
        )
    lines.extend(
        t("permissions.outside", folder=t(_FOLDER_NAMES[row["folder"]]), path=row["path"])
        for row in result.get("folders") or []
        if row.get("outside") and row["open"]
    )
    if result.get("owner_account"):
        lines.append(t(_OWNER_FROM[result["owner_from"]], account=result["owner_account"]))
    if result.get("error"):
        lines.append(result["error"])
    elif result.get("needs_admin") and not result.get("elevated"):
        foreign = [row["owner"] for row in result.get("folders") or [] if row["open"] and not row["mine"]]
        key = "permissions.needs_admin" if platform.is_windows() else "permissions.needs_sudo"
        lines.append(t(key, owner=", ".join(dict.fromkeys(foreign)) or "?", command=result["fix_command"]))
    elif not result.get("ok"):
        lines.append(t("permissions.run_fix", command=result.get("fix_command") or fix_command(admin=False)))
    elif result.get("changed"):
        lines.append(t("permissions.fixed"))
    elif not any(row.get("outside") and row["open"] for row in result.get("folders") or []):
        lines.append(t("permissions.all_closed"))
    return "\n".join(lines)
