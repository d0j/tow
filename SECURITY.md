# Security policy

## Supported versions

Only the latest stable release receives security fixes. Update to it before reporting a problem
with an older release.

| Version | Supported |
|---|---|
| Latest stable release | yes |
| Older releases | no — update with `deploy.ps1 -Ref <tag>` or `tow update --ref <tag>` |

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
  are refused. Writes require a matching `Origin` (CSRF); pages are served with a strict Content-Security-Policy.
- **Secrets at rest.** Site logins, client passwords, messenger tokens, cookies and the password record are
  encrypted in `data/secrets.enc` with the master key in `keys/master.key`, which no backup or export contains.
  Every start closes the TOW folder to other accounts of the computer, so they can neither change the program
  in it nor open `keys/` and `data/` (Windows: only your account, SYSTEM and Administrators; Linux and macOS:
  `keys/` and `data/` 0700, the folder itself 0700 when every account may write in it), and warns, also in
  `tow doctor`, when it cannot. Night copies are signed; a modified copy is not restored.
- **Outbound requests.** Site addresses entered in the UI may not point at this computer or the home network;
  download redirects may not leave the site's configured hosts.
- **Your torrents and folders.** TOW changes only torrents it added (tag `tow`) and refuses system and profile
  folders as download or backup targets.

## What it does not protect

- Anyone who can use your OS account, or read the TOW folder, can read the master key and therefore the secrets.
  An administrator of the computer can always read them.
- "This computer" means every account on it, not only yours. Another person signed in to the same computer, or a
  program running under another account, opens the TOW page on 127.0.0.1 without a password, like you do. Give TOW
  a computer whose other accounts you trust.
- Wrong passwords from the network are slowed per device and, after 30 failures in ten minutes from any devices,
  for all of them: someone on your home network who keeps guessing can lock your other devices out of the sign-in
  page for up to 15 minutes at a time. This computer needs no password and is not affected.
- A reverse proxy on the same machine makes every request look local, so TOW asks no password. Do not run TOW behind
  one unless the proxy authenticates every request.
- TOW is meant for one computer, a home network or a VPN. It is not hardened for direct exposure to the internet.
- Traffic between a device and TOW over the network is plain HTTP: anyone who can watch the home network (a shared
  Wi-Fi, a compromised router) can read the password at sign-in and the session cookie, and that cookie works for
  90 days until the device signs out or you use "Sign out everywhere". Use a VPN (WireGuard, Tailscale) or an SSH
  tunnel on networks you do not fully trust.
- The password reminder is visible to anyone who opens the sign-in page.
