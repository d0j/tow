# Security policy

## Supported versions

Only the latest stable release receives security fixes. Update to it before reporting a problem
with an older release.

| Version | Supported |
|---|---|
| Latest stable release | yes |
| Older releases | no — update in Settings → Version and updates, or with `Update TOW.cmd` (Windows), `Update TOW.command` (macOS) or `update-tow` (Linux) in the TOW folder |

## Reporting a vulnerability

Please report privately through GitHub: **[Report a vulnerability](https://github.com/d0j/tow/security/advisories/new)**
(the repository's *Security* tab, private vulnerability reporting). Do not open a public issue.

Include the version, the OS, what an attacker needs (same computer, home network, a malicious site page, a
crafted backup file…) and steps to reproduce. Remove real passwords, tokens, cookies and addresses. You will get an
answer as soon as the maintainer can; fixes are released as a patch version and credited unless you prefer
otherwise.

## What TOW protects

- **Access.** Requests from this computer need no password (from any account on it, see below); other devices need
  the password and a session, and only after network access is turned on. Requests from public internet addresses
  are refused, and so is a `Host` TOW does not answer (DNS rebinding): on this computer only `localhost` and
  loopback addresses, from other devices an IP address of the home network or the VPN, the computer's plain name or
  `name.local`. Writes require a matching `Origin` (CSRF) and a stated size (forms at most 512 KiB); pages are
  served with a strict Content-Security-Policy. The requests that keep open pages up to date are read-only and
  limited per session of a device and per tab of this computer (`web.rate_burst`, `web.rate_per_sec`).
- **What a device on the network sees.** Errors and diagnostics shown to a device on the network leave out folder
  and file paths and the private part of a link; a user name and password inside a link are hidden from every
  viewer, this computer included. The folders you configured stay visible as settings. Turning network access on
  or off, Site access connections, choosing a network share as a backup folder and accepting a disk as the same one
  (**Same disk — accept**) are done only on this computer.
- **Secrets at rest.** Site logins, client passwords, messenger tokens, cookies and the password record are
  encrypted in `data/secrets.enc` with the master key in `keys/master.key`, which no backup or export contains.
  Every start closes the TOW folder to other accounts of the computer, so they can neither change the program
  in it nor open `keys/` and `data/` (Windows: only your account, SYSTEM and Administrators; Linux and macOS:
  `keys/` and `data/` 0700, the folder itself 0700 when every account may write in it), and warns, also in
  `tow doctor`, when it cannot. A folder another account owns (an install made from an administrator terminal)
  is never changed at start; `tow permissions fix`, run once as administrator, gives it to your account and closes
  it. Nightly backups are signed; a modified copy is not restored.
- **Outbound requests.** Site addresses entered in the UI may not point at this computer or the home network;
  download redirects may not leave the site's configured hosts.
  Automatic new-site previews always use a fresh public-only, IP-pinned client with no environment proxy,
  URL credentials or redirects. The explicit `allow_private_tracker_hosts` setting permits private requests
  for saved tracker origins (including their DNS answers); enable it only when those configured origins are
  trusted. It does not grant private-network access to an unsaved-site preview. Public sites on custom ports
  remain supported. Requests to the torrent client and the sign-in browser never go through a system or
  `HTTP_PROXY` proxy, and every request to a site ends within two and a half minutes, connecting included.
- **Optional access providers.** Disabled by default and configured only from this computer. Their API endpoint
  must be a literal loopback/private address (or localhost), with no credentials, proxy or redirects. The service
  receives only an audited public page (root by default, Kinozal's catalog), not account cookies, passwords,
  passkeys or torrent links. Arbitrary probe paths and queries are refused. A bounded,
  validated response is not proof: TOW verifies it through its own same-origin guarded request. Clearance and its
  matching User-Agent are encrypted in the bounded `data/protection.enc` with expiry and durable attempt leases.
- **Backups and transfer files.** Restoring a nightly backup, a restore point or a TOW file, and `tow import`, keep
  this computer's network access, password, backup folders, opt-ins and access providers; only
  `tow import --accept-security-settings` takes the file's. An import made while this computer's saved passwords
  cannot be opened brings in no password for other devices and turns network access off.
- **Your torrents and folders.** TOW changes only torrents it added (tag `tow`) and refuses system and profile
  folders as download or backup targets. A backup is not written to a folder that is no longer on the disk TOW
  recorded for it.
- **Setup and updates.** TOW's uv reads only the install's own `runtime/uv.toml`, never the account's or the
  computer's `uv.toml`, so a package index, mirror or Python source set there for other projects does not reach
  TOW's setup and updates.

## What it does not protect

- Anyone who can use your OS account, or read the TOW folder, can read the master key and therefore the secrets.
  An administrator of the computer can always read them.
- "This computer" means every account on it, not only yours. Another person signed in to the same computer, or a
  program running under another account, opens the TOW page on 127.0.0.1 without a password, like you do. Give TOW
  a computer whose other accounts you trust.
- Wrong passwords from the network lock that device out after 5 failures in ten minutes (30 seconds, doubling with
  each further failure up to an hour) and, after 30 failures in ten minutes from any devices, all of them: someone
  on your home network who keeps guessing can lock your other devices out of the sign-in page for up to 15 minutes
  at a time. This computer needs no password and is not affected.
- TOW ignores `X-Forwarded-For` and similar headers: it judges a request by the address of the connection, so
  behind a reverse proxy every request comes from the proxy's address. A proxy on the same machine makes every
  request look local: TOW asks no password and cannot tell an internet client from you. A proxy on another device
  makes every request need the password, and all of them count as one device when wrong passwords are slowed
  down. Do not run TOW behind a proxy unless the proxy authenticates every request.
- TOW is meant for one computer, a home network or a VPN. It is not hardened for direct exposure to the internet.
- An external access provider operates its own browser, DNS and requests. TOW's public-only IP-pinned transport
  does not constrain that separate process. Run only trusted services, keep their API private and restrict browser
  egress to prevent access to LAN, link-local and cloud-metadata services. TOW does not install or sandbox them.
  Imported Python plugin modules are trusted executable code, not a security boundary.
- A magnet link from a site's page reaches the torrent client only as its info-hashes and the trackers TOW itself
  may contact (public addresses, unless `allow_private_tracker_hosts` is on); its peers and web seeds are left out.
  A `.torrent` file reaches the client as the site gave it: the trackers and web seeds listed inside it are the
  client's to contact, also when they point into your home network.
- Traffic between a device and TOW over the network is plain HTTP: anyone who can watch the home network (a shared
  Wi-Fi, a compromised router) can read the password at sign-in and the session cookie, and that cookie works for
  90 days until the device signs out or you use "Sign out everywhere". Use a VPN (WireGuard, Tailscale) or an SSH
  tunnel on networks you do not fully trust.
- The password reminder is visible to anyone who opens the sign-in page.
