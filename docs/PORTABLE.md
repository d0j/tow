# Install and operations

TOW is portable and cross-platform: it writes only inside its own folder. A home program: a device
signed in with the password is the owner. The code API this rests on (`tow.paths`, `tow.platform`)
is described in [architecture.md](architecture.md#code-api-paths-and-platform).

## 1. One folder

```
<TOW>/                      the install root (movable, any drive, any OS)
  app/                      the code: a git clone at a release tag, or a release's source archive
  app.prev/                 an archive install's previous code, after an update (one is kept)
  Start TOW.cmd, ...        the start files of the bundle and the installers (§1a)
  config.yaml               live configuration
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
   recognised: when it equals the code folder of a runtime layout, the parent is used.
2. The code folder is named `app` and its parent has `config.yaml` or `data/` → that parent.
3. The code folder is a checkout (`pyproject.toml` + `src/tow`) → the checkout (development).
4. An installed wheel without a layout: the parent of `TOW_CONFIG`, else of `TOW_HOME`; else
   `RuntimeError("TOW_ROOT must be configured …")` — nothing is created in site-packages.

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
- `tow secrets generate-key [--key-file F]` writes `keys/master.key` by default (folder 0700 on
  POSIX; the file is created 0600 in the same call, `O_CREAT|O_EXCL`, never with the umask's mode
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
the owner's account only (child processes inherit it); Windows keeps the install folder's ACLs.

Python and caches (built, in the launchers and, since 1.21, `update.py`'s `launcher_env`): for a
runtime install `UV_PYTHON_INSTALL_DIR=<root>/runtime/python`,
`UV_PYTHON_BIN_DIR=<root>/runtime/bin`, `UV_CACHE_DIR=<root>/runtime/cache`,
`UV_PROJECT_ENVIRONMENT=<root>/app/.venv`, `UV_MANAGED_PYTHON=1`; a `runtime/bin/uv(.exe)` is
preferred over uv on PATH. A development checkout keeps the developer's uv setup unchanged.
`.venv` holds absolute paths: after moving the folder `tow setup` rebuilds it.

Prerequisites (1.22):
- the Windows bundle: nothing (uv, Python and every wheel are inside; the first start is
  offline). `install.ps1` / `install.sh`: the network once (GitHub; on Linux and macOS also
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
  `tow start` (§6). The first start waits for a key so the master-key note is read.
- **Installers** refuse a folder that holds an install (they name the update file) or anything
  else; a failed install leaves the folder as it found it (absent or empty). `--port` /
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

## 2. One process: `tow run` (supervisor)

`tow run` (package `tow.supervisor`) is TOW on every OS since 1.18: web server, schedule, night
copy and watchdog duties in one process. (The five Windows tasks of 1.17 — TOW-serve, -check,
-progress, -backup, -watchdog — are gone since 1.21; see §8.) It ticks once a second and never
blocks for long.

- **Single instance.** `data/run/run.lock` is held while it runs (the OS releases it when the
  process dies, so a crash never leaves a stale lock); `data/run/run.pid` says who holds it.
  A second `tow run` prints "already running" and exits 0 (an autostart that finds TOW running is
  not a failure to retry).
- **The port.** If something listens on the port, `tow run` first checks whether it is a web
  server this install's previous supervisor left behind (killed, crashed): the pid recorded in
  `data/run/status.json` (or, on Windows, the venv launcher's child of it) running
  `-m tow serve --log-file <this install>/data/logs/serve.log`. Only that is stopped, and TOW
  starts. Anything else on the port is never touched: `tow run` says so and exits 3 — or 0 when
  launchd started it (`TOW_AUTOSTART=launchd`), because launchd would retry it every minute
  without a limit; the reason is in `run.log`.
- **Started by the OS.** Task Scheduler, systemd and launchd start `python -m tow run` without
  the launcher's variables: the runtime layout (`<app>/..` holding `config.yaml` / `data/`, or
  `TOW_ROOT`) is resolved first and handed to every child (`TOW_HOME`, `TOW_CONFIG`, `TOW_ROOT`).
- **Web server.** `tow serve --log-file data/logs/serve.log --parent-pid <tow run>` runs as a child
  (stderr into `data/logs/serve-stderr.log`). `/healthz` is asked every 10 s with httpx
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
  process 15 s later if a request hangs).
- **Schedule** (`tow.supervisor.schedule`, tested with a fake clock):
  - check every `interval_sec` after the last *scheduled* one: the supervisor's own last start,
    kept in `data/run/schedule.json` (`check_started_at`), so a restart keeps the cadence;
    `health.auto_at_ts` only until there is one; never `health.at_ts`, which every manual check
    and progress pass moves (in 1.18–1.20 an install whose first check was manual got no
    scheduled check again). The first one two minutes after start. The child is
    `tow check --apply --notify --json` (`how="auto"`). The header countdown and Home's "checks are
    late" use the same real next check (`status.json` → `next.check` while `tow run` runs);
  - progress every 30 minutes: `tow check --apply --notify --progress-only --json`;
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
    secrets — never a restart (the supervisor owns the server). `tow watchdog` makes the same
    pass by hand as a diagnostic; it changes nothing either;
  - jobs run one at a time (a check also holds `check_run_lock` against a check from the web
    page), each with a time limit (check 1 h, progress 10 min, night copy 30 min); output goes to
    `data/logs/<job>-last.log`.
- **Sleep and wake.** A wall clock that jumped ahead of the monotonic one (or a loop that stood
  still for more than five minutes) means the machine slept: overdue jobs run at once, a server
  still starting gets a fresh grace, and the watchdog duty measures lateness from the wake.
- **Control without signals.** Other processes write `data/run/control/restart` or
  `data/run/control/stop` (JSON with who and an operation id); the supervisor polls them every
  second. `tow restart` and Settings → "Restart TOW" restart the web server (Settings follows the
  operation on `data/service-restart.json`: stopping → starting → ready/failed; a new server that
  exits three times before it answers is "failed"); `tow stop` and the updater stop TOW. A stop
  lets a running job finish (up to 10 minutes, then it is stopped), then stops the web server and
  exits 0. SIGTERM (systemd, launchd) and Ctrl+C stop it the same way with a 20-second job limit
  (all in all about 50 s: `SIGNAL_STOP_BUDGET_SEC`, which systemd's `TimeoutStopSec` and launchd's
  `ExitTimeOut` exceed).
- **Status and logs.** `data/run/status.json` is rewritten only when something changes (server
  state and pid, restarts, the running job, the next due times, the last wake) — never on a
  timer, so the disk can sleep. `data/logs/run.log` is rotated at 5 MiB × 3; the terminal gets the
  same lines only when stderr is one (not the journal or `launchd.log`).

Without autostart: `tow run` in a terminal (`<app>\scripts\tow.cmd run` / `<app>/scripts/tow run`).

## 3. Autostart (optional; the only write outside the folder; explicit owner action)

`tow autostart on|off|status [--without-login] [--json]` and Settings → TOW service. One backend
per OS (`tow.autostart`), every OS command through an injectable runner, every change read back
before it counts. A development checkout (code not in `<TOW>/app`) is refused, so the
one-per-user names never point at it. A registration that belongs to another TOW folder is never
replaced or removed — unless the program it runs no longer exists (the task's action, the unit's
`ExecStart`, the agent's `ProgramArguments[0]`): that install was moved or deleted, and its
registration is taken over (or turned off).

- **Windows:** one Task Scheduler task `TOW`, created from XML: action
  `<app>\.venv\Scripts\pythonw.exe -m tow run` (no console window), working directory `<TOW>`;
  logon trigger for the owner; IgnoreNew, no battery limits, no time limit, restart on failure
  (1 min × 999). "Start without signing in": principal `S4U` (the owner's account, no stored
  password) with a boot trigger plus the logon trigger; Windows may require an elevated prompt
  for it, and network shares are not reachable that way. Read back from `schtasks /Query /XML`.
- **Linux:** systemd user unit `~/.config/systemd/user/tow.service` (`$XDG_CONFIG_HOME` honoured):
  `ExecStart="<app>/.venv/bin/tow" run`, `WorkingDirectory=<TOW>`, `Environment="TOW_ROOT=<TOW>"`,
  `Restart=on-failure`, `RestartPreventExitStatus=3`, `KillMode=mixed` (SIGTERM to `tow run` alone;
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
  a TOW launchd started stops with it (the owner is told; `tow run` starts it by hand). Without
  signing in would need a system LaunchDaemon (administrator): not offered.
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
4. `tow autostart on` (and `tow run`, or let the autostart start it).

## 4. Update: `tow update --ref <tag>` (`scripts/update.py`)

### Web updates (1.22.20)

Every page shows the installed version. A new stable-release badge opens Settings → Service → Version and
updates, without a modal or an automatic installation. The shared release cache checks at most every 12 hours
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

Version selection accepts only published stable releases **from 1.22.20**, which retain these web controls.
The previous compatible release has a shortcut. Other versions can be entered explicitly; a target whose
declared state schema is older than the current data is refused before stopping TOW. This is not a promise of
safe downgrade to every historical release: older versions use the terminal, and future incompatible migrations
need restoration of matching data as well as code. Interrupted jobs are not silently overwritten; inspect the
local log, recover through the terminal and verify health before retrying.
An updater record confirming a later successful terminal recovery of the running version releases the stale
web-job reservation; reading status never erases the previous job or changes data.

Windows requires the worker to escape the supervisor's kill-on-close job; refusal never falls back to a child
that will die with it. Linux/macOS installs started normally can detach into their own session. Web updates of
systemd/launchd-managed installations are currently refused with terminal instructions: a new session alone
does not guarantee survival when the service manager stops the process group or cgroup. No autostart or network
access settings are changed by enabling these controls.

Release discovery uses fixed HTTPS endpoints of `d0j/tow` with public-IP-pinned requests, strict version checks,
timeouts and response limits. Archive installs verify SHA256SUMS; git installs use the configured origin.
These establish integrity and transport/repository trust, **not an independent publisher signature**. Keep
the configured origin trusted; installing from an arbitrary URL or branch is not a web option.

Export without overwrite atomically publishes prepared bytes (Windows rename; POSIX hard link) and refuses an
occupied name even if another writer creates it during preparation. POSIX destinations without hard-link support
fail safely rather than using a replacement fallback; choose a supported local filesystem. Unreadable/foreign
restore archives are preserved during rotation. Failure to delete an old copy retains the verified new copy
and reports a cleanup warning.

`app/scripts/update.py`: standard library only, Python 3.11 syntax, run by the install's **base**
Python (the one `app/.venv/pyvenv.cfg` names), not the venv, so `uv sync` can replace venv files
on Windows. `tow update --ref <tag>` prints the exact command; on Windows
`app\scripts\deploy.ps1 -Ref <tag>` (`-HealthTimeoutSec` 90, `-CheckWaitMinutes`, `-KeepSnapshots`)
finds that Python (or any Python 3.11+ through `py -3`) and runs it. uv, Python and uv's cache come
from one resolver, `launcher_env`, the same as the launchers give: `runtime/bin/uv` before uv on
PATH, `UV_PYTHON_INSTALL_DIR`, `UV_PYTHON_BIN_DIR`, `UV_CACHE_DIR` under `<TOW>/runtime`,
`UV_PROJECT_ENVIRONMENT=app/.venv`, `UV_MANAGED_PYTHON=1` (so an install whose Python is still
outside `runtime/` gets it there on its first update, which needs the network; `tow setup`
before the update does the same).

1. One update at a time (`<TOW>/.update.lock`); refuse a development checkout, local edits of
   tracked files and an install that still runs the five Windows tasks of 1.17 (update it to
   v1.20.0 and run `tow autostart migrate --apply` there first); `git fetch --tags --prune
   origin`; resolve the ref; refuse a target older than **v1.18.0** — the minimum rollback target
   of the one-process layout (older versions have no `tow run`).
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
   `data-*-before-*`; night copies, key copies and anything `pre-runtime` are never touched.

**An install without git** (the bundle and both installers; `app/.git` absent, 1.22): the
same steps, with the code from the release instead of git.

- `--ref` is a release tag or `latest` (resolved through GitHub's `/releases/latest`
  redirect); the target must be **v1.22.0 or newer** (an older `update.py` needs git, so it
  could not update this install again). `tow update --ref` says so for such an install.
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
  interruption. The updater is copied to `<TOW>/runtime/update.py` before moving files so the
  launchers can run it even if `app/scripts` is temporarily absent. A rollback moves back the
  entries recorded on disk (the failed code goes to `app.failed/` and is removed), so the old
  `.venv` is in its place again and its absolute paths are right. One `app.prev` is kept after
  a success. An install with an older root launcher can run the base Python with
  `<TOW>/runtime/update.py --ref <tag>` to recover an interrupted switch.
- `--source FILE` (and `--sums FILE`) take a local archive instead of a download.

`<TOW>/update-state.json` records the run (`in_progress` → `ok` / `rolled_back` / `failed`, ref,
commits and versions, snapshot, rollback steps, `data_restored`, `service_running`). The watchdog
holds back for 30 minutes while it says `in_progress`. Texts come from the catalogs (section
`update`), English when the old checkout has none yet.

**The same on every OS** (`<TOW>` is the install, `<python>` its base Python — `tow update --ref
<tag>` prints it):

| what | Windows | Linux / macOS |
|---|---|---|
| update | `<TOW>\app\scripts\deploy.ps1 -Ref v1.21.0` | `<python> <TOW>/app/scripts/update.py --ref v1.21.0` |
| update an install without git | `<TOW>\Update TOW.cmd` (`latest`, or a tag) | `<TOW>/update-tow` · `Update TOW.command` |
| go back to an earlier version (≥ v1.18.0) | `deploy.ps1 -Ref v1.20.0` | `<python> <TOW>/app/scripts/update.py --ref v1.20.0` |
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
  (the code), `TOW_ROOT` (discovery as in §1), `TOW_EXE`, `TOW_HOME`, `TOW_CONFIG`, the uv
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
- `scripts/tow.cmd <command>`: the venv's `tow.exe`, else (not set up yet)
  `uv run --frozen --no-dev --project app tow`; the exit code is TOW's.
  `tow.cmd setup` → `scripts/tow-setup.cmd`: `uv python install --no-bin --no-registry` (the
  version of `.python-version`, into `runtime\python`), rebuilds a `.venv` whose Python no longer
  runs (moved folder) or lives outside `runtime\python` (an install from before 1.18) — renamed
  first, so with TOW still running nothing is removed and setup says "stop TOW" — then
  `uv sync --frozen --no-dev`. In a checkout: `uv sync --frozen`.
- `scripts/tow` (POSIX sh, mode 100755): the same for Linux and macOS (`./tow setup` without
  `--no-registry`), follows a symlink to itself (e.g. `~/bin/tow`).
- `tow start` (1.22, `tow.supervisor.starter`): when this install's `tow run` runs, only the
  page opens; otherwise `python -m tow run` (`pythonw.exe` on Windows) starts detached and
  hidden (`platform.spawn_detached`: it outlives the window or terminal; its early output goes
  to `data/logs/run-stderr.log`), and the page opens once `/healthz` answers (`--wait`, 120 s).
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
  and pull request, and all three must pass (green since 1.21.0).
- Releases (`.github/workflows/release.yml`, 1.22): a `v*` tag (or a dispatch with a tag) takes
  GitHub's source archive of the tag (its version must be the tag's); Windows builds the bundle
  from it and runs `scripts/bundle-smoke.ps1 -Offline` (unpacked into a path with a space and
  Cyrillic letters, `Start TOW.cmd` with every proxy pointing at a closed port, `/healthz`,
  `tow status`, a second start, `Stop TOW.cmd`, uv's and Python's places outside the folder
  unchanged) and `install.ps1` on Windows PowerShell 5.1 with uninstall and purge; Ubuntu and
  macOS run `scripts/install-smoke.sh` (install.sh from the archive, the start and stop files,
  uninstall, purge, nothing outside). Only then the `publish` job (the only one with
  `contents: write`) creates the release if it is missing (notes from CHANGELOG.md; existing
  notes are never changed) and uploads `TOW-windows-x64.zip`, `install.ps1`, `install.sh`,
  `tow-source.tar.gz` and `SHA256SUMS` with `gh`.
- `tests/test_starter.py` (`tow start` with a fake clock), `tests/test_update_archive.py`
  (update.py without git against a local web server that plays GitHub: checksums, the
  fallback to the release's copy, refusals, rollback of a cut-off switch, one `app.prev`),
  `tests/test_bundle.py` (the start files, the zip check) and `tests/test_installers.py`
  (POSIX sh, `sh -n`, the uninstall of install.sh on a stub, PowerShell parsing, the release
  workflow's order and permissions).

## 8. Upgrading from the five-task layout (≤1.17)

An install from 1.17 or earlier ran as five Windows tasks (TOW-serve, -check, -progress, -backup,
-watchdog) and kept its master key outside the folder (`TOW_MASTER_KEY_FILE`). To bring it to the
one-folder, one-process layout: update to a 1.18–1.20 release with `deploy.ps1 -Ref <tag>`, move
the key into the install with `tow keys adopt` (check `tow keys status`), switch to one process
with `tow autostart migrate` (a preview) and `tow autostart migrate --apply`, remove the
`TOW_MASTER_KEY_FILE` variable, and run `tow setup` to put Python inside the install. Then update to
the current release.

Since 1.21 the five-task layout is gone from TOW (`tow install-task`, Settings → "Switch to one
process", the per-task launchers, `restore-snapshot.ps1`, `windows_task.py`; `tow autostart
migrate` only points to v1.20.0). Consequences:

- **Minimum rollback target: v1.18.0.** `deploy.ps1` / `update.py` go back to any version from
  v1.18.0 on (they all run as one process) and refuse anything older before they stop TOW. To go
  back to the five tasks, deploy v1.20.0 first and follow that version's PORTABLE.md
  (`tow autostart off`, `tow install-task`, `schtasks /Run /TN TOW-serve`, then
  `deploy.ps1 -Ref v1.17.1`).
- **An install that still runs the five tasks** cannot update to 1.21 or later directly:
  `update.py` refuses it (`schtasks /Query /TN TOW-serve` names this install's `tow-serve.cmd`).
  Take the steps above with v1.20.0 first.
- `app\tow-local.cmd` is no longer read by the launchers (discovery finds the same folders); a
  leftover file is ignored — delete it.
- If Python is not inside `runtime/` yet, run `tow stop` and `tow setup` before the first update
  to 1.21: the update uses the launchers' environment and would otherwise download Python into
  `runtime/python` itself.
