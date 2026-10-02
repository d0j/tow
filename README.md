<div align="center">

# TOW

**Torrent topic watcher.** Follows tracker topics that replace their `.torrent` in place and adds every new
revision to your torrent client.

[![CI](https://github.com/d0j/tow/actions/workflows/ci.yml/badge.svg)](https://github.com/d0j/tow/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/d0j/tow)](https://github.com/d0j/tow/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python 3.14](https://img.shields.io/badge/python-3.14-3776AB)](.python-version)
[![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-555)](#install)

**English** · [Русский](README.ru.md)

[Install](#install) · [Usage](#usage) · [Remote access](#remote-access) · [Update and backups](#update-and-backups) ·
[Docs](#documentation) · [Changelog](CHANGELOG.md)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/home-en-dark.png">
  <img alt="TOW home page: watched topics with status, site, folder, latest event and progress" src="docs/images/home-en-light.png" width="900">
</picture>

</div>

## Features

- **Watch or add once.** All files, chosen episodes (`S01E03-E05`, `04x01-03`) or patterns (`*.mkv`).
- **Confirmed adds.** The torrent is added stopped, files are selected, the selection is read back, then it starts.
  A failure is reported as a failure.
- **Mirrors.** Several addresses per site with fallback and cooldown; daily download limits respected; sign-in by
  password or a browser window.
- **Notifications.** One message per topic per check, quiet hours, a daily digest, a queue that survives restarts.
- **History and undo.** Every file and revision is recorded; the last change can be undone.
- **Backups.** Signed night copies, restore points, encrypted `.towx` transfer files.
- **One folder.** Code, settings, data, key, backups and its own Python. Move it and it keeps working.
- **Interface** in English and Russian; a new language is one JSON file. Import from Monitorrent.

## Supported

| | |
|---|---|
| **Sites** | rutor, Kinozal, NNM-Club, RuTracker, Tapochek, UnionPeer, fast-torrent; others by hand |
| **Torrent clients** | qBittorrent (Web UI), Transmission 3.0+, Deluge 2.x |
| **Notifications** | Telegram, Discord, WhatsApp (CallMeBot), ntfy |
| **Systems** | Windows 10/11, Linux, macOS (Apple silicon) — all three in CI |

## Install

You need [git](https://git-scm.com/downloads) and [uv](https://docs.astral.sh/uv/) 0.12 or newer. `setup` installs
Python 3.14 and the dependencies inside the TOW folder; nothing else is installed.

<details open>
<summary><b>Windows</b> — PowerShell</summary>

```powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
# open a new terminal, then:
git clone https://github.com/d0j/tow "$HOME\TOW\app"
cd "$HOME\TOW\app"
Copy-Item config.example.yaml ..\config.yaml
.\scripts\tow.cmd setup
.\scripts\tow.cmd run
```

Start with Windows (one task, no console window): `.\scripts\tow.cmd autostart on`.

</details>

<details>
<summary><b>Linux</b></summary>

```sh
sudo apt install git          # or: dnf install git / pacman -S git
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/d0j/tow ~/TOW/app
cd ~/TOW/app
cp config.example.yaml ../config.yaml
./scripts/tow setup
./scripts/tow run
```

Start at login (systemd user unit): `./scripts/tow autostart on`. Start at boot without a login:
`./scripts/tow autostart on --without-login`, then `sudo loginctl enable-linger $USER`.

</details>

<details>
<summary><b>macOS</b></summary>

```sh
brew install git uv           # or: xcode-select --install, and the uv installer above
git clone https://github.com/d0j/tow ~/TOW/app
cd ~/TOW/app
cp config.example.yaml ../config.yaml
./scripts/tow setup
./scripts/tow run
```

Start at login (LaunchAgent): `./scripts/tow autostart on`. Keep the TOW folder and download folders out of
`~/Documents`, `~/Desktop` and `~/Downloads`: macOS blocks background access there. Intel Macs need a Rust toolchain
for one dependency.

</details>

Open **<http://127.0.0.1:8787>**.

> [!IMPORTANT]
> The first start creates `TOW/keys/master.key`. It encrypts every saved password and token and is not part of any
> backup. **Copy it somewhere safe now** — without it, saved logins cannot be restored.

## Usage

1. **Settings → Torrent clients.** Turn on the Web UI in your client; enter address, port, login, password; **Check**.
2. **Settings → Notifications** (optional). Follow *How to connect* for a messenger; **Check**.
3. **Home → +.** Paste a topic link, choose the folder and what to download, **Add**.
4. **Read the dot.** Green — confirmed. Blue — something new. Amber — the site is unreachable for now. Red — open
   the row for the reason.

The [user guide](docs/guide.md) covers every screen, status and message.

**Command line.** Below, `tow` is the launcher in `TOW/app`: `.\scripts\tow.cmd` on Windows, `./scripts/tow` on
Linux and macOS. `tow --help` lists all commands.

| Command | Does |
|---|---|
| `tow run` | web UI, schedule, night copy, watchdog — in one process |
| `tow status` | one line: running, client, sites, topics, last and next check (`--json`) |
| `tow stop` · `tow restart` | stop TOW · restart its web server |
| `tow autostart on\|off\|status` | start with the system |
| `tow check --apply` | check every topic now |
| `tow doctor` | ask the torrent client and the sites now |
| `tow import-monitorrent --db FILE` | preview an import from Monitorrent; add `--apply` to import |

Exit codes: `0` done · `1` wrong command or option · `2` done in part · `3` cannot run · `130` interrupted.

## Remote access

TOW listens on `127.0.0.1:8787`. On the computer itself it asks no password.

| From | How |
|---|---|
| Phone or laptop at home, or over a VPN (WireGuard, Tailscale) | `tow access on` — asks for a password if none is set — then `tow restart`; open `http://<computer>:8787` |
| Anywhere over SSH | `ssh -L 8787:127.0.0.1:8787 user@server`, then open <http://127.0.0.1:8787> |

Requests from public internet addresses are refused, so a router port forward does not work. Do not put TOW behind a
reverse proxy on the same machine: the proxy's requests look local and get no password prompt. `tow access off`
closes network access.

## Update and backups

| | Windows | Linux · macOS |
|---|---|---|
| Update to a release | `.\scripts\deploy.ps1 -Ref v1.21.1` | `tow update --ref v1.21.1` prints the command |
| Go back (v1.18.0 or newer) | `.\scripts\deploy.ps1 -Ref v1.21.0` | the same with the older tag |
| Restore a night copy | `tow restore-snapshot --path <copy> --apply` | the same |
| After moving the folder | `tow stop`, `tow setup`, `tow autostart on` | the same |

An update stops TOW, snapshots `data/` and `config.yaml`, switches the code, starts TOW and checks the version. If
anything fails it rolls back by itself.

| Backup | Where | Notes |
|---|---|---|
| Night copy | `TOW/backup/night/` | daily at 03:30, last 14 |
| Restore point | `TOW/data/restore-points/` | before risky changes, last 10 |
| `.towx` file | where you save it | Settings → Backups; restoring needs the same `master.key` |
| `tow export` / `tow import` | where you save it | protected by its own passphrase; works across installs |

## Documentation

| | |
|---|---|
| [User guide](docs/guide.md) | screens, statuses, notifications, backups, troubleshooting |
| [Install and operations](docs/PORTABLE.md) | folder layout, `tow run`, autostart, updates |
| [Architecture](docs/architecture.md) | processes, modules, data, recovery, security |
| [Extending](docs/EXTENDING.md) | add a torrent client, messenger, site, language or page |
| [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Roadmap](docs/ROADMAP.md) | |

## License

[MIT](LICENSE) © 2026 d0j · [Third-party notices](THIRD-PARTY-NOTICES.md)

TOW is not affiliated with any tracker. It hosts and downloads no content: it reads pages you point it to and hands
torrent files to your own client. Follow the rules of the sites you use and the law where you live.
