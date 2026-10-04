# Changelog

All notable changes to TOW. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [semantic versioning](https://semver.org/). Русская версия: [CHANGELOG.ru.md](CHANGELOG.ru.md).

## [1.22.44] — 2026-10-04

### Fixed

- Apply the job journal's 64 KiB, finite-number and nesting limits in the detached updater,
  including the initial read, broker entry and lease re-read. Unreadable records are retained
  without starting installation or escaping with a parser error. Job identifiers cannot select
  a temporary path; writes validate before an exclusive temporary file, fsync and atomic replace.
- Require ownership of the worker lease and read back the unchanged queued reservation even
  to publish a handoff refusal. A late or failed second worker cannot overwrite another worker's
  job; an unreadable lease never grants permission to publish a terminal result. Refuse a launch
  target that disagrees with its reservation and handle overflowing handoff dates safely.
- Verify current state with strict finite JSON and the store's nesting contract before stopping
  for a web update. Malformed state produces a safe preflight refusal without rewriting data;
  torrent state is not subject to the small job-size limit. The worker remains standalone and
  Python 3.11 compatible.
- Bind every copied-worker entry to the fixed journal next to its own job folder. A foreign
  journal or identifier is refused before opening the record; relays propagate the derived path,
  not a command-line-selected filename. Equivalent absolute paths remain supported.

## [1.22.43] — 2026-10-04

### Fixed

- Bound small service records before JSON decoding, reject excessive nesting, non-finite
  numbers and invalid dates, and preserve the original files during observation. Damaged
  backup or watchdog records show an unknown result, not a healthy backup or fabricated outage;
  settings remain available. Diagnostic writes enforce the same size and JSON limits.
- Use the latest backup attempt's recorded error, not timestamp ordering, to distinguish
  success and failure after clock corrections or attempts within the same millisecond.
  Invalid scheduler timestamps and countdown containers no longer escape into scheduling;
  unreadable or future-dated update markers do not suppress outage reporting.
- Pin future-dated scheduler facts once after a backward clock correction. Repeated ticks
  no longer postpone checks, progress, watchdog duties or failed-copy retries indefinitely;
  startup delays remain bounded, and a future backup date cannot skip every following night.

## [1.22.42] — 2026-10-04

### Fixed

- Check night-copy state, history and both encrypted settings files using the same readers
  as live data, after signature and checksum verification. Damaged containers, non-finite
  numbers, excessive nesting, unsupported state versions and unreadable secrets are refused
  before restore writes or pruning earlier copies. Preview and verification use the same
  preflight; valid legacy stores remain supported without rewriting their bytes.

## [1.22.41] — 2026-10-04

### Fixed

- Refuse drive-relative Windows backup folders such as `D:copies` or `D:` before resolution
  and write probes, including paths already stored in settings. These depend on a drive's current
  directory instead of the TOW install. The refusal explains how to use a full drive path or an
  ordinary install-relative folder; both remain supported on their respective operating systems.
- Bound night-copy descriptions to 1 MiB before parsing and before publishing a new copy.
  Invalid file sizes and dates no longer break Settings; verification refuses coerced or
  unrepresentable sizes. A description exceeding the limit leaves earlier copies untouched.
  Unrepresentable byte counts show an unknown size rather than an application error.

## [1.22.40] — 2026-10-04

### Fixed

- Reject Windows device namespaces with forward, back or mixed slashes before backup folder
  resolution, write probes and torrent-client actions. Shared syntax and folder-policy checks apply
  on every operating system, including configured paths and edits keeping an existing path.
  Ordinary local folders, relative backup locations and explicitly enabled network shares still work.

## [1.22.39] — 2026-10-04

### Fixed

- Normalize UTF-16 and UTF-32 configurations in portable archives to UTF-8 before import writes,
  keeping comments, quoting, anchors and line endings. Verify original checksums first and bound both
  input and converted bytes. Preview, verification and apply agree; malformed Unicode is refused
  without changing live files. Export normalizes only the archive, and rollback preserves exact prior bytes.

## [1.22.38] — 2026-10-04

### Fixed

- Bound serialized YAML by the same UTF-8 byte limit as its reader, including headers, multibyte text,
  escaped scalars and expanded scalar aliases. Save, night restore and import overrides cannot create
  an oversized configuration; refusal happens before replacing files or creating a destination directory.

## [1.22.37] — 2026-10-04

### Fixed

- Bound YAML input, expanded nodes, text and depth before constructing aliases or merge mappings, and check
  programmatic graphs before copying or saving them. Cyclic, oversized and deeply reused references cannot
  reach recursive configuration, backup or restore operations; ordinary safe anchors and merges remain supported.
- Check night-copy configuration during verification and preview, before a restore starts writing. Reject
  non-mapping values such as `false`, `0` and `[]` instead of silently treating them as empty settings.
- Scan credential-shaped fields iteratively, inspecting shared containers once and building a path only for
  the actual match. Large JSON histories do not inherit YAML expansion limits; refusals do not expose input values.

## [1.22.36] — 2026-10-04

### Fixed

- Retry a briefly busy update-worker lease for at most one second using nonblocking attempts and a monotonic
  deadline. A status reader no longer aborts the initial handoff; a competing worker is still excluded.
- Re-read the queued job and check its handoff expiry again after acquiring the lease. Replaced, changed,
  completed or expired jobs cannot start an installation after the wait.

## [1.22.35] — 2026-10-04

### Fixed

- Import Monitorrent state and encrypted credentials in one journaled transaction with read-back before commit.
  Restore the original stores after a write failure, recover interrupted writes on the next data lock, and report
  failed recovery distinctly without discarding its journal or verified safety archive.
- Bind newly imported topics to the selected enabled client (`--client ID`, or the current main client), leaving
  existing topics unchanged. Never copy qBittorrent credentials into another client's secret block, including
  cross-type shared references; report skipped settings in preview and apply.
- Preserve partly configured client/tracker logins, passwords, custom ports and notification recipients too,
  without mixing them with a different imported account. An empty client's default port alone is not a configured login.
- Read all source queries from one SQLite snapshot. Corrupt, busy and malformed databases no longer look like
  successful empty imports; errors are localized and do not print private exception details.

## [1.22.34] — 2026-10-04

### Fixed

- Confirm absence after removing an old restore-point archive; held files, unchanged paths and unreadable
  results leave a cleanup warning while the new verified archive stays usable. Retain foreign and protected copies.
- Preserve safety-archive cleanup warnings through point/file restores, Monitorrent import and web updates.
  Show successful restores separately from incomplete cleanup, including simultaneous audit-log warnings.
- Keep folder-bound cleanup status visible after a page reload, in the collapsed Settings section, archive
  card, history and localized update progress. The watchdog reports pending cleanup and completion once per
  state change without turning a working service or verified copy into an outage.

## [1.22.33] — 2026-10-04

### Fixed

- Report only confirmed removals of old night and safety copies. A held file, partial deletion or unreadable
  outcome leaves a cleanup warning instead of claiming success; a verified new copy remains usable and a
  committed restore is not rolled back because cleanup failed.
- Retain ownership manifests/journals until the final directory removal and preserve them after a held
  directory so cleanup can be retried. Never sweep up foreign files, links, junctions or unfinished restores.
- Show pending night-copy cleanup in the collapsed Settings summary and card, localized copy/restore results,
  CLI output and a labelled history event; clear the night warning after successful cleanup. Messenger
  alerts report cleanup pending and completion once per change, without calling a usable copy a failure.

## [1.22.32] — 2026-10-04

### Fixed

- Bind new web-update jobs to a process-held file lease, not a reusable PID. Long-running updates remain
  protected; an exited worker releases the lease even after a crash. Interrupted jobs stay visible, and a
  later verified terminal recovery can unblock them without rewriting the old journal.
- Check the unique worker script path for older jobs before accepting a live PID as their worker. If process
  identity cannot be read unambiguously, explain it and keep the reservation rather than risk overlapping writes.
  Read old-worker identity only once per status request; do not truncate POSIX process arguments.
- Refuse missing, unreadable or unsupported worker leases before installation; retain the standard-library,
  Python 3.11-compatible detached runner and copied lock module.
- Explain the original master-key requirement directly beside the backup-file controls. Browser backups do
  not contain the key; a different installation's key cannot open them, and a failed check changes no data.

## [1.22.31] — 2026-10-04

### Fixed

- Report backup-file preparation failures in Settings instead of an unexplained server error. Checking and
  restoring a file now explain unavailable storage or folder permissions without changing the current data.
- Clean up staged uploads even if closing the uploaded file fails.
- Report a missing restore audit event without undoing a successfully committed restore. Settings show a
  warning, and the import result and durable transaction marker no longer claim an unwritten event was recorded.
- Run browser-script tests by loading the checked-in modules directly, without evaluating source strings;
  retain the update, rollback, recovery, overlay and countdown checks.
- Keep the security support policy aligned with the latest stable release rather than an obsolete version line.

## [1.22.30] — 2026-10-04

### Fixed

- Compare the loaded page's version with the installed version before offering a reload after an update,
  rollback or recovery. A freshly loaded page no longer asks to reload itself; an older tab retains an
  explicit reload action without automatically navigating or installing again.
- Preserve operation dates, failure details and the update log; keep reload hidden during an active operation.
- Refresh installation status on a manual release check and the existing hourly visible-page check, so
  updates from another tab can be discovered without additional frequent background polling.

## [1.22.29] — 2026-10-04

### Fixed

- Confirm child-process exit before reporting a stop, finishing a timed-out job or replacing the web server.
  Failed stops retain the tracked process and retry with a pause instead of starting overlapping work.
- On an abnormal supervisor exit, attempt to stop both the active job and web server before releasing the
  instance lock. Cleanup failures do not hide the original error or skip the other child.
- Do not terminate a finished child's possibly reused PID. Describe TOW consistently as one supervised
  service, not one operating-system process, in Settings, CLI help and current documentation.
- The activity log describes a queued restart as requested, not completed. Recovery notifications no
  longer claim the reporting watchdog performs the restart.

## [1.22.28] — 2026-10-04

### Fixed

- Keep zero-season folders and zero-numbered specials out of ordinary episode selection and completion.
  They remain downloadable and observable as files; existing misclassified history and its last-event wording
  are corrected silently on reconciliation without changing completion dates.
- Validate episode-rule and file-pattern syntax when saving a topic, before recording changes or checking the client.
  A zero-numbered range cannot silently select only its ordinary episodes.
- Invalid per-file progress, including booleans, out-of-range fractions and overflowing numbers, cannot confirm
  a completed episode or emit completion events merely because a full-sized file is present.

## [1.22.27] — 2026-10-04

### Fixed

- Confirm the stop before restoring file priorities after a failed change; do not restore or resume while
  stopping remains unconfirmed. Shared clients also confirm the restart and report incomplete recovery.
- Reject malformed or out-of-range progress as proof that a stopped torrent is complete. Preserve valid
  completed torrents and active queued/checking states across qBittorrent 4/5, Transmission and Deluge.
- Preserve client error states during ownership confirmation instead of reporting missing ownership.
  qBittorrent refuses an unsafe result even if the pending marker was successfully removed.

## [1.22.26] — 2026-10-04

### Fixed

- Recheck torrent ownership before client changes, relocation and failure cleanup. Selection changes in
  qBittorrent refuse foreign torrents; lost ownership is never reclaimed by clearing the pending marker.
- Confirm the complete file list, stable file identities and explicit priorities before starting a selection.
  Missing or malformed flags are unknown, not implied download/skip choices; reordered lists remain supported.
- Confirm restoration of the previous selection before resuming a failed change. Failed or unsafe rollback
  stays stopped, preserves the original error and reports the cleanup failure.

## [1.22.25] — 2026-10-03

### Fixed

- When installation finishes during a slow update-log read, request the final log after that read completes
  instead of leaving older progress on screen. Coalesce refresh requests without parallel duplicate reads.

## [1.22.24] — 2026-10-03

### Fixed

- Match repeated web-update requests to the new operation after a lost response, without showing an older
  successful result or automatically resending installation. Follow newer operations from another tab.
- Retry transient update-status failures with disabled installation controls instead of claiming the
  installation is unsupported. Refresh an open update log through completion and ignore obsolete responses.
- Reject invalid release-check timestamps instead of displaying an invalid date.

## [1.22.23] — 2026-10-03

### Fixed

- Windows web updates use a local process broker when an outer job forbids detaching. The actual worker verifies
  independence and waits for its relay to exit before stopping TOW; expired reservations refuse late launches.
  The environment travels in memory, not through command-line secrets or a new scheduled task.
- Update results show the specific refusal reason and local start/finish times. A later verified terminal recovery
  clears a stale failed-job view without deleting its original record or log.

## [1.22.22] — 2026-10-03

### Fixed

- Move the installed version and quiet new-release badge into a small floating corner indicator, outside
  the header layout. It hides while overlapping controls or table cells; the check clock stays in the header.

## [1.22.21] — 2026-10-03

### Fixed

- The web updater exits the Windows web-server process tree through an intermediate parent and waits for its
  actual exit before stopping TOW. Job breakaway alone did not protect it against taskkill /T.
- Web version selection refuses 1.22.20 and earlier, which lack this safe handoff.

## [1.22.20] — 2026-10-03

### Added

- Installed version in the page header and a quiet new-release badge. Settings show release notes and a cached
  stable-release check; offline discovery never turns into a false “up to date” result or breaks normal pages.
- Explicit web updates and compatible version selection, with confirmation, a verified encrypted data archive
  before launch, an independent updater, durable progress, bounded redacted logs and automatic code/data rollback
  on installation or health failure. Concurrent web changes are refused while updating. Web targets start at
  1.22.20; newer data schemas and unverified releases are refused. Service-managed POSIX installs use the terminal
  until independently detached workers are supported. Updates are never installed automatically.

### Fixed

- A failed restore-point export never deletes an existing file. Rotation only removes decryptable, verified
  local archives; foreign or damaged files remain. Failure to remove an old copy keeps the new verified point
  and reports a cleanup warning instead of claiming that saving failed.
- Export without overwrite publishes a complete archive without clobbering a file created by another writer.
  Failed read-back never deletes a replacement file. The manual update command uses an explicit release tag
  in both git and archive installs.
- Service help distinguishes watchdog notifications from the supervisor's web-server restarts and removes the
  obsolete five-Windows-task diagnostics description.

## [1.22.19] — 2026-10-03

### Fixed

- Night-copy rollback verifies every saved file and destination before changing any live store, uses the verified
  bytes, and checks each write back. Restore-point destinations are resolved from the saved configuration without
  replacing the live configuration during preflight. Newly written safety copies are verified before restoration starts.
- Failed safety-folder reservation never removes another writer's files. Automatic cleanup preserves unreadable,
  unfinished and foreign-file-containing safety copies and import checkpoints, including links and Windows reparse points.
- Encrypted secrets and undo snapshots reject non-finite numbers, excessive nesting and lossy JSON conversions before
  writing. Invalid encrypted or legacy secret JSON produces a controlled error without replacing existing credentials.
- Import checkpoints are verified before an import starts. An unreadable transaction refuses ordinary writes instead
  of silently allowing edits over a potentially incomplete import; valid finished copies do not replay an old rollback.
- Export refuses quarantined download history instead of creating an apparently healthy bundle with empty history.

## [1.22.18] — 2026-10-03

### Fixed

- Check recovery verifies every backup and target before changing either state or download history, and restores
  the already verified bytes. Damaged copies cannot overwrite working stores before the error is detected.
- Malformed recovery fields, parser limits, directories and Windows reparse points fail closed. A failed journal
  reservation never deletes another writer's files; backup writes are verified before a check can start.
- Settings and undo recovery also reuse verified backup bytes and preflight every target. Cleanup only removes
  recognized regular journal files; interrupted preparation or cleanup no longer leaves a permanently blocking journal.
- Persistent JSON rejects non-finite numbers and reports integer/depth parser limits as storage errors. Writes
  cannot persist invalid numbers; a transient I/O error during a corruption recheck never quarantines a valid replacement.
- Malformed state/history containers fail closed instead of becoming empty history or escaping as attribute errors.
  Read-only checks preserve the source; applying reads retain the corrupt bytes in quarantine, and invalid writes are refused.

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
