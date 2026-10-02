# TOW

**Torrent topic watcher:** follows forum tracker topics that replace their torrent in place and hands each new
revision to your torrent client.

[![CI](https://github.com/d0j/tow/actions/workflows/ci.yml/badge.svg)](https://github.com/d0j/tow/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)](.python-version)

[Русская версия](README.ru.md)

Many trackers keep one topic per show and swap the `.torrent` in it whenever an episode is added. TOW keeps
watching those topics, notices the new revision, adds it to qBittorrent, Transmission or Deluge with exactly the
files you chose, confirms the result by reading it back from the client, and tells you in Telegram, Discord,
WhatsApp or ntfy.

<!-- Screenshot placeholder: Home with a few synthetic topics (green, blue, amber, red dots). Use made-up titles
     and paths only; never a real install. -->
The web page is a single list: one row per topic, a status dot, the site, the folder, the latest event and the
download progress.

## Who it is for

- You follow series on sites like rutor, kinozal, RuTracker or NNM-Club, where a topic is updated in place.
- You run a torrent client with a Web UI on a home computer or a small server.
- You want new episodes added by themselves, with the same folder and file selection every time.

**TOW is not:**

- a downloader — it never fetches media; your torrent client does;
- a search engine or an RSS reader — you give it topic links;
- a way around site rules — no captcha or Cloudflare bypass, no shared accounts.

## Features

- Watch a topic, or add it once. Pick all files, episodes (`S01E03-E05`, `04x01-03`) or file patterns (`*.mkv`).
- Every add is transactional: added stopped, files selected, selection read back, then started. A failure is
  reported as a failure, never as success.
- Several mirrors per site, with automatic fallback and per-mirror cooldown; daily download limits respected.
- Site sign-in by password or, where a site needs it, through a browser window.
- Notifications grouped per check, quiet hours, a daily digest, a queue that survives restarts.
- Status colours that separate site trouble (amber) from your action being needed (red).
- History of every file and revision; one-step undo of the last change.
- Night copies, restore points before risky changes, encrypted transfer files (`.towx`).
- One folder holds everything: code, config, data, key, backups, its own Python. Move it and it still works.
- English and Russian interface; a new language is one JSON file.
- Import from Monitorrent.

## Supported

| Area | Supported |
|---|---|
| Sites (presets) | rutor, Kinozal, NNM-Club, RuTracker, Tapochek, UnionPeer, fast-torrent; any similar site configured by hand |
| Torrent clients | qBittorrent (Web UI), Transmission 3.0+, Deluge 2.x |
| Notifications | Telegram, Discord, WhatsApp (CallMeBot), ntfy |
| Operating systems | Windows 10/11; Linux and macOS — experimental until CI is green there |
| Runtime | Python 3.14 (installed by `setup` inside the TOW folder), [uv](https://docs.astral.sh/uv/) ≥ 0.12, git |

## Quick start

You need [git](https://git-scm.com/) and [uv](https://docs.astral.sh/uv/getting-started/installation/). TOW lives
in one folder (below `~/TOW`); the code goes into its `app` subfolder.

**Windows** (PowerShell):

```powershell
git clone --branch v1.21.0 https://github.com/d0j/tow "$HOME\TOW\app"
cd "$HOME\TOW\app"
Copy-Item config.example.yaml ..\config.yaml
.\scripts\tow.cmd setup
.\scripts\tow.cmd run
```

**Linux / macOS** (experimental):

```sh
git clone --branch v1.21.0 https://github.com/d0j/tow ~/TOW/app
cd ~/TOW/app
cp config.example.yaml ../config.yaml
./scripts/tow setup
./scripts/tow run
```

Open <http://127.0.0.1:8787>. `setup` installs Python and the dependencies inside `TOW/runtime`; the first run
creates the master key `TOW/keys/master.key` and says once to back it up.

> [!IMPORTANT]
> **Back up `keys/master.key` now** — copy it to a USB stick or a password manager. It encrypts every saved
> password and token. Night copies and `.towx` files never contain it, and without it they cannot be restored.

## First five minutes

1. **Welcome page.** Set a password for other devices, or skip: on this computer TOW opens without one.
2. **Settings → Torrent clients.** Turn on the Web UI in your client, enter address, port, login and password,
   press **Check**.
3. **Settings → Notifications** (optional). Follow “How to connect” for a messenger, press **Check**.
4. **Home → +.** Paste a topic link, keep or change the name and folder, choose what to download and whether to
   keep watching, press **Add**.
5. **Watch the dot.** Green: added and confirmed. Blue: something new. Amber: the site is down for now. Red: open
   the row — the reason is there.

The [user guide](docs/guide.md) explains every screen, status and message.

## Run it always

```sh
tow autostart on       # start with the computer: Task Scheduler (Windows), systemd user unit (Linux), LaunchAgent (macOS)
tow autostart status
tow autostart off
```

Here and below `tow` means the launcher: `.\scripts\tow.cmd` on Windows, `./scripts/tow` on Linux and macOS, run
from `TOW/app`. Autostart is the only thing TOW writes outside its folder, and only when you ask.
`--without-login` starts it at boot, before anyone signs in. Stop and restart: `tow stop`, `tow restart`.

## Headless server

TOW listens on `127.0.0.1:8787` only. To reach it from another machine, pick one:

| Way | How | Password |
|---|---|---|
| SSH tunnel | `ssh -L 8787:127.0.0.1:8787 user@server`, then open <http://127.0.0.1:8787> | not asked: the tunnel arrives as local |
| Home network or VPN | `tow access on` (asks for a password if none is set), then `tow restart`; open `http://<server>:8787` | always |

TOW refuses requests whose peer is a public internet address, so a router port forward does not work; use a
VPN (WireGuard, Tailscale) or the tunnel. `tow access off` closes the network again, `tow access status` shows
what is set.

> [!WARNING]
> **Reverse proxies.** A proxy on the same machine forwards every request from `127.0.0.1`, so TOW sees it as
> local and asks no password. Do not put TOW behind a proxy; if you must, the proxy has to authenticate every
> request itself.

## Command line

`tow --help` lists every command; `tow <command> --help` explains its options.

```sh
tow status            # one line: running, torrent client, sites, topics, last and next check, autostart
tow doctor            # ask the torrent client and the sites now
tow check --apply     # check every topic now
tow keys ensure       # create keys/master.key on a new install (tow run does it on first start)
```

| Exit code | Meaning |
|---|---|
| 0 | done |
| 1 | wrong command or option |
| 2 | done in part: a topic, a mirror or a step failed |
| 3 | cannot run: config, secrets, the lock or a file is missing or broken |
| 130 | interrupted |

With `--json` a command prints JSON, also when it fails.

## Backups and the master key

| What | Where | Contains the key? |
|---|---|---|
| Night copy (03:30, last 14) | `TOW/backup/night/` | no |
| Restore point (Settings → Backups, last 10) | `TOW/data/restore-points/` | no |
| TOW file `.towx` (Settings → Backups) | where you save it | no — restoring needs the same `master.key` |
| Pre-update snapshot (last 5) | `TOW/backup/update-…` | no |
| **Master key** | `TOW/keys/master.key` | **is the key** — keep an offline copy |

To move TOW to another computer, copy the whole `TOW` folder (with `keys/`) and run `tow setup` there. To move only
the data between installs with different keys, use `tow export` and `tow import`, which ask for a passphrase of
their own. A lost key cannot be recreated: if TOW says the key is missing, bring the saved copy back with
`tow keys adopt --from FILE`.

## Update and rollback

```powershell
.\scripts\deploy.ps1 -Ref v1.21.1       # Windows, PowerShell 7
```

On any OS `tow update --ref v1.21.1` prints the exact command for this install. The update stops TOW, snapshots
`data/` and `config.yaml`, switches the code, rebuilds the environment, starts TOW and checks it reports the new
version. If anything fails, it rolls back code and data step by step (the failed version's logs stay). To go back
later, update to the previous tag — v1.18.0 at the oldest. Details: [docs/PORTABLE.md](docs/PORTABLE.md).

`<python>` is the install's base Python (the command `tow update` prints names it):

| What | Windows | Linux / macOS |
|---|---|---|
| Update | `.\scripts\deploy.ps1 -Ref v1.21.1` | `<python> scripts/update.py --ref v1.21.1` |
| Go back to an earlier version (≥ v1.18.0) | `.\scripts\deploy.ps1 -Ref v1.20.0` | `<python> scripts/update.py --ref v1.20.0` |
| Restore a night copy | `tow restore-snapshot --path <copy>` to check, then add `--apply` | the same |
| Put back an update snapshot by hand | `tow stop`, then copy `<snapshot>\data\*` over `TOW\data\` and `<snapshot>\config.yaml` over `TOW\config.yaml` | `tow stop`; `cp -a <snapshot>/data/. TOW/data/`; `cp <snapshot>/config.yaml TOW/` |
| Python and environment (after a move or a fresh clone) | `tow stop`, `tow setup` | `tow stop`, `tow setup` |

To move the install to another folder or drive: `tow autostart off`, `tow stop`, move the whole `TOW` folder,
`tow setup` in the new place, `tow autostart on`. (An autostart left pointing at a folder that no longer exists is
taken over by `tow autostart on`.)

## Migrating from Monitorrent

```sh
tow import-monitorrent --db path/to/monitorrent.db           # preview: what would be imported
tow import-monitorrent --db path/to/monitorrent.db --apply   # import topics and logins
```

A restore point is made first. Duplicates (same id or link) are skipped; credentials only fill empty slots.

## Documentation

| Document | For |
|---|---|
| [User guide](docs/guide.md) | concepts, statuses, notifications, backups, troubleshooting |
| [Install and operations](docs/PORTABLE.md) | the folder layout, `tow run`, autostart, update internals |
| [Architecture](docs/architecture.md) | processes, modules, data, recovery, security model |
| [Extending](docs/EXTENDING.md) | adding a torrent client, messenger, site, language or page |
| [Roadmap](docs/ROADMAP.md) | what is done, what is next |
| [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Changelog](CHANGELOG.md) | |

## Legal

TOW is not affiliated with any tracker, torrent client or messenger. It hosts, indexes and downloads no content:
it reads pages you point it to and passes torrent files to your own client. You are responsible for following the
rules of the sites you use and the law where you live.

## License

[MIT](LICENSE) © 2026 d0j. Third-party notices: [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
