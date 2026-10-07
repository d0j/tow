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
- [Updates](#updates)
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
| **Selection** | Which files of the torrent to download: all, checked files, episodes by number, or file patterns. |

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
4. **Add.** TOW checks the topic at once (the button says “Checking…”, up to a minute): the message
   “… — added to the torrent client” means the client confirmed it. If the add is refused, the form opens again
   with everything you typed and the reason above it.

If the site is not known yet, add it first on **Sites** — pasting any topic link there fills in the fields.

## Choosing files

| Choice | Example | Notes |
|---|---|---|
| All files | — | The whole torrent. |
| Choose files | Check files or folders in the contents tree | Literal paths and sizes are retained; new files stay unselected. Missing or resized selected files require review. |
| Episodes by number | `S01E03-E05, S01E07` · `04x01-03` · `5-8` | Video and its subtitles count as one episode. Use `S01E05` when an episode number exists in several seasons. |
| Files by pattern | `*.mkv` · `Subs/*.srt` | Glob patterns: `*`, `?` and `[]` are special. A pattern cannot leave the torrent's folder. |

**Get contents** reads tracker metadata, or use a local `.torrent`. A local file only previews the
contents: the first check still gets the torrent from the site and refuses a different one, and the
file is not kept as the topic's saved contents. A download-limited site asks for confirmation first.
**From magnet (contacts peers)** asks the selected client itself for the metadata: qBittorrent 5.2+ or an
attached Deluge daemon. Existing qBittorrent torrents can be read on older versions.
No transfer is added or changed by this preview. qBittorrent's peer-metadata request may continue after
timeout or closing the form; its Web API cannot cancel it. Unsupported clients need a `.torrent`.

Search hides rows, not selections. Folder checkboxes and **Select all files** include files hidden by
search; the selected count and size cover the whole torrent. A multi-episode file is downloaded whole;
v1 torrent pieces can include bytes from adjacent unselected files. Editing stores the choice for the
next check; cancelling discards the draft. The **?** button explains these rules in the form.

If nothing matches, or the match is unclear, nothing is started. A range of episodes that are not out yet is
*waiting for episodes*, not an error.

Specials (`Season 00`, `S00`, OVA and bonus folders) stay separate files, not ordinary season episodes.
They can be downloaded with **All files** or **Files by pattern**, without inflating the episode counter.
Invalid episode ranges or unsafe file patterns are refused when saving, before a client check.

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
| Global timer | Every hour by default (Settings → Checks, 15 min – 24 h) | Active topics without a personal timer: checks the tracker and adds a changed torrent revision to the selected client. |
| Personal timer | Set minutes when adding or editing a topic, 1 min – 7 days | Overrides the global timer for that topic. A clock and countdown appear in its Home row. |
| Progress | Every 30 minutes | Only asks the client about downloads; no requests to sites. |
| Check all | ↻ in the header (asks to confirm) | Checks active topics now in the background, including those with personal timers; does not reset their countdowns. |
| One topic | ↻ in the row | That topic only; ignores the one-hour mirror pause. |

A check missed while the computer slept or was off runs right after it wakes. Manual checks send notifications the
same way as scheduled ones. **Pause** in a row skips the topic in scheduled checks; the row's check still works.
An empty personal timer uses the global interval. Saving a different personal interval starts a new countdown;
manual checks and progress observations do not reset it.

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
| Night copy | Daily schedule (03:30 by default); keep 7 days by default | Settings → Backups → Nightly backups | settings, topics, history, encrypted passwords |
| Restore point | **Create a backup** in Settings, and automatically before risky changes; last 10 kept | Settings → Backups made by hand | the same |
| TOW file (`.towx`) | Settings → Backups → TOW file → **Save file** | Same card, **Choose a file** | the same, in one encrypted file |
| Export | `tow export --output tow.towx` (asks a passphrase, 12+ characters) | `tow import --input tow.towx --apply` | the same, protected by your passphrase |

- None of them contains the master key, downloaded files or the torrent client's own data.
- **Night copies are signed, not fully encrypted.** Settings, topics, history and logs remain readable files;
  saved passwords, tokens and cookies stay encrypted. Keep the backup folder private. TOW files (`.towx`)
  encrypt the whole copy with the install's **master key** (`keys/master.key`). Checking/restoring a signed
  night copy or restoring a TOW file on another computer needs the same key. `tow export` / `tow import`
  work between installs with different keys.
- **Automatic night copies** can be turned off in Settings → Backups without a restart. Existing copies and
  manual actions stay available; a copy already running finishes normally. A daily slot missed while the
  computer was off or asleep is caught up when TOW runs again, not after a fixed 24-hour wait.
- **Keep night copies, days** accepts a whole number from 1 to 3650; new installations default to 7 days.
  An existing explicit count-based `backup_keep` policy stays in effect until you save days. Saving the field
  does not delete copies: retention applies only after a new copy passes verification. The new copy, the three
  newest earlier copies (whatever their age, so a computer that was off longer keeps some history) and
  future-dated copies are protected. An optional size budget can shorten history, but not delete these copies.
- Both saved-copy lists offer **Check**, **Restore** and **Delete**. Deletion requires confirmation for the
  dated copy and cannot be undone; it removes only that copy, not current settings, torrents or history.
- Before any restore TOW checks the copy and saves the current state; network access settings stay as they are.
  Restoring a night copy keeps the event log (History) as it is: what happened after the copy stays listed.
- Folders: Settings → Backups → Backup folders. Another drive or a network share (`\\server\share`) is fine.

**Keep a copy of `keys/master.key` away from the computer.** TOW creates it on the first start (`tow keys ensure`
does it on its own) and says once to back it up. Without it, saved passwords and tokens cannot be read and copies
cannot be restored. `tow keys status` shows which key file is in use (never the key itself).

## Updates

Settings → **Version and updates**; the version in the corner of every page opens it.

- **Check for updates** asks GitHub for the latest stable release. With **Check for updates automatically** on, TOW
  asks by itself at most every 12 hours while a page is open, and Home shows a small notice when a new version is
  out. Nothing is installed without you, and nothing about your topics or settings is sent.
- **Update** installs it after you confirm. TOW first saves and checks a `.towx` archive of your data, then stops,
  takes a snapshot of `data/` and `config.yaml`, switches the code, starts again and checks that the new version
  answers and reads your data. The page reconnects and shows the result; **Reload page** opens the new interface.
- **What is kept:** settings, topics, history, saved passwords and tokens, `keys/master.key`, autostart and network
  access. Downloaded files are never touched.
- **When the new version fails** to install, start or read the data, the update puts the previous version and data
  back by itself and says so. If they cannot be put back completely, TOW is not started on the mix: the update says
  what to do (usually: run it again).
- **Going back:** **Install another version or roll back** installs an earlier release, 1.22.21 or newer. A version
  that cannot read the current data (anything before 1.23.0 once 1.23 ran) is refused before TOW stops.
- **Without the page** (TOW started by systemd or launchd, or a version before 1.22.20) use `Update TOW.cmd`,
  `update-tow` or the command `tow update --ref <tag>` prints: [README](../README.md#update-and-backups).
- **When an update is cut off** while it replaces the code (the computer turned off, the updater was killed),
  TOW does not start: `tow run`, `tow start` and the start files refuse with “TOW was not started: an update was cut
  off…”. Run the update again: it first puts the previous version back, then installs the release you asked for.
  If TOW kept running on the new version and changed data after the cut, the update stops instead of overwriting
  those changes and names the command (`--discard-newer-data`) that puts the snapshot back anyway. Closing the
  window or Ctrl+C during the switch does not cut it off: the update finishes or rolls back first.
- **Update log:** **More: history, rollback and log** → **Update log** shows the log of the latest update, with the
  install folder written as `<TOW>`. If an update did not finish, read it before trying again.

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
- The sign-out button in the header (shown only on a device signed in over the network) ends this device's
  session.
- **Sign out everywhere** (Settings → Network access) ends every network session; the password stays.
- The reminder you set is visible to anyone who opens the sign-in page — never write the password into it.
- Requests from public internet addresses are always refused. See [Remote access](../README.md#remote-access)
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
| TOW was not started: an update was cut off while it replaced the code … | An update stopped half-way (the computer turned off, the updater was killed). | Run the update again (`Update TOW`): it puts the previous version back first. |
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
