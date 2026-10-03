# Changelog

All notable changes to TOW. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [semantic versioning](https://semver.org/). Русская версия: [CHANGELOG.ru.md](CHANGELOG.ru.md).

## [1.22.18] — 2026-10-03

### Fixed

- Check recovery verifies every backup and target before changing either state or download history, and restores
  the already verified bytes. Damaged copies cannot overwrite working stores before the error is detected.
- Malformed recovery fields, parser limits, directories and Windows reparse points fail closed. A failed journal
  reservation never deletes another writer's files; backup writes are verified before a check can start.

## [1.22.17] — 2026-10-03

### Fixed

- Night copies reject malformed manifests and inconsistent file sizes with a clear error before restoring.
  Member reads are bounded by the recorded size, including files that grow during verification.
- Copies created in the same second no longer collide. Creation, verification and pruning are serialized;
  automatic pruning keeps unsigned, foreign or damaged-manifest folders instead of deleting them.
- Restore point lists and night copies exclude directories, links and unrelated `.towx` filenames.
- A failed reservation of a temporary copy folder never removes another writer's files.
- The release publication helper confirms the configured local mirror's main and annotated tag before
  publishing to GitHub, so installations that fetch from the mirror can find the release.

## [1.22.16] — 2026-10-03

### Fixed

- Configured backup and restore-point folders now receive the same path checks when used as they do when saved in
  Settings. A relative master-key filename cannot escape the data folder through `..` or a link; absolute paths for
  external keys remain supported.
- Bundle reads enforce the size limit during reading too, even if a file changes after its initial size check.

## [1.22.15] — 2026-10-03

### Fixed

- Portable imports now reject non-finite numbers and excessively nested JSON or YAML before changing destination
  data, including the envelope, manifest, and optional events. Export also refuses non-finite values. Invalid imported
  history can no longer reach the download-history JSON endpoint or escape as a parser error.

## [1.22.14] — 2026-10-03

### Fixed

- Preview clips marked with a separated or bracketed `Sample`, `Trailer`, or `Preview` suffix are excluded from
  episode coverage, so an unfinished clip cannot block a completed episode's event.

## [1.22.13] — 2026-10-03

### Fixed

- Sample, trailer, and preview clips explicitly marked in a filename are no longer treated as regular episodes.
  An unfinished clip cannot block a completed episode or suppress its completion event; episode-only selection
  does not select the clip. Generic file labels are not reinterpreted as episode labels for client events.

## [1.22.12] — 2026-10-03

### Fixed

- Numeric filenames in brackets and postposed episode labels no longer treat zero, release years, or common video
  resolutions as episodes.
- Non-finite file progress reported by a torrent client no longer becomes a false 100% completion or episode event.
- Absolute file paths reported by a client are no longer reinterpreted as relative paths during disk verification.

## [1.22.11] — 2026-10-03

### Fixed

- Files explicitly numbered `SxxExx` in an ONA release are now tracked as episodes rather than generic files;
  OVA bonuses, special folders, and unnumbered ONA files remain excluded. Existing completed files gain episode
  progress and corrected last-event labels without duplicate notifications.
- Growing multi-season titles such as `1-31 серии из 52` now retain the known final episode count. Later-starting
  mixed-season packs remain conservatively counted from their files when cumulative numbering cannot be mapped.

## [1.22.10] — 2026-10-02

### Fixed

- A partial episode selection saved with an older seasonless key (for example `E14`) now counts a completed
  season-labelled file (`S03E14`) when the selected files identify exactly one matching season. Conflicting or
  ambiguous seasons remain uncounted, and reconciliation does not generate a duplicate completion event.

## [1.22.9] — 2026-10-02

### Fixed

- Growing torrents keep an explicit season from the original topic title when a newer tracker title has an
  unknown episode total. Current episode keys, the expected file count, and the existing last-event label are
  reconciled without inventing another completion event; ambiguous multi-season titles are not guessed.

## [1.22.8] — 2026-10-02

### Fixed

- Service Settings now shows when the current web server actually started, and labels the older saved restart
  record as a requested restart. Updating TOW no longer leaves an old request looking like the latest start.

## [1.22.7] — 2026-10-02

### Fixed

- Unexpected errors no longer expose raw URLs, credentials, or exception details through topic forms, service
  status, command-line output, logs, bot notifications, or the supervisor's job status. Known state-version errors
  still explain how to recover.
- Night-copy selection refuses symlinks and Windows junctions that point outside the backup directory.
- Long episode titles are bounded before notification pattern matching.

## [1.22.6] — 2026-10-02

### Fixed

- History, the log window and Settings show the current topic name for older file and episode events that recorded
  only a topic ID or torrent hash. Search finds those events by name; ambiguous hashes remain unlabelled.

## [1.22.5] — 2026-10-02

### Fixed

- Custom tracker URL and download-link patterns now have match time limits, including bounded repetitions that the
  input validator cannot reliably classify. Oversized notification titles are limited before pattern matching.
- A site's Open button uses a responding mirror when the active mirror was explicitly probed as down. Flash
  redirects refuse external destinations.

## [1.22.4] — 2026-10-02

### Fixed

- Archive updates extract only validated regular files and directories, rejecting Windows device names, drive
  prefixes and alternate streams without relying on the host Python's tar extraction filter.

## [1.22.3] — 2026-10-02

### Fixed

- Archive updates require the release checksum, cap unpacked size, and recover the old code after a hard process
  interruption. The release workflow runs the full gate on all three systems before publishing.
- Installers refuse unmarked foreign folders on removal; failed reinstalls preserve the earlier config exactly.
- Outbound tracker, notifier and heartbeat connections pin checked public DNS answers unless private hosts are
  explicitly allowed. A night copy fails visibly if a previously backed-up core file disappears.
- Settings shows how many notifications were discarded when a recipient's bounded queue overflowed.

## [1.22.2] — 2026-10-02

1.22.1 was tagged but not published: its Linux and macOS release check failed in the test itself (after installing
again it expected TOW to run without starting it, then called an undefined stop file). 1.22.2 has the same changes
and the fixed check, which CI now runs on every commit.

### Fixed

- Installers: after a removal that kept the data, `-Uninstall -Purge` / `--uninstall --purge` now deletes what
  stayed (it said "no TOW install"), and installing again into the same folder reuses the data, key and settings
  instead of refusing. A failed install there removes only what it added.

### Changed

- Release page: every file has a label saying what it is for, and the notes start with what to download for each
  system, in English and Russian.

## [1.22.0] — 2026-10-02

### Added

- Windows bundle `TOW-windows-x64.zip`: extract it anywhere and double-click **Start TOW.cmd**. Python, uv and every
  package are inside, so the first start needs no internet; `Stop TOW.cmd` and `Update TOW.cmd` sit next to it.
- One-line installers: `irm …/releases/latest/download/install.ps1 | iex` on Windows and
  `curl -LsSf …/releases/latest/download/install.sh | sh` on Linux and macOS. Downloads are checked against the
  release's `SHA256SUMS`; an existing install is never overwritten; `--autostart`, `--port`, `--uninstall`, and on
  Linux `--desktop` for a menu entry. On macOS the installer creates `Start TOW.command`, on Linux `start-tow`.
- `tow start`: starts TOW in the background unless it runs, waits until it answers and opens its page.
- Updates without git: installs from the bundle or the installers download the release from GitHub (`--ref latest`
  or a tag from v1.22.0), check it against `SHA256SUMS`, keep the previous code in `app.prev` and roll back by
  themselves.
- A release workflow builds and tests the bundle and both installers on Windows, Linux and macOS before it uploads
  them to the release.
- Step-by-step install guide for beginners: [docs/install.md](docs/install.md).

### Changed

- README: install from the release first; the git install moved to the install guide.
- On Linux without a desktop TOW never opens a browser in the terminal; it prints the address.

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
