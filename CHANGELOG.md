# Changelog

All notable changes to TOW. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [semantic versioning](https://semver.org/). Русская версия: [CHANGELOG.ru.md](CHANGELOG.ru.md).

## [Unreleased]

### Fixed

- `history_keep_days` and `history_max_items` in `config.yaml` are checked like every other setting: a word or a
  negative number is named when TOW starts instead of quietly becoming the default, and a huge number of days no
  longer makes every check fail.
- A `heartbeat_url` without a server name (`https://` alone, or with a space in it) is named when TOW starts; it
  was taken, and then every ping of the watchdog failed.
- Settings → Checks: a field left empty is refused ("Minutes: enter a whole number…") instead of quietly becoming
  60 minutes or 1 minute with "Saved", and a number written with other digits ("٦٠", "６０") or "1_000" is refused
  too. Changing one of the two fields no longer changes the other: an interval or an Undo time from `config.yaml`
  that the form cannot show (10 minutes, 45 seconds) stays as it is.
- Settings → Torrent clients: a Transmission or Deluge saved with the port field empty gets its own default port
  (9091, 8112), not qBittorrent's 8080. A port written with other digits ("٨٠٨٠") or as "8_080" is refused.
- Settings → Backups → Keep nightly backups, days: a number written with other digits ("٧", "７") or as "1_0" is
  refused instead of being saved as 7 or 10.
- Settings → Language and Theme: a language TOW does not have (such as one whose file was removed while the page
  stayed open) or a theme other than the three offered is refused with a message instead of being saved as
  automatic with "language saved" or "theme saved".
- Choosing the files of a topic (and a magnet preview) asks the site with the same User-Agent as a check: the one
  in `config.yaml`, else TOW's browser string. Without `user_agent` it said only "TOW", which a site may refuse
  while its checks work.
- When `config.yaml` has a `bind` that opens TOW to other devices while `allow_lan` is off, TOW says so in the
  chosen language and names the way out (`tow access on`, or `bind: 127.0.0.1`); a Russian start line carried an
  English sentence that pointed to Settings, which could not open.

## [1.28.2] — 2026-10-09

### Changed

- Topics a check does not ask because of their site (all mirrors resting, the daily limit reached, the site frozen)
  are one line per site and check in History and the event log ("site not asked · topics not checked: 150"), not one
  "check failed" line per topic; each topic keeps its error on Home. With 150 topics on a resting site these lines
  were half of `tow.jsonl`, and its rotation kept hours of history instead of days.
- A check asks qBittorrent much less: the torrents of all topics come from one list per check instead of one
  request per topic, and its Web API version is asked once instead of before every add, start and stop. A
  confirmation after an add or a change is still read from the client itself.

### Fixed

- Restore points, exports and the web update are no longer refused ("plaintext secret-shaped field outside
  encrypted payload: state.json:notify_lease…token") while a messenger delivery is in progress, for example while
  a messenger server cannot be reached: the delivery's claim is named `owner` now, and one an older TOW left is
  recognised in its exact form. A real credential in state, config or history is still refused.
- A Windows zip install that began with an older zip no longer keeps that zip's `Start TOW.cmd`, `Stop TOW.cmd` and
  `Update TOW.cmd` for good: an update writes the new version's files into the TOW folder, before it replaces the
  code and again after. With a 1.22 `Update TOW.cmd`, running the update again after a cut-off update failed with
  "can't open file …\app\scripts\update.py"; the guide says how to recover such an install by hand.
- `Start TOW.cmd` and `Stop TOW.cmd` say that an update was cut off and to run `Update TOW.cmd` again, or that TOW's
  code is incomplete, instead of "The system cannot find the path specified" followed by "TOW did not start: the
  reason is above" with no reason above.
- On an install without git, `Update TOW.cmd v1.21.0` says that such an install goes no further back than v1.22.0,
  and a tag that does not exist (`v9.9.9`) that it is not a release of TOW: both said "the release has no checksum
  for the source archive". `tow update --ref v1.21.0` says the same instead of suggesting a command that is refused.
- An update that refuses to go back to a version that cannot read the data names the version to go back to at most
  (v1.23.0 for today's data) instead of "the version that wrote it".
- An update keeps the oldest update snapshot in `backup/` besides the newest five: a few cut-off and repeated updates
  no longer delete the only copy of the data from before the first update.
- After the TOW folder was moved, `tow setup` (and the first start in the new place) makes uv's link
  `runtime\python\cpython-3.14-…` again inside the folder: it still pointed at the old folder.
- A topic whose link or torrent client you change while a check runs no longer gets the old link's torrent: the
  check saves nothing for it, hands nothing to the client once it sees the change, and the next check adds the new
  link's torrent. Before, the topic kept the old torrent's hash and the next check filed it as a previous version.
- The temporary file of a save that TOW was stopped in the middle of (`data/.state.json.*.tmp`, a full copy of the
  state, and those of the secrets, the download history and `config.yaml`) is removed the next time TOW saves
  anything, once it is a few seconds old. Before, such files stayed forever.
- Pausing, editing or deleting a topic, pausing a site, choosing its mirror or saving a setting while another program
  (an antivirus, a backup tool) keeps TOW's data file open says that the file could not be written and to try again,
  on the same page; TOW also waits a little longer (about 4 s) for the file. Before, it was a server error.
- A Cloudflare check or a slow answer on a few topic pages no longer pauses every mirror of the site: only a
  mirror's own failures count towards its cooldown (a refused or dropped connection, no answer, a server error, a
  Cloudflare check on its front page too), and such a topic shows its own yellow error. Before, two topics behind a
  check and a slow one in a row paused all topics of the site for an hour.
- Scheduled checks keep their interval: the next one was due an interval after the previous one actually started,
  so each started a fraction of a second later than planned (+0.25 s per check, without end). Now it counts from
  the planned start; a check that started more than an interval late (after a long stall) counts from then, without
  a burst of catch-up checks.
- While a new version waits for its previous torrent to stop ("the previous version of the torrent is still
  active"), scheduled checks no longer download its `.torrent` again at every check when the topic page's magnet
  still names it, or, on a page without a magnet, when the site has a daily download limit; and the wait is logged
  once instead of "add started", "add failed" and "check failed" at every check. A newer upload named by the magnet,
  a check you start and the stop of the previous torrent still download the current `.torrent`.
- A site name that answers with an IPv6 form of 0.0.0.0 (`64:ff9b::`, `::ffff:0:0:0`) is refused like other
  non-public addresses: before, TOW took it for a public internet address.

## [1.28.1] — 2026-10-08

### Fixed

- macOS: turning autostart off no longer reports a failure while launchd is still unloading the agent.
- `tow backup`, `restore-snapshot`, `import-rollback`, `password`, `update`, `secrets`, `keys`, `run`, `stop` and
  `restart` explain themselves with `--help`, as `tow --help` promises; in Russian the help no longer mixes in
  English words ("usage:", "options", "show this help message and exit").
- The Guide in Settings says that rutor's mirrors rest 30 minutes, that the check icon in a site's row ends the
  pause, that a check you start still tries a site at its daily limit, and where both logs are.
- A session cookie with a non-ASCII digit (such as "²") is treated as no session: before, every page before sign-in
  and signing out answered with a server error.
- A number in `config.yaml` written with non-ASCII digits ("²") or thousands of digits is named as a wrong value,
  and a `config.yaml` saved in another encoding (Notepad's ANSI) says to save it as UTF-8: before, both gave a
  server error on every page.
- A tracker page that names a charset that is no text encoding (`undefined`, `unicode_escape`, `utf-7`, `base64`…)
  is read as UTF-8 or windows-1251: before, filling in a new site from its topic link failed with a server error,
  and a page title with half a character lost the check results of every topic.
- A magnet link on a topic page with half a character in it is refused before the torrent client ("could not get the
  file list") instead of a server error when the files are listed.
- A topic page with a link that has no valid address (`http://[x/...`) no longer makes filling in a new site from
  its topic link fail with a server error; a topic link the parser refuses gives no title instead of an error.
- A torrent whose file names declare long episode ranges (`S01E0001-E1000` in each of 20,000 names) is read in about
  a second instead of minutes and gigabytes of memory: a file name's range counts in full up to 100 episodes, and a
  torrent whose names declare more than 50,000 episodes in all counts each file as its first episode.
- A check of a topic page of several megabytes of markup takes about a second instead of up to 15 s: its title is
  read from the head of the page, and the page is scanned for its posts and download block once instead of four or
  five times.
- A damaged export or restore point (a broken zip inside the encrypted file) is refused as a damaged archive:
  before, some kinds of damage stopped the import or the cleanup of old restore points with an unexpected error.

## [1.28.0] — 2026-10-08

### Added

- The add and edit forms show the free space of the chosen folder and, once the chosen files are known, whether
  they fit.

### Changed

- Home remembers the chosen order on this device: it stays after a reload, the Home icon or a new tab.
- A new version whose files do not fit on the target drive is added stopped instead of refused (its `.torrent`
  was downloaded again at every check): TOW says once what is missing and starts it by itself when there is room.
- Pages are faster with many topics and sites: with 2000 topics and 200 sites Home, Settings, History, a history
  search and the header's status poll take a fraction of the time they took.

### Fixed

- Changing the network password or **Sign out everywhere** when TOW cannot write `data/sessions.json` says so
  (the password is saved; this device signs in again) instead of a server error.
- `tow permissions fix --owner` in an ordinary terminal says that only an administrator can name the owner and
  changes nothing; before, the option was ignored without a word.
- A moment when Windows held `data/sessions.json` for another writer no longer signs every device on the network
  out at the next sign-in; only a damaged file is reset.
- With many sites the header no longer makes the page wider than the screen and pushes Settings off it: the site
  names scroll sideways between the sections and the controls, and the keyboard reaches them.
- A screen reader hears the state of each site and of the torrent client in the header, and a failed check
  of the countdown, in words: before, only their colour told it.
- Home with hundreds of personal timers redraws each second only the timers on screen, and only what changed;
  a screen reader reads a timer's meaning from its text instead of a label rewritten every second.
- The log in the header and in Settings and the episodes list can be scrolled with the keyboard: each box takes
  the focus and has a name.
- Links inside sentences (notes, hints, the Guide) are underlined, not told from the text by colour alone.
- On a phone a long topic name wraps onto the next line like the folder and the error, instead of ending in "…".
- A Home row's progress ("3/10") is a link of its own that the keyboard and a screen reader reach; it was inside
  the row's toggle. It looks and sits as before.
- A screen reader hears each Home cell with its column ("Site: Kinozal, Folder: …"), on a desktop too, and finds
  the version at the end of the page as its footer.
- Esc folds an open Home or Sites row from inside its edit panel and puts the focus back on the row; what was
  typed stays.
- After pausing, checking, saving or removing a topic or a site, or an action in the header, the focus comes back
  to the same row and button (a removed row's neighbour, else the message) instead of the start of the page.
- A screen reader announces the message an action leaves (an error as an alert), and the message and its Undo
  no longer vanish while the pointer or the focus is on them: they go once both have left.
- Settings → Theme saves a choice in the background: moving between the options with the arrow keys no longer
  reloads the page at every step and keeps the focus on the option; the section's pill names the choice.
- Home with thousands of topics opens faster (it no longer re-orders the rows into the order they already have)
  and typing in the search no longer stalls at every key: the list follows after a short pause.
- Scrolling a long Home is smoother: the version badge looks at what lies under it once a scroll or a change has
  settled, at five points, instead of fifteen points on every frame.
- A site that sends the .torrent's file name in Cyrillic (raw UTF-8 in its headers) no longer fails every download,
  and TOW failing on a site's answer no longer pauses the site's mirrors.
- When a site answers the .torrent with a sign-in page and its page has no magnet link either, the row keeps the
  site's reason (sign in) instead of "the page has no valid magnet link", and the site icon is amber for a sign-in,
  as the Guide says.
- A check of a topic's row adds its torrent back when it was removed from the client, as the Guide says (stopped,
  with its files chosen, confirmed, then started); scheduled checks and Check all only report it.
- Adding a forum site from a topic link (Sites → +) takes the download path from the topic page's own .torrent link
  (dl.php?t=… instead of a guessed download.php?id=…), and a site on an IP address or localhost is named by its
  port (site_8080) instead of "0".
- After a new version of a topic, Home's latest event names the new episode, not one of the episodes the version
  carried over.
- A failed check of a topic TOW already had (the row's check, "Stop the previous one and add") no longer says
  "Added to TOW, …", and a successful "Adopt into TOW" says the torrent was adopted instead of "No changes".
- Header and Sites follow what TOW last saw: a site's name takes the latest check's answer, not only Diagnostics
  (it stayed "not checked yet" after any number of checks); the client chip and the Settings pill turn red right
  after Check finds a wrong password; and a new topic whose site gave the torrent but whose client failed shows the
  site icon green instead of grey.
- Diagnostics and a site's mirror check count a mirror that answers with a missing page (HTTP 404 at the site's
  root, as many trackers do) as answering, with its status, instead of "not a single mirror answers".

### Security

- A device on the home network can no longer fill the TOW folder through the sign-in page: every form is
  limited to 512 KiB and must say its size, and sign-in takes only a plain form (before, a large file sent to it
  was stored in `data/tmp` until the end of the request).
- The web server no longer takes the client's address or scheme from `X-Forwarded-For` / `X-Forwarded-Proto`
  headers, even with `FORWARDED_ALLOW_IPS` set: a program on this computer could make a request look like it
  came from another address, and with `FORWARDED_ALLOW_IPS=*` a device on the network could look local.
- On this computer TOW now answers only `localhost` and loopback addresses (127.0.0.1, [::1]): a device on the
  network could answer LLMNR or mDNS for the computer's name and let a site's page reach TOW through it without
  a password. Other devices may still use the computer's name.
- Windows: TOW runs PowerShell, Task Scheduler (`schtasks`) and `taskkill` from the Windows folder
  (`%SystemRoot%\System32`), never a program of the same name that a search of PATH would find first.
- Site, tracker and messenger addresses that resolve to a multicast group (224.0.0.0/4, ff00::/8) or to an
  IPv4-translated IPv6 form of a home-network address (`::ffff:0:192.168.1.1`) are now refused like other
  addresses of the home network.
- A tracker's answer to the sign-in is read like a topic page, at most 4 MiB: a site (or a page between TOW and
  it) can no longer make TOW read an answer of any size into memory.

## [1.27.1] — 2026-10-08

### Fixed

- `tow.cmd` and `scripts/tow` in a copy of the TOW folder no longer run the original folder's code while the
  original is still there: they say to run setup first, as the start files do.
- TOW itself also refuses to run another folder's code for its folder (a copy's autostart, a command started
  without a launcher): it names both folders and says to run setup, before it writes anything there.
- A copy of the TOW folder started at its old port no longer stops the original folder's running web server,
  taking it for a server its own TOW left behind.
- A `TOW_ROOT` variable left from a move (the old folder) no longer makes TOW start with empty data and a new
  master key in the old place: the launchers and TOW use the folder TOW is in, and `run.log` says to remove it.
- On Windows the TOW service status in Settings no longer stays out of date after Settings or `tow status` read
  it at the moment TOW rewrote it, and no temporary file is left next to it.
- An autostart left by a TOW folder that was moved or deleted is now named in Diagnostics, Settings → TOW service,
  `tow status` and `tow doctor`, with the way to take it over (turn autostart on here); before, nothing said that
  the computer kept starting a program that no longer exists.
- Linux and macOS: `scripts/tow` finds an install whose code folder is named `App` (any case), as TOW itself
  does; before, it took such an install for a development checkout, and setup put Python and uv's cache outside
  the folder.
- Windows: Ctrl+Break in the console of `tow run` stops TOW cleanly, as Ctrl+C does, instead of ending it at once.
- `data/logs/serve-stderr.log` no longer grows for ever: at 5 MiB it becomes `serve-stderr.log.1` when the web
  server starts again (one older file is kept).
- Windows: an error of Task Scheduler or PowerShell when autostart is turned on or off is shown readable in a
  non-English Windows, instead of garbled letters.
- Windows: an update of an install in a folder with Cyrillic letters (such as `C:\Users\Иван\TOW`) recognises
  the web server a stopped TOW left behind and stops it, instead of failing with "port … is still in use after
  stopping TOW".
- Windows, git install: `scripts\deploy.ps1` runs in Windows PowerShell 5.1 too (it demanded PowerShell 7) and in
  a folder with `[ ]` in its name; the guides and `tow update` say how to run it when PowerShell refuses scripts.
- `Update TOW.cmd`, `update-tow` and `Update TOW.command` of new installs run the update with the Python TOW's
  environment was made from, else the newest one by version number (3.14.10 after 3.14.8), never uv's link folder.
- Windows: a TOW folder whose path is longer than 110 characters, with long paths off, is refused by `install.ps1`
  before it writes anything, and `Start TOW.cmd` says so before it prepares TOW, instead of an unexplained failure.
- `install.ps1` says at once that Windows on ARM is not supported (the zip is for x64 PCs) instead of installing
  the x64 zip there; the install guide says so too.

## [1.27.0] — 2026-10-08

### Added

- Settings → Theme: as the system, light or dark, for every device; the page changes at once.

### Changed

- One icon set on every page (the same line width and sizes); Settings is a gear.
- The header keeps the sections and the page's own actions: Diagnostics, the Guide and **Sign out on this
  device** moved to the end of Settings.
- Messages about a copy made with **Create a backup** call it a restore point everywhere; a damaged data file
  points to Settings → Backups.
- The copies made before an update have one name each: the restore point TOW saves first and the update
  snapshot the updater takes (no more "data archive" or "safety copy"); the copy kept before a restore is the
  safety copy.
- An undo that could not be applied says so ("could not undo the change") instead of calling it a rollback, which
  names only going back to an older release.
- Clearer wording: the sign-in field is **User name**; the ntfy field is **ntfy topic name**; the guide and README
  call trackers sites; update errors point to **How to update** and the **Update log**; jargon such as
  "read-back", "Turnstile" and "contacts peers" is gone; Diagnostics names autostart as Settings does; the Home
  sort is **Errors first**, like its filter.
- Small text fixes: "Saved: N" instead of "N saved", a capital letter in the bell's label and in **← Settings**,
  "Checked:" after the version status, dashes, arrows and ellipses in the updater and config messages, "no"
  instead of "NO" in the diagnostics report.

### Removed

- The Monitorrent import (`tow import-monitorrent`). Topics already imported stay as they are; a torrent in the
  client without the `tow` mark is still taken over with **Adopt into TOW** or `tow adopt`.

### Fixed

- `deploy.ps1` written to a file or passed to another program no longer garbles Russian text and the ellipsis.
- A topic's error line names the site by its title and does not repeat itself ("All mirrors are paused", not
  "Mirror paused: nnmclub: all mirrors are paused"); a long one ends in "…"; "Find on …" names the site too.
- Edit offers the same choices, in the same words, as Add; the event's time sits under the event.
- The header search closes with Escape or a click elsewhere; the Log and Downloads windows are as tall as their
  lines and close with the same icon; an empty History says where routine events are.
- Settings → TOW service says when this account's autostart belongs to another TOW folder, as Diagnostics does;
  Diagnostics says "Not set" and "Not connected" instead of a bare "No".
- Layout: check boxes in line with their labels, the backup Save button and pills at their own width, a site's
  mirrors clear of its icons on a phone, long help patterns wrap, Diagnostics cards spaced, the version badge
  steps aside for pills and comes last in the Tab order.
- A second client still loading its torrents after a start is no longer believed empty (and added to again) when
  a check in between did not ask it.
- A broken topic page of a few megabytes (tags or posts never closed) no longer holds a check for minutes.
- Deluge with several daemons: when its Web UI has to be attached again, TOW attaches it to the same daemon, not
  to the first one online.
- A windows-1251 page whose server names only its latin-1 default (and the page no charset) is read as Russian,
  not as "Ñåðèàë".
- A mirror answering with an error status and Cloudflare's `cf-mitigated: challenge` header is reported as a
  Cloudflare check, in a check and in Diagnostics, not as an HTTP error.
- Undoing a password change from another device no longer fails with a server error when TOW cannot read
  `data/sessions.json`; the undo is done and that device signs in again.

## [1.26.0] — 2026-10-08

### Added

- `tow permissions fix --owner ACCOUNT`; without it the fix keeps a personal owner, then takes the autostart
  account, and refuses when the administrator terminal belongs to someone other than the signed-in user.
- `tow adopt --replace-label` for a Deluge torrent that already has another label.
- Home and History say when the data folder was deleted or replaced while TOW ran, and how to restore it.

### Changed

- A torrent or magnet link is taken only from the topic's own download block, never from a post or comment; a
  signed-in page without it says "page not understood" instead of asking to sign in.
- Adopting takes only the torrent the last check saw for the same link and client; `tow adopt` names an
  unreachable client or unknown topics.
- The Monitorrent import brings the hashes of every site, keeps paused topics paused, moves old site addresses to
  today's, names topics it cannot watch, and imports a Transmission connection.
- Moving a topic's folder in the client no longer holds up other saves.
- Messages are plainer: one instruction for a broken `config.yaml`, a taken port named once, the Version card and
  `tow update` in plain words, `tow doctor` in your language, folder warnings once and only when TOW starts.
- The built-in guide covers updating, adopting, signing out and folder access.

### Fixed

- An emptied or new torrent client is believed after two checks; 1.25.0 could keep refusing to add forever.
- A new topic is not saved with a client removed or disabled in another tab meanwhile.
- A damaged sign-in file no longer locks every device out; a device whose session ended is sent to the sign-in
  page instead of getting a bare error.
- A cut-off update is reported by every launcher instead of a raw error; `tow status` says so.
- WhatsApp messages that were delivered are not sent again; a Telegram group upgraded to a supergroup names the
  new chat id; Discord shows titles as written; the watchdog keeps the quiet hours, and impossible quiet hours
  are refused.
- Transmission and Deluge at an IPv6 address keep their port; Deluge reattaches after an unknown method.
- Ordinary pages are no longer taken for a Cloudflare check; page encodings are read correctly.
- A client that answers "Check" shows as answering at once.

### Security

- `tow permissions fix` never gives the TOW folder to an administrator by mistake.
- A password reminder that contains a punctuation-only password is refused.

## [1.25.0] — 2026-10-07

### Added

- **Adopt into TOW**: a topic whose torrent is already in the client without TOW's mark can be taken under TOW's
  management from its row, with `tow adopt` or `tow import-monitorrent --adopt`; only the mark is added.
- `tow permissions` shows who can get into the TOW folder; `tow permissions fix` closes it (from an administrator
  terminal when another account owns the folder).

### Changed

- The TOW folder itself is closed to other accounts of the computer, not only `keys` and `data`; folders are
  compared by account, and only folders of your account are changed.
- Saving is never held up by a slow check, client test or sign-in: those run on their own.
- A manual client check fails in about two seconds when nothing answers, and long buttons say what they are doing.
- Checks are about four times faster with many topics; Home, the topic panel and History open faster.
- Site transport trouble is reported after three failed checks in a row, not on every flap.
- A site signs in again by itself when its session expires; a Cloudflare page or a removed topic no longer
  triggers a sign-in on every check.
- One word for each thing in both languages, plain Russian, and every error says what to do next; a missing or
  wrong master key is named as such.
- A broken `config.yaml` shows a page that names the line and how to recover, instead of a server error.
- `tow start` says why TOW did not start; another TOW on the same port is never taken for this one.

### Fixed

- A legacy site name (with a dot, Cyrillic, long) no longer breaks restore points, export, import or the update
  from the page; loosely written site settings (`'1800'`, `'true'`) load again.
- Restore points and the update from the page work with a large download history.
- A cut-off update never puts back the data of the previous version over newer data, and TOW never starts on
  half-restored files; the start files follow the same rule as `tow run`.
- A message that was delivered is never sent again; a timed-out message is not resent in the same round.
- A torrent left without TOW's mark after a failed add is never shown green.
- A new site from a pasted link gets the right download path again.
- `S02E01.x264` is one episode, and "2 сезон 3 серия" is season 2.
- A topic edited while a check runs is left to the next check.
- The Monitorrent import skips rows that cannot make a usable topic and counts them.
- The update from the page is refused under systemd or launchd autostart instead of stopping TOW for good.
- `tow setup` works after copying the TOW folder; the autostart task with Cyrillic in its path is read correctly.
- A data folder deleted while TOW runs is reported.

### Security

- A password reminder that shares four characters with the password is refused; changing only the reminder
  needs the current password.
- A network-share folder for backups can be chosen only on the computer running TOW.
- A forged sign-out cannot sign every device out.

## [1.24.1] — 2026-10-07

### Fixed

- `tow status` writes the last and the next check in the same language and format.

## [1.24.0] — 2026-10-07

### Added

- **Sign out on this device**: a button in the header of a phone or laptop signed in over the network.
- Home says when a filter matches no topic and offers to show them all.
- The guide has an Updates section: updating from the page, what is kept, going back, a cut-off update.

### Changed

- Commands typed in a terminal answer in the language of Settings → Language, or with automatic, in the
  operating system's language.
- `tow doctor` writes its findings in words and exits with 2 for any finding, a missing torrent client included.
- One word per thing in both languages: Russian says «пароли и токены», «доступ по сети» and «Раздача» for the
  Home column, and Home and Diagnostics name sites by their title (NNM-Club, Rutor) as the Sites page does.
- Sizes use one unit system (KB, MB) and dates the language's format everywhere, the update card included; the
  file picker says "Files selected: 1".
- Updating to `latest` when it is already installed changes nothing; a `latest` older than the installed version is
  refused. A target version that cannot read your data is refused before TOW stops.
- Night copies: the three newest earlier copies are always kept, however old, so a computer that was off for a
  long time keeps some history. Restoring a night copy keeps History as it is.
- Backup messages name the contents in words, say why a folder or file was refused, and tell apart a file that
  is not a TOW backup, needs another master key, is too large or is damaged.
- Checking a Transmission or Deluge client words a failure like qBittorrent's; Get started shows the same client
  state as the header; Diagnostics say in words that a mirror's address is not found.
- On a phone Home rows wrap instead of being cut off, and the version badge no longer covers text.
- A removed topic shows one reason on Home; History shows a resumed topic or site as resumed.
- A saved `config.yaml` names the commented reference that explains every setting.
- A `config.yaml` with a site setting of the wrong type (for example `browser_auth: "false"`) is refused at
  start; a broken `config.yaml` is named with its line and column in your language.
- `tow export` and `tow import` say why they refuse in your language.

### Fixed

- An update interrupted with Ctrl+C or a closed terminal while it switches the code is rolled back first.
- `tow run`, `tow start` and the start files refuse to start over an update cut off mid-switch and say to run the
  update again; that run puts the previous version back first, which now works from `Update TOW.cmd` and
  `update-tow` too. If undoing fails, it says so and starts TOW again.
- Undoing a cut-off update never puts the old snapshot over data changed later; it names the file and the
  `--discard-newer-data` option instead.
- `update.py` started with Python 3.10 says which Python it needs.
- Night copies: a copy that fails its check is removed at once, folders a killed copy left are removed by the next
  one, and every failed copy is recorded (Back up now no longer ends in a server error).
- Restoring a night copy: a restore that fails before it starts leaves nothing behind, a copy can replace a broken
  `config.yaml`, and a copy deleted by hand no longer breaks cleanup monitoring.
- Creating a restore point decrypts only the old points it removes, so it is much faster.
- An import interrupted before the install was moved (another drive letter) recovers instead of blocking saves.
- A legacy night copy with long `1:2:3…` numbers or impossible values is refused quickly instead of hanging or
  failing with a server error.
- Forms: saving the edit of a topic deleted meanwhile is refused; a pasted topic link keeps fields typed by hand;
  an invalid client address, port or check interval is refused.
- Sites: a link pattern without a number group or a path without `{id}` is refused; a site or ntfy address that
  does not resolve now is saved with a warning instead of being taken for a home address.
- A local `.torrent` only previews the contents: the first check still gets the torrent from the site. A chosen
  file and an abandoned preparation are dropped when the link changes or the form is cancelled.
- The content picker shows readable messages for an expired sign-in or a too large file, and "Choose files" is
  offered without JavaScript only where it works.
- The sign-in page and the CLI show the password reset command with the right path in an installed TOW.
- A manual release check no longer holds up saves in other tabs; Home with many topics stays responsive.

### Security

- Windows: `keys\` and `data\` are closed to other accounts at every start, also when an administrator session
  created them; `tow doctor` names a folder that stays open, and `install.ps1` closes them before the first start.
- A magnet preview gives the client only the info-hash and public trackers.
- Passkeys in magnet and `udp://` tracker addresses no longer reach logs, rows or messages.
- Only web addresses reach the sign-in browser; settings and imports accept only http(s) site addresses, and
  names that could change a page address are refused.
- IPv6 forms of local and home-network addresses are recognised as such.
- A password reminder that is the password with other punctuation is refused.
- The update log shows the install folder as `<TOW>`.
- Backup folders refuse Windows device names, streams and administrative shares.
- A messenger's answer is read up to 1 MB.
- `install.sh` runs nothing until it has been downloaded completely.

## [1.23.11] — 2026-10-06

### Changed

- Opening the contents of a watched topic reuses its saved, encrypted torrent metadata instead of asking the
  site again; a changed magnet link makes TOW fetch it anew.
- The version link opens a compact Version and updates panel with history, rollback and the log folded away.
  Home shows a small green notice for a new release; automatic release checks can be turned off.

### Fixed

- Saved metadata that could not be read once is not reused after the topic got a new revision.

## [1.23.10] — 2026-10-06

### Fixed

- Links into the guide no longer hide their heading under the header.
- File patterns keep matching the original file names after a client replaces unsupported characters in them.
- Progress of file rules understands the torrent folder the client reports.
- With "All files", progress counts every video of the torrent: an episode switched off in the client, or a rule
  not applied yet, cannot make a season look complete.
- A file with episodes of several seasons keeps all of them in selection, progress and history.
- Episode names such as `S01x08`, `S01xE08`, `S01_E08` and `S01.E08` are recognised.
- Diagnostics show the current Python, topics, connections and autostart after an update or an edit.
- Error pages carry the same security headers as other pages and are not cached.
- The guide tells personal topic timers from the global timer, no longer says the watchdog restarts TOW, and
  describes the seven-day default and what a night copy keeps readable.

## [1.23.9] — 2026-10-06

### Fixed

- A torrent where a file is also another file's folder is refused with an explanation: such paths could hide
  files in the file picker.

## [1.23.8] — 2026-10-06

### Fixed

- Large sets of file patterns no longer slow down on long, similar paths.

## [1.23.7] — 2026-10-06

### Fixed

- File patterns with brackets no longer slow down large rule sets.

## [1.23.6] — 2026-10-06

### Fixed

- An update in a Windows terminal with a legacy code page no longer stops when it prints non-Latin folder names.

## [1.23.5] — 2026-10-06

### Fixed

- File patterns skip long paths that cannot match before comparing them in full, which keeps large selections
  fast.

## [1.23.4] — 2026-10-06

### Fixed

- The web update log is readable in Russian on Windows with a legacy code page.
- Large sets of file patterns are matched in one pass.

## [1.23.3] — 2026-10-06

### Fixed

- Settings tell the global timer from personal topic timers and say where to set one.
- Long mirror addresses fit the screen without hiding the main-mirror mark.
- On a phone, header controls and long client names no longer overlap the navigation, and links to Settings
  sections scroll clear of the header.

## [1.23.2] — 2026-10-06

### Fixed

- File-rule previews use the same season as checks, also for episodes without a season number; future episodes
  are shown as waiting.
- Selection and waiting messages name episodes as `S02E15`; the file count reads correctly for one file.
- Searching and paging the file list keep the rule messages; long titles and large or overlapping episode rules
  no longer slow down previews and progress.

## [1.23.1] — 2026-10-05

### Fixed

- Pressing Save while the contents are fetched again, or after that failed, no longer brings back the old manual
  selection; Cancel restores the unchanged one.

## [1.23.0] — 2026-10-05

### Added

- Choose files in a contents tree when adding or editing a topic: folders, search, sizes, pages of 200 rows. The
  choice stores paths and sizes; new files stay unchecked, and a changed chosen file asks for review.
- **Get contents** from the site or a local `.torrent`; a site with a daily limit asks first. A refused form keeps
  the prepared choice.
- Preview a magnet link's contents through qBittorrent (Web API 2.11.9+) or Deluge without adding anything; other
  clients need a `.torrent`.
- Cancelling an edit discards the prepared choice.

### Changed

- Data is saved in format 2, which versions before 1.23.0 cannot read: to go back, restore the copy made before
  the update together with the older version. Existing episode and pattern rules keep working.

### Fixed

- Content errors are plain messages, never internal details.
- `.towx` files carry the data format: an older version refuses a newer file before writing anything.

## [1.22.58] — 2026-10-05

### Added

- A personal check interval per topic, set when adding or editing it; empty uses the global one. Home shows a
  clock and countdown. Manual checks move neither timer; missed checks are combined; Undo restores the old timer.

## [1.22.57] — 2026-10-05

### Added

- Night copies and backups made by hand offer the same Check, Restore and Delete. Delete asks to confirm the
  dated copy, also without JavaScript, and removes only that copy.
- Backup lists are folded with their count and size, and list every night copy. Night copies are kept by days
  (7 by default) and can be turned off; older count settings still work. Old copies go only after a new one
  passes its check, and free space is checked before writing.

## [1.22.56] — 2026-10-05

### Fixed

- Restoring a night copy and writing to the event log can no longer interleave, also after a crash mid-restore.

## [1.22.55] — 2026-10-05

### Fixed

- Night copies are written in blocks instead of whole files in memory and reach the disk before they count; a
  failed write keeps the earlier copies.

## [1.22.54] — 2026-10-05

### Changed

- Home filters sites with one dropdown instead of a row of buttons: every site, keyboard use, kept in the address.

## [1.22.53] — 2026-10-05

### Changed

- Home filters sit closer to the list and wrap on narrow screens; sorting is a small icon button with a menu.
  Search, filters and order survive a reload.

## [1.22.52] — 2026-10-04

### Fixed

- The records of an interrupted night restore are size-checked before reading; a damaged one keeps the recovery
  files and blocks writes. Its messages are translated and no longer suggest deleting a marker too early.

## [1.22.51] — 2026-10-04

### Fixed

- Checking a night copy and previewing a restore read large files in blocks; links and special files in a copy
  are refused before reading.

## [1.22.50] — 2026-10-04

### Fixed

- New download folders can be chosen from any of your signed-in devices; system folders stay protected.
- The folder arrow shows the last ten folders used, newest first, also when editing a topic.
- Cancel after a refused add returns to a clean page, also without JavaScript; reloading no longer brings the
  draft back.

## [1.22.49] — 2026-10-04

### Fixed

- Night-copy results from before cleanup monitoring read as unknown, not as a folder failure.

## [1.22.48] — 2026-10-04

### Fixed

- A missing or damaged night-copy cleanup record counts as unknown, not as done. The watchdog keeps the last
  confirmed result, changing the folder no longer clears the old folder's warning, and copies stay listed.

## [1.22.47] — 2026-10-04

### Fixed

- New Windows bundles use Python 3.14.8 and uv 0.12.23; existing installs get the new Python without an
  intermediate update.
- Moving a stopped Windows install rebuilds its Python environment instead of taking the old one for a running TOW.

## [1.22.46] — 2026-10-04

### Fixed

- After an update, an older restore-point cleanup record no longer shows as an unreachable folder; Settings
  explain the older format.

## [1.22.45] — 2026-10-04

### Fixed

- A missing or damaged restore-point cleanup record counts as unknown, not as done. The last confirmed result
  survives watchdog restarts, and Settings show an unknown result while keeping the restore actions.

## [1.22.44] — 2026-10-04

### Fixed

- A web update checks its job record and your data before stopping TOW: a damaged record or state refuses the
  update safely, and a second updater cannot overwrite another one's job.

## [1.22.43] — 2026-10-04

### Fixed

- Damaged backup or watchdog records show an unknown result, not a healthy copy or a false outage.
- A clock set back no longer hides a failed backup or postpones checks, progress and night copies indefinitely.

## [1.22.42] — 2026-10-04

### Fixed

- Checking and restoring a night copy reads its topics, history and encrypted settings like live data; a damaged
  copy is refused before anything is written or older copies are removed.

## [1.22.41] — 2026-10-04

### Fixed

- Windows backup folders like `D:copies` or `D:` are refused with an explanation: use a full path or a folder
  inside the install.
- An oversized or invalid night-copy description no longer breaks Settings or replaces earlier copies.

## [1.22.40] — 2026-10-04

### Fixed

- Windows device paths are refused as backup and download folders, whatever slashes they use; ordinary folders
  and allowed network shares work as before.

## [1.22.39] — 2026-10-04

### Fixed

- Transfer files whose settings are in UTF-16 or UTF-32 are converted to UTF-8 on import, comments kept; broken
  text is refused without changing anything.

## [1.22.38] — 2026-10-04

### Fixed

- Saving settings, restoring a night copy or importing can no longer write a `config.yaml` too large to read.

## [1.22.37] — 2026-10-04

### Fixed

- Oversized or self-referencing YAML is refused before it can slow down settings, backups or restores; ordinary
  anchors still work.
- Night-copy settings are checked before a restore writes; `false`, `0` or `[]` no longer pass as empty settings.

## [1.22.36] — 2026-10-04

### Fixed

- A web update no longer fails to start because a status read briefly held its lock; a job changed or expired
  meanwhile does not start.

## [1.22.35] — 2026-10-04

### Fixed

- Monitorrent import writes topics and logins in one step and puts everything back if writing fails.
- Imported topics go to the chosen client (`--client ID`, or the main one); qBittorrent logins are never copied
  into another client's settings, and logins, ports and recipients you set up are kept.
- A damaged, busy or malformed Monitorrent database is reported instead of being imported as empty.

## [1.22.34] — 2026-10-04

### Fixed

- Removing an old restore point is confirmed; one that cannot be removed leaves a cleanup warning, and the new
  point stays usable.
- Cleanup warnings survive restores, imports and web updates and stay visible after a reload; the watchdog
  reports them once per change.

## [1.22.33] — 2026-10-04

### Fixed

- Old night and safety copies count as removed only once they are gone; otherwise a cleanup warning appears, and
  a usable copy is never called failed. Foreign files, links and unfinished restores are never removed.
- Pending night-copy cleanup shows in Settings, command output and History; messengers report it once per change.

## [1.22.32] — 2026-10-04

### Fixed

- A running web update stays protected for as long as it runs and is released when its process ends, even after
  a crash; an unclear older job is shown with a warning instead of risking two updates at once.
- Settings explain next to the backup file buttons that restoring needs the original master key.

## [1.22.31] — 2026-10-04

### Fixed

- A backup file that cannot be prepared, checked or restored shows the reason in Settings instead of a server
  error.
- A restore whose History entry could not be written is not undone; Settings show a warning.

## [1.22.30] — 2026-10-04

### Fixed

- After an update, rollback or recovery only an older tab offers to reload; dates, errors and the log stay, and a
  manual release check also refreshes the update status.

## [1.22.29] — 2026-10-04

### Fixed

- Stopping TOW, ending a job that ran out of time and restarting the web server wait until the process has really
  ended; when `tow run` crashes, its web server and job are stopped too.
- History says a restart was requested, not done, and messages no longer credit the watchdog with a restart.

## [1.22.28] — 2026-10-04

### Fixed

- Season 0 folders and specials numbered 0 stay files, not ordinary episodes; older history is corrected quietly.
- Episode ranges and file patterns are checked when the topic is saved.
- Invalid progress from a client can no longer mark an episode complete.

## [1.22.27] — 2026-10-04

### Fixed

- After a failed change TOW restores file priorities only once the torrent is confirmed stopped, and resumes it
  only after that.
- Malformed progress no longer counts as a complete torrent; client errors stay errors.

## [1.22.26] — 2026-10-04

### Fixed

- TOW checks that a torrent is still its own before changing, moving or cleaning it up, and never takes over a
  foreign one.
- A file selection starts only after the full file list and priorities are confirmed; a failed change stays
  stopped and keeps the original error.

## [1.22.25] — 2026-10-03

### Fixed

- The update log shows the final lines when an update finished during a slow read.

## [1.22.24] — 2026-10-03

### Fixed

- Repeating a web update after a lost answer follows the new operation and never installs twice. A short status
  failure no longer says the install is unsupported, and an invalid check time is not shown as a date.

## [1.22.23] — 2026-10-03

### Fixed

- Web updates on Windows start even when TOW runs in a restricted process group; a refused update names the
  reason and shows local start and finish times.

## [1.22.22] — 2026-10-03

### Fixed

- The installed version and the new-release badge moved to a small corner indicator that steps aside for
  controls.

## [1.22.21] — 2026-10-03

### Fixed

- A web update on Windows no longer ends together with the web server it stops. The page installs only 1.22.21
  or newer.

## [1.22.20] — 2026-10-03

### Added

- The installed version on every page and a quiet badge for a new stable release; offline means "not checked",
  never "up to date".
- Update or install another version from Settings after confirming: TOW first saves a checked data archive and
  rolls code and data back by itself if the new version fails. Nothing installs automatically; installs started
  by systemd or launchd update from the terminal.

### Fixed

- A failed restore-point export never deletes an existing file; old points are removed only when they are
  verified as TOW's own.
- The update command TOW prints names a release tag for every kind of install, and the service help tells
  watchdog messages from web-server restarts.

## [1.22.19] — 2026-10-03

### Fixed

- Rolling back a night restore checks every saved file first and writes only verified data.
- Cleanup never removes another program's files, unfinished copies or links.
- Damaged saved passwords and tokens give a clear error and are never overwritten.
- An unreadable import record blocks writes instead of letting edits continue over an unfinished import; export
  refuses a quarantined download history instead of writing an empty one.

## [1.22.18] — 2026-10-03

### Fixed

- Recovering an interrupted check, settings save or undo verifies every saved copy before changing anything.
- Damaged state or history is kept aside, never read as empty.

## [1.22.17] — 2026-10-03

### Fixed

- A damaged night copy is refused with a clear error before restoring; copies made within one second no longer
  collide, and automatic cleanup keeps foreign or unsigned folders.
- Restore point lists ignore folders, links and unrelated `.towx` files.

## [1.22.16] — 2026-10-03

### Fixed

- Backup and restore-point folders are checked when used, not only when saved; a relative master-key path
  cannot leave the data folder.
- Transfer files are size-checked while they are read.

## [1.22.15] — 2026-10-03

### Fixed

- Imports refuse invalid numbers and too deeply nested data before changing anything; exports refuse invalid
  values.

## [1.22.14] — 2026-10-03

### Fixed

- Clips marked `Sample`, `Trailer` or `Preview`, also in brackets, no longer block an episode's completion.

## [1.22.13] — 2026-10-03

### Fixed

- Sample, trailer and preview clips are neither counted nor selected as episodes.

## [1.22.12] — 2026-10-03

### Fixed

- Zero, years and video resolutions in file names are not read as episode numbers.
- Invalid progress from a client is not read as 100 %, and absolute file paths from a client are checked as given.

## [1.22.11] — 2026-10-03

### Fixed

- Files numbered `SxxExx` in an ONA release count as episodes, without repeated notifications.
- Growing titles such as `1-31 серии из 52` keep the known total.

## [1.22.10] — 2026-10-02

### Fixed

- A partial episode selection saved without a season (`E14`) counts a finished `S03E14` when the season is clear.

## [1.22.9] — 2026-10-02

### Fixed

- A growing torrent keeps the season of its original title when the new title has an unknown total.

## [1.22.8] — 2026-10-02

### Fixed

- Settings show when the web server really started, and call the older record a requested restart.

## [1.22.7] — 2026-10-02

### Fixed

- Unexpected errors no longer show addresses, logins or internal details in forms, status, the terminal, logs or
  messages.
- Night-copy selection refuses links that lead outside the backup folder; long episode titles are shortened
  before notification patterns are matched.

## [1.22.6] — 2026-10-02

### Fixed

- History, the log and Settings show the current topic name for older events that stored only an id or hash.

## [1.22.5] — 2026-10-02

### Fixed

- A site's custom link patterns have a time limit.
- A site's Open button uses a working mirror when the active one is down; messages after an action never lead
  to another site.

## [1.22.4] — 2026-10-02

### Fixed

- Updates from an archive unpack only ordinary files and folders, refusing Windows device names, drive prefixes
  and streams.

## [1.22.3] — 2026-10-02

### Fixed

- Updates from an archive require the release checksum, limit the unpacked size and put the old code back after
  a hard interruption.
- Installers do not remove folders TOW did not create and keep the earlier config after a failed reinstall.
- Connections to sites, messengers and the heartbeat use only checked public addresses unless private ones are
  allowed; a night copy fails visibly when a file of the previous copy has disappeared.
- Settings show how many notifications were dropped when a recipient's queue overflowed.

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
