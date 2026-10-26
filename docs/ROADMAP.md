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
| Watching | Topics with watch or once; episodes, file patterns and graphical exact file/folder selection with verified metadata; explicit native magnet preview where supported; global or numeric personal tracker intervals with row countdowns; transactional add with read-back for qBittorrent, Transmission and Deluge; replacing a seeding version on request, or by the check itself when the owner turns it on (Settings → Checks or the topic); adopting a torrent already in the client without TOW's mark (**Adopt into TOW**, `tow adopt`); a version that does not fit on the drive added stopped and started once there is room, with the free space shown in the forms; a long verification of existing files by the client finished later from the client alone, without a restart or a false success; a torrent removed from the client added again by the row's check; a check you start while another one runs waits in a bounded queue, and its results name each topic's outcome. |
| Sites | Seven site presets, one module per site; any similar site by hand; mirrors with fallback and cooldown, and a site's own request to wait obeyed; a few bounded automatic retries after a site outage; daily limits; password and browser sign-in; optional disabled-by-default external access providers (on Windows also an installed module started on demand), bounded fallback and verified current-infohash capabilities. See [Site access](PROTECTION.md). |
| Notifications | Telegram, Discord, WhatsApp, ntfy; grouping, quiet hours, digest, a persistent outbox; watchdog alerts with the reason for downtime. |
| Safety | One-step undo of any change; journaled multi-file writes recovered by any process; nightly backups (signed), restore points, `.towx` transfer; data version guard; a folder off the disk TOW recorded for it is never called missing or written to until the owner accepts the disk. |
| Access | Loopback without password; network only with password, sessions, CSRF and CSP; local-only access switches; errors shown to a network device without paths or a link's private part; a request budget per session and tab; the install folder closed to the computer's other accounts (`tow permissions`, `tow permissions fix`). |
| Runtime | One portable folder, one supervised service (`tow run`), autostart for Windows, Linux and macOS, version/release display and explicit web update with verified archive, snapshot, health check and rollback (terminal for service-managed POSIX); a terminal update that runs in its own process and survives its window; uv isolated to the install's own `runtime/uv.toml`; a support matrix derived from the lock and CI. |
| Interface | Language (Auto, Русский, English) and theme as the system, light or dark; fields as wide as what they hold, one solid button per card and confirmations in TOW's own window; an eye on every password, token and key field and a "saved" tag on a stored one; Settings in order of use, one compact Network access card, search by section; one short time format with the full time on a tap and a Full timestamps switch for the Log and History; a site's card that starts with its sign-in; Home sorting remembered per device; one message per action, named by its topic, folding into Recent messages, one list for every signed-in device with shared read and clear; open pages that follow other tabs, devices and TOW itself within about five seconds while visible, never replacing a row or section in use; saves that merge with a change made elsewhere and ask which value to keep only for a field changed on both sides; manual checks that stay on the page and update their rows; keyboard and screen-reader access to the header, rows and messages. |
| Languages | English and Russian; a new language is one JSON file. |
| Structure | The check as a package of steps (`tow.check`: run, topic, apply, client operations, reconcile, space, verification, notices, rows); thin web routes over `tow.web.services`; one crash journal for multi-file writes (`tow.journal`). |
| Quality | Over 10,500 tests in random order on every core with a guard against network, processes and writes outside the temp folder, and a 30-minute limit on the whole run; branch coverage ≥ 93%; strict mypy; ruff with a complexity cap. Blocking CI on Windows, Ubuntu 24.04/26.04 and macOS. |

## Next

Roughly in order of value:

1. **Linux and macOS** — real-machine checks of autostart beyond the blocking CI and installer smoke tests; 64-bit
   ARM Linux and Alpine moved from "expected to work" to tested.
2. **Bot commands** — `/status`, `/check`, `/pause`, `/resume`, `/add` from the owner's chat via long polling (no
   inbound port), changes confirmed with a button.
3. **More messengers** — e-mail (SMTP), Matrix, Slack; each one module with HTTP-faked tests.
4. **More clients** — BiglyBT through the Transmission-compatible RPC, after a full live test.
5. **Backups** — an off-machine copy target; restoring state and history from a nightly backup without the key.
6. **Internals** — one HTTP connection per tracker per run; `qbit*` health keys renamed to `client*`.
7. **Python 3.15** — move when the final release is out.
8. **Optional browser transport** — isolated, separately managed browser sessions for path-specific/continuous
   challenges, only after preserving origin/redirect/credential boundaries, bounded responses, cancellation,
   process cleanup and three-platform tests. Native nodriver/zendriver and curl_cffi transport are not bundled today;
   compatible external services are supported, but cookie replay cannot cover every browser-bound session.

## Out of scope

- Downloading, streaming or moving media; hosting or indexing content.
- Searching trackers or reading RSS feeds — TOW follows links you give it.
- Circumventing download quotas, IP/access bans or account permissions; automatically solving interactive captchas.
  Optional external services may assist with a detected browser challenge, but neither TOW nor a provider guarantees
  successful access. They never receive a torrent download URL or account credentials.
- Exposing TOW directly to the internet; use a VPN or an SSH tunnel.
- Clients that cannot be tested live and safely (closed-source clients with ads and outdated Web UIs).
