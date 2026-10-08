# Install and operations

TOW is portable and cross-platform: it writes only inside its own folder. A home program: a device
signed in with the password is the owner. The code API this rests on (`tow.paths`, `tow.platform`)
is described in [architecture.md](architecture.md#code-api-paths-and-platform).

## 1. One folder

Backup locations and client save paths refuse Windows device namespaces (`\\?\`, `\\.\`),
including forward and mixed separators, before path resolution or filesystem access. Windows
[normalizes forward slashes and recognizes device namespaces](https://learn.microsoft.com/en-us/dotnet/standard/io/file-path-formats);
comparing these paths only with ordinary protected-folder names is not sufficient. The rule applies
on every OS, since a remote client can run on Windows. Ordinary local paths, relative backup folders
and explicitly enabled client network shares are unaffected.

Backup folders also refuse drive-relative Windows forms such as `D:copies` and `D:` on every OS.
Unlike `D:\copies`, these depend on the drive's current directory, as described by
[Microsoft's path-format guidance](https://learn.microsoft.com/en-us/dotnet/standard/io/file-path-formats).
Joining such a path to an install on a different drive does not make it absolute. Use a full drive
path, or an ordinary relative folder such as `copies/daily` (relative to the TOW install). Paths
already stored in settings are checked again before backup/restore-point use; no folder is probed
or created for the refused form.

```
<TOW>/                      the install root (movable, any drive, any OS)
  app/                      the code: a git clone at a release tag, or a release's source archive
  app.prev/                 an archive install's previous code, after an update (one is kept)
  Start TOW.cmd, ...        the start files of the bundle and the installers (§1a)
  config.yaml               live configuration (the UI writes it without comments; every setting
                            is explained in app/config.example.yaml)
  data/                     state, history, secrets.enc, tmp/, browser profiles
  data/logs/                run.log, serve.log, serve-stderr.log, <job>-last.log, launchd.log
  data/run/                 run.lock, run.pid, status.json, schedule.json, control/ (tow run's files)
  keys/master.key           the master key — inside the install, outside data/ (data snapshots,
                            exports and night copies never contain it)
  backup/                   night copies (default backup/night), pre-update snapshots, key copies
  runtime/python/           the uv-managed Python (UV_PYTHON_INSTALL_DIR)
  runtime/cache/            uv cache (UV_CACHE_DIR)
  runtime/bin/uv(.exe)      uv itself, when the bundle or an installer brought it
```
Development checkout: the repo root plays `<TOW>` (data/, keys/, config.yaml gitignored).

Root discovery (`tow.paths.root()`, built):
1. `TOW_ROOT` (the launchers set it). An older launcher that set it to the code folder is
   recognised: when it equals the code folder of a runtime layout, the parent is used. One that
   names another folder than the runtime install whose `app/` runs and whose environment is in
   use (a variable for the whole account left from a move: `C:\TOW` after moving to `D:\TOW`) is
   not followed - otherwise TOW would create `data/` and a new master key in the old place and
   start empty: the launchers ignore it with a note on stderr, TOW takes the code's install
   (`paths.root_env_ignored`), and `tow run` names it in `run.log` (`layout.outside_overrides`).
2. The code folder is named `app` and its parent has `config.yaml` or `data/` → that parent.
3. The code folder is a checkout (`pyproject.toml` + `src/tow`) → the checkout (development).
4. An installed wheel without a layout: the parent of `TOW_CONFIG`, else of `TOW_HOME`; else
   `RuntimeError("TOW_ROOT must be configured …")` — nothing is created in site-packages.

An install runs only its own code: every `tow` command first asks `tow.paths.foreign_code()`
and refuses (exit code 3, `cli.foreign_code`, before anything is written in the folder)
when the root has an `app/` that is not the code running - a copy whose `.venv` still imports
the original's `app/src`, started with the copy as its root (also by its autostart).

`TOW_HOME`/`TOW_CONFIG` stay explicit overrides (tests, scripts). Since 1.21 the launchers no
longer read an `app\tow-local.cmd` (installs before 1.18 set them there; discovery finds the same
folders, and a leftover file is simply ignored — delete it).
`data_dir`, `tmp_dir`, `logs_dir`, `run_dir` are created by the call; `keys_dir`, `backup_root`,
`runtime_dir` are not (whoever writes there creates them). A layout from before
1.18 (`<TOW>\app`, `<TOW>\data`, `<TOW>\config.yaml`) resolves to the same paths as before.

Master key resolution (built, `tow.store`): `TOW_MASTER_KEY` (env, tests/CI) >
`TOW_MASTER_KEY_FILE` (explicit, legacy; a relative path is inside data/) >
`<root>/keys/master.key` > `<data>/master.key` (an install from before 1.18; since 1.21 found by
TOW itself on every start, not only by `tow-env.cmd`). `secret_store_status()["key_source"]` is
`env`, `file`, `install`, `legacy` or `missing`; `master_key_file()` names the file in use;
`tow keys adopt` without `--from` takes `TOW_MASTER_KEY_FILE`, else the legacy file.
- `tow secrets generate-key [--key-file F]` writes `keys/master.key` by default (a new folder is
  for this account only: 0700 on POSIX, no inherited permissions on Windows; the file is created 0600 in the same call, `O_CREAT|O_EXCL`, never with the umask's mode
  first); it refuses the default place while `secrets.enc` exists (that would create a
  second key that takes over once `TOW_MASTER_KEY_FILE` is removed) and never overwrites a file.
- `tow keys adopt [--from F]` (default `TOW_MASTER_KEY_FILE`) copies the key into `keys/` only
  after it decrypts `secrets.enc` and `secrets-undo.enc` (when present), under the persistence
  lock; an identical key already there is "already adopted", a different one is never replaced;
  the copy is read back; the key is never printed, returned or logged. `tow keys status` shows
  the source and the files, not the key. Errors carry a kind (`cli.keys.error.<kind>`).
- Never in a copy of the data: night copies and exports are allowlists of data/ files, restore
  points are exports, update snapshots copy data/ and config.yaml only (never keys/, and exclude a
  legacy data/master.key) — `tests/test_master_key.py` proves it for night copies, exports and
  restore points, `tests/test_update.py` for update snapshots.

Temp (built): `tow.paths.use_private_temp()` at process start — `tow.cli.main` (every command,
`tow serve` included) and the web app's lifespan (for `uvicorn tow.web:app` started by something
else) — sets `tempfile.tempdir = <data>/tmp` and `TMP`/`TEMP`/`TMPDIR` for child processes, and
removes entries older than a day. Other writers use `dir=` inside data/ (store, the autostart
task XML, the browser bundle routes).

Permissions (1.21): at the same place `tow.platform.use_private_files()` sets umask 077 on Linux
and macOS, so data/, logs, temporary files and night copies a TOW process creates are readable by
the owner's account only (child processes inherit it). Every start then runs
`tow.platform.private_root()` on the install root and `tow.platform.private_folders()` on `keys/`
and `data/`: a folder this account owns that other accounts can get into is closed - on Windows
its inherited permissions are replaced by this account, SYSTEM and Administrators (`icacls
/inheritance:r /grant:r`, so an install in `C:\TOW` no longer inherits "Authenticated Users:
modify" from the drive root, and `app\`, `runtime\`, `config.yaml` and the start files inherit
that from the root), on Linux and macOS it becomes 0700 (the root only when every account may
write in it, so a folder shared with a group on purpose stays) - and read back. A folder owned
by another account, also by Administrators, is never changed. A folder that stays open is named
on stderr and in `tow doctor`; the start goes on. `tow permissions` says why; `tow permissions
fix`, run once in an administrator terminal, makes the account autostart runs as (else the one
that opened the terminal, never Administrators) the owner of the root, `keys/` and `data/`,
closes them as above and reads them back, touching nothing outside the root. `--owner ACCOUNT`
(`PC\name` or `name`) names that account when TOW cannot tell it (an administrator password of
another account opened the terminal); outside an administrator terminal it is refused and
nothing changes. A new key folder is closed the same way, and
`install.ps1` closes the install folder before it unpacks anything into it (when it creates the
folder or this account owns it; otherwise `keys\` and `data\` alone).

Python and caches (built, in the launchers and, since 1.21, `update.py`'s `launcher_env`): for a
runtime install `UV_PYTHON_INSTALL_DIR=<root>/runtime/python`,
`UV_PYTHON_BIN_DIR=<root>/runtime/bin`, `UV_CACHE_DIR=<root>/runtime/cache`,
`UV_PROJECT_ENVIRONMENT=<root>/app/.venv`, `UV_MANAGED_PYTHON=1`; a `runtime/bin/uv(.exe)` is
preferred over uv on PATH. A development checkout keeps the developer's uv setup unchanged.
`.venv` holds absolute paths: after moving the folder `tow setup` rebuilds it.
The release smoke tests also move a previously started and stopped installation with its
existing `.venv`, then require an offline rebuild, Python and TOW inside the new folder,
unchanged configuration and master key, and a successful start and stop. This checks relocation,
not only unpacking a fresh archive. Moving a folder between operating systems still requires
that system's uv/Python and dependencies; a Windows binary is not a Linux/macOS executable.
Python is pinned to 3.14.8. New bundles/installers carry uv 0.12.23; existing uv 0.12 builds
can discover the new patch through `tool.uv.python-downloads-json-url`, an official upstream
manifest pinned to a commit (not `main` or `latest`). uv installs and verifies the managed
interpreter normally inside `runtime/python`; old interpreters remain available for rollback.
No external uv executable, system Python, PATH or registry registration is updated.

Prerequisites (1.22):
- the Windows bundle: 64-bit Windows 10 or 11 on x64, nothing else (uv, Python and every wheel
  are inside; the first start is offline). `install.ps1` refuses 32-bit Windows and Windows on
  ARM (also from an x64-emulated or 32-bit PowerShell there) before it writes anything: the zip
  holds x64 programs. `install.ps1` / `install.sh`: the network once (GitHub; on Linux and macOS also
  Python's and the wheels' downloads by uv), `curl` or `wget`, `tar` and a SHA-256 tool on
  Linux and macOS.
- a git install: `git` (`tow update` switches tags), `uv` ≥ 0.12 (`[tool.uv]
  required-version`; on PATH or `<TOW>/runtime/bin/uv`), the network once for `tow setup`
  (Python 3.14 and the locked wheels).
- browser sign-in: Edge, Google Chrome or Chromium - on Linux **not the snap** (a snap Chromium
  cannot use a profile outside the home folder; `.deb`/rpm Chrome or the distribution's Chromium).
- Linux: `lsof` or `ss` (iproute2) to name a program holding TOW's port (without both TOW reads
  `/proc`); a systemd user manager for autostart (an XDG autostart entry only on a machine without
  systemd).
- macOS: Apple Silicon. **Intel Macs** have no `cryptography` wheel in `uv.lock` (50.0.2 ships
  macOS arm64 only), so `uv sync` builds it from source: that needs a Rust toolchain (rustup) and
  the Xcode command line tools; without them Intel Macs are unsupported.

## 1a. Three kinds of install (1.22)

The owner's guide is [install.md](install.md); the README links the stable release assets
(`releases/latest/download/<name>`), built and tested by `.github/workflows/release.yml` (§7).

| kind | how it comes | `app/` | start files in `<TOW>` | update |
|---|---|---|---|---|
| Windows bundle | `TOW-windows-x64.zip` (`scripts/build-bundle.py`) | source archive, no tests | `Start TOW.cmd`, `Stop TOW.cmd`, `Update TOW.cmd`, `README.txt` | `Update TOW.cmd [tag]` → `update.py --ref latest` |
| `install.ps1` | downloads the bundle, checks `SHA256SUMS`, unpacks, runs `Start TOW.cmd` | the same | the same | the same |
| `install.sh` | uv for the platform (its release's `.sha256`) into `runtime/bin`, the tag's source archive (`SHA256SUMS`) into `app/`, `tow setup` | source archive | macOS `Start/Stop/Update TOW.command`, Linux `start-tow`, `stop-tow`, `update-tow` | `update-tow [tag]` → `update.py --ref latest` |
| git | `git clone` (install.md, "Manual install with git") | a clone at a tag | none (`scripts/tow`, `tow.cmd`) | `deploy.ps1 -Ref <tag>` / `update.py --ref <tag>` |

- **The bundle** (`TOW\`): the code, `config.yaml` from the template, the pinned `uv.exe`,
  the CPython of `.python-version` in `runtime\python` and uv's cache with every runtime wheel
  of `uv.lock` in `runtime\cache` (proved at build time by an offline `uv sync` into a second
  environment; entries keyed by the build machine's paths and uv's absolute minor-version link
  are dropped). No `.venv`: it holds absolute paths. ~50 MiB zipped, ~130 MiB unpacked,
  ~170 MiB after the first start.
- **Start files** only call `app/scripts`, so they follow the code through updates:
  `Start TOW.cmd` → `scripts/tow-start.cmd`, `Start TOW.command` / `start-tow` →
  `scripts/tow-start`. These check that `app/.venv` runs and is this folder's own (`tow` and
  the base Python both inside `<TOW>`); otherwise they run `tow setup` - on Windows first with
  `UV_OFFLINE=1` (the bundle's cache), then online, after removing the "Mark of the Web" from
  the folder's files (`Unblock-File`; Windows would ask about each start file again). Then
  `tow start` (§6). The first start waits for a key so the master-key note is read. The update
  files hold their own logic (`app/` may be half-switched): they run `update.py` with the Python
  `app/.venv/pyvenv.cfg` names (by its folder name in `runtime/python`, so a moved folder finds
  it), else with the newest `cpython-3.X.Y` there by number, never a link or a pre-release.
  The three Windows start files come from `scripts/root_files.py`, which `build-bundle.py` writes
  into the zip and `update.py` writes into `<TOW>` again (§4): up to 1.28.1 an update never
  replaced them, so an install that began with an older zip kept that zip's files.
- **Installers** refuse a folder that holds an install (they name the update file) or anything
  but what an uninstall keeps: `data/`, `keys/`, `config.yaml`, `backup/` and the `.tow-install`
  marker. Around those they install again and change nothing of them (an existing `config.yaml`
  stays; `--port` / `-Port` only rewrites its `port:`). Kept data without the marker (an older
  installer, or another program's `data/` and `config.yaml`) is refused unless `--adopt-data` /
  `-AdoptData` says it is the owner's former TOW folder. A failed install removes only what it
  added: a folder it created is removed, kept data and a kept `config.yaml` stay as they were,
  and an empty folder is left empty. `--port` /
  `-Port` (or `TOW_INSTALL_PORT`) sets `port:`. `--uninstall` / `-Uninstall` asks (`--yes`),
  turns autostart off, stops TOW, removes the Linux menu entry that names this folder and
  keeps `data/`, `keys/`, `config.yaml`, `backup/` unless `--purge`. `install.sh` reads
  answers from `/dev/tty` (its stdin is the script under `curl | sh`); `install.ps1` runs in
  Windows PowerShell 5.1 and 7, keeps its settings inside a function and never `exit`s (under
  `irm | iex` that would close the owner's window); it is ASCII (irm decodes a release asset in
  any code page). `TOW_INSTALL_SOURCE` / `TOW_INSTALL_SUMS` install from local files (tests).
- `install.sh` takes uv `-unknown-linux-gnu` on glibc, `-musl` elsewhere, `-apple-darwin` on
  macOS; `--desktop` (Linux) writes `$XDG_DATA_HOME/applications/tow.desktop` (the only write
  outside the folder besides autostart, both on request).

## 2. One supervised service: `tow run`

`tow run` (package `tow.supervisor`) manages TOW on every OS since 1.18: a web-server child,
scheduled child jobs (the global check, personal timers, progress, disk space, night copies) and
watchdog duties. (The five Windows tasks of 1.17 — TOW-serve, -check,
-progress, -backup, -watchdog — are gone since 1.21; see §8.) It ticks once a second and never
blocks for long.

- **Single instance.** `data/run/run.lock` is held while it runs (the OS releases it when the
  process dies, so a crash never leaves a stale lock); `data/run/run.pid` says who holds it.
  A second `tow run` prints "already running" and exits 0 (an autostart that finds TOW running is
  not a failure to retry).
- **The port.** If something listens on the port, `tow run` first checks whether it is a web
  server this install's previous supervisor left behind (killed, crashed): the pid recorded in
  `data/run/status.json` (or, on Windows, the venv launcher's child of it) that answers
  `/healthz` as this install, or runs `-m tow serve --log-file <this install>/data/logs/serve.log`
  (a hung one). `/healthz` names the install only to this computer: `install`, a hash of the code
  folder and the install root (`layout.install_id`), so a copy of the folder on the same port is
  another install - also a copy whose `.venv` still runs the original's code, whose copied
  `status.json` names the original's server. Only that is stopped, and TOW
  starts. Anything else on the port is never touched: `tow run` says so and exits 3 — or 0 when
  launchd started it (`TOW_AUTOSTART=launchd`), because launchd would retry it every minute
  without a limit; the reason is in `run.log`.
- **Started by the OS.** Task Scheduler, systemd and launchd start `python -m tow run` without
  the launcher's variables: the runtime layout (`<app>/..` holding `config.yaml` / `data/`, or
  `TOW_ROOT`) is resolved first and handed to every child (`TOW_HOME`, `TOW_CONFIG`, `TOW_ROOT`).
- **Web server.** `tow serve --log-file data/logs/serve.log --parent-pid <tow run>` runs as a child
  (stderr into `data/logs/serve-stderr.log`; a start that finds it at 5 MiB moves it to
  `serve-stderr.log.1`, replacing the older one). `/healthz` is asked every 10 s with httpx
  `trust_env=False` and a 3 s timeout. A fresh server gets 90 s to answer; a server that answered
  before is "hung" after six failed probes in a row (one minute). An exit or a hang leads to a
  restart after a pause that doubles from 1 s to 5 minutes and drops back to 1 s after ten quiet
  minutes. Restarts are counted over six hours: the first one in a while and, once per window,
  three or more are told to the owner's messengers (`watchdog.alert.restarted*`,
  `watchdog.alert.restarts_repeat`, the last error line from the server's log).
- **The web server never outlives `tow run`** (1.21): the supervisor stops it on every way out of
  its loop (`finally`); on Windows `tow run` puts itself into a kill-on-close job object that its
  children inherit (`platform.bind_children`), so even a killed supervisor takes its server
  along; on Linux the server asks the kernel for SIGTERM when its parent dies
  (`prctl(PR_SET_PDEATHSIG)`, `platform.die_with_parent`); and everywhere `--parent-pid` makes the
  server check every 2 s that the supervisor still runs and stop when it does not (ending the
  process 15 s later if a request hangs); on Linux and macOS by its own parent pid changing
  (re-parented), which a reused pid cannot hide.
- **Schedule** (`tow.supervisor.schedule`, tested with a fake clock):
  - check every `interval_sec` after the last *scheduled* one: the supervisor's own last start,
    kept in `data/run/schedule.json` (`check_started_at`), so a restart keeps the cadence;
    `health.auto_at_ts` only until there is one; never `health.at_ts`, which every manual check
    and progress pass moves (in 1.18–1.20 an install whose first check was manual got no
    scheduled check again). The first one two minutes after start. The child is
    `tow check --apply --notify --global-only --json` (`how="auto"`): the topics without a personal
    timer. The header countdown and Home's "checks are
    late" use the same real next check (`status.json` → `next.check` while `tow run` runs);
  - personal timers: when a topic's own interval is due, the overdue topics go into one child,
    `tow check --apply --notify --timer-only --json` (`how="timer"`); its start is saved in
    `schedule.json` (`timer_attempts`, `timer_batch`) before it is spawned;
  - progress every 30 minutes, the first five minutes after start:
    `tow check --apply --notify --progress-only --json`;
  - disk space every 5 minutes, only while a topic (not paused) waits in its client for disk space:
    `tow check --apply --notify --space-only --json` asks the client alone and starts the torrent
    once it fits;
  - night copy once per local calendar day at `backup_time` (config, default `"03:30"`; an
    unquoted YAML `03:30` is read too). A copy is due only when the newest good one is older than
    the latest slot — never because 24 hours have passed (the autumn DST day has 25, and 1.18–1.20
    copied twice on it): a slot missed while the machine was off or asleep is caught up at once,
    a failed copy is tried again six hours later or at the next slot if that comes first, and
    `status.json` → `next.backup` shows that very time. The slot is built for each calendar day in
    the owner's zone with PEP 495 rules (`zoneinfo`, or this machine's zone): a local time the
    spring switch skips runs an hour later, one the autumn switch repeats runs at its first
    occurrence, once. The last attempt is kept in `data/run/schedule.json`;
  - watchdog duties every 10 minutes **in-process**: lateness, night copies, change alerts, the
    heartbeat ping, the queued-message flush and the removal of an expired "Undo"'s saved
    secrets — never a restart (the supervisor owns the server). A data folder deleted or
    replaced while TOW runs (its id `data/.tow-data`, written once, is not the one `tow run`
    saw at its start) is said once (`watchdog.alert.data_lost`, Settings and `run.log`; the
    messengers too while their tokens are still readable) with the way back: a night copy.
    `tow watchdog` makes the same pass by hand as a diagnostic; it changes nothing either;
  - jobs run one at a time (a check also holds `check_run_lock` against a check from the web
    page), each with a time limit (check and timer 1 h, progress and space 10 min, night copy
    30 min); output goes to
    `data/logs/<job>-last.log` (`check`, `timer`, `progress`, `space`, `backup`).
- **Sleep and wake.** A wall clock that jumped ahead of the monotonic one (or a loop that stood
  still for more than five minutes) means the machine slept: overdue jobs run at once, a server
  still starting gets a fresh grace, and the watchdog duty measures lateness from the wake.
- **Control without signals.** Other processes write `data/run/control/restart` or
  `data/run/control/stop` (JSON with who and an operation id); the supervisor polls them every
  second. `tow restart` and Settings → TOW service → "Restart" restart the web server (Settings follows the
  operation on `data/service-restart.json`: stopping → starting → ready/failed; a new server that
  exits three times before it answers is "failed"); `tow stop` and the updater stop TOW. A stop
  lets a running job finish (up to 10 minutes, then it is stopped), then stops the web server and
  exits 0. SIGTERM (systemd, launchd), SIGHUP (its terminal closed), Ctrl+C and, on Windows,
  Ctrl+Break (SIGBREAK) stop it the same
  way with a 20-second job limit (all in all about 50 s: `SIGNAL_STOP_BUDGET_SEC`, which systemd's `TimeoutStopSec` and launchd's
  `ExitTimeOut` exceed).
- **Status and logs.** `data/run/status.json` is rewritten only when something changes (server
  state and pid, restarts, the running job, the next due times, the last wake) — never on a
  timer, so the disk can sleep. A reader holding it (Windows refuses the replace then) is waited
  for briefly; a write that still fails leaves no temporary file and is made at the next tick. `data/logs/run.log` is rotated at 5 MiB × 3; the terminal gets the
  same lines only when stderr is one (not the journal or `launchd.log`).

Without autostart: `tow run` in a terminal (`<app>\scripts\tow.cmd run` / `<app>/scripts/tow run`).

## 3. Autostart (optional; the only write outside the folder; explicit owner action)

`tow autostart on|off|status [--without-login] [--json]` and Settings → TOW service. One backend
per OS (`tow.autostart`), every OS command through an injectable runner, every change read back
before it counts. A development checkout (code not in `<TOW>/app`) is refused, so the
one-per-user names never point at it. A registration that belongs to another TOW folder is never
replaced or removed — unless the program it runs no longer exists (the task's action, the unit's
`ExecStart`, the agent's `ProgramArguments[0]`): that install was moved or deleted, and its
registration is taken over (or turned off). Until then the OS keeps starting a program that is
gone, so Diagnostics, Settings → TOW service, `tow status` and `tow doctor` say so
(`doctor.stale_autostart`) and that turning autostart on here takes it over.

- **Windows:** one Task Scheduler task `TOW`, created from XML: action
  `<app>\.venv\Scripts\pythonw.exe -m tow run` (no console window), working directory `<TOW>`;
  logon trigger for the owner; IgnoreNew, no battery limits, no time limit, restart on failure
  (1 min × 999). "Start without signing in": principal `S4U` (the owner's account, no stored
  password) with a boot trigger plus the logon trigger; Windows may require an elevated prompt
  for it, and network shares are not reachable that way. Read back with `Export-ScheduledTask`
  through PowerShell as base64 of UTF-8 (`schtasks /Query /XML` writes the ANSI code page: a
  Cyrillic folder on an English Windows came back as `?`); a missing task is told by the error's
  category, not by its translated text.
- **Linux:** systemd user unit `~/.config/systemd/user/tow.service` (`$XDG_CONFIG_HOME` honoured):
  `ExecStart="<app>/.venv/bin/tow" run`, `WorkingDirectory=<TOW>`, `Environment="TOW_ROOT=<TOW>"`,
  `Environment=TOW_AUTOSTART=systemd` (a unit without it, written by 1.24.1 or older, still counts
  as on), `Restart=on-failure`, `RestartPreventExitStatus=3`, `KillMode=mixed` (SIGTERM to `tow run` alone;
  it stops its job and server itself), `TimeoutStopSec=90`; no `After=network-online.target` (a
  system target the user manager does not have). `%`, `$`, quotes and backslashes in the path are
  escaped, so a folder named `50%` or `$HOME` names itself. Then `systemctl --user daemon-reload`
  and `enable --now`; read back with `is-enabled` and the file content. Without signing in needs
  lingering: TOW only shows `loginctl enable-linger <user>`. Only on a machine without systemd
  (no `/run/systemd/system`) is an XDG autostart entry `~/.config/autostart/tow.desktop` written
  instead (no "without signing in"); with systemd running but its user manager not answering
  (an SSH session without one) TOW says so and writes nothing. Turning it off runs `disable` (not
  `--now`) and removes the unit: a running TOW keeps running.
- **macOS:** LaunchAgent `~/Library/LaunchAgents/io.tow.plist` (`ProgramArguments`
  `<app>/.venv/bin/tow run`, `RunAtLoad`, `KeepAlive {SuccessfulExit: false}`, `ExitTimeOut` 60,
  `ProcessType` Adaptive, `TOW_AUTOSTART=launchd`, log in `data/logs/launchd.log`),
  `launchctl enable` + `bootstrap gui/<uid>`. Off unloads it (`launchctl bootout gui/<uid>/io.tow`)
  and removes the plist: a loaded agent would otherwise keep TOW alive until the next sign-in, so
  a TOW launchd started stops with it (the owner is told; `tow run` starts it by hand). The bootout
  returns while launchd still lets that TOW stop: the off is read back once `launchctl print` no
  longer finds the agent, waiting up to ExitTimeOut + 5 s (after that it is reported as not yet
  unloaded). Without signing in would need a system LaunchDaemon (administrator): not offered.
  - `bootstrap gui/<uid>` needs the owner's GUI session: over SSH without one it fails ("Domain
    does not support specified action"); sign in at the Mac once, then turn autostart on (or run
    `tow run` from the SSH session).
  - macOS privacy (TCC): a LaunchAgent may not read `~/Desktop`, `~/Documents`, `~/Downloads`,
    iCloud Drive or removable and network volumes without consent, and there is no window to ask
    in. Keep `<TOW>` and the night copy folder outside them, or give the install's Python
    (`<TOW>/runtime/python/…/bin/python3`) Full Disk Access in System Settings → Privacy & Security.

**Moving the install** (another drive or folder, any OS):

1. `tow autostart off` while the old folder still exists (or skip it: the registration of a
   folder that no longer exists is taken over in step 4);
2. `tow stop`, then move or copy the whole `<TOW>` folder;
3. in the new place: `<app>/scripts/tow setup` (`tow.cmd setup`) — `.venv` holds absolute paths;
   a copy turns uv's link `runtime/python/cpython-X.Y-*` into a plain folder, which setup removes
   (only that entry, only when it is not a link) so that uv can make the link again;
4. `tow autostart on` (and `tow run`, or let the autostart start it).

**Where the folder can live, and what can lead it elsewhere:**

- `TOW_HOME`, `TOW_CONFIG` and `TOW_MASTER_KEY_FILE` win over the folder's own `data/`,
  `config.yaml` and `keys/master.key` in every start alike — the launchers, Task Scheduler,
  systemd and launchd all start TOW in the owner's environment. One set for the whole account
  (left from another install) therefore makes a portable install use another data folder,
  config or key. `tow run` names such a variable in `run.log` at every start
  (`layout.outside_overrides`); remove it unless it is meant for this install. The autostart does
  not clear them: a start by hand and a start by the OS must use the same data.
- Windows without long paths enabled (`LongPathsEnabled`): an install folder longer than about
  110 characters lets paths deep in `app\.venv` pass the 260-character limit, and setup or a
  package import fails. Choose a shorter folder, or enable long paths. `install.ps1` refuses such
  a folder before it writes anything, and the Windows start file says so before it prepares TOW.
- The folder needs a file system with links: uv links `runtime/python/cpython-X.Y-*` to the
  full version (a junction on Windows, a symlink elsewhere). FAT32 and exFAT (many USB sticks)
  have none, and `tow setup` cannot complete there (not verified on every uv version); use NTFS
  (Windows), APFS/HFS+ (macOS) or ext4 and the like (Linux).

## 4. Update: `tow update --ref <tag>` (`scripts/update.py`)

### Web updates (1.22.20)

Every page shows the installed version. Its link opens the separate Settings → Version and updates panel.
Home shows a compact green notice for a known new stable release, without an automatic installation.
Rollback, old successful results and logs are collapsed; active operations and failures stay visible.
The automatic-check checkbox applies immediately (`check_updates: false` disables automatic discovery,
not manual checks or update-job monitoring). The shared release cache checks at most every 12 hours
while the interface is open (manual checks are throttled to one per minute; failures back off for an hour).
No configuration, topics, credentials or diagnostics are sent to GitHub. Offline means “not checked”, not
“up to date”. Normal pages and health do not wait for release discovery.

**Update** requires confirmation and a published stable version. Before launching anything, TOW exports and
reads back an encrypted `.towx` archive of settings, topics, history and credentials into the configured restore
points folder. Its key is derived from the installation's master key: keep `keys/master.key` separately, since
the archive does not contain it. Download a portable backup for moving to another computer; a server-local
copy alone does not protect against disk loss. Media files are never included or changed.

The updater and its base Python run outside `app/` and `.venv/`, so replacing the environment or closing the
web page does not stop the update. Its durable record and bounded redacted log live in `runtime/web-update/`.
Normal web mutations are refused while the job is active. The existing updater stops TOW, creates and verifies
an additional exact snapshot of `config.yaml` and persistent data in `backup/`, then switches code, synchronizes
the locked environment and requires the correct version and readable data. A failed installation or health
check rolls back code and changed data. The web page reconnects after restart, displays the actual result and
offers an explicit reload; a lost response is not treated as proof that no job started.
Each new request is matched to a new job, never to an older successful result. If acceptance cannot be
confirmed, the page only retries reading status and offers reload; it does not resend installation.
Temporary status failures disable installation controls until status is readable again. An open update
log refreshes with progress and completion; late responses from an older operation cannot replace it.
Since 1.22.32, a new worker holds a job-specific OS file lock for its entire installation and rollback,
outside the replaceable app. A long installation is not expired by a timeout; process exit or a crash
releases the lock. Status reads only probe the existing lock, never recreate it or rewrite the journal.
Missing, inaccessible or unsupported leases fail closed. Older job records retain their PID reservation
only while their unique worker script path matches the process command, or its identity cannot be verified;
a clearly unrelated process with a recycled PID does not keep an old job active.
An unknown or ambiguous old-worker identity has an explicit warning; it is not presented as verified.

Version selection accepts only published stable releases **from 1.22.21**, which retain the exiting-parent handoff.
The previous compatible release has a shortcut. Other versions can be entered explicitly; a target whose
declared state schema is older than the current data is refused before stopping TOW. This is not a promise of
safe downgrade to every historical release: older versions use the terminal, and future incompatible migrations
need restoration of matching data as well as code. Interrupted jobs are not silently overwritten; inspect the
local log, recover through the terminal and verify health before retrying.
An updater record confirming a later successful terminal recovery of the running version releases the stale
web-job reservation; reading status never erases the previous job or changes data.

Windows requires the actual worker to escape every inherited job and wait for its short-lived relay to exit.
Since 1.22.23, a relay still inside an outer job uses local `Win32_Process.Create` with breakaway startup flags.
The environment travels through stdin to a fixed PowerShell command, not through command-line secrets or a
temporary secret file; no new task, elevation or remote connection is requested. A failed broker, independence
check or parent wait refuses the update before stopping TOW, with a specific visible reason. This requires
the local WMI service and permission to create a process as the current user. Older web engines can safely
refuse this context; bootstrap the fixed release through the terminal. Both relay and worker use isolated
base Python outside the replaceable app; a queued worker arriving after its reservation expires refuses.
Linux/macOS installs started normally can detach into their own session. Web updates of
systemd/launchd-managed installations are refused with terminal instructions: a new session alone
does not survive when the service manager stops the process group or cgroup (systemd ends everything left in
`tow.service`'s cgroup once `tow run` exits). TOW knows it runs there from `TOW_AUTOSTART` (the unit and the
agent set it) or, for a unit an older TOW wrote, from systemd's `INVOCATION_ID` with the process in the
`tow.service` cgroup (launchd: `XPC_SERVICE_NAME=io.tow`) — `tow.autostart.service_manager`. Detaching
through `systemd-run --user --scope` is not attempted: the terminal update is the one supported path there.
No autostart or network access settings are changed by enabling these controls.

Release discovery uses fixed HTTPS endpoints of `d0j/tow` with public-IP-pinned requests, strict version checks,
timeouts and response limits. Archive installs verify SHA256SUMS; git installs use the configured origin.
These establish integrity and transport/repository trust, **not an independent publisher signature**. Keep
the configured origin trusted; installing from an arbitrary URL or branch is not a web option.

Export without overwrite atomically publishes prepared bytes (Windows rename; POSIX hard link) and refuses an
occupied name even if another writer creates it during preparation. POSIX destinations without hard-link support
fail safely rather than using a replacement fallback; choose a supported local filesystem. Unreadable/foreign
restore archives are preserved during rotation. Failure to delete an old copy retains the verified new copy
and reports a cleanup warning.

`app/scripts/update.py`: standard library only, Python 3.11 syntax (ci compiles it, the web update
worker and its lock module with a real Python 3.11 on one runner), run by the install's **base**
Python (the one `app/.venv/pyvenv.cfg` names), not the venv, so `uv sync` can replace venv files
on Windows. `tow update --ref <tag>` prints the exact command; on Windows
`app\scripts\deploy.ps1 -Ref <tag>` (`-HealthTimeoutSec` 90, `-CheckWaitMinutes`, `-KeepSnapshots`;
Windows PowerShell 5.1 or 7, ASCII, `-LiteralPath`; `powershell -ExecutionPolicy Bypass -File …`
where the execution policy refuses scripts) finds that Python (or any Python 3.11+ through
`py -3`) and runs it. uv, Python and uv's cache come from one resolver, `launcher_env`, the same as the launchers give: `runtime/bin/uv` before uv on
PATH, `UV_PYTHON_INSTALL_DIR`, `UV_PYTHON_BIN_DIR`, `UV_CACHE_DIR` under `<TOW>/runtime`,
`UV_PROJECT_ENVIRONMENT=app/.venv`, `UV_MANAGED_PYTHON=1` (so an install whose Python is still
outside `runtime/` gets it there on its first update, which needs the network; `tow setup`
before the update does the same).

1. One update at a time (`<TOW>/.update.lock`); refuse a development checkout and local edits of
   tracked files; `git fetch --tags --prune origin`; resolve the ref; refuse a target older than **v1.18.0** — the minimum rollback target
   of the supervised-service layout (older versions have no `tow run`) — and a target that cannot
   read `data/state.json`: its `STATE_SCHEMA_VERSION` (none: v1.18–v1.20, format 1) is lower than
   the file's `schema_version` (v1.22 reads format 1, v1.23 writes 2), or the file cannot be
   verified. Both before TOW stops.
2. Stop TOW: the stop request, and wait (it lets a running job finish); only if it does not stop
   within `--wait-minutes` is it stopped forcibly — the supervisor's process tree, and its web
   server and job from `status.json` (on Linux and macOS they have their own sessions), each only
   while its command line is still `-m tow` of this install. With autostart on Linux or macOS the
   OS manager stops it too (`systemctl --user stop tow.service`, `launchctl bootout
   gui/<uid>/io.tow`), so systemd or launchd do not start the old version again meanwhile. A web
   server a dead supervisor left is stopped the same way; then the port must be free.
3. Snapshot `data/` and `config.yaml` into `backup/update-<time>-before-<ref>` with a SHA-256
   manifest (`SNAPSHOT.json`). Never copied: `master.key`, `lan-auth.token`, `sessions.json`,
   `browser-auth/`, `keys/`, `run/`, `tmp/`, `logs/`, lock files.
4. `git checkout --detach <target>`, `uv sync --frozen --no-dev` with that environment.
5. Start: the autostart if it is on (`schtasks /Run /TN TOW`; `systemctl --user start tow.service`;
   `launchctl bootstrap gui/<uid> ~/Library/LaunchAgents/io.tow.plist` when the agent is not
   loaded, else `kickstart`), otherwise `tow run` detached in the background (pythonw, no
   console; outlives the updater).
6. Health: `/healthz` must report the **target version** and `/health.json` must answer (it reads
   the state), within `--health-timeout` (90 s); no proxies.
7. On failure every rollback step runs in its own `try` and is recorded: stop the new one, check
   out the previous code, `uv sync`, put data and config back from the snapshot **if the new
   version changed them** (what the snapshot leaves out is never touched — the failed version's
   logs stay, they say why it failed), start the previous version and check it reports the
   previous version. The outcome is `rolled_back` (previous version answers) or `failed` (it says
   so and how to start TOW by hand). If TOW was stopped and nothing started it, it is started
   again.
8. On success: prune old update snapshots — the newest 5 of `update-*-before-*` and deploy.ps1's
   `data-*-before-*`; night copies, key copies and anything `pre-runtime` are never touched. An
   install without git then gets the start files of the new version (below).

**An install without git** (the bundle and both installers; `app/.git` absent, 1.22): the
same steps, with the code from the release instead of git.

- `--ref` is a release tag or `latest` (resolved through GitHub's `/releases/latest`
  redirect); the target must be **v1.22.0 or newer** (an older `update.py` needs git, so it
  could not update this install again). `tow update --ref` says so for such an install.
  `latest` that is the installed version changes nothing (it says so, exit code 0), and one
  older than the installed version is refused; a release named by its tag installs again or
  goes back.
- Before TOW stops: the release's `SHA256SUMS` (required; without a matching source checksum
  nothing is changed), then GitHub's archive of the tag
  (`archive/refs/tags/<tag>.tar.gz`); if its SHA-256 differs from the `tow-source.tar.gz` line,
  the copy uploaded with the release is taken and checked instead; neither matching is a
  refusal. Downloads go through the system's proxy settings, with the system's certificates plus
  certifi's from `app/.venv`; at most 200 MiB. The archive is unpacked into
  `<TOW>/.update-download` (one top folder; absolute paths, `..` and links refused; at most
  10,000 entries, 256 MiB per entry and 1 GiB total unpacked) and its
  `pyproject.toml` must hold the tag's version; it becomes `<TOW>/app.new`. Any refusal here
  stops nothing and leaves nothing behind.
- Step 4 is a move, not a checkout: the old `app.prev` is removed, every entry of `app/`
  (its `.venv` too) is moved into `app.prev/`, every entry of `app.new/` into `app/` - entry
  by entry, so a terminal whose current folder is `app` does not block it - then `uv sync`
  builds a new `app/.venv` (online; the cache has the old wheels). A switch journal in
  `<TOW>/.update-switch.json` lets the next update restore the old code after a hard process
  interruption. While that record is there, does not say the switch is finished (`accepted`
  or `restored`) and no update holds `.update.lock`, `tow run`, `tow start` and the start
  files refuse to start TOW (`app/` may hold half of the new code) and say to run the update
  again; the start files read the record and the lock with PowerShell, or with grep and
  `flock` (`perl` where there is none), since the code in `app/` may not run. Ctrl+C, Ctrl+Break or a closed terminal do not cut the
  switch off: it finishes or rolls back first. The updater is copied to `<TOW>/runtime/update.py` before moving files so the
  launchers can run it even if `app/scripts` is temporarily absent. A rollback moves back the
  entries recorded on disk (the failed code goes to `app.failed/` and is removed), so the old
  `.venv` is in its place again and its absolute paths are right. One `app.prev` is kept after
  a success. An install with an older root launcher can run the base Python with
  `<TOW>/runtime/update.py --ref <tag>` to recover an interrupted switch.
- The Windows bundle's start files (`Start TOW.cmd`, `Stop TOW.cmd`, `Update TOW.cmd`, when
  `<TOW>` has any of them) are TOW's: an update writes them again from
  `app/scripts/root_files.py` - after the snapshot, before anything moves (from the code that
  runs the update, so a switch cut off later is undone with current files), and after a success
  from the new code - each one only when it differs, through a temporary file and a rename; then
  `runtime/update.py` from the new `app/scripts/update.py`. The log says which were written
  (`update-state.json`: `root_files`); a failure is said and does not fail the update. Changes
  made to them by hand are not kept. `cmd.exe` reads a running batch file again after each
  command from the byte where it stopped, so the new `Update TOW.cmd` (the one running this
  update) holds, exactly at the end of the old file's `update.py` line, a line that ends the
  old run with the update's exit code (`pause & exit /b`; a run of the new file passes over it,
  `TOW_FILE_RUN`). An `Update TOW.cmd` without that line (`--ref "%TOW_REF%"`) is not one TOW
  wrote and is left as it is. A version without `scripts/root_files.py` (going back before it)
  leaves the files as they are; they still start it.
- `--source FILE` (and `--sums FILE`) take a local archive instead of a download.

An update that finds such a record undoes the cut-off one first (stop TOW, move the recorded
entries back, put back the snapshot if data changed, start and check the previous version). The
record says until when TOW ran under the update (`data_until`: the switch, a health check and
the stop after it); data or config changed later is never replaced unasked: the run is refused
before anything changes and names the file and the command with `--discard-newer-data` that
puts the snapshot back anyway. Once the previous code and the data are back (a rollback, or
undoing a cut-off switch), the record first says so (`restored`) and is then removed, before the
previous version starts: what it writes afterwards is the owner's, and a later update never
takes it for the cut-off one's. If undoing fails, the run ends there with `recovery_failed`
(exit code 1, the record stays for the next try); TOW is started again only when nothing was
moved yet, never on half-moved code or half-restored data. A rollback that cannot put code and
data back completely leaves TOW stopped too and says how to finish (run the update again, or
put the snapshot back by hand in a git install). The record is written to disk (flushed) before
anything moves.

`<TOW>/update-state.json` records the run (`in_progress` → `ok` / `rolled_back` / `failed`, or
`recovered` / `recovery_failed` for undoing a cut-off switch, ref,
commits and versions, snapshot, rollback steps, `data_restored`, `service_running`). The watchdog
holds back for 30 minutes while it says `in_progress`. Texts come from the catalogs (section
`update`), English when the old checkout has none yet.

**The same on every OS** (`<TOW>` is the install, `<python>` its base Python — `tow update --ref
<tag>` prints it):

| what | Windows | Linux / macOS |
|---|---|---|
| update | `<TOW>\app\scripts\deploy.ps1 -Ref v1.21.0` | `<python> <TOW>/app/scripts/update.py --ref v1.21.0` |
| update an install without git | `<TOW>\Update TOW.cmd` (`latest`, or a tag) | Linux `<TOW>/update-tow`, macOS `<TOW>/Update TOW.command` (the same) |
| go back to an earlier version (≥ v1.18.0 that reads the data: ≥ v1.23.0 once v1.23 ran) | `deploy.ps1 -Ref v1.23.0` | `<python> <TOW>/app/scripts/update.py --ref v1.23.0` |
| restore a night copy | `tow.cmd restore-snapshot --path <copy>` (check), then `--apply` | `./tow restore-snapshot --path <copy>`, then `--apply` |
| put back an update snapshot by hand | `tow.cmd stop`; copy `<snapshot>\data\*` over `<TOW>\data\` and `<snapshot>\config.yaml` over `<TOW>\config.yaml` | `./tow stop`; `cp -a <snapshot>/data/. <TOW>/data/` and `cp <snapshot>/config.yaml <TOW>/` |
| Python and environment (after a move, a fresh clone) | `tow.cmd stop`, `tow.cmd setup` | `./tow stop`, `./tow setup` |
| stop, restart, start | `tow.cmd stop` / `restart` / `run` | `./tow stop` / `restart` / `run` |

`restore-snapshot` restores a night copy (`backup/night`, verified, signed with a key derived from
the master key) while TOW runs: it keeps a safety copy in `data/before-restore-<time>` and puts the
old files back by itself if anything fails; `tow run` reads a restored `interval_sec` from the
config, nothing else has to follow it. (1.17's `scripts/restore-snapshot.ps1`, which paused the
TOW-check task around it, is gone.) An update snapshot holds no keys: put it back only into the
install it came from.

Since 1.22.37, YAML settings are bounded before object construction, including aliases and merge keys:
16 MiB of UTF-8 input, 100,000 expanded nodes (keys included), 16,777,216 expanded text characters and
32 levels of nesting. Shared references count at each occurrence, and a subtree reused deeper is checked
at that depth. Cyclic, undefined or duplicate anchor names and multiple documents are refused. Ordinary
safe anchors, merge precedence and UTF-8 settings remain supported. The parsed/programmatic graph is
checked again before copying, comparison or serialization. These limits apply to YAML, not JSON history.
YAML 1.1 base-60 numbers are not read (as in YAML 1.2): an unquoted `3:30` stays text, so a long
`1:2:3:…` value cannot take quadratic time. A number or date that cannot be represented (beyond Python's
integer digit limit, an impossible date) is refused like the limits above, also when a legacy unsigned
night copy is checked.
Since 1.22.38, serialization is streamed into a bounded buffer using the same UTF-8 byte limit, including
the header and YAML escaping. A short scalar alias may expand during dumping, and Unicode characters
can occupy several bytes; neither can create a file larger than the reader permits. Save, night restore
and import overrides refuse before replacing files. Destination directories are not created on refusal.
An empty document or mapping is valid; `false`, `0`, an empty string or a list is not a settings mapping.
Since 1.22.39, portable archives accept UTF-8, UTF-16 and UTF-32 YAML with either byte order, with a BOM
or the ASCII/null-byte prefix defined by [YAML 1.2 §5.2](https://yaml.org/spec/1.2.2/#52-character-encodings).
Import checks the original archived bytes against the manifest before converting settings to UTF-8;
the source archive is not modified. Both the original and converted configuration are limited to 16 MiB,
before parsing, checkpoint creation or destination writes. Malformed Unicode is refused, never guessed
or replaced. Conversion preserves comments, quoting, anchors, line endings and BOM characters inside
quoted values; an existing UTF-8 file is preserved byte for byte, including its leading BOM. Export
normalizes only the archived configuration, not the live file. Rollback restores the exact original bytes,
even if the original live configuration was unreadable. Live settings themselves remain UTF-8.
Night-copy verification and preview check the configuration as well as its signature and checksums, before
restoration writes anything. A broken live configuration can still be replaced by a healthy copy; when its
local access settings cannot be read, the restored settings are local-only, and the copy's restore points go to
the restore points folder the copy's settings name (or the default one).

Since 1.22.41, night-copy `MANIFEST.json` is limited to 1 MiB before JSON decoding. New
descriptions are encoded into a bounded buffer before publication; an oversized description
does not publish a new copy or prune earlier ones. File sizes must be exact, nonnegative integers
within the signed 64-bit filesystem range: strings, booleans, fractions and non-finite numbers
are not coerced. Settings skips unreadable descriptions, invalid dates and sizes; inventory
is not proof of signature verification. The descriptor limit follows the
[Python JSON guidance](https://docs.python.org/3/library/json.html) on bounded untrusted input
and does not limit the size of JSON history or stream the full contents of a restore.

Since 1.22.42, night-copy verification, preview and restore also validate the exact verified
state and history bytes with their live-store parsers: finite numbers, bounded nesting,
supported state versions and readable containers. Both encrypted stores must have their
expected format and cipher, decrypt with the local master key, and contain a readable object;
settings undo must contain its secrets object. Validation never quarantines or rewrites source
files. A new copy that does not read back or is hash-correct but unusable is recorded as a failure,
removed (never listed or counted for retention) and cannot prune older copies. A new copy checks the live
stores before it takes the data lock and keeps the hash of the bytes that passed; under the lock a store copied
with that hash is proven, one that changed in between is checked from the copy, and the read-back compares
hashes only, so saves wait for the copy itself, not for parsing. Missing optional stores and valid legacy containers remain supported. This follows
the separation of cryptographic verification and archive consistency checks described in
[Borg's check documentation](https://borgbackup.readthedocs.io/en/stable/usage/check.html),
with application-format checks supplied by TOW's own readers.

The pre-construction check uses [PyYAML parsing events](https://pyyaml.org/wiki/PyYAMLDocumentation)
because [PyYAML 6.0.3 expands merge mappings during construction](https://github.com/yaml/pyyaml/blob/6.0.3/lib/yaml/constructor.py),
before schema validation. Resource bounds also follow the approach used by
[SnakeYAML loader options](https://github.com/snakeyaml/snakeyaml/blob/master/src/main/java/org/yaml/snakeyaml/LoaderOptions.java)
and [go-yaml's alias expansion guard](https://github.com/go-yaml/yaml/blob/v3/decode.go); TOW counts complete
expanded subtrees and text rather than only alias occurrences.

Night-copy and before-restore cleanup is best effort, separate from the verified copy or committed
restore. Only confirmed removals count as pruned. A held file, inaccessible directory or uncertain
read-back shows a cleanup warning; it does not turn a usable new copy or completed restore into a
failure. Ownership manifests/journals remain until the final directory step so interrupted cleanup
can be retried. Foreign members, links, junctions and unfinished restores are kept. A partial copy
(`.tow-<time>.partial`) that a crash, a power loss or the 30-minute job limit left behind is removed by the
next copy when it carries this install's signed proof; one without it (another install, an earlier version)
is kept. Settings shows
pending night cleanup even with the section collapsed; CLI output and history retain the warning.
The watchdog sends separate cleanup-pending and cleanup-complete messenger alerts once per state
change; a usable copy with pending cleanup remains healthy rather than becoming a failed backup.

The same confirmed-absence rule applies to `.towx` restore-point retention. Incomplete cleanup
does not cancel a verified new archive, restore, import or update: their results carry the warning
instead. `data/restore-point-status.json` is best-effort monitoring state, bound to the current archive
folder; it grants no deletion rights. Settings and the watchdog keep point cleanup separate from
night-copy health. A successful subsequent point creation retries retention and clears the warning
for that folder. Web-update jobs also retain a typed cleanup flag across worker restarts and show it
beside the actual update outcome; older jobs without this flag remain readable.

Verification: `tests/test_update.py` runs the real script against a throwaway git clone (git is
the only program the test guard lets through, marker `allow_git`) with uv, the web server,
autostart and processes faked: success with pruning, a target that does not answer (rollback with
data and config put back, the new version's log kept), a rollback step that fails, a previous
version that does not come back, local edits, a concurrent update, a supervisor that ignores the
stop (its web server and job stopped as well, the manager first; a reused pid left alone), a dead
supervisor's web server, a failed snapshot, a target older than v1.18.0, a five-task install, the
launchers' environment and Python 3.11 syntax.

## 5. Platform code in one package (built)

`tow/platform/{__init__,_common,windows,posix}.py` (API in
[architecture.md](architecture.md#code-api-paths-and-platform)):
- **windows.py** — boot time and time asleep (kernel32 `GetTickCount64` /
  `QueryUnbiasedInterruptTime`), sign-in time (wtsapi32), shutdown records (`wevtutil`, events
  1074/41/6008), the port's owner (PowerShell `Get-NetTCPConnection` + `Win32_Process`), hidden
  detached starts (`CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`, leaving the job with
  `CREATE_BREAKAWAY_FROM_JOB` when the job allows it), tree stop (`taskkill /T /F`, then waits),
  `process_alive` through `OpenProcess`/`GetExitCodeProcess` (never `os.kill`, which terminates on
  Windows), Edge then Chrome (Program Files, LocalAppData, PATH), the browser window to the front,
  protected folders: Windows, Program Files, ProgramData, AppData (from the environment, plus
  `C:\Windows`, `C:\Program Files…`, `C:\ProgramData` defaults so a Windows client is protected
  also when TOW runs elsewhere).
- **posix.py** (`PosixBackend("linux" | "macos")`) — boot time from `/proc/uptime` or
  `sysctl -n kern.boottime`; time asleep from two clocks (`CLOCK_BOOTTIME − CLOCK_MONOTONIC` on
  Linux, `CLOCK_MONOTONIC − CLOCK_UPTIME_RAW` on macOS); sign-in time `None`; shutdown records =
  the ends of earlier boots from `journalctl --list-boots -o json` (Linux, best effort; `[]` on
  macOS or without the journal), told as "the computer was shut down or restarted at …";
  `start_new_session` starts; `terminate` sends SIGTERM to the process group it leads (else the
  process), SIGKILL after the timeout; `lsof`/`ps` for the port's owner (since 1.21 on Linux
  without lsof `ss -Hltnp`, without both `/proc/net/tcp{,6}` and `/proc/<pid>/fd`; without ps
  `/proc/<pid>/{stat,cmdline}`); Chrome, Chromium, Edge on
  PATH and the macOS `/Applications` bundles; protected folders `/bin /boot /dev /etc /lib /lib64
  /proc /sbin /sys /usr /var/spool /var/db /var/root /System /Library /Applications /private/etc
  /private/var/root /private/var/db` and `~/.ssh ~/.config ~/Library`. **Not `/var` as a whole**
  (deviation from the first design): download folders live there
  (`/var/lib/transmission-daemon/downloads`, macOS temp folders under `/private/var/folders`).
- Browser sign-in on Linux and macOS (1.21, `browser_launch`): the browser starts with
  `HOME=<data>/browser-auth/<op>/home` (and `XDG_CONFIG_HOME`/`XDG_CACHE_HOME`/`XDG_DATA_HOME`
  under it), `--password-store=basic` and `--use-mock-keychain`, so `~/.pki/nssdb`, the
  fontconfig cache, a keyring or Keychain entry end up in the session folder and are removed with
  it. On Linux a TOW started without `DISPLAY`/`WAYLAND_DISPLAY` (a systemd user service) takes
  `DISPLAY`, `WAYLAND_DISPLAY`, `XAUTHORITY`, `XDG_RUNTIME_DIR` from `systemctl --user
  show-environment` (best effort, never overwriting), and `XAUTHORITY` stays `~/.Xauthority` of
  the real home. A snap Chromium (`/snap/...`, or Ubuntu's `chromium-browser` script) cannot use a
  profile outside the home folder: any other browser is preferred, and a snap alone is refused
  with `browser_auth.snap_browser`.
- Backup folders (`locations.py`): a path of another system (`D:\…`/`\\nas\…` on POSIX, `/mnt/…`
  on Windows) is refused instead of becoming a folder inside the install - in Settings and, since
  1.21, also when a carried config.yaml names one (`backup_root()` / `restore_points_dir()` raise
  with `locations.other_system_config`; nothing is created); the protected message
  names this system's folders. Pages show `scripts\tow.cmd` / `scripts/tow`, `data\` / `data/`
  and a folder example per system (template globals `os_launcher`, `os_sep`, `os_example_folder`).
- Callers moved onto it: `pulse.py` (machine facts, shutdown records, port owner, stop tree),
  `browser_auth.py` (browser lookup, own process group/session on every system, window, tree
  stop — before, Linux/macOS stopped only the main browser process), `folders.py` (protected
  folders of this system plus the other systems' defaults; client paths compared the way the
  client's system compares them; POSIX save roots; "seen from here" for POSIX paths). Since 1.21
  POSIX paths (remembered roots, system folders) ignore case on macOS only - exactly on Linux,
  where `/system` is a download folder - and any profile's `X:\Users\<name>\AppData` of a Windows
  client is protected on every system (`windows.is_profile_appdata`).
- 1.21: the five-task code is gone (`windows_task.py`, `scripts/tow-restart.py`, the task branches
  of `lifecycle.py`, `watchdog.py`, the CLI); `tow.supervisor._os` keeps only what the supervisor
  alone needs (the child interpreter, spawning an owned child with `popen_options`, the port
  probe) and takes `process_alive`, `terminate`, `port_owner`, `bind_children` and
  `die_with_parent` from `tow.platform`.
- File locks stay where they are (`store.py`, `log.py`): `msvcrt` on Windows, `fcntl` elsewhere.

## 6. Launchers (built)

- `scripts/tow-env.cmd` (shared by the Windows launchers `tow.cmd` and `tow-setup.cmd`): `TOW_APP`
  (the code), `TOW_ROOT` (discovery as in §1; one set before is kept only when it names the
  folder above `TOW_APP`, `scripts/tow` the same), `TOW_EXE`, `TOW_HOME`, `TOW_CONFIG`, the uv
  variables of §1 for a runtime install, `TOW_UV` (`runtime\bin\uv.exe` when present). A legacy
  `data\master.key` and `data\lan-auth.token` are no longer set here (since 1.21 `tow.store` and
  `tow.auth` find them, so `scripts/tow`, autostart and the task behave the same).
  No labels and no `exit /b` inside blocks (cmd.exe misreads labels in LF files and loses the
  exit code of `exit /b` without a number inside a block).
- Network sign-in (`tow.access`): the password record in `secrets.enc`. An external access key
  (`TOW_LAN_AUTH_TOKEN`, or a file named by `TOW_LAN_AUTH_TOKEN_FILE`, else `data/lan-auth.token`
  when that file exists; at least 32 characters) is an explicit
  alternative used **only while no password record exists**. A damaged record or unreadable
  secrets refuse every device on the network (the sign-in page says why) and never fall back to
  the key; the password is then set again on the computer running TOW. The key file stays out
  of update snapshots and night copies like the master key.
- Who may ask (1.28, `tow.web.middleware`): the web server ignores `X-Forwarded-For` /
  `X-Forwarded-Proto` (uvicorn `proxy_headers=False`, no `forwarded_allow_ips`), so a request is
  judged by its connection. A peer with a public address is refused whatever `Host` says. From
  this computer `Host` must be `localhost` or a loopback address (`127.0.0.1`, `[::1]`): any
  device on the network can answer LLMNR or mDNS for the computer's own name. From other devices
  it must be a non-public IP address, the computer's plain name, `<name>.local` or the name set as
  `bind`; anything else gets 403 "untrusted host". A write must state its size (a chunked body,
  or an upload without `Content-Length`: 411) and a form stays under 512 KiB (413); the `.towx`
  import and the `.torrent` preview are uploads with their own limits. The sign-in form takes only
  `application/x-www-form-urlencoded` (415). Wrong passwords lock a device out after 5 in 10 minutes (30 s, doubling with each
  further one up to an hour), and every device after 30 from all of them (up to 15 minutes).
- `scripts/tow.cmd <command>`: the venv's `tow.exe`, else (not set up yet)
  `uv run --frozen --no-dev --project app tow`; the exit code is TOW's. An environment that is
  not this folder's own (the start files' question: TOW's code and, in an install, the base
  Python inside `<TOW>`) is refused with "run setup" (exit code 3): a moved folder's points to
  the old place, and a copy's, while the original is still there, would run the original's code.
  `tow.cmd setup` → `scripts/tow-setup.cmd`: `uv python install --no-bin --no-registry` (the
  version of `.python-version`, into `runtime\python`), rebuilds a `.venv` whose Python no longer
  runs (moved folder) or lives outside `runtime\python` (an install from before 1.18) — renamed
  first, so with TOW still running nothing is removed and setup says "stop TOW" — then
  `uv sync --frozen --no-dev` and, last, `tow keys ensure` (a new install gets its master key and
  the note to keep a copy; one in use is left alone). In a checkout: `uv sync --frozen`. Before that it asks
  `layout.setup_check`: TOW of this install running (its lock, or its web server on the port)
  stops the setup; another program or another TOW folder on the port is only named with the port
  (`cli.setup.port_other`) and the setup goes on.
- `scripts/tow` (POSIX sh, mode 100755): the same for Linux and macOS (`./tow setup` without
  `--no-registry`), follows a symlink to itself (e.g. `~/bin/tow`).
- `tow start` (1.22, `tow.supervisor.starter`): when this install's `tow run` runs, only the
  page opens; otherwise `python -m tow run` (`pythonw.exe` on Windows) starts detached and
  hidden (`platform.spawn_detached`: it outlives the window or terminal; its early output goes
  to `data/logs/run-stderr.log`), and the page opens once `/healthz` answers as this install
  (`--wait`, 120 s; another TOW folder on the same port does not count).
  A `tow run` that ends before it answers (another program on the port, a broken config) is
  reported at once with the log - unless another `tow run` took over meanwhile.
  `--no-browser` or `TOW_NO_BROWSER=1` opens no browser; on Linux without `DISPLAY` /
  `WAYLAND_DISPLAY` none is opened either (webbrowser would run a text browser in the
  terminal) and the address is printed. `scripts/tow-start.cmd` and `scripts/tow-start` prepare
  the environment first (§1a).
- Verified by hand: cmd.exe on scratch layouts (a path with a space; legacy and adopted keys;
  development checkout; uv missing) and `sh` from Git Bash for `scripts/tow`.
- 1.21: the per-task launchers (`tow-serve.cmd`, `-check`, `-progress`, `-backup`, `-watchdog`),
  `tow-local.example.cmd` and `restore-snapshot.ps1` are gone; `tow-env.cmd` no longer calls
  `app\tow-local.cmd`.

## 7. Tests and CI

Platform backends are tested by injecting the backend (posix on Windows and vice versa) and an
isolated HOME for autostart files; the supervisor scheduler with a fake clock; `update.py` on a
throwaway clone.

Built so far (this workstream):
- `tests/test_platform.py`: the Windows backend with ctypes/subprocess faked and recorded
  `wevtutil`/PowerShell output; the POSIX backend's logic (`/proc/uptime`, `kern.boottime`,
  clocks, `journalctl` JSON, signals and process groups, `lsof`/`ps`, browsers, folders) for both
  `linux` and `macos` on any machine through module seams. `test_paths.py`, `test_master_key.py`,
  `test_folders.py` (every system refuses every system's folders), `test_launchers.py`.
- Tests no longer patch `os.name` (it turns every `Path` into a `WindowsPath` on POSIX); since
  1.21 no `_on_windows()` seam is left either (the five-task code had them). Truly Windows-only
  tests are skipped elsewhere with a reason (msvcrt byte locks, junctions, read-only deletion).
- The test guard also refuses `os.kill`/`os.killpg` (except multiprocessing children and, on
  Linux and macOS, signal 0 to the test process itself - the existence probe of `process_alive`),
  and moves `tempfile`/TMP and the umask back after every test; `TOW_ROOT` is the test's own
  folder, `HOME` a folder inside it and the `XDG_*_HOME` variables are unset, so autostart files
  never reach the runner's home.
- mypy is clean with `--platform linux` and `--platform darwin` too.
- CI (`.github/workflows/ci.yml`): the full gate runs on Windows, Ubuntu and macOS for every push
  and pull request, and all three must pass (green since 1.21.0); the dependency audit runs on one
  of them (uv.lock is the same everywhere).
- Installers (`.github/workflows/installers.yml`): on pull requests that touch `scripts/`,
  `install/`, the updater or the dependencies, weekly and by hand, the Windows bundle is built from
  the commit and tested as on a tag (below), and `scripts/update-smoke.py` updates the latest
  published release (the bundle on Windows, install.sh on Ubuntu and macOS) to the commit's source
  archive with the real updater (`--source`/`--sums`): a broken copy that changes `data/` and
  cannot start must be rolled back (code, data, version), then the update itself must answer as
  the new version with `data/`, the key and `config.yaml` kept; the same once more with the new updater,
  and once with the copy it leaves in `runtime/update.py`. Weekly and by hand it also measures
  all 45 large file-selection scenarios of `tests/selection_work_probe.py`
  (`TOW_SELECTION_WORK=all`); the gate measures the costliest of each kind.
- Real systems (`.github/workflows/real.yml`): on pull requests that touch the clients, autostart,
  the check or the supervisor, weekly and by hand. An Ubuntu runner installs qBittorrent,
  Transmission and Deluge from apt, starts each on its own port with a login of that run, and
  `tests/integration/test_real_clients.py` drives TOW's own adapters (built by the client factory
  from settings) on torrents whose data is already there: added stopped with a file selection,
  the selection and the "tow" mark read back, started, complete after a recheck, stopped, moved,
  an unmarked torrent adopted, each removed without its files; then `tow check --apply` against a
  local site, once more after the site publishes a new version. On Windows, macOS and Ubuntu
  `tests/integration/test_real_autostart.py` takes a runtime install of the commit (the bundle
  with `install.ps1`, `install.sh`), turns autostart on, finds the task, agent or unit with the
  OS's own tools, lets the OS start TOW (`schtasks /Run`, `launchctl kickstart`,
  `systemctl --user start`; lingering on for the runner's user), waits for `/healthz`, turns it
  off and confirms the entry is gone and TOW stopped; "without signing in" too. These tests carry
  `@pytest.mark.real_system("TOW_REAL_...")`: skipped unless that variable is 1, which only this
  workflow sets; then the test guard lets them through.
- Releases (`.github/workflows/release.yml`, 1.22): a `v*` tag (or a dispatch with a tag) must be
  an annotated `TOW X.Y.Z` on `origin/main` whose content passed `ci` - on its commit or on any
  commit with the same tree, such as the head of the merged pull request (the gate is not run
  again, and the run of the same content on `main` is not waited for);
  then GitHub's source archive of the tag (its version must be the tag's): Windows builds the bundle
  from it and runs `scripts/bundle-smoke.ps1 -Offline` (unpacked into a path with a space and
  Cyrillic letters, `Start TOW.cmd` with every proxy pointing at a closed port, `/healthz`,
  `tow status`, a second start, `Stop TOW.cmd`, uv's and Python's places outside the folder
  unchanged) and `scripts/install-smoke.ps1` (`install.ps1` on Windows PowerShell 5.1 with
  uninstall, a reinstall around the data and purge); Ubuntu and macOS run
  `scripts/install-smoke.sh` (install.sh from the archive, the start and stop files, uninstall,
  purge, nothing outside); the update test above runs against the tag. Only then the
  `publish` job (the only one with `contents: write`) creates the release as a draft if it is
  missing (notes from CHANGELOG.md; existing notes are never changed), uploads
  `TOW-windows-x64.zip`, `install.ps1`, `install.sh`, `tow-source.tar.gz` and `SHA256SUMS` with
  `gh`, downloads them again and checks them against `SHA256SUMS`, and only then publishes it -
  as the latest release only when it is the highest stable version (a fix to an older line is
  not what `releases/latest` gives). Every job after `source` checks out the commit `source`
  checked (its output), never the tag again, and the source archive must record that commit;
  uv's cache is off. The files of a public release are never replaced.
- `tests/test_starter.py` (`tow start` with a fake clock), `tests/test_update_archive.py`
  (update.py without git against a local web server that plays GitHub: checksums, the
  fallback to the release's copy, refusals, rollback of a cut-off switch, one `app.prev`),
  `tests/test_bundle.py` (the start files, the zip check) and `tests/test_installers.py`
  (POSIX sh, `sh -n`, the uninstall of install.sh on a stub, PowerShell parsing, the release
  workflow's order and permissions).

## 8. Upgrading from the five-task layout (≤1.17)

An install from 1.17 or earlier ran as five Windows tasks (TOW-serve, -check, -progress, -backup,
-watchdog) and kept its master key outside the folder (`TOW_MASTER_KEY_FILE`). To bring it to the
one-folder, single-supervisor layout: update to a 1.18–1.20 release with `deploy.ps1 -Ref <tag>`, move
the key into the install with `tow keys adopt` (check `tow keys status`), switch to one supervisor
with `tow autostart migrate` (a preview) and `tow autostart migrate --apply`, remove the
`TOW_MASTER_KEY_FILE` variable, and run `tow setup` to put Python inside the install. Then update to
the current release.

Since 1.21 the five-task layout is gone from TOW (`tow install-task`, the old Settings migration
action, the per-task launchers, `restore-snapshot.ps1`, `windows_task.py`; `tow autostart
migrate` only points to v1.20.0). Consequences:

- **Minimum rollback target: v1.18.0.** `deploy.ps1` / `update.py` go back to a version from
  v1.18.0 on (they all use `tow run`) only when it can read the data: once v1.23 ran,
  `data/state.json` has format 2 and nothing before v1.23.0 is accepted (§4, step 1). Anything
  else is refused before they stop TOW. Going back to the five tasks is therefore possible only
  for data no v1.23 or later wrote: deploy v1.20.0 first and follow that version's PORTABLE.md
  (`tow autostart off`, `tow install-task`, `schtasks /Run /TN TOW-serve`, then
  `deploy.ps1 -Ref v1.17.1`).
- **An install that still runs the five tasks** takes the steps above with v1.20.0 first: 1.21
  and later have no `tow-serve.cmd` for the TOW-serve task to start, so an update of such an
  install to them fails its health check and is rolled back.
- `app\tow-local.cmd` is no longer read by the launchers (discovery finds the same folders); a
  leftover file is ignored — delete it.
- If Python is not inside `runtime/` yet, run `tow stop` and `tow setup` before the first update
  to 1.21: the update uses the launchers' environment and would otherwise download Python into
  `runtime/python` itself.
