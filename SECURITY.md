# Security policy

## Supported versions

Only the latest release line receives security fixes.

| Version | Supported |
|---|---|
| 1.21.x | yes |
| < 1.21 | no — update with `deploy.ps1 -Ref <tag>` or `tow update --ref <tag>` |

## Reporting a vulnerability

Please report privately through GitHub: **[Report a vulnerability](https://github.com/d0j/tow/security/advisories/new)**
(the repository's *Security* tab, private vulnerability reporting). Do not open a public issue.

Include the version, the OS, what an attacker needs (same computer, home network, a malicious site page, a
crafted backup file…) and steps to reproduce. Remove real passwords, tokens, cookies and addresses. You will get an
answer as soon as the maintainer can; fixes are released as a patch version and credited unless you prefer
otherwise.

## What TOW protects

- **Access.** Requests from this computer need no password; other devices need the password and a session, and
  only after network access is turned on. Requests from public internet addresses are refused. Writes require a
  matching `Origin` (CSRF); pages are served with a strict Content-Security-Policy.
- **Secrets at rest.** Site logins, client passwords, messenger tokens, cookies and the password record are
  encrypted in `data/secrets.enc` with the master key in `keys/master.key`, which no backup or export contains.
  Night copies are signed; a modified copy is not restored.
- **Outbound requests.** Site addresses entered in the UI may not point at this computer or the home network;
  download redirects may not leave the site's configured hosts.
- **Your torrents and folders.** TOW changes only torrents it added (tag `tow`) and refuses system and profile
  folders as download or backup targets.

## What it does not protect

- Anyone who can use your OS account, or read the TOW folder, can read the master key and therefore the secrets.
- A reverse proxy on the same machine makes every request look local, so TOW asks no password. Do not run TOW behind
  one unless the proxy authenticates every request.
- TOW is meant for one computer, a home network or a VPN. It is not hardened for direct exposure to the internet.
- Traffic between a device and TOW over the network is plain HTTP; use a VPN (WireGuard, Tailscale) or an SSH tunnel
  on untrusted networks.
- The password reminder is visible to anyone who opens the sign-in page.
