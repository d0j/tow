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
- [Command line](#command-line)
- [Where things are](#where-things-are)

## Concepts

| Term | Meaning |
|---|---|
| **Topic** | A page on a torrent site that holds one torrent, for example a series. TOW remembers its link, a name, a folder and a torrent client. |
| **Follow the topic** | *Yes*: TOW checks the topic on a schedule and adds every new version. *No, once*: TOW adds it one time and then only follows the download progress. |
| **Version** | One `.torrent` of a topic. Sites replace the `.torrent` when episodes are added; each replacement is a new version with a new hash. |
| **Site** | A torrent site TOW knows how to read: how to recognise its links, where the `.torrent` is, how to sign in. Known sites come preconfigured. |
| **Mirror** | Another address of the same site. TOW tries them in order and remembers the one that worked. |
| **Torrent client** | qBittorrent, Transmission or Deluge with its Web UI turned on. TOW only talks to it; the client downloads. |
| **Selection** | Which files of the torrent to download: all, checked files, episodes by number, or file patterns. |

TOW never downloads, copies or moves media itself. It reads pages, downloads `.torrent` files, and asks your
client to add, start, stop or move a torrent. It only ever changes torrents it added itself (tagged `tow`).

## Screens

| Screen | What is there |
|---|---|
| **Home** | Your topics: status dot, site icon, name, folder, latest event, progress. **+** adds a topic. Click a row to edit it. Filters and search hide rows; when nothing matches, **Show all topics** clears them. **Sort**: *As added*, *By name*, *By latest event* or *Errors first*; this device remembers the choice. Until the first topic is added, Home shows three first steps: the torrent client, a messenger (optional), a topic link. |
| **Sites** | Sites and their mirrors, sign-in state, pause and check per site (the icons in its row). **+** adds a site: paste a link to any topic of it first, the rest fills in; patterns and paths are under **Advanced**. |
| **Settings** | Language, theme (as the system, light or dark), torrent clients, notifications, checks, network access and password, version and updates, the TOW service, backups, the log; below them **Diagnostics**, the **Guide** and, on a device signed in over the network, **Sign out on this device**. |
| **Log** | The scroll icon in the header: the latest events, live. |
| **History** | Downloads, errors, changes and notifications, with filters and search. It has no icon in the header: the link **History of downloads and changes** in the Log window opens it. |
| **Diagnostics** | At the end of Settings: the connection to the torrent client and to every site. The in-app **Guide** is next to it. |

The ↻ in the header depends on the page: on Home it is **Check all topics now**, on **Sites** **Check the connection
to the sites** (Diagnostics). The clock in the header counts down to the next scheduled check; before the first check
it shows “—”, and its tooltip says “No check has run yet”.

## Adding a topic

1. **Home → +**, paste the link to the *topic page* (not a magnet link).
2. Keep the suggested name, or type your own. Pick the folder where the client should save the files.
3. Choose **what to download** and whether to **follow the topic**.
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

**Get contents** reads the file list from the site, or choose a local `.torrent`. A local file only previews
the contents: the first check still gets the torrent from the site and refuses a different one, and the
file is not kept as the topic's saved contents. A download-limited site asks for confirmation first.
**From magnet (asks other users)** asks your torrent client for the file list without downloading anything; it
needs qBittorrent 5.2+ or Deluge. Other clients need a `.torrent`.

Search hides rows, not selections. Folder checkboxes and **Select all files** include files hidden by
search; the selected count and size cover the whole torrent. A file with several episodes is downloaded
whole, and the client may also write small parts of neighbouring files that are not chosen. Editing stores
the choice for the next check; cancelling discards the draft. The **?** button explains these rules in the form.

If nothing matches, or the match is unclear, nothing is started. A range of episodes that are not out yet is
*waiting for episodes*, not an error.

Specials (`Season 00`, `S00`, OVA and bonus folders) stay separate files, not ordinary season episodes.
They can be downloaded with **All files** or **Files by pattern**, without inflating the episode counter.
Invalid episode ranges or unsafe file patterns are refused when saving, before a client check.

How an add works, for every client: the torrent is added **stopped** and tagged `tow` + `tow-pending`; the files
are selected; the selection is **read back** from the client; only then the torrent starts and `tow-pending` is
removed. If any step is not confirmed, TOW stops what it added and reports the failure.

Under the folder, the add and edit forms show the free space on its drive (“Free on this disk: …”) and, once the
chosen files are known, whether they fit (“Chosen …, free …: it fits” or “Not enough room: …”).

When the chosen files do not fit on the target drive (only what is not in the folder yet counts, plus a 0.5 GB
reserve), TOW still adds the torrent stopped, with its files selected and read back, but does not start it. The row
says *waiting for disk space* (red: free some space) and you get one message. TOW does not download the `.torrent`
again while it waits; every check, and a short pass every 5 minutes that asks only the client, looks again and
starts the torrent by itself once there is room (you get a message then too). A paused topic is left waiting: the
5-minute pass and scheduled checks skip it, the row's check still looks. If you start the torrent yourself, choose
other files for it in the client, or remove it, TOW stops waiting. Only a drive this computer sees is measured: a
client on another computer, a folder this computer cannot see or free space that cannot be read never holds a
torrent back, and the forms then show no free space.

When a new version arrives while the previous one is still seeding the same file, TOW does not touch the old
torrent. It asks you: **Stop the previous one and add** stops the old torrent (its files stay) and adds the new one.
While it waits, scheduled checks do not download the new version's `.torrent` again when the topic page's magnet
still names it, or, on a page without a magnet, when the site has a daily download limit (a newer upload there is
found once the previous torrent is stopped, or by a check you start); the wait is logged once, not at every check.

## Statuses and colours

Each row has two signals. They describe the **latest TOW check**, not whether the page opens in your browser and
not whether the download is complete.

**The dot** — the topic as a whole:

| Colour | Meaning | What to do |
|---|---|---|
| Green | The last check succeeded and nothing is wrong. | Nothing. |
| Blue | Something new was found or added. An event, not a health result. | Nothing. |
| Yellow | The site is unavailable for now: mirrors down or paused, Cloudflare, a redirect to an unknown address. | Wait; TOW retries on the next check. |
| Red | Your help is needed: the torrent client, the folder or disk, sign-in, a daily limit, the configuration. | Open the row: the full reason is there. |
| Grey | Not checked yet. | Press the row's check icon, or wait. |

**The site icon** (globe) — the site alone: yellow for transport problems (unreachable, paused, Cloudflare, sign-in,
daily limit), red for site-side problems (topic removed, page is not a torrent page, no site configured). A torrent
client or folder error does not colour it.

The red delete button and the pause icon are controls, not health colours. In the header (and on **Sites**) a
site's name follows the latest check or **Diagnostics** that asked the site: green when it answered, yellow when
it did not (mirrors down or paused, Cloudflare, sign-in, daily limit), grey before it was asked; the client name
is green when the client answers, red when it does not (also right after **Check** in Settings), grey before it
was asked; the bell is green when messages get through, red when a messenger refused one, grey when none is
connected.

## Checks

| Check | When | What it does |
|---|---|---|
| Global timer | Every hour by default (Settings → Checks, 15 min – 24 h); the very first one two minutes after TOW starts, and a restart keeps the cadence | Active topics without a personal timer: checks the site and adds a new version of the torrent to the selected client. |
| Personal timer | Set minutes when adding or editing a topic, 1 min – 7 days | Overrides the global timer for that topic. A clock and countdown appear in its Home row. |
| Progress | Every 30 minutes; the first one five minutes after TOW starts | Only asks the client about downloads; no requests to sites. |
| Disk space | Every 5 minutes, only while a torrent waits for disk space | Only asks the client: starts a waiting torrent once its files fit; no requests to sites. |
| Check all | ↻ in the header on Home (asks to confirm) | Checks active topics now in the background, including those with personal timers; does not reset their countdowns. Ignores the mirror pause. |
| One topic | ↻ in the row | That topic only; ignores the mirror pause. |

A check missed while the computer slept or was off runs right after it wakes. Manual checks send notifications the
same way as scheduled ones. **Pause** in a row skips the topic in scheduled checks; the row's check still works.
An empty personal timer uses the global interval. Saving a different personal interval starts a new countdown;
manual checks and progress checks do not reset it.

## Sites, mirrors and sign-in

- **Mirrors.** TOW starts with the main mirror and moves on until one returns the `.torrent`; that one becomes the
  main mirror. Click a mirror on **Sites** to make it the main one.
- **Cooldown.** A mirror that fails three times in a row rests for an hour (rutor: 30 minutes); the others keep
  working. A site's `fail_threshold` and `cooldown_sec` in `config.yaml` change the count and the pause. The site's
  check icon in its row (“Check the site's mirrors”) ends the pause. Only the mirror's own failures count: it refuses
  or drops the connection, does not answer, answers with a server error, or shows a Cloudflare check on its front
  page too. A Cloudflare check or a slow answer on one topic's page alone is that topic's error (yellow) and does not
  pause the mirror for the other topics.
- **Daily limit.** When a site says the download limit for today is reached, scheduled checks leave that site alone
  until tomorrow and do not try other mirrors (they share the limit). A check you start (the row's ↻, **Check all**)
  still tries it.
- **Sign-in.** Sites that need it get a login and password on **Sites**, stored encrypted. A site that only allows
  signing in through a browser (NNM-Club) opens a browser window (Chrome, Chromium or Edge) on the computer
  running TOW; the session is saved after you sign in. When the saved session has expired (the site answers with a
  page instead of the `.torrent`, or shows the topic without its download link), TOW signs in again with the saved
  password: scheduled checks at most once in ten minutes per site, the row's ↻ every time.
- **Site pause** stops scheduled checks of every topic on that site.

## Notifications

Settings → Notifications: Telegram, Discord, WhatsApp (via CallMeBot), ntfy. Each card has “How to connect” steps,
**Check** sends a test message.

- One message per topic per check: added, new version, new episodes, download complete, removed, back in the
  client. An error in the same check is added to the message, not hidden.
- Errors are sent when they appear or change kind, not on every check; “… — works again” follows the recovery.
  A site that does not answer (yellow) is reported once it fails three checks in a row, so a mirror that drops out
  now and then does not send “error” and “works again” every other check.
  When one site fails the same way for three or more topics in one check, you get one message for the site.
- Every message goes to every connected messenger. An undelivered message waits in a queue and goes first next time.
- Optional in `config.yaml`, off when left out:
  - `quiet_hours: "23-8"`: messages are held and sent together afterwards. At most 200 are held; older ones are
    dropped, and the message after the quiet hours says how many.
  - `daily_digest_hour: 9`: once a day after that hour (not during quiet hours), a summary of the day: how many
    torrents were added, how many new versions or files were found, how many downloads completed, and the names of
    up to ten of them. It comes on top of the usual messages.
  - `heartbeat_url: https://…` (it must start with `https://`): an external monitor such as healthchecks.io that
    TOW pings every 10 minutes and that alerts you when TOW goes silent.
- The watchdog tells you when TOW was down and why (sleep, restart, Windows Update, a crash), and when scheduled
  checks or nightly backups fall behind.

## History and undo

- **History** shows downloads, errors, changes and notifications, filterable and searchable. The file history
  of a topic (the row's files link) lists every file of every version; files of older versions are kept for
  `history_keep_days` (730 by default), files of the current version are never dropped.
- **Undo.** After a change (an edit, a delete, a settings save, a password change) its message carries **Undo**
  with the time left; on later pages the button stays in the header. It lasts a short time (Settings → Checks,
  “Show “Undo” for”) and puts back the last change, as one step. Removing a topic
  (**Remove from TOW**) removes the topic from TOW only: the torrent and its files stay in the client, and its
  history stays.
- A network error is said in words (connection refused, no answer in time, address not found…); the raw text is
  under **Details**.

## Backups

| Kind | Made | Restore | Holds |
|---|---|---|---|
| Nightly backup | Daily schedule (03:30 by default), or **Back up now** in that card; keep 7 days by default | Settings → Backups → Nightly backups | settings, topics, history, encrypted passwords |
| Restore point | **Create a backup** in Settings, and automatically before risky changes; last 10 kept | Settings → Backups → Backups made by hand | the same |
| TOW file (`.towx`) | Settings → Backups → TOW file → **Save file** | Same card, **Choose a file** | the same, in one encrypted file |
| Export | `tow export --output tow.towx` (asks a passphrase, 12+ characters) | `tow import --input tow.towx --apply` | the same, protected by your passphrase |

- None of them contains the master key, downloaded files or the torrent client's own data.
- **Nightly backups are signed, not fully encrypted.** Settings, topics, history and logs remain readable files;
  saved passwords, tokens and cookies stay encrypted. Keep the backup folder private. TOW files (`.towx`)
  encrypt the whole copy with the install's **master key** (`keys/master.key`). Checking/restoring a signed
  nightly backup or restoring a TOW file on another computer needs the same key. `tow export` / `tow import`
  work between installs with different keys.
- **Automatic nightly backups** can be turned off in Settings → Backups without a restart. Existing copies and
  manual actions stay available; a copy already running finishes normally. A daily slot missed while the
  computer was off or asleep is caught up when TOW runs again, not after a fixed 24-hour wait. A nightly backup
  that failed is tried again six hours later, or at the next daily time if that comes first.
- **Keep nightly backups, days** accepts a whole number from 1 to 3650; new installations default to 7 days.
  An existing explicit count-based `backup_keep` policy stays in effect until you save days. Saving the field
  does not delete copies: retention applies only after a new copy passes verification. The new copy, the three
  newest earlier copies (whatever their age, so a computer that was off longer keeps some history) and
  future-dated copies are kept by the age rule. An optional size budget (`backup_max_mib`) can delete the three
  earlier copies too; only the new copy and future-dated ones always stay.
- Both saved-copy lists offer **Check**, **Restore** and **Delete**. Deletion requires confirmation for the
  dated copy and cannot be undone; it removes only that copy, not current settings, topics or history.
- Before any restore TOW checks the copy and saves the current state; network access settings stay as they are
  (when the settings in force cannot be read, a nightly backup restore leaves network access off and says so).
  Restoring a nightly backup keeps the event log (History) as it is: what happened after the copy stays listed.
- Folders: Settings → Backups → Nightly backups or Backups made by hand → **Folder for …**. Another drive or a
  network share (`\\server\share`) is fine; a network share is chosen on the computer running TOW, not from
  another device.

**Keep a copy of `keys/master.key` away from the computer.** TOW creates it on the first start (`tow keys ensure`
does it on its own) and says once to back it up. Without it, saved passwords and tokens cannot be read and copies
cannot be restored. `tow keys status` shows which key file is in use (never the key itself).

## Updates

Settings → **Version and updates**; the version in the corner of every page opens it.

- **Check for updates** asks GitHub for the latest stable release. With **Check for updates automatically** on, TOW
  asks by itself at most every 12 hours while a page is open, and Home shows a small notice when a new version is
  out. Nothing is installed without you, and nothing about your topics or settings is sent.
- When TOW is already the latest release, the check says so, and an update (`Update TOW.cmd`, `Update TOW.command`
  on macOS, `update-tow` on Linux) says “… is the latest release: nothing to update” and changes nothing.
- **Update** installs it after you confirm. TOW first saves and checks a restore point (a `.towx` archive of your
  data), then stops, takes an update snapshot of `data/` and `config.yaml`, switches the code, starts again and
  checks that the new version answers and reads your data. The page reconnects and shows the result; **Reload page**
  opens the new interface.
- **What is kept:** settings, topics, history, saved passwords and tokens, `keys/master.key`, autostart and network
  access. Downloaded files are never touched.
- **When the new version fails** to install, start or read the data, the update puts the previous version and data
  back by itself and says so. If they cannot be put back completely, TOW is not started on the mix: the update says
  what to do (usually: run it again).
- **Going back:** **Install another version or roll back** installs an earlier release, 1.22.21 or newer. A version
  that cannot read the current data (anything before 1.23.0 once 1.23 ran) is refused before TOW stops.
- **Without the page** (TOW started by systemd or launchd, or a version before 1.22.20) use `Update TOW.cmd`,
  `Update TOW.command` (macOS), `update-tow` (Linux) or the command `tow update --ref <tag>` prints:
  [README](../README.md#update-and-backups).
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
  effect after a restart; `tow access off` closes it again, at once). A device stays
  signed in for 90 days. Open TOW there by the computer's IP address, its plain name or `name.local`
  ([Remote access](../README.md#remote-access)).
- The password has at least 8 characters. After 5 wrong passwords in 10 minutes a device has to wait 30 seconds,
  and twice as long after each further wrong one, up to an hour; after 30 wrong passwords from all devices
  together, every device waits (up to 15 minutes). The computer running TOW needs no password and is not affected.
- Turning network access on or off, and the first-start page, work only on the computer itself, so nobody on the
  network can lock you out.
- **Forgot the password?** On the computer running TOW set a new one without the old one: Settings → Network access
  → Password, or `tow password`. Other devices then sign in again.
- **Sign out on this device** at the end of Settings (shown only on a device signed in over the network) ends
  this device's session.
- **Sign out everywhere** (Settings → Network access) ends every network session; the password stays.
- The reminder you set is visible to anyone who opens the sign-in page — never write the password into it. TOW
  refuses a reminder that shares four letters or digits in a row with the password; to change only the reminder,
  type the current password (also on the computer running TOW).
- Requests from public internet addresses are always refused. See [Remote access](../README.md#remote-access)
  for SSH tunnels and the reverse-proxy caveat.

## Troubleshooting

The left column is the text TOW shows (on Home, in a row, in a message or on a page).

| Message | Meaning | What to do |
|---|---|---|
| The torrent client is unavailable: new versions are not added. · torrent client unreachable | TOW cannot reach the client's Web UI. | Start the client, check its Web UI is on; Settings → Torrent clients → **Check**. |
| Sign in to *site* (topics: *n*): open the topic's row. · the page has no torrent link — sign in to the site | The site wants a signed-in user for the `.torrent`. | Open the row → **Sign in**, or enter the login on **Sites**. |
| *site*: no mirror answered: … · no connection | Every mirror failed. | Usually temporary. If it lasts, open the site in a browser and add a working mirror on **Sites**. |
| *site*: all mirrors are paused · *site*: the site is paused | Mirrors are resting after failures, or you paused the site. | Wait for the pause to end (an hour; rutor: 30 minutes), or press the check icon in the site's row on **Sites**. A site you paused: resume it there. |
| the site is behind a Cloudflare check — open it in a browser | The site shows a browser challenge. | TOW does not bypass it. Try another mirror or wait. |
| daily download limit reached | The site's download quota for today is used up. | Nothing; TOW tries again tomorrow. |
| the previous version of the torrent is still active on the same file: … | A new version overlaps a torrent that is still seeding. | **Stop the previous one and add** in the row, or stop it in the client. |
| waiting for episodes (…) | The episodes you chose are not in the torrent yet. | Nothing; they are added when they appear. |
| the selection matches no file of the torrent · episode *N* is in several seasons — write it as S01E05 | The selection cannot be applied safely. | Edit the topic's selection. |
| waiting for disk space: … GB short (needs … GB, … GB free in …); TOW starts it by itself when there is room | The new version's files do not fit on the target drive (measured only for a torrent client on this computer). It is in the client, stopped, with its files selected. | Free the space it names (or choose fewer files for the topic): TOW starts the torrent within minutes. |
| not enough disk space: … GB needed, … GB free | Shown by TOW 1.27.1 and earlier, which did not add such a torrent. | Free space or choose another folder. |
| no site is set up for this link | The link belongs to an unknown site. | **Sites → +**, paste the link. |
| the torrent was removed from the client; press the row's check icon to add it back | The torrent is gone from the client. Scheduled checks and **Check all** only report it, so a torrent you removed on purpose does not come back by itself. | Press the row's check icon: TOW adds the current version again (stopped, with its files chosen, confirmed, then started; or waiting for disk space). Or **Remove from TOW**. |
| the torrent in the client was not added by TOW (it has no label tow), so TOW cannot check a partial file selection there — … · the torrent in the client was not added by TOW — its file selection was not changed | The torrent has no `tow` mark, and TOW does not change torrents it did not add. | If it is the topic's torrent, open the topic and choose **Adopt into TOW** (or `tow adopt ID`): TOW marks it and changes nothing else. Or change it in the client, or remove it there and let TOW add it. |
| the torrent is already in the client without the label tow, so TOW cannot manage it — … · the torrent was added (paused), but the client did not keep the label tow on it — … | The torrent has no `tow` mark: you or another program added it, or the client could not set the mark while adding. TOW does not change such a torrent. | Open the topic and choose **Adopt into TOW** (or `tow adopt --all-unmarked` for many): TOW marks the torrent, changes nothing else, and manages it from the next check. After the second message the button appears once the next check has found the torrent. Deluge keeps one label per torrent: one with a label of yours is adopted only with `tow adopt --replace-label ID`, which replaces that label with `tow`. Or remove it in the client and let TOW add it again. |
| another check is running right now; press “Check” again in a minute | One check at a time. | Wait. |
| TOW cannot read its settings file config.yaml (a page instead of TOW) | `config.yaml` has an error; the page names the line. | Correct `config.yaml` in the TOW folder at that place and reload the page: TOW reads it again by itself. If that does not work, copy `config.yaml` back from the newest nightly backup (`backup/night`) and reload. |
| TOW is not started: port 8787 is already in use by another program. | Another program (often a second TOW) uses the port. | Stop that program, or set another `port:` in `config.yaml` and start TOW again. |
| Scheduled checks have not run since … | TOW was not running, or checks keep failing. | `tow autostart status`; look at `data/logs/run.log`. |
| The last scheduled check is blocked: saved passwords and tokens cannot be opened. … · The master key is missing … | The master key is missing or wrong. | `tow keys status`; put back `keys/master.key` from your copy, or `tow keys adopt --from FILE`. |
| state.json was written by a newer TOW … | You went back to an older version. | Update TOW again, or restore a backup made by this version. |
| TOW was not started: an update was cut off while it replaced the code … | An update stopped half-way (the computer turned off, the updater was killed). | Run the update again (`Update TOW`): it puts the previous version back first. |
| Sign-in from other devices is off — TOW opens only on its own computer. | Network access is off. | Turn it on at the computer running TOW. |
| No password for other devices is set yet. | Network access is on, but there is no password. | Set it on the computer running TOW: Settings → Network access. |
| Warning: other accounts on this computer can change the TOW folder … · … can open … | Other accounts may get into the TOW folder (an install in `C:\TOW` made from an administrator terminal belongs to Administrators, so TOW cannot close it at start). | `tow permissions` shows why. `tow permissions fix` closes it; when another account owns the folder, run it once in a terminal opened with **Run as administrator** (Linux, macOS: with `sudo`): the folder becomes your account's — the one autostart runs as — and nothing outside it changes. When you typed another account's administrator password to open that terminal, name your own account: `tow permissions fix --owner PC\name`. |

Still stuck: **Diagnostics** checks the client and every site; `tow doctor` does the same from the command
line. The event log behind History and the Log window is `data/tow.jsonl`; `data/logs/` holds `run.log` and the
logs of the web server and of each scheduled job. When you report a bug, remove personal data first.

## Command line

`tow` is the launcher in the TOW folder: `app\scripts\tow.cmd` on Windows, `~/TOW/app/scripts/tow` on macOS and
Linux. The everyday commands are in the [README](../README.md#usage); `tow --help` and `tow COMMAND --help` explain
every one. The others:

| Command | Does |
|---|---|
| `tow version` | prints the version |
| `tow start --no-browser` | starts TOW in the background without opening the page (also `TOW_NO_BROWSER=1`) |
| `tow check` | a preview of a check that changes nothing; `--apply` hands what it finds to the client and saves it, `--dry-run` keeps it a preview even with `--apply`, `--notify` also sends the messages |
| `tow doctor --notify` | asks the torrent client and the sites, and sends the report to the messengers too |
| `tow watchdog` | is TOW up and are the checks running (a diagnostic; it changes nothing) |
| `tow backup` | makes a nightly backup now (as **Back up now** does) |
| `tow access status` | whether network access is on; `on` opens it after `tow restart`, `off` closes it at once |
| `tow export --output FILE` | the settings, topics, history, passwords and tokens in one file protected by a passphrase; `--include-log` adds the log, `--force` replaces an existing file |
| `tow import --input FILE` | a preview of restoring that file; `--apply` restores it; `--path-map OLD=NEW` changes the start of the topics' folders (another computer; can be given several times) |
| `tow import-rollback --checkpoint FOLDER` | puts back what an import replaced (a preview unless `--apply`) |
| `tow secrets status` | where the passwords and tokens are and which key opens them (`migrate` and `generate-key`: see `--help`) |

Commands typed in a terminal answer in the language chosen in Settings → Language, or, when it is automatic, in
the operating system's language. Messages and the texts scheduled checks record use the language of messages: the
chosen one, or the one your browser last asked for.

## Where things are

| Path (inside the TOW folder) | What |
|---|---|
| `config.yaml` | Settings and sites, without passwords. |
| `data/state.json` | Topics and their state. |
| `data/download_history.json` | Files of every version. |
| `data/secrets.enc` | Passwords, tokens and cookies, encrypted with the master key. |
| `data/tow.jsonl` | The event log behind History and the Log window (old parts are deleted by themselves). |
| `data/logs/` | `run.log` and the logs of the web server and the scheduled jobs (old parts are deleted by themselves). |
| `keys/master.key` | The master key. Never in any copy. |
| `backup/` | Nightly backups and pre-update snapshots. |

An empty password field in any form means “keep the saved one”; saved passwords are never shown.
