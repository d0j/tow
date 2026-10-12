"""The command table: every ``tow`` command, in the order ``tow --help`` lists them. The parser is
built from it and ``tow.cli`` runs the row a command line names (``tow.cli.command.Command``)."""

from __future__ import annotations

from tow.cli.command import Command
from tow.cli.commands import checks, data, security, service

COMMANDS: tuple[Command, ...] = (
    service.VERSION,
    service.STATUS,
    checks.DOCTOR,
    security.PERMISSIONS,
    checks.ADOPT,
    checks.CHECK,
    service.SERVE,
    security.ACCESS,
    security.SECRETS,
    security.KEYS,
    data.EXPORT,
    data.IMPORT,
    data.IMPORT_ROLLBACK,
    data.BACKUP,
    data.RESTORE_SNAPSHOT,
    service.WATCHDOG,
    security.PASSWORD,
    service.RUN,
    service.START,
    service.STOP,
    service.RESTART,
    service.SETUP,
    service.AUTOSTART,
    service.UPDATE,
)
