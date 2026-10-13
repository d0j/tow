# Site access

Ordinary HTTP, configured mirrors and account sign-in work without an external service. The optional access layer
is disabled when `protection` is omitted. It does not install a browser, change your system proxy or download media.

## Connecting an optional service

1. Run a trusted [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) or
   [Byparr](https://github.com/ThePhaseless/Byparr) service separately. Keep its API private and isolate browser egress
   from LAN, link-local and cloud metadata destinations. The browser is outside TOW's network sandbox.
2. On the TOW computer, open the local web address → Settings → Site access. Expand the desired connection, enter
   its loopback/private IP address and choose its order. Only `/v1` is accepted; localhost becomes 127.0.0.1.
3. Enable that connection and the overall access layer, then Save. Saving does not contact or validate the service;
   the status remains unverified until a real applying check has a reason to use it.

Multiple connections of one kind are supported. Lower order numbers run first. A missing module, disabled
connection, refused response or failed attempt does not prevent another enabled connection from being considered
within the same finite attempt/time budget. Nothing promises successful access to every protection scheme.

On Sites → the site's advanced options, **Automatic** permits the optional layer; **Off** disables it for that site.
**Sign in through the browser** uses the existing explicit browser-auth flow instead: external providers do not
replace that session's User-Agent. It is not an unattended browser solver.

## What a check does

1. Ask ordinary mirrors, with any still-valid previously verified access for the exact origin.
2. Only when ordinary mirrors failed with a detected Cloudflare browser challenge, try enabled providers in order.
3. Send the provider only the public origin's root URL: never a password, account cookie, topic, passkey or download.
4. Validate the bounded response and replay that root from TOW using the returned clearance and matching User-Agent.
5. Fetch the requested resource through TOW's existing guarded HTTP, with normal account sign-in if needed.

Cloudflare's [`cf-mitigated: challenge`](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/)
header is definitive evidence. Known check-page titles with their challenge status are a fallback; an ordinary
Cloudflare proxy, embedded script or forum quotation is not. Access bans, rate limits, origin outages, maintenance,
quota and account sign-in are separate reasons, not instructions to call a solver. Preview and diagnostics never
invoke one. Cross-origin download redirects use a fresh cookie-free client; parent-domain metadata cannot grant
cross-origin credential reuse.

An encrypted `data/protection.enc` stores verified clearance, matching User-Agent, expiry and durable cooldown leases.
Defaults: 30 seconds for the recovery chain, at most 2 available providers, 1,800 seconds before another attempt.
The lease is written before calling the service, so a crash does not cause an attempt on every tick. A configuration
change invalidates the old clearance but does not erase cooldowns. Disabling the layer immediately prevents its use
in subsequent requests; an already issued request cannot be recalled.

If that store is unreadable, ordinary HTTP remains available and the damaged file is not silently replaced. On the
TOW computer, Settings → Site access → Recovery → **Reset saved access** explicitly moves the old encrypted bytes
to TOW's private temporary folder, until normal cleanup. It does not remove site passwords or service settings;
reset is refused while an applying check is active.

## Current revision without another download

A site may expose `infohash_path` with `{id}`. Kinozal's preset supplies its service-information page. Only a unique
labelled current hash is accepted. If it matches the stored revision, the client must independently confirm the
owned torrent, path and verified selection before TOW skips downloading its `.torrent`. Progress reconciliation
still runs. Unknown format or a missing optional endpoint falls back to the normal download; a host-level failure
is reported without repeating the same failing requests. A changed rule, missing/foreign/pending task or changed
hash takes the full client-confirmed path. This is not a quota bypass or proof from a cached old torrent.

## Limits and troubleshooting

- A service's `status: ok` is not proof; only TOW's independent replay can verify access.
- Root-only recovery cannot solve a challenge restricted to one particular download path. Interactive captchas
  require the owner's action. Browser/TLS fingerprints or a different public IP can make replay fail safely.
- TOW bounds socket operations and response processing; synchronous OS DNS cannot be interrupted. A late answer
  is refused before use, but a stalled OS resolver can exceed the nominal wall-clock budget.
- Tracker HTTP advertises only gzip, deflate and identity. Compressed wire bytes, each decoding layer and the final
  body share bounded size limits; invalid/truncated or unsupported coding is refused, not decoded without a bound.
  Empty responses, including redirects and HEAD, remain supported. Nonempty compressed streams with bad CRC,
  truncation or trailing garbage are intentionally refused even when a more tolerant client would accept them.
- A malformed settings file remains a configuration error. An unavailable configured service does not prevent
  ordinary tracker operations. In-process extensions are trusted code, not sandboxed.
- The last observed mirror response is not proof that every endpoint on that mirror works. A healthy front page
  can coexist with a broken download endpoint; read the topic's actual error.

Contracts and extension tests: [EXTENDING.md](EXTENDING.md#site-access-provider). Security boundary:
[SECURITY.md](../SECURITY.md).
