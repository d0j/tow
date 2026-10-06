# Roadmap

What TOW does today, what comes next, and what it will not do. Release details: [CHANGELOG.md](../CHANGELOG.md).

## Product contract

These hold in every release:

1. TOW observes trackers, torrent clients and the filesystem; it never downloads, copies or moves media itself.
2. A normal check may add a torrent to the selected client and, when you change a folder, ask the client to move it.
3. A preview (`--dry-run`) changes nothing: no client add or move, no state, history, config, secrets, log or cookie
   writes, no notification, and no `.torrent` download from sites with a daily limit.
4. A client add counts only after read-back; an unconfirmed add is a failure, never a success.
5. Removing a topic removes only the TOW watch; the client torrent and the history stay.
6. Errors are recorded with safe details (no secrets) and never shown as success.

## Done

| Area | State |
|---|---|
| Watching | Topics with watch or once; episodes, file patterns and graphical exact file/folder selection with verified metadata; explicit native magnet preview where supported; global or numeric personal tracker intervals with row countdowns; transactional add with read-back for qBittorrent, Transmission and Deluge; replacing a seeding revision on request. |
| Sites | Seven site presets, one module per site; any similar site by hand; mirrors with fallback and cooldown; daily limits; password and browser sign-in. |
| Notifications | Telegram, Discord, WhatsApp, ntfy; grouping, quiet hours, digest, a persistent outbox; watchdog alerts with the reason for downtime. |
| Safety | One-step undo of any change; journaled multi-file writes recovered by any process; night copies (signed), restore points, `.towx` transfer; data version guard. |
| Access | Loopback without password; network only with password, sessions, CSRF and CSP; local-only access switches. |
| Runtime | One portable folder, one supervised service (`tow run`), autostart for Windows, Linux and macOS, version/release display and explicit web update with verified archive, snapshot, health check and rollback (terminal for service-managed POSIX). |
| Languages | English and Russian; a new language is one JSON file. |
| Quality | Over 5,500 tests in random order with a guard against network, processes and writes outside the temp folder; branch coverage ≥ 89%; strict mypy; ruff with a complexity cap. Blocking CI on Windows, Ubuntu 24.04/26.04 and macOS. |

## Next

Roughly in order of value:

1. **Linux and macOS** — real-machine checks of autostart beyond the blocking CI and installer smoke tests.
2. **qBittorrent on the shared client flow** (`clients/managed.py`) instead of its own, older transaction code.
3. **Smaller check pipeline** — split the check into fetch, decide, apply and record steps.
4. **Bot commands** — `/status`, `/check`, `/pause`, `/resume`, `/add` from the owner's chat via long polling (no
   inbound port), changes confirmed with a button.
5. **More messengers** — e-mail (SMTP), Matrix, Slack; each one module with HTTP-faked tests.
6. **More clients** — BiglyBT through the Transmission-compatible RPC, after a full live test.
7. **Backups** — an off-machine copy target; restoring state and history from a night copy without the key.
8. **Internals** — one HTTP connection per tracker per run; `qbit*` health keys renamed to `client*`.
9. **Python 3.15** — move when the final release is out.

## Out of scope

- Downloading, streaming or moving media; hosting or indexing content.
- Searching trackers or reading RSS feeds — TOW follows links you give it.
- Bypassing captchas, Cloudflare challenges, download limits or other site protections.
- Exposing TOW directly to the internet; use a VPN or an SSH tunnel.
- Clients that cannot be tested live and safely (closed-source clients with ads and outdated Web UIs).
