# Changelog

All notable changes to TOW. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [semantic versioning](https://semver.org/). Русская версия: [CHANGELOG.ru.md](CHANGELOG.ru.md).

## [1.21.0] — 2026-10-02

First public release (MIT). Versions 1.0–1.20 were developed and used privately; this repository starts from a
clean tree.

### Added

- English and Russian documentation: README, user guide, architecture, extending guide, contributing guide,
  security policy.
- First start creates the master key `keys/master.key` by itself and asks once to back it up (`tow keys ensure`).
- CLI: `tow status` (one line or `--json`), `tow access on|off|status` for headless servers, descriptions for
  every command and flag, an exit-code table (0 ok, 1 usage, 2 partial, 3 cannot run, 130 interrupted), JSON on
  failure with `--json`, UTF-8 output.
- Onboarding: the first-start page and an empty Home explain the next steps; the header links the Guide and
  Diagnostics; empty Sites explains what a site is; a styled 404 page.
- A new install watches every site TOW knows until the site list is edited; every setting in
  `config.example.yaml` is explained.

### Changed

- One way to run: `tow run` (web UI, schedule, night copy, watchdog). The minimum rollback target is v1.18.0.
- Status colours follow the contract everywhere: transport trouble is amber, "not checked yet" is grey.
- Sites form: link first, regex and paths under **Advanced**; a refused site keeps what was typed.
- Adding a topic shows "Checking…" until the client confirms the add.
- Undo sits inside its message with a countdown; Settings titles and pills say what is actually set up.
- Network errors are shown in words, raw details on demand.
- Tab titles name the page; the client is called "qBittorrent" everywhere.
- Linux and macOS: TOW's files and folders are private to the owner (umask 077); browser sign-in keeps its
  profile inside the install; folder names are compared case-sensitively on Linux.
- Update: a rollback keeps the failed version's logs; on Linux/macOS TOW is stopped and started through systemd or
  launchd; `scripts/update.py` uses the launchers' environment and runs on Python 3.11+.

### Fixed

- Scheduled checks could stop for good after a manual check on a new install; the schedule now follows
  `tow run`'s own last scheduled check.
- The night copy ran twice on the autumn DST day; `status.json` shows the real retry time after a failure.
- A web server left by a crashed `tow run` blocked the port; it is stopped at the next start, and children stop
  with their parent.
- A message could be sent twice when two parts of one process delivered at once.
- An interrupted night-copy restore is never abandoned; if it cannot be checked or undone, writes are refused
  with the reason.
- Settings "Restart TOW" reports failure when the new server keeps crashing.
- Import of a `.towx` bundle validates `config.yaml` with TOW's own rules.
- A backup folder of another operating system in `config.yaml` is refused instead of being created inside the
  install.
- Autostart: a moved install takes over its old registration; systemd and launchd stop/retry rules fixed.
- Delivery is not logged when no messenger is connected.

### Removed

- The legacy five-task Windows layout (`tow install-task`, "Switch to one process", per-task launchers,
  `restore-snapshot.ps1`, `tow-local.cmd`). Installs still on it update to 1.20 and run
  `tow autostart migrate --apply` first.
- `scripts/tow-rollback.py` (pre-git checkpoints only) and the µTorrent placeholder client.

### Security

- The master key file is created private (0600) from the start.
- `data/before-restore-*` folders keep at most the 3 newest and no settings-undo secrets.
- Test data is fully synthetic; no personal data in the repository.
