"""What a ``tow`` command is: its row in the command table (``tow.cli.commands``), and the exit
codes every command returns (``tow --help`` lists them; ``tow.cli`` exports them)."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass

EXIT_OK = 0
EXIT_USAGE = 1  # a wrong command or option
EXIT_PARTIAL = 2  # done in part: a topic, a mirror or a step failed
EXIT_CANNOT_RUN = 3  # cannot run: config, secrets, the lock, a missing file
EXIT_INTERRUPTED = 130

# What a command adds its parser to: the subcommands of `tow`.
type Subcommands = argparse._SubParsersAction[argparse.ArgumentParser]


@dataclass(frozen=True)
class Command:
    """One command: ``configure`` adds its parser and options, ``run`` does it and returns its
    exit code. Each imports what it needs when it runs, so the table itself stays cheap."""

    name: str
    configure: Callable[[Subcommands], None]
    run: Callable[[argparse.Namespace], int]
    # Runs without tow.cmd/tow-env.cmd (autostart, an update, the owner in a terminal): finds
    # data/ and config.yaml itself first.
    layout: bool = False
    # Started by `tow run` as its child: outside a terminal its texts go to messages and logs,
    # in the owner's message language.
    background: bool = False
    # Starts or serves TOW: says which of its folders other accounts can get into.
    warn_open_folders: bool = False
    # Its folders are repaired before it runs (`tow permissions` reports and repairs them itself).
    protects_folders: bool = True
