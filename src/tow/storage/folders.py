"""Folder protection: the install root, keys/ and data/ private to this account.

The folders other accounts could still open are named on stderr once per process.
"""

from __future__ import annotations

from pathlib import Path

from tow.paths import data_dir, key_file


def install_folders() -> list[Path]:
    """keys/ and data/: folders only this account (and on Windows SYSTEM and Administrators)
    may open - the master key opens every saved password, data/ holds the encrypted ones."""
    return [key_file().parent, data_dir(create=False)]


def protect_install_folders(*, quiet: bool = False) -> list[Path]:
    """At start: the install root, keys/ and data/ of this account become private when other
    accounts can get in (an older install, an install in a drive root on Windows).

    The root first (``tow.platform.private_root``): through it other accounts could change the
    code TOW runs and so read the master key. keys/ and data/ are closed on their own too, so
    they stay private when the root cannot be changed (another account owns it) or they live
    elsewhere. A folder that stays open (another account's, or the change failed) is named on
    stderr (not when ``quiet``: `tow permissions` says it itself) and returned; the start goes
    on. `tow doctor` reports it too, and `tow permissions fix` repairs another account's."""
    import sys

    from tow.i18n import t
    from tow.paths import root
    from tow.platform import private_folders, private_root

    try:
        install, folders = root(), install_folders()
    except OSError, RuntimeError:  # no install located: the command reports that itself
        return []
    root_open = private_root(install)
    still_open = private_folders(folders)
    warned = [install, *still_open] if root_open else still_open
    if not quiet and not _OPEN_FOLDERS_SAID:  # once per process: `tow serve` repairs twice
        if root_open:
            print(t("cli.root_shared", path=str(install)), file=sys.stderr)
        for folder in still_open:
            print(t("cli.folder_shared", path=str(folder)), file=sys.stderr)
        _OPEN_FOLDERS_SAID.extend(warned)
    return warned


# The open folders this process has named on stderr already (protect_install_folders).
_OPEN_FOLDERS_SAID: list[Path] = []
