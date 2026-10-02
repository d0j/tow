# TOW user guide

How TOW thinks, what each screen and colour means, and what to do about each message.
Русская версия: [ru/guide.md](ru/guide.md). Installation: [install.md](install.md).

- [Concepts](#concepts)
- [Screens](#screens)
- [Adding a topic](#adding-a-topic)
- [Choosing files](#choosing-files)
- [Statuses and colours](#statuses-and-colours)
- [Checks](#checks)
- [Sites, mirrors and sign-in](#sites-mirrors-and-sign-in)
- [Notifications](#notifications)
- [History and undo](#history-and-undo)
- [Backups](#backups)
- [Password and network access](#password-and-network-access)
- [Troubleshooting](#troubleshooting)
- [Where things are](#where-things-are)

## Concepts

| Term | Meaning |
|---|---|
| **Topic** | A page on a tracker that holds one torrent, for example a series. TOW remembers its link, a name, a folder and a torrent client. |
| **Watch** | *Keep watching*: TOW checks the topic on a schedule and adds every new revision. *Once*: TOW adds it one time and then only follows the download progress. |
| **Revision** | One version of the topic's torrent. Trackers replace the `.torrent` when episodes are added; each replacement is a new revision with a new hash. |
| **Site** | A tracker TOW knows how to read: how to recognise its links, where the `.torrent` is, how to sign in. Known sites come preconfigured. |
| **Mirror** | Another address of the same site. TOW tries them in order and remembers the one that worked. |
| **Torrent client** | qBittorrent, Transmission or Deluge with its Web UI turned on. TOW only talks to it; the client downloads. |
| **Selection** | Which files of the torrent to download: all, episodes by number, or file patterns. |

TOW never downloads, copies or moves media itself. It reads pages, downloads `.torrent` files, and asks your
client to add, start, stop or move a torrent. It only ever changes torrents it added itself (tagged `tow`).

## Screens

| Screen | What is there |
|---|---|
| **Home** | Your topics: status dot, site icon, name, folder, latest event, progress. **+** adds a topic. Click a row to edit it. Until the first topic is added, Home shows three first steps: the torrent client, a messenger (optional), a topic link. |
| **Sites** | Trackers and their mirrors, sign-in state, pause and check per site. **+** adds a site: paste a link to any topic of it first, the rest fills in; patterns and paths are under **Advanced**. |
| **Settings** | Torrent clients, notifications, check interval, network access and password, the TOW service, backups, language. |
| **History** | Downloads, errors, changes and notifications, with filters and search. |
| **Diagnostics** | The pulse icon in the header: the connection to the torrent client and to every site. The **?** icon opens this guide. |
| **Log** | The list icon in the header: the latest events, live. |

The clock in the header counts down to the next scheduled check; before the first one it says “none yet”.

## Adding a topic

1. **Home → +**, paste the link to the *topic page* (not a magnet link).
2. Keep the suggested name, or type your own. Pick the folder where the client should save the files.
3. Choose **what to download** and whether to **keep watching**.
4. **Add.** TOW checks the topic at once (the button says “Checking…”, up to a minute): the message “… — added to the torrent client” means the client confirmed
   it. If the add is refused, the form opens again with everything you typed and the reason above it.

If the site is not known yet, add it first on **Sites** — pasting any topic link there fills in the fields.

## Choosing files

| Choice | Example | Notes |
|---|---|---|
| All files | — | The whole torrent. |
| Episodes by number | `S01E03-E05, S01E07` · `04x01-03` · `5-8` | Video and its subtitles count as one episode. Use `S01E05` when an episode number exists in several seasons. |
| Files by pattern | `*.mkv` · `Subs/*.srt` | Glob patterns: `*`, `?` and `[]` are special. A pattern cannot leave the torrent's folder. |

If nothing matches, or the match is unclear, nothing is started. A range of episodes that are not out yet is
*waiting for episodes*, not an error.

How an add works, for every client: the torrent is added **stopped** and tagged `tow` + `tow-pending`; the files
are selected; the selection is **read back** from the client; only then the torrent starts and `tow-pending` is
removed. If any step is not confirmed, TOW stops what it added and reports the failure.

When a new revision arrives while the previous one is still seeding the same file, TOW does not touch the old
torrent. It asks you: **Stop the previous one and add** stops the old torrent (its files stay) and adds the new one.

## Statuses and colours

Each row has two signals. They describe the **latest TOW check**, not whether the page opens in your browser and
not whether the download is complete.

**The dot** — the topic as a whole:

| Colour | Meaning | What to do |
|---|---|---|
| Green | The last check succeeded and nothing is wrong. | Nothing. |
| Blue | Something new was found or added. An event, not a health result. | Nothing. |
| Amber | The site is unavailable for now: mirrors down or paused, Cloudflare, a redirect to an unknown host. | Wait; TOW retries on the next check. |
| Red | Your help is needed: the torrent client, the folder or disk, sign-in, a daily limit, the configuration. | Open the row: the full reason is there. |
| Grey | Not checked yet. | Press the row's check icon, or wait. |

**The site icon** (globe) — the site alone: amber for transport problems (unreachable, paused, Cloudflare, sign-in,
daily limit), red for site-side problems (topic removed, page is not a torrent page, no site configured). A torrent
client or folder error does not colour it.

The red delete button and the pause icon are controls, not health colours. In the header, a site's name is amber
when its mirrors did not answer; the client name is green when the client answers, red when it does not, grey
before it was asked; the bell is green when messages get through, red when a messenger refused one, grey when
none is connected.

## Checks

| Check | When | What it does |
|---|---|---|
| Scheduled | Every 12 hours by default (Settings → Checks, 15 min – 24 h) | Every watched topic: downloads the `.torrent`, adds a new revision. |
| Progress | Every 30 minutes | Only asks the client about downloads; no requests to sites. |
| Check all | ↻ in the header (asks to confirm) | The scheduled check, now, in the background. |
| One topic | ↻ in the row | That topic only; ignores the one-hour mirror pause. |

A check missed while the computer slept or was off runs right after it wakes. Manual checks send notifications the
same way as scheduled ones. **Pause** in a row skips the topic in scheduled checks; the row's check still works.

## Sites, mirrors and sign-in

- **Mirrors.** TOW starts with the main mirror and moves on until one returns the `.torrent`; that one becomes the
  main mirror. Click a mirror on **Sites** to make it the main one.
- **Cooldown.** A mirror that fails three times in a row rests for an hour; the others keep working. The site's
  **Check** button ends the pause.
- **Daily limit.** When a site says the download limit for today is reached, TOW leaves that site alone until
  tomorrow and does not try other mirrors (they share the limit).
- **Sign-in.** Sites that need it get a login and password on **Sites**, stored encrypted. A site that only allows
  signing in through a browser (NNM-Club) opens a browser window (Chrome, Chromium or Edge) on the computer
  running TOW; the session is saved after you sign in.
- **Site pause** stops scheduled checks of every topic on that site.

## Notifications

Settings → Notifications: Telegram, Discord, WhatsApp (via CallMeBot), ntfy. Each card has “How to connect” steps,
**Check** sends a test message.

- One message per topic per check: added, new version, new episodes, download complete, removed, back in the
  client. An error in the same check is added to the message, not hidden.
- Errors are sent when they appear or change kind, not on every check; “… — working again” follows the recovery.
  When one site fails the same way for several topics, you get one message for the site.
- Every message goes to every connected messenger. An undelivered message waits in a queue and goes first next time.
- Optional in `config.yaml`: `quiet_hours: "23-8"` (held and sent together afterwards), `daily_digest_hour: 9`,
  `heartbeat_url` (an external monitor such as healthchecks.io that alerts you when TOW goes silent).
- The watchdog tells you when TOW was down and why (sleep, restart, Windows Update, a crash), and when scheduled
  checks or night copies fall behind.

## History and undo

- **History** shows downloads, errors, changes and notifications, filterable and searchable. The file history
  of a topic (the row's files link) lists every file of every revision; files of older revisions are kept for
  `history_keep_days` (730 by default), files of the current revision are never dropped.
- **Undo.** After a change (an edit, a delete, a settings save, a password change) its message carries **Undo**
  with the time left; on later pages the button stays in the header. It lasts a short time (Settings → Checks,
  “Show Undo for”) and puts back the last change, as one step. Removing a topic
  (**Remove from TOW**) removes only the TOW watch: the torrent and its files stay in the client, and its history
  stays.
- A network error is said in words (connection refused, no answer in time, address not found…); the raw text is
  under **Details**.

## Backups

| Kind | Made | Restore | Holds |
|---|---|---|---|
| Night copy | Every night at 03:30, or at start if the last one is older than a day; last 14 kept | Settings → Backups → Nightly backups | settings, topics, history, encrypted passwords |
| Restore point | **Create a backup** in Settings, and automatically before risky changes; last 10 kept | Settings → Backups made by hand | the same |
| TOW file (`.towx`) | Settings → Backups → TOW file → **Save file** | Same card, **Choose a file** | the same, in one encrypted file |
| Export | `tow export --output tow.towx` (asks a passphrase, 12+ characters) | `tow import --input tow.towx --apply` | the same, protected by your passphrase |

- None of them contains the master key, downloaded files or the torrent client's own data.
- Night copies and TOW files are encrypted with the install's **master key** (`keys/master.key`). Restoring them on
  another computer needs the same key. `tow export` / `tow import` work between installs with different keys.
- Before any restore TOW checks the copy and saves the current state; network access settings stay as they are.
- Folders: Settings → Backups → Backup folders. Another drive or a network share (`\\server\share`) is fine.

**Keep a copy of `keys/master.key` away from the computer.** TOW creates it on the first start (`tow keys ensure`
does it on its own) and says once to back it up. Without it, saved passwords and tokens cannot be read and copies
cannot be restored. `tow keys status` shows which key file is in use (never the key itself).

## Password and network access

- On the computer running TOW, it opens **without a password**.
- Other devices (a phone, a laptop, over a VPN) can sign in only with the **password**, and only after network access
  is turned on (Settings → Network access, or `tow access on`, which asks for a password when none is set; it takes
  effect after a restart; `tow access off` closes it again). A device stays
  signed in for 90 days.
- Turning network access on or off, and the first-start page, work only on the computer itself, so nobody on the
  network can lock you out.
- **Forgot the password?** On the computer running TOW set a new one without the old one: Settings → Network access
  → Password, or `tow password`. Other devices then sign in again.
- **Sign out everywhere** (Settings → Network access) ends every network session; the password stays.
- The reminder you set is visible to anyone who opens the sign-in page — never write the password into it.
- Requests from public internet addresses are always refused. See [Headless server](../README.md#remote-access)
  for SSH tunnels and the reverse-proxy caveat.

## Troubleshooting

The left column is the text TOW shows (on Home, in a row, in a message or on a page).

| Message | Meaning | What to do |
|---|---|---|
| The torrent client is unavailable: new torrents are not added. · torrent client unreachable | TOW cannot reach the client's Web UI. | Start the client, check its Web UI is on; Settings → Torrent clients → **Check**. |
| Log in to *site* is needed (topics: *n*) · the page has no torrent link — log in to the site | The site wants a signed-in user for the `.torrent`. | Open the row → **Sign in**, or enter the login on **Sites**. |
| *site*: no mirror answered: … · no connection | Every mirror failed. | Usually temporary. If it lasts, open the site in a browser and add a working mirror on **Sites**. |
| *site*: all mirrors are paused · the site is paused | Mirrors are resting after failures, or you paused the site. | Wait an hour, or press **Check** in the site's row. |
| the site is behind a Cloudflare check — open it in a browser | The site shows a browser challenge. | TOW does not bypass it. Try another mirror or wait. |
| daily download limit reached | The site's download quota for today is used up. | Nothing; TOW tries again tomorrow. |
| the previous version of the torrent is still active on the same file: … | A new revision overlaps a torrent that is still seeding. | **Stop the previous one and add** in the row, or stop it in the client. |
| waiting for episodes (…) | The episodes you chose are not in the torrent yet. | Nothing; they are added when they appear. |
| the selection matches no file of the torrent · episode *N* is in several seasons — write it as S01E05 | The selection cannot be applied safely. | Edit the topic's selection. |
| not enough disk space: … GB needed, … GB free | The target drive is too full. | Free space or choose another folder. |
| no site is set up for this link | The link belongs to an unknown site. | **Sites → +**, paste the link. |
| the torrent was removed from the client | The torrent is gone from the client. | Check again to add it back, or **Remove from TOW**. |
| the torrent in the client was not added by TOW — … | TOW will not change torrents it did not add. | Change it in the client, or remove it there and let TOW add it. |
| another check is running right now; press “Check” again in a minute | One check at a time. | Wait. |
| Scheduled checks have not run since … | TOW was not running, or checks keep failing. | `tow autostart status`; look at `data/logs/run.log`. |
| The last scheduled check is blocked: the secrets store is unavailable. · The master key is missing … | The master key is missing or wrong. | `tow keys status`; put back `keys/master.key` from your copy, or `tow keys adopt --from FILE`. |
| state.json was written by a newer TOW … | You went back to an older version. | Update TOW again, or restore a backup made by this version. |
| Sign-in from other devices is off — TOW opens only on its own computer. | Network access is off. | Turn it on at the computer running TOW. |
| No password for other devices is set yet. | Network access is on, but there is no password. | Set it on the computer running TOW: Settings → Network access. |

Still stuck: **Diagnostics** checks the client and every site; `tow doctor` does the same from the command
line. Logs are in `data/logs/`. When you report a bug, remove personal data first.

## Where things are

| Path (inside the TOW folder) | What |
|---|---|
| `config.yaml` | Settings and sites, without passwords. |
| `data/state.json` | Topics and their state. |
| `data/download_history.json` | Files of every revision. |
| `data/secrets.enc` | Passwords, tokens and cookies, encrypted with the master key. |
| `data/logs/` | Logs (rotated). |
| `keys/master.key` | The master key. Never in any copy. |
| `backup/` | Night copies and pre-update snapshots. |

An empty password field in any form means “keep the saved one”; saved passwords are never shown.
